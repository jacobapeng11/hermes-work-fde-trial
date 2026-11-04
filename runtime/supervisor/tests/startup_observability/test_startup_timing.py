from __future__ import annotations

import json
import ast
import os
from pathlib import Path

import pytest

from platform_supervisor import startup_timing
from platform_supervisor.main import verify_sqlite_before_admission
from platform_supervisor.sqlite_startup_integrity import SQLiteStartupIntegrityCache


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
STEADY_PHASES = [
    "app_create_begin",
    "app_created",
    "lifespan_begin",
    "storage_ready",
    "sqlite_store_started",
    "projection_state_started",
    "mcp_state_started",
    "config_controller_started",
    "supervisor_ready",
    "runs_restored",
    "model_relay_started",
    "hermes_process_started",
    "dashboard_listening",
    "hermes_health_checked",
    "hermes_ready",
    "lifespan_startup_complete",
]


class FailedSqliteStore:
    async def quick_check(self) -> bool:
        return False


def test_marks_keep_first_observation_and_report_deltas() -> None:
    startup_timing.reset_for_tests()
    startup_timing.mark("app_create_begin")
    startup_timing.mark("app_created")
    startup_timing.mark("app_created")

    summary = startup_timing.summary(mode="steady_state")
    assert summary["event"] == "supervisor_startup_phases"
    assert summary["startup_mode"] == "steady_state"
    assert summary["phase_count"] == 2
    assert list(summary["phases"]) == ["app_create_begin", "app_created"]
    for phase in summary["phases"].values():
        assert set(phase) == {"since_previous_ms", "since_process_start_ms"}


def test_phase_names_must_be_stable_identifiers() -> None:
    startup_timing.reset_for_tests()
    with pytest.raises(ValueError):
        startup_timing.mark("bad phase/path")
    assert startup_timing.summary(mode="fresh_probe")["phase_count"] == 0


def test_log_summary_emits_secret_free_json(capsys) -> None:
    startup_timing.reset_for_tests()
    startup_timing.mark("app_created")
    startup_timing.mark("hermes_ready")

    startup_timing.log_summary(mode="steady_state")

    output = capsys.readouterr().out
    payload = json.loads(output.strip().splitlines()[-1])
    assert payload["startup_mode"] == "steady_state"
    assert payload["phases"]["hermes_ready"]["since_process_start_ms"] >= 0
    assert "password" not in output.lower()


def test_integrity_receipt_requires_check_then_clean_shutdown(tmp_path) -> None:
    database_path = tmp_path / "supervisor.db"
    receipt_path = tmp_path / "supervisor.sqlite-integrity.json"
    database_path.write_bytes(bytes(100))
    cache = SQLiteStartupIntegrityCache(database_path, receipt_path)

    assert cache.can_reuse() is False
    cache.record_passed()
    assert cache.can_reuse() is False
    cache.record_clean_shutdown()
    assert cache.can_reuse() is True


def test_integrity_receipt_rejects_changed_database(tmp_path) -> None:
    database_path = tmp_path / "supervisor.db"
    receipt_path = tmp_path / "supervisor.sqlite-integrity.json"
    database_path.write_bytes(bytes(100))
    cache = SQLiteStartupIntegrityCache(database_path, receipt_path)
    cache.record_passed()
    cache.record_clean_shutdown()
    database_path.write_bytes(bytes([1] * 100))
    os.utime(database_path, ns=(0, 1))

    assert cache.can_reuse() is False


def test_integrity_receipt_rejects_invalid_json(tmp_path) -> None:
    database_path = tmp_path / "supervisor.db"
    receipt_path = tmp_path / "supervisor.sqlite-integrity.json"
    database_path.write_bytes(bytes(100))
    receipt_path.write_text("{invalid", encoding="utf-8")
    cache = SQLiteStartupIntegrityCache(database_path, receipt_path)

    assert cache.can_reuse() is False


async def test_failed_quick_check_does_not_write_passed_receipt(tmp_path) -> None:
    database_path = tmp_path / "supervisor.db"
    receipt_path = tmp_path / "supervisor.sqlite-integrity.json"
    database_path.write_bytes(bytes(100))
    cache = SQLiteStartupIntegrityCache(database_path, receipt_path)
    startup_timing.reset_for_tests()

    passed = await verify_sqlite_before_admission(FailedSqliteStore(), cache)

    assert passed is False
    assert receipt_path.exists() is False
    assert list(startup_timing.phases()["phases"]) == [
        "sqlite_quick_check_started",
        "sqlite_quick_check_checked",
    ]


def test_main_lifespan_marks_steady_startup_contract() -> None:
    source = (REPOSITORY_ROOT / "runtime/supervisor/platform_supervisor/main.py").read_text(
        encoding="utf-8"
    )

    # create_app defines lifespan before returning, so app_created appears
    # later in source than the nested lifespan body even though it runs first.
    assert source.index('mark("app_create_begin")') < source.index('mark("lifespan_begin")')
    lifespan_source = source[source.index("async def lifespan") :]
    helper_source = source[: source.index("async def lifespan")]
    lifecycle_phases = [
        "lifespan_begin",
        "storage_ready",
        "sqlite_store_started",
        "projection_state_started",
        "mcp_state_started",
        "config_controller_started",
        "supervisor_ready",
        "runs_restored",
        "model_relay_started",
        "hermes_health_checked",
        "hermes_ready",
        "lifespan_startup_complete",
    ]
    helper_positions = [
        helper_source.index(f'mark("{phase}")')
        for phase in ("hermes_process_started", "dashboard_listening")
    ]
    positions = [lifespan_source.index(f'mark("{phase}")') for phase in lifecycle_phases]
    assert positions == sorted(positions)
    assert helper_positions == sorted(helper_positions)
    log_position = lifespan_source.index("log_summary(mode=settings.HERMES_STARTUP_MODE)")
    assert log_position > positions[-1]
    assert lifespan_source.index("health.mark_hermes_ready()") < lifespan_source.index(
        'mark("hermes_ready")'
    )
    assert lifespan_source.index("sessions.start_background_restore(") < log_position
    assert lifespan_source.index("model_relay.start_transport_warmup()") < lifespan_source.index(
        "await hermes_adapter.health()"
    )


def test_steady_state_starts_serving_after_hermes_health_gate() -> None:
    source = (REPOSITORY_ROOT / "runtime/supervisor/platform_supervisor/main.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    lifespan = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    health_gate_line = source[: source.index("await hermes_adapter.health()")].count(
        "\n"
    ) + 1
    steady_yields = [
        node
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Yield) and node.lineno > health_gate_line
    ]
    assert len(steady_yields) == 1
    assert steady_yields[0].lineno > health_gate_line


def test_main_lifespan_yield_is_reachable() -> None:
    source = (REPOSITORY_ROOT / "runtime/supervisor/platform_supervisor/main.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    lifespan = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(lifespan):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    yield_nodes = [node for node in ast.walk(lifespan) if isinstance(node, ast.Yield)]

    assert yield_nodes
    for yield_node in yield_nodes:
        current: ast.AST | None = parents.get(id(yield_node))
        while current is not None:
            assert not isinstance(current, ast.ExceptHandler)
            current = parents.get(id(current))


def test_main_lifespan_startup_check_is_call_result() -> None:
    source = (REPOSITORY_ROOT / "runtime/supervisor/platform_supervisor/main.py").read_text(
        encoding="utf-8"
    )
    module = ast.parse(source)
    lifespan = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    assignments = [
        node
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "startup_check" for target in node.targets
        )
    ]

    assert assignments
    assert all(isinstance(node.value, ast.Call) for node in assignments)
