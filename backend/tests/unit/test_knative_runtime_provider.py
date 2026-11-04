import json
from dataclasses import replace

import pytest

from app.hermes_runtime.providers.knative import (
    CommandResult,
    KubectlCommandError,
    KubectlKnativeClient,
    KnativeRuntimeProvider,
    KnativeRuntimeSpec,
)
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyMaterial,
)

PVC_IDENTITY = "sha256:" + ("a" * 64)


def _spec() -> KnativeRuntimeSpec:
    return KnativeRuntimeSpec(
        org_id=1,
        user_id=2,
        user_name="测试用户",
        user_code="employee-2",
        runtime_workspace_id=101,
        runtime_id="org-1-rt-101",
        namespace="hermes-runtimes",
        service_name="hermes-agent-101",
        image="kind.local/hermes-agent:v0.19.1-current",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="hermes-agent-101-api-key",
        runtime_root_pvc_name="hermes-agent-101-root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="hermes-agent-101-root-pv",
        runtime_root_host_path="/data/hermes/runtimes/1/101",
        runtime_node_name="demo",
        upstream_commit="cc4cab2f592e60a197e796506de9168f74baf3ea",
        state_schema=23,
        config_schema=33,
        startup_mode="steady_state",
        platform_api_base_url="http://backend.example/api/v1",
        runtime_binding_id="101",
        platform_control_secret_name="hermes-agent-101-platform-control",
        platform_control_key_version=3,
        runtime_root_pvc_uid="pvc-uid-101",
        pvc_storage_identity_hash=PVC_IDENTITY,
        backend_internal_base_url="http://backend.example",
        newapi_base_url="http://newapi.example/v1",
        newapi_api_key_secret_name="newapi-runtime",
        idle_ttl_seconds=900,
    )


