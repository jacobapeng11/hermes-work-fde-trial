from app.services.enterprise_knowledge_settings import (
    EnterpriseKnowledgeSettingsService,
    enterprise_knowledge_enabled,
    merge_enterprise_knowledge_config,
)
from app.services.runtime_workspace import Principal


def test_enterprise_knowledge_defaults_to_disabled() -> None:
    assert enterprise_knowledge_enabled(None) is False
    assert enterprise_knowledge_enabled({}) is False


def test_merge_preserves_unrelated_profile_config() -> None:
    original = {
        "model": {"id": 7},
        "knowledge": {"personal": {"enabled": True}},
    }

    merged = merge_enterprise_knowledge_config(original, enabled=True)

    assert merged == {
        "model": {"id": 7},
        "knowledge": {
            "personal": {"enabled": True},
            "enterprise": {"enabled": True, "mode": "full"},
        },
    }
    assert original == {"model": {"id": 7}, "knowledge": {"personal": {"enabled": True}}}


def test_update_statement_filters_owner_and_locks_row() -> None:
    statement = EnterpriseKnowledgeSettingsService._statement(
        Principal(org_id=7, user_id=9),
        123,
        lock=True,
    )
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "cloud_hermes_profile.id = 123" in compiled
    assert "cloud_hermes_profile.org_id = 7" in compiled
    assert "cloud_hermes_profile.user_id = 9" in compiled
    assert "cloud_hermes_profile.status = 'active'" in compiled
    assert "FOR UPDATE" in compiled
