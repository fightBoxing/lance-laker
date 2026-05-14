"""Unit tests for ``lcp.db.rls``.

Most-important regression: C-1 — every RLS-bearing FROM target in a JOIN
must have its ``tenant_id`` predicate qualified by the *target table*,
not by a floating ``column('tenant_id')`` that MySQL resolves as
ambiguous.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, delete, select, update

from lcp.core.tenant import (
    TenantPrincipal,
    reset_current_tenant,
    set_current_tenant,
)
from lcp.db.rls import (
    RLS_PROTECTED_TABLES,
    _before_execute,
    _find_rls_targets,
    _inject_tenant_filter,
    install_rls_listener,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures: build two protected tables + one unprotected
# ---------------------------------------------------------------------------


@pytest.fixture
def tables() -> dict[str, Table]:
    md = MetaData()
    datasets = Table(
        "dataset",
        md,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", String(64)),
        Column("name", String(128)),
    )
    tasks = Table(
        "task",
        md,
        Column("id", Integer, primary_key=True),
        Column("dataset_id", Integer),
        Column("tenant_id", String(64)),
    )
    audit = Table(
        "audit_log",
        md,
        Column("id", Integer, primary_key=True),
        Column("message", String(256)),
    )
    return {"dataset": datasets, "task": tasks, "audit": audit}


# ---------------------------------------------------------------------------
# _find_rls_targets
# ---------------------------------------------------------------------------


class TestFindRlsTargets:

    def test_finds_single_protected_select(self, tables: dict[str, Table]) -> None:
        stmt = select(tables["dataset"])
        targets = _find_rls_targets(stmt)
        assert len(targets) == 1
        assert targets[0].name == "dataset"

    def test_finds_all_protected_targets_in_join(self, tables: dict[str, Table]) -> None:
        """C-1 regression: both RLS tables in the JOIN must be captured."""

        datasets = tables["dataset"]
        tasks = tables["task"]
        stmt = select(datasets, tasks).select_from(
            datasets.join(tasks, datasets.c.id == tasks.c.dataset_id),
        )
        targets = _find_rls_targets(stmt)
        names = sorted(t.name for t in targets)
        assert names == ["dataset", "task"]

    def test_ignores_unprotected_tables(self, tables: dict[str, Table]) -> None:
        stmt = select(tables["audit"])
        assert _find_rls_targets(stmt) == []

    def test_detects_update_target(self, tables: dict[str, Table]) -> None:
        stmt = update(tables["dataset"]).values(name="x")
        targets = _find_rls_targets(stmt)
        assert [t.name for t in targets] == ["dataset"]

    def test_detects_delete_target(self, tables: dict[str, Table]) -> None:
        stmt = delete(tables["task"])
        targets = _find_rls_targets(stmt)
        assert [t.name for t in targets] == ["task"]

    def test_p5_fast_path_select_without_from(self) -> None:
        """P-5: SELECT without FROM must return [] without calling get_final_froms."""

        from sqlalchemy import literal
        # ``select(literal(1))`` compiles to ``SELECT 1`` — no FROM clause.
        stmt = select(literal(1))
        # Must return empty list without error, even with no principal bound.
        targets = _find_rls_targets(stmt)
        assert targets == []


# ---------------------------------------------------------------------------
# _inject_tenant_filter: the C-1 fix
# ---------------------------------------------------------------------------


class TestInjectTenantFilter:

    def test_predicate_is_qualified_by_table(self, tables: dict[str, Table]) -> None:
        """Rendered SQL must be ``dataset.tenant_id = :<bindname>``."""

        stmt = select(tables["dataset"])
        targets = _find_rls_targets(stmt)
        rewritten = _inject_tenant_filter(stmt, targets, "acme")

        sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
        # The qualification prefix is the evidence that C-1 is fixed.
        assert "dataset.tenant_id" in sql
        assert "'acme'" in sql

    def test_join_gets_both_predicates(self, tables: dict[str, Table]) -> None:
        """Both RLS tables must receive their own qualified predicate."""

        datasets = tables["dataset"]
        tasks = tables["task"]
        stmt = select(datasets, tasks).select_from(
            datasets.join(tasks, datasets.c.id == tasks.c.dataset_id),
        )
        targets = _find_rls_targets(stmt)
        rewritten = _inject_tenant_filter(stmt, targets, "acme")

        sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
        assert "dataset.tenant_id" in sql
        assert "task.tenant_id" in sql
        # Must not produce a bare, ambiguous predicate.
        assert " tenant_id = " not in sql

    def test_injects_on_update(self, tables: dict[str, Table]) -> None:
        stmt = update(tables["dataset"]).values(name="x")
        targets = _find_rls_targets(stmt)
        rewritten = _inject_tenant_filter(stmt, targets, "acme")
        sql = str(rewritten.compile(compile_kwargs={"literal_binds": True}))
        assert "dataset.tenant_id" in sql


# ---------------------------------------------------------------------------
# _before_execute
# ---------------------------------------------------------------------------


class TestBeforeExecuteHook:

    def test_fails_closed_without_principal(self, tables: dict[str, Table]) -> None:
        stmt = select(tables["dataset"])
        with pytest.raises(PermissionError, match="without an authenticated tenant"):
            _before_execute(None, stmt, None, None, {})

    def test_passes_through_unprotected_table(self, tables: dict[str, Table]) -> None:
        """Unprotected tables must not require a principal."""

        stmt = select(tables["audit"])
        out_stmt, _, _ = _before_execute(None, stmt, None, None, {})
        assert out_stmt is stmt

    def test_passes_through_non_dml(self) -> None:
        """DDL or raw text must be untouched."""

        from sqlalchemy import text
        stmt = text("CREATE TABLE foo (id INT)")
        out_stmt, _, _ = _before_execute(None, stmt, None, None, {})
        assert out_stmt is stmt

    def test_skips_when_enforce_disabled(
        self,
        tables: dict[str, Table],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LCP_ENFORCE_TENANT_RLS", "false")
        from lcp.core.config import get_settings
        get_settings.cache_clear()
        try:
            stmt = select(tables["dataset"])
            out_stmt, _, _ = _before_execute(None, stmt, None, None, {})
            assert out_stmt is stmt
        finally:
            get_settings.cache_clear()

    def test_injects_when_principal_bound(self, tables: dict[str, Table]) -> None:
        token = set_current_tenant(
            TenantPrincipal(tenant_id="acme", subject="u", auth_method="oidc"),
        )
        try:
            stmt = select(tables["dataset"])
            out_stmt, _, _ = _before_execute(None, stmt, None, None, {})
            sql = str(out_stmt.compile(compile_kwargs={"literal_binds": True}))
            assert "dataset.tenant_id" in sql
            assert "'acme'" in sql
        finally:
            reset_current_tenant(token)


# ---------------------------------------------------------------------------
# install_rls_listener idempotency
# ---------------------------------------------------------------------------


class TestInstallListener:

    def test_listener_is_idempotent(self) -> None:
        from sqlalchemy import create_engine
        engine = create_engine("sqlite://")
        install_rls_listener(engine)
        install_rls_listener(engine)  # must not raise or double-register
        engine.dispose()


class TestProtectedRegistry:

    def test_registry_is_not_empty(self) -> None:
        assert len(RLS_PROTECTED_TABLES) >= 1

    def test_registry_contains_datasets(self) -> None:
        assert "dataset" in RLS_PROTECTED_TABLES
