#!/usr/bin/env python3
"""Verify the complete Hermes Runtime overlay inside the final image."""

from __future__ import annotations

import argparse
import ctypes.util
import hashlib
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
from typing import Any

from skill_inventory import verify_staged_inventory


EXPECTED_DISTRIBUTIONS = {
    "hermes-platform-supervisor": "0.1.0",
    "hermes-hermes-enterprise-knowledge": "0.1.0",
    "hermes-hermes-personal-knowledge": "0.1.0",
    "hermes-hermes-clarify-lifecycle": "0.1.0",
    "hermes-hermes-kanban-lifecycle": "0.1.0",
    "hermes-hermes-expert-roster": "0.1.0",
    "hermes-hermes-platform-main-conversation-boundary": "0.1.0",
    "Pillow": "12.3.0",
    "edge-tts": "7.2.7",
    "fal-client": "0.13.1",
    "simple-term-menu": "1.6.6",
    "setuptools": "83.0.0",
    "watchfiles": "1.1.1",
}
EXPECTED_PLUGIN_ENTRY_POINTS = {
    "enterprise-knowledge": "hermes_enterprise_knowledge.plugin",
    "personal-knowledge": "hermes_personal_knowledge.plugin",
    "clarify-lifecycle": "hermes_clarify_lifecycle.plugin",
    "kanban-lifecycle": "hermes_kanban_lifecycle.plugin",
    "expert-roster": "hermes_expert_roster.plugin",
    "platform-main-conversation-boundary-v1": (
        "platform_main_conversation_boundary.plugin"
    ),
}
REQUIRED_COMMANDS = (
    "chromium",
    "fc-match",
    "ffmpeg",
    "node",
    "pandoc",
    "pdffonts",
    "pdftotext",
    "soffice",
    "unzip",
    "zip",
)


class RuntimeOverlayError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeOverlayError(message)


