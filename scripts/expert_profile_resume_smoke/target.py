"""Read-only target resolution and safety preconditions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.models import (
    CloudHermesProfile,
    HermesRuntimeBinding,
    RuntimeRun,
    RuntimeSession,
    User,
)
from app.services.runtime_commands import load_personal_runtime_command_target
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec

from .contracts import SmokeError, Target


async def load_target(
    user_code: str,
    runtime_id: str,
) -> tuple[Target, dict[str, Any]]:
    if not runtime_id.startswith("rw-") or not runtime_id[3:].isdigit():
        raise SmokeError("runtime-id must use rw-<positive workspace id>")
    workspace_id = int(runtime_id[3:])
    async with AsyncSessionLocal() as db:
        users = list(
            (
                await db.scalars(
                    select(User).where(
                        User.user_code == user_code,
                        User.status == "active",
                    )
                )
            ).all()
        )
        if len(users) != 1:
            raise SmokeError("user-code did not resolve exactly one active user")
        user = users[0]
        workspace = await db.scalar(
            select(CloudHermesProfile).where(
                CloudHermesProfile.id == workspace_id,
                CloudHermesProfile.user_id == user.id,
                CloudHermesProfile.status == "active",
            )
        )
        binding = await db.scalar(
            select(HermesRuntimeBinding).where(
                HermesRuntimeBinding.runtime_workspace_id == workspace_id,
                HermesRuntimeBinding.user_id == user.id,
                HermesRuntimeBinding.status == "ready",
            )
        )
        if workspace is None or binding is None or not binding.endpoint_base_url:
            raise SmokeError("target workspace or ready Runtime binding is unavailable")
        active_runs = int(
            await db.scalar(
                select(func.count())
                .select_from(RuntimeRun)
                .where(
                    RuntimeRun.runtime_workspace_id == workspace_id,
                    RuntimeRun.status.in_(("accepted", "pending", "running")),
                )
            )
            or 0
        )
        if active_runs:
            raise SmokeError("target workspace has an active Run")
        principal = Principal(org_id=int(user.org_id), user_id=int(user.id))
        command_target = await load_personal_runtime_command_target(
            db,
            org_id=principal.org_id,
            user_id=principal.user_id,
            runtime_workspace_id=workspace_id,
        )
        baseline_sessions = list(
            (
                await db.scalars(
                    select(RuntimeSession.runtime_session_id).where(
                        RuntimeSession.runtime_workspace_id == workspace_id,
                        RuntimeSession.status == "active",
                    )
                )
            ).all()
        )
        target = Target(
            principal=principal,
            user_code=user_code,
            user_name=str(user.name),
            workspace_id=workspace_id,
            runtime_id=str(binding.runtime_id),
            endpoint=RuntimeEndpoint(
                runtime_workspace_id=workspace_id,
                runtime_id=str(binding.runtime_id),
                endpoint_base_url=str(binding.endpoint_base_url),
                endpoint_host_header=str(binding.endpoint_host_header or ""),
                api_key=RuntimeApiKeyCodec().decrypt(str(binding.api_key_encrypted)),
            ),
            command_target=command_target,
        )
    return target, {
        "active_runs": 0,
        "active_session_ids": sorted(map(str, baseline_sessions)),
    }
