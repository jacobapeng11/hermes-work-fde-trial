from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.hermes_runtime.dashboard_protocol import ManagedRuntimeArtifact
from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentConversation,
    ApplicationAgentConversationFileArtifact,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentRun,
    ApplicationClient,
    User,
)
from app.services.chat_files import (
    ChatFileError,
    display_name_for_path,
    normalize_workspace_relative_path,
)
from app.services.file_url_signer import sign_file_download_url
from app.services.runtime_workspace import Principal


_SAFE_SESSION_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ApplicationAgentConversationFileError(RuntimeError):
    def __init__(self, code: str, *, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ApplicationAgentConversationFileRecord:
    file_id: int
    relative_path: str
    display_name: str
    mime_type: str | None
    size_bytes: int
    source_type: str
    runtime_run_id: str
    created_at: datetime | None


@dataclass(frozen=True)
class ApplicationAgentConversationFileDownload:
    agent: ApplicationAgent
    binding: ApplicationAgentRuntimeBinding
    record: ApplicationAgentConversationFileRecord


class ApplicationAgentConversationFileService:
    """MySQL index and authorization boundary for OpenAPI Conversation files."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def sync_runtime_artifacts(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        conversation: ApplicationAgentConversation,
        run: ApplicationAgentRun,
        artifacts: list[ManagedRuntimeArtifact],
    ) -> list[ApplicationAgentConversationFileRecord]:
        self._validate_sync_context(
            agent=agent,
            binding=binding,
            conversation=conversation,
            run=run,
        )
        normalized_by_path: dict[str, ManagedRuntimeArtifact] = {}
        for artifact in artifacts:
            normalized = self._conversation_scoped_path(
                artifact.relative_path,
                runtime_session_id=str(conversation.runtime_session_id),
            )
            if normalized in normalized_by_path:
                raise ApplicationAgentConversationFileError(
                    "application_agent_file_path_conflict",
                    status_code=409,
                )
            if artifact.mime_type is not None:
                if not isinstance(artifact.mime_type, str) or len(
                    artifact.mime_type
                ) > 255:
                    raise ApplicationAgentConversationFileError(
                        "application_agent_file_metadata_invalid",
                        status_code=502,
                    )
            if (
                isinstance(artifact.size_bytes, bool)
                or not isinstance(artifact.size_bytes, int)
                or artifact.size_bytes < 0
            ):
                raise ApplicationAgentConversationFileError(
                    "application_agent_file_metadata_invalid",
                    status_code=502,
                )
            normalized_by_path[normalized] = artifact

        existing_rows = (
            (
                await self.db.execute(
                    select(ApplicationAgentConversationFileArtifact).where(
                        ApplicationAgentConversationFileArtifact.conversation_id
                        == int(conversation.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_by_path = {
            str(row.relative_path): row for row in existing_rows
        }
        active_paths = set(normalized_by_path)

        for relative_path, artifact in normalized_by_path.items():
            row = existing_by_path.get(relative_path)
            path_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
            changed = (
                row is None
                or str(row.status) != "active"
                or str(row.mime_type or "") != str(artifact.mime_type or "")
                or int(row.size_bytes) != int(artifact.size_bytes)
                or self._mtime_is_at_or_after_run_start(
                    artifact=artifact,
                    run=run,
                )
            )
            if row is None:
                row = ApplicationAgentConversationFileArtifact(
                    org_id=int(agent.org_id),
                    application_agent_id=int(agent.id),
                    conversation_id=int(conversation.id),
                    application_agent_run_id=int(run.id),
                    relative_path=relative_path,
                    relative_path_sha256=path_digest,
                    source_type="runtime",
                    status="active",
                )
                self.db.add(row)
                existing_by_path[relative_path] = row
            if changed:
                row.application_agent_run_id = int(run.id)
            row.relative_path = relative_path
            row.relative_path_sha256 = path_digest
            row.mime_type = artifact.mime_type
            row.size_bytes = int(artifact.size_bytes)
            row.source_type = "runtime"
            row.status = "active"

        for relative_path, row in existing_by_path.items():
            if relative_path not in active_paths and row.status == "active":
                row.status = "deleted"

        await self.db.flush()
        rows = (
            (
                await self.db.execute(
                    select(ApplicationAgentConversationFileArtifact, ApplicationAgentRun)
                    .join(
                        ApplicationAgentRun,
                        ApplicationAgentRun.id
                        == ApplicationAgentConversationFileArtifact.application_agent_run_id,
                    )
                    .where(
                        ApplicationAgentConversationFileArtifact.conversation_id
                        == int(conversation.id),
                        ApplicationAgentConversationFileArtifact.status == "active",
                        ApplicationAgentConversationFileArtifact.application_agent_run_id
                        == int(run.id),
                    )
                    .order_by(
                        ApplicationAgentConversationFileArtifact.relative_path.asc(),
                        ApplicationAgentConversationFileArtifact.id.asc(),
                    )
                )
            )
            .all()
        )
        return [self._record(row, run_row) for row, run_row in rows]

    async def list_files(
        self,
        *,
        slug: str,
        client_id: str,
        client_secret: str,
        delegated_principal: Principal | None,
        conversation_key: str,
        external_user_id: str | None = None,
        runtime_run_id: str | None = None,
    ) -> list[ApplicationAgentConversationFileRecord]:
        agent, client, grant, conversation = await self._authorized_conversation(
            slug=slug,
            client_id=client_id,
            client_secret=client_secret,
            delegated_principal=delegated_principal,
            conversation_key=conversation_key,
            external_user_id=external_user_id,
        )
        del agent, client, grant
        statement = (
            select(
                ApplicationAgentConversationFileArtifact,
                ApplicationAgentRun,
            )
            .join(
                ApplicationAgentRun,
                ApplicationAgentRun.id
                == ApplicationAgentConversationFileArtifact.application_agent_run_id,
            )
            .where(
                ApplicationAgentConversationFileArtifact.conversation_id
                == int(conversation.id),
                ApplicationAgentConversationFileArtifact.status == "active",
            )
            .order_by(
                ApplicationAgentConversationFileArtifact.created_at.desc(),
                ApplicationAgentConversationFileArtifact.id.desc(),
            )
        )
        if runtime_run_id is not None:
            statement = statement.where(
                ApplicationAgentRun.runtime_run_id == runtime_run_id
            )
        rows = (await self.db.execute(statement)).all()
        return [self._record(row, run_row) for row, run_row in rows]

    async def file_for_download(
        self,
        *,
        slug: str,
        file_id: int,
    ) -> ApplicationAgentConversationFileDownload:
        row = (
            await self.db.execute(
                select(
                    ApplicationAgentConversationFileArtifact,
                    ApplicationAgent,
                    ApplicationAgentConversation,
                    ApplicationAgentRun,
                    ApplicationAgentRuntimeBinding,
                )
                .join(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ApplicationAgentConversationFileArtifact.application_agent_id,
                )
                .join(
                    ApplicationAgentConversation,
                    ApplicationAgentConversation.id
                    == ApplicationAgentConversationFileArtifact.conversation_id,
                )
                .join(
                    ApplicationAgentRun,
                    ApplicationAgentRun.id
                    == ApplicationAgentConversationFileArtifact.application_agent_run_id,
                )
                .join(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgentRuntimeBinding.application_agent_id
                    == ApplicationAgent.id,
                )
                .where(
                    ApplicationAgent.slug == slug,
                    ApplicationAgent.status == "published",
                    ApplicationAgent.lifecycle_status == "active",
                    ApplicationAgentRuntimeBinding.status == "ready",
                    ApplicationAgentConversationFileArtifact.id == int(file_id),
                    ApplicationAgentConversationFileArtifact.status == "active",
                )
            )
        ).first()
        if row is None:
            raise ApplicationAgentConversationFileError(
                "application_file_not_found",
                status_code=404,
            )
        artifact, agent, conversation, run, binding = row
        if (
            int(artifact.org_id) != int(agent.org_id)
            or int(artifact.application_agent_id) != int(agent.id)
            or int(artifact.conversation_id) != int(conversation.id)
            or int(conversation.application_agent_id) != int(agent.id)
            or int(artifact.application_agent_run_id) != int(run.id)
            or int(run.conversation_id or 0) != int(conversation.id)
            or str(run.runtime_session_id) != str(conversation.runtime_session_id)
        ):
            raise ApplicationAgentConversationFileError(
                "application_file_not_found",
                status_code=404,
            )
        return ApplicationAgentConversationFileDownload(
            agent=agent,
            binding=binding,
            record=self._record(artifact, run),
        )

    async def _authorized_conversation(
        self,
        *,
        slug: str,
        client_id: str,
        client_secret: str,
        delegated_principal: Principal | None,
        conversation_key: str,
        external_user_id: str | None,
    ) -> tuple[
        ApplicationAgent,
        ApplicationClient,
        ApplicationAgentClientGrant,
        ApplicationAgentConversation,
    ]:
        auth_row = (
            await self.db.execute(
                select(
                    ApplicationAgent,
                    ApplicationClient,
                    ApplicationAgentClientGrant,
                )
                .join(
                    ApplicationAgentClientGrant,
                    ApplicationAgentClientGrant.application_agent_id
                    == ApplicationAgent.id,
                )
                .join(
                    ApplicationClient,
                    ApplicationClient.id
                    == ApplicationAgentClientGrant.application_client_id,
                )
                .where(
                    ApplicationAgent.slug == slug,
                    ApplicationAgent.status == "published",
                    ApplicationAgent.lifecycle_status == "active",
                    ApplicationClient.client_id == client_id,
                    ApplicationClient.status == "active",
                    ApplicationAgentClientGrant.status == "active",
                )
            )
        ).first()
        if auth_row is None:
            raise ApplicationAgentConversationFileError(
                "application_agent_or_client_not_found",
                status_code=404,
            )
        agent, client, grant = auth_row
        digest = hashlib.sha256(client_secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(
            str(client.credential_digest_or_reference),
            digest,
        ) or "responses:create" not in set(grant.scopes or []):
            raise ApplicationAgentConversationFileError(
                "application_client_unauthorized",
                status_code=401,
            )
        if int(client.org_id) != int(agent.org_id):
            raise ApplicationAgentConversationFileError(
                "application_agent_conversation_not_found",
                status_code=404,
            )

        if delegated_principal is not None and external_user_id is not None:
            raise ApplicationAgentConversationFileError(
                "application_agent_identity_conflict",
                status_code=409,
            )
        if delegated_principal is not None:
            if (
                int(delegated_principal.org_id) != int(agent.org_id)
                or not bool(grant.allow_user_delegation)
            ):
                raise ApplicationAgentConversationFileError(
                    "delegated_user_unauthorized",
                    status_code=403,
                )
            subject_type = "user"
            subject_key = f"user:{int(delegated_principal.user_id)}"
            subject_user_id: int | None = int(delegated_principal.user_id)
            subject_service_id: str | None = None
            active_user = await self.db.scalar(
                select(User.id).where(
                    User.id == subject_user_id,
                    User.org_id == agent.org_id,
                    User.status == "active",
                )
            )
            if active_user is None:
                raise ApplicationAgentConversationFileError(
                    "delegated_user_not_found",
                    status_code=401,
                )
        elif bool(grant.allow_service_invocation):
            if external_user_id is not None:
                external_digest = hashlib.sha256(
                    f"{client.client_id}\0{external_user_id}".encode("utf-8")
                ).hexdigest()
                subject_type = "external_user"
                subject_key = f"external_user:{external_digest}"
                subject_user_id = None
                subject_service_id = external_user_id
            else:
                subject_type = "service"
                subject_key = f"service:{client.client_id}"
                subject_user_id = None
                subject_service_id = str(client.client_id)
        else:
            raise ApplicationAgentConversationFileError(
                "delegated_user_token_required",
                status_code=401,
            )

        subject_filters = [
            ApplicationAgentConversation.org_id == int(agent.org_id),
            ApplicationAgentConversation.application_agent_id == int(agent.id),
            ApplicationAgentConversation.application_client_id == int(client.id),
            ApplicationAgentConversation.conversation_key == conversation_key,
            ApplicationAgentConversation.status == "active",
            ApplicationAgentConversation.subject_type == subject_type,
            ApplicationAgentConversation.subject_key == subject_key,
        ]
        if subject_user_id is None:
            subject_filters.append(
                ApplicationAgentConversation.subject_user_id.is_(None)
            )
        else:
            subject_filters.append(
                ApplicationAgentConversation.subject_user_id == subject_user_id
            )
        if subject_service_id is None:
            subject_filters.append(
                ApplicationAgentConversation.subject_service_id.is_(None)
            )
        else:
            subject_filters.append(
                ApplicationAgentConversation.subject_service_id
                == subject_service_id
            )
        conversation = await self.db.scalar(
            select(ApplicationAgentConversation).where(*subject_filters)
        )
        if conversation is None:
            raise ApplicationAgentConversationFileError(
                "application_agent_conversation_not_found",
                status_code=404,
            )
        return agent, client, grant, conversation

    def _validate_sync_context(
        self,
        *,
        agent: ApplicationAgent,
        binding: ApplicationAgentRuntimeBinding,
        conversation: ApplicationAgentConversation,
        run: ApplicationAgentRun,
    ) -> None:
        if (
            int(binding.org_id) != int(agent.org_id)
            or int(binding.application_agent_id) != int(agent.id)
            or str(binding.status) != "ready"
            or int(conversation.org_id) != int(agent.org_id)
            or int(conversation.application_agent_id) != int(agent.id)
            or int(run.org_id) != int(agent.org_id)
            or int(run.application_agent_id) != int(agent.id)
            or int(run.runtime_binding_id) != int(binding.id)
            or int(run.conversation_id or 0) != int(conversation.id)
            or str(run.runtime_session_id) != str(conversation.runtime_session_id)
        ):
            raise ApplicationAgentConversationFileError(
                "application_agent_file_context_mismatch",
                status_code=409,
            )

    @staticmethod
    def _mtime_is_at_or_after_run_start(
        *,
        artifact: ManagedRuntimeArtifact,
        run: ApplicationAgentRun,
    ) -> bool:
        if artifact.mtime is None or run.started_at is None:
            return False
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        # Runtime and Backend clocks are normally the same host clock. Keep a
        # bounded skew so a newly overwritten file is not silently missed.
        return artifact.mtime + 300 >= started_at.timestamp()

    @staticmethod
    def _conversation_scoped_path(
        raw_path: str,
        *,
        runtime_session_id: str,
    ) -> str:
        if _SAFE_SESSION_SEGMENT.fullmatch(runtime_session_id) is None:
            raise ApplicationAgentConversationFileError(
                "application_agent_runtime_session_invalid",
                status_code=409,
            )
        try:
            normalized = normalize_workspace_relative_path(raw_path)
        except ChatFileError as exc:
            raise ApplicationAgentConversationFileError(
                "application_agent_file_path_invalid",
                status_code=409,
            ) from exc
        path = PurePosixPath(normalized)
        root = PurePosixPath("workspace", "sessions", runtime_session_id)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ApplicationAgentConversationFileError(
                "application_agent_file_path_invalid",
                status_code=409,
            ) from exc
        if len(path.parts) <= len(root.parts):
            raise ApplicationAgentConversationFileError(
                "application_agent_file_path_invalid",
                status_code=409,
            )
        return normalized

    @staticmethod
    def _record(
        artifact: ApplicationAgentConversationFileArtifact,
        run: ApplicationAgentRun,
    ) -> ApplicationAgentConversationFileRecord:
        relative_path = str(artifact.relative_path)
        return ApplicationAgentConversationFileRecord(
            file_id=int(artifact.id),
            relative_path=relative_path,
            display_name=display_name_for_path(relative_path),
            mime_type=str(artifact.mime_type) if artifact.mime_type else None,
            size_bytes=int(artifact.size_bytes),
            source_type=str(artifact.source_type),
            runtime_run_id=str(run.runtime_run_id),
            created_at=artifact.created_at,
        )


def application_agent_file_download_url(
    *,
    slug: str,
    file_id: int,
) -> str:
    return sign_file_download_url(
        base_url=settings.PUBLIC_API_BASE,
        slug=slug,
        file_id=file_id,
        secret=settings.FILE_DOWNLOAD_SIGNING_SECRET,
        ttl_seconds=settings.FILE_DOWNLOAD_URL_TTL_SECONDS,
    )