def verify(
    *,
    inventory_path: Path,
    skill_root: Path,
    hermes_root: Path,
    dispatcher_root: Path,
    dotnet_root: Path,
    fts5_cjk_so: Path = Path("/opt/hermes-platform/lib/libfts5_cjk.so"),
    readonly_skill_root: Path = Path("/opt/hermes/platform-readonly-skills"),
) -> dict[str, Any]:
    distributions: dict[str, str] = {}
    for name, expected_version in EXPECTED_DISTRIBUTIONS.items():
        try:
            actual_version = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeOverlayError(f"missing platform distribution: {name}") from exc
        _require(
            actual_version == expected_version,
            f"platform distribution version mismatch: {name}={actual_version}",
        )
        distributions[name] = actual_version
    importlib.import_module("platform_supervisor.main")
    importlib.import_module("audioop")
    if sys.version_info[:2] >= (3, 13):
        _require(
            metadata.version("audioop-lts") == "0.2.2",
            "Python 3.13 Runtime must retain audioop-lts==0.2.2",
        )

    installed_entry_points = {
        entry.name: entry.value
        for entry in metadata.entry_points(group="hermes_agent.plugins")
    }
    for name, expected_value in EXPECTED_PLUGIN_ENTRY_POINTS.items():
        _require(
            installed_entry_points.get(name) == expected_value,
            f"missing or mismatched Hermes plugin entry point: {name}",
        )
        next(
            entry
            for entry in metadata.entry_points(group="hermes_agent.plugins")
            if entry.name == name
        ).load()

    for command in REQUIRED_COMMANDS:
        _require(
            shutil.which(command) is not None, f"missing Runtime command: {command}"
        )
    _require(
        (3, 11) <= sys.version_info[:2] < (3, 14),
        "Runtime Python is outside the fixed >=3.11,<3.14 range",
    )
    node_version = subprocess.run(
        ["node", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    _require(node_version.startswith("v26."), f"Runtime Node is not version 26: {node_version}")
    _require(ctypes.util.find_library("atomic") is not None, "libatomic1 is unavailable")
    sqlite_connection = sqlite3.connect(":memory:")
    try:
        sqlite_connection.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')"
        )
        sqlite_connection.execute("INSERT INTO docs VALUES ('hermes')")
        matched = sqlite_connection.execute(
            "SELECT count(*) FROM docs WHERE docs MATCH 'erm'"
        ).fetchone()
    finally:
        sqlite_connection.close()
    _require(bool(matched and matched[0] == 1), "SQLite FTS5 trigram self-test failed")
    _require(fts5_cjk_so.is_file(), "missing immutable FTS5 CJK extension")
    cjk_digest = hashlib.sha256(fts5_cjk_so.read_bytes()).hexdigest()
    cjk_connection = sqlite3.connect(":memory:")
    try:
        cjk_connection.enable_load_extension(True)
        cjk_connection.load_extension(str(fts5_cjk_so))
        cjk_connection.enable_load_extension(False)
        cjk_connection.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='cjk_unicode61')"
        )
        cjk_connection.execute("INSERT INTO docs VALUES ('中文检索')")
        cjk_match = cjk_connection.execute(
            "SELECT count(*) FROM docs WHERE docs MATCH '中文'"
        ).fetchone()
    finally:
        cjk_connection.close()
    _require(bool(cjk_match and cjk_match[0] == 1), "SQLite FTS5 CJK self-test failed")
    _require(
        os.environ.get("HERMES_FTS5_CJK_SO") == str(fts5_cjk_so),
        "HERMES_FTS5_CJK_SO does not select the immutable extension",
    )
    _require(
        os.environ.get("HERMES_EXPECTED_CONFIG_SCHEMA") == "34",
        "target Dashboard config schema is not pinned to 34",
    )
    _require(
        (hermes_root / ".venv/bin/platform-supervisor").is_file(),
        "missing platform-supervisor entry point",
    )
    _require(
        (hermes_root / ".venv/bin/platform-hermes-child-exec").is_file(),
        "missing platform-hermes-child-exec entry point",
    )
    venv_python = hermes_root / ".venv/bin/python"
    _require(
        stat.S_ISREG(os.lstat(venv_python).st_mode),
        "Hermes venv Python is not a fixed regular file",
    )
    _require(
        sys.base_prefix == "/opt/hermes-platform/python",
        "Hermes Python base remains under the root-private uv directory",
    )
    _require(
        (hermes_root / "browser-node/node_modules/.bin/agent-browser").is_file(),
        "missing locked agent-browser CLI",
    )
    _require(
        (hermes_root / "office-bin/minimax-docx/MiniMaxAIDocx.Cli").is_file(),
        "missing published minimax-docx CLI",
    )
    _require((dotnet_root / "dotnet").is_file(), "missing .NET runtime host")
    _require(
        (dotnet_root / "shared/Microsoft.NETCore.App").is_dir(),
        "missing .NET 8 runtime",
    )
    _require(not (dotnet_root / "sdk").exists(), ".NET SDK leaked into final image")
    _require(
        (dispatcher_root / "kanban-dispatcher/run").is_file()
        and (dispatcher_root / "user/contents.d/kanban-dispatcher").is_file(),
        "missing Kanban Dispatcher s6 definition",
    )
    _require(
        os.environ.get("HERMES_BUNDLED_SKILLS") == str(skill_root),
        "HERMES_BUNDLED_SKILLS does not select the curated Skill root",
    )
    _require(
        os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1",
        "runtime lazy installs are not disabled",
    )
    for filename in (
        "overlay_contract.py",
        "upstream_component_probe.py",
        "verify_dependency_compatibility.py",
        "verify_lazy_install_policy.py",
        "verify_platform_boundary_plugin.py",
        "source-normalization-f80f453-v1.yaml",
        "v0.20.1.yaml",
    ):
        _require(
            (Path("/opt/hermes-platform/runtime-overlay") / filename).is_file(),
            f"missing Runtime overlay evidence asset: {filename}",
        )

    skill_report = verify_staged_inventory(
        inventory_path=inventory_path,
        destination=skill_root,
    )
    readonly_skill_report = verify_staged_inventory(
        inventory_path=inventory_path,
        destination=readonly_skill_root,
        root="platform-readonly-skills",
    )
    report: dict[str, Any] = {
        "status": "passed",
        "verifier_version": 1,
        "distributions": distributions,
        "plugin_entry_points": {
            name: installed_entry_points[name]
            for name in sorted(EXPECTED_PLUGIN_ENTRY_POINTS)
        },
        "skill_inventory_sha256": skill_report["inventory_sha256"],
        "skill_count": skill_report["skill_count"],
        "readonly_skill_count": readonly_skill_report["skill_count"],
        "dotnet_sdk_present": False,
        "node_version": node_version,
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_fts5_trigram": True,
        "sqlite_fts5_cjk": True,
        "fts5_cjk_sha256": cjk_digest,
        "libatomic_present": True,
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory",
        default="/opt/hermes-platform/runtime-overlay/skill-inventory.json",
    )
    parser.add_argument(
        "--skill-root",
        default="/opt/hermes/platform-default-skills",
    )
    parser.add_argument("--hermes-root", default="/opt/hermes")
    parser.add_argument("--dispatcher-root", default="/etc/s6-overlay/s6-rc.d")
    parser.add_argument("--dotnet-root", default="/usr/share/dotnet")
    args = parser.parse_args()
    report = verify(
        inventory_path=Path(args.inventory),
        skill_root=Path(args.skill_root),
        hermes_root=Path(args.hermes_root),
        dispatcher_root=Path(args.dispatcher_root),
        dotnet_root=Path(args.dotnet_root),
    )
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
