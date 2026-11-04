from app.models import CloudHermesProfile, RuntimeRun
from app.repositories.runtime import RuntimeRunRepository, RuntimeWorkspaceRepository
from app.services.model_permissions import PermissionGrant, model_permission_allows


def test_default_profile_query_uses_platform_owner() -> None:
    statement = RuntimeWorkspaceRepository.default_profile_statement(org_id=7, user_id=9)
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "cloud_hermes_profile.org_id = 7" in sql
    assert "cloud_hermes_profile.user_id = 9" in sql
    assert "default_chat_agent" in sql
    assert CloudHermesProfile.__table__.c.user_id.name == "user_id"


def test_create_default_profile_statement_uses_platform_owner_and_profile_type() -> None:
    statement = RuntimeWorkspaceRepository.create_default_profile_statement(
        org_id=7,
        user_id=9,
    )
    compiled = statement.compile()

    assert statement.table.name == "cloud_hermes_profile"
    assert compiled.params["org_id"] == 7
    assert compiled.params["user_id"] == 9
    assert compiled.params["profile_type"] == "default_chat_agent"
    assert compiled.params["name"] == "默认数字人"
    assert compiled.params["status"] == "active"
    assert "workspace_id" not in compiled.params
    assert "hrm_user_id" not in compiled.params


def test_binding_query_uses_runtime_workspace_id() -> None:
    statement = RuntimeWorkspaceRepository.binding_by_workspace_statement(runtime_workspace_id=123)
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "hermes_runtime_binding.runtime_workspace_id = 123" in sql
    assert "workspace_id" not in RuntimeRun.__table__.c


def test_create_runtime_binding_statement_uses_knative_endpoint_and_no_legacy_owner() -> None:
    statement = RuntimeWorkspaceRepository.create_runtime_binding_statement(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        runtime_id="rt-123",
        service_name="runtime-employee-7",
        namespace="hermes-runtimes",
        image="hermes-agent:cloud",
        api_key_encrypted="encrypted-runtime-key",
        runtime_root_pvc_name="runtime-employee-7-root",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-7",
        hermes_home_sub_path="orgs/7/users/9/profiles/123/home",
        workspace_sub_path="orgs/7/users/9/profiles/123/workspace",
        container_mounts={
            "hermes_home": "/data/hermes/home",
            "workspace": "/data/workspace",
        },
    )
    compiled = statement.compile()

    assert statement.table.name == "hermes_runtime_binding"
    assert compiled.params["org_id"] == 7
    assert compiled.params["user_id"] == 9
    assert compiled.params["runtime_workspace_id"] == 123
    assert compiled.params["provider"] == "knative"
    assert compiled.params["runtime_id"] == "rt-123"
    assert compiled.params["image_digest"] is None
    assert compiled.params["knative_service"] == "runtime-employee-7"
    assert compiled.params["k8s_namespace"] == "hermes-runtimes"
    assert "gateway_base_url" not in compiled.params
    assert compiled.params["runtime_root_pvc"] == "runtime-employee-7-root"
    assert (
        compiled.params["runtime_root_host_path"]
        == "/data/hermes/runtimes/7/employee-7"
    )
    assert compiled.params["runtime_config_source"] == "pvc"
    assert compiled.params["hermes_home_mount_path"] == "/data/hermes/home"
    assert compiled.params["workspace_mount_path"] == "/data/workspace"
    assert compiled.params["api_key_encrypted"] == "encrypted-runtime-key"
    assert compiled.params["status"] == "starting"
    assert compiled.params["pending_reload"] is True
    assert "workspace_id" not in compiled.params
    assert "pod_ip" not in compiled.params


def test_binding_service_query_scopes_normalized_name_to_namespace() -> None:
    statement = RuntimeWorkspaceRepository.binding_by_service_statement(
        namespace="hermes-runtimes",
        service_name="runtime-employee-7",
    )
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "hermes_runtime_binding.k8s_namespace = 'hermes-runtimes'" in sql
    assert "hermes_runtime_binding.knative_service = 'runtime-employee-7'" in sql




def test_update_runtime_binding_status_statement_persists_route_without_secret() -> None:
    statement = RuntimeWorkspaceRepository.update_runtime_binding_status_statement(
        runtime_workspace_id=123,
        values={
            "knative_status_url": "http://hermes-runtime-123.hermes-runtimes.hermes.local",
            "route_host": "hermes-runtime-123.hermes-runtimes.hermes.local",
            "endpoint_base_url": "http://127.0.0.1:30364",
            "endpoint_host_header": "hermes-runtime-123.hermes-runtimes.hermes.local",
            "status": "ready",
            "last_error": None,
        },
    )
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "UPDATE hermes_runtime_binding SET" in sql
    assert "knative_status_url=" in sql
    assert "endpoint_host_header=" in sql
    assert "gateway_base_url" not in sql
    assert "latest_created_revision" not in sql
    assert "latest_ready_revision" not in sql
    assert "traffic_revision" not in sql
    assert "status='ready'" in sql
    assert "hermes_runtime_binding.runtime_workspace_id = 123" in sql
    assert "api_key_encrypted" not in sql


def test_active_run_query_filters_pending_and_running() -> None:
    statement = RuntimeRunRepository.active_run_statement(
        runtime_workspace_id=123,
        runtime_run_id="run-1",
    )
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "runtime_run.runtime_workspace_id = 123" in sql
    assert "runtime_run.runtime_run_id = 'run-1'" in sql
    assert "runtime_run.status IN ('pending', 'running')" in sql


def test_model_permission_allows_unrestricted_and_matching_grants() -> None:
    assert model_permission_allows([], user_id=9)
    assert model_permission_allows([PermissionGrant("all", "*")], user_id=9)
    assert model_permission_allows([PermissionGrant("user", "9")], user_id=9)
    assert model_permission_allows(
        [PermissionGrant("department", "dept-a")],
        user_id=9,
        department_ids={"dept-a"},
    )


def test_model_permission_rejects_non_matching_grants() -> None:
    assert not model_permission_allows([PermissionGrant("user", "10")], user_id=9)
    assert not model_permission_allows(
        [PermissionGrant("department", "dept-b")],
        user_id=9,
        department_ids={"dept-a"},
    )
