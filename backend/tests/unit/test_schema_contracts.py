from pathlib import Path

from app.schemas.runtime_workspace import (
    DefaultModelUpdateResponse,
    RuntimeModelSummary,
    RuntimeRunSummary,
    RuntimeWorkspaceSummary,
)
from app.schemas.auth import AuthPrincipalSummary, AuthTokenResponse, LoginRequest
from app.schemas.admin import (
    AdminAuditLogSummary,
    AdminModelConfigResponse,
    AdminModelStatusRequest,
    AdminModelCatalogPublishRequest,
    AdminModelCatalogPublishResponse,
    AdminModelSummary,
    AdminModelUpdateResponse,
    AdminPrincipalSummary,
    AdminRuntimeWorkspaceResourceSummary,
)
from app.schemas.admin_kubernetes import (
    AdminPodSummary,
    AdminRuntimeKubernetesSnapshot,
)
from app.schemas.assets import (
    AssetSummary,
    ExpertPackageSummary,
    ExpertPackageVersionSummary,
    InstallExpertResponse,
    PlazaTemplateSummary,
)
from app.schemas.runtime_chat import (
    ChatFileSummary,
    RegisterChatFileRequest,
    RenameChatFileRequest,
    RuntimeChatRequest,
    RuntimeChatRunSummary,
    SessionModelSelectionRequest,
    SessionModelSelectionResponse,
    RuntimeSessionSummary,
)


FORBIDDEN_SCHEMA_TERMS = (
    "BusinessWorkspace",
    "copaw",
    "copaw_",
    "hrm_user_id",
    "sso_subject",
    "runtimeToken",
    "runtimeApiKey",
    "providerKey",
    "providerApiKey",
    "mcpSecret",
    "hostPath",
    "api_key_encrypted",
    "provider_base_url",
    "upstream_base_url",
    "pod_ip",
    "podIp",
    "runtime_api_key",
    "runtimeApiKey",
)

FORBIDDEN_PUBLIC_FIELD_NAMES = {
    "BusinessWorkspace",
    "business_workspace_id",
    "workspace_id",
    "copaw",
    "copaw_agent_id",
    "copaw_run_id",
    "copaw_session_id",
    "runtime_id",
    "runtime_token",
    "runtime_api_key",
    "api_key",
    "api_key_encrypted",
    "provider_key",
    "provider_api_key",
    "provider_base_url",
    "upstream_base_url",
    "mcp_secret",
    "host_path",
    "kubeconfig",
}

PUBLIC_PYDANTIC_MODELS = (
    RuntimeWorkspaceSummary,
    RuntimeRunSummary,
    RuntimeModelSummary,
    DefaultModelUpdateResponse,
    RuntimeSessionSummary,
    SessionModelSelectionRequest,
    SessionModelSelectionResponse,
    RuntimeChatRequest,
    RuntimeChatRunSummary,
    ChatFileSummary,
    RegisterChatFileRequest,
    RenameChatFileRequest,
    AssetSummary,
    ExpertPackageSummary,
    ExpertPackageVersionSummary,
    PlazaTemplateSummary,
    InstallExpertResponse,
    AdminPrincipalSummary,
    AdminAuditLogSummary,
    AdminModelSummary,
    AdminModelConfigResponse,
    AdminModelStatusRequest,
    AdminModelCatalogPublishRequest,
    AdminModelCatalogPublishResponse,
    AdminModelUpdateResponse,
    AdminRuntimeWorkspaceResourceSummary,
    LoginRequest,
    AuthPrincipalSummary,
    AuthTokenResponse,
)


def test_runtime_workspace_pydantic_schema_uses_platform_owner() -> None:
    fields = set(RuntimeWorkspaceSummary.model_fields)

    assert "user_id" in fields
    assert "runtime_workspace_id" in fields
    assert "workspace_id" not in fields
    assert "hrm_user_id" not in fields
    assert "sso_subject" not in fields


def test_runtime_run_schema_uses_runtime_run_id_not_copaw() -> None:
    fields = set(RuntimeRunSummary.model_fields)

    assert "runtime_run_id" in fields
    assert "model_entry_id" in fields
    assert "copaw_run_id" not in fields


