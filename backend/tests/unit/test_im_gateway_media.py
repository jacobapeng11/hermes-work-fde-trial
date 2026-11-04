from __future__ import annotations

import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.im_gateway import runtime_bridge as runtime_bridge_module
from app.im_gateway.runtime_bridge import (
    ApplicationImRuntimeBridge,
    ImRuntimeBridgeError,
    PersonalImRuntimeBridge,
    _detect_mime_type,
    _validate_inbound_media_type,
)
from app.hermes_runtime.dashboard_protocol import ManagedRuntimeArtifact
from app.im_gateway.leases import OwnedImChannel
from app.services.chat_files import ChatFileRecord


def test_detects_media_from_bytes_instead_of_filename(tmp_path) -> None:
    disguised = tmp_path / "not-really-an-image.png"
    disguised.write_bytes(b"%PDF-1.7\nminimal")

    assert _detect_mime_type(disguised, disguised.name) == "application/pdf"
    with pytest.raises(
        ImRuntimeBridgeError,
        match="does not match its declared type",
    ):
        _validate_inbound_media_type(
            "image", _detect_mime_type(disguised, disguised.name)
        )


def test_detects_images_and_openxml_archives_from_content(tmp_path) -> None:
    jpeg = tmp_path / "payload.bin"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)
    assert _detect_mime_type(jpeg, jpeg.name) == "image/jpeg"
    _validate_inbound_media_type("image", "image/jpeg")

    document = tmp_path / "unknown.zip"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    assert _detect_mime_type(document, document.name) == (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    )


@pytest.mark.asyncio
async def test_artifact_delivery_accepts_null_delivered_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _query: object) -> object:
            return SimpleNamespace(delivered_artifact_ids=None)

    class Dashboard:
        async def list_session_artifacts(
            self,
            _endpoint: object,
            *,
            runtime_session_id: str,
        ) -> list[object]:
            assert runtime_session_id == "runtime-session"
            return []

    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        lambda: Session(),
    )
    bridge = object.__new__(ApplicationImRuntimeBridge)
    bridge.dashboard = Dashboard()

    await bridge._deliver_generated_artifacts(
        queued=SimpleNamespace(inbound_message_id=1),
        endpoint=object(),
        runtime_session_id="runtime-session",
        input_paths=set(),
        started_wall_time=0,
    )


@pytest.mark.asyncio
async def test_artifact_follow_up_failure_does_not_reopen_delivered_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked: list[dict[str, object]] = []

    class Session:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def mark_artifact_delivery_failed(
            self, **values: object
        ) -> bool:
            marked.append(values)
            return True

    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        Session,
    )
    monkeypatch.setattr(runtime_bridge_module, "ImInboundQueue", Queue)
    bridge = object.__new__(ApplicationImRuntimeBridge)
    bridge._deliver_generated_artifacts = AsyncMock(
        side_effect=ImRuntimeBridgeError(
            "Runtime control failed with status 422"
        )
    )
    queued = SimpleNamespace(
        inbound_message_id=709,
        owner_instance_id="backend-a",
        lease_epoch=23,
    )

    await bridge._deliver_generated_artifacts_best_effort(
        queued=queued,
        endpoint=object(),
        runtime_session_id="runtime-session",
        input_paths=set(),
        started_wall_time=0,
    )

    assert marked == [
        {
            "inbound_message_id": 709,
            "owner_instance_id": "backend-a",
            "lease_epoch": 23,
            "error_code": "ImRuntimeBridgeError",
            "error_message": "Runtime control failed with status 422",
        }
    ]


