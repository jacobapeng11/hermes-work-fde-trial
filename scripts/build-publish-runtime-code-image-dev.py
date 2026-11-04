#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.services.runtime_image_config import (  # noqa: E402
    RuntimeImageConfig,
    RuntimeImageConfigStore,
)
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
    lock_path = REPOSITORY_ROOT / ".runtime/runtime-image-lifecycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lifecycle_lock:
        try:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another Runtime image lifecycle operation is active"
            ) from exc
        return _run()


def _run() -> int:
    parser = argparse.ArgumentParser(
        description="Build a unique Supervisor code overlay, publish it, and update backend/.env."
    )
    parser.add_argument(
        "--env-file", type=Path, default=REPOSITORY_ROOT / "backend/.env"
    )
    parser.add_argument(
        "--publisher", choices=("auto", "kind", "containerd"), default="auto"
    )
    parser.add_argument(
        "--image-repository",
        default="kind.local/hermes-agent",
        help="Local Runtime image repository (must end with hermes-agent).",
    )
    parser.add_argument("--kind-cluster-name", default="hermes")
    parser.add_argument(
        "--capabilities-image",
        default=None,
        help="Optional immutable Hermes capabilities-layer image used as the code overlay parent.",
    )
    parser.add_argument(
        "--active-mcp-reload-test",
        action="store_true",
        help=(
            "Build a test-only image that allows one active-Run MCP reload smoke; "
            "never use this image as the fleet default."
        ),
    )
    parser.add_argument("--print-plan", action="store_true")
    args = parser.parse_args()
    if not args.image_repository.endswith("hermes-agent"):
        parser.error("--image-repository must end with hermes-agent")

    config_store = RuntimeImageConfigStore(args.env_file)
    current = config_store.read()
    revision = _checked(["git", "rev-parse", "HEAD"]).strip()
    revision_short = revision[:12]
    if len(revision) != 40:
        raise RuntimeError("git HEAD is not a full 40-character release revision")
    supervisor_digest = _tree_digest(REPOSITORY_ROOT / "runtime/supervisor")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    image_kind = (
        "active-mcp-reload-test" if args.active_mcp_reload_test else "code"
    )
    target = (
        f"{args.image_repository}:{image_kind}-{timestamp}-{revision_short}-"
        f"{supervisor_digest[:12]}"
    )
    tracked_tree_before = _tracked_tree_state()
    publisher = _resolve_publisher(args.publisher, args.kind_cluster_name)
    if args.print_plan:
        print(
            json.dumps(
                {
                    "base_image": current.image,
                    "capabilities_image": args.capabilities_image
                    or "auto:current-image-ancestry",
                    "target_image": target,
                    "platform_revision": revision,
                    "supervisor_tree_sha256": supervisor_digest,
                    "publisher": publisher,
                    "dockerfile": "Dockerfile.runtime-code-overlay",
                    "active_mcp_reload_test": args.active_mcp_reload_test,
                    "will_update": [
                        "HERMES_RUNTIME_IMAGE",
                        "HERMES_RUNTIME_IMAGE_MANIFEST_DIGEST",
                        "HERMES_RUNTIME_IMAGE_CONFIG_ID",
                    ],
                },
                sort_keys=True,
            )
        )
        return 0

    if (
        subprocess.run(
            ["docker", "image", "inspect", target],
            text=True,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    ):
        raise RuntimeError(f"unique Runtime image tag already exists: {target}")

    capabilities_image = _resolve_code_base(args.capabilities_image or current.image)
    base_invariant = inspect_runtime_code_invariant(
        current.image,
        run_checked=_checked,
    )

    with tempfile.TemporaryDirectory(
        prefix="hermes-runtime-code-context-"
    ) as context_dir:
        context = Path(context_dir)
        _prepare_context(context)
        _run_checked(
            [
                "docker",
                "build",
                "--build-arg",
                f"HERMES_BASE_IMAGE={current.image}",
                "--build-arg",
                f"HERMES_CAPABILITIES_IMAGE={capabilities_image}",
                "--build-arg",
                f"HERMES_PLATFORM_REVISION={revision}",
                "--build-arg",
                f"HERMES_SUPERVISOR_TREE_SHA256={supervisor_digest}",
                "--build-arg",
                f"HERMES_IMMUTABLE_RUNTIME_SHA256={base_invariant.payload_sha256}",
                "--build-arg",
                "HERMES_ACTIVE_MCP_RELOAD_TEST="
                + ("1" if args.active_mcp_reload_test else "0"),
                "-f",
                str(context / "Dockerfile.runtime-code-overlay"),
                "-t",
                target,
                str(context),
            ]
        )
    inspected = json.loads(_checked(["docker", "image", "inspect", target]))[0]
    target_invariant = inspect_runtime_code_invariant(target, run_checked=_checked)
    if (
        target_invariant.payload != base_invariant.payload
        or target_invariant.oci_config != base_invariant.oci_config
        or target_invariant.payload_sha256 != base_invariant.payload_sha256
    ):
        raise RuntimeError("code-only Runtime image changed an immutable parent contract")
    local_config_id = str(inspected.get("Id") or "")
    labels = (inspected.get("Config") or {}).get("Labels") or {}
    expected_labels = {
        "hermes.platform.code-contract-version": "runtime-code-contract-v1",
        "hermes.platform.build-strategy": "supervisor-code-only",
        "hermes.platform.base-image": current.image,
        "hermes.platform.capabilities-image": capabilities_image,
        "hermes.platform.revision": revision,
        "hermes.platform.supervisor-tree-sha256": supervisor_digest,
        "hermes.platform.immutable-runtime-sha256": base_invariant.payload_sha256,
        "hermes.platform.active-mcp-reload-test": (
            "1" if args.active_mcp_reload_test else "0"
        ),
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise RuntimeError("built Runtime image code contract labels are incomplete")

    runner = SubprocessRunner()
    with tempfile.TemporaryDirectory(prefix="hermes-runtime-publish-") as temporary:
        identity = RuntimeImagePublisher(
            runner,
            publisher=publisher,
            kind_cluster_name=args.kind_cluster_name,
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
        "contract_version": "runtime-code-build-receipt-v1",
        "base_image": current.image,
        "capabilities_image": capabilities_image,
        "image": identity.reference,
        "containerd_target_digest": identity.target_digest,
        "platform_manifest_digest": identity.platform_manifest_digest,
        "config_id": identity.config_id,
        "publisher": identity.publisher,
        "platform_revision": revision,
        "supervisor_tree_sha256": supervisor_digest,
        "immutable_runtime_sha256": base_invariant.payload_sha256,
        "active_mcp_reload_test": args.active_mcp_reload_test,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_dir = REPOSITORY_ROOT / ".runtime/runtime-code-images"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{identity.config_id.removeprefix('sha256:')}.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_path.chmod(0o600)
    tracked_tree_after = _tracked_tree_state()
    if tracked_tree_after != tracked_tree_before:
        raise RuntimeError("Runtime image build modified Git tracked files")
    print(
        json.dumps(
            {
                "image": identity.reference,
                "platform_revision": revision,
                "supervisor_tree_sha256": supervisor_digest,
                "platform_manifest_digest": identity.platform_manifest_digest,
                "config_id": identity.config_id,
                "receipt": str(receipt_path.relative_to(REPOSITORY_ROOT)),
                "env_updated": str(args.env_file),
            },
            sort_keys=True,
        )
    )
    return 0


def _resolve_code_base(image_reference: str) -> str:
    """Follow only this image's ancestry; never stack another code overlay."""
    seen: set[str] = set()
    while image_reference not in seen:
        seen.add(image_reference)
        inspected = json.loads(
            _checked(["docker", "image", "inspect", image_reference])
        )[0]
        labels = (inspected.get("Config") or {}).get("Labels") or {}
        if labels.get("hermes.platform.active-mcp-reload-test") == "1":
            raise RuntimeError("test-only MCP image cannot be a Runtime fleet base")
        if labels.get("hermes.platform.build-strategy") != "supervisor-code-only":
            # Keep the repository-qualified reference; a bare image ID is
            # parsed by Docker as docker.io/library/<id> instead of local data.
            return image_reference
        parent = labels.get("hermes.platform.capabilities-image") or labels.get(
            "hermes.platform.base-image"
        )
        if not parent:
            raise RuntimeError(
                "code image has no recorded base; restore its full Runtime base"
            )
        image_reference = str(parent)
    raise RuntimeError("Runtime code image ancestry contains a cycle")


def _prepare_context(context: Path) -> None:
    shutil.copy2(
        REPOSITORY_ROOT / "Dockerfile.runtime-code-overlay",
        context / "Dockerfile.runtime-code-overlay",
    )
    source = REPOSITORY_ROOT / "runtime/supervisor"
    package_root = context / "runtime/supervisor"
    package_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "pyproject.toml", package_root / "pyproject.toml")
    shutil.copytree(
        source / "platform_supervisor",
        package_root / "platform_supervisor",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyo",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        ),
    )


def _resolve_publisher(requested: str, cluster_name: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("kind"):
        clusters = subprocess.run(
            ["kind", "get", "clusters"],
            text=True,
            capture_output=True,
            check=False,
        )
        if clusters.returncode == 0 and cluster_name in clusters.stdout.split():
            return "kind"
    if shutil.which("ctr"):
        return "containerd"
    raise RuntimeError("no local Runtime image publisher is available (kind or ctr)")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(
            part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
            for part in path.parts
        )
        and path.suffix not in {".pyc", ".pyo"}
    )
    for path in files:
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _checked(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise RuntimeError(f"{' '.join(command)} failed: {message}")
    return completed.stdout


def _run_checked(command: list[str]) -> None:
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed with exit {completed.returncode}"
        )


def _tracked_tree_state() -> str:
    """Return a stable snapshot of tracked-file changes before/after a build."""
    return _checked(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
