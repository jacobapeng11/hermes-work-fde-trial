import pytest

from app.services.runtime_image_config import RuntimeImageConfig
from app.services.runtime_image_verification import (
    RuntimeImageVerificationError,
    require_live_image_matches_target,
)


TARGET = RuntimeImageConfig(
    image="kind.local/hermes-agent:code-target",
    manifest_digest="sha256:" + "a" * 64,
    config_id="sha256:" + "b" * 64,
)


@pytest.mark.parametrize(
    "image_id",
    [
        "containerd://sha256:" + "b" * 64,
        "docker://sha256:" + "b" * 64,
        "docker-pullable://kind.local/hermes-agent@sha256:" + "a" * 64,
    ],
)
def test_live_image_id_accepts_published_manifest_or_config_identity(image_id: str) -> None:
    assert require_live_image_matches_target(image_id, TARGET).startswith("sha256:")


def test_live_image_id_rejects_old_runtime_image() -> None:
    with pytest.raises(RuntimeImageVerificationError, match="does not match"):
        require_live_image_matches_target("containerd://sha256:" + "c" * 64, TARGET)
