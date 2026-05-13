-- =============================================================================
-- LCP State Schema (MySQL 8.0+)
--
-- Engine:    InnoDB
-- Charset:   utf8mb4 / utf8mb4_0900_ai_ci
-- Notes:
--   * All business keys use VARCHAR(64) UUIDs; the auto-increment id is the
--     internal primary key and never exposed via API.
--   * State columns use VARCHAR (not ENUM) so new states can be added without
--     online DDL.
--   * Free-form payloads use the native JSON type (MySQL 8.0+).
--   * `updated_at` is auto-updated on row change.
-- =============================================================================
CREATE DATABASE IF NOT EXISTS `lcp` DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_0900_ai_ci;
USE `lcp`;
-- -----------------------------------------------------------------------------
-- Table: dataset
--   Datasets registered into LCP management (1:1 with a Lance table).
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `dataset`;
CREATE TABLE `dataset` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `dataset_uuid` VARCHAR(64) NOT NULL COMMENT 'Global business id',
    `catalog` VARCHAR(128) NOT NULL COMMENT 'Gravitino catalog',
    `db_schema` VARCHAR(128) NOT NULL COMMENT 'Gravitino schema',
    `table_name` VARCHAR(128) NOT NULL COMMENT 'Lance table name',
    `storage_uri` VARCHAR(1024) NOT NULL COMMENT 'Object storage URI',
    `tenant_id` VARCHAR(64) NOT NULL DEFAULT 'default',
    `owner` VARCHAR(128) DEFAULT NULL,
    `description` VARCHAR(1024) DEFAULT NULL,
    `status` VARCHAR(32) NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE / PAUSED / ARCHIVED / DELETED',
    `row_count` BIGINT UNSIGNED NOT NULL DEFAULT 0,
    `fragment_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `index_coverage` DECIMAL(5, 4) NOT NULL DEFAULT 0.0000,
    `latest_version` BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Lance manifest version',
    `extra` JSON DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_dataset_uuid` (`dataset_uuid`),
    UNIQUE KEY `uk_catalog_schema_table` (`catalog`, `db_schema`, `table_name`),
    KEY `idx_tenant_status` (`tenant_id`, `status`),
    KEY `idx_updated_at` (`updated_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Datasets under LCP management';
-- -----------------------------------------------------------------------------
-- Table: vectorization_rule
--   Vectorization binding for each dataset (source columns -> target column).
--   1 dataset can hold N rules (one per target vector column).
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `vectorization_rule`;
CREATE TABLE `vectorization_rule` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `dataset_uuid` VARCHAR(64) NOT NULL,
    `target_column` VARCHAR(128) NOT NULL COMMENT 'Vector column name',
    `source_columns` JSON NOT NULL COMMENT 'Array of source column names',
    `model_name` VARCHAR(128) NOT NULL,
    `model_version` VARCHAR(64) NOT NULL,
    `model_endpoint` VARCHAR(512) DEFAULT NULL,
    `batch_size` INT UNSIGNED NOT NULL DEFAULT 64,
    `trigger_type` VARCHAR(32) NOT NULL DEFAULT 'ON_INSERT' COMMENT 'ON_INSERT / SCHEDULED / MANUAL',
    `cron_expr` VARCHAR(64) DEFAULT NULL,
    `enabled` TINYINT(1) NOT NULL DEFAULT 1,
    `extra` JSON DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_dataset_target` (`dataset_uuid`, `target_column`),
    KEY `idx_enabled_trigger` (`enabled`, `trigger_type`),
    CONSTRAINT `fk_vrule_dataset` FOREIGN KEY (`dataset_uuid`) REFERENCES `dataset` (`dataset_uuid`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Per-dataset vectorization rules';
-- -----------------------------------------------------------------------------
-- Table: vector_index
--   Vector / scalar indexes managed by LCP.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `vector_index`;
CREATE TABLE `vector_index` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `dataset_uuid` VARCHAR(64) NOT NULL,
    `index_name` VARCHAR(128) NOT NULL,
    `column_name` VARCHAR(128) NOT NULL,
    `index_type` VARCHAR(32) NOT NULL COMMENT 'HNSW / IVF_PQ / IVF_FLAT / BTREE / BITMAP / FTS',
    `params` JSON DEFAULT NULL COMMENT 'Type-specific params',
    `status` VARCHAR(32) NOT NULL DEFAULT 'BUILDING' COMMENT 'BUILDING / READY / OPTIMIZING / MERGING / FAILED / DROPPED',
    `coverage` DECIMAL(5, 4) NOT NULL DEFAULT 0.0000,
    `fragment_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `delta_count` INT UNSIGNED NOT NULL DEFAULT 0,
    `last_optimized_at` DATETIME(3) DEFAULT NULL,
    `last_merged_at` DATETIME(3) DEFAULT NULL,
    `last_seen_version` BIGINT UNSIGNED DEFAULT NULL COMMENT 'lance latest_version last observed by event-driven watcher; NULL = never observed',
    `error_message` VARCHAR(1024) DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_dataset_index_name` (`dataset_uuid`, `index_name`),
    KEY `idx_status` (`status`),
    KEY `idx_dataset_status` (`dataset_uuid`, `status`),
    CONSTRAINT `fk_index_dataset` FOREIGN KEY (`dataset_uuid`) REFERENCES `dataset` (`dataset_uuid`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Vector / scalar indexes managed by LCP';
-- -----------------------------------------------------------------------------
-- Table: task
--   Unified task store for vectorize / compaction / index / lifecycle jobs.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `task`;
CREATE TABLE `task` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `task_uuid` VARCHAR(64) NOT NULL,
    `task_type` VARCHAR(32) NOT NULL COMMENT 'VECTORIZE / COMPACTION / INDEX_BUILD / INDEX_OPTIMIZE / INDEX_CLEANUP / LIFECYCLE_RECYCLE',
    `dataset_uuid` VARCHAR(64) NOT NULL,
    `tenant_id` VARCHAR(64) NOT NULL DEFAULT 'default',
    `status` VARCHAR(32) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING / QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED',
    `priority` TINYINT UNSIGNED NOT NULL DEFAULT 5,
    `progress` DECIMAL(5, 4) NOT NULL DEFAULT 0.0000,
    `attempt` INT UNSIGNED NOT NULL DEFAULT 0,
    `max_attempts` INT UNSIGNED NOT NULL DEFAULT 3,
    `worker_id` VARCHAR(128) DEFAULT NULL,
    `idempotency_key` VARCHAR(128) DEFAULT NULL,
    `params` JSON DEFAULT NULL,
    `result` JSON DEFAULT NULL,
    `error_code` VARCHAR(64) DEFAULT NULL,
    `error_message` VARCHAR(2048) DEFAULT NULL,
    `scheduled_at` DATETIME(3) DEFAULT NULL,
    `started_at` DATETIME(3) DEFAULT NULL,
    `finished_at` DATETIME(3) DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_task_uuid` (`task_uuid`),
    UNIQUE KEY `uk_idempotency_key` (`idempotency_key`),
    KEY `idx_status_priority_scheduled` (`status`, `priority`, `scheduled_at`),
    KEY `idx_dataset_type_status` (`dataset_uuid`, `task_type`, `status`),
    KEY `idx_tenant_status` (`tenant_id`, `status`),
    KEY `idx_worker_status` (`worker_id`, `status`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Unified task store';
-- -----------------------------------------------------------------------------
-- Table: task_event
--   Audit / progress trail for tasks (append-only).
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `task_event`;
CREATE TABLE `task_event` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `task_uuid` VARCHAR(64) NOT NULL,
    `event_type` VARCHAR(32) NOT NULL COMMENT 'STATE_CHANGE / PROGRESS / WARNING / ERROR / NOTE',
    `from_status` VARCHAR(32) DEFAULT NULL,
    `to_status` VARCHAR(32) DEFAULT NULL,
    `progress` DECIMAL(5, 4) DEFAULT NULL,
    `message` VARCHAR(2048) DEFAULT NULL,
    `payload` JSON DEFAULT NULL,
    `worker_id` VARCHAR(128) DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    KEY `idx_task_uuid_created` (`task_uuid`, `created_at`),
    KEY `idx_event_type_created` (`event_type`, `created_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Append-only task event trail';
-- -----------------------------------------------------------------------------
-- Table: lifecycle_policy
--   Tier / TTL policies bound to a dataset.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `lifecycle_policy`;
CREATE TABLE `lifecycle_policy` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `dataset_uuid` VARCHAR(64) NOT NULL,
    `policy_name` VARCHAR(128) NOT NULL,
    `tier_rules` JSON DEFAULT NULL COMMENT 'Hot/warm/cold tier rules',
    `ttl_days` INT UNSIGNED DEFAULT NULL COMMENT 'Delete data older than N days; NULL = never',
    `compaction_threshold` JSON DEFAULT NULL COMMENT 'e.g. {"min_small_fragments":100,"window":"1h"}',
    `index_optimize_cron` VARCHAR(64) DEFAULT NULL,
    -- Watcher (event-driven INDEX_OPTIMIZE) configuration; default-off so
    -- existing datasets are not affected when this feature lands.
    `index_watch_enabled` TINYINT(1) NOT NULL DEFAULT 0,
    `index_watch_min_unindexed_rows` INT UNSIGNED DEFAULT 1000 COMMENT 'unindexed-rows threshold; NULL disables this signal',
    `index_watch_min_version_drift` INT UNSIGNED DEFAULT 1 COMMENT 'lance version-drift threshold; NULL disables this signal',
    `index_watch_stale_minutes` INT UNSIGNED DEFAULT 30 COMMENT 'force optimise after N minutes; NULL disables stale fallback',
    `enabled` TINYINT(1) NOT NULL DEFAULT 1,
    `last_run_at` DATETIME(3) DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_dataset_policy` (`dataset_uuid`, `policy_name`),
    KEY `idx_enabled_lastrun` (`enabled`, `last_run_at`),
    CONSTRAINT `fk_policy_dataset` FOREIGN KEY (`dataset_uuid`) REFERENCES `dataset` (`dataset_uuid`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Dataset lifecycle policies';
-- -----------------------------------------------------------------------------
-- Table: meta_sync_log
--   Records each LCP <-> Gravitino sync attempt.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `meta_sync_log`;
CREATE TABLE `meta_sync_log` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `sync_id` VARCHAR(64) NOT NULL,
    `scope` VARCHAR(32) NOT NULL COMMENT 'ALL / CATALOG / SCHEMA / DATASET',
    `catalog` VARCHAR(128) DEFAULT NULL,
    `db_schema` VARCHAR(128) DEFAULT NULL,
    `dataset_uuid` VARCHAR(64) DEFAULT NULL,
    `status` VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    `scanned` INT UNSIGNED NOT NULL DEFAULT 0,
    `updated` INT UNSIGNED NOT NULL DEFAULT 0,
    `skipped` INT UNSIGNED NOT NULL DEFAULT 0,
    `error_message` VARCHAR(2048) DEFAULT NULL,
    `started_at` DATETIME(3) DEFAULT NULL,
    `finished_at` DATETIME(3) DEFAULT NULL,
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_sync_id` (`sync_id`),
    KEY `idx_status_created` (`status`, `created_at`),
    KEY `idx_dataset_created` (`dataset_uuid`, `created_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Meta sync attempt log';
-- -----------------------------------------------------------------------------
-- Table: worker_registry
--   Active worker leases used by the scheduler.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `worker_registry`;
CREATE TABLE `worker_registry` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `worker_id` VARCHAR(128) NOT NULL,
    `worker_type` VARCHAR(64) NOT NULL COMMENT 'vdw / embedding / compaction / indexing',
    `host` VARCHAR(255) DEFAULT NULL,
    `lease_id` VARCHAR(64) NOT NULL,
    `capacity` INT UNSIGNED NOT NULL DEFAULT 1,
    `in_flight` INT UNSIGNED NOT NULL DEFAULT 0,
    `labels` JSON DEFAULT NULL,
    `status` VARCHAR(32) NOT NULL DEFAULT 'ALIVE' COMMENT 'ALIVE / DRAINING / DEAD',
    `last_heartbeat_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `created_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `updated_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_worker_id` (`worker_id`),
    KEY `idx_type_status_heartbeat` (`worker_type`, `status`, `last_heartbeat_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Active worker leases';
-- -----------------------------------------------------------------------------
-- Table: distributed_lock
--   Coarse-grained locks for scheduler concurrency control.
-- -----------------------------------------------------------------------------
DROP TABLE IF EXISTS `distributed_lock`;
CREATE TABLE `distributed_lock` (
    `lock_key` VARCHAR(191) NOT NULL,
    `holder` VARCHAR(128) NOT NULL,
    `acquired_at` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    `expires_at` DATETIME(3) NOT NULL,
    `extra` JSON DEFAULT NULL,
    PRIMARY KEY (`lock_key`),
    KEY `idx_expires_at` (`expires_at`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci COMMENT = 'Coarse-grained distributed locks';