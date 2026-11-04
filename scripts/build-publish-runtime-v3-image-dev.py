#!/usr/bin/env python3
"""Build and publish the complete Skill Policy v3 Runtime overlay.

This intentionally uses the immutable Hermes v0.20.1 parent recorded on the
current image.  It changes only the platform overlay, bundled Skill assets and
pre-installed capability dependencies; Hermes source is never copied or
patched by this flow.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import re

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.services.runtime_image_config import RuntimeImageConfig, RuntimeImageConfigStore  # noqa: E402
from app.services.runtime_image_publisher import RuntimeImageCommandResult, RuntimeImagePublisher  # noqa: E402


REQUIRED_PARENT_LABELS = (
    "hermes.platform.upstream-version",
    "hermes.platform.upstream-source-sha256",
    "hermes.platform.source-manifest-sha256",
    "hermes.platform.base-image-reference",
    "hermes.platform.base-image-immutable-reference",
    "hermes.platform.base-image-digest",
    "hermes.platform.parent-type",
    "hermes.platform.parent-attestation-sha256",
    "hermes.platform.root-official-image-digest",
)

CONTENT_PATHS = (
    "Dockerfile.runtime-supervisor-overlay",
    "runtime/hermes-runtime-overlay",
    "runtime/hermes-browser-runtime",
    "runtime/hermes-browser-skills",
    "runtime/hermes-platform-skills",
    "runtime/hermes-office-runtime-zh-v2",
    "runtime/hermes-office-skills-zh-v2",
    "runtime/hermes-capability-runtime",
    "runtime/supervisor",
    "runtime/enterprise-knowledge-plugin",
    "runtime/personal-knowledge-plugin",
    "runtime/clarify-lifecycle-plugin",
    "runtime/kanban-lifecycle-plugin",
    "runtime/expert-roster-plugin",
    "runtime/kanban-dispatcher",
    "backend/app/hermes_runtime/official-upstream-skills.json",
)


class SubprocessRunner:
    def run(self, command: list[str]) -> RuntimeImageCommandResult:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        return RuntimeImageCommandResult(completed.stdout, completed.stderr, completed.returncode)


def main() -> int:
    lock_path = REPOSITORY_ROOT / ".runtime/runtime-image-lifecycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lifecycle_lock:
        try:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Runtime image lifecycle operation is active") from exc
        return _run()


def _run() -> int:
    parser = argparse.ArgumentParser(
        description="Build the complete Skill Policy v3 Runtime overlay and atomically publish it."
    )
    parser.add_argument("--env-file", type=Path, default=REPOSITORY_ROOT / "backend/.env")
    parser.add_argument("--publisher", choices=("auto", "kind", "containerd"), default="auto")
    parser.add_argument("--image-repository", default="kind.local/hermes-agent")
    parser.add_argument("--kind-cluster-name", default="hermes")
    parser.add_argument("--print-plan", action="store_true")
    args = parser.parse_args()
    if not args.image_repository.endswith("hermes-agent"):
        parser.error("--image-repository must end with hermes-agent")

    config_store = RuntimeImageConfigStore(args.env_file)
    current = config_store.read()
    current_inspect = _inspect_image(current.image)
    current_labels = _labels(current_inspect)
    _validate_parent_labels(current_labels)
    parent = current_labels["hermes.platform.base-image-immutable-reference"]
    if "@sha256:" not in parent:
        raise RuntimeError("current Runtime image has no immutable Hermes parent reference")
    # The certified parent may only exist in the target containerd namespace
    # (and not in the host Docker daemon).  In that case the current image is
    # a safe local build source: the complete overlay overwrites Supervisor,
    # bundled Skills and all capability paths, while the immutable parent
    # identity remains asserted in the output labels.
    build_base, parent_available = _resolve_build_base(parent, current.image)

    revision = _checked(["git", "rev-parse", "HEAD"]).strip()
    if len(revision) != 40:
        raise RuntimeError("git HEAD is not a full 40-character release revision")
    overlay_digest = _content_digest()
    inventory_digest = _file_digest(REPOSITORY_ROOT / "runtime/hermes-runtime-overlay/skill-inventory.json")
    font_digest = _file_digest(REPOSITORY_ROOT / "runtime/hermes-office-runtime-zh-v2/font-manifest.json")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    target = f"{args.image_repository}:v3-{timestamp}-{revision[:12]}-{overlay_digest[:12]}"
    publisher = _resolve_publisher(args.publisher, args.kind_cluster_name)
    build_proxy = _docker_build_proxy()
    tracked_tree_before = _tracked_tree_state()
    plan = {
        "base_image": parent,
        "build_base_image": build_base,
        "immutable_parent_available": parent_available,
        "current_image": current.image,
        "target_image": target,
        "dockerfile": "Dockerfile.runtime-supervisor-overlay",
        "build_strategy": "complete-runtime-overlay",
        "platform_revision": revision,
        "runtime_overlay_sha256": overlay_digest,
        "skill_inventory_sha256": inventory_digest,
        "font_manifest_sha256": font_digest,
        "publisher": publisher,
        "build_network": "host",
        "build_proxy_configured": bool(build_proxy),
        "will_update": ["HERMES_RUNTIME_IMAGE", "HERMES_RUNTIME_IMAGE_MANIFEST_DIGEST", "HERMES_RUNTIME_IMAGE_CONFIG_ID"],
        "hermes_source_unchanged": True,
    }
    if args.print_plan:
        print(json.dumps(plan, sort_keys=True))
        return 0

    if subprocess.run(["docker", "image", "inspect", target], capture_output=True, check=False).returncode == 0:
        raise RuntimeError(f"unique Runtime image tag already exists: {target}")

    build_args = [
        "--network", "host",
        "--build-arg", f"HERMES_BASE_IMAGE={build_base}",
        "--build-arg", f"HERMES_UPSTREAM_VERSION={current_labels['hermes.platform.upstream-version']}",
        "--build-arg", f"HERMES_UPSTREAM_COMMIT={current_labels['hermes.platform.upstream-source-sha256']}",
        "--build-arg", f"HERMES_UPSTREAM_SOURCE_SHA256={current_labels['hermes.platform.upstream-source-sha256']}",
        "--build-arg", f"HERMES_SOURCE_MANIFEST_SHA256={current_labels['hermes.platform.source-manifest-sha256']}",
        "--build-arg", f"HERMES_PLATFORM_REVISION={revision}",
        "--build-arg", f"HERMES_RELEASE_ID=hr-v3-{timestamp}-{overlay_digest[:16]}",
        "--build-arg", f"HERMES_BASE_IMAGE_REFERENCE={current_labels['hermes.platform.base-image-reference']}",
        "--build-arg", f"HERMES_BASE_IMAGE_IMMUTABLE_REFERENCE={parent}",
        "--build-arg", f"HERMES_BASE_IMAGE_DIGEST={current_labels['hermes.platform.base-image-digest']}",
        "--build-arg", f"HERMES_PARENT_TYPE={current_labels['hermes.platform.parent-type']}",
        "--build-arg", f"HERMES_PARENT_ATTESTATION_SHA256={current_labels['hermes.platform.parent-attestation-sha256']}",
        "--build-arg", f"HERMES_ROOT_OFFICIAL_IMAGE_DIGEST={current_labels['hermes.platform.root-official-image-digest']}",
        "--build-arg", f"HERMES_RUNTIME_OVERLAY_SHA256={overlay_digest}",
        "--build-arg", f"HERMES_SKILL_INVENTORY_SHA256={inventory_digest}",
        "--build-arg", f"HERMES_FONT_MANIFEST_SHA256={font_digest}",
        "-f", str(REPOSITORY_ROOT / "Dockerfile.runtime-supervisor-overlay"),
        "-t", target, str(REPOSITORY_ROOT),
    ]
    if os.environ.get("HERMES_RUNTIME_BUILD_NO_CACHE"):
        build_args.insert(0, "--no-cache")
    if build_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            value = build_proxy.get(key)
            if value:
                build_args[0:0] = ["--build-arg", f"{key}={value}"]
    _run_checked(["docker", "build", *build_args])
    inspected = _inspect_image(target)
    labels = _labels(inspected)
    expected = {
        "hermes.platform.build-strategy": "complete-runtime-overlay",
        "hermes.platform.upstream-version": current_labels["hermes.platform.upstream-version"],
        "hermes.platform.upstream-source-sha256": current_labels["hermes.platform.upstream-source-sha256"],
        "hermes.platform.source-manifest-sha256": current_labels["hermes.platform.source-manifest-sha256"],
        "hermes.platform.base-image-immutable-reference": parent,
        "hermes.platform.revision": revision,
        "hermes.platform.runtime-overlay-sha256": overlay_digest,
        "hermes.platform.skill-inventory-sha256": inventory_digest,
        "hermes.platform.font-manifest-sha256": font_digest,
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RuntimeError("built V3 Runtime image labels do not close the parent/source contract")

    local_config_id = str(inspected.get("Id") or "")
    with tempfile.TemporaryDirectory(prefix="hermes-runtime-v3-publish-") as temporary:
        identity = RuntimeImagePublisher(
            SubprocessRunner(), publisher=publisher, kind_cluster_name=args.kind_cluster_name
        ).publish(
            image_reference=target,
            local_config_id=local_config_id,
            docker_archive_path=str(Path(temporary) / "runtime-image.tar"),
        )
    replacement = RuntimeImageConfig(
        image=identity.reference,
        manifest_digest=identity.platform_manifest_digest,
        config_id=identity.config_id,
    )
    config_store.replace(expected=current, replacement=replacement)
    receipt = {
        "contract_version": "runtime-v3-build-receipt-v1",
        "base_image": parent,
        "current_image": current.image,
        "image": identity.reference,
        "containerd_target_digest": identity.target_digest,
        "platform_manifest_digest": identity.platform_manifest_digest,
        "config_id": identity.config_id,
        "publisher": identity.publisher,
        "platform_revision": revision,
        "runtime_overlay_sha256": overlay_digest,
        "skill_inventory_sha256": inventory_digest,
        "font_manifest_sha256": font_digest,
        "hermes_source_unchanged": True,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt["receipt_sha256"] = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt_dir = REPOSITORY_ROOT / ".runtime/runtime-v3-images"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{identity.config_id.removeprefix('sha256:')}.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    if _tracked_tree_state() != tracked_tree_before:
        raise RuntimeError("Runtime image build modified Git tracked files")
    print(json.dumps({"image": identity.reference, "config_id": identity.config_id, "platform_manifest_digest": identity.platform_manifest_digest, "receipt": str(receipt_path.relative_to(REPOSITORY_ROOT)), "env_updated": str(args.env_file), **{k: plan[k] for k in ("base_image", "platform_revision", "runtime_overlay_sha256", "skill_inventory_sha256", "font_manifest_sha256")}}, sort_keys=True))
    return 0


def _inspect_image(reference: str) -> dict[str, object]:
    try:
        document = json.loads(_checked(["docker", "image", "inspect", reference]))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"docker image inspect returned invalid JSON for {reference}") from exc
    if not document or not isinstance(document[0], dict):
        raise RuntimeError(f"docker image inspect returned no image for {reference}")
    return document[0]


def _labels(inspected: dict[str, object]) -> dict[str, str]:
    labels = (inspected.get("Config") or {}).get("Labels") if isinstance(inspected.get("Config"), dict) else None
    return {str(k): str(v) for k, v in (labels or {}).items() if v is not None}


def _validate_parent_labels(labels: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_PARENT_LABELS if not labels.get(key)]
    if missing:
        raise RuntimeError("current Runtime image lacks immutable parent labels: " + ", ".join(missing))


def _resolve_build_base(parent: str, current: str) -> tuple[str, bool]:
    result = subprocess.run(["docker", "image", "inspect", parent], capture_output=True, check=False)
    if result.returncode == 0:
        return parent, True
    current_result = subprocess.run(["docker", "image", "inspect", current], capture_output=True, check=False)
    if current_result.returncode != 0:
        raise RuntimeError("neither immutable Hermes parent nor current Runtime image is available to Docker")
    return current, False


def _content_digest() -> str:
    digest = hashlib.sha256()
    for relative in CONTENT_PATHS:
        path = REPOSITORY_ROOT / relative
        if not path.exists():
            raise RuntimeError(f"V3 content path is missing: {relative}")
        if path.is_file():
            files = [path]
        else:
            files = sorted(path.rglob("*"))
        for file_path in files:
            if not file_path.is_file() or file_path.suffix in {".pyc", ".pyo"} or "__pycache__" in file_path.parts:
                continue
            name = file_path.relative_to(REPOSITORY_ROOT).as_posix().encode()
            data = file_path.read_bytes()
            digest.update(len(name).to_bytes(4, "big"))
            digest.update(name)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"required V3 manifest is missing: {path.relative_to(REPOSITORY_ROOT)}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_publisher(requested: str, cluster_name: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("kind"):
        result = subprocess.run(["kind", "get", "clusters"], text=True, capture_output=True, check=False)
        if result.returncode == 0 and cluster_name in result.stdout.split():
            return "kind"
    if shutil.which("ctr"):
        return "containerd"
    raise RuntimeError("no local Runtime image publisher is available (kind or ctr)")


def _docker_build_proxy() -> dict[str, str]:
    """Read daemon proxy endpoints without reintroducing them into service env."""
    result = subprocess.run(["docker", "info"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(HTTP Proxy|HTTPS Proxy|No Proxy):\s*(\S+)", line)
        if match:
            values[{"HTTP Proxy": "HTTP_PROXY", "HTTPS Proxy": "HTTPS_PROXY", "No Proxy": "NO_PROXY"}[match.group(1)]] = match.group(2)
    return values


def _checked(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {completed.stderr.strip() or completed.returncode}")
    return completed.stdout


def _run_checked(command: list[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed with exit {completed.returncode}")


def _tracked_tree_state() -> str:
    return _checked(["git", "status", "--porcelain=v1", "--untracked-files=no"])


if __name__ == "__main__":
    raise SystemExit(main())
