from datetime import datetime, timezone

from app.im_gateway.scheduled_delivery import (
    personal_wecom_channel_is_scheduled_task_eligible,
)
from app.models import ImChannel


def _channel(**overrides) -> ImChannel:
    values = {
        "target_type": "personal_agent",
        "provider": "wecom",
        "enabled": True,
        "bot_id": "bot-1",
        "secret_encrypted": "ciphertext",
        "status": "connected",
        "proactive_target_encrypted": "target-ciphertext",
        "proactive_target_digest": "a" * 64,
        "proactive_target_confirmed_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return ImChannel(**values)


def test_personal_wecom_scheduled_eligibility_requires_confirmed_target() -> None:
    assert personal_wecom_channel_is_scheduled_task_eligible(_channel())
    assert not personal_wecom_channel_is_scheduled_task_eligible(
        _channel(proactive_target_encrypted=None)
    )
    assert not personal_wecom_channel_is_scheduled_task_eligible(
        _channel(proactive_target_digest=None)
    )
    assert not personal_wecom_channel_is_scheduled_task_eligible(
        _channel(proactive_target_confirmed_at=None)
    )


def test_generation_does_not_make_an_unconfirmed_channel_eligible() -> None:
    assert not personal_wecom_channel_is_scheduled_task_eligible(
        _channel(
            config_generation=999,
            proactive_target_encrypted=None,
            proactive_target_digest=None,
            proactive_target_confirmed_at=None,
        )
    )
