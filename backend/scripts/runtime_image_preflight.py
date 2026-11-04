#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.runtime_image_config import RuntimeImageConfig, RuntimeImageConfigStore  # noqa: E402
from app.services.runtime_code_image_contract import (  # noqa: E402
    inspect_runtime_code_invariant,
)
from app.services.runtime_image_publisher import (  # noqa: E402
    RuntimeImageCommandResult,
    RuntimeImagePublisher,
)


class SubprocessRunner:
    def run(self, command: list[str]) -> RuntimeImageCommandResult:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        return RuntimeImageCommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only validation of the backend/.env Runtime image target."
    )
    parser.add_argument("--env-file", type=Path, default=BACKEND_ROOT / ".env")
    parser.add_argument("--kind-cluster-name", default="hermes")
    args = parser.parse_args()
    target = RuntimeImageConfigStore(args.env_file).read()
    if not target.manifest_digest or not target.config_id:
        raise RuntimeError("backend/.env Runtime image identity is incomplete")

    inspected = json.loads(_checked(["docker", "image", "inspect", target.image]))
    if len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise RuntimeError("local Runtime image inspect returned an ambiguous result")
    image = inspected[0]
    if not str(image.get("Id") or "").startswith("sha256:"):
        raise RuntimeError("local Runtime image identity is unavailable")
    labels = (image.get("Config") or {}).get("Labels") or {}
    if labels.get("hermes.platform.build-strategy") == "complete-runtime-overlay":
        return _preflight_complete_runtime(target, labels, args.kind_cluster_name)
    if (
        labels.get("hermes.platform.code-contract-version")
        != "runtime-code-contract-v1"
        or labels.get("hermes.platform.build-strategy") != "supervisor-code-only"
        or not labels.get("hermes.platform.base-image")
        or not labels.get("hermes.platform.supervisor-tree-sha256")
    ):
        raise RuntimeError("local Runtime image code-only labels are incomplete")
    contract = json.loads(
        _checked(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/cat",
                target.image,
                "/opt/hermes-platform/runtime-code-contract.json",
            ]
        )
    )
    invariant = inspect_runtime_code_invariant(target.image, run_checked=_checked)
    for field, label in {
        "contract_version": "hermes.platform.code-contract-version",
        "build_strategy": "hermes.platform.build-strategy",
        "base_image": "hermes.platform.base-image",
        "platform_revision": "hermes.platform.revision",
        "supervisor_tree_sha256": "hermes.platform.supervisor-tree-sha256",
        "immutable_runtime_sha256": "hermes.platform.immutable-runtime-sha256",
    }.items():
        if contract.get(field) != labels.get(label):
            raise RuntimeError("embedded Runtime code contract does not match image labels")

    receipt_path = (
        REPOSITORY_ROOT
        / ".runtime/runtime-code-images"
        / f"{target.config_id.removeprefix('sha256:')}.json"
    )
    receipt = None
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("image") != target.image
            or receipt.get("platform_manifest_digest") != target.manifest_digest
            or receipt.get("config_id") != target.config_id
            or receipt.get("base_image") != contract.get("base_image")
            or receipt.get("immutable_runtime_sha256")
            != invariant.payload_sha256
        ):
            raise RuntimeError("Runtime image receipt does not match backend/.env")
    if contract.get("immutable_runtime_sha256") != invariant.payload_sha256:
        raise RuntimeError("Runtime image immutable payload no longer matches its contract")

    publisher = _resolve_publisher(
        str((receipt or {}).get("publisher") or ""), args.kind_cluster_name
    )
    published = RuntimeImagePublisher(
        SubprocessRunner(),
        publisher=publisher,
        kind_cluster_name=args.kind_cluster_name,
    ).inspect_published(
        image_reference=target.image,
        local_config_id=target.config_id,
    )
    if (
        published.platform_manifest_digest != target.manifest_digest
        or published.config_id != target.config_id
    ):
        raise RuntimeError("published Runtime image identity does not match backend/.env")
    print(
        json.dumps(
            {
                "image": target.image,
                "manifest_digest": target.manifest_digest,
                "config_id": target.config_id,
                "contract_version": contract["contract_version"],
                "publisher": publisher,
                "receipt_checked": receipt is not None,
            },
            sort_keys=True,
        )
    )
    return 0


def _preflight_complete_runtime(
    target: RuntimeImageConfig,
    labels: dict[str, str],
    cluster_name: str,
) -> int:
    """Validate a complete V3 overlay without applying the code-only contract."""
    required = (
        "hermes.platform.upstream-version",
        "hermes.platform.upstream-source-sha256",
        "hermes.platform.source-manifest-sha256",
        "hermes.platform.base-image-immutable-reference",
        "hermes.platform.runtime-overlay-sha256",
        "hermes.platform.skill-inventory-sha256",
        "hermes.platform.font-manifest-sha256",
    )
    missing = [key for key in required if not labels.get(key)]
    if missing:
        raise RuntimeError("local complete Runtime labels are incomplete: " + ", ".join(missing))
    config = target
    receipt_path = (
        REPOSITORY_ROOT
        / ".runtime/runtime-v3-images"
        / f"{config.config_id.removeprefix('sha256:')}.json"
    )
    if not receipt_path.exists():
        raise RuntimeError("complete Runtime V3 receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        receipt.get("image") != config.image
        or receipt.get("platform_manifest_digest") != config.manifest_digest
        or receipt.get("config_id") != config.config_id
        or receipt.get("hermes_source_unchanged") is not True
    ):
        raise RuntimeError("complete Runtime V3 receipt does not match backend/.env")
    publisher = _resolve_publisher(str(receipt.get("publisher") or ""), cluster_name)
    published = RuntimeImagePublisher(
        SubprocessRunner(), publisher=publisher, kind_cluster_name=cluster_name
    ).inspect_published(image_reference=config.image, local_config_id=config.config_id)
    if published.platform_manifest_digest != config.manifest_digest or published.config_id != config.config_id:
        raise RuntimeError("published complete Runtime image identity does not match backend/.env")
    print(json.dumps({
        "image": config.image,
        "manifest_digest": config.manifest_digest,
        "config_id": config.config_id,
        "build_strategy": "complete-runtime-overlay",
        "upstream_version": labels["hermes.platform.upstream-version"],
        "skill_inventory_sha256": labels["hermes.platform.skill-inventory-sha256"],
        "publisher": publisher,
        "receipt_checked": True,
    }, sort_keys=True))
    return 0


def _resolve_publisher(receipt_publisher: str, cluster_name: str) -> str:
    if receipt_publisher in {"kind", "containerd"}:
        return receipt_publisher
    if shutil.which("kind"):
        result = subprocess.run(
            ["kind", "get", "clusters"], text=True, capture_output=True, check=False
        )
        if result.returncode == 0 and cluster_name in result.stdout.split():
            return "kind"
    if shutil.which("ctr"):
        return "containerd"
    raise RuntimeError("published Runtime image cannot be inspected (kind/ctr unavailable)")


def _checked(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise RuntimeError(f"Runtime image preflight command failed: {message}")
    return completed.stdout


if __name__ == "__main__":
    raise SystemExit(main())
