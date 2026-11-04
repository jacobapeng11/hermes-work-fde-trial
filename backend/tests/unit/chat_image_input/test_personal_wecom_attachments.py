from types import SimpleNamespace

import pytest

from app.hermes_runtime.public_history_message import (
    normalize_internal_history_message,
)
from app.im_gateway.runtime_bridge import (
    PersonalImRuntimeBridge,
    _wecom_batch_attachment_entry,
)


@pytest.mark.asyncio
async def test_personal_wecom_image_only_uses_default_prompt_and_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=61,
        inbound_message_id=51,
        ordinal=0,
        display_name="截图.png",
        mime_type="image/png",
    )
    artifact = SimpleNamespace(
        id=71,
        relative_path="workspace/sessions/rs-1-a/im-inbox/截图.png",
    )

    class Result:
        def all(self):
            return [(attachment, artifact)]

    class Session:
        async def execute(self, _statement):
            return Result()

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        "app.im_gateway.runtime_bridge.AsyncSessionLocal",
        SessionContext,
    )
    bridge = object.__new__(PersonalImRuntimeBridge)
    queued = SimpleNamespace(
        normalized_content={"text": "[企微图片附件]"},
        batch_messages=[],
        inbound_message_id=51,
        quote_payload=None,
    )

    prepared = await bridge._build_prompt(queued)

    assert prepared.prompt.startswith("请识别这张图片")
    assert "workspace/sessions/rs-1-a/im-inbox/截图.png" not in prepared.prompt
    assert "Do not call vision_analyze, terminal, file/workspace tools" in prepared.prompt
    assert prepared.attachments[0].file_id == "71"
    assert prepared.attachments[0].mime_type == "image/png"


@pytest.mark.asyncio
async def test_personal_wecom_mixed_attachments_separate_image_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = SimpleNamespace(
        id=61,
        inbound_message_id=51,
        ordinal=0,
        display_name="截图.png",
        mime_type="image/png",
    )
    document = SimpleNamespace(
        id=62,
        inbound_message_id=51,
        ordinal=1,
        display_name="报告.txt",
        mime_type="text/plain",
    )
    image_artifact = SimpleNamespace(
        id=71,
        relative_path="workspace/sessions/rs-1-a/im-inbox/截图.png",
    )
    document_artifact = SimpleNamespace(
        id=72,
        relative_path="workspace/sessions/rs-1-a/im-inbox/报告.txt",
    )

    class Result:
        def all(self):
            return [(image, image_artifact), (document, document_artifact)]

    class Session:
        async def execute(self, _statement):
            return Result()

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        "app.im_gateway.runtime_bridge.AsyncSessionLocal",
        SessionContext,
    )
    bridge = object.__new__(PersonalImRuntimeBridge)
    queued = SimpleNamespace(
        normalized_content={"text": "检查这两个附件"},
        batch_messages=[],
        inbound_message_id=51,
        quote_payload=None,
    )

    prepared = await bridge._build_prompt(queued)

    assert "workspace/sessions/rs-1-a/im-inbox/截图.png" not in prepared.prompt
    assert "- 截图.png" in prepared.prompt
    assert (
        "- 报告.txt: workspace/sessions/rs-1-a/im-inbox/报告.txt"
        in prepared.prompt
    )
    assert "Do not call vision_analyze, terminal, file/workspace tools" in prepared.prompt


def test_wecom_batch_image_entry_does_not_expose_path() -> None:
    entry = _wecom_batch_attachment_entry(
        display_name="截图.png",
        relative_path="workspace/sessions/rs-1-a/im-inbox/截图.png",
        mime_type="image/png",
    )

    assert entry == {"name": "截图.png", "delivery": "vision_channel"}


def test_wecom_attachment_contract_is_hidden_from_public_history() -> None:
    prepared = normalize_internal_history_message(
        {
            "role": "user",
            "content": (
                "检查附件\n\n<platform_attachments>\n"
                "The user attached these files from WeCom. Read them from "
                "the current workspace when needed.\n"
                "Attachment-specific delivery instructions below override "
                "the default workspace instruction.\n"
                "Image attachments are already delivered.\n- 截图.png\n"
                "</platform_attachments>"
            ),
        },
        0,
    )

    assert prepared is not None
    assert prepared["content"] == "检查附件"
