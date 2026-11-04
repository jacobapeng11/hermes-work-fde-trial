#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

LIB_DIR = Path(__file__).resolve().parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from proxy_environment import proxy_free_environment


COMMAND_TIMEOUT_SECONDS = 5
KNATIVE_DEPLOYMENTS = (
    "activator",
    "autoscaler",
    "controller",
    "webhook",
    "net-kourier-controller",
)
KOURIER_DEPLOYMENTS = ("3scale-kourier-gateway",)
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


def _safe_detail(value: str) -> str:
    first_line = " ".join(value.strip().splitlines()) or "check failed"
    redacted = re.sub(
        r"(?i)\b(token|password|secret|api[_-]?key)=\S+",
        r"\1=<redacted>",
        first_line,
    )
    return redacted[:240]


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=proxy_free_environment(),
        )
    except FileNotFoundError:
        return False, f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return False, "check timed out"
    if result.returncode != 0:
        return False, _safe_detail(result.stderr or result.stdout)
    return True, result.stdout


def _check_swap(node_name: str) -> dict[str, str]:
    local_names = {
        socket.gethostname(),
        socket.getfqdn(),
        socket.gethostname().split(".", 1)[0],
        socket.getfqdn().split(".", 1)[0],
    }
    if node_name not in local_names:
        return {"status": "ready", "detail": "runtime node is remote"}
    try:
        active_swaps = Path("/proc/swaps").read_text().splitlines()[1:]
    except OSError as exc:
        return {"status": "not_ready", "detail": _safe_detail(str(exc))}
    if active_swaps:
        devices = ", ".join(line.split()[0] for line in active_swaps if line.split())
        return {
            "status": "not_ready",
            "detail": f"swap is enabled on {devices}",
        }
    return {"status": "ready", "detail": "swap is disabled"}


def _check_kubernetes_api(kubectl: str) -> tuple[dict[str, str], bool]:
    ok, output = _run(
        [kubectl, "--request-timeout=5s", "get", "--raw=/readyz"]
    )
    if not ok:
        return {"status": "not_ready", "detail": output}, False
    if "ok" not in output.lower():
        return {
            "status": "not_ready",
            "detail": "Kubernetes readyz did not return ok",
        }, False
    return {"status": "ready", "detail": "Kubernetes API is ready"}, True


def _check_node(kubectl: str, node_name: str) -> dict[str, str]:
    if not node_name:
        return {
            "status": "not_ready",
            "detail": "HERMES_RUNTIME_NODE_NAME is missing",
        }
    ok, output = _run(
        [
            kubectl,
            "--request-timeout=5s",
            "get",
            "node",
            node_name,
            "-o",
            "json",
        ]
    )
    if not ok:
        return {"status": "not_ready", "detail": output}
    try:
        payload = json.loads(output)
        ready = next(
            (
                item.get("status")
                for item in payload.get("status", {}).get("conditions", [])
                if item.get("type") == "Ready"
            ),
            None,
        )
    except (json.JSONDecodeError, AttributeError):
        return {"status": "not_ready", "detail": "invalid Node status payload"}
    if ready != "True":
        return {
            "status": "not_ready",
            "detail": f"Node {node_name} Ready={ready or 'missing'}",
        }
    return {"status": "ready", "detail": f"Node {node_name} is Ready"}


def _check_deployments(
    kubectl: str,
    namespace: str,
    deployments: tuple[str, ...],
) -> dict[str, str]:
    args = [
        kubectl,
        "--request-timeout=5s",
        "-n",
        namespace,
        "get",
        *(f"deployment/{name}" for name in deployments),
        "-o",
        "json",
    ]
    ok, output = _run(args)
    if not ok:
        return {"status": "not_ready", "detail": output}
    try:
        payload = json.loads(output)
        items = payload.get("items")
        if items is None and payload.get("kind") == "Deployment":
            items = [payload]
        availability = {
            item["metadata"]["name"]: int(
                item.get("status", {}).get("availableReplicas") or 0
            )
            for item in items or []
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {
            "status": "not_ready",
            "detail": f"invalid Deployment status payload for {namespace}",
        }
    unavailable = [
        name for name in deployments if availability.get(name, 0) < 1
    ]
    if unavailable:
        return {
            "status": "not_ready",
            "detail": f"unavailable Deployments: {', '.join(unavailable)}",
        }
    return {
        "status": "ready",
        "detail": f"{namespace} Deployments are Available",
    }


def _check_ingress(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "status": "not_ready",
            "detail": "HERMES_RUNTIME_INGRESS_BASE_URL is invalid",
        }
    try:
        with _NO_PROXY_OPENER.open(
            Request(url.rstrip("/") + "/", method="GET"),
            timeout=COMMAND_TIMEOUT_SECONDS,
        ) as response:
            code = response.status
    except HTTPError as exc:
        code = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return {"status": "not_ready", "detail": _safe_detail(str(exc))}
    return {
        "status": "ready",
        "detail": f"Runtime ingress responded with HTTP {code}",
    }


def probe() -> dict[str, Any]:
    kubectl = os.environ.get("KUBECTL_BIN", "kubectl")
    node_name = os.environ.get("HERMES_RUNTIME_NODE_NAME", "")
    ingress_url = os.environ.get("HERMES_RUNTIME_INGRESS_BASE_URL", "")
    checks: dict[str, dict[str, str]] = {
        "swap": _check_swap(node_name),
    }
    api_check, api_ready = _check_kubernetes_api(kubectl)
    checks["kubernetes_api"] = api_check
    if api_ready:
        checks["runtime_node"] = _check_node(kubectl, node_name)
        checks["knative_control_plane"] = _check_deployments(
            kubectl,
            "knative-serving",
            KNATIVE_DEPLOYMENTS,
        )
        checks["kourier"] = _check_deployments(
            kubectl,
            "kourier-system",
            KOURIER_DEPLOYMENTS,
        )
    else:
        skipped = {
            "status": "not_ready",
            "detail": "Kubernetes API is unavailable",
        }
        checks["runtime_node"] = dict(skipped)
        checks["knative_control_plane"] = dict(skipped)
        checks["kourier"] = dict(skipped)
    checks["runtime_ingress"] = _check_ingress(ingress_url)
    ready = all(item["status"] == "ready" for item in checks.values())
    return {"status": "ready" if ready else "not_ready", "checks": checks}


def _print_text(payload: dict[str, Any]) -> None:
    print("Runtime infrastructure readiness")
    for name, result in payload["checks"].items():
        print(f"{name}: {result['status']} - {result['detail']}")
    print(f"runtime_infrastructure: {payload['status']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Public Dev Runtime infrastructure readiness."
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
    )
    args = parser.parse_args()
    payload = probe()
    if args.output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        _print_text(payload)
    return 0 if payload["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
