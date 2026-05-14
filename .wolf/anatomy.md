# anatomy.md

> Auto-maintained by OpenWolf. Last scanned: 2026-05-14T04:00:01.242Z
> Files: 516 tracked | Anatomy hits: 0 | Misses: 0

## ./

- `.coverage` (~50245 tok)
- `.dockerignore` — Docker ignore rules (~138 tok)
- `.gitignore` — Git ignore rules (~139 tok)
- `.python-version` (~2 tok)
- `.spectral.yaml` (~440 tok)
- `buf.yaml` (~77 tok)
- `CLAUDE.md` — OpenWolf (~57 tok)
- `coverage.xml` — Declares name (~6805 tok)
- `Dockerfile` — Docker container definition (~718 tok)
- `pyproject.toml` — Python project configuration (~828 tok)
- `pytest-report.xml` (~3136 tok)
- `README-skeleton.md` — LCP Skeleton (Phase-1 Stub) (~484 tok)

## .claude/

- `settings.json` (~441 tok)
- `settings.local.json` (~22 tok)

## .claude/rules/

- `openwolf.md` (~313 tok)

## .claude/worktrees/zesty-giggling-duckling/

- `.dockerignore` — Docker ignore rules (~138 tok)
- `.gitignore` — Git ignore rules (~139 tok)
- `.python-version` (~2 tok)
- `.spectral.yaml` (~440 tok)
- `buf.yaml` (~77 tok)
- `CLAUDE.md` — OpenWolf (~57 tok)
- `Dockerfile` — Docker container definition (~607 tok)
- `pyproject.toml` — Python project configuration (~828 tok)
- `README-skeleton.md` — LCP Skeleton (Phase-1 Stub) (~484 tok)

## .claude/worktrees/zesty-giggling-duckling/.claude/

- `settings.json` (~441 tok)
- `settings.local.json` (~275 tok)

## .claude/worktrees/zesty-giggling-duckling/.claude/rules/

- `openwolf.md` (~313 tok)

## .claude/worktrees/zesty-giggling-duckling/.github/workflows/

- `api-lint.yml` — CI: API Lint (OpenAPI + Protobuf) (~581 tok)
- `tests.yml` — CI: Tests (pytest + coverage) (~743 tok)

## .claude/worktrees/zesty-giggling-duckling/.pytest_cache/

- `.gitignore` — Git ignore rules (~10 tok)
- `CACHEDIR.TAG` (~51 tok)
- `README.md` — Project documentation (~76 tok)

## .claude/worktrees/zesty-giggling-duckling/.pytest_cache/v/cache/

- `lastfailed` (~1 tok)
- `nodeids` (~5206 tok)

## .claude/worktrees/zesty-giggling-duckling/deploy/k8s/

- `00-namespace-config.yaml` — Namespace, secrets, configmap for the LCP control plane. (~586 tok)
- `10-api-deployment.yaml` — REST API Deployment. (~714 tok)
- `20-worker-deployment.yaml` — Lifecycle worker Deployment. (~557 tok)
- `30-planner-cronjob.yaml` — Planner CronJob -- one Pod per minute, each runs `plan_once` and exits. (~499 tok)

## .claude/worktrees/zesty-giggling-duckling/docs/architecture/

- `LCP-API-DDL-Sequence-Delivery-Report.md` — LCP 接口契约 / 数据模型 / 运维时序补充交付报告 (~1733 tok)
- `LCP-Architecture-Design.md` — 目录 (~3628 tok)
- `LCP-Datasets-CRUD-MySQL-Delivery-Report.md` — LCP — Datasets CRUD 接 MySQL（本地 K8s）交付报告 (~2747 tok)
- `LCP-Indexes-CRUD-MySQL-Delivery-Report.md` — LCP — Indexes CRUD 接 MySQL（本地 K8s）交付报告 (~2324 tok)
- `LCP-K8s-Deployment-Smoke-Report.md` — LCP k8s Deployment Dry-run + Lance/MinIO Smoke Report (~2613 tok)
- `LCP-Lifecycle-CRUD-MySQL-Delivery-Report.md` — LCP — Lifecycle Policies CRUD 接 MySQL（本地 K8s）交付报告 (~2315 tok)
- `LCP-Lifecycle-Worker-E2E-Delivery-Report.md` — LCP — Lifecycle Worker 接入（首个端到端业务闭环）交付报告 (~3066 tok)
- `LCP-Lifecycle-Worker-Real-Executor-Delivery-Report.md` — LCP — Lifecycle Worker (Real Executor) 交付报告 (~2972 tok)
- `LCP-Scheduler-Primitives-MySQL-Delivery-Report.md` — LCP — Scheduler Primitives 接 MySQL（本地 K8s）交付报告 (~3015 tok)
- `LCP-Skeleton-Code-Review.md` — LCP Phase-1 Skeleton — Critical Code Review (挑刺式 Review) (~5895 tok)
- `LCP-Skeleton-Delivery-Report.md` — LCP Phase-1 Skeleton — Delivery Report (~2448 tok)
- `LCP-Skeleton-Fix-Report.md` — LCP Skeleton — Code Review Fix Report (~3099 tok)
- `LCP-Tasks-CRUD-MySQL-Delivery-Report.md` — LCP — Tasks CRUD 接 MySQL（本地 K8s）交付报告 (~2565 tok)
- `LCP-Test-CI-Integration-Report.md` — LCP Skeleton — pytest CI Integration Report (~2298 tok)
- `LCP-Test-Coverage-Delivery-Report.md` — LCP Skeleton — Test Coverage Delivery Report (~2682 tok)
- `LCP-UvLock-Frozen-CI-Report.md` — LCP — `uv.lock` Generation & CI Frozen-Sync Integration (~2598 tok)
- `LCP-Vectorization-CRUD-MySQL-Delivery-Report.md` — LCP — Vectorization Rule CRUD 接 MySQL（本地 K8s）交付报告 (~2421 tok)