def test_unified_kservice_runs_supervisor_execution_plane() -> None:
    manifest = KnativeRuntimeProvider().build_kservice_manifest(_spec())

    assert manifest["kind"] == "Service"
    assert manifest["metadata"]["namespace"] == "hermes-runtimes"
    labels = manifest["metadata"]["labels"]
    assert labels["hermes.platform/runtime-workspace-id"] == "101"
    assert labels["hermes.platform/runtime-binding-type"] == "personal_runtime"
    assert labels["hermes.platform/runtime-binding-id"] == "101"
    annotations = manifest["spec"]["template"]["metadata"]["annotations"]
    assert annotations["autoscaling.knative.dev/min-scale"] == "0"
    assert annotations["autoscaling.knative.dev/max-scale"] == "1"
    assert annotations["autoscaling.knative.dev/scale-down-delay"] == "900s"
    assert annotations["hermes.platform/control-key-version"] == "3"
    assert annotations["hermes.platform/pvc-storage-identity-hash"] == PVC_IDENTITY
    revision = manifest["spec"]["template"]
    pod = revision["spec"]
    assert pod["serviceAccountName"] == "hermes-runtime-workload"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {
        "supplementalGroups": [1000],
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert pod["containerConcurrency"] == 0
    assert pod["timeoutSeconds"] == 3900
    assert pod["responseStartTimeoutSeconds"] == 3900
    # Knative RevisionSpec does not expose the Kubernetes Pod field.  The
    # controller derives the generated Pod grace period from timeoutSeconds.
    assert "terminationGracePeriodSeconds" not in pod
    container = pod["containers"][0]
    assert container["image"] == _spec().image
    assert container["imagePullPolicy"] == "Never"
    assert container["command"] == ["/opt/hermes/.venv/bin/platform-supervisor-privileged-entry"]
    assert container["args"] == [
        "--",
        "/opt/hermes/.venv/bin/platform-supervisor",
    ]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {
            "drop": ["ALL"],
            "add": ["KILL", "SETGID", "SETUID"],
        },
        "runAsNonRoot": False,
        "runAsUser": 0,
        "runAsGroup": 0,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["ports"] == [{"name": "http1", "containerPort": 9120}]
    assert container["readinessProbe"]["httpGet"] == {
        "path": "/control-readyz",
        "port": 9120,
    }
    assert container["livenessProbe"]["httpGet"] == {
        "path": "/healthz",
        "port": 9120,
    }
    assert container["startupProbe"] == {
        "httpGet": {"path": "/livez", "port": 9120},
        "periodSeconds": 2,
        "timeoutSeconds": 1,
        "failureThreshold": 150,
    }
    env_by_name = {item["name"]: item for item in container["env"]}
    assert {
        "API_SERVER_ENABLED",
        "API_SERVER_HOST",
        "API_SERVER_PORT",
        "HERMES_DASHBOARD",
        "HERMES_DASHBOARD_HOST",
        "HERMES_DASHBOARD_PORT",
    }.isdisjoint(env_by_name)
    assert (
        env_by_name["API_SERVER_KEY"]["valueFrom"]["secretKeyRef"]["name"]
        == "hermes-agent-101-api-key"
    )
    assert env_by_name["RUNTIME_ID"]["value"] == "org-1-rt-101"
    assert env_by_name["RUNTIME_PVC_STORAGE_IDENTITY_HASH"]["value"] == (PVC_IDENTITY)
    assert env_by_name["HERMES_SUPERVISOR_POD_UID"]["valueFrom"] == {
        "fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}
    }
    assert env_by_name["HERMES_KNATIVE_REVISION_NAME"]["valueFrom"] == {
        "fieldRef": {
            "apiVersion": "v1",
            "fieldPath": ("metadata.labels['serving.knative.dev/revision']"),
        }
    }
    assert env_by_name["RUNTIME_BINDING_ID"]["value"] == "101"
    assert env_by_name["RUNTIME_MODE"]["value"] == "personal"
    assert env_by_name["PROVIDER_ROUTE_ACCEPTANCE_MODE"]["value"] == ("compatible")
    assert env_by_name["BACKEND_INTERNAL_BASE_URL"]["value"] == ("http://backend.example")
    assert env_by_name["NEWAPI_BASE_URL"]["value"] == "http://newapi.example/v1"
    assert env_by_name["NEWAPI_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "newapi-runtime",
        "key": "NEWAPI_API_KEY",
    }
    assert env_by_name["HERMES_WRITE_SAFE_ROOT"]["value"] == "/data/workspace"
    assert env_by_name["HERMES_KANBAN_WORKSPACES_ROOT"]["value"] == (
        "/data/workspace/.kanban-workspaces"
    )
    assert env_by_name["HERMES_DASHBOARD_FILES_ROOT"]["value"] == "/data/workspace"
    assert env_by_name["HERMES_HOME"]["value"] == "/opt/data"
    assert env_by_name["HERMES_MANAGED"]["value"] == "platform"
    assert env_by_name["TERMINAL_CWD"]["value"] == "/data/workspace"
    assert env_by_name["WORKSPACE_DIR"]["value"] == "/data/workspace"
    assert env_by_name["HERMES_SKIP_CONFIG_MIGRATION"]["value"] == "1"
    assert env_by_name["HERMES_KANBAN_ORCHESTRATOR_ENABLED"]["value"] == "1"
    assert env_by_name["HERMES_TUI_WS_ORPHAN_REAP_GRACE_S"]["value"] == "900"
    assert env_by_name["HERMES_PLATFORM_API_BASE_URL"]["value"] == ("http://backend.example/api/v1")
    assert env_by_name["HERMES_RUNTIME_WORKSPACE_ID"]["value"] == "101"
    assert "MODEL_PROXY_BASE_URL" not in env_by_name
    assert "OPENAI_API_KEY" not in env_by_name
    assert all("CONTROL" not in name for name in env_by_name)
    assert {mount["mountPath"] for mount in container["volumeMounts"]} == {
        "/opt/data",
        "/data/workspace",
        "/data/platform",
        "/run/secrets/hermes-platform-control/control-keys.json",
        "/tmp",
    }
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert mounts["/opt/data"]["name"] == "runtime-root"
    assert mounts["/opt/data"]["subPath"] == "home"
    assert mounts["/data/workspace"]["name"] == "runtime-root"
    assert mounts["/data/workspace"]["subPath"] == "workspace"
    assert mounts["/data/platform"]["subPath"] == "supervisor"
    assert (
        mounts["/run/secrets/hermes-platform-control/control-keys.json"]["subPath"]
        == "control-keys.json"
    )
    volumes = pod["volumes"]
    assert volumes == [
        {
            "name": "runtime-root",
            "persistentVolumeClaim": {"claimName": "hermes-agent-101-root"},
        },
        {
            "name": "platform-control-key",
            "secret": {
                "secretName": "hermes-agent-101-platform-control",
                "defaultMode": 0o400,
                "items": [
                    {
                        "key": "control-keys.json",
                        "path": "control-keys.json",
                        "mode": 0o400,
                    }
                ],
            },
        },
        {
            "name": "runtime-tmp",
            "emptyDir": {"sizeLimit": "512Mi"},
        },
    ]
    assert "initContainers" not in manifest["spec"]["template"]["spec"]


def test_platform_control_secret_has_closed_file_contract() -> None:
    manifest = KnativeRuntimeProvider().build_platform_control_secret_manifest(
        spec=_spec(),
        material=RuntimePlatformControlKeyMaterial(
            binding_type="personal_runtime",
            binding_id=101,
            runtime_id="org-1-rt-101",
            key_version=3,
            root_key=b"k" * 32,
        ),
    )

    document = json.loads(manifest["stringData"]["control-keys.json"])
    assert document == {
        "contract_version": 1,
        "runtime_id": "org-1-rt-101",
        "binding_type": "personal_runtime",
        "binding_id": 101,
        "pvc_storage_identity_hash": PVC_IDENTITY,
        "current_key_version": 3,
        "keys": {"3": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s"},
    }


def test_fresh_probe_is_write_free_and_has_no_bootstrap_or_config_inputs() -> None:
    manifest = KnativeRuntimeProvider().build_kservice_manifest(
        replace(_spec(), startup_mode="fresh_probe")
    )
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}

    assert env["HERMES_STARTUP_MODE"]["value"] == "fresh_probe"
    assert "RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL" not in env
    assert "RUNTIME_BOOTSTRAP_INTENT_ID" not in env
    assert all(mount["name"] != "runtime-schema-expectation" for mount in container["volumeMounts"])


def test_fresh_bootstrap_requires_and_projects_backend_authorization() -> None:
    spec = replace(
        _spec(),
        startup_mode="fresh_bootstrap",
        initial_config_download_url="http://backend/internal/runtime-config/101",
        initial_config_digest="a" * 64,
        bootstrap_intent_id="bootstrap-intent-101",
        bootstrap_authorization_digest="sha256:" + "b" * 64,
    )
    manifest = KnativeRuntimeProvider().build_kservice_manifest(spec)
    env = {
        item["name"]: item for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["HERMES_STARTUP_MODE"]["value"] == "fresh_bootstrap"
    assert env["RUNTIME_BOOTSTRAP_INTENT_ID"]["value"] == "bootstrap-intent-101"
    assert env["RUNTIME_BOOTSTRAP_AUTHORIZATION_DIGEST"]["value"] == ("sha256:" + "b" * 64)


def test_fresh_bootstrap_rejects_missing_backend_authorization() -> None:
    with pytest.raises(ValueError, match="authorization is incomplete"):
        KnativeRuntimeProvider().build_kservice_manifest(
            replace(_spec(), startup_mode="fresh_bootstrap")
        )


def test_steady_state_rejects_bootstrap_authorization() -> None:
    with pytest.raises(ValueError, match="steady-state"):
        KnativeRuntimeProvider().build_kservice_manifest(
            replace(
                _spec(),
                bootstrap_intent_id="bootstrap-intent-101",
                bootstrap_authorization_digest="sha256:" + "b" * 64,
            )
        )


def test_application_agent_kservice_is_single_pod_and_never_scales_to_zero() -> None:
    manifest = KnativeRuntimeProvider().build_kservice_manifest(
        replace(
            _spec(),
            application_agent_id=31,
            min_scale=1,
            max_scale=1,
        )
    )

    assert manifest["metadata"]["labels"]["hermes.platform/application-agent-id"] == "31"
    annotations = manifest["spec"]["template"]["metadata"]["annotations"]
    assert annotations["autoscaling.knative.dev/min-scale"] == "1"
    assert annotations["autoscaling.knative.dev/max-scale"] == "1"
    env = {
        item["name"]: item for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["HERMES_APPLICATION_AGENT_ID"]["value"] == "31"
    assert env["RUNTIME_MODE"]["value"] == "application"
    assert env["HERMES_KANBAN_ORCHESTRATOR_ENABLED"]["value"] == "0"
    assert env["APPLICATION_AGENT_MAX_INFLIGHT_RUNS"]["value"] == "500"
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["startupProbe"] == {
        "httpGet": {"path": "/livez", "port": 9120},
        "periodSeconds": 2,
        "timeoutSeconds": 1,
        "failureThreshold": 150,
    }
    assert container["resources"] == {
        "requests": {
            "cpu": "1m",
            "memory": "200Mi",
            "ephemeral-storage": "2Gi",
        },
        "limits": {
            "memory": "6Gi",
            "ephemeral-storage": "4Gi",
        },
    }
    assert {"name": "runtime-tmp", "mountPath": "/tmp"} in container["volumeMounts"]
    assert {
        "name": "runtime-tmp",
        "emptyDir": {"sizeLimit": "2Gi"},
    } in pod["volumes"]


def test_application_agent_kservice_accepts_environment_sized_requests() -> None:
    manifest = KnativeRuntimeProvider().build_kservice_manifest(
        replace(
            _spec(),
            application_agent_id=31,
            min_scale=1,
            max_scale=1,
            application_agent_cpu_request="500m",
            application_agent_memory_request="2Gi",
        )
    )

    requests = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
    assert requests["cpu"] == "500m"
    assert requests["memory"] == "2Gi"


def test_personal_runtime_has_small_explicit_cpu_request() -> None:
    manifest = KnativeRuntimeProvider().build_kservice_manifest(_spec())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "1m", "memory": "200Mi"},
        "limits": {"memory": "6Gi"},
    }


def test_pvc_manifest_uses_runtime_scoped_claim() -> None:
    pvc = KnativeRuntimeProvider().build_pvc_manifest(
        namespace="hermes-runtimes",
        name="hermes-agent-101-home",
    )

    assert pvc["kind"] == "PersistentVolumeClaim"
    assert pvc["metadata"] == {"name": "hermes-agent-101-home", "namespace": "hermes-runtimes"}
    assert pvc["spec"]["storageClassName"] == "local-path"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"


def test_static_local_root_volume_binds_runtime_path_to_its_claim() -> None:
    provider = KnativeRuntimeProvider()

    pv = provider.build_local_pv_manifest(
        name="hermes-runtime-101-root-pv",
        host_path="/data/hermes/runtimes/1/101",
        node_name="demo",
        storage_size="5Gi",
    )
    pvc = provider.build_static_pvc_manifest(
        namespace="hermes-runtimes",
        name="hermes-runtime-101-root",
        volume_name="hermes-runtime-101-root-pv",
        storage_size="5Gi",
    )

    assert pv["spec"]["local"] == {"path": "/data/hermes/runtimes/1/101"}
    assert pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"] == [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["demo"]}
    ]
    assert pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert pvc["spec"]["storageClassName"] == ""
    assert pvc["spec"]["volumeName"] == "hermes-runtime-101-root-pv"


