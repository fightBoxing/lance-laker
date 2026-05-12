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
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    String,
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


# Re-export common SQLAlchemy types so callers can ``from lcp.db.models import``
# only what they need without pulling in the full SQLAlchemy namespace.
__all__ = ["Base", "Dataset", "SmallInteger"]
