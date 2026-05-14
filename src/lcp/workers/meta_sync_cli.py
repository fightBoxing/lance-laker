"""CLI entry-point for meta-sync -- one reconcile pass, then exit.

Used by the k8s CronJob (``deploy/k8s/50-meta-sync-cronjob.yaml``).
Kept thin: all reconcile logic lives in
:mod:`lcp.services.meta_sync_service`; this module just wires up an
engine, a Gravitino client, parses CLI flags, and runs one pass under
a system principal.

Run with::

    python -m lcp.workers.meta_sync_cli                # ALL, push enabled
    python -m lcp.workers.meta_sync_cli --scope SCHEMA --schema public
    python -m lcp.workers.meta_sync_cli --no-push      # pull-only
    python -m lcp.workers.meta_sync_cli --dataset-uuid <uuid>

Exit codes
----------
- ``0`` -- reconcile finished; per-dataset failures are logged but do
  not flip the exit code, so a single missing fileset does not poison
  the entire CronJob history.  Hard infrastructure errors (DB
  unreachable, Gravitino auth wrong) still return non-zero.
- ``1`` -- unrecoverable error during set-up or teardown.
- ``2`` -- bad CLI arguments (caught by argparse).

Why exit-0 even when some datasets failed:
    Cron retries inflict more pain than they cure when the failure is
    "this one dataset is misconfigured".  We surface failures via logs
    and via the per-item ``status`` so a human can act, but we do not
    block the whole pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from lcp.core.config import get_settings
from lcp.core.tenant import with_system_context
from lcp.db.rls import install_rls_listener
from lcp.integrations.gravitino import GravitinoClient
from lcp.services import dataset_service, meta_sync_service

_LOGGER = logging.getLogger("lcp.workers.meta_sync_cli")

# Vocabulary mirrors lcp.schemas.meta.MetaSyncRequest so REST and CLI
# stay in lock-step.  CATALOG is reserved in OpenAPI; the CLI refuses
# it for the same reason the REST layer does.
_VALID_SCOPES = ("ALL", "SCHEMA", "DATASET")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Return the argparse parser; broken out so tests can call it directly."""

    parser = argparse.ArgumentParser(
        prog="lcp-meta-sync",
        description=(
            "Reconcile LCP dataset rows against Gravitino filesets. "
            "Designed to be invoked by a k8s CronJob; safe to re-run."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=_VALID_SCOPES,
        default="ALL",
        help="Reconcile scope (default: ALL).",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Required when --scope=SCHEMA.",
    )
    parser.add_argument(
        "--dataset-uuid",
        default=None,
        help="Required when --scope=DATASET.",
    )

    push_group = parser.add_mutually_exclusive_group()
    push_group.add_argument(
        "--push",
        dest="push_properties",
        action="store_true",
        help=(
            "Write LCP operational state back to fileset.properties "
            "(default)."
        ),
    )
    push_group.add_argument(
        "--no-push",
        dest="push_properties",
        action="store_false",
        help="Pull only; skip the property write-back.",
    )
    parser.set_defaults(push_properties=True)

    return parser


def _validate_args(args: argparse.Namespace) -> None:
    """Cross-field validation; raises ``SystemExit`` with code 2 on failure.

    Done after argparse so the error messages are uniform.
    """

    if args.scope == "SCHEMA" and not args.schema:
        raise SystemExit("error: --scope=SCHEMA requires --schema")
    if args.scope == "DATASET" and not args.dataset_uuid:
        raise SystemExit("error: --scope=DATASET requires --dataset-uuid")


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    """One reconcile pass, set-up and tear-down inclusive."""

    settings = get_settings()

    # Fail fast when meta-sync is implicitly disabled.  Cron jobs run
    # in environments without Gravitino (e.g. dev clusters) and an
    # empty URL must not cause crash-loops.
    if not settings.gravitino_url:
        _LOGGER.warning(
            "meta-sync: LCP_GRAVITINO_URL is empty; nothing to do",
        )
        return 0

    engine = create_async_engine(
        settings.db_dsn,
        future=True,
        echo=False,
        pool_pre_ping=True,
    )
    install_rls_listener(engine.sync_engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        with with_system_context():
            async with factory() as session:
                async with GravitinoClient.from_settings(settings) as client:
                    report = await _reconcile(session, client, args)
        _LOGGER.info(
            "meta-sync: total=%d synced=%d changed=%d pushed=%d "
            "missing=%d errors=%d",
            report.total,
            report.synced,
            report.changed,
            report.pushed,
            report.missing,
            report.errors,
        )
        # Per-dataset errors are logged at WARN below but do not flip
        # the exit code; see module docstring for the rationale.
        for item in report.items:
            if item.status != "synced":
                _LOGGER.warning(
                    "meta-sync: dataset=%s status=%s error=%s",
                    item.dataset_uuid,
                    item.status,
                    item.error,
                )
        return 0
    finally:
        await engine.dispose()


async def _reconcile(
    session,  # type: ignore[no-untyped-def]  # AsyncSession; loose for tests
    client,  # type: ignore[no-untyped-def]   # GravitinoClient or stub
    args: argparse.Namespace,
) -> meta_sync_service.SyncReport:
    """Dispatch to the right service entry-point based on ``args.scope``."""

    if args.scope == "DATASET":
        # Replicates the router's lookup-then-reconcile sequence so the
        # CLI surfaces "dataset not found" as a SyncReport miss rather
        # than a stack trace.
        try:
            ds = await dataset_service.get_dataset(session, args.dataset_uuid)
        except dataset_service.DatasetNotFoundError:
            _LOGGER.warning(
                "meta-sync: dataset %s not found in LCP",
                args.dataset_uuid,
            )
            return meta_sync_service.SyncReport()
        outcome = await meta_sync_service.reconcile_dataset(
            session,
            client,
            ds,
            push_properties=args.push_properties,
        )
        report = meta_sync_service.SyncReport()
        report.record(outcome)
        return report

    if args.scope == "SCHEMA":
        return await meta_sync_service.reconcile_all(
            session,
            client,
            schema=args.schema,
            push_properties=args.push_properties,
        )

    # ALL
    return await meta_sync_service.reconcile_all(
        session,
        client,
        schema=None,
        push_properties=args.push_properties,
    )


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Entry-point usable from CLI and tests.

    ``argv`` defaults to :data:`sys.argv[1:]` when None.  Tests pass
    explicit lists to avoid leaking process args into argparse.
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(args)

    try:
        return asyncio.run(_run(args))
    except Exception:
        # Log the full traceback so the failed Pod's logs explain *why*.
        _LOGGER.exception("meta-sync run failed")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
