from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatFileArtifact, CloudHermesProfile, RuntimeRun, RuntimeSession
from app.models.runtime_agent_profile import RuntimeAgentProfile
from app.hermes_runtime.constants import runtime_session_workspace_path
from app.services.runtime_workspace import Principal


ALLOWED_SOURCE_TYPES = {"upload", "tool", "export"}
FORBIDDEN_PATH_NAMES = {".env", "config.yaml"}
FORBIDDEN_DIRECTORIES = {"home", "logs", "log", ".work", ".venv", "__pycache__"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


class ChatFileError(ValueError):
    pass


class ChatFilePermissionError(PermissionError):
    pass


class ChatFileSessionDeletedError(ChatFilePermissionError):
    code = "expert_session_deleted"


@dataclass(frozen=True)
class ChatFileRecord:
    file_id: int
    runtime_workspace_id: int
    runtime_session_id: str | None
    runtime_run_id: str | None
    relative_path: str
    display_name: str
    mime_type: str | None
    size_bytes: int
    source_type: str
    created_at: object | None = None
    status: str = "active"
    sha256_digest: str | None = None


@dataclass(frozen=True)
class RegisterChatFileCommand:
    runtime_workspace_id: int
    runtime_session_id: str | None
    runtime_run_id: str | None
    relative_path: str
    mime_type: str | None
    size_bytes: int
    source_type: str


class ChatFileStore(Protocol):
    async def session_workspace_relative_root(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str:
        ...

    async def register_file(
        self,
        *,
        org_id: int,
        user_id: int,
        command: RegisterChatFileCommand,
    ) -> ChatFileRecord:
        ...

    async def reserve_upload(
        self,
        *,
        org_id: int,
        user_id: int,
        command: RegisterChatFileCommand,
    ) -> ChatFileRecord:
        ...

    async def list_files(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
        runtime_run_id: str | None = None,
    ) -> list[ChatFileRecord]:
        ...

    async def get_file(
        self,
        *,
        org_id: int,
        user_id: int,
        file_id: int,
    ) -> ChatFileRecord:
        ...

    async def get_files(
        self,
        *,
        org_id: int,
        user_id: int,
        file_ids: list[int],
    ) -> list[ChatFileRecord]:
        ...

    async def rename_file(
        self,
        *,
        org_id: int,
        user_id: int,
        file_id: int,
        relative_path: str,
    ) -> ChatFileRecord:
        ...

    async def delete_file(
        self,
        *,
        org_id: int,
        user_id: int,
        file_id: int,
    ) -> ChatFileRecord:
        ...


class ChatFileIndexService:
    def __init__(self, store: ChatFileStore) -> None:
        self.store = store

    async def scoped_session_relative_path(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
        relative_path: str,
    ) -> str:
        normalized_path = normalize_workspace_relative_path(relative_path)
        session_root = await self.store.session_workspace_relative_root(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
        )
        return session_scoped_relative_path(normalized_path, session_root)

    async def register_file(self, *, principal: Principal, command: RegisterChatFileCommand) -> ChatFileRecord:
        relative_path = normalize_workspace_relative_path(command.relative_path)
        if command.runtime_session_id is not None:
            relative_path = await self.scoped_session_relative_path(
                principal=principal,
                runtime_workspace_id=command.runtime_workspace_id,
                runtime_session_id=command.runtime_session_id,
                relative_path=relative_path,
            )
        command = RegisterChatFileCommand(
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            runtime_run_id=command.runtime_run_id,
            relative_path=relative_path,
            mime_type=command.mime_type,
            size_bytes=validate_size(command.size_bytes),
            source_type=validate_source_type(command.source_type),
        )
        return await self.store.register_file(
            org_id=principal.org_id,
            user_id=principal.user_id,
            command=command,
        )

    async def reserve_upload(
        self,
        *,
        principal: Principal,
        command: RegisterChatFileCommand,
    ) -> ChatFileRecord:
        if command.runtime_session_id is None:
            raise ChatFileError("runtime_session_id is required")
        relative_path = await self.scoped_session_relative_path(
            principal=principal,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            relative_path=normalize_workspace_relative_path(
                command.relative_path
            ),
        )
        return await self.store.reserve_upload(
            org_id=principal.org_id,
            user_id=principal.user_id,
            command=RegisterChatFileCommand(
                runtime_workspace_id=command.runtime_workspace_id,
                runtime_session_id=command.runtime_session_id,
                runtime_run_id=None,
                relative_path=relative_path,
                mime_type=command.mime_type,
                size_bytes=validate_size(command.size_bytes),
                source_type="upload",
            ),
        )

    async def assert_writable_session(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        check = getattr(self.store, "assert_writable_session", None)
        if callable(check):
            await check(
                org_id=principal.org_id,
                user_id=principal.user_id,
                runtime_workspace_id=runtime_workspace_id,
                runtime_session_id=runtime_session_id,
            )

    async def list_files(
        self,
        *,
        principal: Principal,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
        runtime_run_id: str | None = None,
    ) -> list[ChatFileRecord]:
        records = await self.store.list_files(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            runtime_run_id=runtime_run_id,
        )
        latest: dict[tuple[str | None, str], ChatFileRecord] = {}
        for record in sorted(records, key=lambda item: item.file_id, reverse=True):
            if any(
                part in FORBIDDEN_DIRECTORIES
                for part in PurePosixPath(record.relative_path).parts
            ):
                continue
            latest.setdefault((record.runtime_session_id, record.relative_path), record)
        return list(latest.values())

    async def get_file(self, *, principal: Principal, file_id: int) -> ChatFileRecord:
        return await self.store.get_file(org_id=principal.org_id, user_id=principal.user_id, file_id=file_id)

    async def get_files(self, *, principal: Principal, file_ids: list[int]) -> list[ChatFileRecord]:
        ordered_ids = list(dict.fromkeys(file_ids))
        if not ordered_ids:
            return []
        records = await self.store.get_files(
            org_id=principal.org_id,
            user_id=principal.user_id,
            file_ids=ordered_ids,
        )
        by_id = {record.file_id: record for record in records}
        try:
            return [by_id[file_id] for file_id in ordered_ids]
        except KeyError as exc:
            raise ChatFilePermissionError("chat file not found") from exc

    async def rename_file(self, *, principal: Principal, file_id: int, relative_path: str) -> ChatFileRecord:
        record = await self.get_file(principal=principal, file_id=file_id)
        normalized_path = normalize_workspace_relative_path(relative_path)
        if record.runtime_session_id is not None:
            normalized_path = await self.scoped_session_relative_path(
                principal=principal,
                runtime_workspace_id=record.runtime_workspace_id,
                runtime_session_id=record.runtime_session_id,
                relative_path=normalized_path,
            )
        selected: ChatFileRecord | None = None
        for identity_record in await self._identity_records(principal=principal, record=record):
            renamed = await self.store.rename_file(
                org_id=principal.org_id,
                user_id=principal.user_id,
                file_id=identity_record.file_id,
                relative_path=normalized_path,
            )
            if identity_record.file_id == file_id:
                selected = renamed
        if selected is None:
            raise ChatFilePermissionError("chat file not found")
        return selected

    async def delete_file(self, *, principal: Principal, file_id: int) -> ChatFileRecord:
        record = await self.get_file(principal=principal, file_id=file_id)
        selected: ChatFileRecord | None = None
        for identity_record in await self._identity_records(principal=principal, record=record):
            deleted = await self.store.delete_file(
                org_id=principal.org_id,
                user_id=principal.user_id,
                file_id=identity_record.file_id,
            )
            if identity_record.file_id == file_id:
                selected = deleted
        if selected is None:
            raise ChatFilePermissionError("chat file not found")
        return selected

    async def _identity_records(
        self,
        *,
        principal: Principal,
        record: ChatFileRecord,
    ) -> list[ChatFileRecord]:
        records = await self.store.list_files(
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=record.runtime_workspace_id,
            runtime_session_id=record.runtime_session_id,
        )
        return [item for item in records if item.relative_path == record.relative_path]


def normalize_workspace_relative_path(raw_path: str) -> str:
    if not raw_path or not raw_path.strip():
        raise ChatFileError("relative_path is required")
    if "\\" in raw_path:
        raise ChatFileError("relative_path must use POSIX separators")
    path = PurePosixPath(raw_path.strip())
    if path.is_absolute():
        raise ChatFileError("relative_path must not be absolute")
    parts = path.parts
    if not parts or parts[0] != "workspace":
        raise ChatFileError("relative_path must be under workspace/")
    if len(parts) == 1:
        raise ChatFileError("relative_path must point to a file under workspace/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ChatFileError("relative_path must not contain traversal segments")
    lowered = [part.lower() for part in parts]
    for part in lowered:
        if part in FORBIDDEN_PATH_NAMES:
            raise ChatFileError(f"forbidden file path: {part}")
        if part in FORBIDDEN_DIRECTORIES:
            raise ChatFileError(f"forbidden directory path: {part}")
        if any(part.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            raise ChatFileError(f"forbidden sqlite path: {part}")
    return path.as_posix()


def session_scoped_relative_path(relative_path: str, session_root: str) -> str:
    path = PurePosixPath(relative_path)
    root = PurePosixPath(session_root)
    if path.parts[:len(root.parts)] == root.parts:
        return path.as_posix()
    if path.parts[:2] in {("workspace", "sessions"), ("workspace", "experts")}:
        raise ChatFileError("relative_path must stay within the current session directory")
    return PurePosixPath(*root.parts, *path.parts[1:]).as_posix()


def session_workspace_relative_root(
    runtime_session_id: str,
    *,
    runtime_agent_profile_id: int | None = None,
    hermes_profile_name: str = "default",
) -> str:
    root = runtime_session_workspace_path(
        "workspace",
        runtime_session_id,
        runtime_agent_profile_id=runtime_agent_profile_id,
        hermes_profile_name=hermes_profile_name,
    )
    if root is None:
        raise ChatFileError("runtime session workspace is unavailable")
    return normalize_workspace_relative_path(root)


def validate_source_type(source_type: str) -> str:
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ChatFileError("source_type must be upload, tool, or export")
    return source_type


def validate_size(size_bytes: int) -> int:
    if size_bytes < 0:
        raise ChatFileError("size_bytes must be non-negative")
    return size_bytes


def display_name_for_path(relative_path: str) -> str:
    return PurePosixPath(relative_path).name


class SqlAlchemyChatFileStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def session_workspace_relative_root(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> str:
        result = await self.db.execute(
            select(RuntimeSession, RuntimeAgentProfile)
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.id == RuntimeSession.runtime_agent_profile_id,
            )
            .where(
                RuntimeSession.org_id == org_id,
                RuntimeSession.user_id == user_id,
                RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                RuntimeSession.runtime_session_id == runtime_session_id,
                RuntimeSession.status.in_(("active", "expert_deleted")),
            )
        )
        row = result.first()
        if row is None:
            raise ChatFilePermissionError("runtime session not found")
        session, profile = row
        return session_workspace_relative_root(
            str(session.runtime_session_id),
            runtime_agent_profile_id=int(profile.id),
            hermes_profile_name=str(profile.hermes_profile_name),
        )

    async def register_file(
        self,
        *,
        org_id: int,
        user_id: int,
        command: RegisterChatFileCommand,
    ) -> ChatFileRecord:
        session_id, run_id = await self._resolve_context(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            runtime_run_id=command.runtime_run_id,
            require_writable_session=True,
        )
        session_filter = (
            ChatFileArtifact.session_id.is_(None)
            if session_id is None
            else ChatFileArtifact.session_id == session_id
        )
        matches = (
            await self.db.execute(
                select(ChatFileArtifact)
                .where(
                    ChatFileArtifact.org_id == org_id,
                    ChatFileArtifact.user_id == user_id,
                    ChatFileArtifact.runtime_workspace_id == command.runtime_workspace_id,
                    session_filter,
                    ChatFileArtifact.relative_path == command.relative_path,
                    ChatFileArtifact.status == "active",
                )
                .order_by(ChatFileArtifact.id.desc())
            )
        ).scalars().all()
        if matches:
            artifact = matches[0]
            artifact.run_id = run_id
            artifact.mime_type = command.mime_type
            artifact.size_bytes = command.size_bytes
            artifact.source_type = command.source_type
            artifact.visibility = "owner"
            artifact.status = "active"
            for duplicate in matches[1:]:
                duplicate.status = "deleted"
        else:
            artifact = ChatFileArtifact(
                org_id=org_id,
                user_id=user_id,
                runtime_workspace_id=command.runtime_workspace_id,
                session_id=session_id,
                run_id=run_id,
                relative_path=command.relative_path,
                mime_type=command.mime_type,
                size_bytes=command.size_bytes,
                source_type=command.source_type,
                visibility="owner",
                status="active",
            )
            self.db.add(artifact)
        await self.db.commit()
        await self.db.refresh(artifact)
        return await self._record_for_artifact(artifact)

    async def reserve_upload(
        self,
        *,
        org_id: int,
        user_id: int,
        command: RegisterChatFileCommand,
    ) -> ChatFileRecord:
        await self.db.execute(
            update(ChatFileArtifact)
            .where(
                ChatFileArtifact.status == "pending",
                ChatFileArtifact.created_at
                < datetime.utcnow() - timedelta(minutes=10),
            )
            .values(status="failed")
        )
        session_id, _ = await self._resolve_context(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=command.runtime_workspace_id,
            runtime_session_id=command.runtime_session_id,
            runtime_run_id=None,
            require_writable_session=True,
        )
        requested_path = PurePosixPath(command.relative_path)
        artifact = ChatFileArtifact(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=command.runtime_workspace_id,
            session_id=session_id,
            run_id=None,
            relative_path=(
                requested_path.parent
                / ".pending"
                / requested_path.name
            ).as_posix(),
            mime_type=command.mime_type,
            size_bytes=command.size_bytes,
            source_type="upload",
            visibility="owner",
            status="pending",
        )
        self.db.add(artifact)
        await self.db.flush()
        artifact.relative_path = (
            requested_path.parent
            / str(int(artifact.id))
            / requested_path.name
        ).as_posix()
        await self.db.commit()
        await self.db.refresh(artifact)
        return await self._record_for_artifact(artifact)

    async def list_files(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None = None,
        runtime_run_id: str | None = None,
    ) -> list[ChatFileRecord]:
        session_id, run_id = await self._resolve_context(
            org_id=org_id,
            user_id=user_id,
            runtime_workspace_id=runtime_workspace_id,
            runtime_session_id=runtime_session_id,
            runtime_run_id=runtime_run_id,
        )
        statement = select(ChatFileArtifact).where(
            ChatFileArtifact.org_id == org_id,
            ChatFileArtifact.user_id == user_id,
            ChatFileArtifact.runtime_workspace_id == runtime_workspace_id,
            ChatFileArtifact.status == "active",
        )
        if runtime_session_id is not None:
            statement = statement.where(ChatFileArtifact.session_id == session_id)
        if runtime_run_id is not None:
            statement = statement.where(ChatFileArtifact.run_id == run_id)
        statement = statement.order_by(ChatFileArtifact.created_at.desc(), ChatFileArtifact.id.desc())
        artifacts = (await self.db.execute(statement)).scalars().all()
        return [await self._record_for_artifact(artifact) for artifact in artifacts]

    async def get_file(self, *, org_id: int, user_id: int, file_id: int) -> ChatFileRecord:
        artifact = await self._owned_artifact(org_id=org_id, user_id=user_id, file_id=file_id)
        return await self._record_for_artifact(artifact)

    async def get_files(
        self,
        *,
        org_id: int,
        user_id: int,
        file_ids: list[int],
    ) -> list[ChatFileRecord]:
        if not file_ids:
            return []
        rows = (
            await self.db.execute(
                select(
                    ChatFileArtifact,
                    RuntimeSession.runtime_session_id.label("session_runtime_session_id"),
                    RuntimeRun.runtime_run_id.label("runtime_run_id"),
                    RuntimeRun.runtime_session_id.label("run_runtime_session_id"),
                )
                .outerjoin(RuntimeSession, RuntimeSession.id == ChatFileArtifact.session_id)
                .outerjoin(RuntimeRun, RuntimeRun.id == ChatFileArtifact.run_id)
                .where(
                    ChatFileArtifact.id.in_(file_ids),
                    ChatFileArtifact.org_id == org_id,
                    ChatFileArtifact.user_id == user_id,
                    ChatFileArtifact.status == "active",
                )
            )
        ).all()
        return [
            self._record_from_values(
                artifact,
                runtime_session_id=session_runtime_session_id or run_runtime_session_id,
                runtime_run_id=runtime_run_id,
            )
            for artifact, session_runtime_session_id, runtime_run_id, run_runtime_session_id in rows
        ]

    async def rename_file(self, *, org_id: int, user_id: int, file_id: int, relative_path: str) -> ChatFileRecord:
        artifact = await self._owned_artifact(org_id=org_id, user_id=user_id, file_id=file_id)
        await self._assert_artifact_writable(artifact)
        artifact.relative_path = relative_path
        await self.db.commit()
        await self.db.refresh(artifact)
        return await self._record_for_artifact(artifact)

    async def delete_file(self, *, org_id: int, user_id: int, file_id: int) -> ChatFileRecord:
        artifact = await self._owned_artifact(org_id=org_id, user_id=user_id, file_id=file_id)
        await self._assert_artifact_writable(artifact)
        artifact.status = "deleted"
        await self.db.commit()
        await self.db.refresh(artifact)
        return await self._record_for_artifact(artifact)

    async def _resolve_context(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str | None,
        runtime_run_id: str | None,
        require_writable_session: bool = False,
    ) -> tuple[int | None, int | None]:
        profile = (
            await self.db.execute(
                select(CloudHermesProfile).where(
                    CloudHermesProfile.id == runtime_workspace_id,
                    CloudHermesProfile.org_id == org_id,
                    CloudHermesProfile.user_id == user_id,
                    CloudHermesProfile.status == "active",
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise ChatFilePermissionError("runtime workspace not found")

        session: RuntimeSession | None = None
        if runtime_session_id is not None:
            session = (
                await self.db.execute(
                    select(RuntimeSession).where(
                        RuntimeSession.org_id == org_id,
                        RuntimeSession.user_id == user_id,
                        RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                        RuntimeSession.runtime_session_id == runtime_session_id,
                        RuntimeSession.status.in_(("active", "expert_deleted")),
                    )
                )
            ).scalar_one_or_none()
            if session is None:
                raise ChatFilePermissionError("runtime session not found")
            if require_writable_session and session.status == "expert_deleted":
                raise ChatFileSessionDeletedError(ChatFileSessionDeletedError.code)

        run: RuntimeRun | None = None
        if runtime_run_id is not None:
            run = (
                await self.db.execute(
                    select(RuntimeRun).where(
                        RuntimeRun.org_id == org_id,
                        RuntimeRun.user_id == user_id,
                        RuntimeRun.runtime_workspace_id == runtime_workspace_id,
                        RuntimeRun.runtime_run_id == runtime_run_id,
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                raise ChatFilePermissionError("runtime run not found")
            if session is not None and run.session_id != session.id:
                raise ChatFilePermissionError("runtime run does not belong to runtime session")

        return (
            int(session.id) if session is not None else None,
            int(run.id) if run is not None else None,
        )

    async def assert_writable_session(
        self,
        *,
        org_id: int,
        user_id: int,
        runtime_workspace_id: int,
        runtime_session_id: str,
    ) -> None:
        session = (
            await self.db.execute(
                select(RuntimeSession).where(
                    RuntimeSession.org_id == org_id,
                    RuntimeSession.user_id == user_id,
                    RuntimeSession.runtime_workspace_id == runtime_workspace_id,
                    RuntimeSession.runtime_session_id == runtime_session_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            raise ChatFilePermissionError("runtime session not found")
        if session.status == "expert_deleted":
            raise ChatFileSessionDeletedError(ChatFileSessionDeletedError.code)
        if session.status != "active":
            raise ChatFilePermissionError("runtime session not found")

    async def _assert_artifact_writable(self, artifact: ChatFileArtifact) -> None:
        session_id = artifact.session_id
        if session_id is None and artifact.run_id is not None:
            session_id = await self.db.scalar(
                select(RuntimeRun.session_id).where(RuntimeRun.id == artifact.run_id)
            )
        if session_id is None:
            return
        session_status = await self.db.scalar(
            select(RuntimeSession.status).where(RuntimeSession.id == session_id)
        )
        if session_status == "expert_deleted":
            raise ChatFileSessionDeletedError(ChatFileSessionDeletedError.code)

    async def _owned_artifact(self, *, org_id: int, user_id: int, file_id: int) -> ChatFileArtifact:
        artifact = (
            await self.db.execute(
                select(ChatFileArtifact).where(
                    ChatFileArtifact.id == file_id,
                    ChatFileArtifact.org_id == org_id,
                    ChatFileArtifact.user_id == user_id,
                    ChatFileArtifact.status == "active",
                )
            )
        ).scalar_one_or_none()
        if artifact is None:
            raise ChatFilePermissionError("chat file not found")
        normalize_workspace_relative_path(str(artifact.relative_path))
        return artifact

    async def _record_for_artifact(self, artifact: ChatFileArtifact) -> ChatFileRecord:
        runtime_session_id: str | None = None
        runtime_run_id: str | None = None
        if artifact.session_id is not None:
            session = await self.db.get(RuntimeSession, artifact.session_id)
            runtime_session_id = str(session.runtime_session_id) if session else None
        if artifact.run_id is not None:
            run = await self.db.get(RuntimeRun, artifact.run_id)
            runtime_run_id = str(run.runtime_run_id) if run else None
            if runtime_session_id is None and run and run.runtime_session_id:
                runtime_session_id = str(run.runtime_session_id)
        return self._record_from_values(
            artifact,
            runtime_session_id=runtime_session_id,
            runtime_run_id=runtime_run_id,
        )

    @staticmethod
    def _record_from_values(
        artifact: ChatFileArtifact,
        *,
        runtime_session_id: str | None,
        runtime_run_id: str | None,
    ) -> ChatFileRecord:
        relative_path = normalize_workspace_relative_path(str(artifact.relative_path))
        return ChatFileRecord(
            file_id=int(artifact.id),
            runtime_workspace_id=int(artifact.runtime_workspace_id),
            runtime_session_id=str(runtime_session_id) if runtime_session_id else None,
            runtime_run_id=str(runtime_run_id) if runtime_run_id else None,
            relative_path=relative_path,
            display_name=display_name_for_path(relative_path),
            mime_type=str(artifact.mime_type) if artifact.mime_type else None,
            size_bytes=int(artifact.size_bytes),
            source_type=str(artifact.source_type),
            created_at=artifact.created_at,
            status=str(getattr(artifact, "status", "active")),
            sha256_digest=(
                str(getattr(artifact, "sha256_digest", None))
                if getattr(artifact, "sha256_digest", None)
                else None
            ),
        )
