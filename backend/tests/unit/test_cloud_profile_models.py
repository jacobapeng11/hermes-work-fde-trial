from app.models import Base, CloudHermesProfile


def test_cloud_profile_table_is_registered() -> None:
    assert "cloud_hermes_profile" in Base.metadata.tables


def test_cloud_profile_owner_uses_platform_user_pk() -> None:
    table = CloudHermesProfile.__table__

    user_fk_targets = {fk.target_fullname for fk in table.c.user_id.foreign_keys}
    assert user_fk_targets == {"user.id"}
    assert "hrm_user_id" not in table.c
    assert "workspace_id" not in table.c
    assert "copaw_agent_id" not in table.c


def test_cloud_profile_default_model_uses_model_entry() -> None:
    table = CloudHermesProfile.__table__

    model_fk_targets = {fk.target_fullname for fk in table.c.default_model_id.foreign_keys}
    assert model_fk_targets == {"model_entry.id"}


def test_default_chat_agent_lookup_index_exists() -> None:
    indexes = {index.name for index in CloudHermesProfile.__table__.indexes}

    assert "ix_cloud_profile_default_chat_agent" in indexes