## .claude/worktrees/zesty-giggling-duckling/docs/architecture/api/openapi/

- `lcp-control-api.yaml` (~2000 tok)
- `lcp-index-api.yaml` (~1419 tok)
- `lcp-lifecycle-api.yaml` (~2165 tok)
- `lcp-meta-api.yaml` (~1357 tok)
- `lcp-task-api.yaml` — Declares in (~1804 tok)
- `lcp-vectorization-api.yaml` — Declares is (~2496 tok)

## .claude/worktrees/zesty-giggling-duckling/docs/architecture/api/protobuf/

- `embedding_service.proto` — Embedding service gRPC contract. (~580 tok)
- `lcp_worker.proto` — LCP <-> Worker bidirectional gRPC contract. (~995 tok)
- `vdw_writer.proto` — VDW (Vector Data Writer) gRPC contract. (~630 tok)

## .claude/worktrees/zesty-giggling-duckling/docs/architecture/ddl/

- `lcp_rls_views.sql` — LCP Multi-Tenant Row-Level Security (RLS) — DB-layer fallback (~1377 tok)
- `lcp_state_schema.sql` — LCP State Schema (MySQL 8.0+) (~3871 tok)

## .claude/worktrees/zesty-giggling-duckling/docs/architecture/diagrams/

- `01-overall-layered-architecture.excalidraw` (~7786 tok)
- `02-lcp-internal-modules.excalidraw` (~8019 tok)
- `03-three-planes-separation.excalidraw` (~10398 tok)
- `04-vector-ingestion-sequence.excalidraw` (~11858 tok)
- `05-compaction-flow.excalidraw` (~13383 tok)
- `06-index-rebuild-flow.excalidraw` (~13873 tok)
- `07-lifecycle-recycle-flow.excalidraw` (~13758 tok)

## .claude/worktrees/zesty-giggling-duckling/docs/architecture/security/

- `LCP-Security-Design.md` — LCP Security Design — OAuth2/OIDC + mTLS + Multi-Tenant RLS (~2508 tok)

## .claude/worktrees/zesty-giggling-duckling/scripts/

