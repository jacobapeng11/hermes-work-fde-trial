from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.runtime_edge import RuntimeEdgeGrantRequest
from app.services.runtime_edge_grants import (
    RuntimeEdgeGrantError,
    RuntimeEdgeGrantService,
    RuntimeEdgeGrantTarget,
)
from app.services.runtime_ticket_issuer import RuntimeTicketIssuer
from app.services.runtime_maintenance import RuntimeMaintenanceError
from app.services.runtime_workspace import Principal


EMPTY_DIGEST = "sha256:" + "0" * 64


class FrozenGrantControl:
    async def require_grant_issuance(self, *, lock: bool = False) -> None:
        assert lock is True
        raise RuntimeMaintenanceError(
            "MAINTENANCE_ADMISSION_FROZEN",
            retryable=True,
        )


class ScalarSequenceDb:
    def __init__(self, *values: object) -> None:
        self.values = list(values)

    async def scalar(self, _statement: object) -> object:
        return self.values.pop(0)


def test_bootstrap_uses_same_origin_fixed_path_and_confirmed_lifetimes() -> None:
    service = RuntimeEdgeGrantService(None)  # type: ignore[arg-type]
    principal = Principal(org_id=3, user_id=7)
    target = RuntimeEdgeGrantTarget(
        runtime_id="runtime-1",
        binding_id="17",
        endpoint_host_header="runtime-1.runtimes.example",
        api_server_key="runtime-secret-" + "x" * 32,
        runtime_session_id="session-00000001",
        model_entry_id=9,
        upstream_model_id="qwen3-235b-a22b",
        profile_id="profile-5",
        runtime_agent_profile_id=51,
        config_revision="revision-18",
        profile_desired_revision=0,
        profile_applied_revision=0,
        profile_digest=EMPTY_DIGEST,
        skill_digest=EMPTY_DIGEST,
        mcp_digest=EMPTY_DIGEST,
        knowledge_digest=EMPTY_DIGEST,
    )

    response = service.issue_bootstrap(
        principal=principal,
        target=target,
        allowed_origin="https://agent.example.com",
        now=1_800_000_000,
    )

    assert response.websocket_path == "/runtime-edge/runtime-1/ws"
    assert response.websocket_protocol == "hermes-runtime-v1"
    assert response.runtime_agent_profile_id == 51
    assert (
        response.session_grant_expires_at.timestamp()
        - response.runtime_access_expires_at.timestamp()
        == 1500
    )
    access = RuntimeTicketIssuer(
        runtime_id=target.runtime_id,
        secret=target.api_server_key,
    ).verify_runtime_access(
        response.runtime_access,
        expected_origin="https://agent.example.com",
        now=1_800_000_001,
    )
    assert access.user_id == principal.user_id


async def test_target_fails_closed_before_queries_while_maintenance_is_active() -> None:
    service = RuntimeEdgeGrantService(
        None,  # type: ignore[arg-type]
        maintenance_control=FrozenGrantControl(),
    )

    with pytest.raises(RuntimeEdgeGrantError, match="MAINTENANCE_ADMISSION_FROZEN"):
        await service.target(
            principal=Principal(org_id=3, user_id=7),
            request=RuntimeEdgeGrantRequest(
                runtime_workspace_id=17,
                runtime_session_id="session-00000001",
            ),
        )


def test_stale_target_cannot_issue_after_grant_gate_closes() -> None:
    service = RuntimeEdgeGrantService(None)  # type: ignore[arg-type]
    target = RuntimeEdgeGrantTarget(
        runtime_id="runtime-1",
        binding_id="17",
        endpoint_host_header="runtime-1.runtimes.example",
        api_server_key="runtime-secret-" + "x" * 32,
        runtime_session_id="session-00000001",
        model_entry_id=9,
        upstream_model_id="qwen3-235b-a22b",
        profile_id="profile-5",
        runtime_agent_profile_id=51,
        config_revision="revision-18",
        profile_desired_revision=0,
        profile_applied_revision=0,
        profile_digest=EMPTY_DIGEST,
        skill_digest=EMPTY_DIGEST,
        mcp_digest=EMPTY_DIGEST,
        knowledge_digest=EMPTY_DIGEST,
        admission_generation=2,
        grant_issuance_enabled=False,
    )

    with pytest.raises(RuntimeEdgeGrantError, match="RUNTIME_GRANT_ISSUANCE_DISABLED"):
        service.issue_bootstrap(
            principal=Principal(org_id=3, user_id=7),
            target=target,
            allowed_origin="https://agent.example.com",
            now=1_800_000_000,
        )


async def test_profile_execution_identity_uses_applied_revision_snapshot() -> None:
    service = RuntimeEdgeGrantService(
        ScalarSequenceDb(
            SimpleNamespace(id=51, desired_revision=8, applied_revision=7),
            SimpleNamespace(
                profile_digest="sha256:" + "2" * 64,
                skill_digest="sha256:" + "3" * 64,
                mcp_digest="sha256:" + "4" * 64,
                knowledge_digest="sha256:" + "5" * 64,
            ),
        )  # type: ignore[arg-type]
    )

    identity = await service._profile_execution_identity(
        principal=Principal(org_id=3, user_id=7),
        runtime_workspace_id=17,
        runtime_agent_profile_id=51,
        hermes_profile_name="expert-alpha",
    )

    assert identity == (
        7,
        7,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
        "sha256:" + "5" * 64,
    )


async def test_profile_execution_identity_rejects_missing_applied_snapshot() -> None:
    service = RuntimeEdgeGrantService(
        ScalarSequenceDb(
            SimpleNamespace(id=51, desired_revision=7, applied_revision=7),
            None,
        )  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeEdgeGrantError, match="revision is unavailable"):
        await service._profile_execution_identity(
            principal=Principal(org_id=3, user_id=7),
            runtime_workspace_id=17,
            runtime_agent_profile_id=51,
            hermes_profile_name="expert-alpha",
        )
