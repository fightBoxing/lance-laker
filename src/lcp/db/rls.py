"""Application-layer Row-Level Security (RLS).

MySQL 8 has no native row-level security, so we enforce tenant isolation at
two layers:

1. **App-layer (this module)** — A SQLAlchemy ``before_execute`` listener
   inspects every Core SQL statement and rewrites it to add a
   ``<table>.tenant_id = :current_tenant`` predicate when the statement
   targets an RLS-protected table.

2. **DB-layer** — A read-only MySQL account is granted access only to
   ``v_*_tenant`` views that filter by ``CURRENT_USER`` or by a
   session-scoped variable.  See ``docs/architecture/ddl/lcp_rls_views.sql``.

The skeleton ships with the *hook* registration only; the registry of
RLS-enabled tables should be populated as ORM models are introduced in
later phases.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.sql import Delete, Select, Update
from sqlalchemy.sql.expression import ClauseElement
from sqlalchemy.sql.selectable import Join

from lcp.core.config import get_settings
from lcp.core.tenant import get_current_tenant

# Tables that must be filtered by tenant_id; populate as models are added.
# NOTE: Names must match the actual SQL table names in
# ``docs/architecture/ddl/lcp_state_schema.sql`` (singular form).  The
# remaining placeholders (tasks/indexes/compactions/embedding_jobs/
# lifecycle_rules) are kept for forward-compatibility and will be aligned
# with the DDL when their corresponding ORM models are introduced.
RLS_PROTECTED_TABLES: set[str] = {
    "dataset",
    "tasks",
    "indexes",
    "compactions",
    "embedding_jobs",
    "lifecycle_rules",
}


def _iter_leaf_tables(node: Any) -> list[Any]:
    """Walk ``node`` recursively and return all leaf Table-like objects.

    SQLAlchemy 2.x's ``Select.get_final_froms`` returns a ``Join`` object for
    joined selects rather than its constituent tables; the RLS hook must
    descend into each side of the join so that every tenant-bearing table
    receives its own qualified predicate.
    """

    if isinstance(node, Join):
        return _iter_leaf_tables(node.left) + _iter_leaf_tables(node.right)
    return [node]


def _find_rls_targets(stmt: ClauseElement) -> list[Any]:
    """Return the FROM/UPDATE/DELETE targets that belong to RLS tables.

    The returned objects are SQLAlchemy ``Table`` (or table-like) entities,
    so the caller can use ``target.c.tenant_id`` to qualify the column and
    avoid ``Column 'tenant_id' in where clause is ambiguous`` errors when
    the statement joins multiple tenant-bearing tables.
    """

    roots: list[Any] = []
    if isinstance(stmt, Select):
        roots = list(stmt.get_final_froms())
    elif isinstance(stmt, (Update, Delete)):
        target = getattr(stmt, "table", None)
        if target is not None:
            roots = [target]

    leaves: list[Any] = []
    for root in roots:
        leaves.extend(_iter_leaf_tables(root))

    targets: list[Any] = []
    seen: set[int] = set()
    for leaf in leaves:
        name = getattr(leaf, "name", None)
        if name in RLS_PROTECTED_TABLES and hasattr(leaf, "c"):
            if id(leaf) not in seen:
                seen.add(id(leaf))
                targets.append(leaf)
    return targets


def _inject_tenant_filter(
    stmt: ClauseElement,
    targets: list[Any],
    tenant_id: str,
) -> ClauseElement:
    """Append ``<target>.tenant_id = :tenant_id`` for every RLS target.

    When the same statement joins two RLS tables, both predicates are
    AND-ed in.  This is correct because every RLS table is tenant-scoped
    and a cross-tenant join is never legitimate.
    """

    rewritten = stmt
    for target in targets:
        tenant_col = target.c.tenant_id
        if isinstance(rewritten, (Select, Update, Delete)):
            rewritten = rewritten.where(tenant_col == tenant_id)
    return rewritten


def _before_execute(  # noqa: PLR0913 - SQLAlchemy hook signature is fixed
    conn: Any,
    clauseelement: ClauseElement,
    multiparams: Any,
    params: Any,
    execution_options: dict[str, Any],
) -> tuple[ClauseElement, Any, Any]:
    """SQLAlchemy ``before_execute`` listener that injects RLS predicates."""

    del conn, execution_options  # unused, kept for signature compatibility

    settings = get_settings()
    if not settings.enforce_tenant_rls:
        return clauseelement, multiparams, params

    if not isinstance(clauseelement, (Select, Update, Delete)):
        return clauseelement, multiparams, params

    targets = _find_rls_targets(clauseelement)
    if not targets:
        return clauseelement, multiparams, params

    principal = get_current_tenant()
    if principal is None:
        # No principal bound — fail closed.
        raise PermissionError(
            "RLS-protected statement executed without an authenticated "
            "tenant context.",
        )

    rewritten = _inject_tenant_filter(clauseelement, targets, principal.tenant_id)
    return rewritten, multiparams, params


def install_rls_listener(engine: Any) -> None:
    """Register the RLS hook on ``engine`` (idempotent)."""

    if event.contains(engine, "before_execute", _before_execute):
        return
    event.listen(
        engine,
        "before_execute",
        _before_execute,
        retval=True,
    )
