#!/usr/bin/env python3
"""Report read-only configuration source and live effective identities.

This command reads only configuration source metadata and .env key names. It
does not import Backend Settings, print configuration values, or read Secret
values. Kubernetes/MySQL live metadata is read only when ``--live`` is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import yaml
from dotenv import dotenv_values

from lib.config_governance_live import owned_field_diff, secret_metadata


ENVIRONMENTS = ("test", "prod")
RUNTIME_NAMESPACE = "hermes-runtimes"
KNATIVE_NAMESPACE = "knative-serving"


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _working_tree_dirty(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _render_kustomize(root: Path, environment: str) -> bytes:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl is required for offline Kustomize rendering")
    overlay = root / "ops" / "kubernetes" / "overlays" / environment
    result = subprocess.run(
        [kubectl, "kustomize", str(overlay)],
        check=True,
        capture_output=True,
    )
    rendered = result.stdout
    for document in rendered.decode("utf-8").split("\n---"):
        if "\nkind: Secret\n" in document and (
            "\ndata:\n" in document or "\nstringData:\n" in document
        ):
            raise RuntimeError(
                f"{environment}: rendered Secret contains data and cannot be reported"
            )
    return rendered


def _run_json_checker(
    script: Path,
    root: Path,
    *extra_args: str,
) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(root),
            "--format",
            "json",
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{script.name}: checker returned invalid JSON"
        ) from exc
    if not isinstance(report, dict):
        raise RuntimeError(f"{script.name}: checker returned a non-object report")
    return report


def _kubectl_json(*args: str) -> dict[str, Any]:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl is required for live configuration reporting")
    result = subprocess.run(
        [kubectl, "--request-timeout=10s", *args, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("kubectl returned a non-object JSON payload")
    return payload


def _condition_status(item: dict[str, Any], condition_type: str) -> str:
    for condition in item.get("status", {}).get("conditions", []) or []:
        if condition.get("type") == condition_type:
            return str(condition.get("status", "Unknown"))
    return "Unknown"


def _live_kubernetes_report() -> dict[str, Any]:
    configmaps = _kubectl_json(
        "get", "configmap", "-n", KNATIVE_NAMESPACE,
        "config-defaults", "config-features", "config-deployment",
    )
    services = _kubectl_json("get", "kservice", "-n", RUNTIME_NAMESPACE)
    revisions = _kubectl_json("get", "revision", "-n", RUNTIME_NAMESPACE)
    pods = _kubectl_json("get", "pod", "-n", RUNTIME_NAMESPACE)
    secrets = secret_metadata(RUNTIME_NAMESPACE)

    cm_entries = []
    for item in configmaps.get("items", []):
        data = item.get("data") or {}
        cm_entries.append({
            "name": item.get("metadata", {}).get("name"),
            "resource_version": item.get("metadata", {}).get("resourceVersion"),
            "generation": item.get("metadata", {}).get("generation"),
            "data_keys": sorted(data),
            "data": data,
        })

    service_entries = []
    for item in services.get("items", []):
        metadata = item.get("metadata", {})
        service_entries.append({
            "name": metadata.get("name"),
            "generation": metadata.get("generation"),
            "observed_generation": item.get("status", {}).get("observedGeneration"),
            "ready": _condition_status(item, "Ready"),
            "latest_ready_revision": item.get("status", {}).get("latestReadyRevisionName"),
        })

    revision_entries = []
    for item in revisions.get("items", []):
        metadata = item.get("metadata", {})
        containers = item.get("spec", {}).get("containers", []) or []
        images = sorted({str(container.get("image")) for container in containers if container.get("image")})
        config_identity = {}
        for container in containers:
            for env in container.get("env", []) or []:
                name = env.get("name")
                if name in {
                    "RUNTIME_CONFIG_REVISION",
                    "RUNTIME_INITIAL_CONFIG_DIGEST",
                    "HERMES_KNATIVE_REVISION_NAME",
                }:
                    config_identity[name] = env.get("value") or "<ref>"
        revision_entries.append({
            "name": metadata.get("name"),
            "generation": metadata.get("generation"),
            "ready": _condition_status(item, "Ready"),
            "image_references": images,
            "config_identity": config_identity,
        })

    pod_entries = []
    for item in pods.get("items", []):
        metadata = item.get("metadata", {})
        statuses = item.get("status", {}).get("containerStatuses", []) or []
        pod_entries.append({
            "name": metadata.get("name"),
            "phase": item.get("status", {}).get("phase"),
            "ready": bool(statuses) and _condition_status(item, "Ready") == "True"
            and not metadata.get("deletionTimestamp"),
            "revision": metadata.get("labels", {}).get("serving.knative.dev/revision"),
            "service": metadata.get("labels", {}).get("serving.knative.dev/service"),
            "container_count": len(statuses),
            "image_id_count": len({status.get("imageID") for status in statuses if status.get("imageID")}),
        })

    secret_entries = []
    for item in secrets.get("items", []):
        metadata = item.get("metadata", {})
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        secret_entries.append({
            "name": metadata.get("name"),
            "resource_version": metadata.get("resourceVersion"),
            "generation": metadata.get("generation"),
            "key_version": labels.get("hermes.platform/control-key-version"),
            "metadata_version": annotations.get("hermes.platform/secret-version"),
        })

    return {
        "status": "ok",
        "configmaps": cm_entries,
        "kservices": {
            "total": len(service_entries),
            "ready": sum(entry["ready"] == "True" for entry in service_entries),
            "entries": service_entries,
        },
        "revisions": {
            "total": len(revision_entries),
            "ready": sum(entry["ready"] == "True" for entry in revision_entries),
            "entries": revision_entries,
        },
        "pods": {
            "total": len(pod_entries),
            "running": sum(entry["phase"] == "Running" for entry in pod_entries),
            "ready": sum(entry["ready"] for entry in pod_entries),
            "entries": pod_entries,
        },
        "secrets": {
            "total": len(secret_entries),
            "entries": secret_entries,
            "values_read": False,
        },
    }


def _live_mysql_report(root: Path) -> dict[str, Any]:
    query = r'''
import asyncio, json
from sqlalchemy import text
from app.database import AsyncSessionLocal

QUERIES = {
    "active_profiles": """
      SELECT COUNT(*) total,
             COALESCE(SUM(desired_revision = applied_revision), 0) equal_count,
             COALESCE(SUM(desired_revision <> applied_revision), 0) drift_count,
             COALESCE(MAX(desired_revision), 0) max_desired,
             COALESCE(MAX(applied_revision), 0) max_applied
      FROM runtime_agent_profile WHERE status = 'active'
    """,
    "mcp_registry": """
      SELECT COUNT(*) total,
             COALESCE(SUM(desired_revision = applied_revision), 0) equal_count,
             COALESCE(SUM(desired_revision <> applied_revision), 0) drift_count,
             COALESCE(MAX(desired_revision), 0) max_desired,
             COALESCE(MAX(applied_revision), 0) max_applied
      FROM runtime_mcp_registry_state
    """,
    "application_agents": """
      SELECT COUNT(*) total,
             COALESCE(SUM(config_generation = COALESCE(applied_generation, 0)), 0) equal_count,
             COALESCE(SUM(config_generation <> COALESCE(applied_generation, 0)), 0) drift_count,
             COALESCE(SUM(apply_status = 'applied'), 0) applied_count,
             COALESCE(MAX(config_generation), 0) max_desired,
             COALESCE(MAX(applied_generation), 0) max_applied
      FROM application_agent WHERE lifecycle_status = 'active'
    """,
    "runtime_bindings": """
      SELECT id binding_id, runtime_id, knative_service, status, config_generation,
             config_hash, image_digest, runtime_config_source, 'personal' binding_type
      FROM hermes_runtime_binding
      WHERE status NOT IN ('deleted', 'deleting')
      UNION ALL
      SELECT id binding_id, runtime_id, knative_service, status, config_generation,
             NULL config_hash, image_digest, 'application' runtime_config_source,
             'application' binding_type
      FROM application_agent_runtime_binding
      WHERE status NOT IN ('absent', 'stopped')
      ORDER BY runtime_id
    """,
    "profile_drifts": """
      SELECT id, runtime_workspace_id, desired_revision, applied_revision, status
      FROM runtime_agent_profile
      WHERE status = 'active' AND desired_revision <> applied_revision
      ORDER BY id
    """,
}

async def main():
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        result = {}
        for name, sql in QUERIES.items():
            rows = (await session.execute(text(sql))).mappings().all()
            result[name] = (
                [dict(row) for row in rows]
                if name in {"runtime_bindings", "profile_drifts"}
                else dict(rows[0])
            )
        result["runtime_health_receipts"] = []
        for binding in result["runtime_bindings"]:
            receipt = (await session.execute(text("""
                SELECT /*+ MAX_EXECUTION_TIME(3000) */ runtime_id, binding_id,
                       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.config_revision')) config_revision,
                       JSON_UNQUOTE(JSON_EXTRACT(payload, '$.config_digest')) config_digest,
                       occurred_at
                FROM runtime_projection_receipt
                WHERE runtime_id = :runtime_id AND binding_id = :binding_id
                  AND projection_type = 'runtime.health'
                ORDER BY occurred_at DESC, id DESC LIMIT 1
            """), {"runtime_id": binding["runtime_id"], "binding_id": str(binding["binding_id"])})).mappings().first()
            if receipt:
                result["runtime_health_receipts"].append(dict(receipt))
        await session.rollback()
        print(json.dumps(result, default=str, sort_keys=True))

asyncio.run(main())
'''
    backend = root / "backend"
    result = subprocess.run(
        [str(backend / ".venv/bin/python"), "-c", query],
        cwd=backend,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    payload = json.loads(result.stdout)
    return {"status": "ok", "tables": payload}


def _live_backend_report(env_file: Path) -> dict[str, Any]:
    host = _env_value(env_file, "BACKEND_LOCAL_HOST") or "127.0.0.1"
    port = _env_value(env_file, "BACKEND_PORT") or "8011"
    checks = {}
    for path in ("/health", "/api/v1/health", "/api/v1/readyz"):
        url = f"http://{host}:{port}{path}"
        try:
            with urlopen(url, timeout=3) as response:
                checks[path] = {
                    "reachable": True,
                    "http_status": response.status,
                }
        except HTTPError as exc:
            checks[path] = {
                "reachable": True,
                "http_status": exc.code,
            }
        except (OSError, URLError, ValueError) as exc:
            checks[path] = {
                "reachable": False,
                "http_status": None,
                "error_type": type(exc).__name__,
            }
    return {
        "status": "ok" if all(item["http_status"] == 200 for item in checks.values()) else "unavailable",
        "host_source": "backend/.env:BACKEND_LOCAL_HOST",
        "port_source": "backend/.env:BACKEND_PORT",
        "checks": checks,
    }


def _env_value(env_file: Path, key: str) -> str | None:
    if not env_file.exists():
        return None
    return dotenv_values(env_file, interpolate=False).get(key)


def _offline_cluster_fields(root: Path, env_file: Path) -> dict[str, str | None]:
    patch_path = root / "deploy" / "kubernate" / "knative-serving-config-deployment-patch.yaml"
    patch = yaml.safe_load(patch_path.read_text(encoding="utf-8")) or {}
    patch_data = patch.get("data") or {}
    return {
        "config-deployment/queue-sidecar-cpu-request": patch_data.get(
            "queue-sidecar-cpu-request"
        ),
        "config-defaults/max-revision-timeout-seconds": _env_value(
            env_file, "HERMES_RUNTIME_LONG_REQUEST_TIMEOUT_SECONDS"
        ),
    }


def _cluster_field_drift_report(
    root: Path,
    env_file: Path,
    kubernetes: dict[str, Any],
) -> dict[str, Any]:
    desired = _offline_cluster_fields(root, env_file)
    live_by_name = {
        entry["name"]: entry.get("data", {})
        for entry in kubernetes.get("configmaps", [])
    }
    observed = {
        "config-deployment/queue-sidecar-cpu-request": live_by_name.get(
            "config-deployment", {}
        ).get("queue-sidecar-cpu-request"),
        "config-defaults/max-revision-timeout-seconds": live_by_name.get(
            "config-defaults", {}
        ).get("max-revision-timeout-seconds"),
    }
    entries = []
    for field, expected in desired.items():
        actual = observed[field]
        comparable = expected is not None and actual is not None
        entries.append({
            "field": field,
            "source": (
                "deploy/kubernate/knative-serving-config-deployment-patch.yaml"
                if field.startswith("config-deployment/")
                else "backend/.env + scripts/configure-knative-long-runs.sh"
            ),
            "desired_present": expected is not None,
            "observed_present": actual is not None,
            "consistent": comparable and str(expected) == str(actual),
            "needs_manual_review": not comparable or str(expected) != str(actual),
        })
    return {
        "status": "compared",
        "total": len(entries),
        "consistent": sum(entry["consistent"] for entry in entries),
        "needs_manual_review": sum(entry["needs_manual_review"] for entry in entries),
        "entries": entries,
        "uncompared_scope": "other Kustomize fields and Knative feature patches",
    }


def _kustomize_resource_report(root: Path, environment: str) -> dict[str, Any]:
    rendered = _render_kustomize(root, environment).decode("utf-8")
    desired = [item for item in yaml.safe_load_all(rendered) if isinstance(item, dict)]
    results = []
    resource_names = {
        "ServiceAccount": "serviceaccount",
        "Role": "role",
        "ClusterRole": "clusterrole",
        "RoleBinding": "rolebinding",
        "ClusterRoleBinding": "clusterrolebinding",
    }
    for item in desired:
        kind = item.get("kind")
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        if kind == "Secret":
            results.append({"kind": kind, "name": name, "status": "metadata-only"})
            continue
        resource = resource_names.get(kind)
        if not resource or not name:
            results.append({"kind": kind, "name": name, "status": "uncompared"})
            continue
        args = ["get", resource, str(name)]
        namespace = metadata.get("namespace")
        if namespace:
            args.extend(["-n", str(namespace)])
        try:
            live = _kubectl_json(*args)
            compare = {"metadata": {"name": name}}
            for key in ("namespace", "labels"):
                if key in metadata:
                    compare["metadata"][key] = metadata[key]
            if kind in {"Role", "ClusterRole"}:
                compare["rules"] = item.get("rules", [])
            elif kind in {"RoleBinding", "ClusterRoleBinding"}:
                compare["roleRef"] = item.get("roleRef", {})
                compare["subjects"] = item.get("subjects", [])
            elif kind == "ServiceAccount":
                compare["automountServiceAccountToken"] = item.get("automountServiceAccountToken")
            differences = owned_field_diff(compare, live)
            results.append({"kind": kind, "name": name, "status": "consistent" if not differences else "drift", "difference_count": len(differences)})
        except (RuntimeError, subprocess.CalledProcessError):
            results.append({"kind": kind, "name": name, "status": "missing"})
    compared = [item for item in results if item["status"] in {"consistent", "drift", "missing"}]
    return {
        "status": "compared",
        "total": len(compared),
        "consistent": sum(item["status"] == "consistent" for item in compared),
        "needs_manual_review": sum(item["status"] != "consistent" for item in compared),
        "entries": results,
        "metadata_only": sum(item["status"] == "metadata-only" for item in results),
        "secret_policy": "metadata-only; Secret values are not compared",
    }


def _bundle_alignment_report(
    kubernetes: dict[str, Any],
    mysql: dict[str, Any],
) -> dict[str, Any]:
    service_by_name = {
        entry["name"]: entry for entry in kubernetes["kservices"]["entries"]
    }
    revision_by_name = {
        entry["name"]: entry for entry in kubernetes["revisions"]["entries"]
    }
    health_by_runtime = {
        entry["runtime_id"]: entry
        for entry in mysql["tables"].get("runtime_health_receipts", [])
    }
    rows = []
    for binding in mysql["tables"].get("runtime_bindings", []):
        service = service_by_name.get(binding["knative_service"])
        revision = revision_by_name.get(
            service.get("latest_ready_revision") if service else None
        )
        identity = (revision or {}).get("config_identity", {})
        expected_revision = f"generation-{binding['config_generation']}"
        observed_revision = identity.get("RUNTIME_CONFIG_REVISION")
        health_receipt = health_by_runtime.get(binding["runtime_id"])
        receipt_revision = (health_receipt or {}).get("config_revision")
        observed_digest = identity.get("RUNTIME_INITIAL_CONFIG_DIGEST")
        receipt_digest = (health_receipt or {}).get("config_digest")
        digest_consistent = bool(observed_digest and receipt_digest and observed_digest == receipt_digest)
        rows.append({
            "runtime_id": binding["runtime_id"],
            "knative_service": binding["knative_service"],
            "desired_generation": binding["config_generation"],
            "observed_revision": observed_revision,
            "observed_digest_present": bool(observed_digest),
            "digest_consistent": digest_consistent,
            "health_receipt_present": health_receipt is not None,
            "health_receipt_revision": receipt_revision,
            "health_receipt_digest_present": bool(
                (health_receipt or {}).get("config_digest")
            ),
            "service_ready": bool(service and service.get("ready") == "True"),
            "revision_ready": bool(revision and revision.get("ready") == "True"),
            "consistent": (
                bool(service and service.get("ready") == "True")
                and bool(revision and revision.get("ready") == "True")
                and observed_revision == expected_revision
                and receipt_revision == expected_revision
                and digest_consistent
            ),
        })
    return {
        "total": len(rows),
        "consistent": sum(row["consistent"] for row in rows),
        "needs_manual_review": sum(not row["consistent"] for row in rows),
        "entries": rows,
        "health_receipts": {
            "total": len(health_by_runtime),
            "revision_matched": sum(
                entry.get("health_receipt_revision") == f"generation-{entry['desired_generation']}"
                for entry in rows
            ),
        },
    }


def _configuration_checks(root: Path, env_file: Path) -> dict[str, Any]:
    config_registry = _run_json_checker(
        root / "scripts" / "check-config-registry.py",
        root,
    )
    checks: dict[str, Any] = {
        "config_registry": {
            "status": config_registry.get("status", "unknown"),
            "settings_code_fields": config_registry.get("settings_code_fields"),
            "settings_registry_fields": config_registry.get(
                "settings_registry_fields"
            ),
            "issues": len(config_registry.get("issues", [])),
        },
        "active_env": {
            "path": env_file.relative_to(root).as_posix()
            if env_file.is_relative_to(root)
            else str(env_file),
        },
    }
    if not env_file.exists():
        checks["active_env"].update(
            {
                "status": "not_available",
                "reason": "env-file-missing",
            }
        )
        return checks

    env_registry = _run_json_checker(
        root / "scripts" / "check-env-registry.py",
        root,
        "--env-file",
        str(env_file),
    )
    active = env_registry.get("active", {})
    checks["active_env"].update(
        {
            "status": env_registry.get("status", "unknown"),
            "key_count": active.get("key_count"),
            "unknown_keys": len(active.get("unknown_keys", [])),
            "duplicate_keys": len(active.get("duplicate_keys", [])),
        }
    )
    return checks


def build_report(
    root: Path,
    environments: tuple[str, ...],
    *,
    env_file: Path | None = None,
    live: bool = False,
) -> dict[str, Any]:
    active_env = (env_file or (root / "backend" / ".env")).resolve()
    report: dict[str, Any] = {
        "status": "live-metadata-compared" if live else "cluster-infra-source-only",
        "repository_revision": _git_revision(root),
        "working_tree_dirty": _working_tree_dirty(root),
        "live_state": {
            "kubernetes_queried": False,
            "backend_queried": False,
            "backend_env_read": active_env.exists(),
            "mysql_queried": False,
            "secret_values_read": False,
        },
        "configuration_checks": _configuration_checks(root, active_env),
        "environments": {},
    }
    for environment in environments:
        rendered = _render_kustomize(root, environment)
        report["environments"][environment] = {
            "source_path": (
                root / "ops" / "kubernetes" / "overlays" / environment
            ).relative_to(root).as_posix(),
            "rendered_manifest_digest": _sha256_bytes(rendered),
            "effective_strategy": "offline-render-only",
            "drift_status": "live-state-not-compared",
        }
    if live:
        report["live_state"].update({
            "kubernetes_queried": True,
            "backend_queried": True,
            "mysql_queried": True,
        })
        report["live_backend"] = _live_backend_report(active_env)
        report["live_kubernetes"] = _live_kubernetes_report()
        report["live_mysql"] = _live_mysql_report(root)
        report["config_bundle_alignment"] = _bundle_alignment_report(
            report["live_kubernetes"], report["live_mysql"]
        )
        report["cluster_field_drift"] = _cluster_field_drift_report(
            root, active_env, report["live_kubernetes"]
        )
        report["kustomize_resource_drift"] = {
            environment: _kustomize_resource_report(root, environment)
            for environment in environments
        }
    return report


def _print_text(report: dict[str, Any]) -> None:
    print(f"status: {report['status']}")
    print(f"repository_revision: {report['repository_revision']}")
    print(f"working_tree_dirty: {report['working_tree_dirty']}")
    live_state = report["live_state"]
    print(
        "live_state: "
        f"kubernetes_queried={live_state['kubernetes_queried']} "
        f"backend_queried={live_state['backend_queried']} "
        f"backend_env_read={live_state['backend_env_read']} "
        f"mysql_queried={live_state['mysql_queried']} "
        f"secret_values_read={live_state['secret_values_read']}"
    )
    checks = report["configuration_checks"]
    registry = checks["config_registry"]
    env = checks["active_env"]
    print("configuration_checks:")
    print(
        "  config_registry: "
        f"status={registry['status']} "
        f"code_fields={registry['settings_code_fields']} "
        f"registry_fields={registry['settings_registry_fields']} "
        f"issues={registry['issues']}"
    )
    print(
        "  active_env: "
        f"path={env['path']} "
        f"status={env['status']}"
        + (
            f" key_count={env['key_count']} "
            f"unknown_keys={env['unknown_keys']} "
            f"duplicate_keys={env['duplicate_keys']}"
            if env["status"] != "not_available"
            else f" reason={env['reason']}"
        )
    )
    for environment, entry in report["environments"].items():
        print(f"{environment}:")
        print(f"  source_path: {entry['source_path']}")
        print(f"  rendered_manifest_digest: {entry['rendered_manifest_digest']}")
        print(f"  effective_strategy: {entry['effective_strategy']}")
        print(f"  drift_status: {entry['drift_status']}")
    if "live_kubernetes" in report:
        backend = report["live_backend"]
        print("live_backend:")
        print(f"  status: {backend['status']}")
        for path, check in backend["checks"].items():
            print(
                f"  {path}: reachable={check['reachable']} "
                f"http_status={check['http_status']}"
            )
        live = report["live_kubernetes"]
        print("live_kubernetes:")
        print(f"  configmaps: {len(live['configmaps'])}")
        print(f"  kservices: {live['kservices']['ready']}/{live['kservices']['total']} ready")
        print(f"  revisions: {live['revisions']['ready']}/{live['revisions']['total']} ready")
        print(f"  pods: {live['pods']['ready']}/{live['pods']['total']} ready")
        print(f"  secrets: {live['secrets']['total']} metadata-only")
        mysql = report["live_mysql"]
        print("live_mysql:")
        for name, table in mysql["tables"].items():
            if name in {"runtime_bindings", "profile_drifts", "runtime_health_receipts"}:
                continue
            print(
                f"  {name}: total={table['total']} "
                f"equal={table['equal_count']} drift={table['drift_count']}"
            )
        alignment = report["config_bundle_alignment"]
        print("config_bundle_alignment:")
        print(
            f"  consistent={alignment['consistent']}/{alignment['total']} "
            f"needs_manual_review={alignment['needs_manual_review']}"
        )
        receipts = alignment["health_receipts"]
        print(
            "  supervisor_health_receipts: "
            f"present={receipts['total']} "
            f"revision_matched={receipts['revision_matched']}/{alignment['total']}"
        )
        drift = report["cluster_field_drift"]
        print("cluster_field_drift:")
        print(
            f"  compared={drift['consistent']}/{drift['total']} "
            f"needs_manual_review={drift['needs_manual_review']} "
            f"uncompared_scope={drift['uncompared_scope']}"
        )
        for environment, resource_drift in report["kustomize_resource_drift"].items():
            print(f"kustomize_resource_drift[{environment}]:")
            print(
                f"  consistent={resource_drift['consistent']}/{resource_drift['total']} "
                f"needs_manual_review={resource_drift['needs_manual_review']} "
                f"metadata_only={resource_drift['metadata_only']}"
            )
        profile_drifts = mysql["tables"].get("profile_drifts", [])
        print(f"profile_drift_entries: {len(profile_drifts)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="read live Kubernetes and MySQL metadata without modifying resources",
    )
    parser.add_argument(
        "--environment",
        choices=ENVIRONMENTS,
        action="append",
        dest="environments",
        help="environment to inspect; defaults to both",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="output_format",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="active Backend .env path; defaults to backend/.env",
    )
    args = parser.parse_args()
    report = build_report(
        args.root.resolve(),
        tuple(args.environments or ENVIRONMENTS),
        env_file=args.env_file,
        live=args.live,
    )
    if args.output_format == "json":
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"config-governance-status: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
