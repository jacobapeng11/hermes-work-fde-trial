from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / "scripts" / "smoke-hermes-raw-mcp-reload.py"
PACKAGE = ROOT / "scripts" / "hermes_raw_mcp_reload_smoke"
BUSINESS_FIXTURE = PACKAGE / "business_mcp_fixture.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_raw_reload_smoke_has_thin_entry_and_bounded_modules() -> None:
    assert len(_source(ENTRY).splitlines()) <= 30
    assert "from hermes_raw_mcp_reload_smoke.cli import main" in _source(ENTRY)
    for path in PACKAGE.glob("*.py"):
        assert len(_source(path).splitlines()) <= 320


def test_active_reload_is_apply_gated_and_uses_exact_cleanup() -> None:
    cli = _source(PACKAGE / "cli.py")
    assert 'result.add_argument("--apply", action="store_true")' in cli
    assert "await edge.cleanup(cleanup_errors)" in cli
    assert "await profiles.cleanup(cleanup_errors)" in cli
    assert "await profiles.verify_cleanup(cleanup_errors)" in cli
    assert "await dashboard.reload_mcp(target.endpoint)" in cli
    assert "await pods.require_test_marker(pod)" in cli


def test_active_reload_uses_stable_mcp_asset_and_profile_desired_projection() -> None:
    source = _source(BUSINESS_FIXTURE)

    assert "AssetCatalogService" in source
    assert "CreateMcpAssetCommand" in source
    assert 'f"/user/experts/{profile_id}/capabilities"' in source
    assert "RuntimeMcpRegistryEntry" in source
    assert "replace_mcp_servers" not in source


def test_smoke_never_reads_or_uses_the_dashboard_token() -> None:
    source = _source(PACKAGE / "raw_dashboard.py")
    assert 'TEST_MARKER = "/opt/hermes-platform/allow-active-mcp-reload-test"' in source
    assert "HERMES_DASHBOARD_SESSION_TOKEN" not in source
    assert "reload.mcp" not in source


def test_experiment_has_one_explicit_supervisor_reload_call() -> None:
    source = _source(PACKAGE / "experiment.py")
    tree = ast.parse(source, filename=str(PACKAGE / "experiment.py"))
    reload_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reload_mcp"
    ]
    assert len(reload_calls) == 1
    assert 'self.store.record_control(\n                "supervisor.reload_mcp"' in source
    assert "for attempt in range(41):" in source
    assert "await asyncio.sleep(0.5)" in source
    assert "if observed_ids <= owned_ids:" in source
    assert "run_id=self.store.checkpoint.run_ids[-1]" in source


def test_active_reload_outcome_classification() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from expert_profile_resume_smoke.contracts import RunEvidence
        from hermes_raw_mcp_reload_smoke.experiment import classify_outcome
    finally:
        sys.path.pop(0)

    run = RunEvidence(label="active", session_id="test")
    run.events.append({"event": "chat.done"})
    run.final_text = "MCP_A_TEST"
    outcome = classify_outcome(
        reload_result={"ok": True, "status": "reloaded"},
        pending_while_hold=False,
        active_run=run,
        same_session_recovery=run,
        fresh_session_recovery=run,
        marker="MCP_A_TEST",
        hold_tool_calls=1,
        active_marker_tool_delta=2,
    )
    assert outcome["code"] == (
        "reload_completed_during_active_tool_and_all_runs_recovered"
    )

    missing = RunEvidence(label="missing", session_id="test")
    missing.events.append({"event": "chat.done"})
    degraded = classify_outcome(
        reload_result={"ok": True, "status": "reloaded"},
        pending_while_hold=False,
        active_run=run,
        same_session_recovery=missing,
        fresh_session_recovery=run,
        marker="MCP_A_TEST",
        hold_tool_calls=1,
        active_marker_tool_delta=2,
    )
    assert degraded["code"] == "same_session_agent_stale_after_reload"
    assert degraded["same_session_recovered"] is False
