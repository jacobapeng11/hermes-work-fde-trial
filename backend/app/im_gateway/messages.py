from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WeComMessageRejected(ValueError):
    code = "wecom_message_rejected"


class WeComGroupMessageIgnored(WeComMessageRejected):
    code = "wecom_group_message_ignored"


class WeComUnsupportedMessage(WeComMessageRejected):
    code = "wecom_unsupported_message"


@dataclass(frozen=True)
class NormalizedMedia:
    ordinal: int
    media_type: str
    url: str
    aes_key: str | None
    quoted: bool = False
    display_name: str | None = None


@dataclass(frozen=True)
class NormalizedWeComMessage:
    external_message_id: str
    provider_request_id: str
    external_conversation_id: str
    message_type: str
    text: str
    quote_payload: dict[str, Any] | None
    sender_member_id: str = ""
    chat_type: str = "direct"
    bot_mentioned: bool = False
    media: list[NormalizedMedia] = field(default_factory=list)
    provider_created_at: int | None = None

    @property
    def normalized_content(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provider_created_at": self.provider_created_at,
            "attachment_count": len(self.media),
            "chat_type": self.chat_type,
        }


def normalize_wecom_message(
    frame: dict[str, Any],
    *,
    allow_group: bool = False,
) -> NormalizedWeComMessage:
    headers = frame.get("headers")
    body = frame.get("body")
    if not isinstance(headers, dict) or not isinstance(body, dict):
        raise WeComMessageRejected("invalid WeCom message frame")
    raw_chat_type = str(body.get("chattype") or "").strip().lower()
    if raw_chat_type not in {"single", "group"}:
        raise WeComMessageRejected("invalid WeCom chat type")
    if raw_chat_type == "group" and not allow_group:
        raise WeComGroupMessageIgnored(
            "only single-chat WeCom messages are accepted"
        )
    message_id = _required_text(body, "msgid")
    request_id = _required_text(headers, "req_id")
    sender = body.get("from")
    if not isinstance(sender, dict):
        raise WeComMessageRejected("WeCom sender is missing")
    sender_member_id = _required_text(sender, "userid")
    chat_type = "group" if raw_chat_type == "group" else "direct"
    conversation_id = (
        _required_text(body, "chatid")
        if chat_type == "group"
        else sender_member_id
    )
    message_type = str(body.get("msgtype") or "").strip().lower()
    media: list[NormalizedMedia] = []
    text = _normalize_body(message_type, body, media)
    quote_payload = _normalize_quote(body.get("quote"), media)
    if not text.strip():
        text = _attachment_prompt(media)
    created_at = body.get("create_time")
    return NormalizedWeComMessage(
        external_message_id=message_id,
        provider_request_id=request_id,
        external_conversation_id=conversation_id,
        sender_member_id=sender_member_id,
        chat_type=chat_type,
        # The WeCom AI Bot group-message contract delivers a frame only when
        # the bot is addressed.  No broad text regex is used to guess this.
        bot_mentioned=chat_type == "group",
        message_type=message_type,
        text=text,
        quote_payload=quote_payload,
        media=media,
        provider_created_at=(
            int(created_at)
            if isinstance(created_at, (int, float))
            else None
        ),
    )


def _normalize_body(
    message_type: str,
    body: dict[str, Any],
    media: list[NormalizedMedia],
) -> str:
    if message_type == "text":
        return _content(body, "text")
    if message_type == "voice":
        # SDK 1.0.7 exposes only Enterprise WeChat's public transcript.
        transcript = _content(body, "voice", required=False)
        return transcript or "[企微语音消息未提供转写文本]"
    if message_type in {"image", "file", "video"}:
        _append_media(media, message_type, body.get(message_type))
        return f"[企微{_media_label(message_type)}附件]"
    if message_type == "mixed":
        mixed = body.get("mixed")
        if not isinstance(mixed, dict) or not isinstance(
            mixed.get("msg_item"), list
        ):
            raise WeComMessageRejected("invalid mixed message")
        parts: list[str] = []
        for item in mixed["msg_item"]:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("msgtype") or "")
            if item_type == "text":
                value = _content(item, "text", required=False)
                if value:
                    parts.append(value)
            elif item_type == "image":
                _append_media(media, "image", item.get("image"))
                parts.append("[企微图片附件]")
            else:
                raise WeComUnsupportedMessage(
                    f"unsupported mixed item: {item_type}"
                )
        return "\n".join(parts)
    raise WeComUnsupportedMessage(
        f"unsupported WeCom message type: {message_type or 'unknown'}"
    )


def _normalize_quote(
    value: object, media: list[NormalizedMedia]
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    quote_type = str(value.get("msgtype") or "").strip().lower()
    if quote_type == "text":
        return {
            "msgtype": "text",
            "text": _content(value, "text", required=False),
        }
    if quote_type == "voice":
        return {
            "msgtype": "voice",
            "text": _content(value, "voice", required=False),
        }
    if quote_type in {"image", "file"}:
        _append_media(
            media, quote_type, value.get(quote_type), quoted=True
        )
        return {
            "msgtype": quote_type,
            "attachment": True,
        }
    if quote_type == "mixed":
        mixed = value.get("mixed")
        if not isinstance(mixed, dict) or not isinstance(
            mixed.get("msg_item"), list
        ):
            raise WeComMessageRejected("invalid quoted mixed message")
        items: list[dict[str, Any]] = []
        for item in mixed["msg_item"]:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("msgtype") or "")
            if item_type == "text":
                items.append(
                    {
                        "msgtype": "text",
                        "text": _content(
                            item, "text", required=False
                        ),
                    }
                )
            elif item_type == "image":
                _append_media(
                    media, "image", item.get("image"), quoted=True
                )
                items.append(
                    {"msgtype": "image", "attachment": True}
                )
            else:
                raise WeComUnsupportedMessage(
                    f"unsupported quoted mixed item: {item_type}"
                )
        return {"msgtype": "mixed", "items": items}
    raise WeComUnsupportedMessage(
        f"unsupported quote type: {quote_type or 'unknown'}"
    )


def _append_media(
    media: list[NormalizedMedia],
    media_type: str,
    value: object,
    *,
    quoted: bool = False,
) -> None:
    if not isinstance(value, dict):
        raise WeComMessageRejected(f"invalid {media_type} media")
    url = _required_text(value, "url")
    if not url.startswith("https://"):
        raise WeComMessageRejected("invalid WeCom media URL")
    aes_key = value.get("aeskey")
    media.append(
        NormalizedMedia(
            ordinal=len(media),
            media_type=media_type,
            url=url,
            aes_key=(
                str(aes_key).strip()
                if isinstance(aes_key, str) and aes_key.strip()
                else None
            ),
            quoted=quoted,
        )
    )


def _content(
    parent: dict[str, Any], key: str, *, required: bool = True
) -> str:
    value = parent.get(key)
    if not isinstance(value, dict):
        if required:
            raise WeComMessageRejected(f"missing {key} content")
        return ""
    content = value.get("content")
    if isinstance(content, str):
        return content
    if required:
        raise WeComMessageRejected(f"missing {key} text")
    return ""


def _required_text(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WeComMessageRejected(f"missing {key}")
    return value.strip()


def _attachment_prompt(media: list[NormalizedMedia]) -> str:
    if not media:
        return "[企微消息没有可读取的正文]"
    labels = "、".join(_media_label(item.media_type) for item in media)
    return f"[企微附件：{labels}]"


def _media_label(media_type: str) -> str:
    return {
        "image": "图片",
        "file": "文件",
        "video": "视频",
        "voice": "语音",
    }.get(media_type, "媒体")
