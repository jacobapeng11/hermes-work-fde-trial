from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from app.hermes_runtime.constants import (
    HERMES_ENTERPRISE_KNOWLEDGE_MOUNT,
    HERMES_HOME,
    HERMES_RUNTIME_SHARED_GID,
    HERMES_RUNTIME_USER_UID,
    HERMES_WORKSPACE,
    RUNTIME_SUPERVISOR_PORT,
)
from app.network_environment import proxy_free_environment
from app.services.runtime_platform_control import (
    PLATFORM_CONTROL_KEY_FILE_NAME,
    PLATFORM_CONTROL_KEY_FILE_PATH,
    PLATFORM_CONTROL_KEY_VOLUME_NAME,
    RuntimePlatformControlKeyMaterial,
    platform_control_key_document,
)

ENTERPRISE_KNOWLEDGE_PV_NAME = "hermes-enterprise-knowledge-pv"
ENTERPRISE_KNOWLEDGE_PVC_NAME = "hermes-enterprise-knowledge"
ENTERPRISE_KNOWLEDGE_STORAGE_SIZE = "10Mi"


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class CommandRunner(Protocol):
    async def run(self, command: list[str], *, input_text: str | None = None) -> CommandResult: ...


class SubprocessCommandRunner:
    TRANSIENT_KUBECTL_ERROR = re.compile(
        r"TLS handshake timeout|Unable to connect to the server|"
        r"connection refused|i/o timeout|context deadline exceeded|"
        r"unable to return a response in the time allotted",
        re.IGNORECASE,
    )

    async def run(self, command: list[str], *, input_text: str | None = None) -> CommandResult:
        def _run() -> CommandResult:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                env=proxy_free_environment(overrides={"PYTHONUNBUFFERED": "1"}),
            )
            return CommandResult(
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
            )

        result = await asyncio.to_thread(_run)
        # The single-host test cluster's API server can transiently stall
        # under fleet-wide cutover load (observed multi-second handshakes and
        # sporadic TLS timeouts). Read-only retry keeps one slow kubectl call
        # from failing an otherwise healthy binding reconcile.
        for attempt in range(5):
            if result.returncode == 0 or input_text is not None:
                break
            if not self.TRANSIENT_KUBECTL_ERROR.search(result.stderr):
                break
            await asyncio.sleep(3.0 * (attempt + 1))
            result = await asyncio.to_thread(_run)
        return result


class KubectlCommandError(RuntimeError):
    pass


