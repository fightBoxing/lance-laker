"""Unit tests for ``lcp.core.tenant``."""

from __future__ import annotations

import asyncio

import pytest

from lcp.core.tenant import (
    TenantPrincipal,
    get_current_tenant,
    require_current_tenant,
    reset_current_tenant,
    set_current_tenant,
)


pytestmark = pytest.mark.unit


def _make_principal(tenant: str = "acme", sub: str = "u-1", method: str = "oidc") -> TenantPrincipal:
    return TenantPrincipal(tenant_id=tenant, subject=sub, auth_method=method)


class TestSetAndGet:

    def test_returns_none_when_unset(self) -> None:
        assert get_current_tenant() is None

    def test_set_and_reset_round_trip(self) -> None:
        assert get_current_tenant() is None
        token = set_current_tenant(_make_principal("acme"))
        try:
            current = get_current_tenant()
            assert current is not None
            assert current.tenant_id == "acme"
        finally:
            reset_current_tenant(token)
        assert get_current_tenant() is None


class TestRequireCurrentTenant:

    def test_raises_when_unset(self) -> None:
        with pytest.raises(RuntimeError, match="No tenant principal"):
            require_current_tenant()

    def test_returns_when_set(self) -> None:
        token = set_current_tenant(_make_principal("acme"))
        try:
            assert require_current_tenant().tenant_id == "acme"
        finally:
            reset_current_tenant(token)


class TestPrincipalIsImmutable:

    def test_dataclass_frozen(self) -> None:
        p = _make_principal()
        with pytest.raises((AttributeError, Exception)):
            p.tenant_id = "mutated"  # type: ignore[misc]


class TestAsyncIsolation:
    """Regression: tenants set in one task must not leak into another."""

    async def test_contextvar_isolates_concurrent_tasks(self) -> None:
        observed: list[str | None] = []
        started = asyncio.Event()
        can_read = asyncio.Event()

        async def worker(tenant: str, bucket_idx: int) -> None:
            token = set_current_tenant(_make_principal(tenant))
            try:
                observed.append(None)
                observed[bucket_idx] = get_current_tenant().tenant_id  # type: ignore[union-attr]
                started.set()
                await can_read.wait()
                # Still isolated after awaits.
                observed[bucket_idx] = get_current_tenant().tenant_id  # type: ignore[union-attr]
            finally:
                reset_current_tenant(token)

        t1 = asyncio.create_task(worker("t1", 0))
        t2 = asyncio.create_task(worker("t2", 1))
        await started.wait()
        can_read.set()
        await asyncio.gather(t1, t2)

        # Each task only ever saw its own tenant.
        assert observed == ["t1", "t2"]
