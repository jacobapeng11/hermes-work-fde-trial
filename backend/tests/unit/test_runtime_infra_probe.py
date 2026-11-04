from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "lib" / "runtime_infra_probe.py"
SPEC = importlib.util.spec_from_file_location("runtime_infra_probe", MODULE_PATH)
assert SPEC and SPEC.loader
runtime_infra_probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_infra_probe)


def test_probe_reports_ready_when_every_check_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = {"status": "ready", "detail": "ok"}
    monkeypatch.setattr(runtime_infra_probe, "_check_swap", lambda _node: ready)
    monkeypatch.setattr(
        runtime_infra_probe,
        "_check_kubernetes_api",
        lambda _kubectl: (ready, True),
    )
    monkeypatch.setattr(
        runtime_infra_probe,
        "_check_node",
        lambda _kubectl, _node: ready,
    )
    monkeypatch.setattr(
        runtime_infra_probe,
        "_check_deployments",
        lambda _kubectl, _namespace, _deployments: ready,
    )
    monkeypatch.setattr(
        runtime_infra_probe,
        "_check_ingress",
        lambda _url: ready,
    )

    payload = runtime_infra_probe.probe()

    assert payload["status"] == "ready"
    assert all(
        check["status"] == "ready" for check in payload["checks"].values()
    )


def test_probe_fails_closed_when_kubernetes_api_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = {"status": "ready", "detail": "ok"}
    unavailable = {"status": "not_ready", "detail": "request timed out"}
    monkeypatch.setattr(runtime_infra_probe, "_check_swap", lambda _node: ready)
    monkeypatch.setattr(
        runtime_infra_probe,
        "_check_kubernetes_api",
        lambda _kubectl: (unavailable, False),
    )
    monkeypatch.setattr(
        runtime_infra_probe,
        "_check_ingress",
        lambda _url: unavailable,
    )

    payload = runtime_infra_probe.probe()

    assert payload["status"] == "not_ready"
    assert payload["checks"]["kubernetes_api"]["status"] == "not_ready"
    assert payload["checks"]["runtime_node"]["status"] == "not_ready"
    assert payload["checks"]["knative_control_plane"]["status"] == "not_ready"
    assert payload["checks"]["kourier"]["status"] == "not_ready"


def test_deployment_check_requires_every_expected_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "kind": "List",
        "items": [
            {
                "metadata": {"name": "controller"},
                "status": {"availableReplicas": 1},
            },
            {
                "metadata": {"name": "webhook"},
                "status": {"availableReplicas": 0},
            },
        ],
    }
    monkeypatch.setattr(
        runtime_infra_probe,
        "_run",
        lambda _args: (True, json.dumps(payload)),
    )

    result = runtime_infra_probe._check_deployments(
        "kubectl",
        "knative-serving",
        ("controller", "webhook"),
    )

    assert result["status"] == "not_ready"
    assert "webhook" in result["detail"]


def test_safe_detail_redacts_common_credentials() -> None:
    detail = runtime_infra_probe._safe_detail(
        "token=abc password=def api_key=ghi secret=jkl"
    )

    assert "abc" not in detail
    assert "def" not in detail
    assert "ghi" not in detail
    assert "jkl" not in detail
