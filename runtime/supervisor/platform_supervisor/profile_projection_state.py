from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from platform_supervisor.asset_contracts import (
    ProfileCapabilityEvidence,
    ProfileComponentDigests,
    ProfileExecutionControlRequest,
    ProfileExecutionControlResponse,
    ProfileProjectionApplyRequest,
    ProfileProjectionApplyResponse,
)


class ProfileProjectionStateError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ProfileProjectionState:
    operation_id: str
    runtime_agent_profile_id: int
    hermes_profile_name: str
    desired_revision: int
    applied_revision: int
    status: str
    digests: ProfileComponentDigests | None
    applied_digests: ProfileComponentDigests | None = None
    effective_generation: int = 0
    capabilities: tuple[ProfileCapabilityEvidence, ...] = ()
    execution_revoked: bool = False
    admission_generation: int = 0
    error_code: str | None = None
    error_message: str | None = None

    def response(self) -> ProfileProjectionApplyResponse:
        return ProfileProjectionApplyResponse(
            operation_id=self.operation_id,
            runtime_agent_profile_id=self.runtime_agent_profile_id,
            desired_revision=self.desired_revision,
            applied_revision=self.applied_revision,
            effective_generation=self.effective_generation,
            status=self.status,
            digests=self.applied_digests or self.digests,
            capabilities=list(self.capabilities),
            error_code=self.error_code,
            error_message=self.error_message,
        )


