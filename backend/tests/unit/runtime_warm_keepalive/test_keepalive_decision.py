from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.runtime_warm_keepalive import decide_warm_keepalive


NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _receipt(
    seconds_ago: int,
    *,
    websockets: int = 0,
    runs: int = 0,
) -> tuple[datetime, dict]:
    return (
        NOW - timedelta(seconds=seconds_ago),
        {
            "websockets": {"active": websockets},
            "runs": {"active": runs},
        },
    )


def test_no_receipts_never_keeps_warm() -> None:
    decision = decide_warm_keepalive(
        [],
        last_activity_at=None,
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (False, "no_health_projection")


def test_stale_receipt_is_not_woken_up() -> None:
    decision = decide_warm_keepalive(
        [_receipt(600)],
        last_activity_at=None,
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (False, "health_projection_stale")


def test_connected_client_never_scales_down() -> None:
    decision = decide_warm_keepalive(
        [_receipt(10, websockets=1)],
        last_activity_at=None,
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (True, "client_online")


def test_active_run_never_scales_down() -> None:
    decision = decide_warm_keepalive(
        [_receipt(10, runs=2)],
        last_activity_at=None,
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (True, "run_active")


def test_idle_within_ttl_stays_warm() -> None:
    decision = decide_warm_keepalive(
        [
            _receipt(30),
            _receipt(600, websockets=1),
        ],
        last_activity_at=None,
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (True, "idle_ttl_window")


def test_idle_beyond_ttl_stops_keepalive() -> None:
    decision = decide_warm_keepalive(
        [
            _receipt(30),
            _receipt(1200, websockets=1),
        ],
        last_activity_at=None,
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (False, "idle_expired")


def test_run_activity_within_ttl_stays_warm() -> None:
    """Health ws=0 but a recent run.* projection proves the Runtime was busy."""
    decision = decide_warm_keepalive(
        [_receipt(30)],
        last_activity_at=NOW - timedelta(seconds=600),
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (True, "idle_ttl_window")


def test_run_activity_beyond_ttl_stops_keepalive() -> None:
    decision = decide_warm_keepalive(
        [_receipt(30)],
        last_activity_at=NOW - timedelta(seconds=1200),
        now=NOW,
        idle_ttl_seconds=900,
        receipt_fresh_seconds=180,
    )
    assert (decision.keep, decision.reason) == (False, "idle_expired")
