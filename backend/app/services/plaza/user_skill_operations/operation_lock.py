from __future__ import annotations

import asyncio


class RuntimeOperationLocks:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def for_workspace(self, workspace_id: int) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(workspace_id, asyncio.Lock())