def test_application_local_volume_can_require_single_pod_writer() -> None:
    provider = KnativeRuntimeProvider()
    pv = provider.build_local_pv_manifest(
        name="application-runtime-31-root-pv",
        host_path="/data/hermes/application-agents/1/31",
        node_name="demo",
        storage_size="20Gi",
        access_mode="ReadWriteOncePod",
    )
    pvc = provider.build_static_pvc_manifest(
        namespace="hermes-runtimes",
        name="application-runtime-31-root",
        volume_name="application-runtime-31-root-pv",
        storage_size="20Gi",
        access_mode="ReadWriteOncePod",
    )

    assert pv["spec"]["accessModes"] == ["ReadWriteOncePod"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteOncePod"]


def test_secret_manifest_contains_only_runtime_api_key() -> None:
    secret = KnativeRuntimeProvider().build_secret_manifest(
        namespace="hermes-runtimes",
        name="runtime-secret",
        api_server_key="runtime-api-key",
    )

    assert secret["kind"] == "Secret"
    assert secret["stringData"] == {"API_SERVER_KEY": "runtime-api-key"}


def test_parse_ready_status_builds_host_header_endpoint() -> None:
    payload = {
        "status": {
            "url": "http://hermes-agent-101.hermes-runtimes.svc.cluster.local",
            "latestCreatedRevisionName": "rev-a",
            "latestReadyRevisionName": "rev-b",
            "conditions": [{"type": "Ready", "status": "True"}],
        }
    }

    status = KnativeRuntimeProvider().parse_status(
        payload,
        endpoint_base_url="http://127.0.0.1:31080",
    )

    assert status["status"] == "ready"
    assert status["endpoint_base_url"] == "http://127.0.0.1:31080"
    assert status["endpoint_host_header"] == "hermes-agent-101.hermes-runtimes.svc.cluster.local"
    assert status["latest_ready_revision"] == "rev-b"


def test_parse_failed_status_keeps_last_error() -> None:
    payload = {
        "status": {
            "url": "http://hermes-agent-101.hermes-runtimes.svc.cluster.local",
            "conditions": [{"type": "Ready", "status": "False", "message": "image pull failed"}],
        }
    }

    status = KnativeRuntimeProvider().parse_status(
        payload,
        endpoint_base_url="http://127.0.0.1:31080",
    )

    assert status["status"] == "failed"
    assert status["last_error"] == "image pull failed"


class FakeCommandRunner:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = result or CommandResult(stdout="{}", stderr="", returncode=0)
        self.calls: list[dict] = []

    async def run(self, command: list[str], *, input_text: str | None = None) -> CommandResult:
        self.calls.append({"command": command, "input_text": input_text})
        return self.result


async def test_kubectl_client_applies_manifest_via_stdin() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner, kubeconfig="/root/.kube/config")
    manifest = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "runtime-secret"}}

    await client.apply(manifest)

    assert runner.calls[0]["command"] == [
        "kubectl",
        "--kubeconfig",
        "/root/.kube/config",
        "apply",
        "-f",
        "-",
    ]
    assert json.loads(runner.calls[0]["input_text"]) == manifest


