#!/usr/bin/env python3
"""Verify a target Hermes environment without carrying source-mutation code."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import sys
from typing import Any


CONTRACT_VERSION = 1
HERMES_ROOT = Path("/opt/hermes")
ALLOWED_NODE_PREFIX = PurePosixPath(
    "plugins/platforms/photon/sidecar/node_modules"
)


class UpstreamEnvironmentError(RuntimeError):
    pass


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _normalized_distribution_name(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(".", "-")


def _load_contract(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpstreamEnvironmentError("cannot read upstream environment contract") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != CONTRACT_VERSION
        or payload.get("enabled") is not True
        or not isinstance(payload.get("upstream_version"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(payload.get("upstream_commit") or ""))
        is None
        or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("compatibility_sha256") or ""))
        is None
        or not isinstance(payload.get("target"), dict)
    ):
        raise UpstreamEnvironmentError("invalid upstream environment contract")
    identity = dict(payload)
    actual_digest = identity.pop("contract_sha256", None)
    expected_digest = hashlib.sha256(_canonical_json(identity)).hexdigest()
    if actual_digest != expected_digest:
        raise UpstreamEnvironmentError("upstream environment contract digest mismatch")
    return payload


def _verify_python(target: dict[str, Any]) -> dict[str, str]:
    expected = target.get("python_distributions")
    if not isinstance(expected, dict) or not expected:
        raise UpstreamEnvironmentError("missing target Python distribution contract")
    installed = {
        _normalized_distribution_name(str(distribution.metadata.get("Name") or "")): str(
            distribution.version
        )
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    }
    actual: dict[str, str] = {}
    for raw_name, expected_version in sorted(expected.items()):
        name = _normalized_distribution_name(str(raw_name))
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
            or not isinstance(expected_version, str)
            or installed.get(name) != expected_version
        ):
            raise UpstreamEnvironmentError(
                f"Python distribution {name} is {installed.get(name)!r}, "
                f"expected {expected_version!r}"
            )
        actual[name] = installed[name]
    return actual


def _verify_node(target: dict[str, Any]) -> dict[str, str]:
    node = target.get("node")
    if not isinstance(node, dict):
        raise UpstreamEnvironmentError("missing target Node package contract")
    raw_path = node.get("package_json")
    package_name = node.get("package_name")
    expected_version = node.get("version")
    if not all(isinstance(value, str) and value for value in (raw_path, package_name, expected_version)):
        raise UpstreamEnvironmentError("invalid target Node package contract")
    relative = PurePosixPath(str(raw_path))
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != raw_path
        or ALLOWED_NODE_PREFIX not in relative.parents
    ):
        raise UpstreamEnvironmentError("target Node package path is outside the allowed prefix")
    try:
        document = json.loads(HERMES_ROOT.joinpath(*relative.parts).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpstreamEnvironmentError("cannot read target Node package") from exc
    actual_name = document.get("name") if isinstance(document, dict) else None
    actual_version = document.get("version") if isinstance(document, dict) else None
    if actual_name != package_name or actual_version != expected_version:
        raise UpstreamEnvironmentError(
            f"Node package identity is {actual_name!r}@{actual_version!r}, "
            f"expected {package_name!r}@{expected_version!r}"
        )
    return {"package_name": package_name, "version": expected_version}


def _verify_sqlite(target: dict[str, Any]) -> dict[str, str]:
    sqlite = target.get("sqlite")
    expected_version = sqlite.get("version") if isinstance(sqlite, dict) else None
    if not isinstance(expected_version, str) or sqlite3.sqlite_version != expected_version:
        raise UpstreamEnvironmentError(
            f"linked SQLite is {sqlite3.sqlite_version!r}, expected {expected_version!r}"
        )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')"
        )
        connection.execute("INSERT INTO docs VALUES ('hermes')")
        matched = connection.execute(
            "SELECT count(*) FROM docs WHERE docs MATCH 'erm'"
        ).fetchone()
    finally:
        connection.close()
    if not matched or matched[0] != 1:
        raise UpstreamEnvironmentError("SQLite FTS5 trigram self-test failed")
    return {"version": sqlite3.sqlite_version, "fts5_trigram": "passed"}


def verify(*, contract_path: Path) -> dict[str, Any]:
    contract = _load_contract(contract_path)
    target = contract["target"]
    report: dict[str, Any] = {
        "status": "passed",
        "enabled": True,
        "upstream_version": contract["upstream_version"],
        "upstream_commit": contract["upstream_commit"],
        "compatibility_sha256": contract["compatibility_sha256"],
        "contract_sha256": contract["contract_sha256"],
        "python_distributions": _verify_python(target),
        "node": _verify_node(target),
        "sqlite": _verify_sqlite(target),
    }
    report["report_sha256"] = hashlib.sha256(_canonical_json(report)).hexdigest()
    return report


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()
    report = verify(contract_path=Path(args.contract))
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except UpstreamEnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