def test_runtime_model_schema_excludes_provider_secret_fields() -> None:
    fields = set(RuntimeModelSummary.model_fields)
    update_fields = set(DefaultModelUpdateResponse.model_fields)

    assert "model_entry_id" in fields
    assert "provider_display_name" in fields
    assert "api_key" not in fields
    assert "api_key_encrypted" not in fields
    assert "provider_base_url" not in fields
    assert "projection_scope" in update_fields
    assert "pending_reload" in update_fields


def test_session_model_selection_schema_has_only_session_scope_fields() -> None:
    request_fields = set(SessionModelSelectionRequest.model_fields)
    response_fields = set(SessionModelSelectionResponse.model_fields)

    assert request_fields == {"model_entry_id"}
    assert response_fields == {
        "runtime_workspace_id",
        "runtime_session_id",
        "model_entry_id",
        "applies_to",
        "active_run_count",
    }


def test_admin_runtime_resource_schema_exposes_employee_no_without_identity_secrets() -> None:
    fields = set(AdminRuntimeWorkspaceResourceSummary.model_fields)

    assert "employee_no" in fields
    assert "resource_key" in fields
    assert "runtime_kind" in fields
    assert "application_agent_id" in fields
    assert "sso_subject" not in fields
    assert "api_key" not in fields
    assert "api_key_encrypted" not in fields


def test_admin_pod_summary_schema_exposes_resource_metrics() -> None:
    fields = set(AdminPodSummary.model_fields)

    assert "cpu_usage_millicores" in fields
    assert "memory_usage_bytes" in fields
    assert "resource_metrics_collected_at" in fields


def test_admin_runtime_kubernetes_snapshot_has_no_gateway_contract() -> None:
    fields = set(AdminRuntimeKubernetesSnapshot.model_fields)

    assert "runtime_service" in fields
    assert "resource_key" in fields
    assert "runtime_kind" in fields
    assert "pods" in fields
    assert "gateway_service" not in fields


def test_runtime_chat_and_session_schemas_use_runtime_ids() -> None:
    session_fields = set(RuntimeSessionSummary.model_fields)
    chat_fields = set(RuntimeChatRequest.model_fields)
    run_fields = set(RuntimeChatRunSummary.model_fields)
    file_fields = set(ChatFileSummary.model_fields)
    register_file_fields = set(RegisterChatFileRequest.model_fields)
    rename_file_fields = set(RenameChatFileRequest.model_fields)

    assert "runtime_session_id" in session_fields
    assert "runtime_workspace_id" in chat_fields
    assert "runtime_run_id" in run_fields
    assert "relative_path" in file_fields
    assert "relative_path" in register_file_fields
    assert "relative_path" in rename_file_fields
    for fields in (session_fields, chat_fields, run_fields, file_fields, register_file_fields, rename_file_fields):
        assert "workspace_id" not in fields
        assert "copaw_session_id" not in fields
        assert "api_key" not in fields


def test_asset_and_plaza_schemas_exclude_runtime_and_secret_fields() -> None:
    schema_fields = [
        set(AssetSummary.model_fields),
        set(ExpertPackageSummary.model_fields),
        set(ExpertPackageVersionSummary.model_fields),
        set(PlazaTemplateSummary.model_fields),
        set(InstallExpertResponse.model_fields),
    ]

    assert "runtime_workspace_id" in InstallExpertResponse.model_fields
    assert "projection_scope" in InstallExpertResponse.model_fields
    for fields in schema_fields:
        assert "workspace_id" not in fields
        assert "copaw_agent_id" not in fields
        assert "runtime_id" not in fields
        assert "api_key" not in fields
        assert "api_key_encrypted" not in fields
        assert "provider_base_url" not in fields
        assert "host_path" not in fields


def test_public_pydantic_schemas_do_not_expose_legacy_or_secret_field_names() -> None:
    offenders: list[str] = []
    for model in PUBLIC_PYDANTIC_MODELS:
        field_names = set(model.model_fields)
        forbidden = FORBIDDEN_PUBLIC_FIELD_NAMES.intersection(field_names)
        offenders.extend(f"{model.__name__}.{field_name}" for field_name in sorted(forbidden))

    assert offenders == []


def test_shared_typescript_schema_excludes_legacy_terms() -> None:
    schema_root = Path(__file__).resolve().parents[3] / "packages" / "schemas" / "src"
    payload = "\n".join(path.read_text() for path in schema_root.rglob("*.ts"))

    for term in FORBIDDEN_SCHEMA_TERMS:
        assert term not in payload
