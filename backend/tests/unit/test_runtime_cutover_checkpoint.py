from pathlib import Path

import pytest

from app.services.runtime_cutover_checkpoint import (
    RuntimeCutoverCheckpointError,
    RuntimeCutoverCheckpointStore,
)
from app.services.runtime_image_config import RuntimeImageConfig


def _target(suffix: str = "a") -> RuntimeImageConfig:
    return RuntimeImageConfig(
        image=f"kind.local/hermes-agent:code-{suffix}",
        manifest_digest="sha256:" + suffix * 64,
        config_id="sha256:" + "b" * 64,
    )


def test_checkpoint_is_atomic_resumable_and_secret_free(tmp_path: Path) -> None:
    path = tmp_path / "fleet.json"
    store = RuntimeCutoverCheckpointStore(path)
    planned = store.initialize(
        flow="fleet",
        target=_target(),
        bindings=[
            {
                "binding_type": "personal_runtime",
                "binding_id": 1,
                "runtime_id": "rt-1",
                "pvc_uid": "pvc-1",
                "pvc_storage_identity_hash": "sha256:" + "c" * 64,
            }
        ],
    )

    resumed = store.initialize(flow="fleet", target=_target(), bindings=[])
    completed = store.advance(
        stage="verified",
        status="completed",
        checks={"live_image_id": True},
    )

    assert resumed == planned
    assert completed["status"] == "completed"
    assert completed["checks"] == {"live_image_id": True}
    assert path.stat().st_mode & 0o777 == 0o600


def test_checkpoint_rejects_target_change_and_sensitive_fields(tmp_path: Path) -> None:
    store = RuntimeCutoverCheckpointStore(tmp_path / "single.json")
    store.initialize(flow="single", target=_target(), bindings=[])

    with pytest.raises(RuntimeCutoverCheckpointError, match="conflicts"):
        store.initialize(flow="single", target=_target("c"), bindings=[])
    with pytest.raises(RuntimeCutoverCheckpointError, match="sensitive"):
        RuntimeCutoverCheckpointStore._reject_sensitive_fields({"api_key": "no"})
