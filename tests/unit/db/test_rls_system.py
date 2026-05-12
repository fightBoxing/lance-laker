"""Unit tests for the RLS bypass when running under a system principal."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select

from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
    with_system_context,
)
from lcp.db.rls import _before_execute, _find_rls_targets, _inject_tenant_filter


@pytest.fixture
def tables() -> dict[str, Table]:
    """Build a tiny in-memory schema with one RLS-protected table."""

    md = MetaData()
    dataset = Table(
        "dataset",
        md,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", String(64)),
    )
    return {"dataset": dataset}


class TestSystemPrincipalBypass:

    def test_system_principal_skips_filter(
        self, tables: dict[str, Table],
    ) -> None:
        """Under with_system_context() the hook must not inject tenant_id."""

        with with_system_context():
            stmt = select(tables["dataset"])
            rewritten, _params, _multi = _before_execute(
                None, stmt, (), {}, _Context(),
            )
            sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
            # Bypass means: no WHERE clause was added by the hook.
            assert "WHERE" not in sql.upper()

    def test_regular_principal_still_filters(
        self, tables: dict[str, Table],
    ) -> None:
        """A normal tenant principal must still receive the WHERE clause."""

        token = set_current_tenant(
            TenantPrincipal(
                tenant_id="acme", subject="alice", auth_method="oidc",
            ),
        )
        try:
            stmt = select(tables["dataset"])
            rewritten, _params, _multi = _before_execute(
                None, stmt, (), {}, _Context(),
            )
            sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
            assert "WHERE" in sql.upper()
            assert "acme" in sql
        finally:
            reset_current_tenant(token)

    def test_system_context_restores_previous(
        self, tables: dict[str, Table],
    ) -> None:
        """Exiting with_system_context() must restore the previous principal."""

        token = set_current_tenant(
            TenantPrincipal(
                tenant_id="acme", subject="alice", auth_method="oidc",
            ),
        )
        try:
            with with_system_context():
                pass
            # Outside the context: the original tenant must be back.
            stmt = select(tables["dataset"])
            rewritten, _params, _multi = _before_execute(
                None, stmt, (), {}, _Context(),
            )
            sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
            assert "acme" in sql
        finally:
            reset_current_tenant(token)

    def test_helper_targets_resolve(self, tables: dict[str, Table]) -> None:
        """Sanity: the helper functions are still importable and produce sane output."""

        stmt = select(tables["dataset"])
        targets = _find_rls_targets(stmt)
        assert [t.name for t in targets] == ["dataset"]
        rewritten = _inject_tenant_filter(stmt, targets, "acme")
        sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
        assert "acme" in sql


class _Context:
    """Stub of SQLAlchemy's execution context, only the attributes we read."""

    @property
    def execution_options(self) -> dict[str, Any]:
        return {}
