from app.schemas.model_runtime_capabilities import (
    RuntimeModelCapabilities,
    normalize_runtime_capabilities,
    supports_native_vision,
)
from app.schemas.runtime_edge import GrantedAttachment
from app.schemas.runtime_internal import RuntimeTurnAttachment
from app.services.runtime_edge_grants import RuntimeEdgeGrantService


def test_missing_vision_capability_preserves_legacy_payload() -> None:
    normalized = normalize_runtime_capabilities(
        {
            "thinking_mode": "provider_default",
            "reasoning_replay": "not_supported",
        }
    )

    assert normalized == {
        "thinking_mode": "provider_default",
        "reasoning_replay": "not_supported",
    }
    assert supports_native_vision(normalized) is False


def test_native_vision_capability_is_normalized() -> None:
    capabilities = RuntimeModelCapabilities.model_validate(
        {"vision": "native"}
    )

    assert supports_native_vision(capabilities) is True


def test_attachment_contracts_carry_mime_type() -> None:
    payload = {
        "file_id": "12",
        "display_name": "截图.png",
        "relative_path": "workspace/uploads/截图.png",
        "mime_type": "image/png",
    }

    assert GrantedAttachment.model_validate(payload).mime_type == "image/png"
    assert (
        RuntimeTurnAttachment.model_validate(payload).mime_type
        == "image/png"
    )


def test_image_attachment_rejects_non_vision_model() -> None:
    try:
        RuntimeEdgeGrantService._require_vision_support(
            runtime_capabilities={"vision": "not_supported"},
            attachments=(
                {
                    "file_id": "12",
                    "display_name": "截图.png",
                    "relative_path": "workspace/uploads/截图.png",
                    "mime_type": "image/png",
                },
            ),
        )
    except PermissionError as exc:
        assert str(exc) == "MODEL_VISION_UNSUPPORTED"
    else:
        raise AssertionError("expected MODEL_VISION_UNSUPPORTED")
