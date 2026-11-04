from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_current_schema_sql_excludes_retired_skill_event_compatibility() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "runtime_skill_manage_event" not in content
    assert "DROP TABLE IF EXISTS alembic_version" in content
    assert "alembic upgrade" not in content.lower()
    assert "version_num" not in content


def test_current_schema_sql_is_repeat_safe_for_created_objects() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "information_schema.COLUMNS" in content
    assert "information_schema.STATISTICS" in content
    assert "information_schema.KEY_COLUMN_USAGE" in content
    assert "DROP PROCEDURE IF EXISTS hermes_apply_current_schema" in content
    assert "CREATE TABLE IF NOT EXISTS personal_skill_category" in content
    assert "uq_personal_skill_category_owner_name" in content
    assert "CREATE TABLE IF NOT EXISTS personal_config_bundle_artifact" in content
    assert "uq_pcb_artifact_ws_generation" in content
    assert "Retire the old irreversible" in content
    assert "revoked_at = COALESCE(revoked_at, archived_at, UTC_TIMESTAMP())" in content


def test_current_schema_sql_removes_application_agent_description() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "TABLE_NAME = 'application_agent'" in content
    assert "COLUMN_NAME = 'description'" in content
    assert "DROP COLUMN description" in content


def test_current_schema_sql_creates_enterprise_idp_admin_work_number_permission_table() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS admin_work_no_permissions" in content
    assert "uq_admin_work_no_permissions_org_work_no" in content
    assert "ix_admin_work_no_permissions_org_work_no_status" in content


def test_current_schema_sql_adds_cross_day_daily_presence_column() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "TABLE_NAME = 'user'" in content
    assert "COLUMN_NAME = 'last_daily_presence_at'" in content
    assert "ADD COLUMN last_daily_presence_at DATETIME NULL" in content


def test_current_schema_sql_adds_application_personal_knowledge_bindings() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    assert "ADD COLUMN personal_knowledge_binding_digest" in content
    assert (
        "CREATE TABLE IF NOT EXISTS "
        "application_agent_personal_knowledge_binding"
    ) in content
    assert "permission_revoked" in content
    assert "fk_application_agent_personal_kb_knowledge" not in content


def test_current_schema_sql_adds_weknora_file_identities() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    for column_name in (
        "provider_document_id",
        "provider_status",
        "provider_error",
        "provider_synced_at",
    ):
        assert "TABLE_NAME = 'knowledge_base_file'" in content
        assert f"COLUMN_NAME = '{column_name}'" in content
    assert "MODIFY COLUMN oss_url VARCHAR(1000) NULL" in content
    assert "ADD UNIQUE INDEX uq_kb_file_provider_document" in content


def test_current_schema_sql_adds_weknora_capability_snapshots() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    assert "TABLE_NAME = 'knowledge_base'" in content
    assert "COLUMN_NAME = 'provider_capabilities'" in content
    assert "ADD COLUMN provider_capabilities JSON NULL" in content
    assert "COLUMN_NAME = 'provider_capabilities_synced_at'" in content
    assert "ADD COLUMN provider_capabilities_synced_at DATETIME NULL" in content


def test_current_schema_sql_upgrades_application_asset_ownership_columns() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "TABLE_NAME = 'skill'" in content
    assert "COLUMN_NAME = 'application_agent_id'" in content
    assert "ADD COLUMN application_agent_id BIGINT NULL" in content
    assert "ix_skill_application_agent_id" in content
    assert "TABLE_NAME = 'knowledge_base'" in content
    assert "ix_knowledge_base_application_agent_id" in content


def test_current_schema_sql_adds_shared_wecom_identity_mapping() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN qw_token VARCHAR(100) NULL" in content
    assert "ADD INDEX ix_user_org_qw_token (org_id, qw_token)" in content


def test_current_schema_sql_adds_ttft_hot_path_objects() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS knowledge_context_epoch" in content
    assert "ADD COLUMN runtime_provider_run_id VARCHAR(128) NULL" in content
    assert "ix_runtime_run_session_status_id" in content
    assert "ADD COLUMN dedupe_key VARCHAR(128) NULL" in content
    assert "uq_runtime_run_event_dedupe" in content


def test_current_schema_sql_adds_model_runtime_capabilities() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "TABLE_NAME = 'model_entry'" in content
    assert "COLUMN_NAME = 'runtime_capabilities'" in content
    assert "ADD COLUMN runtime_capabilities JSON NULL" in content


