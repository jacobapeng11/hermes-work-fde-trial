from __future__ import annotations

import pytest

from platform_supervisor.security import (
    RuntimeAccessReplay,
    RuntimeTokenError,
    RuntimeTokenVerifier,
)
from platform_supervisor.security.tokens import encode_runtime_token


SECRET = "runtime-secret-" + "x" * 32


def access_claims(**overrides):
    result = {
        "token_type": "runtime_access",
        "schema_version": 1,
        "sub": "user-7",
        "user_id": 7,
        "org_id": 3,
        "runtime_id": "runtime-1",
        "admission_generation": 11,
        "allowed_origin": "https://agent.example.com",
        "connection_nonce": "nonce-0000000000000001",
        "issued_at": 1_800_000_000,
        "expires_at": 1_800_000_300,
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_runtime_access_is_origin_bound_and_one_time() -> None:
    verifier = RuntimeTokenVerifier(
        runtime_id="runtime-1",
        secret=SECRET,
        clock=lambda: 1_800_000_010,
    )
    token = encode_runtime_token(access_claims(), secret=SECRET)

    claims = await verifier.verify_runtime_access(
        token,
        origin="https://agent.example.com",
    )
    assert claims.user_id == 7
    with pytest.raises(RuntimeAccessReplay):
        await verifier.verify_runtime_access(
            token,
            origin="https://agent.example.com",
        )


@pytest.mark.asyncio
async def test_runtime_access_rejects_wrong_origin_runtime_signature_and_expiry() -> None:
    verifier = RuntimeTokenVerifier(
        runtime_id="runtime-1",
        secret=SECRET,
        clock=lambda: 1_800_000_400,
    )
    with pytest.raises(RuntimeTokenError, match="Origin"):
        await RuntimeTokenVerifier(
            runtime_id="runtime-1",
            secret=SECRET,
            clock=lambda: 1_800_000_010,
        ).verify_runtime_access(
            encode_runtime_token(access_claims(), secret=SECRET),
            origin="https://evil.example.com",
        )
    with pytest.raises(RuntimeTokenError, match="another Runtime"):
        await RuntimeTokenVerifier(
            runtime_id="runtime-2",
            secret=SECRET,
            clock=lambda: 1_800_000_010,
        ).verify_runtime_access(
            encode_runtime_token(access_claims(), secret=SECRET),
            origin="https://agent.example.com",
        )
    with pytest.raises(RuntimeTokenError, match="expired"):
        await verifier.verify_runtime_access(
            encode_runtime_token(access_claims(), secret=SECRET),
            origin="https://agent.example.com",
        )
    with pytest.raises(RuntimeTokenError, match="signature"):
        await RuntimeTokenVerifier(
            runtime_id="runtime-1",
            secret=SECRET,
            clock=lambda: 1_800_000_010,
        ).verify_runtime_access(
            encode_runtime_token(
                access_claims(),
                secret="different-secret-" + "y" * 32,
            ),
            origin="https://agent.example.com",
        )


def test_session_grant_is_bound_to_runtime_and_limits() -> None:
    verifier = RuntimeTokenVerifier(
        runtime_id="runtime-1",
        secret=SECRET,
        clock=lambda: 1_800_000_010,
    )
    token = encode_runtime_token(
        {
            "token_type": "session_grant",
            "schema_version": 1,
            "user_id": 7,
            "org_id": 3,
            "runtime_id": "runtime-1",
            "admission_generation": 11,
            "session_id": "session-00000001",
            "profile_id": "default",
            "runtime_agent_profile_id": 51,
            "model_id": "model-9",
            "upstream_model_id": "qwen3-235b-a22b",
            "config_revision": "revision-18",
            "profile_desired_revision": 0,
            "profile_applied_revision": 0,
            "profile_digest": "sha256:" + "0" * 64,
            "skill_digest": "sha256:" + "0" * 64,
            "mcp_digest": "sha256:" + "0" * 64,
            "knowledge_digest": "sha256:" + "0" * 64,
            "max_prompt_bytes": 1_000_000,
            "max_output_tokens": 32_000,
            "issued_at": 1_800_000_000,
            "expires_at": 1_800_001_800,
        },
        secret=SECRET,
    )
    claims = verifier.verify_session_grant(token)
    assert claims.max_prompt_bytes == 1_000_000
    assert claims.runtime_agent_profile_id == 51
