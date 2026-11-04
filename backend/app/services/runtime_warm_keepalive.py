from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from collections.abc import Awaitable

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.hermes_runtime.protocol import shared_runtime_http_client
from app.models import HermesRuntimeBinding

logger = logging.getLogger(__name__)

HEALTH_PROJECTION_TYPE = "runtime.health"
ACTIVITY_PROJECTION_PREFIX = "run."
HEALTH_HISTORY_LIMIT = 40


@dataclass(frozen=True, slots=True)
class WarmDecision:
    keep: bool
    reason: str


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def decide_warm_keepalive(
    receipts: list[tuple[datetime, dict[str, Any]]],
    *,
    last_activity_at: datetime | None,
    now: datetime,
    idle_ttl_seconds: int,
    receipt_fresh_seconds: int,
) -> WarmDecision:
    """Decide whether one Runtime must be kept warm.

    Newer receipts come first. A Runtime is kept warm while the latest health
    projection is fresh and any of the following holds:
    - a WebUI/desktop WebSocket is connected (websockets.active > 0), or
    - a Run is executing (runs.active > 0), or
    - the last such busy moment is within the idle TTL.
    Stale or missing receipts mean the Pod is gone or not reporting; it is
    never woken up by this worker.
    """
    if not receipts:
        return WarmDecision(False, "no_health_projection")
    latest_at, latest_payload = receipts[0]
    latest_at = _aware_utc(latest_at) or now
    if (now - latest_at).total_seconds() > receipt_fresh_seconds:
        return WarmDecision(False, "health_projection_stale")
    if _positive((latest_payload.get("websockets") or {}).get("active")):
        return WarmDecision(True, "client_online")
    if _positive((latest_payload.get("runs") or {}).get("active")):
        return WarmDecision(True, "run_active")
    last_busy: datetime | None = _aware_utc(last_activity_at)
    for occurred_at, payload in receipts:
        aware_occurred = _aware_utc(occurred_at)
        if aware_occurred is None:
            continue
        if last_busy is not None and aware_occurred <= last_busy:
            break
        if _positive((payload.get("websockets") or {}).get("active")) or _positive(
            (payload.get("runs") or {}).get("active")
        ):
            if last_busy is None or aware_occurred > last_busy:
                last_busy = aware_occurred
    if last_busy is None:
        return WarmDecision(False, "never_busy")
    if (now - last_busy).total_seconds() <= idle_ttl_seconds:
        return WarmDecision(True, "idle_ttl_window")
    return WarmDecision(False, "idle_expired")


async def load_health_receipts(
    session: Any, runtime_id: str
) -> list[tuple[datetime, dict[str, Any]]]:
    from app.models import RuntimeProjectionReceipt

    rows = (
        await session.execute(
            select(
                RuntimeProjectionReceipt.occurred_at,
                RuntimeProjectionReceipt.payload,
            )
            .where(
                RuntimeProjectionReceipt.runtime_id == runtime_id,
                RuntimeProjectionReceipt.projection_type == HEALTH_PROJECTION_TYPE,
            )
            .order_by(RuntimeProjectionReceipt.id.desc())
            .limit(HEALTH_HISTORY_LIMIT)
        )
    ).all()
    return [(row[0], dict(row[1])) for row in rows]


async def load_last_activity_at(session: Any, runtime_id: str) -> datetime | None:
    """Return the most recent run.* projection time for one Runtime."""
    from app.models import RuntimeProjectionReceipt

    return await session.scalar(
        select(RuntimeProjectionReceipt.occurred_at)
        .where(
            RuntimeProjectionReceipt.runtime_id == runtime_id,
            RuntimeProjectionReceipt.projection_type.like(
                f"{ACTIVITY_PROJECTION_PREFIX}%"
            ),
        )
        .order_by(RuntimeProjectionReceipt.id.desc())
        .limit(1)
    )


async def keep_runtimes_warm_once(
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    stats = {"kept": 0, "skipped": 0, "failed": 0}
    client = await shared_runtime_http_client()
    async with AsyncSessionLocal() as session:
        bindings = (
            (
                await session.execute(
                    select(HermesRuntimeBinding).where(
                        HermesRuntimeBinding.status == "ready",
                        HermesRuntimeBinding.endpoint_base_url.is_not(None),
                        HermesRuntimeBinding.endpoint_host_header.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        targets = [
            (str(binding.runtime_id), str(binding.endpoint_base_url), str(binding.endpoint_host_header))
            for binding in bindings
        ]
    for runtime_id, base_url, host_header in targets:
        try:
            async with AsyncSessionLocal() as session:
                receipts = await load_health_receipts(session, runtime_id)
                last_activity = await load_last_activity_at(session, runtime_id)
            decision = decide_warm_keepalive(
                receipts,
                last_activity_at=_aware_utc(last_activity),
                now=now,
                idle_ttl_seconds=settings.RUNTIME_WARM_IDLE_TTL_SECONDS,
                receipt_fresh_seconds=settings.RUNTIME_WARM_RECEIPT_FRESH_SECONDS,
            )
            if not decision.keep:
                stats["skipped"] += 1
                continue
            response = await client.head(
                f"{base_url.rstrip('/')}/livez",
                headers={"Host": host_header},
                timeout=5.0,
            )
            await response.aclose()
            stats["kept"] += 1
        except (httpx.HTTPError, asyncio.TimeoutError, OSError, ValueError):
            stats["failed"] += 1
        except Exception:
            logger.exception(
                "Runtime warm keepalive failed",
                extra={"runtime_id": runtime_id},
            )
            stats["failed"] += 1
    return stats


async def keep_runtimes_warm_forever(
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    interval = max(10.0, float(settings.RUNTIME_WARM_KEEPALIVE_INTERVAL_SECONDS))
    while True:
        try:
            stats = await keep_runtimes_warm_once()
            if stats["kept"] or stats["failed"]:
                logger.info(
                    "Runtime warm keepalive kept=%(kept)s skipped=%(skipped)s failed=%(failed)s",
                    {"kept": stats["kept"], "skipped": stats["skipped"], "failed": stats["failed"]},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Runtime warm keepalive cycle failed")
        await sleep(interval)
