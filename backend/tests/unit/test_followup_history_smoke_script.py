from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "smoke_followup_history.py"
SPEC = importlib.util.spec_from_file_location("smoke_followup_history", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_websocket_url_uses_explicit_ip_base() -> None:
    assert MODULE.websocket_url(
        "http://192.0.2.15",
        "/runtime-edge/runtime-1/ws",
    ) == "ws://192.0.2.15/runtime-edge/runtime-1/ws"


@pytest.mark.asyncio
async def test_handshake_receipt_has_stage_and_status_without_response_material(
    monkeypatch,
) -> None:
    response = type("Response", (), {"status_code": 503})()
    error = MODULE.InvalidStatus(response)

    class RejectedConnection:
        async def __aenter__(self):
            raise error

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        MODULE.websockets,
        "connect",
        lambda *_args, **_kwargs: RejectedConnection(),
    )
    target = MODULE.Target(
        kind="personal",
        principal=MODULE.Principal(user_id=2, org_id=1),
        runtime_workspace_id=119,
        application_agent_id=None,
        binding_id=9,
        runtime_id="rt-119",
    )
    bootstrap = MODULE.SessionBootstrap(
        target=target,
        runtime_session_id="rs-safe",
        model_entry_id=1,
        runtime_id="rt-119",
        websocket_path="/runtime-edge/runtime-1/ws",
        websocket_protocol="runtime-edge.v1",
        runtime_access="x" * 32,
        session_grant="y" * 32,
    )

    with pytest.raises(
        MODULE.SmokeError,
        match="history reconnect handshake returned HTTP 503",
    ) as caught:
        async with MODULE.connect_runtime_edge(
            bootstrap,
            runtime_edge_base="http://192.0.2.15",
            timeout_seconds=10,
            stage="history reconnect",
        ):
            pass
    assert "runtime-access" not in str(caught.value)
    assert "x" * 32 not in str(caught.value)


def test_followup_history_requires_exact_identity_order_and_hides_markers() -> None:
    followups = [("correction-1", "A"), ("correction-2", "A")]
    MODULE.assert_followup_history(
        [
            {"role": "user", "content": "A", "client_message_id": "correction-1"},
            {"role": "user", "content": "A", "client_message_id": "correction-2"},
        ],
        followups,
    )

    with pytest.raises(MODULE.SmokeError, match="lost or duplicated"):
        MODULE.assert_followup_history(
            [{"role": "user", "content": "A", "client_message_id": "correction-1"}],
            followups,
        )


def test_followup_history_rejects_internal_transport_markers() -> None:
    with pytest.raises(MODULE.SmokeError, match="internal aggregate marker"):
        MODULE.assert_followup_history(
            [
                {
                    "role": "user",
                    "content": "[Additional user correction]",
                    "client_message_id": "correction-1",
                }
            ],
            [("correction-1", "[Additional user correction]")],
        )


@pytest.mark.asyncio
async def test_cleanup_waits_for_the_exact_session_runs_to_finish(monkeypatch) -> None:
    observations = iter((True, True, False))
    sleeps: list[float] = []

    async def active_runs(_target, _runtime_session_id):
        return next(observations)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(MODULE, "_session_has_active_runs", active_runs)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    await MODULE.wait_for_cleanup_safe_session(
        MODULE.Target(
            kind="personal",
            principal=MODULE.Principal(user_id=2, org_id=1),
            runtime_workspace_id=119,
            application_agent_id=None,
            binding_id=9,
            runtime_id="rt-119",
        ),
        "rs-temporary",
        timeout_seconds=1,
    )

    assert sleeps == [0.5, 0.5]


@pytest.mark.asyncio
async def test_reconnect_prepare_issues_fresh_runtime_access() -> None:
    target = MODULE.Target(
        kind="personal",
        principal=MODULE.Principal(user_id=2, org_id=1),
        runtime_workspace_id=119,
        application_agent_id=None,
        binding_id=9,
        runtime_id="rt-119",
    )
    api = MODULE.BackendSessionApi(target)

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, _path, *, json):
            self.calls += 1
            assert json["runtime_session_id"] == "rs-safe"
            return MODULE.httpx.Response(
                200,
                json={
                    "model_entry_id": 1,
                    "runtime_id": "rt-119",
                    "websocket_path": "/runtime-edge/runtime-1/ws",
                    "websocket_protocol": "runtime-edge.v1",
                    "runtime_access": f"access-{self.calls}",
                    "session_grant": f"grant-{self.calls}",
                },
            )

    client = Client()
    api.client = client  # type: ignore[assignment]

    first = await api.prepare("rs-safe", 1)
    second = await api.prepare("rs-safe", 1)

    assert client.calls == 2
    assert first.runtime_access == "access-1"
    assert second.runtime_access == "access-2"
