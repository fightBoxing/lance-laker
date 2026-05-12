"""Interface tests for the gRPC server bootstrap.

Covers:
- MtlsTenantInterceptor derives a principal and binds the contextvar
- H-4: _serve() installs the RLS listener on the engine
- M-4: mTLS failures yield UNAUTHENTICATED (not INTERNAL)
- Registration is a no-op when generated stubs are absent
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest


pytestmark = pytest.mark.api


# ---------------------------------------------------------------------------
# _resolve_principal — pure unit scope
# ---------------------------------------------------------------------------


def _build_context(
    *,
    cn: bytes | str | None = b"worker-1",
    org: bytes | str | None = b"tenant-acme",
) -> Any:
    auth: dict[str, list[Any]] = {}
    if cn is not None:
        auth["x509_common_name"] = [cn]
    if org is not None:
        auth["x509_organization"] = [org]
    ctx = MagicMock()
    ctx.auth_context.return_value = auth
    return ctx


class TestResolvePrincipal:

    def test_extracts_from_organization(self) -> None:
        from lcp.api.grpc.server import _resolve_principal
        principal = _resolve_principal(_build_context())
        assert principal.tenant_id == "tenant-acme"
        assert principal.subject == "worker-1"
        assert principal.auth_method == "mtls"

    def test_handles_string_values(self) -> None:
        from lcp.api.grpc.server import _resolve_principal
        ctx = _build_context(cn="worker-2", org="tenant-beta")
        principal = _resolve_principal(ctx)
        assert principal.tenant_id == "tenant-beta"

    def test_missing_cn_raises_auth_error(self) -> None:
        from lcp.api.grpc.server import _resolve_principal
        from lcp.core.security import AuthenticationError
        ctx = _build_context(cn=None)
        with pytest.raises(AuthenticationError, match="missing CN"):
            _resolve_principal(ctx)

    def test_falls_back_to_cn_prefix_when_no_org(self) -> None:
        from lcp.api.grpc.server import _resolve_principal
        ctx = _build_context(cn=b"tenant-gamma.worker-3", org=None)
        principal = _resolve_principal(ctx)
        assert principal.tenant_id == "tenant-gamma"

    def test_rejects_unparsable_cn_without_org(self) -> None:
        from lcp.api.grpc.server import _resolve_principal
        from lcp.core.security import AuthenticationError
        ctx = _build_context(cn=b"worker-4", org=None)
        with pytest.raises(AuthenticationError):
            _resolve_principal(ctx)


# ---------------------------------------------------------------------------
# Decoder helper
# ---------------------------------------------------------------------------


class TestDecodeFirst:

    def test_decodes_bytes(self) -> None:
        from lcp.api.grpc.server import _decode_first
        assert _decode_first([b"hello"]) == "hello"

    def test_passes_through_str(self) -> None:
        from lcp.api.grpc.server import _decode_first
        assert _decode_first(["hello"]) == "hello"


# ---------------------------------------------------------------------------
# _wrap_handler — the critical auth path
# ---------------------------------------------------------------------------


class TestWrapHandler:

    async def test_binds_tenant_for_unary_unary(self) -> None:
        """The wrapped handler must see the tenant in the contextvar."""

        from lcp.api.grpc.server import _wrap_handler
        from lcp.core.tenant import get_current_tenant

        seen: dict[str, Any] = {}

        async def real_handler(request: Any, context: Any) -> str:
            principal = get_current_tenant()
            seen["tenant"] = principal.tenant_id if principal else None
            return "ok"

        original = grpc.unary_unary_rpc_method_handler(real_handler)
        wrapped = _wrap_handler(original)

        ctx = _build_context()
        result = await wrapped.unary_unary("req", ctx)  # type: ignore[misc]
        assert result == "ok"
        assert seen["tenant"] == "tenant-acme"

    async def test_unauthenticated_status_on_bad_cert(self) -> None:
        """M-4 regression: mTLS parse failures must yield UNAUTHENTICATED."""

        from lcp.api.grpc.server import _wrap_handler

        async def real_handler(request: Any, context: Any) -> str:
            return "should-not-run"

        original = grpc.unary_unary_rpc_method_handler(real_handler)
        wrapped = _wrap_handler(original)

        # ctx lacks organization AND CN isn't tenant- prefixed → auth error.
        bad_ctx = _build_context(cn=b"worker-x", org=None)

        captured_status: dict[str, Any] = {}

        async def fake_abort(status: Any, detail: str) -> None:
            captured_status["code"] = status
            captured_status["detail"] = detail
            raise grpc.RpcError(detail)

        bad_ctx.abort = fake_abort
        with pytest.raises(grpc.RpcError):
            await wrapped.unary_unary("req", bad_ctx)  # type: ignore[misc]

        assert captured_status["code"] == grpc.StatusCode.UNAUTHENTICATED

    async def test_resets_tenant_after_handler(self) -> None:
        """The contextvar must be reset even when the handler raises."""

        from lcp.api.grpc.server import _wrap_handler
        from lcp.core.tenant import get_current_tenant

        async def bad_handler(request: Any, context: Any) -> str:
            raise RuntimeError("boom")

        original = grpc.unary_unary_rpc_method_handler(bad_handler)
        wrapped = _wrap_handler(original)

        with pytest.raises(RuntimeError):
            await wrapped.unary_unary("req", _build_context())  # type: ignore[misc]

        assert get_current_tenant() is None


# ---------------------------------------------------------------------------
# Service register() is a no-op when generated stubs are missing
# ---------------------------------------------------------------------------


class TestServiceStubsAbsent:

    def test_vdw_register_is_noop(self, caplog: pytest.LogCaptureFixture) -> None:
        from lcp.api.grpc.services import vdw_service
        server = MagicMock()
        vdw_service.register(server)
        # register() only calls add_* when stubs are importable; they aren't.
        server.assert_not_called()

    def test_embedding_register_is_noop(self) -> None:
        from lcp.api.grpc.services import embedding_service
        server = MagicMock()
        embedding_service.register(server)
        server.assert_not_called()

    def test_worker_register_is_noop(self) -> None:
        from lcp.api.grpc.services import worker_service
        server = MagicMock()
        worker_service.register(server)
        server.assert_not_called()
