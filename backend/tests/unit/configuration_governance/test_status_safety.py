from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("governance_status", ROOT / "scripts/config-governance-status.py")
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def test_startup_gates_do_not_require_candidate_registry():
    gate = (ROOT / "scripts/check-gates.sh").read_text()
    assert "check-config-registry.py" not in gate
    assert "config_governance" not in gate


def test_backend_http_failure_is_not_healthy(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("BACKEND_LOCAL_HOST=127.0.0.1\nBACKEND_PORT=8011\n")

    def unavailable(*args, **kwargs):
        raise HTTPError("http://localhost", 503, "unavailable", {}, None)

    monkeypatch.setattr(status, "urlopen", unavailable)
    assert status._live_backend_report(env)["status"] != "ok"


def test_env_parser_supports_export_comments_and_last_assignment(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BACKEND_PORT=8000\nexport BACKEND_PORT='8011' # local\n")
    assert status._env_value(env, "BACKEND_PORT") == "8011"


def test_pending_pod_is_not_ready(monkeypatch):
    def kube(*args):
        if "pod" in args:
            return {"items": [{"metadata": {"name": "pending"}, "status": {"phase": "Pending"}}]}
        return {"items": []}

    monkeypatch.setattr(status, "_kubectl_json", kube)
    monkeypatch.setattr(status, "secret_metadata", lambda namespace: {"items": []}, raising=False)
    assert status._live_kubernetes_report()["pods"]["ready"] == 0


def test_bundle_alignment_requires_supervisor_digest_match():
    kubernetes = {
        "kservices": {"entries": [{"name": "svc", "ready": "True", "latest_ready_revision": "rev"}]},
        "revisions": {"entries": [{"name": "rev", "ready": "True", "config_identity": {
            "RUNTIME_CONFIG_REVISION": "generation-1", "RUNTIME_INITIAL_CONFIG_DIGEST": "digest-a"
        }}]},
    }
    mysql = {"tables": {"runtime_bindings": [{"runtime_id": "r", "knative_service": "svc", "config_generation": 1}],
                          "runtime_health_receipts": [{"runtime_id": "r", "config_revision": "generation-1", "config_digest": "digest-b"}]}}
    report = status._bundle_alignment_report(kubernetes, mysql)
    assert report["consistent"] == 0
    assert report["needs_manual_review"] == 1
