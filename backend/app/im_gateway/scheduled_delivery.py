from __future__ import annotations

from app.models.im_gateway import ImChannel


WECOM_SCHEDULED_TARGET_NOT_READY = "SCHEDULED_TASK_WECOM_TARGET_NOT_READY"
_DELIVERABLE_CHANNEL_STATUSES = frozenset(
    {"connecting", "connected", "reconnecting"}
)


def personal_wecom_channel_is_scheduled_task_eligible(
    channel: ImChannel | None,
) -> bool:
    """Return whether a fixed personal WeCom Session has a durable recipient.

    ``config_generation`` deliberately is not part of this predicate.  It
    fences connection actors, while the proactive target is established only
    by a successfully persisted direct inbound message.
    """

    return bool(
        channel is not None
        and channel.target_type == "personal_agent"
        and channel.provider == "wecom"
        and channel.enabled
        and channel.bot_id
        and channel.secret_encrypted
        and channel.status in _DELIVERABLE_CHANNEL_STATUSES
        and channel.proactive_target_encrypted
        and channel.proactive_target_digest
        and channel.proactive_target_confirmed_at
    )
