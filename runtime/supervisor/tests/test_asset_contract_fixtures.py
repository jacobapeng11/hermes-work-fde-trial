import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from platform_supervisor.asset_contracts import (
    CapabilityErrorEvent,
    ProfileExecutionControlRequest,
    ProfileExecutionControlResponse,
    ProfileProjectionApplyRequest,
    ProfileProjectionApplyResponse,
    RuntimeMcpRegistryApplyRequest,
    RuntimeMcpRegistryApplyResponse,
)


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "runtime-asset-control-v1"
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_supervisor_profile_projection_contract_matches_shared_fixture() -> None:
    payload = _fixture("profile-projection-apply.json")

    parsed = ProfileProjectionApplyRequest.model_validate(payload)

    assert parsed.model_dump(mode="json") == payload


def test_supervisor_runtime_mcp_registry_contract_matches_shared_fixture() -> None:
    payload = _fixture("runtime-mcp-registry-apply.json")

    parsed = RuntimeMcpRegistryApplyRequest.model_validate(payload)

    assert parsed.model_dump(mode="json") == payload


def test_supervisor_profile_projection_result_matches_shared_fixture() -> None:
    payload = _fixture("profile-projection-result.json")

    parsed = ProfileProjectionApplyResponse.model_validate(payload)

    assert parsed.model_dump(mode="json") == payload


def test_supervisor_runtime_mcp_registry_result_matches_shared_fixture() -> None:
    payload = _fixture("runtime-mcp-registry-result.json")

    parsed = RuntimeMcpRegistryApplyResponse.model_validate(payload)

    assert parsed.model_dump(mode="json") == payload


@pytest.mark.parametrize(
    ("fixture_name", "contract"),
    [
        ("profile-execution-revoke.json", ProfileExecutionControlRequest),
        ("profile-execution-result.json", ProfileExecutionControlResponse),
        ("capability-error-event.json", CapabilityErrorEvent),
    ],
)
def test_supervisor_execution_and_capability_contracts_match_shared_fixtures(
    fixture_name: str,
    contract: type,
) -> None:
    payload = _fixture(fixture_name)

    parsed = contract.model_validate(payload)

    assert parsed.model_dump(mode="json") == payload


def test_supervisor_registry_contract_rejects_plaintext_credentials() -> None:
    payload = _fixture("runtime-mcp-registry-apply.json")
    payload["servers"][0]["connection_config"]["url"] = (
        "https://user:password@mcp.example.com"
    )

    with pytest.raises(ValidationError, match="credential_refs"):
        RuntimeMcpRegistryApplyRequest.model_validate(payload)
