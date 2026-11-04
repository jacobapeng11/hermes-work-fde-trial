from __future__ import annotations

from app.api.internal.runtime_config_bundles import (
    _application_revision_agent,
)
from app.models import ApplicationAgent, ApplicationAgentConfigRevision


def test_application_bundle_uses_requested_immutable_revision() -> None:
    current = ApplicationAgent(
        id=31,
        org_id=1,
        created_by_user_id=99,
        slug="wireder",
        config_generation=6,
        config={
            "model_entry_id": 7,
            "system_prompt": "generation six",
        },
    )
    requested = ApplicationAgentConfigRevision(
        application_agent_id=31,
        org_id=1,
        generation=4,
        config_snapshot={
            "model_entry_id": 1,
            "system_prompt": "generation four",
        },
        config_hash="f" * 64,
        created_by_user_id=99,
    )

    view = _application_revision_agent(current, requested)

    assert view.config_generation == 4
    assert view.config == requested.config_snapshot
    assert view.config is not requested.config_snapshot
    assert current.config_generation == 6
    assert current.config["system_prompt"] == "generation six"