class KubectlKnativeClient:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        kubectl: str = "kubectl",
        kubeconfig: str | None = None,
    ) -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.kubectl = kubectl
        self.kubeconfig = kubeconfig

    async def apply(self, manifest: dict[str, Any]) -> None:
        command = [*self._base_command(), "apply", "-f", "-"]
        input_text = json.dumps(manifest, ensure_ascii=False)
        result = await self.runner.run(command, input_text=input_text)
        self._raise_for_error(command, result)

    async def delete_secret(self, *, namespace: str, name: str) -> None:
        self._require_exact_dns_name(namespace, resource="Secret namespace")
        self._require_exact_dns_name(name, resource="Secret")
        command = [
            *self._base_command(),
            "delete",
            "secret",
            name,
            "-n",
            namespace,
            "--ignore-not-found",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def get_kservice(self, *, namespace: str, name: str) -> dict[str, Any]:
        return await self._get_object(resource="ksvc", namespace=namespace, name=name)

    async def get_secret(self, *, namespace: str, name: str) -> dict[str, Any]:
        return await self._get_object(resource="secret", namespace=namespace, name=name)

    async def get_job(self, *, namespace: str, name: str) -> dict[str, Any]:
        return await self._get_object(resource="job", namespace=namespace, name=name)

    async def get_configmap(self, *, namespace: str, name: str) -> dict[str, Any]:
        return await self._get_object(
            resource="configmap",
            namespace=namespace,
            name=name,
        )

    async def zero_initial_scale_allowed(self) -> bool:
        configmap = await self.get_configmap(
            namespace="knative-serving",
            name="config-autoscaler",
        )
        data = configmap.get("data")
        if not isinstance(data, dict):
            return False
        scale_to_zero = str(data.get("enable-scale-to-zero") or "true").lower()
        return (
            scale_to_zero != "false"
            and str(data.get("allow-zero-initial-scale") or "").lower() == "true"
        )

    async def get_persistent_volume_claim(
        self,
        *,
        namespace: str,
        name: str,
    ) -> dict[str, Any]:
        return await self._get_object(
            resource="pvc",
            namespace=namespace,
            name=name,
        )

    async def get_persistent_volume(self, *, name: str) -> dict[str, Any]:
        self._require_exact_dns_name(name, resource="PV")
        command = [*self._base_command(), "get", "pv", name, "-o", "json"]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise KubectlCommandError("kubectl get pv returned non-object JSON")
        return payload

    async def kservice_exists(self, *, namespace: str, name: str) -> bool:
        return await self._named_resource_exists(
            resource="ksvc",
            namespace=namespace,
            name=name,
            resource_label="KService",
        )

    async def secret_exists(self, *, namespace: str, name: str) -> bool:
        return await self._named_resource_exists(
            resource="secret",
            namespace=namespace,
            name=name,
            resource_label="Secret",
        )

    async def persistent_volume_claim_exists(
        self,
        *,
        namespace: str,
        name: str,
    ) -> bool:
        return await self._named_resource_exists(
            resource="pvc",
            namespace=namespace,
            name=name,
            resource_label="PVC",
        )

    async def persistent_volume_exists(self, *, name: str) -> bool:
        self._require_exact_dns_name(name, resource="PV")
        command = [
            *self._base_command(),
            "get",
            "pv",
            name,
            "--ignore-not-found=true",
            "-o",
            "name",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        return bool(result.stdout.strip())

    async def _named_resource_exists(
        self,
        *,
        resource: str,
        namespace: str,
        name: str,
        resource_label: str,
    ) -> bool:
        self._require_exact_dns_name(namespace, resource="KService namespace")
        self._require_exact_dns_name(name, resource=resource_label)
        command = [
            *self._base_command(),
            "get",
            resource,
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "name",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        return bool(result.stdout.strip())

    async def delete_kservice(
        self,
        *,
        namespace: str,
        name: str,
        wait: bool = True,
    ) -> None:
        self._require_exact_dns_name(
            namespace,
            resource="KService namespace",
        )
        self._require_exact_dns_name(name, resource="KService")
        command = [
            *self._base_command(),
            "delete",
            "ksvc",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
        ]
        if not wait:
            command.append("--wait=false")
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def delete_job(self, *, namespace: str, name: str) -> None:
        self._require_exact_dns_name(namespace, resource="Job namespace")
        self._require_exact_dns_name(name, resource="Job")
        command = [
            *self._base_command(),
            "delete",
            "job",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "--wait=true",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def get_job_logs(self, *, namespace: str, name: str) -> str:
        self._require_exact_dns_name(namespace, resource="Job namespace")
        self._require_exact_dns_name(name, resource="Job")
        command = [
            *self._base_command(),
            "logs",
            f"job/{name}",
            "-n",
            namespace,
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        return result.stdout

    async def delete_pod(
        self,
        *,
        namespace: str,
        name: str,
        grace_period_seconds: int | None = None,
        wait: bool = True,
        force: bool = False,
    ) -> None:
        """Delete one exact Pod, optionally returning before kubelet termination.

        ``force`` is an API-level Kubernetes deletion only; it never performs
        host-side containerd/PID termination.
        """
        self._require_exact_dns_name(namespace, resource="Pod namespace")
        self._require_exact_dns_name(name, resource="Pod")
        if grace_period_seconds is not None and grace_period_seconds < 0:
            raise ValueError("Pod grace period cannot be negative")
        command = [
            *self._base_command(),
            "delete",
            "pod",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            f"--wait={'true' if wait else 'false'}",
        ]
        if grace_period_seconds is not None:
            command.append(f"--grace-period={grace_period_seconds}")
        if force:
            command.append("--force")
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def delete_persistent_volume_claim(
        self,
        *,
        namespace: str,
        name: str,
    ) -> None:
        self._require_exact_dns_name(
            namespace,
            resource="PVC namespace",
        )
        self._require_exact_dns_name(name, resource="PVC")
        command = [
            *self._base_command(),
            "delete",
            "pvc",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def delete_persistent_volume(self, *, name: str) -> None:
        self._require_exact_dns_name(name, resource="PV")
        command = [
            *self._base_command(),
            "delete",
            "pv",
            name,
            "--ignore-not-found=true",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def delete_service(self, *, namespace: str, name: str) -> None:
        dns_name = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
        if not re.fullmatch(dns_name, namespace):
            raise ValueError("Service deletion requires one exact DNS namespace")
        if not re.fullmatch(dns_name, name):
            raise ValueError("Service deletion requires one exact DNS name")
        command = [
            *self._base_command(),
            "delete",
            "service",
            name,
            "-n",
            namespace,
            "--ignore-not-found=true",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)

    async def list_kservice_pods(
        self,
        *,
        namespace: str,
        service_name: str,
    ) -> list[dict[str, Any]]:
        command = [
            *self._base_command(),
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"serving.knative.dev/service={service_name}",
            "-o",
            "json",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        payload = json.loads(result.stdout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise KubectlCommandError("kubectl get pods returned invalid object list JSON")
        return items

    async def list_persistent_volume_claim_pods(
        self,
        *,
        namespace: str,
        pvc_name: str,
    ) -> list[dict[str, Any]]:
        """Return only Pods in ``namespace`` mounting one exact PVC."""
        self._require_exact_dns_name(namespace, resource="PVC namespace")
        self._require_exact_dns_name(pvc_name, resource="PVC")
        command = [*self._base_command(), "get", "pods", "-A", "-o", "json"]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        payload = json.loads(result.stdout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise KubectlCommandError("kubectl get pods returned invalid object list JSON")
        matches: list[dict[str, Any]] = []
        for item in items:
            metadata = item.get("metadata")
            spec = item.get("spec")
            if not isinstance(metadata, dict) or metadata.get("namespace") != namespace:
                continue
            volumes = spec.get("volumes") if isinstance(spec, dict) else None
            if not isinstance(volumes, list):
                continue
            for volume in volumes:
                claim = volume.get("persistentVolumeClaim") if isinstance(volume, dict) else None
                if isinstance(claim, dict) and claim.get("claimName") == pvc_name:
                    matches.append(item)
                    break
        return matches

    async def list_runtime_kservices(self, *, namespace: str) -> list[dict[str, Any]]:
        """List Backend-projected Runtime KServices for closed-fleet auditing."""
        self._require_exact_dns_name(namespace, resource="KService namespace")
        command = [
            *self._base_command(),
            "get",
            "ksvc",
            "-n",
            namespace,
            "-l",
            "hermes.platform/runtime-id",
            "-o",
            "json",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        payload = json.loads(result.stdout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise KubectlCommandError("kubectl get ksvc returned invalid object list JSON")
        return items

    async def list_runtime_pods(self, *, namespace: str) -> list[dict[str, Any]]:
        """List Runtime-labelled Pods for fleet closure and orphan detection."""
        self._require_exact_dns_name(namespace, resource="Pod namespace")
        command = [
            *self._base_command(),
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            "hermes.platform/runtime-id",
            "-o",
            "json",
        ]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        payload = json.loads(result.stdout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise KubectlCommandError("kubectl get pods returned invalid Runtime list JSON")
        return items

    async def _get_object(
        self,
        *,
        resource: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any]:
        command = [*self._base_command(), "get", resource, name, "-n", namespace, "-o", "json"]
        result = await self.runner.run(command)
        self._raise_for_error(command, result)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise KubectlCommandError(f"kubectl get {resource} returned non-object JSON")
        return payload

    def _base_command(self) -> list[str]:
        command = [self.kubectl]
        if self.kubeconfig:
            command.extend(["--kubeconfig", self.kubeconfig])
        return command

    @staticmethod
    def _require_exact_dns_name(value: str, *, resource: str) -> None:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value):
            raise ValueError(f"{resource} deletion requires one exact DNS name")

    @staticmethod
    def _raise_for_error(command: list[str], result: CommandResult) -> None:
        if result.returncode == 0:
            return
        safe_command = " ".join(command)
        message = (
            result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        )
        raise KubectlCommandError(f"{safe_command} failed: {message}")


@dataclass(frozen=True)
class KnativeRuntimeSpec:
    org_id: int
    user_id: int
    user_name: str
    user_code: str
    runtime_workspace_id: int
    runtime_id: str
    namespace: str
    service_name: str
    image: str
    service_account: str
    api_server_key_secret_name: str
    runtime_root_pvc_name: str
    hermes_home_sub_path: str
    workspace_sub_path: str
    runtime_root_pv_name: str
    runtime_root_host_path: str
    runtime_node_name: str
    upstream_commit: str
    state_schema: int
    config_schema: int
    startup_mode: Literal["steady_state", "fresh_probe", "fresh_bootstrap"]
    platform_control_secret_name: str = ""
    platform_control_key_version: int = 0
    runtime_root_pvc_uid: str | None = None
    pvc_storage_identity_hash: str | None = None
    runtime_user_uid: int = HERMES_RUNTIME_USER_UID
    runtime_primary_gid: int = HERMES_RUNTIME_USER_UID
    runtime_shared_gid: int = HERMES_RUNTIME_SHARED_GID
    default_model_entry_id: int | None = None
    image_pull_policy: str = "Never"
    pvc_storage_class: str = "local-path"
    pvc_storage_size: str = "5Gi"
    runtime_root_access_mode: Literal[
        "ReadWriteOncePod",
        "ReadWriteOnce",
    ] = "ReadWriteOnce"
    long_request_timeout_seconds: int = 3900
    idle_ttl_seconds: int = 600
    ws_orphan_reap_grace_seconds: int = 900
    platform_api_base_url: str | None = None
    enterprise_knowledge_host_path: str | None = None
    application_agent_id: int | None = None
    min_scale: int = 0
    max_scale: int = 1
    zero_initial_scale: bool = False
    runtime_binding_id: str = ""
    runtime_config_revision: str = "0"
    supervisor_data_sub_path: str = "supervisor"
    backend_internal_base_url: str = "http://backend:8011"
    newapi_base_url: str = "http://newapi:3000/v1"
    newapi_api_key_secret_name: str = "hermes-newapi-runtime"
    application_agent_max_inflight_runs: int = 500
    application_agent_cpu_request: str = "1m"
    application_agent_memory_request: str = "200Mi"
    personal_runtime_cpu_request: str = "1m"
    personal_runtime_memory_request: str = "200Mi"
    application_agent_post_run_warm_grace_seconds: int = 600
    application_agent_hot_session_limit: int = 64
    application_agent_hot_session_ttl_seconds: int = 900
    post_turn_review_activation_window_seconds: int = 30
    post_turn_review_idle_timeout_seconds: int = 15
    post_turn_review_max_duration_seconds: int = 300
    provider_route_acceptance_mode: Literal["compatible", "required"] = "compatible"
    initial_config_download_url: str | None = None
    initial_config_digest: str | None = None
    bootstrap_intent_id: str | None = None
    bootstrap_authorization_digest: str | None = None


class KnativeRuntimeProvider:
    def build_kservice_manifest(self, spec: KnativeRuntimeSpec) -> dict[str, Any]:
        try:
            runtime_binding_id = int(spec.runtime_binding_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("KService Runtime Binding identity is invalid") from exc
        if runtime_binding_id <= 0 or str(runtime_binding_id) != spec.runtime_binding_id:
            raise ValueError("KService Runtime Binding identity is invalid")
        binding_type = (
            "application_runtime" if spec.application_agent_id is not None else "personal_runtime"
        )
        if (
            not spec.platform_control_secret_name
            or spec.platform_control_key_version <= 0
            or not str(spec.runtime_root_pvc_uid or "").strip()
            or not re.fullmatch(
                r"sha256:[a-f0-9]{64}",
                str(spec.pvc_storage_identity_hash or ""),
            )
        ):
            raise ValueError("KService platform control identity is incomplete")
        if spec.startup_mode == "fresh_probe":
            if spec.initial_config_download_url or spec.initial_config_digest:
                raise ValueError("fresh probe cannot receive an initial ConfigBundle")
            if spec.bootstrap_intent_id or spec.bootstrap_authorization_digest:
                raise ValueError("fresh probe cannot receive bootstrap authorization")
        elif spec.startup_mode == "fresh_bootstrap":
            if not spec.bootstrap_intent_id or not re.fullmatch(
                r"sha256:[a-f0-9]{64}",
                str(spec.bootstrap_authorization_digest or ""),
            ):
                raise ValueError("fresh bootstrap authorization is incomplete")
        elif spec.bootstrap_intent_id or spec.bootstrap_authorization_digest:
            raise ValueError("steady-state KService cannot receive bootstrap authorization")
        labels = {
            "hermes.platform/org-id": str(spec.org_id),
            "hermes.platform/owner-user-id": str(spec.user_id),
            "hermes.platform/runtime-workspace-id": str(spec.runtime_workspace_id),
            "hermes.platform/runtime-id": spec.runtime_id,
            "hermes.platform/runtime-binding-type": binding_type,
            "hermes.platform/runtime-binding-id": str(runtime_binding_id),
        }
        if spec.application_agent_id is not None:
            labels["hermes.platform/component"] = "application-agent"
            labels["hermes.platform/application-agent-id"] = str(spec.application_agent_id)
        api_key_ref = {
            "secretKeyRef": {
                "name": spec.api_server_key_secret_name,
                "key": "API_SERVER_KEY",
            }
        }
        env = [
            {"name": "HERMES_STARTUP_MODE", "value": spec.startup_mode},
            {"name": "API_SERVER_KEY", "valueFrom": api_key_ref},
            {
                "name": "NEWAPI_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": spec.newapi_api_key_secret_name,
                        "key": "NEWAPI_API_KEY",
                    }
                },
            },
            {"name": "NEWAPI_BASE_URL", "value": spec.newapi_base_url.rstrip("/")},
            {"name": "RUNTIME_ID", "value": spec.runtime_id},
            {
                "name": "RUNTIME_PVC_STORAGE_IDENTITY_HASH",
                "value": str(spec.pvc_storage_identity_hash),
            },
            {
                "name": "HERMES_SUPERVISOR_POD_UID",
                "valueFrom": {
                    "fieldRef": {
                        "apiVersion": "v1",
                        "fieldPath": "metadata.uid",
                    }
                },
            },
            {
                "name": "HERMES_KNATIVE_REVISION_NAME",
                "valueFrom": {
                    "fieldRef": {
                        "apiVersion": "v1",
                        "fieldPath": ("metadata.labels['serving.knative.dev/revision']"),
                    }
                },
            },
            {
                "name": "RUNTIME_BINDING_ID",
                "value": spec.runtime_binding_id or spec.runtime_id,
            },
            {
                "name": "RUNTIME_CONFIG_REVISION",
                "value": spec.runtime_config_revision,
            },
            {
                "name": "RUNTIME_MODE",
                "value": ("application" if spec.application_agent_id is not None else "personal"),
            },
            {
                "name": "BACKEND_INTERNAL_BASE_URL",
                "value": spec.backend_internal_base_url.rstrip("/"),
            },
            {"name": "RUNTIME_DATA_ROOT", "value": "/data/platform"},
            {"name": "HERMES_HOME", "value": HERMES_HOME},
            {"name": "HERMES_MANAGED", "value": "platform"},
            {"name": "WORKSPACE_DIR", "value": HERMES_WORKSPACE},
            {"name": "TERMINAL_CWD", "value": HERMES_WORKSPACE},
            {"name": "HERMES_WRITE_SAFE_ROOT", "value": HERMES_WORKSPACE},
            {
                "name": "HERMES_KANBAN_WORKSPACES_ROOT",
                "value": f"{HERMES_WORKSPACE}/.kanban-workspaces",
            },
            {"name": "HERMES_DASHBOARD_FILES_ROOT", "value": HERMES_WORKSPACE},
            {"name": "HERMES_SKIP_CONFIG_MIGRATION", "value": "1"},
            {
                "name": "HERMES_TUI_WS_ORPHAN_REAP_GRACE_S",
                "value": str(spec.ws_orphan_reap_grace_seconds),
            },
            {"name": "HERMES_RUNTIME_WORKSPACE_ID", "value": str(spec.runtime_workspace_id)},
            {"name": "ENTERPRISE_KNOWLEDGE_ROOT", "value": HERMES_ENTERPRISE_KNOWLEDGE_MOUNT},
            {
                "name": "HERMES_KANBAN_ORCHESTRATOR_ENABLED",
                "value": ("0" if spec.application_agent_id is not None else "1"),
            },
            {
                "name": "APPLICATION_AGENT_MAX_INFLIGHT_RUNS",
                "value": str(spec.application_agent_max_inflight_runs),
            },
            {
                "name": "APPLICATION_AGENT_POST_RUN_WARM_GRACE_SECONDS",
                "value": str(spec.application_agent_post_run_warm_grace_seconds),
            },
            {
                "name": "APPLICATION_AGENT_HOT_SESSION_LIMIT",
                "value": str(spec.application_agent_hot_session_limit),
            },
            {
                "name": "APPLICATION_AGENT_HOT_SESSION_TTL_SECONDS",
                "value": str(spec.application_agent_hot_session_ttl_seconds),
            },
            {
                "name": "POST_TURN_REVIEW_ACTIVATION_WINDOW_SECONDS",
                "value": str(spec.post_turn_review_activation_window_seconds),
            },
            {
                "name": "POST_TURN_REVIEW_IDLE_TIMEOUT_SECONDS",
                "value": str(spec.post_turn_review_idle_timeout_seconds),
            },
            {
                "name": "POST_TURN_REVIEW_MAX_DURATION_SECONDS",
                "value": str(spec.post_turn_review_max_duration_seconds),
            },
            {
                "name": "PROVIDER_ROUTE_ACCEPTANCE_MODE",
                "value": spec.provider_route_acceptance_mode,
            },
        ]
        if spec.bootstrap_intent_id and spec.bootstrap_authorization_digest:
            env.extend(
                [
                    {
                        "name": "RUNTIME_BOOTSTRAP_INTENT_ID",
                        "value": spec.bootstrap_intent_id,
                    },
                    {
                        "name": "RUNTIME_BOOTSTRAP_AUTHORIZATION_DIGEST",
                        "value": spec.bootstrap_authorization_digest,
                    },
                ]
            )
        if spec.initial_config_download_url and spec.initial_config_digest:
            env.extend(
                [
                    {
                        "name": "RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL",
                        "value": spec.initial_config_download_url,
                    },
                    {
                        "name": "RUNTIME_INITIAL_CONFIG_DIGEST",
                        "value": spec.initial_config_digest,
                    },
                ]
            )
        if spec.application_agent_id is not None:
            env.append(
                {
                    "name": "HERMES_APPLICATION_AGENT_ID",
                    "value": str(spec.application_agent_id),
                }
            )
        volume_mounts = [
            {
                "name": "runtime-root",
                "mountPath": HERMES_HOME,
                "subPath": spec.hermes_home_sub_path,
            },
            {
                "name": "runtime-root",
                "mountPath": HERMES_WORKSPACE,
                "subPath": spec.workspace_sub_path,
            },
            {
                "name": "runtime-root",
                "mountPath": "/data/platform",
                "subPath": spec.supervisor_data_sub_path,
            },
            {
                "name": PLATFORM_CONTROL_KEY_VOLUME_NAME,
                "mountPath": PLATFORM_CONTROL_KEY_FILE_PATH,
                "subPath": PLATFORM_CONTROL_KEY_FILE_NAME,
                "readOnly": True,
            },
        ]
        volumes = [
            {
                "name": "runtime-root",
                "persistentVolumeClaim": {"claimName": spec.runtime_root_pvc_name},
            },
            {
                "name": PLATFORM_CONTROL_KEY_VOLUME_NAME,
                "secret": {
                    "secretName": spec.platform_control_secret_name,
                    "defaultMode": 0o400,
                    "items": [
                        {
                            "key": PLATFORM_CONTROL_KEY_FILE_NAME,
                            "path": PLATFORM_CONTROL_KEY_FILE_NAME,
                            "mode": 0o400,
                        }
                    ],
                },
            },
        ]
        volumes.append(
            {
                "name": "runtime-tmp",
                "emptyDir": {
                    "sizeLimit": ("2Gi" if spec.application_agent_id is not None else "512Mi")
                },
            }
        )
        volume_mounts.append(
            {
                "name": "runtime-tmp",
                "mountPath": "/tmp",
            }
        )
        if spec.platform_api_base_url:
            env.append(
                {
                    "name": "HERMES_PLATFORM_API_BASE_URL",
                    "value": spec.platform_api_base_url.rstrip("/"),
                }
            )
        if spec.enterprise_knowledge_host_path:
            volumes.append(
                {
                    "name": "enterprise-knowledge",
                    "persistentVolumeClaim": {
                        "claimName": ENTERPRISE_KNOWLEDGE_PVC_NAME,
                        "readOnly": True,
                    },
                }
            )
            volume_mounts.append(
                {
                    "name": "enterprise-knowledge",
                    "mountPath": HERMES_ENTERPRISE_KNOWLEDGE_MOUNT,
                    "readOnly": True,
                }
            )
        return {
            "apiVersion": "serving.knative.dev/v1",
            "kind": "Service",
            "metadata": {
                "name": spec.service_name,
                "namespace": spec.namespace,
                "labels": labels,
            },
            "spec": {
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {
                            "autoscaling.knative.dev/min-scale": str(spec.min_scale),
                            **(
                                {"autoscaling.knative.dev/initial-scale": "0"}
                                if spec.zero_initial_scale
                                else {}
                            ),
                            "autoscaling.knative.dev/max-scale": str(spec.max_scale),
                            "autoscaling.knative.dev/scale-down-delay": f"{spec.idle_ttl_seconds}s",
                            "hermes.platform/control-key-version": str(
                                spec.platform_control_key_version
                            ),
                            "hermes.platform/pvc-storage-identity-hash": str(
                                spec.pvc_storage_identity_hash
                            ),
                        },
                    },
                    "spec": {
                        "serviceAccountName": spec.service_account,
                        "automountServiceAccountToken": False,
                        "securityContext": {
                            "supplementalGroups": [spec.runtime_shared_gid],
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "containerConcurrency": 0,
                        "timeoutSeconds": spec.long_request_timeout_seconds,
                        "responseStartTimeoutSeconds": spec.long_request_timeout_seconds,
                        "containers": [
                            {
                                "image": spec.image,
                                "imagePullPolicy": spec.image_pull_policy,
                                "command": [
                                    "/opt/hermes/.venv/bin/platform-supervisor-privileged-entry"
                                ],
                                "args": [
                                    "--",
                                    "/opt/hermes/.venv/bin/platform-supervisor",
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {
                                        "drop": ["ALL"],
                                        # Supervisor must stop/reap the fixed
                                        # UID 10001 Hermes child.  The child
                                        # exec wrapper clears every capability
                                        # before exec, so KILL remains confined
                                        # to the UID 10000 Supervisor.
                                        "add": ["KILL", "SETGID", "SETUID"],
                                    },
                                    "runAsNonRoot": False,
                                    "runAsUser": 0,
                                    "runAsGroup": 0,
                                    "seccompProfile": {"type": "RuntimeDefault"},
                                },
                                "ports": [
                                    {
                                        "name": "http1",
                                        "containerPort": RUNTIME_SUPERVISOR_PORT,
                                    }
                                ],
                                "resources": (
                                    {
                                        "requests": {
                                            "cpu": (
                                                spec.application_agent_cpu_request
                                                if spec.application_agent_id is not None
                                                else spec.personal_runtime_cpu_request
                                            ),
                                            **(
                                                {
                                                    "memory": spec.application_agent_memory_request,
                                                    "ephemeral-storage": "2Gi",
                                                }
                                                if spec.application_agent_id is not None
                                                else {"memory": spec.personal_runtime_memory_request}
                                            ),
                                        },
                                        "limits": {
                                            "memory": "6Gi",
                                            **(
                                                {"ephemeral-storage": "4Gi"}
                                                if spec.application_agent_id is not None
                                                else {}
                                            ),
                                        },
                                    }
                                ),
                                "env": env,
                                "volumeMounts": volume_mounts,
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": "/control-readyz",
                                        "port": RUNTIME_SUPERVISOR_PORT,
                                    },
                                    "periodSeconds": 2,
                                    "timeoutSeconds": 1,
                                    "failureThreshold": 60,
                                },
                                "livenessProbe": {
                                    "httpGet": {
                                        "path": "/healthz",
                                        "port": RUNTIME_SUPERVISOR_PORT,
                                    },
                                    "periodSeconds": 10,
                                    "timeoutSeconds": 2,
                                    "failureThreshold": 6,
                                },
                                "startupProbe": {
                                    "httpGet": {
                                        "path": "/livez",
                                        "port": RUNTIME_SUPERVISOR_PORT,
                                    },
                                    "periodSeconds": 2,
                                    "timeoutSeconds": 1,
                                    # Application-agent PVCs can take ~220s
                                    # to cold-start on a full image cutover;
                                    # keep a 300s window so the first Knative
                                    # retry is not required merely to pass the
                                    # probe.
                                    "failureThreshold": 150,
                                },
                            }
                        ],
                        "volumes": volumes,
                    },
                }
            },
        }

    def build_pvc_manifest(
        self,
        *,
        namespace: str,
        name: str,
        storage_class: str = "local-path",
        storage_size: str = "5Gi",
        access_mode: Literal[
            "ReadWriteOncePod",
            "ReadWriteOnce",
        ] = "ReadWriteOnce",
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "accessModes": [access_mode],
                "storageClassName": storage_class,
                "resources": {"requests": {"storage": storage_size}},
            },
        }

    def build_local_pv_manifest(
        self,
        *,
        name: str,
        host_path: str,
        node_name: str,
        storage_size: str = "5Gi",
        access_mode: Literal[
            "ReadWriteOncePod",
            "ReadWriteOnce",
        ] = "ReadWriteOnce",
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {"name": name},
            "spec": {
                "capacity": {"storage": storage_size},
                "accessModes": [access_mode],
                "persistentVolumeReclaimPolicy": "Retain",
                "storageClassName": "",
                "volumeMode": "Filesystem",
                "local": {"path": host_path},
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": [node_name],
                                    }
                                ]
                            }
                        ]
                    }
                },
            },
        }

    def build_static_pvc_manifest(
        self,
        *,
        namespace: str,
        name: str,
        volume_name: str,
        storage_size: str = "5Gi",
        access_mode: Literal[
            "ReadWriteOncePod",
            "ReadWriteOnce",
        ] = "ReadWriteOnce",
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {
                "accessModes": [access_mode],
                "storageClassName": "",
                "volumeName": volume_name,
                "resources": {"requests": {"storage": storage_size}},
            },
        }

    def build_enterprise_knowledge_volume_manifests(
        self,
        spec: KnativeRuntimeSpec,
    ) -> tuple[dict[str, Any], ...]:
        if not spec.enterprise_knowledge_host_path:
            return ()
        return (
            self.build_local_pv_manifest(
                name=ENTERPRISE_KNOWLEDGE_PV_NAME,
                host_path=spec.enterprise_knowledge_host_path,
                node_name=spec.runtime_node_name,
                storage_size=ENTERPRISE_KNOWLEDGE_STORAGE_SIZE,
            ),
            self.build_static_pvc_manifest(
                namespace=spec.namespace,
                name=ENTERPRISE_KNOWLEDGE_PVC_NAME,
                volume_name=ENTERPRISE_KNOWLEDGE_PV_NAME,
                storage_size=ENTERPRISE_KNOWLEDGE_STORAGE_SIZE,
            ),
        )

    def build_secret_manifest(
        self,
        *,
        namespace: str,
        name: str,
        api_server_key: str,
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "stringData": {"API_SERVER_KEY": api_server_key},
        }

    def build_platform_control_secret_manifest(
        self,
        *,
        spec: KnativeRuntimeSpec,
        material: RuntimePlatformControlKeyMaterial,
    ) -> dict[str, Any]:
        binding_type = (
            "application_runtime" if spec.application_agent_id is not None else "personal_runtime"
        )
        if (
            material.runtime_id != spec.runtime_id
            or material.binding_id != int(spec.runtime_binding_id)
            or material.binding_type != binding_type
            or material.key_version != spec.platform_control_key_version
            or not spec.runtime_root_pvc_uid
            or not spec.pvc_storage_identity_hash
        ):
            raise ValueError("platform control Secret does not match the Runtime spec")
        document = platform_control_key_document(
            material=material,
            pvc_storage_identity_hash=spec.pvc_storage_identity_hash,
        )
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": spec.platform_control_secret_name,
                "namespace": spec.namespace,
                "labels": {
                    "hermes.platform/runtime-id": spec.runtime_id,
                    "hermes.platform/control-key-version": str(material.key_version),
                },
            },
            "type": "Opaque",
            "stringData": {
                PLATFORM_CONTROL_KEY_FILE_NAME: json.dumps(
                    document,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        }

    def parse_status(
        self,
        kservice: dict[str, Any],
        *,
        endpoint_base_url: str,
    ) -> dict[str, Any]:
        status = kservice.get("status") or {}
        url = status.get("url")
        route_host = urlparse(url).netloc if isinstance(url, str) else None
        conditions = status.get("conditions") or []
        ready = next((item for item in conditions if item.get("type") == "Ready"), {})
        is_ready = ready.get("status") == "True"
        return {
            "knative_status_url": url,
            "route_host": route_host,
            "endpoint_base_url": endpoint_base_url,
            "endpoint_host_header": route_host,
            "latest_created_revision": status.get("latestCreatedRevisionName"),
            "latest_ready_revision": status.get("latestReadyRevisionName"),
            "status": "ready" if is_ready else "failed",
            "last_error": None if is_ready else ready.get("message") or ready.get("reason"),
        }

    def precreated_kservice_matches(
        self,
        kservice: dict[str, Any],
        spec: KnativeRuntimeSpec,
        *,
        require_initial_config: bool = True,
    ) -> bool:
        binding_type = (
            "application_runtime"
            if spec.application_agent_id is not None
            else "personal_runtime"
        )
        expected_labels = {
            "hermes.platform/org-id": str(spec.org_id),
            "hermes.platform/owner-user-id": str(spec.user_id),
            "hermes.platform/runtime-workspace-id": str(spec.runtime_workspace_id),
            "hermes.platform/runtime-id": spec.runtime_id,
            "hermes.platform/runtime-binding-type": binding_type,
            "hermes.platform/runtime-binding-id": str(spec.runtime_binding_id),
        }
        status = kservice.get("status") or {}
        conditions = status.get("conditions") or []
        if not any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in conditions
            if isinstance(item, dict)
        ):
            return False
        if status.get("latestCreatedRevisionName") != status.get("latestReadyRevisionName"):
            return False
        template = (kservice.get("spec") or {}).get("template") or {}
        service_labels = (kservice.get("metadata") or {}).get("labels") or {}
        template_labels = (template.get("metadata") or {}).get("labels") or {}
        if not expected_labels.items() <= service_labels.items():
            return False
        if not expected_labels.items() <= template_labels.items():
            return False
        annotations = (template.get("metadata") or {}).get("annotations") or {}
        if annotations.get("autoscaling.knative.dev/min-scale") != str(spec.min_scale):
            return False
        if annotations.get("autoscaling.knative.dev/initial-scale") != "0":
            return False
        if annotations.get("hermes.platform/control-key-version") != str(
            spec.platform_control_key_version
        ):
            return False
        if annotations.get("hermes.platform/pvc-storage-identity-hash") != str(
            spec.pvc_storage_identity_hash
        ):
            return False
        containers = (template.get("spec") or {}).get("containers") or []
        if (template.get("spec") or {}).get("serviceAccountName") != spec.service_account:
            return False
        if len(containers) != 1 or containers[0].get("image") != spec.image:
            return False
        env = {
            item.get("name"): item.get("value")
            for item in containers[0].get("env") or []
            if isinstance(item, dict)
        }
        expected_env = {
            "HERMES_STARTUP_MODE": spec.startup_mode,
            "RUNTIME_ID": spec.runtime_id,
            "RUNTIME_BINDING_ID": spec.runtime_binding_id,
            "RUNTIME_CONFIG_REVISION": spec.runtime_config_revision,
        }
        if require_initial_config:
            expected_env.update(
                {
                    "RUNTIME_INITIAL_CONFIG_DOWNLOAD_URL": spec.initial_config_download_url,
                    "RUNTIME_INITIAL_CONFIG_DIGEST": spec.initial_config_digest,
                }
            )
        if not all(env.get(name) == value for name, value in expected_env.items()):
            return False
        volumes = {
            item.get("name"): item
            for item in (template.get("spec") or {}).get("volumes") or []
            if isinstance(item, dict)
        }
        runtime_volume = volumes.get("runtime-root") or {}
        runtime_volume_source = runtime_volume.get("persistentVolumeClaim") or {}
        return runtime_volume_source.get("claimName") == spec.runtime_root_pvc_name