async def test_kubectl_client_reads_kservice_json() -> None:
    runner = FakeCommandRunner(
        CommandResult(
            stdout='{"status":{"url":"http://hermes-runtime-123.hermes-runtimes.hermes.local"}}',
            stderr="",
            returncode=0,
        )
    )
    client = KubectlKnativeClient(runner=runner)

    payload = await client.get_kservice(namespace="hermes-runtimes", name="hermes-runtime-123")

    assert payload["status"]["url"] == "http://hermes-runtime-123.hermes-runtimes.hermes.local"
    assert runner.calls[0]["command"] == [
        "kubectl",
        "get",
        "ksvc",
        "hermes-runtime-123",
        "-n",
        "hermes-runtimes",
        "-o",
        "json",
    ]


async def test_kubectl_client_lists_only_runtime_labelled_kservices() -> None:
    runner = FakeCommandRunner(
        CommandResult(stdout='{"items":[{"metadata":{"name":"runtime-10002"}}]}', stderr="", returncode=0)
    )
    client = KubectlKnativeClient(runner=runner)

    items = await client.list_runtime_kservices(namespace="hermes-runtimes")

    assert items[0]["metadata"]["name"] == "runtime-10002"
    assert runner.calls[0]["command"] == [
        "kubectl",
        "get",
        "ksvc",
        "-n",
        "hermes-runtimes",
        "-l",
        "hermes.platform/runtime-id",
        "-o",
        "json",
    ]


