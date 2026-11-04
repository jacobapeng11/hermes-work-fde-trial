-- Current explicit schema updates for existing Hermes Cloud databases.
--
-- This file intentionally has no revision number or migration history. New
-- tables are created from SQLAlchemy metadata before this file runs. Keep only
-- direct, repeat-safe changes required for existing tables here.

DROP PROCEDURE IF EXISTS hermes_apply_current_schema;

DELIMITER $$

CREATE PROCEDURE hermes_apply_current_schema()
BEGIN
    DECLARE object_count INT DEFAULT 0;
    DECLARE invalid_count INT DEFAULT 0;
    DECLARE legacy_foreign_key_name VARCHAR(255) DEFAULT NULL;
    DECLARE legacy_column_name VARCHAR(255) DEFAULT NULL;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user'
       AND COLUMN_NAME = 'last_daily_presence_at';
    IF object_count = 0 THEN
        ALTER TABLE `user`
            ADD COLUMN last_daily_presence_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user'
       AND COLUMN_NAME = 'login_count';
    IF object_count = 0 THEN
        ALTER TABLE `user`
            ADD COLUMN login_count INT NOT NULL DEFAULT 0;
        UPDATE `user`
           SET login_count = 1
         WHERE last_login_at IS NOT NULL;
    END IF;

    CREATE TABLE IF NOT EXISTS personal_skill_category (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        owner_user_id BIGINT NOT NULL,
        category_key VARCHAR(64) NOT NULL,
        display_name VARCHAR(100) NOT NULL,
        normalized_name VARCHAR(100) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_personal_skill_category_owner_key
            UNIQUE (org_id, owner_user_id, category_key),
        CONSTRAINT uq_personal_skill_category_owner_name
            UNIQUE (org_id, owner_user_id, normalized_name),
        INDEX ix_personal_skill_category_org (org_id),
        INDEX ix_personal_skill_category_owner (owner_user_id),
        INDEX ix_personal_skill_category_owner_display
            (org_id, owner_user_id, display_name),
        CONSTRAINT fk_personal_skill_category_org
            FOREIGN KEY (org_id) REFERENCES organization(id),
        CONSTRAINT fk_personal_skill_category_owner
            FOREIGN KEY (owner_user_id) REFERENCES user(id)
    );

    CREATE TABLE IF NOT EXISTS runtime_maintenance_control (
        id INT NOT NULL,
        maintenance_state VARCHAR(16) NOT NULL DEFAULT 'open',
        maintenance_epoch VARCHAR(64) NULL,
        maintenance_reason VARCHAR(500) NULL,
        started_at DATETIME NULL,
        completed_at DATETIME NULL,
        admission_generation BIGINT NOT NULL DEFAULT 1,
        grant_issuance_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_runtime_maintenance_control_state
            CHECK (maintenance_state IN ('open', 'frozen'))
    );

    INSERT INTO runtime_maintenance_control (
        id,
        maintenance_state,
        admission_generation,
        grant_issuance_enabled,
        created_at,
        updated_at
    ) VALUES (1, 'open', 1, TRUE, UTC_TIMESTAMP(), UTC_TIMESTAMP())
    ON DUPLICATE KEY UPDATE id = VALUES(id);

    CREATE TABLE IF NOT EXISTS runtime_bootstrap_intent (
        id BIGINT NOT NULL AUTO_INCREMENT,
        intent_id VARCHAR(36) NOT NULL,
        runtime_binding_type VARCHAR(24) NOT NULL,
        runtime_binding_id BIGINT NOT NULL,
        runtime_id VARCHAR(128) NOT NULL,
        pvc_storage_identity_hash VARCHAR(71) NOT NULL,
        target_image VARCHAR(1000) NOT NULL,
        target_image_digest VARCHAR(255) NOT NULL,
        authorization_digest VARCHAR(71) NOT NULL,
        authorization_document JSON NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'authorized',
        authorized_at DATETIME NOT NULL,
        completed_at DATETIME NULL,
        last_error TEXT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_runtime_bootstrap_intent_id UNIQUE (intent_id),
        CONSTRAINT uq_runtime_bootstrap_intent_authorization_digest
            UNIQUE (authorization_digest),
        CONSTRAINT ck_runtime_bootstrap_intent_binding_type
            CHECK (runtime_binding_type IN ('personal_runtime', 'application_runtime')),
        CONSTRAINT ck_runtime_bootstrap_intent_status
            CHECK (status IN ('authorized', 'completed', 'failed')),
        INDEX ix_runtime_bootstrap_intent_binding
            (runtime_binding_type, runtime_binding_id, created_at)
    );

    -- Supervisor control roots are per Binding, encrypted at rest, and
    -- rotated by appending a new version.  They are never stored in Runtime
    -- environment variables.
    CREATE TABLE IF NOT EXISTS runtime_platform_control_key (
        id BIGINT NOT NULL AUTO_INCREMENT,
        binding_type VARCHAR(24) NOT NULL,
        binding_id BIGINT NOT NULL,
        runtime_id VARCHAR(128) NOT NULL,
        key_version INT NOT NULL,
        root_key_encrypted VARCHAR(1000) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        retired_at DATETIME NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_runtime_platform_control_key_version
            UNIQUE (binding_type, binding_id, key_version),
        CONSTRAINT ck_runtime_platform_control_key_binding_type
            CHECK (binding_type IN ('personal_runtime', 'application_runtime')),
        CONSTRAINT ck_runtime_platform_control_key_status
            CHECK (status IN ('active', 'retired')),
        CONSTRAINT ck_runtime_platform_control_key_positive_version
            CHECK (key_version > 0),
        INDEX ix_runtime_platform_control_key_active
            (binding_type, binding_id, status),
        INDEX ix_runtime_platform_control_key_runtime
            (runtime_id, status)
    );

    -- Personal Runtime exact-generation ConfigBundle artifacts: the files and
    -- digest of one (workspace, generation) are immutable; identity changes
    -- must advance the Binding generation instead of rewriting old revisions.
    CREATE TABLE IF NOT EXISTS personal_config_bundle_artifact (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        runtime_workspace_id BIGINT NOT NULL,
        config_generation INT NOT NULL,
        config_revision VARCHAR(64) NOT NULL,
        config_digest VARCHAR(128) NOT NULL,
        files JSON NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_pcb_artifact_ws_generation
            UNIQUE (runtime_workspace_id, config_generation),
        INDEX ix_pcb_artifact_org (org_id)
    );

    -- The immutable PVC UID is learned from Kubernetes during orchestration.
    -- Existing rows intentionally remain NULL and must be rebound by a live
    -- reconcile; no path/name-derived backfill is permitted.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'runtime_root_pvc_uid';
    IF object_count = 0 THEN
        ALTER TABLE hermes_runtime_binding
            ADD COLUMN runtime_root_pvc_uid VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'pvc_storage_identity_hash';
    IF object_count = 0 THEN
        ALTER TABLE hermes_runtime_binding
            ADD COLUMN pvc_storage_identity_hash VARCHAR(71) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'runtime_root_pvc_uid';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_runtime_binding
            ADD COLUMN runtime_root_pvc_uid VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'pvc_storage_identity_hash';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_runtime_binding
            ADD COLUMN pvc_storage_identity_hash VARCHAR(71) NULL;
    END IF;

    -- Application Agent instructions live in config.system_prompt.  The old
    -- description metadata had no runtime or management semantics.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent'
       AND COLUMN_NAME = 'description';
    IF object_count > 0 THEN
        ALTER TABLE application_agent
            DROP COLUMN description;
    END IF;

    -- Application Agent deletion is an asynchronous control-plane lifecycle.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent'
       AND COLUMN_NAME = 'lifecycle_status';
    IF object_count = 0 THEN
        ALTER TABLE application_agent
            ADD COLUMN lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'active',
            ADD COLUMN delete_requested_at DATETIME NULL,
            ADD COLUMN delete_last_error_code VARCHAR(100) NULL;
    END IF;

    -- Prepared knowledge contexts are invalidated by one authoritative
    -- organization generation.  The business mutation and generation bump
    -- share a database transaction.
    CREATE TABLE IF NOT EXISTS knowledge_context_epoch (
        organization_id BIGINT NOT NULL,
        generation BIGINT NOT NULL DEFAULT 1,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (organization_id),
        CONSTRAINT fk_knowledge_context_epoch_organization
            FOREIGN KEY (organization_id) REFERENCES organization(id)
    );

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'runtime_run'
      AND COLUMN_NAME = 'runtime_provider_run_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_run
            ADD COLUMN runtime_provider_run_id VARCHAR(128) NULL;
    END IF;

    -- Model-specific Runtime wire contract.  Existing rows remain NULL and
    -- are normalized to the safe no-reasoning-replay default by Backend.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'model_entry'
       AND COLUMN_NAME = 'runtime_capabilities';
    IF object_count = 0 THEN
        ALTER TABLE model_entry
            ADD COLUMN runtime_capabilities JSON NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_run'
       AND INDEX_NAME = 'ix_runtime_run_session_status_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_run
            ADD INDEX ix_runtime_run_session_status_id
                (session_id, status, id);
    END IF;

    -- User Web clarify closes only after an idempotent continuation is
    -- admitted.  Legacy expires_at rows remain readable historical state.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'auto_default_at';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN auto_default_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'answer_source';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN answer_source VARCHAR(16) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'claim_expires_at';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN claim_expires_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'resume_attempt_count';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN resume_attempt_count INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'next_resume_at';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN next_resume_at DATETIME NULL;
    END IF;

    -- Expert Kanban workers ask on the originating Web session and receive
    -- their answer in place; they never create a continuation Run.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'mode';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN mode VARCHAR(32) NOT NULL DEFAULT 'web_run';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'collaboration_task_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN collaboration_task_id BIGINT NULL,
            ADD CONSTRAINT fk_runtime_clarify_collaboration_task
                FOREIGN KEY (collaboration_task_id)
                REFERENCES expert_kanban_collaboration_task (id)
                ON DELETE SET NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'expert_profile_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN expert_profile_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND COLUMN_NAME = 'requested_by';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD COLUMN requested_by VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_kanban_collaboration'
       AND COLUMN_NAME = 'wait_attempt_count';
    IF object_count = 0 THEN
        ALTER TABLE expert_kanban_collaboration
            ADD COLUMN wait_attempt_count INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND INDEX_NAME = 'ix_runtime_clarify_request_auto_default';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD INDEX ix_runtime_clarify_request_auto_default
                (status, auto_default_at);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_clarify_request'
       AND INDEX_NAME = 'ix_runtime_clarify_request_retry';
    IF object_count = 0 THEN
        ALTER TABLE runtime_clarify_request
            ADD INDEX ix_runtime_clarify_request_retry
                (status, next_resume_at);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_run_event'
       AND COLUMN_NAME = 'dedupe_key';
    IF object_count = 0 THEN
        ALTER TABLE runtime_run_event
            ADD COLUMN dedupe_key VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_run_event'
       AND INDEX_NAME = 'uq_runtime_run_event_dedupe';
    IF object_count = 0 THEN
        ALTER TABLE runtime_run_event
            ADD UNIQUE INDEX uq_runtime_run_event_dedupe
                (run_id, dedupe_key);
    END IF;

    -- EnterpriseIdp 管理后台只通过工号授权表判权。该表没有任何浏览器写入入口，
    -- 后续人员由受控数据库操作维护；初始化数据由 scripts.seed 幂等补齐。
    CREATE TABLE IF NOT EXISTS admin_work_no_permissions (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        work_no VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'active',
        remark VARCHAR(255) NULL,
        created_by BIGINT NULL,
        updated_by BIGINT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_admin_work_no_permissions_org_work_no UNIQUE (org_id, work_no),
        CONSTRAINT ck_admin_work_no_permissions_status CHECK (status IN ('active', 'disabled')),
        CONSTRAINT fk_admin_work_no_permissions_org FOREIGN KEY (org_id) REFERENCES organization(id),
        INDEX ix_admin_work_no_permissions_org_id (org_id),
        INDEX ix_admin_work_no_permissions_org_work_no_status (org_id, work_no, status)
    );

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent'
       AND COLUMN_NAME = 'personal_knowledge_binding_digest';
    IF object_count = 0 THEN
        ALTER TABLE application_agent
            ADD COLUMN personal_knowledge_binding_digest VARCHAR(64)
            NOT NULL DEFAULT
            'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
    END IF;

    CREATE TABLE IF NOT EXISTS application_agent_personal_knowledge_binding (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        application_agent_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        selected_by_user_id BIGINT NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'enabled',
        selected_at DATETIME NOT NULL,
        revoked_at DATETIME NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_application_agent_personal_kb
            UNIQUE (application_agent_id, knowledge_base_id),
        CONSTRAINT ck_application_agent_personal_kb_status
            CHECK (
                status IN (
                    'enabled',
                    'permission_revoked',
                    'disabled'
                )
            ),
        CONSTRAINT fk_application_agent_personal_kb_org
            FOREIGN KEY (org_id) REFERENCES organization(id),
        CONSTRAINT fk_application_agent_personal_kb_agent
            FOREIGN KEY (application_agent_id)
            REFERENCES application_agent(id) ON DELETE CASCADE,
        CONSTRAINT fk_application_agent_personal_kb_selector
            FOREIGN KEY (selected_by_user_id) REFERENCES `user`(id),
        INDEX ix_application_agent_personal_kb_org (org_id),
        INDEX ix_application_agent_personal_kb_agent
            (application_agent_id),
        INDEX ix_application_agent_personal_kb_knowledge
            (knowledge_base_id),
        INDEX ix_application_agent_personal_kb_status
            (application_agent_id, status)
    );

    CREATE TABLE IF NOT EXISTS application_agent_studio_session (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        application_agent_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        runtime_session_id VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL DEFAULT '新任务',
        status VARCHAR(32) NOT NULL DEFAULT 'active',
        last_active_at DATETIME NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_application_agent_studio_session
            UNIQUE (
                application_agent_id,
                user_id,
                runtime_session_id
            ),
        CONSTRAINT fk_application_agent_studio_session_agent
            FOREIGN KEY (application_agent_id)
            REFERENCES application_agent(id),
        CONSTRAINT fk_application_agent_studio_session_user
            FOREIGN KEY (user_id) REFERENCES `user`(id),
        INDEX ix_application_agent_studio_session_org_id (org_id),
        INDEX ix_application_agent_studio_session_agent_id
            (application_agent_id),
        INDEX ix_application_agent_studio_session_user_id (user_id),
        INDEX ix_application_agent_studio_session_status (status),
        INDEX ix_application_agent_studio_session_recent
            (application_agent_id, user_id, last_active_at)
    );

    CREATE TABLE IF NOT EXISTS application_agent_studio_file_artifact (
        id BIGINT NOT NULL AUTO_INCREMENT,
        studio_session_id BIGINT NOT NULL,
        application_agent_run_id BIGINT NULL,
        relative_path VARCHAR(1000) NOT NULL,
        mime_type VARCHAR(255) NULL,
        size_bytes BIGINT NOT NULL DEFAULT 0,
        sha256_digest VARCHAR(64) NULL,
        source_type VARCHAR(32) NOT NULL,
        status VARCHAR(16) NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_application_agent_studio_file_status
            CHECK (status IN ('pending', 'active', 'failed', 'deleted')),
        CONSTRAINT ck_application_agent_studio_file_source
            CHECK (source_type IN ('upload', 'runtime')),
        CONSTRAINT fk_application_agent_studio_file_session
            FOREIGN KEY (studio_session_id)
            REFERENCES application_agent_studio_session(id),
        CONSTRAINT fk_application_agent_studio_file_run
            FOREIGN KEY (application_agent_run_id)
            REFERENCES application_agent_run(id),
        INDEX ix_application_agent_studio_file_session
            (studio_session_id, status, created_at),
        INDEX ix_application_agent_studio_file_run
            (application_agent_run_id),
        INDEX ix_application_agent_studio_file_path
            (studio_session_id, relative_path(766))
    );

    -- OpenAPI Conversation files are a control-plane index only. Runtime PVC
    -- remains the file-body authority; application-agent consistency is checked
    -- by the synchronization service before each upsert.
    CREATE TABLE IF NOT EXISTS application_agent_conversation_file_artifact (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        application_agent_id BIGINT NOT NULL,
        conversation_id BIGINT NOT NULL,
        application_agent_run_id BIGINT NOT NULL,
        relative_path VARCHAR(1000) NOT NULL,
        relative_path_sha256 VARCHAR(64) NOT NULL,
        mime_type VARCHAR(255) NULL,
        size_bytes BIGINT NOT NULL DEFAULT 0,
        source_type VARCHAR(32) NOT NULL DEFAULT 'runtime',
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_application_agent_conversation_file_path
            UNIQUE (conversation_id, relative_path_sha256),
        CONSTRAINT ck_application_agent_conversation_file_source
            CHECK (source_type IN ('runtime')),
        CONSTRAINT ck_application_agent_conversation_file_status
            CHECK (status IN ('active', 'deleted')),
        CONSTRAINT fk_application_agent_conversation_file_org
            FOREIGN KEY (org_id) REFERENCES organization(id),
        CONSTRAINT fk_application_agent_conversation_file_agent
            FOREIGN KEY (application_agent_id)
            REFERENCES application_agent(id),
        CONSTRAINT fk_application_agent_conversation_file_conversation
            FOREIGN KEY (conversation_id)
            REFERENCES application_agent_conversation(id),
        CONSTRAINT fk_application_agent_conversation_file_run
            FOREIGN KEY (application_agent_run_id)
            REFERENCES application_agent_run(id),
        INDEX ix_application_agent_conversation_file_org (org_id),
        INDEX ix_application_agent_conversation_file_agent
            (application_agent_id),
        INDEX ix_application_agent_conversation_file_list
            (conversation_id, status, created_at),
        INDEX ix_application_agent_conversation_file_run
            (application_agent_run_id)
    );

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_studio_file_artifact'
       AND INDEX_NAME = 'ix_application_agent_studio_file_session';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_studio_file_artifact
            ADD INDEX ix_application_agent_studio_file_session
                (studio_session_id, status, created_at);
    END IF;

    -- The path upsert index must stay under MySQL's 3072-byte InnoDB limit
    -- while retaining the full VARCHAR(1000) path value.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_studio_file_artifact'
       AND INDEX_NAME = 'ix_application_agent_studio_file_path';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_studio_file_artifact
            ADD INDEX ix_application_agent_studio_file_path
                (studio_session_id, relative_path(766));
    END IF;

    -- The old Application Studio catalogue lived only in Hermes state.db.
    -- Preserve every non-empty historical Studio session in the Backend
    -- catalogue before browsers switch to Runtime Edge.  Runtime Edge remains
    -- authoritative for the actual message history; a missing legacy title is
    -- represented as 新任务 and can be renamed normally.
    INSERT INTO application_agent_studio_session (
        org_id,
        application_agent_id,
        user_id,
        runtime_session_id,
        title,
        status,
        last_active_at,
        created_at,
        updated_at
    )
    SELECT
        run.org_id,
        run.application_agent_id,
        run.subject_user_id,
        run.runtime_session_id,
        '新任务',
        'active',
        MAX(
            COALESCE(
                run.finished_at,
                run.started_at,
                run.created_at
            )
        ),
        MIN(run.created_at),
        CURRENT_TIMESTAMP
    FROM application_agent_run AS run
    WHERE run.channel = 'studio'
      AND run.subject_user_id IS NOT NULL
      AND run.runtime_session_id LIKE CONCAT(
          'studio-',
          run.application_agent_id,
          '-',
          run.subject_user_id,
          '-%'
      )
    GROUP BY
        run.org_id,
        run.application_agent_id,
        run.subject_user_id,
        run.runtime_session_id
    ON DUPLICATE KEY UPDATE
        last_active_at = GREATEST(
            COALESCE(
                application_agent_studio_session.last_active_at,
                VALUES(last_active_at)
            ),
            VALUES(last_active_at)
        ),
        updated_at = CURRENT_TIMESTAMP;

    -- Application Agent WeCom identity uses the same provider-user mapping as
    -- cowork-hermes.  Existing Hermes users keep their local primary keys;
    -- the external binder updates this nullable field by org_id + user_code.
    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user'
       AND COLUMN_NAME = 'qw_token';
    IF object_count = 0 THEN
        ALTER TABLE `user`
            ADD COLUMN qw_token VARCHAR(100) NULL;
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user'
       AND INDEX_NAME = 'ix_user_org_qw_token';
    IF object_count = 0 THEN
        ALTER TABLE `user`
            ADD INDEX ix_user_org_qw_token (org_id, qw_token);
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND COLUMN_NAME = 'origin_runtime_workspace_id';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD COLUMN origin_runtime_workspace_id BIGINT NULL;
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND COLUMN_NAME = 'active_name_key';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD COLUMN active_name_key VARCHAR(200) NULL;
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND COLUMN_NAME = 'market_name_key';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD COLUMN market_name_key VARCHAR(200) NULL;
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND COLUMN_NAME = 'execution_status';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD COLUMN execution_status VARCHAR(32) NOT NULL DEFAULT 'enabled';
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND COLUMN_NAME = 'execution_status_changed_at';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD COLUMN execution_status_changed_at DATETIME NULL;
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND COLUMN_NAME = 'execution_status_reason';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD COLUMN execution_status_reason VARCHAR(1000) NULL;
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package_version'
       AND COLUMN_NAME = 'change_summary';
    IF object_count = 0 THEN
        ALTER TABLE expert_package_version
            ADD COLUMN change_summary TEXT NULL;
    END IF;

    UPDATE expert_package AS package
    JOIN runtime_agent_profile AS profile
      ON profile.expert_package_id = package.id
     AND profile.ownership_type = 'self_created'
     AND profile.status <> 'deleted'
       SET package.origin_runtime_workspace_id = COALESCE(
               package.origin_runtime_workspace_id,
               profile.runtime_workspace_id
           ),
           package.active_name_key = COALESCE(
               package.active_name_key,
               LOWER(TRIM(package.name))
           );

    -- Historical duplicate names cannot be rejected during an in-place
    -- migration.  Clearing their tombstone keys preserves both experts; the
    -- first subsequent edit adopts the new uniqueness rule.
    UPDATE expert_package AS package
    JOIN (
        SELECT org_id,
               owner_user_id,
               origin_runtime_workspace_id,
               active_name_key
          FROM expert_package
         WHERE origin_runtime_workspace_id IS NOT NULL
           AND active_name_key IS NOT NULL
         GROUP BY org_id,
                  owner_user_id,
                  origin_runtime_workspace_id,
                  active_name_key
        HAVING COUNT(*) > 1
    ) AS duplicate_name
      ON duplicate_name.org_id = package.org_id
     AND duplicate_name.owner_user_id = package.owner_user_id
     AND duplicate_name.origin_runtime_workspace_id = package.origin_runtime_workspace_id
     AND duplicate_name.active_name_key = package.active_name_key
       SET package.active_name_key = NULL;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND INDEX_NAME = 'uq_expert_owner_workspace_active_name';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD CONSTRAINT uq_expert_owner_workspace_active_name
            UNIQUE (
                org_id,
                owner_user_id,
                origin_runtime_workspace_id,
                active_name_key
            );
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND INDEX_NAME = 'uq_expert_market_name_lease';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD CONSTRAINT uq_expert_market_name_lease
            UNIQUE (org_id, market_name_key);
    END IF;

    SELECT COUNT(*)
      INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'expert_package'
       AND INDEX_NAME = 'ix_expert_package_execution_status';
    IF object_count = 0 THEN
        ALTER TABLE expert_package
            ADD INDEX ix_expert_package_execution_status (execution_status);
    END IF;

    -- The IM Gateway is shared by personal and application Agent targets.
    -- Existing rows are personal targets and are backfilled without changing
    -- their fixed Runtime Session semantics.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'target_type';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN target_type VARCHAR(32) NOT NULL
            DEFAULT 'personal_agent';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'created_by_user_id';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN created_by_user_id BIGINT NULL;
    END IF;
    UPDATE im_channel
       SET created_by_user_id = user_id
     WHERE created_by_user_id IS NULL;
    SELECT COUNT(*) INTO invalid_count
      FROM im_channel
     WHERE created_by_user_id IS NULL;
    IF invalid_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'IM channels require a creator';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'application_agent_id';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN application_agent_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'application_client_id';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN application_client_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'member_access_url';
    IF object_count > 0 THEN
        ALTER TABLE im_channel
            DROP COLUMN member_access_url;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'direct_enabled';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN direct_enabled BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'group_enabled';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN group_enabled BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;

    ALTER TABLE im_channel
        MODIFY COLUMN created_by_user_id BIGINT NOT NULL,
        MODIFY COLUMN user_id BIGINT NULL,
        MODIFY COLUMN runtime_workspace_id BIGINT NULL,
        MODIFY COLUMN runtime_session_row_id BIGINT NULL;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND INDEX_NAME = 'uq_im_channel_application_provider';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD CONSTRAINT uq_im_channel_application_provider
            UNIQUE (application_agent_id, provider);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND INDEX_NAME = 'ix_im_channel_application';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD INDEX ix_im_channel_application
            (org_id, application_agent_id, provider);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_onboarding_session'
       AND COLUMN_NAME = 'target_type';
    IF object_count = 0 THEN
        ALTER TABLE im_onboarding_session
            ADD COLUMN target_type VARCHAR(32) NOT NULL
            DEFAULT 'personal_agent';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'proactive_target_encrypted';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN proactive_target_encrypted TEXT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'proactive_target_digest';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN proactive_target_digest VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_channel'
       AND COLUMN_NAME = 'proactive_target_confirmed_at';
    IF object_count = 0 THEN
        ALTER TABLE im_channel
            ADD COLUMN proactive_target_confirmed_at DATETIME NULL;
    END IF;

    CREATE TABLE IF NOT EXISTS im_outbound_delivery (
        id BIGINT NOT NULL AUTO_INCREMENT,
        delivery_key VARCHAR(255) NOT NULL,
        delivery_kind VARCHAR(32) NOT NULL DEFAULT 'scheduled_task_result',
        channel_id BIGINT NOT NULL,
        scheduled_task_id BIGINT NOT NULL,
        runtime_run_row_id BIGINT NOT NULL,
        runtime_run_id VARCHAR(128) NOT NULL,
        bot_id_digest VARCHAR(64) NOT NULL,
        target_encrypted TEXT NOT NULL,
        target_digest VARCHAR(64) NOT NULL,
        content TEXT NOT NULL,
        artifact_ids JSON NULL,
        delivered_artifact_ids JSON NULL,
        provider_delivered_text TEXT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        attempt_count INT NOT NULL DEFAULT 0,
        next_attempt_at DATETIME NULL,
        processing_owner VARCHAR(160) NULL,
        processing_lease_epoch BIGINT NULL,
        delivered_at DATETIME NULL,
        error_code VARCHAR(100) NULL,
        error_message TEXT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_im_outbound_delivery_key UNIQUE (delivery_key),
        CONSTRAINT fk_im_outbound_delivery_channel FOREIGN KEY (channel_id)
            REFERENCES im_channel(id) ON DELETE CASCADE,
        CONSTRAINT fk_im_outbound_delivery_task FOREIGN KEY (scheduled_task_id)
            REFERENCES scheduled_task(id) ON DELETE CASCADE,
        CONSTRAINT fk_im_outbound_delivery_run FOREIGN KEY (runtime_run_row_id)
            REFERENCES runtime_run(id) ON DELETE CASCADE,
        INDEX ix_im_outbound_delivery_channel_id (channel_id),
        INDEX ix_im_outbound_delivery_scheduled_task_id (scheduled_task_id),
        INDEX ix_im_outbound_delivery_runtime_run_row_id (runtime_run_row_id),
        INDEX ix_im_outbound_delivery_status (status),
        INDEX ix_im_outbound_delivery_run (runtime_run_id),
        INDEX ix_im_outbound_delivery_claim
            (channel_id, status, next_attempt_at, updated_at)
    );

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_onboarding_session'
       AND COLUMN_NAME = 'application_agent_id';
    IF object_count = 0 THEN
        ALTER TABLE im_onboarding_session
            ADD COLUMN application_agent_id BIGINT NULL;
    END IF;
    ALTER TABLE im_onboarding_session
        MODIFY COLUMN runtime_workspace_id BIGINT NULL;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_onboarding_session'
       AND INDEX_NAME = 'ix_im_onboarding_application';
    IF object_count = 0 THEN
        ALTER TABLE im_onboarding_session
            ADD INDEX ix_im_onboarding_application
            (application_agent_id, provider, status);
    END IF;

    -- One WeCom lane may have one executing batch and one mutable successor.
    -- Nullable unique slot keys provide this invariant on both MySQL 5.7 and
    -- MySQL 8 without partial indexes.
    CREATE TABLE IF NOT EXISTS im_execution_batch (
        id BIGINT NOT NULL AUTO_INCREMENT,
        channel_id BIGINT NOT NULL,
        target_type VARCHAR(32) NOT NULL,
        ordering_key VARCHAR(255) NOT NULL,
        execution_slot_key VARCHAR(320) NULL,
        collection_slot_key VARCHAR(320) NULL,
        state VARCHAR(32) NOT NULL DEFAULT 'collecting',
        response_inbound_message_id BIGINT NULL,
        predecessor_batch_id BIGINT NULL,
        predecessor_run_kind VARCHAR(32) NULL,
        predecessor_run_row_id BIGINT NULL,
        predecessor_runtime_run_id VARCHAR(128) NULL,
        execution_run_kind VARCHAR(32) NULL,
        execution_run_row_id BIGINT NULL,
        execution_runtime_run_id VARCHAR(128) NULL,
        cancel_command_id VARCHAR(128) NULL,
        payload_digest VARCHAR(64) NULL,
        sealed_payload JSON NULL,
        processing_owner VARCHAR(160) NULL,
        processing_lease_epoch BIGINT NULL,
        next_attempt_at DATETIME NULL,
        collected_at DATETIME NOT NULL,
        interrupt_requested_at DATETIME NULL,
        predecessor_released_at DATETIME NULL,
        sealed_at DATETIME NULL,
        accepted_at DATETIME NULL,
        terminal_at DATETIME NULL,
        terminal_status VARCHAR(32) NULL,
        terminal_reason VARCHAR(100) NULL,
        error_code VARCHAR(100) NULL,
        error_message TEXT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_im_execution_batch_execution_slot
            UNIQUE (execution_slot_key),
        CONSTRAINT uq_im_execution_batch_collection_slot
            UNIQUE (collection_slot_key),
        CONSTRAINT fk_im_execution_batch_channel
            FOREIGN KEY (channel_id) REFERENCES im_channel(id)
            ON DELETE CASCADE,
        CONSTRAINT fk_im_execution_batch_predecessor
            FOREIGN KEY (predecessor_batch_id)
            REFERENCES im_execution_batch(id) ON DELETE SET NULL,
        INDEX ix_im_execution_batch_channel_id (channel_id),
        INDEX ix_im_execution_batch_state (state),
        INDEX ix_im_execution_batch_response (response_inbound_message_id),
        INDEX ix_im_execution_batch_recovery
            (state, next_attempt_at, updated_at),
        INDEX ix_im_execution_batch_lane
            (channel_id, ordering_key, id)
    );

    CREATE TABLE IF NOT EXISTS im_ingress_ticket_counter (
        channel_id BIGINT NOT NULL,
        ingress_ordering_key VARCHAR(255) NOT NULL,
        last_ticket BIGINT NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (channel_id, ingress_ordering_key),
        CONSTRAINT fk_im_ingress_ticket_counter_channel
            FOREIGN KEY (channel_id)
            REFERENCES im_channel(id) ON DELETE CASCADE
    );

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'ingress_ordering_key';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN ingress_ordering_key VARCHAR(255) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'ingress_ticket';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN ingress_ticket BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'delivery_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN delivery_id VARCHAR(256) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'client_message_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN client_message_id VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'dispatch_lane';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN dispatch_lane VARCHAR(32) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'ingress_ticket_state';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN ingress_ticket_state VARCHAR(32) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'ingress_payload_digest';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN ingress_payload_digest VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'terminal_outcome_digest';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN terminal_outcome_digest VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'runtime_disposition_seq';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN runtime_disposition_seq BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'runtime_disposition_state';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN runtime_disposition_state VARCHAR(32) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'runtime_canonical_run_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN runtime_canonical_run_id VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'runtime_parent_run_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN runtime_parent_run_id VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'runtime_start_after_seq';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN runtime_start_after_seq BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'uq_im_inbound_ingress_ticket';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD UNIQUE INDEX uq_im_inbound_ingress_ticket
            (channel_id, ingress_ordering_key, ingress_ticket);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'uq_im_inbound_delivery_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD UNIQUE INDEX uq_im_inbound_delivery_id (delivery_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'uq_im_inbound_client_message_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD UNIQUE INDEX uq_im_inbound_client_message_id
            (client_message_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'ix_im_inbound_runtime_canonical_run';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD INDEX ix_im_inbound_runtime_canonical_run
            (channel_id, runtime_canonical_run_id, runtime_start_after_seq);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'ix_im_inbound_dispatch_lane';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD INDEX ix_im_inbound_dispatch_lane
            (dispatch_lane, ingress_ticket_state, next_attempt_at, id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'target_type';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN target_type VARCHAR(32) NOT NULL
            DEFAULT 'personal_agent';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'application_agent_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN application_agent_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'chat_type';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN chat_type VARCHAR(16) NOT NULL DEFAULT 'direct';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'sender_member_id_digest';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN sender_member_id_digest VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'sender_member_id_encrypted';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN sender_member_id_encrypted TEXT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'sender_platform_user_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN sender_platform_user_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'ordering_key';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN ordering_key VARCHAR(255) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'execution_batch_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN execution_batch_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'batch_ordinal';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN batch_ordinal INT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.KEY_COLUMN_USAGE
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'execution_batch_id'
       AND REFERENCED_TABLE_NAME = 'im_execution_batch';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD CONSTRAINT fk_im_inbound_execution_batch
            FOREIGN KEY (execution_batch_id)
            REFERENCES im_execution_batch(id) ON DELETE SET NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'uq_im_inbound_batch_ordinal';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD UNIQUE INDEX uq_im_inbound_batch_ordinal
            (execution_batch_id, batch_ordinal);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'identity_status';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN identity_status VARCHAR(32) NOT NULL
            DEFAULT 'not_required';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'capacity_wait_attempt_count';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN capacity_wait_attempt_count INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'capacity_wait_started_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN capacity_wait_started_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'application_agent_run_id';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN application_agent_run_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'db_committed_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN db_committed_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'scheduler_claimed_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN scheduler_claimed_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'identity_resolved_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN identity_resolved_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'run_created_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN run_created_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'runtime_submitted_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN runtime_submitted_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'first_runtime_public_event_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN first_runtime_public_event_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'first_provider_dispatch_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN first_provider_dispatch_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND COLUMN_NAME = 'first_provider_confirmed_at';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD COLUMN first_provider_confirmed_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'ix_im_inbound_ordering_fifo';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD INDEX ix_im_inbound_ordering_fifo
            (
                channel_id,
                ordering_key,
                status,
                next_attempt_at,
                received_at,
                id
            );
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'ix_im_inbound_application';
    IF object_count = 0 THEN
        ALTER TABLE im_inbound_message
            ADD INDEX ix_im_inbound_application
            (application_agent_id, received_at);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_message_attachment'
       AND COLUMN_NAME = 'runtime_relative_path';
    IF object_count = 0 THEN
        ALTER TABLE im_message_attachment
            ADD COLUMN runtime_relative_path VARCHAR(1000) NULL;
    END IF;

    -- backend/.env is the only desired Hermes Runtime image source. Bindings
    -- keep only the bound image, disposable storage pointer, and upgrade lock.
    SET legacy_foreign_key_name = NULL;
    SELECT CONSTRAINT_NAME
      INTO legacy_foreign_key_name
      FROM information_schema.KEY_COLUMN_USAGE
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'current_release_id'
       AND REFERENCED_TABLE_NAME IS NOT NULL
     LIMIT 1;
    IF legacy_foreign_key_name IS NOT NULL THEN
        SET @legacy_drop_foreign_key_sql = CONCAT(
            'ALTER TABLE hermes_runtime_binding DROP FOREIGN KEY `',
            REPLACE(legacy_foreign_key_name, '`', '``'),
            '`'
        );
        PREPARE legacy_drop_foreign_key_statement FROM @legacy_drop_foreign_key_sql;
        EXECUTE legacy_drop_foreign_key_statement;
        DEALLOCATE PREPARE legacy_drop_foreign_key_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'current_release_id';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN current_release_id;
    END IF;

    SET legacy_foreign_key_name = NULL;
    SELECT CONSTRAINT_NAME
      INTO legacy_foreign_key_name
      FROM information_schema.KEY_COLUMN_USAGE
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'desired_release_id'
       AND REFERENCED_TABLE_NAME IS NOT NULL
     LIMIT 1;
    IF legacy_foreign_key_name IS NOT NULL THEN
        SET @legacy_drop_foreign_key_sql = CONCAT(
            'ALTER TABLE hermes_runtime_binding DROP FOREIGN KEY `',
            REPLACE(legacy_foreign_key_name, '`', '``'),
            '`'
        );
        PREPARE legacy_drop_foreign_key_statement FROM @legacy_drop_foreign_key_sql;
        EXECUTE legacy_drop_foreign_key_statement;
        DEALLOCATE PREPARE legacy_drop_foreign_key_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'desired_release_id';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN desired_release_id;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'upgrade_lock_job_id';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN upgrade_lock_job_id;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'image_version';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN image_version;
    END IF;

    SET legacy_foreign_key_name = NULL;
    SELECT CONSTRAINT_NAME
      INTO legacy_foreign_key_name
      FROM information_schema.KEY_COLUMN_USAGE
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'current_release_id'
       AND REFERENCED_TABLE_NAME IS NOT NULL
     LIMIT 1;
    IF legacy_foreign_key_name IS NOT NULL THEN
        SET @legacy_drop_foreign_key_sql = CONCAT(
            'ALTER TABLE application_agent_runtime_binding DROP FOREIGN KEY `',
            REPLACE(legacy_foreign_key_name, '`', '``'),
            '`'
        );
        PREPARE legacy_drop_foreign_key_statement FROM @legacy_drop_foreign_key_sql;
        EXECUTE legacy_drop_foreign_key_statement;
        DEALLOCATE PREPARE legacy_drop_foreign_key_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'current_release_id';
    IF object_count > 0 THEN
        ALTER TABLE application_agent_runtime_binding
            DROP COLUMN current_release_id;
    END IF;

    SET legacy_foreign_key_name = NULL;
    SELECT CONSTRAINT_NAME
      INTO legacy_foreign_key_name
      FROM information_schema.KEY_COLUMN_USAGE
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'desired_release_id'
       AND REFERENCED_TABLE_NAME IS NOT NULL
     LIMIT 1;
    IF legacy_foreign_key_name IS NOT NULL THEN
        SET @legacy_drop_foreign_key_sql = CONCAT(
            'ALTER TABLE application_agent_runtime_binding DROP FOREIGN KEY `',
            REPLACE(legacy_foreign_key_name, '`', '``'),
            '`'
        );
        PREPARE legacy_drop_foreign_key_statement FROM @legacy_drop_foreign_key_sql;
        EXECUTE legacy_drop_foreign_key_statement;
        DEALLOCATE PREPARE legacy_drop_foreign_key_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'desired_release_id';
    IF object_count > 0 THEN
        ALTER TABLE application_agent_runtime_binding
            DROP COLUMN desired_release_id;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = 'upgrade_lock_job_id';
    IF object_count > 0 THEN
        ALTER TABLE application_agent_runtime_binding
            DROP COLUMN upgrade_lock_job_id;
    END IF;

    -- Prompt Contract V2 is acknowledged only after the Runtime accepts the
    -- first turn.  Persisting the acknowledged digest avoids resending the
    -- large, stable platform contract on every Application Agent follow-up.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_conversation'
       AND COLUMN_NAME = 'prompt_contract_version';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_conversation
            ADD COLUMN prompt_contract_version INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_conversation'
       AND COLUMN_NAME = 'prompt_contract_digest';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_conversation
            ADD COLUMN prompt_contract_digest VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_conversation'
       AND COLUMN_NAME = 'prompt_contract_confirmed_at';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_conversation
            ADD COLUMN prompt_contract_confirmed_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'idempotency_key';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN idempotency_key VARCHAR(255) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'payload_digest';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN payload_digest VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'response_snapshot';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN response_snapshot JSON NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'submission_state';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN submission_state VARCHAR(32)
                NOT NULL DEFAULT 'reserved';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'submit_intent_at';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN submit_intent_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'dashboard_accepted_at';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN dashboard_accepted_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND COLUMN_NAME = 'capacity_released_at';
    IF object_count = 0 THEN
        ALTER TABLE application_agent_run
            ADD COLUMN capacity_released_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_run'
       AND INDEX_NAME =
           'uq_application_agent_run_channel_idempotency'
       AND (
            (SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'application_agent_id')
         OR (SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'application_client_id')
         OR (SEQ_IN_INDEX = 3 AND COLUMN_NAME = 'channel')
         OR (SEQ_IN_INDEX = 4 AND COLUMN_NAME = 'idempotency_key')
       );
    IF object_count <> 4 THEN
        SELECT COUNT(*) INTO object_count
          FROM information_schema.STATISTICS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'application_agent_run'
           AND INDEX_NAME =
               'uq_application_agent_run_channel_idempotency';
        IF object_count > 0 THEN
            ALTER TABLE application_agent_run
                DROP INDEX
                    uq_application_agent_run_channel_idempotency;
        END IF;
        SELECT COUNT(*) INTO invalid_count
          FROM (
                SELECT application_agent_id,
                       application_client_id,
                       channel,
                       idempotency_key
                  FROM application_agent_run
                 WHERE idempotency_key IS NOT NULL
                 GROUP BY application_agent_id,
                          application_client_id,
                          channel,
                          idempotency_key
                HAVING COUNT(*) > 1
          ) AS duplicate_application_run_idempotency;
        IF invalid_count > 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT =
                    'duplicate application_agent_run idempotency key';
        END IF;
        ALTER TABLE application_agent_run
            ADD UNIQUE KEY
                uq_application_agent_run_channel_idempotency
                (
                    application_agent_id,
                    application_client_id,
                    channel,
                    idempotency_key
                );
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_inbound_message'
       AND INDEX_NAME = 'uq_im_inbound_application_agent_run';
    IF object_count = 0 THEN
        SELECT COUNT(*) INTO invalid_count
          FROM (
                SELECT application_agent_run_id
                  FROM im_inbound_message
                 WHERE application_agent_run_id IS NOT NULL
                 GROUP BY application_agent_run_id
                HAVING COUNT(*) > 1
          ) AS duplicate_application_agent_runs;
        IF invalid_count > 0 THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT =
                    'duplicate im_inbound_message application_agent_run_id';
        END IF;
        ALTER TABLE im_inbound_message
            ADD UNIQUE KEY uq_im_inbound_application_agent_run
                (application_agent_run_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'chat_file_artifact'
       AND COLUMN_NAME = 'sha256_digest';
    IF object_count = 0 THEN
        ALTER TABLE chat_file_artifact
            ADD COLUMN sha256_digest VARCHAR(64) NULL;
    END IF;

    -- Runtime compute environments are disposable; deployment incarnation and
    -- observed Knative Revision fields are not authoritative database state.
    SET legacy_column_name = CONCAT('runtime_', 'generation');
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = legacy_column_name;
    IF object_count > 0 THEN
        SET @hermes_schema_sql = CONCAT(
            'ALTER TABLE hermes_runtime_binding DROP COLUMN `',
            legacy_column_name,
            '`'
        );
        PREPARE hermes_schema_statement FROM @hermes_schema_sql;
        EXECUTE hermes_schema_statement;
        DEALLOCATE PREPARE hermes_schema_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'application_agent_runtime_binding'
       AND COLUMN_NAME = legacy_column_name;
    IF object_count > 0 THEN
        SET @hermes_schema_sql = CONCAT(
            'ALTER TABLE application_agent_runtime_binding DROP COLUMN `',
            legacy_column_name,
            '`'
        );
        PREPARE hermes_schema_statement FROM @hermes_schema_sql;
        EXECUTE hermes_schema_statement;
        DEALLOCATE PREPARE hermes_schema_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'model_usage_log'
       AND COLUMN_NAME = legacy_column_name;
    IF object_count > 0 THEN
        SET @hermes_schema_sql = CONCAT(
            'ALTER TABLE model_usage_log DROP COLUMN `',
            legacy_column_name,
            '`'
        );
        PREPARE hermes_schema_statement FROM @hermes_schema_sql;
        EXECUTE hermes_schema_statement;
        DEALLOCATE PREPARE hermes_schema_statement;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'latest_created_revision';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN latest_created_revision;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'latest_ready_revision';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN latest_ready_revision;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'hermes_runtime_binding'
       AND COLUMN_NAME = 'traffic_revision';
    IF object_count > 0 THEN
        ALTER TABLE hermes_runtime_binding
            DROP COLUMN traffic_revision;
    END IF;

    -- A terminal WeCom answer is durable before any provider call.  The
    -- digest makes retries of the same Runtime result idempotent without
    -- adding another delivery table.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'im_delivery_checkpoint'
       AND COLUMN_NAME = 'delivery_digest';
    IF object_count = 0 THEN
        ALTER TABLE im_delivery_checkpoint
            ADD COLUMN delivery_digest VARCHAR(64) NULL;
    END IF;

    -- Profile Desired/Applied heads are the only execution-facing pointers in
    -- the unified asset model.  Existing generation columns remain readable
    -- until the one-time cutover but are not populated by the new path.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'desired_revision';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN desired_revision INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'applied_revision';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN applied_revision INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'desired_digest';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN desired_digest VARCHAR(71) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'applied_digest';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN applied_digest VARCHAR(71) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'effective_generation';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN effective_generation INT NOT NULL DEFAULT 0;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'capability_health';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN capability_health JSON NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND COLUMN_NAME = 'active_display_name_key';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD COLUMN active_display_name_key VARCHAR(200) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_agent_profile'
       AND INDEX_NAME = 'uq_runtime_agent_profile_workspace_active_display_name';
    IF object_count = 0 THEN
        ALTER TABLE runtime_agent_profile
            ADD CONSTRAINT uq_runtime_agent_profile_workspace_active_display_name
            UNIQUE (runtime_workspace_id, active_display_name_key);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'installed_expert_binding'
       AND COLUMN_NAME = 'installation_epoch';
    IF object_count = 0 THEN
        ALTER TABLE installed_expert_binding
            ADD COLUMN installation_epoch INT NOT NULL DEFAULT 1;
    END IF;

    -- The one-time asset migration fills these stable SkillHub references
    -- before its finalizer retires the former identity and tables.
    -- Application code has no fallback to that identity.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user_skillhub_installation'
       AND COLUMN_NAME = 'skill_id';
    IF object_count = 0 THEN
        ALTER TABLE user_skillhub_installation
            ADD COLUMN skill_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user_skillhub_installation'
       AND COLUMN_NAME = 'installed_skill_version_id';
    IF object_count = 0 THEN
        ALTER TABLE user_skillhub_installation
            ADD COLUMN installed_skill_version_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'user_skillhub_installation'
       AND COLUMN_NAME = 'source_status';
    IF object_count = 0 THEN
        ALTER TABLE user_skillhub_installation
            ADD COLUMN source_status VARCHAR(32) NULL;
    END IF;

    -- The existing logical Skill tables become the unified public, personal
    -- and reviewed-artifact catalog at cutover.  Adding nullable/source
    -- metadata is safe before data classification; the preflight blocks any
    -- ambiguous row from being promoted.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'application_agent_id';
    IF object_count = 0 THEN
        ALTER TABLE skill
            ADD COLUMN application_agent_id BIGINT NULL;
        CREATE INDEX ix_skill_application_agent_id
            ON skill (application_agent_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'asset_scope';
    IF object_count = 0 THEN
        ALTER TABLE skill
            ADD COLUMN asset_scope VARCHAR(16) NOT NULL DEFAULT 'personal';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'visibility';
    IF object_count = 0 THEN
        ALTER TABLE skill
            ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'private';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'category_key';
    IF object_count = 0 THEN
        ALTER TABLE skill
            ADD COLUMN category_key VARCHAR(64) NOT NULL DEFAULT 'other';
        CREATE INDEX ix_skill_category_key ON skill (category_key);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'archived_at';
    IF object_count = 0 THEN
        ALTER TABLE skill ADD COLUMN archived_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'revoked_at';
    IF object_count = 0 THEN
        ALTER TABLE skill ADD COLUMN revoked_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND COLUMN_NAME = 'market_visible';
    IF object_count = 0 THEN
        ALTER TABLE skill
            ADD COLUMN market_visible BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill'
       AND INDEX_NAME = 'ix_skill_market_visible';
    IF object_count = 0 THEN
        CREATE INDEX ix_skill_market_visible ON skill (market_visible);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base'
       AND COLUMN_NAME = 'application_agent_id';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base
            ADD COLUMN application_agent_id BIGINT NULL;
        CREATE INDEX ix_knowledge_base_application_agent_id
            ON knowledge_base (application_agent_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base'
       AND COLUMN_NAME = 'provider_capabilities';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base
            ADD COLUMN provider_capabilities JSON NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base'
       AND COLUMN_NAME = 'provider_capabilities_synced_at';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base
            ADD COLUMN provider_capabilities_synced_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base_file'
       AND COLUMN_NAME = 'provider_document_id';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base_file
            ADD COLUMN provider_document_id VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base_file'
       AND COLUMN_NAME = 'provider_status';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base_file
            ADD COLUMN provider_status VARCHAR(32) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base_file'
       AND COLUMN_NAME = 'provider_error';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base_file
            ADD COLUMN provider_error TEXT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base_file'
       AND COLUMN_NAME = 'provider_synced_at';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base_file
            ADD COLUMN provider_synced_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base_file'
       AND COLUMN_NAME = 'oss_url'
       AND IS_NULLABLE = 'NO';
    IF object_count > 0 THEN
        ALTER TABLE knowledge_base_file
            MODIFY COLUMN oss_url VARCHAR(1000) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'knowledge_base_file'
       AND INDEX_NAME = 'uq_kb_file_provider_document';
    IF object_count = 0 THEN
        ALTER TABLE knowledge_base_file
            ADD UNIQUE INDEX uq_kb_file_provider_document (provider_document_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill_version'
       AND COLUMN_NAME = 'package_sha256';
    IF object_count = 0 THEN
        ALTER TABLE skill_version ADD COLUMN package_sha256 VARCHAR(64) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'skill_version'
       AND COLUMN_NAME = 'published_at';
    IF object_count = 0 THEN
        ALTER TABLE skill_version ADD COLUMN published_at DATETIME NULL;
    END IF;

    -- ``mcp_version`` is created from SQLAlchemy metadata before this
    -- procedure.  These columns turn the pre-existing logical MCP row into
    -- its stable catalog head without copying any plaintext credential.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'mcp'
       AND COLUMN_NAME = 'description';
    IF object_count = 0 THEN
        ALTER TABLE mcp ADD COLUMN description TEXT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'mcp'
       AND COLUMN_NAME = 'visibility';
    IF object_count = 0 THEN
        ALTER TABLE mcp
            ADD COLUMN visibility VARCHAR(16) NOT NULL DEFAULT 'private';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'mcp'
       AND COLUMN_NAME = 'current_version_id';
    IF object_count = 0 THEN
        ALTER TABLE mcp ADD COLUMN current_version_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'mcp'
       AND COLUMN_NAME = 'archived_at';
    IF object_count = 0 THEN
        ALTER TABLE mcp ADD COLUMN archived_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'mcp'
       AND COLUMN_NAME = 'revoked_at';
    IF object_count = 0 THEN
        ALTER TABLE mcp ADD COLUMN revoked_at DATETIME NULL;
    END IF;

    -- Retire the old irreversible "normal unlist" axis.  Preserve the old
    -- unavailable intent as a reversible execution stop until an admin
    -- explicitly restores the asset.
    UPDATE skill
       SET revoked_at = COALESCE(revoked_at, archived_at, UTC_TIMESTAMP()),
           archived_at = NULL,
           status = 'published'
     WHERE asset_scope = 'public'
       AND archived_at IS NOT NULL;

    UPDATE mcp
       SET revoked_at = COALESCE(revoked_at, archived_at, UTC_TIMESTAMP()),
           archived_at = NULL,
           status = CASE
               WHEN visibility = 'private' THEN 'private'
               ELSE 'published'
           END
     WHERE archived_at IS NOT NULL;

    -- Active MCP business names are leased independently from immutable
    -- historical assets.  This permits terminal revoke + same-slug recreate
    -- while Runtime identity remains based on mcp.id.
    CREATE TABLE IF NOT EXISTS mcp_slug_lease (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        namespace_key VARCHAR(64) NOT NULL,
        slug VARCHAR(128) NOT NULL,
        mcp_id BIGINT NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uniq_mcp_slug_lease_namespace
            UNIQUE (org_id, namespace_key, slug),
        CONSTRAINT uniq_mcp_slug_lease_asset UNIQUE (mcp_id),
        CONSTRAINT fk_mcp_slug_lease_org
            FOREIGN KEY (org_id) REFERENCES organization (id),
        CONSTRAINT fk_mcp_slug_lease_asset
            FOREIGN KEY (mcp_id) REFERENCES mcp (id) ON DELETE CASCADE,
        INDEX ix_mcp_slug_lease_org_id (org_id),
        INDEX ix_mcp_slug_lease_org_asset (org_id, mcp_id)
    );

    SELECT COUNT(*) INTO invalid_count
      FROM (
            SELECT org_id,
                   CASE
                       WHEN visibility = 'private'
                           THEN CONCAT('user:', owner_user_id)
                       ELSE 'organization'
                   END AS namespace_key,
                   slug
              FROM mcp
             WHERE status NOT IN ('deleted', 'revoked')
               AND revoked_at IS NULL
             GROUP BY org_id, namespace_key, slug
            HAVING COUNT(*) > 1
      ) AS duplicate_active_mcp_slugs;
    IF invalid_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'duplicate active MCP slug namespace';
    END IF;

    INSERT IGNORE INTO mcp_slug_lease (
        org_id,
        namespace_key,
        slug,
        mcp_id,
        created_at,
        updated_at
    )
    SELECT org_id,
           CASE
               WHEN visibility = 'private'
                   THEN CONCAT('user:', owner_user_id)
               ELSE 'organization'
           END,
           slug,
           id,
           UTC_TIMESTAMP(),
           UTC_TIMESTAMP()
      FROM mcp
     WHERE status NOT IN ('deleted', 'revoked')
       AND revoked_at IS NULL;

    SELECT COUNT(*) INTO invalid_count
      FROM mcp
      LEFT JOIN mcp_slug_lease
        ON mcp_slug_lease.mcp_id = mcp.id
       AND mcp_slug_lease.org_id = mcp.org_id
       AND mcp_slug_lease.slug = mcp.slug
       AND mcp_slug_lease.namespace_key =
           CASE
               WHEN mcp.visibility = 'private'
                   THEN CONCAT('user:', mcp.owner_user_id)
               ELSE 'organization'
           END
     WHERE mcp.status NOT IN ('deleted', 'revoked')
       AND mcp.revoked_at IS NULL
       AND mcp_slug_lease.id IS NULL;
    IF invalid_count > 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'active MCP slug lease backfill incomplete';
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'mcp'
       AND INDEX_NAME = 'uq_mcp_owner_slug';
    IF object_count > 0 THEN
        ALTER TABLE mcp DROP INDEX uq_mcp_owner_slug;
    END IF;

    -- Registry reload evidence correlates an immediate process-wide reload
    -- with Runs that were already active at that moment.
    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_mcp_registry_state'
       AND COLUMN_NAME = 'last_reload_at';
    IF object_count = 0 THEN
        ALTER TABLE runtime_mcp_registry_state
            ADD COLUMN last_reload_at DATETIME NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_mcp_registry_state'
       AND COLUMN_NAME = 'last_reload_affected_run_ids';
    IF object_count = 0 THEN
        ALTER TABLE runtime_mcp_registry_state
            ADD COLUMN last_reload_affected_run_ids JSON NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_session'
       AND COLUMN_NAME = 'context_source_runtime_session_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_session
            ADD COLUMN context_source_runtime_session_id VARCHAR(128) NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_session'
       AND INDEX_NAME = 'ix_runtime_session_context_source_runtime_session_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_session
            ADD INDEX ix_runtime_session_context_source_runtime_session_id
                (context_source_runtime_session_id);
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'runtime_session'
       AND COLUMN_NAME = 'context_head_runtime_session_id';
    IF object_count = 0 THEN
        ALTER TABLE runtime_session
            ADD COLUMN context_head_runtime_session_id VARCHAR(128) NULL;
    END IF;

    UPDATE runtime_session root
      JOIN runtime_session branch
        ON branch.runtime_workspace_id = root.runtime_workspace_id
       AND branch.context_source_runtime_session_id = root.runtime_session_id
       AND branch.status IN ('active', 'expert_deleted')
      LEFT JOIN runtime_session newer_branch
        ON newer_branch.runtime_workspace_id = branch.runtime_workspace_id
       AND newer_branch.context_source_runtime_session_id = branch.context_source_runtime_session_id
       AND newer_branch.status IN ('active', 'expert_deleted')
       AND newer_branch.id > branch.id
      SET root.context_head_runtime_session_id = branch.runtime_session_id
      WHERE root.context_source_runtime_session_id IS NULL
        AND root.context_head_runtime_session_id IS NULL
        AND newer_branch.id IS NULL;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'chat_log'
       AND COLUMN_NAME = 'context_runtime_agent_profile_id';
    IF object_count = 0 THEN
        ALTER TABLE chat_log
            ADD COLUMN context_runtime_agent_profile_id BIGINT NULL;
    END IF;

    SELECT COUNT(*) INTO object_count
      FROM information_schema.STATISTICS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'chat_log'
       AND INDEX_NAME = 'ix_chat_log_context_runtime_agent_profile_id';
    IF object_count = 0 THEN
        ALTER TABLE chat_log
            ADD INDEX ix_chat_log_context_runtime_agent_profile_id
                (context_runtime_agent_profile_id);
    END IF;

    UPDATE chat_log log
      JOIN runtime_session target
        ON target.id = log.session_id
       AND target.runtime_workspace_id = log.runtime_workspace_id
      LEFT JOIN runtime_session source
        ON source.runtime_workspace_id = target.runtime_workspace_id
       AND source.runtime_session_id = target.context_source_runtime_session_id
      SET log.context_runtime_agent_profile_id = CASE
          WHEN log.ingest_source = 'hermes_backfill'
               AND source.runtime_agent_profile_id IS NOT NULL
            THEN source.runtime_agent_profile_id
          ELSE target.runtime_agent_profile_id
      END
     WHERE log.context_runtime_agent_profile_id IS NULL;

    -- Runtime Edge admission evidence includes rejected requests that have no
    -- RuntimeRun yet, so it cannot reuse the run projection receipt table.
    CREATE TABLE IF NOT EXISTS runtime_admission_audit (
        id BIGINT NOT NULL AUTO_INCREMENT,
        event_id VARCHAR(128) NOT NULL,
        payload_digest VARCHAR(64) NOT NULL,
        runtime_id VARCHAR(128) NOT NULL,
        binding_id VARCHAR(128) NOT NULL,
        org_id BIGINT NOT NULL,
        user_id BIGINT NULL,
        runtime_workspace_id BIGINT NULL,
        runtime_session_id VARCHAR(256) NULL,
        request_id VARCHAR(128) NULL,
        client_message_id VARCHAR(128) NULL,
        runtime_run_id VARCHAR(128) NULL,
        operation VARCHAR(64) NOT NULL,
        status VARCHAR(64) NOT NULL,
        outcome VARCHAR(32) NOT NULL,
        stage VARCHAR(128) NOT NULL,
        error_code VARCHAR(128) NULL,
        exception_type VARCHAR(128) NULL,
        profile_id VARCHAR(128) NULL,
        model_id VARCHAR(128) NULL,
        config_revision VARCHAR(128) NULL,
        attachment_count INT NULL,
        prompt_bytes INT NULL,
        upstream_status INT NULL,
        upstream_path VARCHAR(512) NULL,
        occurred_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_runtime_admission_audit_event UNIQUE (event_id),
        INDEX ix_runtime_admission_audit_org_occurred (org_id, occurred_at),
        INDEX ix_runtime_admission_audit_runtime_occurred (runtime_id, occurred_at),
        INDEX ix_runtime_admission_audit_session_occurred (runtime_session_id, occurred_at),
        INDEX ix_runtime_admission_audit_message_occurred (client_message_id, occurred_at),
        INDEX ix_runtime_admission_audit_run_occurred (runtime_run_id, occurred_at),
        INDEX ix_runtime_admission_audit_error_occurred (outcome, error_code, occurred_at)
    );

    CREATE TABLE IF NOT EXISTS app_center_app (
        id BIGINT NOT NULL AUTO_INCREMENT,
        org_id BIGINT NOT NULL,
        name VARCHAR(64) NOT NULL,
        description VARCHAR(64) NOT NULL DEFAULT 'HTML 应用',
        source_type VARCHAR(16) NOT NULL,
        share_mode VARCHAR(8) NOT NULL,
        html_content LONGTEXT NULL,
        package_blob LONGBLOB NOT NULL,
        access_url VARCHAR(512) NOT NULL,
        demo_id VARCHAR(32) NOT NULL,
        demo_code VARCHAR(64) NOT NULL,
        publish_version_id VARCHAR(32) NOT NULL,
        business_domain VARCHAR(128) NULL,
        creator_user_id BIGINT NOT NULL,
        copied_from_app_id BIGINT NULL,
        copied_from_creator_name VARCHAR(100) NULL,
        source_session_id VARCHAR(64) NULL,
        source_run_id VARCHAR(64) NULL,
        idempotency_key VARCHAR(64) NULL,
        idempotency_fingerprint VARCHAR(64) NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_app_center_idempotency
            UNIQUE (org_id, creator_user_id, idempotency_key),
        CONSTRAINT fk_app_center_org FOREIGN KEY (org_id) REFERENCES organization (id),
        CONSTRAINT fk_app_center_creator FOREIGN KEY (creator_user_id) REFERENCES user (id),
        INDEX ix_app_center_org_creator (org_id, creator_user_id),
        INDEX ix_app_center_org_updated (org_id, updated_at)
    );

    SELECT COUNT(*) INTO object_count
      FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = DATABASE()
       AND TABLE_NAME = 'app_center_app'
       AND COLUMN_NAME = 'description';
    IF object_count = 0 THEN
        ALTER TABLE app_center_app
            ADD COLUMN description VARCHAR(64) NOT NULL DEFAULT 'HTML 应用';
        UPDATE app_center_app
           SET description = LEFT(NULLIF(TRIM(SUBSTRING_INDEX(
               SUBSTRING_INDEX(
                   SUBSTRING_INDEX(html_content, '</title>', 1),
                   '<title',
                   -1
               ),
               '>',
               -1
           )), ''), 20)
         WHERE html_content LIKE '%<title>%';
        UPDATE app_center_app
           SET description = LEFT(NULLIF(TRIM(name), ''), 20)
         WHERE description = 'HTML 应用';
    END IF;

    DROP TABLE IF EXISTS alembic_version;
END$$

DELIMITER ;

CALL hermes_apply_current_schema();
DROP PROCEDURE hermes_apply_current_schema;