@pytest.mark.asyncio
async def test_personal_attachment_staging_uses_current_channel_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source_path = tmp_path / "report.txt"
    source_path.write_text("current session attachment", encoding="utf-8")
    registered_commands: list[object] = []
    uploaded_paths: list[str] = []

    channel = SimpleNamespace(
        id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_row_id=55,
        target_type="personal_agent",
        provider="wecom",
    )
    current_session = SimpleNamespace(
        id=55,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_id="session-current",
        source_type="wecom",
        status="active",
    )
    attachment = SimpleNamespace(
        id=61,
        display_name="report.txt",
        media_type="file",
    )

    class Result:
        def first(self):
            return channel, current_session

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

    class SessionContext:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Queue:
        def __init__(self, _db: object) -> None:
            pass

        async def pending_attachments(
            self,
            *_args: object,
            **_kwargs: object,
        ):
            return [
                (
                    attachment,
                    {"url": "https://example.test/report.txt"},
                )
            ]

        async def mark_attachment_ready(
            self,
            **_kwargs: object,
        ) -> bool:
            return True

        async def finish_staging(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> bool:
            return True

        async def mark_staging_failed(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> str:
            return "failed"

    class NodeBridge:
        async def download_media(self, **_kwargs: object) -> dict:
            return {
                "path": str(source_path),
                "filename": "report.txt",
                "size_bytes": source_path.stat().st_size,
            }

        def require_scoped_path(self, value: str):
            return source_path.__class__(value)

    class Dashboard:
        async def upload_managed_artifact(
            self,
            _endpoint: object,
            *,
            relative_path: str,
            content: object,
            filename: str,
            mime_type: str | None,
        ) -> ManagedRuntimeArtifact:
            del content, filename
            uploaded_paths.append(relative_path)
            return ManagedRuntimeArtifact(
                relative_path=relative_path,
                mime_type=mime_type,
                size_bytes=source_path.stat().st_size,
            )

    class FileIndex:
        def __init__(self, _store: object) -> None:
            pass

        async def register_file(
            self,
            *,
            principal: object,
            command: object,
        ) -> ChatFileRecord:
            del principal
            registered_commands.append(command)
            return ChatFileRecord(
                file_id=71,
                runtime_workspace_id=4,
                runtime_session_id=getattr(
                    command, "runtime_session_id"
                ),
                runtime_run_id=None,
                relative_path=getattr(command, "relative_path"),
                display_name="report.txt",
                mime_type=getattr(command, "mime_type"),
                size_bytes=getattr(command, "size_bytes"),
                source_type="upload",
            )

    async def activate_runtime(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(
        runtime_bridge_module,
        "AsyncSessionLocal",
        SessionContext,
    )
    monkeypatch.setattr(runtime_bridge_module, "ImInboundQueue", Queue)
    monkeypatch.setattr(
        runtime_bridge_module,
        "ChatFileIndexService",
        FileIndex,
    )
    bridge = object.__new__(PersonalImRuntimeBridge)
    bridge.node_bridge = NodeBridge()
    bridge.dashboard = Dashboard()
    bridge.activate_runtime = activate_runtime
    stale_actor = OwnedImChannel(
        channel_id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_row_id=5,
        runtime_session_id="session-stale",
        bot_id="bot",
        secret="secret",
        config_generation=6,
        lease_epoch=7,
        status="connected",
    )

    status = await bridge.stage_attachments(
        queued_channel=stale_actor,
        inbound_message_id=51,
        owner_instance_id="backend-1",
    )

    assert status == "queued"
    assert len(uploaded_paths) == 1
    assert uploaded_paths[0].startswith(
        "workspace/sessions/session-current/im-inbox/"
    )
    assert "session-stale" not in uploaded_paths[0]
    assert len(registered_commands) == 1
    assert (
        registered_commands[0].runtime_session_id
        == "session-current"
    )


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("channel", "target_type", "application_agent"),
        ("channel", "provider", "feishu"),
        ("session", "status", "archived"),
        ("session", "org_id", 99),
        ("session", "user_id", 99),
        ("session", "runtime_workspace_id", 99),
        ("queued", "user_id", 99),
        ("queued", "runtime_workspace_id", 99),
    ],
)
@pytest.mark.asyncio
async def test_personal_attachment_session_refresh_rejects_invariant_mismatch(
    target: str,
    field: str,
    value: object,
) -> None:
    channel = SimpleNamespace(
        id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_row_id=55,
        target_type="personal_agent",
        provider="wecom",
    )
    current_session = SimpleNamespace(
        id=55,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
        runtime_session_id="session-current",
        source_type="wecom",
        status="active",
    )
    queued_channel = SimpleNamespace(
        channel_id=1,
        org_id=2,
        user_id=3,
        runtime_workspace_id=4,
    )
    objects = {
        "channel": channel,
        "session": current_session,
        "queued": queued_channel,
    }
    setattr(objects[target], field, value)

    class Result:
        def first(self):
            return channel, current_session

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

    bridge = object.__new__(PersonalImRuntimeBridge)
    with pytest.raises(
        ImRuntimeBridgeError,
        match="invariant is broken",
    ):
        await bridge._current_personal_channel_session_id(
            Session(),
            queued_channel=queued_channel,
        )
