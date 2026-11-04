from app.models import AdminWorkNoPermission
from scripts.schema_bootstrap import expected_schema, schema_errors


def test_schema_errors_reports_missing_table() -> None:
    assert schema_errors({"example": {"id"}}, {}) == ["missing table: example"]


def test_schema_errors_reports_missing_columns() -> None:
    assert schema_errors(
        {"example": {"id", "name"}},
        {"example": {"id"}},
    ) == ["example missing columns: name"]


def test_schema_errors_ignores_extra_tables_and_columns() -> None:
    assert (
        schema_errors(
            {"example": {"id"}},
            {"example": {"id", "legacy"}, "extra": {"id"}},
        )
        == []
    )


def test_current_model_contract_includes_knowledge_provider_fields() -> None:
    schema = expected_schema()

    assert {
        "kind",
        "provider",
        "ragflow_id",
        "provider_error",
        "provider_capabilities",
        "provider_capabilities_synced_at",
    }.issubset(schema["knowledge_base"])
    assert {
        "provider_document_id",
        "provider_status",
        "provider_error",
        "provider_synced_at",
    }.issubset(schema["knowledge_base_file"])


def test_current_model_contract_includes_enterprise_idp_admin_work_number_permissions() -> None:
    schema = expected_schema()

    assert "admin_work_no_permissions" in schema
    assert {
        "org_id",
        "work_no",
        "status",
        "remark",
        "created_by",
        "updated_by",
    }.issubset(schema["admin_work_no_permissions"])
    assert any(
        tuple(column.name for column in index.columns) == ("org_id", "work_no", "status")
        for index in AdminWorkNoPermission.__table__.indexes
    )


def test_current_model_contract_includes_weknora_rerank_policy() -> None:
    schema = expected_schema()

    assert {
        "id",
        "provider",
        "rerank_enabled",
        "updated_by_user_id",
        "created_at",
        "updated_at",
    }.issubset(schema["weknora_rerank_config"])


def test_current_model_contract_includes_personal_im_gateway() -> None:
    schema = expected_schema()

    assert {
        "im_channel",
        "im_channel_lease",
        "im_onboarding_session",
        "im_inbound_message",
        "im_message_attachment",
        "im_delivery_checkpoint",
        "im_outbound_delivery",
        "scheduled_task_execution_lease",
    }.issubset(schema)
    assert {
        "runtime_workspace_id",
        "runtime_session_row_id",
        "secret_encrypted",
        "config_generation",
        "proactive_target_encrypted",
        "proactive_target_digest",
        "proactive_target_confirmed_at",
    }.issubset(schema["im_channel"])
    assert {
        "processing_owner",
        "processing_lease_epoch",
        "runtime_run_row_id",
    }.issubset(schema["im_inbound_message"])
    assert {
        "provider_delivered_text",
        "delivered_artifact_ids",
        "last_runtime_event_seq",
    }.issubset(schema["im_delivery_checkpoint"])
    assert {
        "delivery_key",
        "channel_id",
        "scheduled_task_id",
        "runtime_run_row_id",
        "target_encrypted",
        "target_digest",
        "provider_delivered_text",
        "delivered_artifact_ids",
        "processing_owner",
        "processing_lease_epoch",
    }.issubset(schema["im_outbound_delivery"])
    assert {
        "prompt_contract_version",
        "prompt_contract_digest",
        "prompt_contract_confirmed_at",
    }.issubset(schema["application_agent_conversation"])
    assert {
        "idempotency_key",
        "submission_state",
        "submit_intent_at",
        "dashboard_accepted_at",
        "capacity_released_at",
    }.issubset(schema["application_agent_run"])


def test_current_model_contract_uses_backend_runtime_maintenance_authority() -> None:
    schema = expected_schema()

    assert {
        "maintenance_state",
        "maintenance_epoch",
        "maintenance_reason",
        "started_at",
        "completed_at",
        "admission_generation",
        "grant_issuance_enabled",
    }.issubset(schema["runtime_maintenance_control"])
    assert {
        "intent_id",
        "runtime_binding_type",
        "runtime_binding_id",
        "runtime_id",
        "pvc_storage_identity_hash",
        "target_image",
        "target_image_digest",
        "authorization_digest",
        "authorization_document",
        "status",
        "authorized_at",
        "completed_at",
        "last_error",
    }.issubset(schema["runtime_bootstrap_intent"])
    for legacy_table in (
        "hermes_runtime_release_control",
        "hermes_upgrade_job",
        "hermes_runtime_schema_intent",
    ):
        assert legacy_table not in schema


def test_current_model_contract_has_no_deployment_incarnation_columns() -> None:
    schema = expected_schema()
    retired = {
        "runtime_" + "generation",
        "latest_created_revision",
        "latest_ready_revision",
        "traffic_revision",
    }

    assert retired.isdisjoint(schema["hermes_runtime_binding"])
    assert ("runtime_" + "generation") not in schema["application_agent_runtime_binding"]
    assert ("runtime_" + "generation") not in schema["model_usage_log"]


def test_current_model_contract_includes_platform_control_key_and_pvc_identity() -> None:
    schema = expected_schema()

    assert {
        "binding_type",
        "binding_id",
        "runtime_id",
        "key_version",
        "root_key_encrypted",
        "status",
        "retired_at",
    }.issubset(schema["runtime_platform_control_key"])
    for table_name in (
        "hermes_runtime_binding",
        "application_agent_runtime_binding",
    ):
        assert {
            "runtime_root_pvc_uid",
            "pvc_storage_identity_hash",
        }.issubset(schema[table_name])
