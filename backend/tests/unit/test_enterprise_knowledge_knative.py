from app.hermes_runtime.providers.knative import KnativeRuntimeProvider, KnativeRuntimeSpec


def spec() -> KnativeRuntimeSpec:
    return KnativeRuntimeSpec(
        org_id=1,
        user_id=2,
        user_name="测试用户",
        user_code="employee-2",
        runtime_workspace_id=101,
        runtime_id="rt-101",
        namespace="hermes-runtimes",
        service_name="hermes-agent-101",
        image="hermes-enterprise:test",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="runtime-key",
        runtime_root_pvc_name="runtime-root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="runtime-pv",
        runtime_root_host_path="/data/runtimes/1/101",
        runtime_node_name="node-1",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        runtime_binding_id="101",
        platform_control_secret_name="hermes-agent-101-platform-control",
        platform_control_key_version=1,
        runtime_root_pvc_uid="pvc-uid-101",
        pvc_storage_identity_hash="sha256:" + ("a" * 64),
        platform_api_base_url="http://platform:8001/api/v1",
        enterprise_knowledge_host_path="/data/hermes/enterprise-knowledge",
    )


def test_unified_kservice_receives_runtime_knowledge_env_and_read_only_mount() -> None:
    manifest = KnativeRuntimeProvider().build_kservice_manifest(spec())
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["HERMES_PLATFORM_API_BASE_URL"]["value"] == "http://platform:8001/api/v1"
    assert env["HERMES_RUNTIME_WORKSPACE_ID"]["value"] == "101"
    assert env["API_SERVER_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "runtime-key"
    mount = next(item for item in container["volumeMounts"] if item["name"] == "enterprise-knowledge")
    assert mount == {
        "name": "enterprise-knowledge",
        "mountPath": "/knowledge/enterprise",
        "readOnly": True,
    }
    volume = next(item for item in pod["volumes"] if item["name"] == "enterprise-knowledge")
    assert volume == {
        "name": "enterprise-knowledge",
        "persistentVolumeClaim": {"claimName": "hermes-enterprise-knowledge", "readOnly": True},
    }
    assert "hostPath" not in volume


def test_shared_local_volume_binds_filtered_bundle_to_fixed_claim() -> None:
    manifests = KnativeRuntimeProvider().build_enterprise_knowledge_volume_manifests(spec())

    assert [manifest["kind"] for manifest in manifests] == ["PersistentVolume", "PersistentVolumeClaim"]
    pv, pvc = manifests
    assert pv["metadata"]["name"] == "hermes-enterprise-knowledge-pv"
    assert pv["spec"]["local"] == {"path": "/data/hermes/enterprise-knowledge"}
    assert pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"] == [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-1"]}
    ]
    assert pvc["metadata"] == {
        "name": "hermes-enterprise-knowledge",
        "namespace": "hermes-runtimes",
    }
    assert pvc["spec"]["volumeName"] == "hermes-enterprise-knowledge-pv"
