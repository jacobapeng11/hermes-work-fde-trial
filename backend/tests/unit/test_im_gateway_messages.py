from __future__ import annotations

import pytest

from app.im_gateway.messages import (
    WeComGroupMessageIgnored,
    WeComMessageRejected,
    WeComUnsupportedMessage,
    normalize_wecom_message,
)


def _frame(msgtype: str, payload: dict, **body_overrides):
    return {
        "headers": {"req_id": "req-1"},
        "body": {
            "msgid": f"msg-{msgtype}",
            "aibotid": "bot-1",
            "chattype": "single",
            "from": {"userid": "zhangsan"},
            "create_time": 1785286800,
            "msgtype": msgtype,
            msgtype: payload,
            **body_overrides,
        },
    }


@pytest.mark.parametrize(
    ("msgtype", "payload", "expected_text", "media_type"),
    [
        ("text", {"content": "你好"}, "你好", None),
        (
            "image",
            {"url": "https://example.test/i", "aeskey": "ik"},
            "[企微图片附件]",
            "image",
        ),
        (
            "voice",
            {"content": "语音转写"},
            "语音转写",
            None,
        ),
        (
            "file",
            {"url": "https://example.test/f", "aeskey": "fk"},
            "[企微文件附件]",
            "file",
        ),
        (
            "video",
            {"url": "https://example.test/v", "aeskey": "vk"},
            "[企微视频附件]",
            "video",
        ),
    ],
)
def test_normalizes_every_official_sdk_message_type(
    msgtype: str,
    payload: dict,
    expected_text: str,
    media_type: str | None,
) -> None:
    message = normalize_wecom_message(_frame(msgtype, payload))

    assert message.message_type == msgtype
    assert message.text == expected_text
    assert message.external_conversation_id == "zhangsan"
    assert message.provider_request_id == "req-1"
    assert message.provider_created_at == 1785286800
    assert [item.media_type for item in message.media] == (
        [media_type] if media_type else []
    )


def test_normalizes_mixed_and_all_official_quote_variants() -> None:
    message = normalize_wecom_message(
        _frame(
            "mixed",
            {
                "msg_item": [
                    {
                        "msgtype": "text",
                        "text": {"content": "正文"},
                    },
                    {
                        "msgtype": "image",
                        "image": {
                            "url": "https://example.test/main-image",
                            "aeskey": "main-key",
                        },
                    },
                ]
            },
            quote={
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {
                            "msgtype": "text",
                            "text": {"content": "引用正文"},
                        },
                        {
                            "msgtype": "image",
                            "image": {
                                "url": "https://example.test/quote-image",
                                "aeskey": "quote-key",
                            },
                        },
                    ]
                },
            },
        )
    )

    assert message.text == "正文\n[企微图片附件]"
    assert message.quote_payload == {
        "msgtype": "mixed",
        "items": [
            {"msgtype": "text", "text": "引用正文"},
            {"msgtype": "image", "attachment": True},
        ],
    }
    assert [(item.media_type, item.quoted) for item in message.media] == [
        ("image", False),
        ("image", True),
    ]

    for quote_type, quote_payload, expected in (
        (
            "text",
            {"text": {"content": "引用文本"}},
            {"msgtype": "text", "text": "引用文本"},
        ),
        (
            "voice",
            {"voice": {"content": "引用语音转写"}},
            {"msgtype": "voice", "text": "引用语音转写"},
        ),
        (
            "image",
            {
                "image": {
                    "url": "https://example.test/quoted-image",
                    "aeskey": "qik",
                }
            },
            {"msgtype": "image", "attachment": True},
        ),
        (
            "file",
            {
                "file": {
                    "url": "https://example.test/quoted-file",
                    "aeskey": "qfk",
                }
            },
            {"msgtype": "file", "attachment": True},
        ),
    ):
        quoted = normalize_wecom_message(
            _frame(
                "text",
                {"content": "问题"},
                quote={"msgtype": quote_type, **quote_payload},
            )
        )
        assert quoted.quote_payload == expected


def test_rejects_groups_and_types_outside_the_sdk_contract() -> None:
    with pytest.raises(WeComGroupMessageIgnored):
        normalize_wecom_message(
            _frame("text", {"content": "群消息"}, chattype="group")
        )

    with pytest.raises(WeComUnsupportedMessage):
        normalize_wecom_message(
            _frame("location", {"latitude": 22.5})
        )

    with pytest.raises(
        WeComMessageRejected, match="invalid WeCom media URL"
    ):
        normalize_wecom_message(
            _frame(
                "file",
                {"url": "http://127.0.0.1/private", "aeskey": "key"},
            )
        )


def test_application_channel_accepts_addressed_group_with_real_sender() -> None:
    message = normalize_wecom_message(
        _frame(
            "text",
            {"content": "查询群内工单"},
            chattype="group",
            chatid="group-ticket-42",
            **{"from": {"userid": "lisi"}},
        ),
        allow_group=True,
    )

    assert message.chat_type == "group"
    assert message.bot_mentioned is True
    assert message.external_conversation_id == "group-ticket-42"
    assert message.sender_member_id == "lisi"
    assert message.text == "查询群内工单"
