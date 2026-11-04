from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.schemas.runtime_internal import (
    InternalTurnCommandPayload,
    RuntimeCommandAck,
    RuntimeTurnAttachment,
    RuntimeTurnIdentity,
)
from app.services.runtime_channel_fast_lane import (
    RuntimeChannelFastLaneClient,
    issue_channel_execution_lease,
)
from app.services.runtime_commands import (
    RuntimeCommandTarget,
    build_runtime_command,
)


TARGET = RuntimeCommandTarget(
    runtime_id="runtime-1",
    binding_id="binding-1",
    endpoint_host_header="runtime-1.runtime.example",
    api_server_key="runtime-secret-" + "x" * 32,
    admission_generation=11,
)


def _lease():
    return issue_channel_execution_lease(
        target=TARGET,
        org_id=3,
        channel_id=31,
        target_type="application_agent",
        target_id=25,
        subject_user_id=7,
        runtime_session_id="app-25-session-0001",
        model_entry_id="19",
        profile_id="application-25",
        application_agent_id=25,
        gateway_instance_id="backend-1:im-gateway",
        channel_lease_epoch=7,
        channel_generation=4,
        config_generation=31,
        issued_at=datetime(2026, 10, 11, 10, tzinfo=timezone.utc),
    )


def _attached_envelope():
    payload = InternalTurnCommandPayload(
        source="application_wecom",
        run_id="run-wecom-image-0001",
        client_message_id="message-wecom-image-0001",
        identity=RuntimeTurnIdentity(
            user_id=7,
            org_id=3,
            session_id="app-25-session-0001",
            profile_id="application-25",
            runtime_agent_profile_id=0,
            application_agent_id=25,
            profile_desired_revision=31,
            profile_applied_revision=31,
            profile_digest="sha256:" + "1" * 64,
            skill_digest="sha256:" + "2" * 64,
            mcp_digest="sha256:" + "3" * 64,
            knowledge_digest="sha256:" + "4" * 64,
            model_id="19",
            upstream_model_id="deepseek-v4-flash",
            context_length=1_000_000,
            runtime_capabilities={"vision": "native"},
            config_revision="generation-31",
            admission_generation=11,
            max_prompt_bytes=1024,
            max_output_tokens=4096,
        ),
        prompt="请识别这张图片",
        attachment_file_ids=["11"],
        attachments=[
            RuntimeTurnAttachment(
                file_id="11",
                display_name="wecom-image.jpg",
                relative_path=(
                    "workspace/sessions/app-25-session-0001/"
                    "im-inbox/wecom-image.jpg"
                ),
                mime_type="image/jpeg",
            )
        ],
        channel_execution_lease=_lease(),
    )
    return build_runtime_command(
        target=TARGET,
        command_id="command-wecom-image-0001",
        payload=payload,
        issued_at=datetime(2026, 10, 11, 10, tzinfo=timezone.utc),
        deadline_seconds=600,
    )


class _FastConnection:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit(self, *args: Any, **kwargs: Any) -> RuntimeCommandAck:
        self.submitted.append({"args": args, **kwargs})
        return RuntimeCommandAck(
            command_id="command-wecom-image-0001",
            state="accepted",
            run_id="run-wecom-image-0001",
        )


class _Pool:
    def __init__(self, connection: _FastConnection) -> None:
        self._connection = connection

    async def connection(self, **_kwargs: Any) -> _FastConnection:
        return self._connection


class _Fallback:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, **kwargs: Any) -> RuntimeCommandAck:
        self.sent.append(kwargs)
        return RuntimeCommandAck(
            command_id="command-wecom-image-0001",
            state="accepted",
            run_id="run-wecom-image-0001",
        )


@pytest.mark.asyncio
async def test_attachment_turn_bypasses_fast_lane_v1() -> None:
    fast = _FastConnection()
    fallback = _Fallback()
    client = RuntimeChannelFastLaneClient(
        pool=_Pool(fast),
        fallback=fallback,
    )

    ack = await client.send(
        target=TARGET,
        envelope=_attached_envelope(),
    )

    assert ack.run_id == "run-wecom-image-0001"
    assert fast.submitted == []
    assert len(fallback.sent) == 1
    assert fallback.sent[0]["envelope"].payload.attachments[
        0
    ].mime_type == "image/jpeg"
