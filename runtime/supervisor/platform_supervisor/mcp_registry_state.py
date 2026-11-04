from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from platform_supervisor.asset_contracts import (
    RuntimeMcpRegistryApplyRequest,
    RuntimeMcpRegistryApplyResponse,
    RuntimeMcpRegistryServerEvidence,
)


class McpRegistryStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class McpRegistryState:
    operation_id: str
    desired_revision: int
    applied_revision: int
    status: str
    registry_digest: str | None
    servers: tuple[RuntimeMcpRegistryServerEvidence, ...] = ()
    affected_active_run_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def response(self) -> RuntimeMcpRegistryApplyResponse:
        return RuntimeMcpRegistryApplyResponse(
            operation_id=self.operation_id,
            desired_revision=self.desired_revision,
            applied_revision=self.applied_revision,
            status=self.status,
            registry_digest=self.registry_digest,
            servers=list(self.servers),
            affected_active_run_ids=list(self.affected_active_run_ids),
            error_code=self.error_code,
            error_message=self.error_message,
        )


class McpRegistryStateStore:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        if str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
            connection.close()
            raise McpRegistryStateError("MCP_REGISTRY_STATE_WAL_REQUIRED")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS runtime_mcp_registry_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                operation_id TEXT NOT NULL UNIQUE,
                desired_revision INTEGER NOT NULL,
                applied_revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                registry_digest TEXT,
                servers_json TEXT NOT NULL,
                affected_run_ids_json TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT
            );
            COMMIT;
            """
        )
        self._connection = connection

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    async def get(self) -> McpRegistryState | None:
        async with self._lock:
            row = self._require().execute(
                "SELECT * FROM runtime_mcp_registry_state WHERE singleton = 1"
            ).fetchone()
        return self._row(row) if row is not None else None

    async def begin(
        self,
        request: RuntimeMcpRegistryApplyRequest,
        *,
        affected_run_ids: list[str],
    ) -> McpRegistryState:
        async with self._lock:
            connection = self._require()
            current_row = connection.execute(
                "SELECT * FROM runtime_mcp_registry_state WHERE singleton = 1"
            ).fetchone()
            current = self._row(current_row) if current_row is not None else None
            if current is not None:
                if (
                    current.desired_revision == request.desired_revision
                    and current.registry_digest == request.registry_digest
                ):
                    if current.status in {"ready", "reloading"}:
                        return current
                    if current.applied_revision != request.expected_applied_revision:
                        raise McpRegistryStateError("MCP_REGISTRY_APPLIED_MISMATCH")
                if request.desired_revision <= current.desired_revision:
                    if request.desired_revision != current.desired_revision:
                        raise McpRegistryStateError("MCP_REGISTRY_REVISION_STALE")
                if current.applied_revision != request.expected_applied_revision:
                    raise McpRegistryStateError("MCP_REGISTRY_APPLIED_MISMATCH")
            elif request.expected_applied_revision != 0:
                raise McpRegistryStateError("MCP_REGISTRY_APPLIED_MISMATCH")
            applied_revision = current.applied_revision if current is not None else 0
            connection.execute(
                """
                INSERT INTO runtime_mcp_registry_state (
                    singleton, operation_id, desired_revision, applied_revision,
                    status, registry_digest, servers_json,
                    affected_run_ids_json, error_code, error_message
                ) VALUES (1, ?, ?, ?, 'reloading', ?, '[]', ?, NULL, NULL)
                ON CONFLICT(singleton) DO UPDATE SET
                    operation_id = excluded.operation_id,
                    desired_revision = excluded.desired_revision,
                    status = 'reloading',
                    registry_digest = excluded.registry_digest,
                    servers_json = '[]',
                    affected_run_ids_json = excluded.affected_run_ids_json,
                    error_code = NULL,
                    error_message = NULL
                """,
                (
                    request.operation_id,
                    request.desired_revision,
                    applied_revision,
                    request.registry_digest,
                    json.dumps(sorted(set(affected_run_ids))),
                ),
            )
        state = await self.get()
        assert state is not None
        return state

    async def is_reloading(self) -> bool:
        state = await self.get()
        return state is not None and state.status == "reloading"

    async def finish(
        self,
        request: RuntimeMcpRegistryApplyRequest,
        *,
        status: str,
        servers: list[RuntimeMcpRegistryServerEvidence],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> McpRegistryState:
        async with self._lock:
            connection = self._require()
            applied_revision = (
                request.desired_revision if status == "ready" else request.expected_applied_revision
            )
            cursor = connection.execute(
                """
                UPDATE runtime_mcp_registry_state
                SET applied_revision = ?, status = ?, servers_json = ?,
                    error_code = ?, error_message = ?
                WHERE singleton = 1 AND operation_id = ?
                    AND desired_revision = ? AND registry_digest = ?
                """,
                (
                    applied_revision,
                    status,
                    json.dumps(
                        [item.model_dump(mode="json") for item in servers],
                        sort_keys=True,
                    ),
                    error_code,
                    error_message,
                    request.operation_id,
                    request.desired_revision,
                    request.registry_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise McpRegistryStateError("MCP_REGISTRY_OPERATION_SUPERSEDED")
        state = await self.get()
        assert state is not None
        return state

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise McpRegistryStateError("MCP_REGISTRY_STATE_NOT_STARTED")
        return self._connection

    @staticmethod
    def _row(row: sqlite3.Row) -> McpRegistryState:
        return McpRegistryState(
            operation_id=str(row["operation_id"]),
            desired_revision=int(row["desired_revision"]),
            applied_revision=int(row["applied_revision"]),
            status=str(row["status"]),
            registry_digest=(
                str(row["registry_digest"])
                if row["registry_digest"] is not None
                else None
            ),
            servers=tuple(
                RuntimeMcpRegistryServerEvidence.model_validate(item)
                for item in json.loads(str(row["servers_json"]))
            ),
            affected_active_run_ids=tuple(
                str(item)
                for item in json.loads(str(row["affected_run_ids_json"]))
            ),
            error_code=(str(row["error_code"]) if row["error_code"] else None),
            error_message=(
                str(row["error_message"]) if row["error_message"] else None
            ),
        )


__all__ = ["McpRegistryState", "McpRegistryStateError", "McpRegistryStateStore"]
