from __future__ import annotations

import pytest
from pydantic import ValidationError

from platform_supervisor.contracts import CancelCommandPayload


def test_cancel_payload_remains_backward_compatible() -> None:
    payload = CancelCommandPayload(run_id="run-clarity-0001")

    assert payload.clarify_request_id is None


def test_clarify_cancel_payload_accepts_request_id() -> None:
    payload = CancelCommandPayload(
        run_id="run-clarity-0001",
        clarify_request_id="clr_12345678",
    )

    assert payload.clarify_request_id == "clr_12345678"


@pytest.mark.parametrize("request_id", ["", "x" * 129])
def test_clarify_cancel_payload_rejects_invalid_request_id(
    request_id: str,
) -> None:
    with pytest.raises(ValidationError):
        CancelCommandPayload(
            run_id="run-clarity-0001",
            clarify_request_id=request_id,
        )
