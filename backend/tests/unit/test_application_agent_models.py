from app.models import Base


def test_application_agent_tables_are_part_of_current_metadata() -> None:
    assert {
        "application_agent",
        "application_agent_config_revision",
        "application_agent_personal_knowledge_binding",
        "application_agent_skill",
        "application_agent_runtime_binding",
        "application_client",
        "application_agent_client_grant",
        "application_agent_authorization_code",
        "application_agent_conversation",
        "application_agent_run",
        "application_agent_memory",
        "application_agent_wecom_channel",
        "application_agent_wecom_oauth_state",
        "application_agent_wecom_binding_ticket",
        "application_agent_wecom_user_binding",
        "application_agent_wecom_callback_event",
        "organization_wecom_identity",
        "application_agent_wecom_user_access",
        "wecom_identity_binding_ticket",
    }.issubset(Base.metadata.tables)


def test_personal_knowledge_binding_preserves_revoked_history() -> None:
    table = Base.metadata.tables[
        "application_agent_personal_knowledge_binding"
    ]
    foreign_key_targets = {
        foreign_key.target_fullname
        for foreign_key in table.foreign_keys
    }

    assert "application_agent.id" in foreign_key_targets
    assert "knowledge_base.id" not in foreign_key_targets


def test_application_agent_conversation_and_active_run_are_isolated() -> None:
    conversation = Base.metadata.tables["application_agent_conversation"]
    run = Base.metadata.tables["application_agent_run"]
    conversation_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in conversation.constraints
        if constraint.name
    }
    run_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in run.constraints
        if constraint.name
    }

    assert conversation_constraints[
        "uq_application_agent_conversation_scope"
    ] == (
        "org_id",
        "application_agent_id",
        "application_client_id",
        "subject_key",
        "conversation_key",
    )
    assert run_constraints["uq_application_agent_run_active_session"] == (
        "runtime_binding_id",
        "active_session_key",
    )
    assert run_constraints[
        "uq_application_agent_run_channel_idempotency"
    ] == (
        "application_agent_id",
        "application_client_id",
        "channel",
        "idempotency_key",
    )
    assert {
        "submission_state",
        "submit_intent_at",
        "dashboard_accepted_at",
        "capacity_released_at",
        "payload_digest",
        "response_snapshot",
    }.issubset(run.c.keys())


def test_im_channel_has_no_redundant_member_invitation_url() -> None:
    assert (
        "member_access_url"
        not in Base.metadata.tables["im_channel"].c
    )
