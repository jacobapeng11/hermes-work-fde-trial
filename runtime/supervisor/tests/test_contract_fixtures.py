from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from platform_supervisor.contracts import (
    ChannelFastLaneDeliveryAck,
    ChannelFastLaneSubmitFrame,
    ChannelFastLaneV2DeliveryAck,
    ChannelFastLaneV2DispositionAck,
    ChannelFastLaneV2DispositionFrame,
    ChannelFastLaneV2ResumeFrame,
    ChannelFastLaneV2SubmitFrame,
    RuntimeAccessClaims,
    RuntimeCommandEnvelope,
    RuntimeEdgeRunEvent,
    RuntimeEdgeRunCancelAck,
    RuntimeEdgeRunRedirectAccepted,
    RuntimeEdgeRunRedirectCanceled,
    RuntimeEdgeRunRedirectConsumed,
    RuntimeEdgeRunRedirectPending,
    RuntimeEdgeRunRedirectPromoted,
    RuntimeEdgeRunSteerSubmissionUncertain,
    RuntimeEdgeRunSubmitRequest,
    RuntimeProjectionBatch,
    SessionGrantClaims,
    canonical_payload_digest,
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
    ],
)
def test_shared_fixture_matches_supervisor_contract(name: str, model: type) -> None:
    payload = load_fixture(name)

    parsed = model.model_validate(payload)

    assert parsed.model_dump(mode="json", exclude_none=True) == payload


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("web-redirect-pending.json", RuntimeEdgeRunRedirectPending),
        ("web-redirect-accepted.json", RuntimeEdgeRunRedirectAccepted),
        ("web-redirect-consumed.json", RuntimeEdgeRunRedirectConsumed),
        ("web-redirect-promoted.json", RuntimeEdgeRunRedirectPromoted),
        ("web-redirect-canceled.json", RuntimeEdgeRunRedirectCanceled),
        (
            "web-steer-submission-uncertain.json",
            RuntimeEdgeRunSteerSubmissionUncertain,
        ),
        ("web-run-event-steer-accepted.json", RuntimeEdgeRunEvent),
        ("web-run-event-steer-consumed.json", RuntimeEdgeRunEvent),
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
            "channel-fast-lane-v2-disposition-ordinary-run-accepted.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        (
            "channel-fast-lane-v2-disposition-redirect-accepted.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        (
            "channel-fast-lane-v2-disposition-redirect-consumed.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        (
            "channel-fast-lane-v2-disposition-promoted.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        (
            "channel-fast-lane-v2-disposition-uncertain.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        (
            "channel-fast-lane-v2-disposition-canceled.json",
            ChannelFastLaneV2DispositionFrame,
        ),
        ("channel-delivery-v2-ack.json", ChannelFastLaneV2DeliveryAck),
    ],
)
def test_redirect_and_fast_lane_v2_shared_fixtures_match_supervisor_contract(
    name: str,
    model: type,
) -> None:
    payload = load_fixture(name)

    parsed = model.model_validate(payload)

    assert parsed.model_dump(mode="json", exclude_unset=True) == payload


def test_fast_lane_v2_disposition_rejects_raw_upstream_success_states() -> None:
    payload = load_fixture("channel-fast-lane-v2-disposition-pending.json")

    for raw_state in ("queued", "steered", "redirected"):
        payload["state"] = raw_state
        with pytest.raises(ValidationError):
            ChannelFastLaneV2DispositionFrame.model_validate(payload)


def test_command_envelope_requires_one_admission_generation() -> None:
    payload = load_fixture("internal-turn-command.json")
    payload["payload"]["identity"]["admission_generation"] += 1
    payload["payload_digest"] = canonical_payload_digest(payload["payload"])

    with pytest.raises(ValidationError, match="admission_generation"):
        RuntimeCommandEnvelope.model_validate(payload)


def test_fast_lane_envelope_and_lease_require_one_admission_generation() -> None:
    payload = load_fixture("channel-fast-lane-v2-submit.json")
    envelope = payload["envelope"]
    envelope["payload"]["channel_execution_lease"][
        "admission_generation"
    ] += 1
    envelope["payload_digest"] = canonical_payload_digest(envelope["payload"])

    with pytest.raises(ValidationError, match="admission_generation"):
        ChannelFastLaneV2SubmitFrame.model_validate(payload)


@pytest.mark.parametrize(
    ("fixture_name", "model"),
    [
        ("runtime-access.json", RuntimeAccessClaims),
        ("session-grant.json", SessionGrantClaims),
    ],
)
def test_external_claims_require_positive_admission_generation(
    fixture_name: str,
    model: type,
) -> None:
    payload = load_fixture(fixture_name)
    payload["admission_generation"] = 0

    with pytest.raises(ValidationError, match="admission_generation"):
        model.model_validate(payload)


def test_supervisor_rejects_tampered_internal_command() -> None:
    payload = load_fixture("internal-turn-command.json")
    payload["payload"]["prompt"] = "tampered"

    with pytest.raises(ValidationError, match="payload_digest does not match"):
        RuntimeCommandEnvelope.model_validate(payload)


def test_run_cancel_ack_contract_is_strict_and_correlated() -> None:
    parsed = RuntimeEdgeRunCancelAck.model_validate(
        {
            "schema_version": 1,
            "type": "run.cancel_ack",
            "run_id": "run-cancel-00000001",
            "status": "accepted",
        }
    )

    assert parsed.status == "accepted"
    with pytest.raises(ValidationError):
        RuntimeEdgeRunCancelAck.model_validate(
            {
                "schema_version": 1,
                "type": "run.cancel_ack",
                "run_id": "run-cancel-00000001",
                "status": "unknown",
            }
        )