async def test_kubectl_client_lists_only_runtime_labelled_pods() -> None:
    runner = FakeCommandRunner(
        CommandResult(stdout='{"items":[{"metadata":{"name":"runtime-pod"}}]}', stderr="", returncode=0)
    )
    client = KubectlKnativeClient(runner=runner)

    items = await client.list_runtime_pods(namespace="hermes-runtimes")

    assert items[0]["metadata"]["name"] == "runtime-pod"
    assert runner.calls[0]["command"] == [
        "kubectl",
        "get",
        "pods",
        "-n",
        "hermes-runtimes",
        "-l",
        "hermes.platform/runtime-id",
        "-o",
        "json",
    ]


async def test_kubectl_client_lists_only_pods_mounting_one_exact_pvc() -> None:
    runner = FakeCommandRunner(
        CommandResult(
            stdout=(
                '{"items":['
                '{"metadata":{"name":"target","namespace":"hermes-runtimes"},'
                '"spec":{"volumes":[{"persistentVolumeClaim":{"claimName":"runtime-1-root"}}]}},'
                '{"metadata":{"name":"other-claim","namespace":"hermes-runtimes"},'
                '"spec":{"volumes":[{"persistentVolumeClaim":{"claimName":"other-root"}}]}},'
                '{"metadata":{"name":"other-namespace","namespace":"other"},'
                '"spec":{"volumes":[{"persistentVolumeClaim":{"claimName":"runtime-1-root"}}]}}'
                ']}'
            ),
            stderr="",
            returncode=0,
        )
    )
    client = KubectlKnativeClient(runner=runner)

    pods = await client.list_persistent_volume_claim_pods(
        namespace="hermes-runtimes",
        pvc_name="runtime-1-root",
    )

    assert [item["metadata"]["name"] for item in pods] == ["target"]
    assert runner.calls[0]["command"] == [
        "kubectl",
        "get",
        "pods",
        "-A",
        "-o",
        "json",
    ]


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("service.serving.knative.dev/runtime-10002\n", True),
        ("", False),
    ],
)
async def test_kubectl_client_checks_kservice_existence(
    stdout: str,
    expected: bool,
) -> None:
    runner = FakeCommandRunner(CommandResult(stdout=stdout, stderr="", returncode=0))
    client = KubectlKnativeClient(runner=runner)

    assert (
        await client.kservice_exists(
            namespace="hermes-runtimes",
            name="runtime-10002",
        )
        is expected
    )
    assert runner.calls[0]["command"] == [
        "kubectl",
        "get",
        "ksvc",
        "runtime-10002",
        "-n",
        "hermes-runtimes",
        "--ignore-not-found=true",
        "-o",
        "name",
    ]


