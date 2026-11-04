from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.services.runtime_commands as runtime_commands_module
from app.services.runtime_commands import RuntimeCommandClient


class _TerminalSnapshotClient(RuntimeCommandClient):
    async def get_run(
        self, *, target: Any, run_id: str
    ) -> dict[str, Any]:
        del target
        return {
            "run_id": run_id,
            "status": "COMPLETED",
            "answer": "完成",
            "events": [],
        }

    async def stream_run_events(
        self,
        *,
        target: Any,
        run_id: str,
        after_seq: int = 0,
        timeout_seconds: float = 600,
    ):
        del target, run_id, after_seq, timeout_seconds
        await asyncio.sleep(60)
        yield {}


@pytest.mark.asyncio
async def test_terminal_snapshot_wins_when_stream_join_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fast_stream_join(
        awaitable: Any,
        timeout: float,
    ) -> Any:
        del timeout
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
            try:
                await awaitable
            except asyncio.CancelledError:
                pass
            raise asyncio.TimeoutError
        return await awaitable

    monkeypatch.setattr(
        runtime_commands_module.asyncio,
        "wait_for",
        fast_stream_join,
    )
    client = _TerminalSnapshotClient()
    projected: list[str] = []

    async def event_sink(name: str, _data: dict[str, Any]) -> None:
        projected.append(name)

    snapshot = await client.wait_run(
        target=object(),  # type: ignore[arg-type]
        run_id="run-image-00000001",
        event_sink=event_sink,
        poll_interval_seconds=0,
    )

    assert snapshot["status"] == "COMPLETED"
    assert snapshot["answer"] == "完成"
    assert projected == ["chat.done"]
    await client.close()