def test_current_schema_sql_adds_application_capacity_wait_columns() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN capacity_wait_attempt_count INT NOT NULL DEFAULT 0" in content
    assert "ADD COLUMN capacity_wait_started_at DATETIME NULL" in content


def test_current_schema_sql_adds_wecom_delivery_digest() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "TABLE_NAME = 'im_delivery_checkpoint'" in content
    assert "COLUMN_NAME = 'delivery_digest'" in content
    assert "ADD COLUMN delivery_digest VARCHAR(64) NULL" in content


def test_current_schema_sql_adds_unified_profile_and_mcp_heads() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    for column_name in (
        "desired_revision",
        "applied_revision",
        "desired_digest",
        "applied_digest",
    ):
        assert "TABLE_NAME = 'runtime_agent_profile'" in content
        assert f"COLUMN_NAME = '{column_name}'" in content

    assert "CREATE TABLE IF NOT EXISTS mcp_slug_lease" in content
    assert "duplicate active MCP slug namespace" in content
    assert "active MCP slug lease backfill incomplete" in content
    assert "ALTER TABLE mcp DROP INDEX uq_mcp_owner_slug" in content
    for column_name in (
        "description",
        "visibility",
        "current_version_id",
        "archived_at",
        "revoked_at",
    ):
        assert "TABLE_NAME = 'mcp'" in content
        assert f"COLUMN_NAME = '{column_name}'" in content

    for table_name, column_names in {
        "skill": ("asset_scope", "visibility", "archived_at", "revoked_at"),
        "skill_version": (
            "package_sha256",
            "published_at",
        ),
    }.items():
        assert f"TABLE_NAME = '{table_name}'" in content
        for column_name in column_names:
            assert f"COLUMN_NAME = '{column_name}'" in content

    for column_name in (
        "last_reload_at",
        "last_reload_affected_run_ids",
    ):
        assert "TABLE_NAME = 'runtime_mcp_registry_state'" in content
        assert f"COLUMN_NAME = '{column_name}'" in content


def test_current_schema_sql_adds_personal_wecom_proactive_delivery() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN proactive_target_encrypted TEXT NULL" in content
    assert "ADD COLUMN proactive_target_digest VARCHAR(64) NULL" in content
    assert "ADD COLUMN proactive_target_confirmed_at DATETIME NULL" in content
    assert "CREATE TABLE IF NOT EXISTS im_outbound_delivery" in content
    assert "uq_im_outbound_delivery_key" in content
    assert "ix_im_outbound_delivery_claim" in content


def test_current_schema_sql_adds_wecom_execution_batches() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS im_execution_batch" in content
    assert "uq_im_execution_batch_execution_slot" in content
    assert "uq_im_execution_batch_collection_slot" in content
    assert "ADD COLUMN execution_batch_id BIGINT NULL" in content
    assert "ADD COLUMN batch_ordinal INT NULL" in content
    assert "uq_im_inbound_batch_ordinal" in content


def test_current_schema_sql_adds_commit_ordered_im_ingress_tickets() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS im_ingress_ticket_counter" in content
    assert "PRIMARY KEY (channel_id, ingress_ordering_key)" in content
    for column_name in (
        "ingress_ordering_key",
        "ingress_ticket",
        "delivery_id",
        "client_message_id",
        "dispatch_lane",
        "ingress_ticket_state",
        "ingress_payload_digest",
        "terminal_outcome_digest",
        "runtime_disposition_seq",
        "runtime_disposition_state",
        "runtime_canonical_run_id",
        "runtime_parent_run_id",
        "runtime_start_after_seq",
    ):
        assert f"COLUMN_NAME = '{column_name}'" in content
    assert "uq_im_inbound_ingress_ticket" in content
    assert "uq_im_inbound_delivery_id" in content
    assert "uq_im_inbound_client_message_id" in content
    assert "ix_im_inbound_dispatch_lane" in content
    assert "ix_im_inbound_runtime_canonical_run" in content


def test_current_schema_sql_backfills_legacy_application_studio_sessions() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "INSERT INTO application_agent_studio_session" in content
    assert "FROM application_agent_run AS run" in content
    assert "WHERE run.channel = 'studio'" in content
    assert "ON DUPLICATE KEY UPDATE" in content


def test_current_schema_sql_removes_member_invitation_url() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "DROP COLUMN member_access_url" in content
    assert "ADD COLUMN member_access_url" not in content


def test_current_schema_sql_does_not_carry_retired_asset_schema() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "public_skill_version" not in content
    assert "source_review_id" not in content
    assert "asset_review" not in content