- `dump_smoke_state.py` — Dump the post-smoke state of the LCP DB (task + worker rows). (~665 tok)
- `k8s_smoke_seed.py` — End-to-end k8s smoke fixture: seed one dataset + lifecycle policy. (~1286 tok)
- `lance_minio_smoke.py` — Lance + MinIO smoke test -- proves the lance data plane is operational. (~1164 tok)
- `local_e2e_smoke_index_build.py` — Block B end-to-end smoke: REST/service -> task -> worker -> lance index. (~3334 tok)
- `local_e2e_smoke_index_optimize.py` — Block C end-to-end smoke: optimize_index -> task -> worker -> lance. (~2524 tok)
- `local_e2e_smoke.py` — End-to-end smoke: dataset + TTL policy + worker execution + MinIO side-effect. (~3765 tok)
- `probe_local_env.py` — One-shot probe: MinIO health + bucket list + MySQL auth/db check. (~638 tok)
- `start_lcp_api_local.sh` — Starts the LCP REST API in the background against the local k8s MinIO + MySQL. (~426 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp.egg-info/

- `dependency_links.txt` (~1 tok)
- `entry_points.txt` (~22 tok)
- `PKG-INFO` (~875 tok)
- `requires.txt` (~124 tok)
- `SOURCES.txt` (~496 tok)
- `top_level.txt` (~1 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/

- `__init__.py` — LCP top-level package. (~15 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/api/

- `__init__.py` — REST API package. (~7 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/api/grpc/

- `__init__.py` — gRPC server package. (~8 tok)
- `server.py` — gRPC server entry-point with mTLS and tenant-binding interceptor. (~2339 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/api/grpc/services/

- `__init__.py` — gRPC service implementations (stubs). (~13 tok)
- `embedding_service.py` — Embedding service stub. (~352 tok)
- `vdw_service.py` — Vector Data Writer (VDW) service stub. (~348 tok)
- `worker_service.py` — LCP Worker service stub. (~600 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/api/rest/

- `__init__.py` — FastAPI REST app package. (~10 tok)
- `auth.py` — OAuth2 / OIDC authentication for the REST surface. (~1303 tok)
- `deps.py` — Reusable FastAPI dependencies. (~222 tok)
- `main.py` — FastAPI application entry-point for LCP REST API. (~796 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/api/rest/routers/

- `__init__.py` — HTTP routers (one per OpenAPI bundle). (~13 tok)
- `datasets.py` — Datasets router. (~1058 tok)
- `indexes.py` — Indexes router. (~1829 tok)
- `lifecycle.py` — Lifecycle policies router. (~2008 tok)
- `meta.py` — Meta sync router (stub). (~555 tok)
- `tasks.py` — Tasks router. (~1421 tok)
- `vectorization.py` — Vectorization-rule router. (~2073 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/core/

- `__init__.py` — Core utilities: configuration, tenant context, security helpers. (~21 tok)
- `config.py` — Application-wide configuration, sourced from environment variables. (~1682 tok)
- `security.py` — Security helpers shared by REST and gRPC stacks. (~2554 tok)
- `tenant.py` — Tenant context propagation across async tasks. (~1346 tok)
- `time.py` — Shared time utilities for the LCP codebase. (~231 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/data_plane/

- `__init__.py` — Data-plane integration layer. (~227 tok)
- `lance_io.py` — Thin wrapper around the ``lance`` python API. (~3057 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/db/

- `__init__.py` — Database layer: async session factory + multi-tenant RLS hooks. (~20 tok)
- `models.py` — SQLAlchemy ORM models for the LCP state schema. (~4590 tok)
- `rls.py` — Application-layer Row-Level Security (RLS). (~1745 tok)
- `session.py` — Async SQLAlchemy session factory. (~692 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/schemas/

- `__init__.py` — Pydantic schemas package for request/response shapes. (~18 tok)
- `dataset.py` — Pydantic request / response schemas for the dataset API. (~703 tok)
- `index.py` — Pydantic request / response schemas for the index API. (~766 tok)
- `lifecycle.py` — Pydantic request / response schemas for the lifecycle policy API. (~625 tok)
- `task.py` — Pydantic request / response schemas for the task API. (~773 tok)
- `vectorization.py` — Pydantic request / response schemas for the vectorization rule API. (~1185 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/services/

- `__init__.py` — Business-logic services package. (~66 tok)
- `dataset_service.py` — CRUD business logic for the ``dataset`` table. (~1368 tok)
- `index_service.py` — Business logic for the ``vector_index`` table. (~2977 tok)
- `lifecycle_planner_service.py` — Lifecycle planner: turn enabled ``lifecycle_policy`` rows into ``task`` rows. (~3401 tok)
- `lifecycle_service.py` — Business logic for the ``lifecycle_policy`` table. (~1725 tok)
- `scheduler_service.py` — Scheduler primitives for worker registration and task dispatch. (~4682 tok)
- `task_service.py` — Business logic for the ``task`` table. (~2458 tok)
- `vectorization_service.py` — Business logic for the ``vectorization_rule`` table. (~1841 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/workers/

- `__init__.py` — Long-running worker processes that consume the task table. (~79 tok)
- `lifecycle_executors.py` — Compatibility shim re-exporting executors from the new sub-package. (~250 tok)
- `lifecycle_planner_cli.py` — CLI entry-point for the lifecycle planner -- one tick, then exit. (~715 tok)
- `lifecycle_worker_service.py` — Lifecycle worker: bridge between scheduler primitives and executors. (~2233 tok)
- `lifecycle_worker.py` — Lifecycle worker daemon -- the long-running process that consumes tasks. (~2276 tok)

## .claude/worktrees/zesty-giggling-duckling/src/lcp/workers/executors/

- `__init__.py` — Lifecycle executor implementations. (~278 tok)
- `base.py` — Executor base classes and the default registry factory. (~1096 tok)
- `compaction.py` — COMPACTION executor: merges small lance fragments into bigger ones. (~933 tok)
- `index_build.py` — INDEX_BUILD executor: builds an ANN index on a dataset column. (~1858 tok)
- `index_optimize.py` — INDEX_OPTIMIZE executor: rebuilds delta indices, then promotes rows. (~1084 tok)
- `ttl_delete.py` — TTL_DELETE executor: prunes rows older than a TTL window. (~1101 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/

- `__init__.py` (~0 tok)
- `conftest.py` — Global pytest fixtures shared by unit and API tests. (~2284 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/api/

- `__init__.py` (~0 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/api/grpc/

- `__init__.py` (~0 tok)
- `test_server.py` — Interface tests for the gRPC server bootstrap. (~1931 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/api/rest/

- `__init__.py` (~0 tok)
- `test_auth.py` — REST auth middleware — interface tests. (~1738 tok)
- `test_routers.py` — Interface tests for the REST routers (smoke + contract level). (~10143 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/fixtures/

- `__init__.py` (~0 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/integration/

- `__init__.py` — Integration tests; require external services (MySQL, Gravitino, etc.). (~22 tok)
- `test_datasets_mysql.py` — Integration tests against the real MySQL instance running inside the local (~1643 tok)
- `test_indexes_mysql.py` — Integration tests for the index service against real MySQL. (~2047 tok)
- `test_lifecycle_e2e_mysql.py` — End-to-end integration tests for the lifecycle planner against MySQL. (~2933 tok)
- `test_lifecycle_mysql.py` — Integration tests for the lifecycle policy service against real MySQL. (~2125 tok)
- `test_lifecycle_worker_e2e_mysql.py` — End-to-end integration test: planner emits, real worker consumes. (~3015 tok)
- `test_scheduler_mysql.py` — Integration tests for the scheduler primitives against real MySQL. (~3104 tok)
- `test_tasks_mysql.py` — Integration tests for the task service against real MySQL. (~1807 tok)
- `test_vectorization_mysql.py` — Integration tests for the vectorization service against real MySQL. (~2339 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/

- `__init__.py` (~0 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/core/

- `__init__.py` (~0 tok)
- `test_config.py` — Unit tests for ``lcp.core.config`` — env overrides + secure defaults. (~937 tok)
- `test_security.py` — Unit tests for ``lcp.core.security``. (~2458 tok)
- `test_tenant.py` — Unit tests for ``lcp.core.tenant``. (~794 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/data_plane/

- `__init__.py` — Unit tests for the data_plane package. (~13 tok)
- `test_lance_io.py` — Unit tests for ``lcp.data_plane.lance_io``. (~3878 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/db/

- `__init__.py` (~0 tok)
- `test_rls_system.py` — Unit tests for the RLS bypass when running under a system principal. (~1023 tok)
- `test_rls.py` — Unit tests for ``lcp.db.rls``. (~2232 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/services/

- `__init__.py` (~0 tok)
- `test_index_service.py` — Unit tests for ``index_service`` task-emission behaviour. (~3695 tok)
- `test_lifecycle_planner_service.py` — Unit tests for the lifecycle planner against in-memory SQLite. (~2795 tok)
- `test_scheduler_service.py` — Unit tests for the scheduler primitives. (~3957 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/workers/

- `__init__.py` — Unit tests for the lifecycle worker package. (~15 tok)
- `test_lifecycle_executors.py` — Unit tests for the three lifecycle executors. (~2851 tok)
- `test_lifecycle_worker_service.py` — Unit tests for ``lifecycle_worker_service.run_iteration``. (~2201 tok)

## .claude/worktrees/zesty-giggling-duckling/tests/unit/workers/executors/

- `__init__.py` — Tests for executors subpackage. (~11 tok)
- `test_real_mode.py` — Tests that exercise the real (lance-backed) code path of each executor. (~6254 tok)

## .codebuddy/

- `2026-05-12-python-env-setup.md` — Python 版本选型与虚拟环境搭建报告 (~1171 tok)
- `2026-05-13-block-a-acceptance-report.md` — Wave 2 / Block A 验收报告 (~1566 tok)
- `2026-05-13-github-initial-push-report.md` — Lance Laker — GitHub Initial Push Report (~2451 tok)
- `2026-05-13-local-e2e-smoke-report.md` — Local End-to-End Smoke Report (~1714 tok)
- `commit_msg_block_b.txt` — Declares wires (~885 tok)
- `commit_msg_block_c.txt` (~783 tok)
- `commit_msg_c1.txt` (~411 tok)
- `commit_msg_c2.txt` (~235 tok)
- `commit_msg_c3.txt` (~542 tok)
- `commit_msg_smoke.txt` (~754 tok)

## .codebuddy/coverage-html/

- `.gitignore` — Git ignore rules (~8 tok)
- `class_index.html` — Coverage report (~5568 tok)
- `coverage_html_cb_dd2e7eb5.js` — For details: https://github.com/coveragepy/coveragepy/blob/main/NOTICE.txt (~7272 tok)
- `function_index.html` — Coverage report (~16085 tok)
- `index.html` — Coverage report (~3756 tok)
- `status.json` (~1285 tok)
- `style_cb_9ff733b0.css` — Styles: 116 rules, 51 media queries (~4599 tok)
- `z_783d5c95bee7cabf_rls_py.html` — Coverage for src/lcp/db/rls.py: 95.5% (~11763 tok)
- `z_783d5c95bee7cabf_session_py.html` — Coverage for src/lcp/db/session.py: 86.4% (~5170 tok)
- `z_9af2fd4acd7fada3_auth_py.html` — Coverage for src/lcp/api/rest/auth.py: 91.2% (~11407 tok)
- `z_9af2fd4acd7fada3_deps_py.html` — Coverage for src/lcp/api/rest/deps.py: 100.0% (~3057 tok)
- `z_9af2fd4acd7fada3_main_py.html` — Coverage for src/lcp/api/rest/main.py: 0.0% (~7754 tok)
- `z_a51f7dcaa6db8155_config_py.html` — Coverage for src/lcp/core/config.py: 100.0% (~6400 tok)
- `z_a51f7dcaa6db8155_security_py.html` — Coverage for src/lcp/core/security.py: 82.0% (~19020 tok)
- `z_a51f7dcaa6db8155_tenant_py.html` — Coverage for src/lcp/core/tenant.py: 100.0% (~5097 tok)
- `z_ad54a14bee8f6878_datasets_py.html` — Coverage for src/lcp/api/rest/routers/datasets.py: 100.0% (~6376 tok)
- `z_ad54a14bee8f6878_indexes_py.html` — Coverage for src/lcp/api/rest/routers/indexes.py: 100.0% (~6531 tok)
- `z_ad54a14bee8f6878_meta_py.html` — Coverage for src/lcp/api/rest/routers/meta.py: 100.0% (~5382 tok)
- `z_ad54a14bee8f6878_tasks_py.html` — Coverage for src/lcp/api/rest/routers/tasks.py: 94.1% (~6367 tok)
- `z_ccaeb748b2ac9b19_server_py.html` — Coverage for src/lcp/api/grpc/server.py: 42.6% (~18907 tok)

## .codebuddy/logs/

- `lcp-api.log` (~119 tok)
- `lcp-api.pid` (~2 tok)
- `watcher.pid` (~2 tok)
- `worker.log` (~339 tok)
- `worker.pid` (~2 tok)

## .github/workflows/

- `api-lint.yml` — CI: API Lint (OpenAPI + Protobuf) (~581 tok)
- `tests.yml` — CI: Tests (pytest + coverage) (~743 tok)

## .mypy_cache/

- `.gitignore` — Git ignore rules (~10 tok)
- `CACHEDIR.TAG` (~51 tok)
- `missing_stubs` (~28 tok)

## .pytest_cache/

- `.gitignore` — Git ignore rules (~10 tok)
- `CACHEDIR.TAG` (~51 tok)
- `README.md` — Project documentation (~76 tok)

## .pytest_cache/v/cache/

- `lastfailed` (~172 tok)
- `nodeids` (~8718 tok)

## .ruff_cache/

- `.gitignore` — Git ignore rules (~10 tok)
- `CACHEDIR.TAG` (~12 tok)

## .ruff_cache/0.15.12/

- `15355979815381793510` (~744 tok)
- `2645877985117538134` (~575 tok)
- `3546248559746707481` (~86 tok)

## .venv/

- `.gitignore` — Git ignore rules (~1 tok)
- `.lock` (~0 tok)
- `CACHEDIR.TAG` (~12 tok)
- `pyvenv.cfg` (~37 tok)

## .venv/bin/

- `activate` — Permission is hereby granted, free of charge, to any person obtaining (~1096 tok)
- `activate_this.py` — Permission is hereby granted, free of charge, to any person obtaining (~681 tok)
- `activate.bat` (~718 tok)
- `activate.csh` — Permission is hereby granted, free of charge, to any person obtaining (~704 tok)
- `activate.fish` — Permission is hereby granted, free of charge, to any person obtaining (~1123 tok)
- `activate.nu` — Permission is hereby granted, free of charge, to any person obtaining (~1009 tok)
- `activate.ps1` — Permission is hereby granted, free of charge, to any person obtaining (~737 tok)
- `coverage-3.12` — -*- coding: utf-8 -*- (~100 tok)
- `coverage3` — -*- coding: utf-8 -*- (~100 tok)
- `deactivate.bat` (~462 tok)
- `dmypy` — -*- coding: utf-8 -*- (~99 tok)
- `dotenv` — -*- coding: utf-8 -*- (~93 tok)
- `f2py` — -*- coding: utf-8 -*- (~94 tok)
- `fastapi` — -*- coding: utf-8 -*- (~92 tok)
- `httpx` — -*- coding: utf-8 -*- (~91 tok)
- `jp.py` — main (~500 tok)
- `lcp-grpc` — -*- coding: utf-8 -*- (~94 tok)
- `lcp-rest` — -*- coding: utf-8 -*- (~94 tok)
- `mypy` — -*- coding: utf-8 -*- (~98 tok)
- `mypyc` — -*- coding: utf-8 -*- (~93 tok)
- `numpy-config` — -*- coding: utf-8 -*- (~94 tok)
- `py.test` — -*- coding: utf-8 -*- (~95 tok)
- `pydoc.bat` (~325 tok)
- `pygmentize` — -*- coding: utf-8 -*- (~94 tok)
- `pyrsa-decrypt` — -*- coding: utf-8 -*- (~93 tok)
- `pyrsa-encrypt` — -*- coding: utf-8 -*- (~93 tok)
- `pyrsa-keygen` — -*- coding: utf-8 -*- (~92 tok)
- `pyrsa-priv2pub` — -*- coding: utf-8 -*- (~99 tok)
- `pyrsa-sign` — -*- coding: utf-8 -*- (~91 tok)
- `pyrsa-verify` — -*- coding: utf-8 -*- (~92 tok)
- `pytest` — -*- coding: utf-8 -*- (~95 tok)
- `python-grpc-tools-protoc` — -*- coding: utf-8 -*- (~97 tok)
- `stubgen` — -*- coding: utf-8 -*- (~93 tok)
- `stubtest` — -*- coding: utf-8 -*- (~93 tok)
- `uvicorn` — -*- coding: utf-8 -*- (~93 tok)
- `watchfiles` — -*- coding: utf-8 -*- (~93 tok)
- `websockets` — -*- coding: utf-8 -*- (~93 tok)

## .venv/include/site/python3.12/greenlet/

- `greenlet.h` — ifndef Py_GREENLETOBJECT_H (~1359 tok)

## .venv/lib/python3.12/site-packages/

- `__editable__.lcp-0.1.0.pth` (~15 tok)
- `_virtualenv.pth` (~5 tok)
- `_virtualenv.py` — Patches that are applied at runtime to the virtual environment. (~1241 tok)
- `a1_coverage.pth` (~55 tok)
- `distutils-precedence.pth` (~41 tok)
- `mypy_extensions.py` — Defines experimental extensions to the standard "typing" module that are (~2216 tok)
- `py.py` — shim for pylib going away (~94 tok)
- `six.py` — Utilities for writing code that runs on Python 2 and 3 (~9916 tok)
- `typing_extensions.py` — _Sentinel: final, done, done, disjoint_base + 1 more (~45837 tok)

## .venv/lib/python3.12/site-packages/_distutils_hack/

- `__init__.py` — don't import any costly modules (~1930 tok)
- `override.py` (~13 tok)

## .venv/lib/python3.12/site-packages/_pytest/

- `__init__.py` (~112 tok)
- `_argcomplete.py` — Allow bash-completion for argparse with argcomplete if installed. (~1079 tok)
- `_version.py` — file generated by vcs-versioning (~149 tok)
- `cacheprovider.py` — Implementation of the cache provider. (~6614 tok)
- `capture.py` — Per-test stdout/stderr capturing mechanism. (~10523 tok)
- `compat.py` — Python version compatibility code and random general utilities. (~2930 tok)
- `debugging.py` — Interactive debugging with PDB, the Python Debugger. (~3985 tok)
- `deprecated.py` — Deprecation messages and bits of code used elsewhere in the codebase that (~1032 tok)
- `doctest.py` — Discover and run doctests in modules and test files. (~7280 tok)
- `faulthandler.py` — pytest_addoption, pytest_configure, pytest_unconfigure, get_stderr_fileno + 5 more (~1215 tok)
- `fixtures.py` — mypy: allow-untyped-defs (~22481 tok)
- `freeze_support.py` — Provides a function to report all internal modules for using freezing (~372 tok)
- `helpconfig.py` — Version info, help messages, tracing configuration. (~2863 tok)
- `hookspec.py` — Hook specifications for pytest plugins which are invoked by pytest itself (~12292 tok)
- `junitxml.py` — Report test results in JUnit-XML format, for use with Jenkins and build (~7292 tok)
- `legacypath.py` — Add backward compatibility support for the legacy py path type. (~4740 tok)
- `logging.py` — Access and control log capturing. (~10067 tok)
- `main.py` — Core implementation of the testing process: init, session, runtest loop. (~12125 tok)
- `monkeypatch.py` — Monkeypatching and mocking functionality. (~4429 tok)
- `nodes.py` — mypy: allow-untyped-defs (~7583 tok)
- `outcomes.py` — Exception classes and constants handling test outcomes as well as (~2888 tok)
- `pastebin.py` — Submit failure or test session information to a pastebin service. (~1188 tok)
- `pathlib.py` — URL patterns: 1 routes (~10823 tok)
- `py.typed` (~0 tok)
- `pytester_assertions.py` — Helper plugin for pytester; should not be loaded on its own. (~644 tok)
- `pytester.py` — (Disabled by default) support for testing pytest and pytest plugins. (~17826 tok)
- `python_api.py` — mypy: allow-untyped-defs (~9056 tok)
- `python.py` — Python test discovery, setup and run of test functions. (~19644 tok)
- `raises.py` — of: raises, raises, raises, raises + 4 more (~17167 tok)
- `recwarn.py` — Record warnings during test function execution. (~3825 tok)
- `reports.py` — mypy: allow-untyped-defs (~6638 tok)
- `runner.py` — Basic collect and runtest protocol implementations. (~5653 tok)
- `scope.py` — Scope: next_lower, next_higher, from_user (~783 tok)
- `setuponly.py` — pytest_addoption, pytest_fixture_setup, pytest_fixture_post_finalizer, pytest_cmdline_main (~905 tok)
- `setupplan.py` — pytest_addoption, pytest_fixture_setup, pytest_cmdline_main (~339 tok)
- `skipping.py` — Support for skip/xfail functions and markers. (~3089 tok)
- `stash.py` — View: get (~883 tok)
- `stepwise.py` — class: pytest_addoption, pytest_configure, pytest_sessionfinish, last_cache_date + 7 more (~2197 tok)
- `subtests.py` — Builtin plugin that adds subtests support. (~3784 tok)
- `terminal.py` — Terminal reporting of the full testing process. (~18410 tok)
- `terminalprogress.py` — A plugin to register the TerminalProgressPlugin plugin. (~330 tok)
- `threadexception.py` — ThreadExceptionMeta: collect_thread_exception, cleanup, thread_exception_hook, pytest_configure + 3 more (~1416 tok)
- `timing.py` — Indirection for time functions. (~888 tok)
- `tmpdir.py` — Support for providing temporary directories to test functions. (~3579 tok)
- `tracemalloc.py` — tracemalloc_message (~223 tok)
- `unittest.py` — Discover and run std-library "unittest" style tests. (~6996 tok)
- `unraisableexception.py` — UnraisableMeta: gc_collect_harder, collect_unraisable, cleanup, unraisable_hook + 4 more (~1480 tok)
- `warning_types.py` — PytestWarning: simple, format, warn_explicit_for (~1257 tok)
- `warnings.py` — mypy: allow-untyped-defs (~1484 tok)

## .venv/lib/python3.12/site-packages/_pytest/_code/

- `__init__.py` — Python inspection/code generation API. (~149 tok)
- `code.py` — mypy: allow-untyped-defs (~16036 tok)
- `source.py` — mypy: allow-untyped-defs (~2221 tok)

## .venv/lib/python3.12/site-packages/_pytest/_io/

- `__init__.py` (~55 tok)
- `pprint.py` — mypy: allow-untyped-defs (~5607 tok)
- `saferepr.py` — SafeRepr: repr, repr_instance, safeformat, saferepr + 1 more (~1167 tok)
- `terminalwriter.py` — Helper functions for writing to terminals and files. (~2570 tok)
- `wcwidth.py` — wcwidth, wcswidth (~369 tok)

## .venv/lib/python3.12/site-packages/_pytest/_py/

- `__init__.py` (~0 tok)
- `error.py` — create errno-specific classes for IO or os calls. (~993 tok)
- `path.py` — local path implementation. (~14066 tok)

## .venv/lib/python3.12/site-packages/_pytest/assertion/

- `__init__.py` — Support for presenting detailed information in failing assertions. (~2035 tok)
- `rewrite.py` — .py" for example) we can't bail out based (~13774 tok)
- `truncate.py` — Utilities for truncating assertion output. (~1554 tok)
- `util.py` — Utilities for assertion debugging. (~5875 tok)

## .venv/lib/python3.12/site-packages/_pytest/config/

- `__init__.py` — Command line options, config-file and conftest.py processing. (~22658 tok)
- `argparsing.py` — mypy: allow-untyped-defs (~5840 tok)
- `compat.py` — URL configuration (~842 tok)
- `exceptions.py` — Declares UsageError (~90 tok)
- `findpaths.py` — URL configuration (~3680 tok)

## .venv/lib/python3.12/site-packages/_pytest/mark/

- `__init__.py` — Generic mechanism for marking and selecting python functions. (~2820 tok)
- `expression.py` — TokenType: lex, accept, accept, accept + 10 more (~3213 tok)
- `structures.py` — mypy: allow-untyped-defs (~6593 tok)

## .venv/lib/python3.12/site-packages/_yaml/

- `__init__.py` — This is a stub package designed to roughly emulate the _yaml (~401 tok)

## .venv/lib/python3.12/site-packages/aiomysql-0.3.2.dist-info/

- `INSTALLER` (~1 tok)
- `METADATA` (~1347 tok)
- `RECORD` (~1040 tok)
- `REQUESTED` (~0 tok)
- `top_level.txt` (~6 tok)
- `WHEEL` (~25 tok)

## .venv/lib/python3.12/site-packages/aiomysql-0.3.2.dist-info/licenses/

- `LICENSE` — Project license (~286 tok)

## .venv/lib/python3.12/site-packages/aiomysql/

- `__init__.py` (~634 tok)
- `_scm_version.py` — file generated by setuptools-scm (~202 tok)
- `_scm_version.pyi` — This stub file is necessary because `_scm_version.py` (~34 tok)
- `_version.py` (~25 tok)
- `.gitignore` — Git ignore rules (~5 tok)
- `connection.py` — Python implementation of the MySQL client-server protocol (~15020 tok)
- `cursors.py` — Cursor: connection, description, rowcount, rownumber + 13 more (~6717 tok)
- `log.py` — Logging configuration. (~35 tok)
- `pool.py` — based on aiopg pool (~2401 tok)
- `utils.py` — _ContextManager: send, throw, close, gi_frame + 2 more (~1282 tok)

## .venv/lib/python3.12/site-packages/aiomysql/sa/

- `__init__.py` — Optional support for sqlalchemy.sql dynamic query generation. (~161 tok)
- `connection.py` — params - represent bound parameter values to be (~4256 tok)
- `engine.py` — ported from: (~1976 tok)
- `exc.py` — ported from: https://github.com/aio-libs/aiopg/blob/master/aiopg/sa/exc.py (~222 tok)
- `result.py` — ported from: (~4306 tok)
- `transaction.py` — ported from: (~1404 tok)

## .venv/lib/python3.12/site-packages/aiosqlite-0.22.1.dist-info/

- `INSTALLER` (~1 tok)
- `METADATA` — Declares to (~1150 tok)
- `RECORD` (~363 tok)
- `REQUESTED` (~0 tok)
- `WHEEL` (~22 tok)

## .venv/lib/python3.12/site-packages/aiosqlite-0.22.1.dist-info/licenses/

- `LICENSE` — Project license (~286 tok)

## .venv/lib/python3.12/site-packages/aiosqlite/

- `__init__.py` — asyncio bridge to the standard sqlite3 module (~254 tok)
- `__version__.py` (~45 tok)
- `context.py` — Result: send, throw, close, contextmanager + 1 more (~414 tok)
- `core.py` — Connection: set_result, set_exception, stop, close_and_stop + 25 more (~4313 tok)
- `cursor.py` — Cursor: execute, executemany, executescript, fetchone + 11 more (~994 tok)
- `py.typed` (~0 tok)

## .venv/lib/python3.12/site-packages/aiosqlite/tests/

- `__init__.py` (~26 tok)
- `__main__.py` (~47 tok)
- `helpers.py` — setup_logger (~207 tok)
- `perf.py` — PerfTest: timed, wrapper, setUpClass, tearDownClass + 12 more (~2072 tok)
- `smoke.py` — SmokeTest: setUpClass, setUp, test_connection_await, test_connection_context + 19 more (~5672 tok)

## .venv/lib/python3.12/site-packages/annotated_doc-0.0.4.dist-info/

- `entry_points.txt` (~9 tok)
- `INSTALLER` (~1 tok)
- `METADATA` — Declares attributes (~1751 tok)
- `RECORD` (~227 tok)
- `REQUESTED` (~0 tok)
- `WHEEL` (~24 tok)

## .venv/lib/python3.12/site-packages/annotated_doc-0.0.4.dist-info/licenses/

- `LICENSE` — Project license (~290 tok)

## .venv/lib/python3.12/site-packages/annotated_doc/

- `__init__.py` (~15 tok)
- `main.py` — Doc: hi (~308 tok)
- `py.typed` (~0 tok)

## .venv/lib/python3.12/site-packages/annotated_types-0.7.0.dist-info/

- `INSTALLER` (~1 tok)
- `METADATA` — Declares MyClass (~4013 tok)
- `RECORD` (~207 tok)
- `REQUESTED` (~0 tok)
- `WHEEL` (~24 tok)

## .venv/lib/python3.12/site-packages/annotated_types-0.7.0.dist-info/licenses/

- `LICENSE` — Project license (~289 tok)

## .venv/lib/python3.12/site-packages/annotated_types/

- `__init__.py` — Declares from (~3949 tok)
- `py.typed` (~0 tok)
- `test_cases.py` — Test file (~1834 tok)

## .venv/lib/python3.12/site-packages/anyio-4.13.0.dist-info/

- `entry_points.txt` (~10 tok)
- `INSTALLER` (~1 tok)
- `METADATA` (~1203 tok)
- `RECORD` (~1093 tok)
- `REQUESTED` (~0 tok)
- `top_level.txt` (~2 tok)
- `WHEEL` (~25 tok)

## .venv/lib/python3.12/site-packages/anyio-4.13.0.dist-info/licenses/

- `LICENSE` — Project license (~288 tok)

## .venv/lib/python3.12/site-packages/anyio/

- `__init__.py` — Declares as (~1763 tok)
- `from_thread.py` — _BlockingAsyncContextManager: run, run_sync, run_async_cm, started + 9 more (~5469 tok)
- `functools.py` — _InitialMissingType: cache_info, cache_parameters, cache_clear, cache_info + 12 more (~3451 tok)
- `lowlevel.py` — View: get, get, get (~1474 tok)
- `py.typed` (~0 tok)
- `pytest_plugin.py` — FreePortFactory: extract_backend_and_options, get_runner, pytest_addoption, pytest_configure + 10 more (~3650 tok)
- `to_interpreter.py` — _Worker: destroy, call, destroy, call + 4 more (~2029 tok)
- `to_process.py` — from: run_sync, send_raw_command, current_default_process_limiter, process_worker (~2800 tok)
- `to_thread.py` — run_sync, current_default_thread_limiter (~770 tok)

## .venv/lib/python3.12/site-packages/anyio/_backends/

- `__init__.py` (~0 tok)
- `_asyncio.py` — _State: close, get_loop, run, find_root_task + 2 more (~28422 tok)
- `_trio.py` — from: cancel, deadline, deadline, cancel_called + 25 more (~11819 tok)

## .venv/lib/python3.12/site-packages/anyio/_core/

- `__init__.py` (~0 tok)
- `_asyncio_selector_thread.py` — Selector: start, add_reader, add_writer, remove_reader + 3 more (~1608 tok)
- `_contextmanagers.py` — Declares _SupportsCtxMgr (~2062 tok)
- `_eventloop.py` — because: run, sleep, sleep_forever, sleep_until + 9 more (~1842 tok)
- `_exceptions.py` — BrokenResourceError: iterate_exceptions (~1260 tok)
- `_fileio.py` — from: wrapped, aclose, read, read1 + 35 more (~7333 tok)
- `_resources.py` — aclose_forcefully (~125 tok)
- `_signals.py` — open_signal_receiver (~291 tok)
- `_sockets.py` — URL configuration (~9992 tok)
- `_streams.py` — Declares create_memory_object_stream (~516 tok)
- `_subprocesses.py` — run_process, drain_stream, open_process (~2262 tok)
- `_synchronization.py` — from: set, is_set, wait, statistics + 29 more (~6018 tok)
- `_tasks.py` — _IgnoredTaskStatus: started, cancel, deadline, deadline + 8 more (~1553 tok)
- `_tempfile.py` — TemporaryFile: aclose, rollover, closed, read + 6 more (~5607 tok)
- `_testing.py` — TaskInfo: has_pending_cancellation, get_current_task, get_running_tasks, wait_all_tasks_blocked (~669 tok)
- `_typedattr.py` — TypedAttributeSet: typed_attribute, extra_attributes, extra, extra + 1 more (~717 tok)

## .venv/lib/python3.12/site-packages/anyio/abc/

- `__init__.py` (~820 tok)
- `_eventloop.py` — AsyncBackend: run, current_token, current_time, cancelled_exception_class + 43 more (~3037 tok)
- `_resources.py` — AsyncResource: aclose (~224 tok)
- `_sockets.py` — SocketAttribute: extra_attributes, from_socket, from_socket, send_fds + 9 more (~3750 tok)
- `_streams.py` — UnreliableObjectReceiveStream: receive, send, send_eof, receive + 5 more (~2138 tok)
- `_subprocesses.py` — Process: wait, terminate, kill, send_signal + 5 more (~591 tok)
- `_tasks.py` — TaskStatus: started, started, started, start_soon + 1 more (~1064 tok)
- `_testing.py` — TestRunner: run_asyncgen_fixture, run_fixture, run_test (~521 tok)

## .venv/lib/python3.12/site-packages/anyio/streams/

- `__init__.py` (~0 tok)
- `buffered.py` — BufferedByteReceiveStream: aclose, buffer, extra_attributes, feed_data + 6 more (~1790 tok)
- `file.py` — URL configuration (~1266 tok)
- `memory.py` — MemoryObjectStreamStatistics: statistics, receive_nowait, receive, clone + 9 more (~3069 tok)
- `stapled.py` — from: receive, send, send_eof, aclose + 9 more (~1255 tok)
- `text.py` — TextReceiveStream: receive, aclose, extra_attributes, send + 8 more (~1648 tok)
- `tls.py` — from: wrap, unwrap, aclose, receive + 4 more (~4373 tok)

## .venv/lib/python3.12/site-packages/ast_serialize-0.3.0.dist-info/

- `INSTALLER` (~1 tok)
- `METADATA` (~354 tok)
- `RECORD` (~259 tok)
- `REQUESTED` (~0 tok)
- `WHEEL` (~28 tok)

## .venv/lib/python3.12/site-packages/ast_serialize-0.3.0.dist-info/licenses/

- `LICENSE` — Project license (~318 tok)

## .venv/lib/python3.12/site-packages/ast_serialize-0.3.0.dist-info/sboms/

- `mypy_parser.cyclonedx.json` — Declares built (~27973 tok)

## .venv/lib/python3.12/site-packages/ast_serialize/

- `__init__.py` (~39 tok)
- `__init__.pyi` — Declares ParseError (~240 tok)
- `py.typed` (~0 tok)

## .venv/lib/python3.12/site-packages/boto3/

- `__init__.py` — may not use this file except in compliance with the License. A copy of (~962 tok)
- `compat.py` — may not use this file except in compliance with the License. A copy of (~915 tok)
- `crt.py` — may not use this file except in compliance with the License. A copy of (~2073 tok)
- `exceptions.py` — may not use this file except in compliance with the License. A copy of (~1197 tok)
- `session.py` — may not use this file except in compliance with the License. A copy of (~6316 tok)
- `utils.py` — may not use this file except in compliance with the License. A copy of (~859 tok)

## .venv/lib/python3.12/site-packages/boto3/data/cloudformation/2010-05-15/

- `resources-1.json` (~1460 tok)

## .venv/lib/python3.12/site-packages/boto3/data/cloudwatch/2010-08-01/

- `resources-1.json` (~3340 tok)

## .venv/lib/python3.12/site-packages/boto3/data/dynamodb/2012-08-10/

- `resources-1.json` (~1100 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2014-10-01/

- `resources-1.json` (~19563 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2015-03-01/

- `resources-1.json` (~19563 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2015-04-15/

- `resources-1.json` (~19563 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2015-10-01/

- `resources-1.json` (~21876 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2016-04-01/

- `resources-1.json` (~21876 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2016-09-15/

- `resources-1.json` (~21876 tok)

## .venv/lib/python3.12/site-packages/boto3/data/ec2/2016-11-15/

- `resources-1.json` (~21978 tok)

## .venv/lib/python3.12/site-packages/boto3/data/glacier/2012-06-01/

- `resources-1.json` (~5698 tok)

## .venv/lib/python3.12/site-packages/boto3/data/iam/2010-05-08/

- `resources-1.json` (~14388 tok)

## .venv/lib/python3.12/site-packages/boto3/data/s3/2006-03-01/

- `resources-1.json` (~10630 tok)

## .venv/lib/python3.12/site-packages/boto3/data/sns/2010-03-31/

- `resources-1.json` (~2598 tok)

## .venv/lib/python3.12/site-packages/boto3/data/sqs/2012-11-05/

- `resources-1.json` (~1870 tok)

## .venv/lib/python3.12/site-packages/boto3/docs/

- `__init__.py` — may not use this file except in compliance with the License. A copy of (~527 tok)
- `action.py` — may not use this file except in compliance with the License. A copy of (~2321 tok)
- `attr.py` — may not use this file except in compliance with the License. A copy of (~715 tok)
- `base.py` — may not use this file except in compliance with the License. A copy of (~601 tok)
- `client.py` — may not use this file except in compliance with the License. A copy of (~287 tok)
- `collection.py` — may not use this file except in compliance with the License. A copy of (~3228 tok)
- `docstring.py` — may not use this file except in compliance with the License. A copy of (~718 tok)
- `method.py` — may not use this file except in compliance with the License. A copy of (~779 tok)
- `resource.py` — may not use this file except in compliance with the License. A copy of (~4324 tok)
- `service.py` — may not use this file except in compliance with the License. A copy of (~2433 tok)
- `subresource.py` — may not use this file except in compliance with the License. A copy of (~1648 tok)
- `utils.py` — may not use this file except in compliance with the License. A copy of (~1554 tok)
- `waiter.py` — may not use this file except in compliance with the License. A copy of (~1476 tok)

## .venv/lib/python3.12/site-packages/boto3/dynamodb/

- `__init__.py` — may not use this file except in compliance with the License. A copy of (~161 tok)
- `conditions.py` — may not use this file except in compliance with the License. A copy of (~4294 tok)
- `table.py` — may not use this file except in compliance with the License. A copy of (~1812 tok)
- `transform.py` — may not use this file except in compliance with the License. A copy of (~3689 tok)
- `types.py` — may not use this file except in compliance with the License. A copy of (~2726 tok)

## .venv/lib/python3.12/site-packages/boto3/ec2/

- `__init__.py` — may not use this file except in compliance with the License. A copy of (~161 tok)
