#!/usr/bin/env python3
"""Compare private environment key names with the configuration registry.

This command reads environment variable names only. It never prints values,
loads Backend Settings, queries Kubernetes, or connects to MySQL.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml


KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
ENV_FILE_NAMES = ("backend/.env", "backend/.env_pub", "backend/.env.84")


@dataclass(frozen=True)
class EnvFileReport:
    path: str
    key_count: int
    settings_keys: list[str]
    non_settings_keys: list[str]
    sensitive_keys: list[str]
    unknown_keys: list[str]
    duplicate_keys: list[str]
    registered_not_set: list[str]


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _load_registry(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: registry root must be a mapping")
    return payload


def _registry_keys(registry: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    settings: set[str] = set()
    non_settings: set[str] = set()
    sensitive: set[str] = set()

    for domain in registry.get("settings_domains", []):
        if not isinstance(domain, dict):
            continue
        keys = {str(key) for key in domain.get("keys", [])}
        settings.update(keys)
        if domain.get("sensitive") in (True, "mixed"):
            sensitive.update(keys)

    for domain in registry.get("non_settings_environment_domains", []):
        if not isinstance(domain, dict):
            continue
        keys = {str(key) for key in domain.get("keys", [])}
        non_settings.update(keys)
        if domain.get("sensitive") in (True, "mixed"):
            sensitive.update(keys)

    return settings, non_settings, sensitive


def _read_keys(path: Path) -> tuple[set[str], list[str]]:
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = KEY_RE.match(line)
        if match:
            keys.append(match.group(1))
    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    return set(keys), duplicates


def _file_report(
    root: Path,
    path: Path,
    settings: set[str],
    non_settings: set[str],
    sensitive: set[str],
) -> EnvFileReport:
    keys, duplicates = _read_keys(path)
    registered = settings | non_settings
    return EnvFileReport(
        path=_display_path(root, path),
        key_count=len(keys),
        settings_keys=sorted(keys & settings),
        non_settings_keys=sorted(keys & non_settings),
        sensitive_keys=sorted(keys & sensitive),
        unknown_keys=sorted(keys - registered),
        duplicate_keys=duplicates,
        registered_not_set=sorted(registered - keys),
    )


def build_report(root: Path, env_file: Path, registry_path: Path) -> dict[str, Any]:
    registry = _load_registry(registry_path)
    settings, non_settings, sensitive = _registry_keys(registry)
    if not env_file.is_file():
        return {
            "status": "failed",
            "active_env": _display_path(root, env_file),
            "registered_keys": len(settings | non_settings),
            "active": None,
            "compatibility": [],
            "issues": [{"code": "active-env-missing", "path": _display_path(root, env_file)}],
        }

    active = _file_report(root, env_file, settings, non_settings, sensitive)
    issues: list[dict[str, str]] = []
    for key in active.unknown_keys:
        issues.append(
            {
                "code": "active-env-key-unregistered",
                "path": f"{active.path}:{key}",
            }
        )
    for key in active.duplicate_keys:
        issues.append(
            {
                "code": "active-env-key-duplicated",
                "path": f"{active.path}:{key}",
            }
        )

    compatibility: list[dict[str, Any]] = []
    for relative_path in ENV_FILE_NAMES:
        candidate = root / relative_path
        if candidate == env_file or not candidate.is_file():
            continue
        variant = _file_report(root, candidate, settings, non_settings, sensitive)
        active_keys = set(active.settings_keys + active.non_settings_keys + active.unknown_keys)
        variant_keys = set(variant.settings_keys + variant.non_settings_keys + variant.unknown_keys)
        compatibility.append(
            {
                "path": variant.path,
                "key_count": variant.key_count,
                "added_vs_active": sorted(variant_keys - active_keys),
                "removed_vs_active": sorted(active_keys - variant_keys),
                "unknown_keys": variant.unknown_keys,
                "duplicate_keys": variant.duplicate_keys,
            }
        )

    return {
        "status": "ok" if not issues else "failed",
        "active_env": active.path,
        "registered_keys": len(settings | non_settings),
        "active": asdict(active),
        "compatibility": compatibility,
        "issues": issues,
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"status: {report['status']}")
    print(f"active_env: {report['active_env']}")
    print(f"registered_keys: {report['registered_keys']}")
    active = report["active"]
    if active is None:
        for issue in report["issues"]:
            print(f"- [{issue['code']}] {issue['path']}")
        return
    print(f"active_env_keys: {active['key_count']}")
    print(f"  settings_keys: {len(active['settings_keys'])}")
    print(f"  non_settings_keys: {len(active['non_settings_keys'])}")
    print(f"  sensitive_keys_present: {len(active['sensitive_keys'])}")
    print(f"  unknown_keys: {len(active['unknown_keys'])}")
    print(f"  duplicate_keys: {len(active['duplicate_keys'])}")
    print(f"  registered_not_set: {len(active['registered_not_set'])}")
    for key in active["unknown_keys"]:
        print(f"- [active-env-key-unregistered] {key}")
    for key in active["duplicate_keys"]:
        print(f"- [active-env-key-duplicated] {key}")
    for variant in report["compatibility"]:
        print(f"{variant['path']}:")
        print(f"  key_count: {variant['key_count']}")
        print(f"  added_vs_active: {len(variant['added_vs_active'])}")
        print(f"  removed_vs_active: {len(variant['removed_vs_active'])}")
        print(f"  unknown_keys: {len(variant['unknown_keys'])}")
        print(f"  duplicate_keys: {len(variant['duplicate_keys'])}")
        for key in variant["added_vs_active"]:
            print(f"  + {key}")
        for key in variant["removed_vs_active"]:
            print(f"  - {key}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("docs/architecture/current/configuration/registry.yaml"),
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    root = args.root.resolve()
    env_file = (args.env_file or root / "backend/.env").resolve()
    registry_path = args.registry
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    report = build_report(root, env_file, registry_path.resolve())
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, yaml.YAMLError) as exc:
        print(f"check-env-registry: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
