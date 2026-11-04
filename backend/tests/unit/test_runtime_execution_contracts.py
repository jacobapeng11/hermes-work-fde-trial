from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.application_agent import ApplicationAgentConfig
from app.schemas.runtime_edge import (
    RUNTIME_EDGE_WEBSOCKET_PROTOCOL,
    RuntimeAccessClaims,
    RuntimeEdgeRunCancelAck,
    RuntimeEdgeRunSubmitRequest,
    SessionGrantClaims,
)
from app.schemas.runtime_internal import (
    ChannelFastLaneDeliveryAck,
    ChannelFastLaneSubmitFrame,
    ChannelFastLaneV2DeliveryAck,
    ChannelFastLaneV2DispositionAck,
    ChannelFastLaneV2DispositionFrame,
    ChannelFastLaneV2ResumeFrame,
    ChannelFastLaneV2SubmitFrame,
    RuntimeCommandEnvelope,
    RuntimeProjectionBatch,
)


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "runtime-execution-plane-v1"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("runtime-access.json", RuntimeAccessClaims),
        ("session-grant.json", SessionGrantClaims),
        ("web-run-submit.json", RuntimeEdgeRunSubmitRequest),
        ("internal-turn-command.json", RuntimeCommandEnvelope),
        ("cancel-command.json", RuntimeCommandEnvelope),
        ("projection-batch.json", RuntimeProjectionBatch),
        ("channel-fast-lane-submit.json", ChannelFastLaneSubmitFrame),
        ("channel-delivery-ack.json", ChannelFastLaneDeliveryAck),
        ("channel-fast-lane-v2-submit.json", ChannelFastLaneV2SubmitFrame),
        ("channel-fast-lane-v2-resume.json", ChannelFastLaneV2ResumeFrame),
        (
            "channel-fast-lane-v2-disposition-ack.json",
            ChannelFastLaneV2DispositionAck,
        ),
        (
            "channel-fast-lane-v2-disposition-pending.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        (
            "channel-fast-lane-v2-disposition-promoted.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        ("channel-delivery-v2-ack.json", ChannelFastLaneV2DeliveryAck),
    ],
)
def test_runtime_execution_fixture_matches_backend_contract(name: str, model: type) -> None:
    payload = load_fixture(name)

    parsed = model.model_validate(payload)

    assert parsed.model_dump(
        mode="json",
        exclude_none=model is not ChannelFastLaneV2DispositionFrame,
    ) == payload


def test_runtime_access_cannot_outlive_five_minutes() -> None:
    payload = load_fixture("runtime-access.json")
    payload["expires_at"] += 1

    with pytest.raises(ValidationError, match="cannot exceed 300 seconds"):
        RuntimeAccessClaims.model_validate(payload)


def test_backend_accepts_run_cancel_ack_contract() -> None:
    parsed = RuntimeEdgeRunCancelAck.model_validate(
        {
            "schema_version": 1,
            "type": "run.cancel_ack",
            "run_id": "run-cancel-00000001",
            "status": "already_terminal",
        }
    )

    assert parsed.status == "already_terminal"


def test_session_grant_cannot_outlive_thirty_minutes() -> None:
    payload = load_fixture("session-grant.json")
    payload["expires_at"] += 1

    with pytest.raises(ValidationError, match="cannot exceed 1800 seconds"):
        SessionGrantClaims.model_validate(payload)


def test_projection_allows_run_level_item_without_sequence() -> None:
    payload = load_fixture("projection-batch.json")
    payload["items"][0].pop("seq")

    parsed = RuntimeProjectionBatch.model_validate(payload)

    assert parsed.items[0].run_id == payload["items"][0]["run_id"]
    assert parsed.items[0].seq is None


def test_projection_sequence_requires_run_id() -> None:
    payload = load_fixture("projection-batch.json")
    payload["items"][0].pop("run_id")

    with pytest.raises(ValidationError, match="seq requires run_id"):
        RuntimeProjectionBatch.model_validate(payload)


def test_command_payload_digest_is_verified() -> None:
    payload = load_fixture("internal-turn-command.json")
    payload["payload"]["prompt"] = "tampered"

    with pytest.raises(ValidationError, match="payload_digest does not match"):
        RuntimeCommandEnvelope.model_validate(payload)


def test_projection_payload_digest_is_verified() -> None:
    payload = load_fixture("projection-batch.json")
    payload["items"][0]["payload"]["status"] = "failed"

    with pytest.raises(ValidationError, match="payload_digest does not match"):
        RuntimeProjectionBatch.model_validate(payload)


def test_runtime_websocket_subprotocol_is_fixed() -> None:
    assert RUNTIME_EDGE_WEBSOCKET_PROTOCOL == "hermes-runtime-v1"


def test_application_capacity_is_not_an_agent_level_config_override() -> None:
    fields = set(ApplicationAgentConfig.model_fields)

    assert "max_inflight_runs" not in fields
    assert "queue_limit" not in fields
    assert "queue_timeout_seconds" not in fields
