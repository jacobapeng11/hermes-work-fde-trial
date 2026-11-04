#!/usr/bin/env python3
"""Validate the configuration registry against Backend Settings source files.

The checker reads only tracked source files and the registry YAML. It does not
load Backend Settings, read backend/.env, query Kubernetes, or connect to MySQL.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
from typing import Any

import yaml


REQUIRED_SETTINGS_DOMAIN_FIELDS = (
    "id",
    "description",
    "owner",
    "authority",
    "source",
    "keys",
    "environments",
    "apply_action",
    "sensitive",
    "affects_ttft",
)
EXPECTED_ENVIRONMENTS = {"test", "prod"}


@dataclass(frozen=True)
class RegistryIssue:
    code: str
    path: str
    message: str


def _load_registry(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: registry root must be a mapping")
    return payload


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _is_settings_field_name(name: str) -> bool:
    return name.isupper() and not name.startswith("_")


def _extract_settings_fields(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    fields: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Settings"):
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                if _is_settings_field_name(statement.target.id):
                    fields.add(statement.target.id)
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name) and _is_settings_field_name(target.id):
                        fields.add(target.id)
    return fields


def _settings_modules(root: Path, registry: dict[str, Any]) -> dict[str, Path]:
    observed_sources = registry.get("observed_sources")
    if not isinstance(observed_sources, dict):
        raise RuntimeError("observed_sources must be a mapping")
    modules = observed_sources.get("settings_modules")
    if not isinstance(modules, dict):
        raise RuntimeError("observed_sources.settings_modules must be a mapping")
    return {
        str(name): root / str(relative_path)
        for name, relative_path in modules.items()
    }


def _settings_code_fields(root: Path, registry: dict[str, Any]) -> tuple[dict[str, str], list[RegistryIssue]]:
    field_to_module: dict[str, str] = {}
    issues: list[RegistryIssue] = []
    duplicates: dict[str, list[str]] = defaultdict(list)
    for module_name, path in _settings_modules(root, registry).items():
        registry_path = path.relative_to(root).as_posix()
        if not path.exists():
            issues.append(
                RegistryIssue(
                    code="settings-module-missing",
                    path=f"observed_sources.settings_modules.{module_name}",
                    message=f"{registry_path} does not exist",
                )
            )
            continue
        for field_name in sorted(_extract_settings_fields(path)):
            if field_name in field_to_module:
                duplicates[field_name].extend([field_to_module[field_name], registry_path])
            field_to_module[field_name] = registry_path
    for field_name, paths in sorted(duplicates.items()):
        unique_paths = sorted(set(paths))
        issues.append(
            RegistryIssue(
                code="settings-code-key-duplicated",
                path=field_name,
                message=f"declared in multiple Settings modules: {', '.join(unique_paths)}",
            )
        )
    return field_to_module, issues


def _registry_settings_fields(
    root: Path,
    registry: dict[str, Any],
    code_fields: dict[str, str],
) -> tuple[dict[str, str], list[RegistryIssue]]:
    domains = registry.get("settings_domains")
    if not isinstance(domains, list):
        raise RuntimeError("settings_domains must be a list")

    field_to_domain: dict[str, str] = {}
    all_keys: list[str] = []
    issues: list[RegistryIssue] = []
    domain_ids: list[str] = []

    for index, domain in enumerate(domains):
        path = f"settings_domains[{index}]"
        if not isinstance(domain, dict):
            issues.append(
                RegistryIssue(
                    code="settings-domain-invalid",
                    path=path,
                    message="domain entry must be a mapping",
                )
            )
            continue
        domain_id = str(domain.get("id") or f"<missing-{index}>")
        domain_ids.append(domain_id)
        for field_name in REQUIRED_SETTINGS_DOMAIN_FIELDS:
            if field_name not in domain:
                issues.append(
                    RegistryIssue(
                        code="settings-domain-field-missing",
                        path=f"{path}.{field_name}",
                        message=f"{domain_id} is missing required field {field_name}",
                    )
                )
        keys = domain.get("keys")
        if not isinstance(keys, list) or not keys:
            issues.append(
                RegistryIssue(
                    code="settings-domain-keys-invalid",
                    path=f"{path}.keys",
                    message=f"{domain_id} must declare at least one key",
                )
            )
            continue
        source = domain.get("source")
        module_value = source.get("module") if isinstance(source, dict) else None
        module_path = root / str(module_value) if module_value else None
        if module_path is None:
            issues.append(
                RegistryIssue(
                    code="settings-domain-module-missing",
                    path=f"{path}.source.module",
                    message=f"{domain_id} must declare its Settings module",
                )
            )
        elif not module_path.exists():
            issues.append(
                RegistryIssue(
                    code="settings-domain-module-not-found",
                    path=f"{path}.source.module",
                    message=f"{domain_id} points to missing module {module_value}",
                )
            )
        environments = domain.get("environments")
        if set(environments or []) != EXPECTED_ENVIRONMENTS:
            issues.append(
                RegistryIssue(
                    code="settings-domain-environments-invalid",
                    path=f"{path}.environments",
                    message=f"{domain_id} must target exactly test and prod",
                )
            )
        for key in keys:
            key_name = str(key)
            all_keys.append(key_name)
            field_to_domain[key_name] = domain_id
            if module_path is not None and key_name in code_fields:
                actual_module = code_fields[key_name]
                expected_module = module_path.relative_to(root).as_posix()
                if actual_module != expected_module:
                    issues.append(
                        RegistryIssue(
                            code="settings-domain-module-mismatch",
                            path=f"{path}.keys",
                            message=(
                                f"{key_name} belongs to {actual_module}, "
                                f"but {domain_id} declares {expected_module}"
                            ),
                        )
                    )

    for domain_id, count in sorted(Counter(domain_ids).items()):
        if count > 1:
            issues.append(
                RegistryIssue(
                    code="settings-domain-id-duplicated",
                    path=domain_id,
                    message=f"domain id appears {count} times",
                )
            )

    for key_name, count in sorted(Counter(all_keys).items()):
        if count > 1:
            issues.append(
                RegistryIssue(
                    code="settings-registry-key-duplicated",
                    path=key_name,
                    message=f"key appears in {count} settings domains",
                )
            )
    return field_to_domain, issues


def build_report(root: Path, registry_path: Path) -> dict[str, Any]:
    registry = _load_registry(registry_path)
    code_fields, issues = _settings_code_fields(root, registry)
    registry_fields, registry_issues = _registry_settings_fields(
        root,
        registry,
        code_fields,
    )
    issues.extend(registry_issues)

    missing = sorted(set(code_fields) - set(registry_fields))
    stale = sorted(set(registry_fields) - set(code_fields))
    for key_name in missing:
        issues.append(
            RegistryIssue(
                code="settings-registry-key-missing",
                path=key_name,
                message=f"{key_name} exists in Backend Settings but is not registered",
            )
        )
    for key_name in stale:
        issues.append(
            RegistryIssue(
                code="settings-registry-key-stale",
                path=key_name,
                message=f"{key_name} is registered but no longer exists in Backend Settings",
            )
        )

    status = "ok" if not issues else "failed"
    return {
        "status": status,
        "registry_path": _display_path(root, registry_path),
        "settings_code_fields": len(code_fields),
        "settings_registry_fields": len(registry_fields),
        "settings_domains": len(registry.get("settings_domains") or []),
        "issues": [asdict(issue) for issue in sorted(issues, key=lambda item: (item.code, item.path))],
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"status: {report['status']}")
    print(f"registry_path: {report['registry_path']}")
    print(f"settings_code_fields: {report['settings_code_fields']}")
    print(f"settings_registry_fields: {report['settings_registry_fields']}")
    print(f"settings_domains: {report['settings_domains']}")
    print(f"issues: {len(report['issues'])}")
    for issue in report["issues"]:
        print(f"- [{issue['code']}] {issue['path']}: {issue['message']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("docs/architecture/current/configuration/registry.yaml"),
        help="registry YAML path, relative to --root unless absolute",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    root = args.root.resolve()
    registry_path = args.registry
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    report = build_report(root, registry_path.resolve())
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, yaml.YAMLError) as exc:
        print(f"check-config-registry: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
