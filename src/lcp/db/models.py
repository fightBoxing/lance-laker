"""SQLAlchemy ORM models for the LCP state schema.

Only models that are actively used by handlers are declared here.  The full
DDL (8 tables) is the source of truth: ``docs/architecture/ddl/lcp_state_schema.sql``.

Style: SQLAlchemy 2.0 declarative + ``Mapped[...]`` / ``mapped_column`` so that
type-checkers see the same types the runtime sees.  All time columns are
timezone-naive ``DATETIME(3)`` to match the DDL exactly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# SQLite's ROWID alias only fires for ``INTEGER PRIMARY KEY``.  Production
# uses BigInteger; in unit tests we degrade to plain Integer for sqlite so
# auto-increment still works without forcing the test to provide ``id``.
_PK_BIGINT = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """Shared declarative base; keeps metadata in one place."""


class Dataset(Base):
    """Mirror of the ``dataset`` table.

    Business key is ``dataset_uuid``; the auto-increment ``id`` is internal
    and never returned over the API.  ``tenant_id`` is mandatory because the
    RLS hook in :mod:`lcp.db.rls` filters every query by it.
    """

    __tablename__ = "dataset"

    id: Mapped[int] = mapped_column(_PK_BIGINT, primary_key=True, autoincrement=True)
    dataset_uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    catalog: Mapped[str] = mapped_column(String(128), nullable=False)
    db_schema: Mapped[str] = mapped_column(String(128), nullable=False)
    table_name: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fragment_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    index_coverage: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0000"),
    )
    latest_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # NOTE: `func.now()` resolves to UTC_TIMESTAMP on MySQL and CURRENT_TIMESTAMP
    # on SQLite; both are acceptable for the skeleton.  Production MySQL falls
    # back to the table-level ``DEFAULT CURRENT_TIMESTAMP(3)`` from the DDL.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Tiny ``__repr__`` helps when debugging tests.
    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Dataset uuid={self.dataset_uuid} status={self.status}>"


class Task(Base):
    """Mirror of the ``task`` table.

    Tasks are submitted by users / schedulers and consumed by background
    workers.  The state machine is enforced in :mod:`lcp.services.task_service`,
    not in the model, so the column type stays a plain ``VARCHAR`` (matching
    the DDL) rather than a SQL-level ENUM.  This lets us add new states
    without an online DDL migration.
    """

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(_PK_BIGINT, primary_key=True, autoincrement=True)
    task_uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default",
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    # ``priority`` follows the DDL: 0 (highest) .. 9 (lowest); 5 is default.
    # ``SmallInteger`` is enough for [0, 9] and uses 2 bytes on MySQL.
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)
    progress: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0000"),
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The ``idempotency_key`` is unique across the whole table (per DDL).
    # Service layer turns the resulting IntegrityError into a 200/202 reply
    # that returns the existing task instead of creating a duplicate.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True,
    )
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Task uuid={self.task_uuid} type={self.task_type} status={self.status}>"


class Index(Base):
    """Mirror of the ``vector_index`` table.

    Unlike :class:`Dataset` and :class:`Task`, this table has **no**
    ``tenant_id`` column: the DDL deliberately leaves tenancy implicit and
    relies on the ``dataset_uuid`` foreign key.  The service layer enforces
    isolation by always loading the parent ``Dataset`` first (which IS RLS-
    filtered) before touching the index, so a cross-tenant ``dataset_uuid``
    is rejected at the dataset lookup with 404.

    Business uniqueness is ``(dataset_uuid, index_name)`` (per DDL
    ``uk_dataset_index_name``); ``id`` is internal only.
    """

    __tablename__ = "vector_index"
    __table_args__ = (
        # Mirrors DDL ``UNIQUE KEY uk_dataset_index_name``; required for
        # IntegrityError on duplicate insert in both MySQL and sqlite tests.
        UniqueConstraint("dataset_uuid", "index_name", name="uk_dataset_index_name"),
    )

    id: Mapped[int] = mapped_column(_PK_BIGINT, primary_key=True, autoincrement=True)
    dataset_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    index_name: Mapped[str] = mapped_column(String(128), nullable=False)
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    index_type: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="BUILDING")
    coverage: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0.0000"),
    )
    fragment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delta_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_optimized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True,
    )
    last_merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"<Index dataset={self.dataset_uuid} name={self.index_name} status={self.status}>"


class LifecyclePolicy(Base):
    """Mirror of the ``lifecycle_policy`` table.

    Like :class:`Index`, this table has **no** ``tenant_id`` column: tenancy is
    enforced via the parent :class:`Dataset` lookup in the service layer
    (``dataset_service.get_dataset`` is RLS-filtered, so a wrong tenant
    yields ``DatasetNotFoundError`` and the policy row is never reached).

    Business uniqueness is ``(dataset_uuid, policy_name)`` per DDL
    ``uk_dataset_policy``; declarative requires the explicit composite
    ``UniqueConstraint`` to enforce that on inserts (the same lesson learned
    on :class:`Index`).
    """

    __tablename__ = "lifecycle_policy"
    __table_args__ = (
        # Mirrors DDL ``UNIQUE KEY uk_dataset_policy``.
        UniqueConstraint(
            "dataset_uuid", "policy_name", name="uk_dataset_policy",
        ),
    )

    id: Mapped[int] = mapped_column(_PK_BIGINT, primary_key=True, autoincrement=True)
    dataset_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tier_rules: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # ``ttl_days`` is nullable: NULL means "never delete" (DDL contract).
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compaction_threshold: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True,
    )
    index_optimize_cron: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    # MySQL stores ``enabled`` as TINYINT(1); SQLAlchemy ``Boolean`` maps
    # cleanly on both MySQL and SQLite, so no ``with_variant`` is needed.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"<LifecyclePolicy dataset={self.dataset_uuid} "
            f"name={self.policy_name} enabled={self.enabled}>"
        )


# Re-export common SQLAlchemy types so callers can ``from lcp.db.models import``
# only what they need without pulling in the full SQLAlchemy namespace.
__all__ = ["Base", "Dataset", "Index", "LifecyclePolicy", "Task"]
