#!/usr/bin/env python3
"""Read-only verification of official Hermes source inside a Runtime image."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from typing import Any


MANIFEST_VERSION = 1
IGNORED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
ALLOWED_RUNTIME_PREFIXES = (
    PurePosixPath(".git"),
    PurePosixPath(".venv"),
    PurePosixPath(".playwright"),
    PurePosixPath("browser-node"),
    PurePosixPath("office-bin"),
    PurePosixPath("office-node"),
    PurePosixPath("office-venv"),
    PurePosixPath("platform-default-skills"),
    PurePosixPath("node_modules"),
    PurePosixPath("web/node_modules"),
    PurePosixPath("ui-tui/node_modules"),
    PurePosixPath("plugins/platforms/photon/sidecar/node_modules"),
    PurePosixPath("hermes_cli/web_dist"),
    PurePosixPath("ui-tui/dist"),
    PurePosixPath("hermes_agent.egg-info"),
    PurePosixPath("bin/hermes"),
    PurePosixPath(".hermes_build_sha"),
    PurePosixPath(".install_method"),
    PurePosixPath("README.md"),
)


class SourceIntegrityError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SourceIntegrityError(f"unsafe source path: {value!r}")
    if path.as_posix() != value:
        raise SourceIntegrityError(f"non-canonical source path: {value!r}")
    return path


def _under(path: PurePosixPath, prefix: PurePosixPath) -> bool:
    return path == prefix or prefix in path.parents


def _runtime_only(path: PurePosixPath) -> bool:
    return bool(IGNORED_PARTS.intersection(path.parts)) or any(
        _under(path, prefix) for prefix in ALLOWED_RUNTIME_PREFIXES
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceIntegrityError(f"cannot read source manifest: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("manifest_version") != MANIFEST_VERSION
        or not isinstance(payload.get("files"), dict)
        or not isinstance(payload.get("upstream_commit"), str)
        or not isinstance(payload.get("manifest_sha256"), str)
    ):
        raise SourceIntegrityError("invalid source manifest")
    return payload


def verify(*, manifest_path: Path, root: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    expected_files = manifest["files"]
    modified: list[str] = []
    missing: list[str] = []
    unexpected: list[str] = []

    for raw, expected in sorted(expected_files.items()):
        relative = _canonical_relative(raw)
        target = root.joinpath(*relative.parts)
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            missing.append(raw)
            continue
        if not isinstance(expected, dict):
            modified.append(raw)
        elif expected.get("kind") == "symlink":
            actual = (
                {"kind": "symlink", "target": os.readlink(target)}
                if stat.S_ISLNK(mode)
                else None
            )
            if actual != expected:
                modified.append(raw)
        elif expected.get("kind") == "file":
            actual = (
                {
                    "kind": "file",
                    "sha256": _sha256(target),
                    "mode": stat.S_IMODE(mode),
                }
                if stat.S_ISREG(mode)
                else None
            )
            if actual != expected:
                modified.append(raw)
        else:
            modified.append(raw)

    for path in sorted(root.rglob("*")):
        if not (path.is_file() or path.is_symlink()):
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if relative.as_posix() in expected_files or _runtime_only(relative):
            continue
        unexpected.append(relative.as_posix())

    report: dict[str, Any] = {
        "status": "passed"
        if not modified and not missing and not unexpected
        else "failed",
        "upstream_commit": manifest["upstream_commit"],
        "source_manifest_sha256": manifest["manifest_sha256"],
        "modified_count": len(modified),
        "missing_count": len(missing),
        "unexpected_count": len(unexpected),
        "modified": modified[:100],
        "missing": missing[:100],
        "unexpected": unexpected[:100],
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--root", default="/opt/hermes")
    args = parser.parse_args()
    report = verify(
        manifest_path=Path(args.manifest),
        root=Path(args.root),
    )
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(_main())
