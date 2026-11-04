#!/usr/bin/env python3
"""Verify the fixed upstream dependency override without hiding other conflicts."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - target images use Python 3.11+
    import tomli as tomllib


EXPECTED_MSAL_VERSION = "1.36.0"
EXPECTED_CRYPTOGRAPHY_VERSION = "50.0.0"
EXPECTED_PROJECT_PIN = "cryptography==50.0.0"
EXPECTED_UV_OVERRIDE = "cryptography>=50,<51"
EXPECTED_CONFLICT = (
    "The package `msal` requires `cryptography>=2.5,<49`, "
    "but `50.0.0` is installed"
)
_CHECKED_LINE = re.compile(r"^Checked [1-9][0-9]* packages in .+$")


class DependencyCompatibilityError(RuntimeError):
    pass


def _load_project(root: Path) -> dict:
    path = root / "pyproject.toml"
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DependencyCompatibilityError("cannot read fixed upstream pyproject") from exc
    if not isinstance(value, dict):
        raise DependencyCompatibilityError("fixed upstream pyproject is invalid")
    return value


def _verify_upstream_override(root: Path) -> None:
    project = _load_project(root)
    project_table = project.get("project")
    tool = project.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    dependencies = project_table.get("dependencies") if isinstance(project_table, dict) else None
    overrides = uv.get("override-dependencies") if isinstance(uv, dict) else None
    if (
        not isinstance(dependencies, list)
        or EXPECTED_PROJECT_PIN not in dependencies
        or not isinstance(overrides, list)
        or EXPECTED_UV_OVERRIDE not in overrides
    ):
        raise DependencyCompatibilityError("fixed upstream cryptography override drifted")


def _verify_versions() -> None:
    if (
        metadata.version("msal") != EXPECTED_MSAL_VERSION
        or metadata.version("cryptography") != EXPECTED_CRYPTOGRAPHY_VERSION
    ):
        raise DependencyCompatibilityError("allowed dependency conflict version drifted")


def _verify_uv_output(*, returncode: int, output: str) -> None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    checked = [line for line in lines if _CHECKED_LINE.fullmatch(line)]
    semantic = [line for line in lines if line not in checked]
    if (
        returncode != 1
        or len(checked) > 1
        or semantic != ["Found 1 incompatibility", EXPECTED_CONFLICT]
    ):
        raise DependencyCompatibilityError("uv pip check reported an unexpected conflict set")


def verify(*, root: Path, python: Path, uv: Path) -> dict[str, object]:
    if not root.is_dir() or not python.is_file() or not uv.is_file():
        raise DependencyCompatibilityError("dependency verifier input is missing")
    _verify_upstream_override(root)
    _verify_versions()
    completed = subprocess.run(
        [str(uv), "pip", "check", "--python", str(python), "--no-progress"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    _verify_uv_output(
        returncode=completed.returncode,
        output="\n".join((completed.stdout, completed.stderr)),
    )
    return {
        "status": "passed",
        "contract": "hermes-upstream-dependency-overrides-f80f453-v1",
        "allowed_conflicts": [
            {
                "requiring_distribution": "msal",
                "requiring_version": EXPECTED_MSAL_VERSION,
                "requirement": "cryptography>=2.5,<49",
                "installed_distribution": "cryptography",
                "installed_version": EXPECTED_CRYPTOGRAPHY_VERSION,
                "upstream_override": EXPECTED_UV_OVERRIDE,
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/opt/hermes")
    parser.add_argument("--python", default="/opt/hermes/.venv/bin/python")
    parser.add_argument("--uv", default="/usr/local/bin/uv")
    args = parser.parse_args()
    try:
        report = verify(root=Path(args.root), python=Path(args.python), uv=Path(args.uv))
    except (DependencyCompatibilityError, metadata.PackageNotFoundError) as exc:
        print(f"dependency compatibility verification failed: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
