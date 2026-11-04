from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import PurePosixPath
import re

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentStudioFileArtifact,
    ApplicationAgentStudioSession,
)
from app.services.chat_files import (
    ChatFileError,
    ChatFilePermissionError,
    ChatFileRecord,
    display_name_for_path,
    normalize_workspace_relative_path,
)
from app.services.runtime_workspace import Principal


_SAFE_SESSION_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_PENDING_TTL = timedelta(minutes=10)


class ApplicationAgentStudioChatFileService:
    """MySQL index for files scoped to one Application Agent Studio Session."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def reserve_upload(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        runtime_session_id: str,
        display_name: str,
        mime_type: str | None = None,
        size_bytes: int,
    ) -> ChatFileRecord:
        if size_bytes <= 0:
            raise ChatFileError("size_bytes must be positive")
        if mime_type is not None and len(mime_type) > 255:
            raise ChatFileError("mime_type is too long")
        agent, session = await self._session_context(
            principal=principal,
            application_agent_id=application_agent_id,
            runtime_session_id=runtime_session_id,
        )
        filename = _safe_upload_filename(display_name)
        session_segment = _safe_session_segment(runtime_session_id)
        await self.db.execute(
            update(ApplicationAgentStudioFileArtifact)
            .where(
                ApplicationAgentStudioFileArtifact.studio_session_id == int(session.id),
                ApplicationAgentStudioFileArtifact.status == "pending",
                ApplicationAgentStudioFileArtifact.created_at < datetime.utcnow() - _PENDING_TTL,
            )
            .values(status="failed")
        )

        artifact = ApplicationAgentStudioFileArtifact(
            studio_session_id=int(session.id),
            application_agent_run_id=None,
            relative_path=(f"workspace/sessions/{session_segment}/.pending/{filename}"),
            mime_type=mime_type,
            size_bytes=size_bytes,
            source_type="upload",
            status="pending",
        )
        self.db.add(artifact)
        await self.db.flush()
        artifact.relative_path = (
            f"workspace/sessions/{session_segment}/{int(artifact.id)}/{filename}"
        )
        artifact.relative_path = normalize_workspace_relative_path(str(artifact.relative_path))
        await self.db.flush()
        return self._record(agent, session, None, artifact)

    async def list_files(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        runtime_session_id: str | None,
        runtime_run_id: str | None = None,
    ) -> list[ChatFileRecord]:
        if runtime_session_id is None:
            raise ChatFileError("runtime_session_id is required")
        agent, session = await self._session_context(
            principal=principal,
            application_agent_id=application_agent_id,
            runtime_session_id=runtime_session_id,
        )
        statement = (
            select(ApplicationAgentStudioFileArtifact, ApplicationAgentRun)
            .outerjoin(
                ApplicationAgentRun,
                ApplicationAgentRun.id
                == ApplicationAgentStudioFileArtifact.application_agent_run_id,
            )
            .where(
                ApplicationAgentStudioFileArtifact.studio_session_id == int(session.id),
                ApplicationAgentStudioFileArtifact.status == "active",
            )
            .order_by(
                ApplicationAgentStudioFileArtifact.created_at.desc(),
                ApplicationAgentStudioFileArtifact.id.desc(),
            )
        )
        if runtime_run_id is not None:
            statement = statement.where(ApplicationAgentRun.runtime_run_id == runtime_run_id)
        rows = (await self.db.execute(statement)).all()
        return [self._record(agent, session, run, artifact) for artifact, run in rows]

    async def get_file(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        file_id: int,
        include_pending: bool = False,
    ) -> ChatFileRecord:
        artifact, agent, session, run = await self._owned_artifact(
            principal=principal,
            application_agent_id=application_agent_id,
            file_id=file_id,
            statuses=("pending", "active", "failed") if include_pending else ("active",),
        )
        return self._record(agent, session, run, artifact)

    async def get_files(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        file_ids: list[int],
    ) -> list[ChatFileRecord]:
        ordered_ids = list(dict.fromkeys(file_ids))
        if not ordered_ids:
            return []
        rows = await self._owned_artifact_rows(
            principal=principal,
            application_agent_id=application_agent_id,
            statuses=("active",),
            file_ids=ordered_ids,
        )
        by_id = {
            int(artifact.id): self._record(agent, session, run, artifact)
            for artifact, agent, session, run in rows
        }
        try:
            return [by_id[file_id] for file_id in ordered_ids]
        except KeyError as exc:
            raise ChatFilePermissionError("chat file not found") from exc

    async def resolve_attachments(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        runtime_session_id: str,
        file_ids: list[int],
    ) -> tuple[dict[str, str | None], ...]:
        ordered_ids = list(dict.fromkeys(file_ids))
        if len(ordered_ids) != len(file_ids):
            raise ChatFilePermissionError("attachment identifiers must be unique")
        if not ordered_ids:
            return ()
        rows = await self._artifact_rows(
            principal=principal,
            application_agent_id=application_agent_id,
            studio_session_id=None,
            runtime_session_id=runtime_session_id,
            statuses=("active",),
            file_ids=ordered_ids,
        )
        by_id = {
            int(artifact.id): self._record(agent, artifact_session, run, artifact)
            for artifact, agent, artifact_session, run in rows
        }
        if any(file_id not in by_id for file_id in ordered_ids):
            raise ChatFilePermissionError("chat file not found")
        return tuple(
            {
                "file_id": str(by_id[file_id].file_id),
                "display_name": by_id[file_id].display_name,
                "relative_path": by_id[file_id].relative_path,
                "mime_type": by_id[file_id].mime_type,
            }
            for file_id in ordered_ids
        )

    async def rename_file(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        file_id: int,
        relative_path: str,
    ) -> ChatFileRecord:
        artifact, agent, session, run = await self._owned_artifact(
            principal=principal,
            application_agent_id=application_agent_id,
            file_id=file_id,
            statuses=("active",),
        )
        normalized = normalize_workspace_relative_path(relative_path)
        current_parent = str(artifact.relative_path).rsplit("/", 1)[0]
        target_parent = normalized.rsplit("/", 1)[0]
        if target_parent != current_parent:
            raise ChatFilePermissionError(
                "relative_path must stay within the current file directory"
            )
        _safe_upload_filename(display_name_for_path(normalized))
        artifact.relative_path = normalized
        await self.db.flush()
        return self._record(agent, session, run, artifact)

    async def delete_file(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        file_id: int,
    ) -> ChatFileRecord:
        artifact, agent, session, run = await self._owned_artifact(
            principal=principal,
            application_agent_id=application_agent_id,
            file_id=file_id,
            statuses=("active",),
        )
        artifact.status = "deleted"
        await self.db.flush()
        return self._record(agent, session, run, artifact)

    async def apply_file_status(
        self,
        *,
        binding: object,
        projection_type: str,
        payload: dict,
    ) -> None:
        if projection_type not in {"file.ready", "file.failed"}:
            raise ChatFileError("unsupported Runtime file projection")
        file_id = _positive_int(payload.get("file_id"))
        relative_path = str(payload.get("relative_path") or "")
        session_runtime_id = str(payload.get("session_id") or "")
        if file_id is None or not relative_path or not session_runtime_id:
            raise ChatFilePermissionError("Runtime file projection is invalid")
        if int(payload.get("org_id") or 0) != int(getattr(binding, "org_id") or 0):
            raise ChatFilePermissionError("Runtime file projection binding mismatch")
        try:
            normalized = normalize_workspace_relative_path(relative_path)
        except ChatFileError as exc:
            raise ChatFilePermissionError("Runtime file path is invalid") from exc

        rows = await self.db.execute(
            select(
                ApplicationAgentStudioFileArtifact,
                ApplicationAgent,
                ApplicationAgentStudioSession,
            )
            .join(
                ApplicationAgentStudioSession,
                ApplicationAgentStudioSession.id
                == ApplicationAgentStudioFileArtifact.studio_session_id,
            )
            .join(
                ApplicationAgent,
                ApplicationAgent.id == ApplicationAgentStudioSession.application_agent_id,
            )
            .where(
                ApplicationAgentStudioFileArtifact.id == file_id,
                ApplicationAgent.id == int(getattr(binding, "application_agent_id") or 0),
                ApplicationAgent.org_id == int(getattr(binding, "org_id") or 0),
                ApplicationAgentStudioSession.runtime_session_id == session_runtime_id,
                ApplicationAgentStudioSession.status == "active",
                ApplicationAgentStudioFileArtifact.relative_path == normalized,
            )
        )
        row = rows.first()
        if row is None:
            raise ChatFilePermissionError("Runtime file metadata is unavailable")
        artifact, _, session = row
        if int(payload.get("user_id") or 0) != int(session.user_id):
            raise ChatFilePermissionError("Runtime file projection binding mismatch")

        if projection_type == "file.failed":
            if artifact.status == "active":
                return
            if artifact.status != "pending":
                raise ChatFilePermissionError("Runtime file projection is invalid")
            artifact.status = "failed"
            await self.db.flush()
            return

        size_bytes = _positive_int(payload.get("size_bytes"))
        sha256_digest = str(payload.get("sha256") or "")
        if size_bytes is None or _SHA256_DIGEST.fullmatch(sha256_digest) is None:
            raise ChatFilePermissionError("Runtime file projection is invalid")
        if artifact.status == "active":
            if (
                int(artifact.size_bytes) != size_bytes
                or str(artifact.sha256_digest or "") != sha256_digest
            ):
                raise ChatFilePermissionError("Runtime file projection conflicts")
            return
        if artifact.status != "pending":
            raise ChatFilePermissionError("Runtime file projection is invalid")
        artifact.size_bytes = size_bytes
        artifact.sha256_digest = sha256_digest
        if payload.get("mime_type"):
            artifact.mime_type = str(payload["mime_type"])
        artifact.status = "active"
        await self.db.flush()

    async def materialize_artifact_event(
        self,
        *,
        binding: object,
        run: ApplicationAgentRun,
        event_name: str,
        event_data: dict,
    ) -> None:
        if event_name not in {
            "chat.artifact.created",
            "chat.artifact.updated",
            "chat.artifact.deleted",
        }:
            raise ChatFileError("unsupported Runtime artifact projection")
        if (
            int(run.org_id) != int(getattr(binding, "org_id") or 0)
            or int(run.application_agent_id) != int(getattr(binding, "application_agent_id") or 0)
            or int(run.runtime_binding_id) != int(getattr(binding, "id") or 0)
        ):
            raise ChatFilePermissionError("artifact run binding mismatch")
        session = await self.db.scalar(
            select(ApplicationAgentStudioSession).where(
                ApplicationAgentStudioSession.org_id == int(run.org_id),
                ApplicationAgentStudioSession.application_agent_id == int(run.application_agent_id),
                ApplicationAgentStudioSession.user_id == int(run.subject_user_id or 0),
                ApplicationAgentStudioSession.runtime_session_id == str(run.runtime_session_id),
                ApplicationAgentStudioSession.status == "active",
            )
        )
        if session is None:
            raise ChatFilePermissionError("artifact studio session is unavailable")
        raw_path = event_data.get("relative_path")
        if not isinstance(raw_path, str):
            raise ChatFilePermissionError("artifact relative path is missing")
        try:
            normalized = normalize_workspace_relative_path(raw_path)
            normalized_path = PurePosixPath(normalized)
            root = PurePosixPath(
                "workspace",
                "sessions",
                _safe_session_segment(str(session.runtime_session_id)),
            )
            normalized_path.relative_to(root)
            if len(normalized_path.parts) <= len(root.parts):
                raise ValueError("artifact path is not a file")
        except (ChatFileError, ValueError) as exc:
            raise ChatFilePermissionError("artifact escapes session root") from exc

        matches = (
            (
                await self.db.execute(
                    select(ApplicationAgentStudioFileArtifact)
                    .where(
                        ApplicationAgentStudioFileArtifact.studio_session_id == int(session.id),
                        ApplicationAgentStudioFileArtifact.relative_path == normalized,
                    )
                    .order_by(ApplicationAgentStudioFileArtifact.id.desc())
                )
            )
            .scalars()
            .all()
        )
        if event_name == "chat.artifact.deleted":
            for artifact in matches:
                artifact.status = "deleted"
            await self.db.flush()
            return

        size = event_data.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ChatFilePermissionError("artifact size is invalid")
        mime_type = event_data.get("mime_type")
        if mime_type is not None and not isinstance(mime_type, str):
            raise ChatFilePermissionError("artifact mime type is invalid")
        if matches:
            artifact = matches[0]
            for duplicate in matches[1:]:
                duplicate.status = "deleted"
        else:
            artifact = ApplicationAgentStudioFileArtifact(
                studio_session_id=int(session.id),
                application_agent_run_id=int(run.id),
                relative_path=normalized,
                size_bytes=0,
                source_type="runtime",
                status="active",
            )
            self.db.add(artifact)
        artifact.application_agent_run_id = int(run.id)
        artifact.mime_type = mime_type
        artifact.size_bytes = size
        artifact.source_type = "runtime"
        artifact.status = "active"
        await self.db.flush()

    async def _session_context(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        runtime_session_id: str,
    ) -> tuple[ApplicationAgent, ApplicationAgentStudioSession]:
        row = (
            await self.db.execute(
                select(ApplicationAgent, ApplicationAgentStudioSession)
                .join(
                    ApplicationAgentStudioSession,
                    ApplicationAgentStudioSession.application_agent_id == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.id == application_agent_id,
                    ApplicationAgent.org_id == principal.org_id,
                    ApplicationAgent.created_by_user_id == principal.user_id,
                    ApplicationAgent.lifecycle_status == "active",
                    ApplicationAgentStudioSession.org_id == principal.org_id,
                    ApplicationAgentStudioSession.application_agent_id == application_agent_id,
                    ApplicationAgentStudioSession.user_id == principal.user_id,
                    ApplicationAgentStudioSession.runtime_session_id == runtime_session_id,
                    ApplicationAgentStudioSession.status == "active",
                )
            )
        ).first()
        if row is None:
            raise ChatFilePermissionError("runtime workspace not found")
        return row

    async def _owned_artifact(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        file_id: int,
        statuses: tuple[str, ...],
    ) -> tuple[
        ApplicationAgentStudioFileArtifact,
        ApplicationAgent,
        ApplicationAgentStudioSession,
        ApplicationAgentRun | None,
    ]:
        rows = await self._owned_artifact_rows(
            principal=principal,
            application_agent_id=application_agent_id,
            statuses=statuses,
            file_ids=[file_id],
        )
        if not rows:
            raise ChatFilePermissionError("chat file not found")
        return rows[0]

    async def _owned_artifact_rows(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        statuses: tuple[str, ...],
        file_ids: list[int],
    ) -> list[
        tuple[
            ApplicationAgentStudioFileArtifact,
            ApplicationAgent,
            ApplicationAgentStudioSession,
            ApplicationAgentRun | None,
        ]
    ]:
        if not file_ids:
            return []
        return await self._artifact_rows(
            principal=principal,
            application_agent_id=application_agent_id,
            studio_session_id=None,
            statuses=statuses,
            file_ids=file_ids,
            runtime_session_id=None,
        )

    async def _artifact_rows(
        self,
        *,
        principal: Principal,
        application_agent_id: int,
        studio_session_id: int | None,
        statuses: tuple[str, ...],
        file_ids: list[int],
        runtime_session_id: str | None = None,
    ):
        statement = (
            select(
                ApplicationAgentStudioFileArtifact,
                ApplicationAgent,
                ApplicationAgentStudioSession,
                ApplicationAgentRun,
            )
            .join(
                ApplicationAgentStudioSession,
                ApplicationAgentStudioSession.id
                == ApplicationAgentStudioFileArtifact.studio_session_id,
            )
            .join(
                ApplicationAgent,
                ApplicationAgent.id == ApplicationAgentStudioSession.application_agent_id,
            )
            .outerjoin(
                ApplicationAgentRun,
                ApplicationAgentRun.id
                == ApplicationAgentStudioFileArtifact.application_agent_run_id,
            )
            .where(
                ApplicationAgentStudioFileArtifact.id.in_(file_ids),
                ApplicationAgent.id == application_agent_id,
                ApplicationAgent.org_id == principal.org_id,
                ApplicationAgent.created_by_user_id == principal.user_id,
                ApplicationAgent.lifecycle_status == "active",
                ApplicationAgentStudioSession.org_id == principal.org_id,
                ApplicationAgentStudioSession.user_id == principal.user_id,
            )
        )
        if studio_session_id is not None:
            statement = statement.where(
                ApplicationAgentStudioFileArtifact.studio_session_id == studio_session_id
            )
        if runtime_session_id is not None:
            statement = statement.where(
                ApplicationAgentStudioSession.runtime_session_id == runtime_session_id,
                ApplicationAgentStudioSession.status == "active",
            )
        statement = statement.where(ApplicationAgentStudioFileArtifact.status.in_(statuses))
        return (await self.db.execute(statement)).all()

    @staticmethod
    def _record(
        agent: ApplicationAgent,
        session: ApplicationAgentStudioSession,
        run: ApplicationAgentRun | None,
        artifact: ApplicationAgentStudioFileArtifact,
    ) -> ChatFileRecord:
        relative_path = normalize_workspace_relative_path(str(artifact.relative_path))
        return ChatFileRecord(
            file_id=int(artifact.id),
            runtime_workspace_id=int(agent.id),
            runtime_session_id=str(session.runtime_session_id),
            runtime_run_id=str(run.runtime_run_id) if run is not None else None,
            relative_path=relative_path,
            display_name=display_name_for_path(relative_path),
            mime_type=str(artifact.mime_type) if artifact.mime_type else None,
            size_bytes=int(artifact.size_bytes),
            source_type=str(artifact.source_type),
            created_at=artifact.created_at,
            status=str(artifact.status),
            sha256_digest=(str(artifact.sha256_digest) if artifact.sha256_digest else None),
        )


def _safe_session_segment(runtime_session_id: str) -> str:
    if _SAFE_SESSION_SEGMENT.fullmatch(runtime_session_id) is None:
        raise ChatFileError("runtime_session_id is invalid")
    return runtime_session_id


def _safe_upload_filename(raw_name: str) -> str:
    name = str(raw_name).strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or "\x00" in name or len(name) > 255:
        raise ChatFileError("upload filename is invalid")
    return name


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
