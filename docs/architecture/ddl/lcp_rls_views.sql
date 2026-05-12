-- =============================================================================
-- LCP Multi-Tenant Row-Level Security (RLS) — DB-layer fallback
-- =============================================================================
-- This script provides the **defence-in-depth** layer for tenant isolation.
-- The primary enforcement happens at the application layer (SQLAlchemy
-- ``before_execute`` hook in ``lcp.db.rls``).  This script:
--
--   1. Defines a session-scoped variable @lcp_current_tenant.
--   2. Creates ``v_<table>_tenant`` views that filter by that variable.
--   3. Creates a read-only role granted access ONLY to the views.
--
-- Operators rotate the application service account between two roles:
--   - lcp_app_rw   : full DML on base tables (used by the LCP service)
--   - lcp_view_ro  : SELECT only on v_*_tenant views (used by ad-hoc analysts
--                    and BI tools that must respect tenant boundaries)
--
-- IMPORTANT: MySQL 8 does not support TRUE row-level security.  This view
-- pattern is a *containment* layer; it is not a substitute for the application
-- hook.  Both layers must be in place.
-- =============================================================================
USE lcp;
-- ---------------------------------------------------------------------------
-- 1. Session variable contract
-- ---------------------------------------------------------------------------
-- Every connection in lcp_view_ro must execute:
--     SET @lcp_current_tenant = '<tenant_id>';
-- before issuing any query.  Connection-poolers should reset the variable
-- when handing back the connection (use a connection-init callback).
-- ---------------------------------------------------------------------------
-- 2. Tenant-filtered views
-- ---------------------------------------------------------------------------
-- Each view exposes the same columns as the base table but adds a hard
-- predicate ``tenant_id = @lcp_current_tenant``.  When @lcp_current_tenant
-- is NULL, the view returns zero rows (fail-closed).
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_datasets_tenant AS
SELECT *
FROM datasets
WHERE tenant_id IS NOT NULL
    AND tenant_id = @lcp_current_tenant;
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_tasks_tenant AS
SELECT *
FROM tasks
WHERE tenant_id IS NOT NULL
    AND tenant_id = @lcp_current_tenant;
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_indexes_tenant AS
SELECT *
FROM indexes
WHERE tenant_id IS NOT NULL
    AND tenant_id = @lcp_current_tenant;
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_compactions_tenant AS
SELECT *
FROM compactions
WHERE tenant_id IS NOT NULL
    AND tenant_id = @lcp_current_tenant;
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_embedding_jobs_tenant AS
SELECT *
FROM embedding_jobs
WHERE tenant_id IS NOT NULL
    AND tenant_id = @lcp_current_tenant;
CREATE OR REPLACE SQL SECURITY INVOKER VIEW v_lifecycle_rules_tenant AS
SELECT *
FROM lifecycle_rules
WHERE tenant_id IS NOT NULL
    AND tenant_id = @lcp_current_tenant;
-- ---------------------------------------------------------------------------
-- 3. Roles and grants
-- ---------------------------------------------------------------------------
CREATE ROLE IF NOT EXISTS lcp_app_rw;
CREATE ROLE IF NOT EXISTS lcp_view_ro;
-- Application service account: full DML on base tables.
GRANT SELECT,
    INSERT,
    UPDATE,
    DELETE ON lcp.datasets TO lcp_app_rw;
GRANT SELECT,
    INSERT,
    UPDATE,
    DELETE ON lcp.tasks TO lcp_app_rw;
GRANT SELECT,
    INSERT,
    UPDATE,
    DELETE ON lcp.indexes TO lcp_app_rw;
GRANT SELECT,
    INSERT,
    UPDATE,
    DELETE ON lcp.compactions TO lcp_app_rw;
GRANT SELECT,
    INSERT,
    UPDATE,
    DELETE ON lcp.embedding_jobs TO lcp_app_rw;
GRANT SELECT,
    INSERT,
    UPDATE,
    DELETE ON lcp.lifecycle_rules TO lcp_app_rw;
-- Analyst / BI account: SELECT only on tenant-filtered views.
-- Crucially, lcp_view_ro must NOT receive any direct grant on base tables.
GRANT SELECT ON lcp.v_datasets_tenant TO lcp_view_ro;
GRANT SELECT ON lcp.v_tasks_tenant TO lcp_view_ro;
GRANT SELECT ON lcp.v_indexes_tenant TO lcp_view_ro;
GRANT SELECT ON lcp.v_compactions_tenant TO lcp_view_ro;
GRANT SELECT ON lcp.v_embedding_jobs_tenant TO lcp_view_ro;
GRANT SELECT ON lcp.v_lifecycle_rules_tenant TO lcp_view_ro;
-- ---------------------------------------------------------------------------
-- 4. Verification snippet
-- ---------------------------------------------------------------------------
-- Run as lcp_view_ro:
--     SET @lcp_current_tenant = 'tenant-acme';
--     SELECT COUNT(*) FROM v_datasets_tenant;       -- only acme rows
--     SET @lcp_current_tenant = NULL;
--     SELECT COUNT(*) FROM v_datasets_tenant;       -- 0 rows (fail-closed)
-- ---------------------------------------------------------------------------