async def test_kubectl_client_deletes_only_exact_gateway_service_name() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_service(
        namespace="hermes-runtimes",
        name="runtime-10002-api",
    )

    assert runner.calls[0]["command"] == [
        "kubectl",
        "delete",
        "service",
        "runtime-10002-api",
        "-n",
        "hermes-runtimes",
        "--ignore-not-found=true",
    ]


@pytest.mark.parametrize(
    ("namespace", "name"),
    [
        ("hermes-runtimes", ""),
        ("hermes-runtimes", "runtime-*"),
        ("hermes-runtimes", "--all"),
        ("*", "runtime-10002-api"),
    ],
)
async def test_kubectl_client_rejects_non_exact_service_identity(
    namespace: str,
    name: str,
) -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    with pytest.raises(ValueError, match="exact DNS"):
        await client.delete_service(namespace=namespace, name=name)

    assert runner.calls == []


async def test_kubectl_client_deletes_only_exact_kservice_name() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_kservice(namespace="hermes-runtimes", name="runtime-10002-stream")

    assert runner.calls[0]["command"] == [
        "kubectl",
        "delete",
        "ksvc",
        "runtime-10002-stream",
        "-n",
        "hermes-runtimes",
        "--ignore-not-found=true",
    ]


async def test_kubectl_client_can_delete_exact_kservice_without_waiting() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_kservice(
        namespace="hermes-runtimes",
        name="runtime-10002-stream",
        wait=False,
    )

    assert runner.calls[0]["command"][-1] == "--wait=false"


