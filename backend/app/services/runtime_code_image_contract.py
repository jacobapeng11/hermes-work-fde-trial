from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable


_PROBE = r'''
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import sqlite3
import yaml
import hermes_state

def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

distribution = metadata.distribution("hermes-agent")
hermes_digest = hashlib.sha256()
for item in sorted(distribution.files or [], key=str):
    relative = str(item)
    path = Path(item.locate())
    if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
        continue
    hermes_digest.update(relative.encode())
    hermes_digest.update(b"\0")
    hermes_digest.update(file_digest(path).encode())
    hermes_digest.update(b"\0")

manifest_path = Path("/opt/hermes-platform/runtime-overlay/v0.20.1.yaml")
manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
distributions = sorted(
    (str(dist.metadata.get("Name") or "").lower(), dist.version)
    for dist in metadata.distributions()
    if str(dist.metadata.get("Name") or "").lower() != "hermes-platform-supervisor"
)
paths = {}
# `/home/hermes` is a per-Runtime PVC mountpoint supplied only by the
# KService.  A code-only parent image must not require it to exist.
for value in ("/opt/hermes", "/opt/hermes/.venv"):
    path = Path(value)
    stat = path.stat()
    paths[value] = {"uid": stat.st_uid, "gid": stat.st_gid, "mode": stat.st_mode & 0o7777}

payload = {
    "hermes_distribution_version": distribution.version,
    "hermes_distribution_sha256": hermes_digest.hexdigest(),
    "hermes_state_schema": hermes_state.SCHEMA_VERSION,
    "compatibility_manifest_sha256": file_digest(manifest_path),
    "compatibility_identity": {
        key: manifest[key]
        for key in (
            "upstream_version", "upstream_commit", "state_schema", "config_schema"
        )
    },
    "python_distributions": distributions,
    "sqlite_version": sqlite3.sqlite_version,
    "fts5_cjk_sha256": file_digest(Path("/opt/hermes-platform/lib/libfts5_cjk.so")),
    "hermes_uv_lock_sha256": file_digest(Path("/opt/hermes/uv.lock")),
    "storage_paths": paths,
}
print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
'''

_OCI_CONFIG_FIELDS = (
    "User",
    "Entrypoint",
    "Cmd",
    "WorkingDir",
    "Env",
    "Volumes",
    "ExposedPorts",
)


@dataclass(frozen=True)
class RuntimeCodeImageInvariant:
    payload: dict[str, object]
    payload_sha256: str
    oci_config: dict[str, object]


def inspect_runtime_code_invariant(
    image_reference: str,
    *,
    run_checked: Callable[[list[str]], str],
) -> RuntimeCodeImageInvariant:
    inspected = json.loads(run_checked(["docker", "image", "inspect", image_reference]))
    if len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise RuntimeError("Runtime image inspect returned an ambiguous result")
    config = inspected[0].get("Config")
    if not isinstance(config, dict):
        raise RuntimeError("Runtime image OCI Config is unavailable")
    payload = json.loads(
        run_checked(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/opt/hermes/.venv/bin/python",
                image_reference,
                "-c",
                _PROBE,
            ]
        )
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime immutable probe returned a non-object")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return RuntimeCodeImageInvariant(
        payload=payload,
        payload_sha256="sha256:" + hashlib.sha256(canonical).hexdigest(),
        oci_config={field: config.get(field) for field in _OCI_CONFIG_FIELDS},
    )


__all__ = ["RuntimeCodeImageInvariant", "inspect_runtime_code_invariant"]
