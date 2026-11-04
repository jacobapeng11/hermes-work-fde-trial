"""Read-only infrastructure transport and owned-field comparisons.

Not imported by service startup. Never fall back to a full Secret object.
"""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import quote


def safe_metadata(item: dict) -> dict:
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    return {
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "resourceVersion": metadata.get("resourceVersion"),
        "generation": metadata.get("generation"),
        "labels": {key: labels[key] for key in (
            "hermes.platform/control-key-version", "hermes.platform/runtime-id"
        ) if key in labels},
    }


def secret_metadata(namespace: str) -> dict:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl unavailable for metadata query")
    # Private Unix socket: no listening TCP port, no kubeconfig credential copy.
    with tempfile.TemporaryDirectory(prefix="config-status-") as directory:
        path = Path(directory) / "proxy.sock"
        with subprocess.Popen(
            [kubectl, "proxy", f"--unix-socket={path}",
             "--reject-methods=^(POST|PUT|PATCH|DELETE|CONNECT)$",
             f"--accept-paths=^/api/v1/namespaces/{namespace}/secrets$"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as process:
            try:
                deadline = time.monotonic() + 5
                while not path.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("metadata proxy unavailable")
                    time.sleep(0.05)
                entries = []
                cursor = ""
                while True:
                    connection = HTTPConnection("localhost", timeout=10)
                    connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    connection.sock.settimeout(10)
                    try:
                        connection.sock.connect(str(path))
                        connection.request(
                            "GET", f"/api/v1/namespaces/{quote(namespace, safe='')}/secrets"
                            f"?limit=500&continue={quote(cursor, safe='')}",
                            headers={"Accept": "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"},
                        )
                        response = connection.getresponse()
                        if response.status != 200:
                            raise RuntimeError(f"metadata query HTTP {response.status}")
                        payload = json.load(response)
                        if payload.get("kind") != "PartialObjectMetadataList":
                            raise RuntimeError("metadata-only response required; no fallback")
                        entries.extend({"metadata": safe_metadata(item)} for item in payload.get("items", []))
                        cursor = payload.get("metadata", {}).get("continue", "")
                    finally:
                        connection.close()
                    if not cursor:
                        return {"items": entries}
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def owned_field_diff(desired: dict, observed: dict) -> list[str]:
    """Compare declared fields only; unordered RBAC rules/subjects are sets."""
    differences = []

    def compare(want, have, path):
        if isinstance(want, dict):
            if not isinstance(have, dict):
                differences.append(path)
                return
            for key, value in want.items():
                compare(value, have.get(key), f"{path}.{key}" if path else key)
        elif isinstance(want, list):
            def canonical(value):
                if isinstance(value, dict):
                    return {k: canonical(v) for k, v in sorted(value.items())}
                if isinstance(value, list):
                    return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
                return value
            if canonical(want) != canonical(have):
                differences.append(path)
        elif want != have:
            differences.append(path)

    compare(desired, observed, "")
    return differences