async def test_kubectl_client_deletes_only_exact_pod_name_and_waits() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_pod(
        namespace="hermes-runtimes",
        name="runtime-10002-stream-pod",
        grace_period_seconds=1,
    )

    assert runner.calls[0]["command"] == [
        "kubectl",
        "delete",
        "pod",
        "runtime-10002-stream-pod",
        "-n",
        "hermes-runtimes",
        "--ignore-not-found=true",
        "--wait=true",
        "--grace-period=1",
    ]


async def test_kubectl_client_force_deletes_exact_pod_without_waiting() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_pod(
        namespace="hermes-runtimes",
        name="runtime-10002-stream-pod",
        grace_period_seconds=0,
        wait=False,
        force=True,
    )

    assert runner.calls[0]["command"][-3:] == [
        "--wait=false",
        "--grace-period=0",
        "--force",
    ]


@pytest.mark.parametrize("name", ["", "runtime-*", "--all"])
async def test_kubectl_client_rejects_non_exact_kservice_name(name: str) -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    with pytest.raises(ValueError, match="exact DNS name"):
        await client.delete_kservice(namespace="hermes-runtimes", name=name)

    assert runner.calls == []


async def test_kubectl_client_deletes_projection_secret_by_name() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_secret(namespace="hermes-runtimes", name="projection-9-4-token")

    assert runner.calls[0]["command"] == [
        "kubectl",
        "delete",
        "secret",
        "projection-9-4-token",
        "-n",
        "hermes-runtimes",
        "--ignore-not-found",
    ]


async def test_kubectl_client_deletes_exact_pvc_and_pv_names() -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    await client.delete_persistent_volume_claim(
        namespace="hermes-runtimes",
        name="application-agent-31-root",
    )
    await client.delete_persistent_volume(name="application-agent-31-root-pv")

    assert [call["command"] for call in runner.calls] == [
        [
            "kubectl",
            "delete",
            "pvc",
            "application-agent-31-root",
            "-n",
            "hermes-runtimes",
            "--ignore-not-found=true",
        ],
        [
            "kubectl",
            "delete",
            "pv",
            "application-agent-31-root-pv",
            "--ignore-not-found=true",
        ],
    ]


@pytest.mark.parametrize("name", ["", "application-agent-*", "--all"])
async def test_kubectl_client_rejects_non_exact_persistent_volume_names(
    name: str,
) -> None:
    runner = FakeCommandRunner()
    client = KubectlKnativeClient(runner=runner)

    with pytest.raises(ValueError, match="exact DNS name"):
        await client.delete_persistent_volume(name=name)

    assert runner.calls == []


async def test_kubectl_client_raises_without_leaking_stdin_on_failure() -> None:
    runner = FakeCommandRunner(CommandResult(stdout="", stderr="forbidden", returncode=1))
    client = KubectlKnativeClient(runner=runner)

    try:
        await client.apply({"kind": "Secret", "stringData": {"API_SERVER_KEY": "secret-value"}})
    except KubectlCommandError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected KubectlCommandError")

    assert "kubectl apply -f -" in message
    assert "forbidden" in message
    assert "secret-value" not in message