def test_current_schema_sql_upgrades_expert_release_columns() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN origin_runtime_workspace_id BIGINT NULL" in content
    assert "ADD COLUMN active_name_key VARCHAR(200) NULL" in content
    assert "ADD COLUMN change_summary TEXT NULL" in content
    assert "uq_expert_owner_workspace_active_name" in content


def test_current_schema_sql_adds_skill_expert_integration_heads() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    for column_name in (
        "active_display_name_key",
        "market_name_key",
        "execution_status",
        "execution_status_changed_at",
        "execution_status_reason",
        "installation_epoch",
    ):
        assert f"COLUMN_NAME = '{column_name}'" in content
    assert "uq_runtime_agent_profile_workspace_active_display_name" in content
    assert "uq_expert_market_name_lease" in content


def test_current_schema_sql_removes_binding_release_pointers() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    for table_name in (
        "hermes_runtime_binding",
        "application_agent_runtime_binding",
    ):
        assert f"TABLE_NAME = '{table_name}'" in content
    assert "DROP COLUMN current_release_id" in content
    assert "DROP COLUMN desired_release_id" in content
    assert "COLUMN_NAME = 'upgrade_lock_job_id'" in content
    assert "DROP COLUMN upgrade_lock_job_id" in content
    assert "COLUMN_NAME = 'image_version'" in content
    assert "DROP COLUMN image_version" in content
    assert "active_storage_generation_id" not in content


def test_current_schema_sql_removes_runtime_incarnation_columns() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "SET legacy_column_name = CONCAT('runtime_', 'generation')" in content
    assert content.count("legacy_column_name") >= 7
    for column_name in (
        "latest_created_revision",
        "latest_ready_revision",
        "traffic_revision",
    ):
        assert f"COLUMN_NAME = '{column_name}'" in content
        assert f"DROP COLUMN {column_name}" in content


def test_current_schema_sql_adds_platform_control_keys_and_pvc_identity() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS runtime_platform_control_key" in content
    assert "root_key_encrypted VARCHAR(1000) NOT NULL" in content
    assert "uq_runtime_platform_control_key_version" in content
    assert "ADD COLUMN runtime_root_pvc_uid VARCHAR(128) NULL" in content
    assert "ADD COLUMN pvc_storage_identity_hash VARCHAR(71) NULL" in content
    assert "path/name-derived backfill is permitted" in content


def test_current_schema_sql_does_not_touch_retired_upgrade_tables() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    for table_name in (
        "hermes_runtime_release",
        "hermes_runtime_release_control",
        "hermes_upgrade_job",
        "hermes_upgrade_target",
        "hermes_upgrade_event",
        "hermes_runtime_schema_intent",
        "hermes_runtime_schema_source_facts",
        "hermes_runtime_incomplete_intent_supersession",
    ):
        assert table_name not in content


def test_current_schema_sql_excludes_retired_runtime_skill_compatibility() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(encoding="utf-8")

    assert "user_runtime_skill" not in content
    assert "runtime_skill_binding" not in content


def test_current_schema_sql_aligns_skill_market_visibility_index() -> None:
    content = (ROOT / "backend" / "scripts" / "current_schema.sql").read_text(
        encoding="utf-8"
    )

    assert "COLUMN_NAME = 'market_visible'" in content
    assert "CREATE INDEX ix_skill_market_visible ON skill (market_visible)" in content


def test_retired_upgrade_table_cleanup_is_explicit_and_fail_closed() -> None:
    content = (
        ROOT
        / "backend"
        / "scripts"
        / "drop_retired_hermes_upgrade_tables.sql"
    ).read_text(
        encoding="utf-8"
    )

    assert "CONFIRM_DROP_RETIRED_HERMES_UPGRADE_TABLES" in content
    assert "maintenance_state = 'frozen'" in content
    assert "grant_issuance_enabled = FALSE" in content
    assert "non-retired tables still reference" in content
    drop_order = (
        "hermes_upgrade_event",
        "hermes_upgrade_target",
        "hermes_runtime_incomplete_intent_supersession",
        "hermes_runtime_schema_source_facts",
        "hermes_runtime_schema_intent",
        "hermes_upgrade_job",
        "hermes_runtime_release_control",
        "hermes_runtime_release",
    )
    positions = [content.index(f"DROP TABLE IF EXISTS {name}") for name in drop_order]
    assert positions == sorted(positions)
    current_schema = (
        ROOT / "backend" / "scripts" / "current_schema.sql"
    ).read_text(encoding="utf-8")
    assert "drop_retired_hermes_upgrade_tables" not in current_schema
