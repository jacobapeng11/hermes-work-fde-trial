from app.models import Base, HermesRuntimeBinding


def test_runtime_binding_table_is_registered() -> None:
    assert "hermes_runtime_binding" in Base.metadata.tables


def test_runtime_binding_uses_cloud_profile_as_workspace() -> None:
    table = HermesRuntimeBinding.__table__

    workspace_targets = {fk.target_fullname for fk in table.c.runtime_workspace_id.foreign_keys}
    assert workspace_targets == {"cloud_hermes_profile.id"}
    assert "workspace_id" not in table.c


def test_runtime_binding_contains_knative_endpoint_and_mount_fields() -> None:
    columns = set(HermesRuntimeBinding.__table__.c.keys())

    assert {
        "k8s_namespace",
        "knative_service",
        "knative_status_url",
        "route_host",
        "endpoint_base_url",
        "endpoint_host_header",
        "runtime_root_host_path",
        "runtime_root_pvc",
        "hermes_home_sub_path",
        "workspace_sub_path",
        "hermes_home_mount_path",
        "workspace_mount_path",
    }.issubset(columns)
    assert "gateway_base_url" not in columns
    assert HermesRuntimeBinding.__table__.c.runtime_config_source.default.arg == "pvc"


def test_runtime_binding_excludes_host_port_worker_pool_fields() -> None:
    columns = set(HermesRuntimeBinding.__table__.c.keys())

    assert "host" not in columns
    assert "port" not in columns
    assert "worker_id" not in columns
    assert "pool_id" not in columns
    assert "container_id" not in columns
    assert "copaw_agent_id" not in columns


def test_runtime_binding_has_generation_status_and_reload_fields() -> None:
    columns = set(HermesRuntimeBinding.__table__.c.keys())

    assert {
        "config_generation",
        "config_hash",
        "status",
        "active_run_count",
        "pending_reload",
        "pending_reload_scopes",
        "api_key_encrypted",
    }.issubset(columns)


def test_runtime_binding_keeps_owner_lookup_index() -> None:
    assert "ix_runtime_binding_owner" in {
        index.name for index in HermesRuntimeBinding.__table__.indexes
    }
