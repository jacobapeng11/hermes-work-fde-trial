#!/usr/bin/env bash
set -euo pipefail

# Read-only verification for the redesigned single-Pod execution plane.
# It activates one exact Knative service and inspects only that service's Pod.

# shellcheck source=scripts/lib/no-proxy-env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/no-proxy-env.sh"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
PYTHON="$BACKEND_DIR/.venv/bin/python"
ENV_FILE="$BACKEND_DIR/.env"
SERVICE=""
OUTPUT=""

fail_gate() {
  printf 'Gate 类型: %s\n阻塞项: %s\n' "$1" "$2" >&2
  exit "${3:-2}"
}

usage() {
  cat <<'EOF'
Usage:
  bash scripts/smoke-runtime-supervisor.sh \
    --service <exact-runtime-kservice> \
    --output /tmp/<evidence>.json

The command is read-only. It activates the selected Knative route, verifies
the Supervisor manifest, health, ports, processes and SQLite durability mode,
then writes redacted evidence under /tmp.
EOF
}

while (($#)); do
  case "$1" in
    --service)
      (($# >= 2)) || fail_gate configuration "--service 缺少参数。"
      SERVICE="$2"
      shift 2
      ;;
    --output)
      (($# >= 2)) || fail_gate configuration "--output 缺少参数。"
      OUTPUT="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail_gate configuration "不支持的参数: $1"
      ;;
  esac
done

[[ "$SERVICE" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] \
  || fail_gate configuration "--service 必须是精确的 DNS 名。"
(( ${#SERVICE} <= 63 )) \
  || fail_gate configuration "--service 超过 63 个字符。"
[[ "$OUTPUT" == /tmp/*.json ]] \
  || fail_gate configuration "--output 必须是 /tmp 下的显式 JSON 路径。"
[[ ! -L "$OUTPUT" ]] \
  || fail_gate configuration "--output 不能是符号链接。"
[[ -x "$PYTHON" ]] \
  || fail_gate environment "backend/.venv/bin/python 不存在。"
[[ -f "$ENV_FILE" ]] \
  || fail_gate environment "backend/.env 不存在。"
command -v kubectl >/dev/null 2>&1 \
  || fail_gate environment "缺少命令: kubectl"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
hermes_clear_proxy_env

export HERMES_SUPERVISOR_SMOKE_NAMESPACE="${HERMES_RUNTIME_NAMESPACE:-hermes-runtimes}"
export HERMES_SUPERVISOR_SMOKE_INGRESS="${HERMES_RUNTIME_INGRESS_BASE_URL:-}"
[[ -n "$HERMES_SUPERVISOR_SMOKE_INGRESS" ]] \
  || fail_gate configuration "缺少 HERMES_RUNTIME_INGRESS_BASE_URL。"

PYTHONPATH="$BACKEND_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON" - "$SERVICE" "$OUTPUT" <<'PY'
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx


service, output_raw = sys.argv[1:]
output = Path(output_raw)
namespace = os.environ["HERMES_SUPERVISOR_SMOKE_NAMESPACE"]
ingress = os.environ["HERMES_SUPERVISOR_SMOKE_INGRESS"].rstrip("/")


def kube_json(
    resource: str,
    name: str | None = None,
    *,
    selector: str | None = None,
) -> dict[str, Any]:
    command = ["kubectl", "-n", namespace, "get", resource]
    if name:
        command.append(name)
    if selector:
        command.extend(["-l", selector])
    command.extend(["-o", "json"])
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Kubernetes read failed for exact {resource}")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Kubernetes returned invalid {resource} JSON")
    return payload


def one_user_container(kservice: dict[str, Any]) -> dict[str, Any]:
    pod_spec = (
        (((kservice.get("spec") or {}).get("template") or {}).get("spec"))
        or {}
    )
    containers = pod_spec.get("containers") or []
    if len(containers) != 1 or not isinstance(containers[0], dict):
        raise RuntimeError("Runtime KService must declare one user container")
    return containers[0]


def assert_manifest(kservice: dict[str, Any]) -> dict[str, Any]:
    template = (kservice.get("spec") or {}).get("template") or {}
    pod_spec = template.get("spec") or {}
    container = one_user_container(kservice)
    if container.get("command") != ["/opt/hermes/.venv/bin/platform-supervisor"]:
        raise RuntimeError("Runtime does not start platform-supervisor")
    if container.get("args") not in (None, []):
        raise RuntimeError("Runtime has unexpected Supervisor arguments")
    ports = {
        int(item.get("containerPort"))
        for item in container.get("ports") or []
        if isinstance(item, dict) and item.get("containerPort") is not None
    }
    if ports != {9120}:
        raise RuntimeError(f"Runtime public ports are not exactly {{9120}}: {ports}")
    readiness = (container.get("readinessProbe") or {}).get("httpGet") or {}
    if readiness.get("path") != "/readyz" or readiness.get("port") != 9120:
        raise RuntimeError("Runtime readiness probe is not Supervisor /readyz")
    liveness = (container.get("livenessProbe") or {}).get("httpGet") or {}
    if liveness.get("path") != "/healthz" or liveness.get("port") != 9120:
        raise RuntimeError("Runtime liveness probe is not Supervisor /healthz")
    security = container.get("securityContext") or {}
    if (
        security.get("allowPrivilegeEscalation") is not False
        or security.get("runAsNonRoot") is not True
        or (security.get("capabilities") or {}).get("drop") != ["ALL"]
        or (security.get("seccompProfile") or {}).get("type") != "RuntimeDefault"
    ):
        raise RuntimeError("Runtime container security boundary is incomplete")
    pod_security = pod_spec.get("securityContext") or {}
    if (
        pod_security.get("runAsNonRoot") is not True
        or (pod_security.get("seccompProfile") or {}).get("type")
        != "RuntimeDefault"
    ):
        raise RuntimeError("Runtime Pod security boundary is incomplete")
    if (
        pod_spec.get("containerConcurrency") != 0
        or pod_spec.get("timeoutSeconds") != 3900
        or pod_spec.get("responseStartTimeoutSeconds") != 3900
    ):
        raise RuntimeError("Runtime concurrency or long-run request contract drifted")
    if "terminationGracePeriodSeconds" in pod_spec:
        raise RuntimeError(
            "Runtime KService contains a field forbidden by Knative RevisionSpec"
        )
    mounts = {
        str(item.get("mountPath")): str(item.get("name"))
        for item in container.get("volumeMounts") or []
        if isinstance(item, dict)
    }
    required_mounts = {
        "/opt/data",
        "/data/workspace",
        "/data/platform",
        "/tmp",
    }
    if not required_mounts.issubset(mounts):
        raise RuntimeError("Runtime persistent or temporary mounts are incomplete")
    env_by_name = {
        str(item.get("name") or ""): item
        for item in container.get("env") or []
        if isinstance(item, dict)
    }
    env_names = set(env_by_name)
    required_env = {
        "API_SERVER_KEY",
        "NEWAPI_API_KEY",
        "NEWAPI_BASE_URL",
        "RUNTIME_ID",
        "RUNTIME_BINDING_ID",
        "RUNTIME_CONFIG_REVISION",
        "RUNTIME_MODE",
        "BACKEND_INTERNAL_BASE_URL",
        "APPLICATION_AGENT_MAX_INFLIGHT_RUNS",
    }
    if not required_env.issubset(env_names):
        raise RuntimeError("Runtime Supervisor environment is incomplete")
    forbidden_prefixes = (
        "MYSQL_",
        "REDIS_",
        "WECOM_",
        "ADMIN_",
    )
    if any(name.startswith(forbidden_prefixes) for name in env_names):
        raise RuntimeError("Runtime Pod contains a forbidden platform credential")
    runtime_mode = str((env_by_name.get("RUNTIME_MODE") or {}).get("value") or "")
    if runtime_mode not in {"personal", "application"}:
        raise RuntimeError("Runtime mode is invalid")
    runtime_claims = [
        str(
            ((item.get("persistentVolumeClaim") or {}).get("claimName"))
            or ""
        )
        for item in pod_spec.get("volumes") or []
        if isinstance(item, dict) and item.get("name") == "runtime-root"
    ]
    if len(runtime_claims) != 1 or not runtime_claims[0]:
        raise RuntimeError("Runtime root PVC identity is invalid")
    labels = (kservice.get("metadata") or {}).get("labels") or {}
    runtime_id = str(labels.get("hermes.platform/runtime-id") or "")
    if not runtime_id:
        raise RuntimeError("Runtime KService is missing its runtime-id label")
    return {
        "runtime_id": runtime_id,
        "container_port": 9120,
        "command": "platform-supervisor",
        "runtime_mode": runtime_mode,
        "runtime_root_pvc": runtime_claims[0],
        "required_mounts": sorted(required_mounts),
        "security": "restricted",
    }


async def wait_for_ready_pod(timeout: float = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        payload = kube_json(
            "pods",
            selector=f"serving.knative.dev/service={service}",
        )
        items = [
            item
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
        ready = []
        for pod in items:
            conditions = (pod.get("status") or {}).get("conditions") or []
            if any(
                item.get("type") == "Ready" and item.get("status") == "True"
                for item in conditions
                if isinstance(item, dict)
            ):
                ready.append(pod)
        if len(ready) == 1:
            return ready[0]
        if len(ready) > 1:
            raise RuntimeError("exact Runtime service has more than one Ready Pod")
        if time.monotonic() >= deadline:
            raise RuntimeError("Runtime Supervisor Pod did not become Ready")
        await asyncio.sleep(1)


def inspect_processes_and_storage(pod_name: str) -> dict[str, Any]:
    probe = r'''
import glob
import json
import os
from pathlib import Path
import socket
import sqlite3

def listening(port):
    sock = socket.socket()
    sock.settimeout(1)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()

commands = []
for path in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        if int(path.split("/")[2]) == os.getpid():
            continue
        commands.append(
            open(path, "rb").read().replace(b"\0", b" ").decode(errors="replace")
        )
    except (OSError, ValueError):
        pass

database = Path("/data/platform/supervisor.db")
if not database.is_file():
    raise RuntimeError("Supervisor SQLite database is missing")
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
try:
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
finally:
    connection.close()

print(json.dumps({
    "ports": {
        "supervisor_9120": listening(9120),
        "dashboard_9119": listening(9119),
        "model_relay_9121": listening(9121),
        "legacy_gateway_8642": listening(8642),
    },
    "processes": {
        "supervisor": sum("platform-supervisor" in command for command in commands),
        "dashboard": sum(
            "/opt/hermes/.venv/bin/hermes" in command
            and "dashboard" in command.split()
            for command in commands
        ),
        "legacy_gateway": sum("gateway run" in command for command in commands),
    },
    "sqlite": {
        "journal_mode": journal_mode,
        "synchronous": synchronous,
        "quick_check": quick_check,
    },
}))
'''
    completed = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            pod_name,
            "-c",
            "user-container",
            "--",
            "/opt/hermes/.venv/bin/python",
            "-c",
            probe,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Runtime process/storage probe failed")
    result = json.loads(completed.stdout)
    if result["ports"] != {
        "supervisor_9120": True,
        "dashboard_9119": True,
        "model_relay_9121": True,
        "legacy_gateway_8642": False,
    }:
        raise RuntimeError(f"Runtime port boundary failed: {result['ports']}")
    if result["processes"] != {
        "supervisor": 1,
        "dashboard": 1,
        "legacy_gateway": 0,
    }:
        raise RuntimeError(f"Runtime process boundary failed: {result['processes']}")
    if result["sqlite"] != {
        "journal_mode": "wal",
        "synchronous": 2,
        "quick_check": "ok",
    }:
        raise RuntimeError(f"Runtime SQLite durability failed: {result['sqlite']}")
    return result


async def main() -> None:
    kservice = kube_json("ksvc", service)
    manifest = assert_manifest(kservice)
    root_pvc = kube_json("pvc", manifest["runtime_root_pvc"])
    access_modes = (root_pvc.get("spec") or {}).get("accessModes") or []
    expected_access_mode = (
        os.environ.get(
            "HERMES_APPLICATION_AGENT_PVC_ACCESS_MODE",
            "ReadWriteOncePod",
        )
        if manifest["runtime_mode"] == "application"
        else "ReadWriteOnce"
    )
    if access_modes != [expected_access_mode]:
        raise RuntimeError(
            "Runtime root PVC access mode drifted: "
            f"actual={access_modes} expected={[expected_access_mode]}"
        )
    manifest["runtime_root_access_mode"] = expected_access_mode
    status = kservice.get("status") or {}
    route_url = str(status.get("url") or "")
    route_host = urlparse(route_url).netloc
    revision = str(status.get("latestReadyRevisionName") or "")
    if not route_host or not revision:
        raise RuntimeError("Runtime KService has no Ready route")
    started = time.perf_counter()
    async with httpx.AsyncClient(
        timeout=180,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = await client.get(
            f"{ingress}/readyz",
            headers={"Host": route_host},
        )
    activation_ms = round((time.perf_counter() - started) * 1000, 3)
    if response.status_code != 200:
        raise RuntimeError(
            f"Runtime Supervisor readiness failed ({response.status_code})"
        )
    health = response.json()
    if health.get("ready") is not True or health.get("state") != "HERMES_READY":
        raise RuntimeError("Runtime Supervisor did not report HERMES_READY")
    pod = await wait_for_ready_pod()
    pod_name = str((pod.get("metadata") or {}).get("name") or "")
    if not pod_name:
        raise RuntimeError("Runtime Pod identity is missing")
    termination_grace = (pod.get("spec") or {}).get(
        "terminationGracePeriodSeconds"
    )
    if termination_grace != 3900:
        raise RuntimeError(
            "Knative did not derive the 3900 second Pod termination grace period"
        )
    manifest["termination_grace_period_seconds"] = termination_grace
    runtime = inspect_processes_and_storage(pod_name)
    evidence = {
        "schema": "hermes-runtime-supervisor-smoke-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "service": service,
        "revision": revision,
        "activation_to_ready_ms": activation_ms,
        "manifest": manifest,
        "health": {
            "ready": True,
            "state": "HERMES_READY",
            "error_code": health.get("error_code"),
        },
        "runtime": runtime,
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


asyncio.run(main())
PY

printf 'runtime Supervisor smoke: ok service=%s output=%s\n' "$SERVICE" "$OUTPUT"