class ProfileProjectionStateStore:
    """Durable Supervisor observation for Profile pending/applied state."""

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
            raise ProfileProjectionStateError("PROFILE_STATE_WAL_REQUIRED")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS profile_projection_state (
                runtime_agent_profile_id INTEGER PRIMARY KEY,
                hermes_profile_name TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                desired_revision INTEGER NOT NULL,
                applied_revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                profile_digest TEXT,
                skill_digest TEXT,
                mcp_digest TEXT,
                knowledge_digest TEXT,
                applied_profile_digest TEXT,
                applied_skill_digest TEXT,
                applied_mcp_digest TEXT,
                applied_knowledge_digest TEXT,
                effective_generation INTEGER NOT NULL DEFAULT 0,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                execution_revoked INTEGER NOT NULL DEFAULT 0,
                admission_generation INTEGER NOT NULL DEFAULT 0,
                execution_operation_id TEXT,
                execution_action TEXT,
                error_code TEXT,
                error_message TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_profile_projection_operation
                ON profile_projection_state(operation_id);
            COMMIT;
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(profile_projection_state)"
            ).fetchall()
        }
        additions = {
            "applied_profile_digest": "TEXT",
            "applied_skill_digest": "TEXT",
            "applied_mcp_digest": "TEXT",
            "applied_knowledge_digest": "TEXT",
            "effective_generation": "INTEGER NOT NULL DEFAULT 0",
            "capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
            "execution_revoked": "INTEGER NOT NULL DEFAULT 0",
            "admission_generation": "INTEGER NOT NULL DEFAULT 0",
            "execution_operation_id": "TEXT",
            "execution_action": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE profile_projection_state ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            UPDATE profile_projection_state
            SET applied_profile_digest = profile_digest,
                applied_skill_digest = skill_digest,
                applied_mcp_digest = mcp_digest,
                applied_knowledge_digest = knowledge_digest,
                effective_generation = CASE
                    WHEN effective_generation = 0 THEN 1
                    ELSE effective_generation
                END
            WHERE applied_revision > 0 AND applied_profile_digest IS NULL
            """
        )
        self._connection = connection

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    async def begin(
        self,
        request: ProfileProjectionApplyRequest,
    ) -> ProfileProjectionState:
        async with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM profile_projection_state "
                    "WHERE runtime_agent_profile_id = ?",
                    (request.runtime_agent_profile_id,),
                ).fetchone()
                existing = self._row(row) if row is not None else None
                if existing is not None:
                    same_target = (
                        existing.desired_revision == request.desired_revision
                        and existing.digests == request.digests
                        and existing.hermes_profile_name == request.hermes_profile_name
                    )
                    if same_target:
                        if existing.status == "applied":
                            connection.execute("COMMIT")
                            return existing
                        connection.execute(
                            """
                            UPDATE profile_projection_state
                            SET operation_id = ?, status = 'pending',
                                error_code = NULL, error_message = NULL,
                                updated_at = ?
                            WHERE runtime_agent_profile_id = ?
                            """,
                            (
                                request.operation_id,
                                datetime.now(timezone.utc).isoformat(),
                                request.runtime_agent_profile_id,
                            ),
                        )
                        row = connection.execute(
                            "SELECT * FROM profile_projection_state "
                            "WHERE runtime_agent_profile_id = ?",
                            (request.runtime_agent_profile_id,),
                        ).fetchone()
                        connection.execute("COMMIT")
                        assert row is not None
                        return self._row(row)
                    if request.desired_revision <= existing.desired_revision:
                        raise ProfileProjectionStateError(
                            "PROFILE_PROJECTION_TARGET_CONFLICT"
                        )
                    if existing.applied_revision != request.expected_applied_revision:
                        raise ProfileProjectionStateError(
                            "PROFILE_APPLIED_REVISION_STALE"
                        )
                    if (
                        existing.effective_generation
                        != request.expected_effective_generation
                    ):
                        raise ProfileProjectionStateError(
                            "PROFILE_EFFECTIVE_GENERATION_STALE"
                        )
                elif request.expected_applied_revision != 0:
                    raise ProfileProjectionStateError(
                        "PROFILE_APPLIED_REVISION_STALE"
                    )
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    INSERT INTO profile_projection_state (
                        runtime_agent_profile_id, hermes_profile_name,
                        operation_id, desired_revision, applied_revision,
                        status, profile_digest, skill_digest, mcp_digest,
                        knowledge_digest, error_code, error_message, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, NULL, NULL, ?)
                    ON CONFLICT(runtime_agent_profile_id) DO UPDATE SET
                        hermes_profile_name = excluded.hermes_profile_name,
                        operation_id = excluded.operation_id,
                        desired_revision = excluded.desired_revision,
                        status = 'pending',
                        profile_digest = excluded.profile_digest,
                        skill_digest = excluded.skill_digest,
                        mcp_digest = excluded.mcp_digest,
                        knowledge_digest = excluded.knowledge_digest,
                        error_code = NULL,
                        error_message = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        request.runtime_agent_profile_id,
                        request.hermes_profile_name,
                        request.operation_id,
                        request.desired_revision,
                        request.expected_applied_revision,
                        request.digests.profile,
                        request.digests.skill,
                        request.digests.mcp,
                        request.digests.knowledge,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM profile_projection_state "
                    "WHERE runtime_agent_profile_id = ?",
                    (request.runtime_agent_profile_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        assert row is not None
        return self._row(row)

    async def applied(
        self,
        request: ProfileProjectionApplyRequest,
        *,
        status: str = "applied",
        capabilities: tuple[ProfileCapabilityEvidence, ...] = (),
    ) -> ProfileProjectionState:
        return await self._finish(
            request,
            status=status,
            applied_revision=request.desired_revision,
            capabilities=capabilities,
            error_code=None,
            error_message=None,
        )

    async def failed(
        self,
        request: ProfileProjectionApplyRequest,
        *,
        error_code: str,
        error_message: str,
    ) -> ProfileProjectionState:
        current = await self.get(request.runtime_agent_profile_id)
        applied_revision = current.applied_revision if current is not None else 0
        return await self._finish(
            request,
            status="failed",
            applied_revision=applied_revision,
            capabilities=None,
            error_code=error_code[:128],
            error_message=error_message[:1000],
        )

    async def _finish(
        self,
        request: ProfileProjectionApplyRequest,
        *,
        status: str,
        applied_revision: int,
        capabilities: tuple[ProfileCapabilityEvidence, ...] | None,
        error_code: str | None,
        error_message: str | None,
    ) -> ProfileProjectionState:
        async with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM profile_projection_state "
                    "WHERE runtime_agent_profile_id = ?",
                    (request.runtime_agent_profile_id,),
                ).fetchone()
                if row is None:
                    raise ProfileProjectionStateError(
                        "PROFILE_PROJECTION_STATE_MISSING"
                    )
                current = self._row(row)
                if (
                    current.operation_id != request.operation_id
                    or current.desired_revision != request.desired_revision
                    or current.digests != request.digests
                ):
                    raise ProfileProjectionStateError(
                        "PROFILE_PROJECTION_OPERATION_STALE"
                    )
                if capabilities is None:
                    connection.execute(
                        """
                        UPDATE profile_projection_state
                        SET applied_revision = ?, status = ?, error_code = ?,
                            error_message = ?, updated_at = ?
                        WHERE runtime_agent_profile_id = ?
                        """,
                        (
                            applied_revision,
                            status,
                            error_code,
                            error_message,
                            datetime.now(timezone.utc).isoformat(),
                            request.runtime_agent_profile_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE profile_projection_state
                        SET applied_revision = ?, status = ?, error_code = NULL,
                            error_message = NULL,
                            applied_profile_digest = ?, applied_skill_digest = ?,
                            applied_mcp_digest = ?, applied_knowledge_digest = ?,
                            effective_generation = effective_generation + 1,
                            capabilities_json = ?, updated_at = ?
                        WHERE runtime_agent_profile_id = ?
                        """,
                        (
                            applied_revision,
                            status,
                            request.digests.profile,
                            request.digests.skill,
                            request.digests.mcp,
                            request.digests.knowledge,
                            json.dumps(
                                [item.model_dump(mode="json") for item in capabilities],
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            datetime.now(timezone.utc).isoformat(),
                            request.runtime_agent_profile_id,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM profile_projection_state "
                    "WHERE runtime_agent_profile_id = ?",
                    (request.runtime_agent_profile_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        assert row is not None
        return self._row(row)

    async def get(self, runtime_agent_profile_id: int) -> ProfileProjectionState | None:
        async with self._lock:
            row = self._require_connection().execute(
                "SELECT * FROM profile_projection_state "
                "WHERE runtime_agent_profile_id = ?",
                (runtime_agent_profile_id,),
            ).fetchone()
        return self._row(row) if row is not None else None

    async def control(
        self,
        request: ProfileExecutionControlRequest,
    ) -> ProfileExecutionControlResponse:
        async with self._lock:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM profile_projection_state "
                    "WHERE runtime_agent_profile_id = ?",
                    (request.runtime_agent_profile_id,),
                ).fetchone()
                if row is None:
                    raise ProfileProjectionStateError(
                        "PROFILE_PROJECTION_STATE_MISSING"
                    )
                current_generation = int(row["admission_generation"] or 0)
                current_action = str(row["execution_action"] or "")
                current_operation = str(row["execution_operation_id"] or "")
                if request.admission_generation < current_generation:
                    raise ProfileProjectionStateError(
                        "PROFILE_ADMISSION_GENERATION_STALE"
                    )
                already = (
                    request.admission_generation == current_generation
                    and request.action == current_action
                )
                if request.admission_generation == current_generation and not already:
                    raise ProfileProjectionStateError(
                        "PROFILE_ADMISSION_GENERATION_CONFLICT"
                    )
                if not already:
                    connection.execute(
                        """
                        UPDATE profile_projection_state
                        SET execution_revoked = ?, admission_generation = ?,
                            execution_operation_id = ?, execution_action = ?,
                            updated_at = ?
                        WHERE runtime_agent_profile_id = ?
                        """,
                        (
                            1 if request.action == "revoke" else 0,
                            request.admission_generation,
                            request.operation_id,
                            request.action,
                            datetime.now(timezone.utc).isoformat(),
                            request.runtime_agent_profile_id,
                        ),
                    )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return ProfileExecutionControlResponse(
            operation_id=(current_operation if already else request.operation_id),
            runtime_agent_profile_id=request.runtime_agent_profile_id,
            action=request.action,
            admission_generation=request.admission_generation,
            status="already_applied" if already else "applied",
            affected_active_run_ids=[],
        )

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ProfileProjectionStateError("PROFILE_STATE_NOT_STARTED")
        return self._connection

    @staticmethod
    def _row(row: sqlite3.Row) -> ProfileProjectionState:
        digests = None
        if row["profile_digest"] is not None:
            digests = ProfileComponentDigests(
                profile=str(row["profile_digest"]),
                skill=str(row["skill_digest"]),
                mcp=str(row["mcp_digest"]),
                knowledge=str(row["knowledge_digest"]),
            )
        applied_digests = None
        if row["applied_profile_digest"] is not None:
            applied_digests = ProfileComponentDigests(
                profile=str(row["applied_profile_digest"]),
                skill=str(row["applied_skill_digest"]),
                mcp=str(row["applied_mcp_digest"]),
                knowledge=str(row["applied_knowledge_digest"]),
            )
        try:
            capabilities = tuple(
                ProfileCapabilityEvidence.model_validate(item)
                for item in json.loads(str(row["capabilities_json"] or "[]"))
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProfileProjectionStateError(
                "PROFILE_CAPABILITY_STATE_INVALID"
            ) from exc
        return ProfileProjectionState(
            operation_id=str(row["operation_id"]),
            runtime_agent_profile_id=int(row["runtime_agent_profile_id"]),
            hermes_profile_name=str(row["hermes_profile_name"]),
            desired_revision=int(row["desired_revision"]),
            applied_revision=int(row["applied_revision"]),
            status=str(row["status"]),
            digests=digests,
            applied_digests=applied_digests,
            effective_generation=int(row["effective_generation"] or 0),
            capabilities=capabilities,
            execution_revoked=bool(row["execution_revoked"]),
            admission_generation=int(row["admission_generation"] or 0),
            error_code=str(row["error_code"]) if row["error_code"] else None,
            error_message=(
                str(row["error_message"]) if row["error_message"] else None
            ),
        )


__all__ = [
    "ProfileProjectionState",
    "ProfileProjectionStateError",
    "ProfileProjectionStateStore",
]
