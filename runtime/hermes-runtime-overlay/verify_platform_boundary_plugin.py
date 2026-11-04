#!/usr/bin/env python3
"""Verify the platform main-conversation marker plugin without loading Hermes."""

from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - host test interpreter is 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from overlay_contract import canonical_json, load_overlay_contract


class BoundaryPluginVerificationError(RuntimeError):
    pass


def _secure_digest(root: Path, relative: str) -> str:
    target = root.joinpath(*relative.split("/"))
    resolved_root = root.resolve(strict=True)
    try:
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise BoundaryPluginVerificationError(f"unsafe plugin source: {relative}") from exc
    cursor = root
    for part in relative.split("/"):
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
        except OSError as exc:
            raise BoundaryPluginVerificationError(f"missing plugin source: {relative}") from exc
        if stat.S_ISLNK(mode):
            raise BoundaryPluginVerificationError(f"plugin source uses symlink: {relative}")
    if not resolved_target.is_file():
        raise BoundaryPluginVerificationError(f"plugin source is not a file: {relative}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved_target, flags)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return "sha256:" + digest.hexdigest()


def _registration_surface(plugin_root: Path) -> dict[str, Any]:
    plugin_path = (
        plugin_root
        / "src"
        / "platform_main_conversation_boundary"
        / "plugin.py"
    )
    tree = ast.parse(plugin_path.read_text(encoding="utf-8"), filename=str(plugin_path))
    registrations: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not node.func.attr.startswith("register_"):
            continue
        value = None
        if node.args and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value if isinstance(node.args[0].value, str) else None
        registrations.append((node.func.attr, value))
    if registrations != [("register_middleware", "llm_request")]:
        raise BoundaryPluginVerificationError(
            f"plugin registration surface mismatch: {registrations!r}"
        )
    return {"middleware": ["llm_request"], "tools": [], "hooks": [], "listeners": []}


def _verify_pyproject(plugin_root: Path, contract: dict[str, Any]) -> None:
    try:
        pyproject = tomllib.loads((plugin_root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BoundaryPluginVerificationError("cannot read boundary plugin pyproject") from exc
    project = pyproject.get("project") if isinstance(pyproject, dict) else None
    entry_points = project.get("entry-points") if isinstance(project, dict) else None
    group = (
        entry_points.get(contract["entry_point_group"])
        if isinstance(entry_points, dict)
        else None
    )
    if (
        not isinstance(project, dict)
        or project.get("name") != contract["distribution"]
        or project.get("version") != contract["version"]
        or not isinstance(group, dict)
        or group.get(contract["entry_point_name"]) != contract["entry_point_value"]
    ):
        raise BoundaryPluginVerificationError("boundary plugin packaging identity mismatch")


def _verify_golden(plugin_root: Path, contract: dict[str, Any]) -> None:
    sys.path.insert(0, str(plugin_root / "src"))
    try:
        from platform_main_conversation_boundary.marker import (
            BoundaryMarkerSigner,
            canonical_json as marker_canonical_json,
            route_token_from_base_url,
        )
    finally:
        sys.path.pop(0)
    golden = contract["golden_vector"]
    payload = golden["payload"]
    key = base64.urlsafe_b64decode(golden["key_base64url"] + "=")
    request_nonce = base64.urlsafe_b64decode(payload["request_nonce"] + "==")
    signer = BoundaryMarkerSigner(
        key=key,
        boundary_epoch_nonce=payload["boundary_epoch_nonce"],
        clock=lambda: datetime.fromisoformat(payload["issued_at"].replace("Z", "+00:00")),
        random_bytes=lambda size: request_nonce if size == 16 else b"",
    )
    actual = signer.build_marker(
        route_token=payload["route_token"],
        session_id=payload["session_id"],
        task_id=payload["task_id"],
        turn_id=payload["turn_id"],
        api_request_id=payload["api_request_id"],
        api_call_count=payload["api_call_count"],
    )
    if marker_canonical_json(payload).decode("utf-8") != golden["canonical_payload"]:
        raise BoundaryPluginVerificationError("boundary marker canonical payload mismatch")
    payload_digest = "sha256:" + hashlib.sha256(marker_canonical_json(payload)).hexdigest()
    if payload_digest != golden["payload_digest"]:
        raise BoundaryPluginVerificationError("boundary marker payload digest mismatch")
    if actual != golden["header"]:
        raise BoundaryPluginVerificationError("boundary marker header mismatch")
    if route_token_from_base_url(golden["canonical_base_url"]) != payload["route_token"]:
        raise BoundaryPluginVerificationError("canonical Relay base URL did not match")
    if any(route_token_from_base_url(value) is not None for value in golden["invalid_base_urls"]):
        raise BoundaryPluginVerificationError("non-canonical Relay base URL produced a marker")


def verify(*, manifest_path: Path, plugin_root: Path) -> dict[str, Any]:
    manifest = load_overlay_contract(manifest_path)
    contract = manifest["runtime_overlay"]["platform_main_conversation_boundary"]
    _verify_pyproject(plugin_root, contract)
    actual_sources = {
        relative: _secure_digest(plugin_root, relative)
        for relative in sorted(contract["source_files"])
    }
    if actual_sources != contract["source_files"]:
        raise BoundaryPluginVerificationError("boundary plugin source digest mismatch")
    surface = _registration_surface(plugin_root)
    _verify_golden(plugin_root, contract)
    report: dict[str, Any] = {
        "status": "passed",
        "contract": contract["contract"],
        "distribution": contract["distribution"],
        "version": contract["version"],
        "source_files": actual_sources,
        "registration_surface": surface,
        "golden_vector_passed": True,
    }
    report["report_digest"] = "sha256:" + hashlib.sha256(canonical_json(report)).hexdigest()
    return report


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--plugin-root", required=True)
    args = parser.parse_args()
    report = verify(
        manifest_path=Path(args.manifest),
        plugin_root=Path(args.plugin_root),
    )
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except (BoundaryPluginVerificationError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
