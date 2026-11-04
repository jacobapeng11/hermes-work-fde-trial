from __future__ import annotations

import base64
import json

import pytest

from app.schemas.provider_routes import ProviderRouteGrantV1
from app.services.runtime_ticket_issuer import (
    RuntimeTicketError,
    RuntimeTicketIssuer,
    RuntimeTicketIssuanceError,
)


EMPTY_DIGEST = "sha256:" + "0" * 64


def test_runtime_tickets_have_expected_scoped_claims() -> None:
    secret = "runtime-secret-" + "x" * 32
    issuer = RuntimeTicketIssuer(
        runtime_id="runtime-1",
        secret=secret,
        admission_generation=1,
    )
    access = issuer.runtime_access(
        sub="user-7",
        user_id=7,
        org_id=3,
        allowed_origin="https://agent.example.com",
        connection_nonce="nonce-0000000000000001",
        issued_at=1_800_000_000,
    )
    grant = issuer.session_grant(
        user_id=7,
        org_id=3,
        session_id="session-00000001",
        profile_id="default",
        runtime_agent_profile_id=51,
        model_id="model-9",
        upstream_model_id="qwen3-235b-a22b",
        context_length=1_000_000,
        runtime_capabilities={"reasoning_replay": "required", "thinking_mode": "enabled"},
        provider_route=ProviderRouteGrantV1(
            capsule="prc1.3." + "a" * 12 + "." + "b" * 32,
            digest="sha256:" + "c" * 64,
        ),
        config_revision="revision-18",
        profile_desired_revision=27,
        profile_applied_revision=27,
        profile_digest="sha256:" + "2" * 64,
        skill_digest="sha256:" + "3" * 64,
        mcp_digest="sha256:" + "4" * 64,
        knowledge_digest="sha256:" + "5" * 64,
        max_prompt_bytes=1_000_000,
        max_output_tokens=32_000,
        issued_at=1_800_000_000,
    )
    access_payload = json.loads(
        base64.urlsafe_b64decode(
            access.split(".")[1] + "=" * (-len(access.split(".")[1]) % 4)
        )
    )
    grant_payload = json.loads(
        base64.urlsafe_b64decode(
            grant.split(".")[1] + "=" * (-len(grant.split(".")[1]) % 4)
        )
    )

    assert access_payload["runtime_id"] == "runtime-1"
    assert access_payload["admission_generation"] == 1
    assert access_payload["allowed_origin"] == "https://agent.example.com"
    assert access_payload["expires_at"] - access_payload["issued_at"] == 300
    assert grant_payload["session_id"] == "session-00000001"
    assert grant_payload["runtime_agent_profile_id"] == 51
    assert grant_payload["admission_generation"] == 1
    assert grant_payload["expires_at"] - grant_payload["issued_at"] == 1800
    assert grant_payload["runtime_capabilities"]["reasoning_replay"] == "required"
    assert grant_payload["context_length"] == 1_000_000
    assert grant_payload["provider_route"]["digest"] == "sha256:" + "c" * 64
    assert grant_payload["profile_applied_revision"] == 27
    assert grant_payload["mcp_digest"] == "sha256:" + "4" * 64
    verified = issuer.verify_runtime_access(
        access,
        expected_origin="https://agent.example.com",
        now=1_800_000_001,
    )
    assert verified.user_id == 7
    file_ticket = issuer.file_transfer(
        operation="upload",
        user_id=7,
        org_id=3,
        session_id="session-00000001",
        file_id=19,
        relative_path="workspace/sessions/session-00000001/19/report.txt",
        display_name="report.txt",
        mime_type="text/plain",
        max_bytes=1024,
        issued_at=1_800_000_000,
    )
    file_claims = issuer.verify_file_transfer(
        file_ticket,
        operation="upload",
        now=1_800_000_001,
    )
    assert file_claims.file_id == 19
    assert len(file_claims.ticket_id) >= 16
    assert file_claims.expires_at - file_claims.issued_at == 300


def test_runtime_access_verification_rejects_origin_expiry_and_tampering() -> None:
    issuer = RuntimeTicketIssuer(
        runtime_id="runtime-1",
        secret="runtime-secret-" + "x" * 32,
        admission_generation=1,
    )
    access = issuer.runtime_access(
        sub="7",
        user_id=7,
        org_id=3,
        allowed_origin="https://agent.example.com",
        connection_nonce="nonce-0000000000000001",
        issued_at=1_800_000_000,
    )
    with pytest.raises(RuntimeTicketError, match="Origin"):
        issuer.verify_runtime_access(
            access,
            expected_origin="https://other.example.com",
            now=1_800_000_001,
        )
    with pytest.raises(RuntimeTicketError, match="expired"):
        issuer.verify_runtime_access(
            access,
            expected_origin="https://agent.example.com",
            now=1_800_000_301,
        )
    with pytest.raises(RuntimeTicketError, match="signature"):
        issuer.verify_runtime_access(
            f"{access[:-1]}x",
            expected_origin="https://agent.example.com",
            now=1_800_000_001,
        )


def test_runtime_grant_issuance_gate_blocks_access_and_session_tokens() -> None:
    issuer = RuntimeTicketIssuer(
        runtime_id="runtime-1",
        secret="runtime-secret-" + "x" * 32,
        admission_generation=2,
        grant_issuance_enabled=False,
    )

    with pytest.raises(
        RuntimeTicketIssuanceError,
        match="RUNTIME_GRANT_ISSUANCE_DISABLED",
    ):
        issuer.runtime_access(
            sub="7",
            user_id=7,
            org_id=3,
            allowed_origin="https://agent.example.com",
            connection_nonce="nonce-0000000000000001",
            issued_at=1_800_000_000,
        )
    with pytest.raises(
        RuntimeTicketIssuanceError,
        match="RUNTIME_GRANT_ISSUANCE_DISABLED",
    ):
        issuer.session_grant(
            user_id=7,
            org_id=3,
            session_id="session-00000001",
            profile_id="default",
            runtime_agent_profile_id=51,
            model_id="model-9",
            upstream_model_id="qwen3-235b-a22b",
            config_revision="revision-18",
            profile_desired_revision=0,
            profile_applied_revision=0,
            profile_digest=EMPTY_DIGEST,
            skill_digest=EMPTY_DIGEST,
            mcp_digest=EMPTY_DIGEST,
            knowledge_digest=EMPTY_DIGEST,
            max_prompt_bytes=1_000_000,
            max_output_tokens=32_000,
            issued_at=1_800_000_000,
        )


def test_runtime_ticket_issuer_retains_internal_admission_generation() -> None:
    issuer = RuntimeTicketIssuer(
        runtime_id="runtime-1",
        secret="runtime-secret-" + "x" * 32,
        admission_generation=7,
    )

    assert issuer.admission_generation == 7
