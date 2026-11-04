from types import SimpleNamespace

import pytest

from app.config import settings
import app.services.default_mcp_bootstrap as bootstrap_module
from app.services.default_mcp_bootstrap import (
    DefaultMcpBootstrapService,
    FAILED,
    PENDING,
    SUCCEEDED,
    default_mcp_bootstrap_status,
    with_default_mcp_bootstrap_status,
)
from app.services.runtime_workspace import RuntimeWorkspaceRecord
from app.repositories.runtime import RuntimeWorkspaceRepository


def record() -> RuntimeWorkspaceRecord:
    return RuntimeWorkspaceRecord(
        runtime_workspace_id=17,
        cloud_profile_id=17,
        org_id=3,
        user_id=9,
        user_name="测试用户",
        user_code="employee-9",
        name="默认数字人",
        status="active",
        runtime_status="ready",
    )


def test_default_mcp_bootstrap_status_preserves_unrelated_profile_config() -> None:
    original = {"theme": "dark", "default_mcp_bootstrap": {"status": "pending"}}

    updated = with_default_mcp_bootstrap_status(
        original,
        status=FAILED,
        slug="web-search-mcp",
        error="  unavailable\n",
    )

    assert original["default_mcp_bootstrap"] == {"status": "pending"}
    assert updated["theme"] == "dark"
    assert default_mcp_bootstrap_status(updated) == FAILED
    assert updated["default_mcp_bootstrap"] == {
        "status": "failed",
        "slug": "web-search-mcp",
        "error": "unavailable",
    }


def test_new_default_profile_starts_with_the_one_time_mcp_marker() -> None:
    statement = RuntimeWorkspaceRepository.create_default_profile_statement(
        org_id=3,
        user_id=9,
    )

    assert statement.compile().params["profile_config"] == {
        "default_mcp_bootstrap": {"status": "pending"},
        "knowledge": {"enterprise": {"enabled": True, "mode": "full"}},
    }


class FakeSession:
    def __init__(self, values):
        self.values = list(values)
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, _statement):
        return self.values.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_default_mcp_bootstrap_installs_once_and_does_not_rearm(monkeypatch) -> None:
    profile = SimpleNamespace(profile_config={"default_mcp_bootstrap": {"status": PENDING}})
    asset = SimpleNamespace(id=42)
    default_profile = SimpleNamespace(id=84)
    session = FakeSession([profile, asset, default_profile])
    calls = []

    class FakeMutationService:
        def __init__(self, session):
            assert session is not None

        async def mutate_default_asset(self, mutation):
            calls.append(mutation)

    monkeypatch.setattr(settings, "PERSONAL_DEFAULT_MCP_SLUG", "web-search-mcp")
    monkeypatch.setattr(bootstrap_module, "ProfileDesiredMutationService", FakeMutationService)

    await DefaultMcpBootstrapService(session=session).ensure_for_runtime(record())

    assert len(calls) == 1
    assert calls[0].runtime_agent_profile_id == 84
    assert calls[0].asset_id == 42
    assert calls[0].asset_type == "mcp"
    assert default_mcp_bootstrap_status(profile.profile_config) == SUCCEEDED
    assert session.commits == 1


@pytest.mark.asyncio
async def test_missing_default_mcp_records_failure_without_raising(monkeypatch) -> None:
    profile = SimpleNamespace(profile_config={"default_mcp_bootstrap": {"status": PENDING}})
    session = FakeSession([profile, None, SimpleNamespace(id=84), profile])
    monkeypatch.setattr(settings, "PERSONAL_DEFAULT_MCP_SLUG", "web-search-mcp")

    await DefaultMcpBootstrapService(session=session).ensure_for_runtime(record())

    assert default_mcp_bootstrap_status(profile.profile_config) == FAILED
    assert profile.profile_config["default_mcp_bootstrap"]["slug"] == "web-search-mcp"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_bootstrap_does_not_wait_for_active_run(monkeypatch) -> None:
    profile = SimpleNamespace(profile_config={"default_mcp_bootstrap": {"status": PENDING}})
    session = FakeSession([profile, SimpleNamespace(id=42), SimpleNamespace(id=84)])
    calls = []

    class FakeMutationService:
        def __init__(self, _session):
            pass

        async def mutate_default_asset(self, mutation):
            calls.append(mutation)

    monkeypatch.setattr(settings, "PERSONAL_DEFAULT_MCP_SLUG", "web-search-mcp")
    monkeypatch.setattr(bootstrap_module, "ProfileDesiredMutationService", FakeMutationService)

    await DefaultMcpBootstrapService(session=session).ensure_for_runtime(record())

    assert len(calls) == 1
    assert default_mcp_bootstrap_status(profile.profile_config) == SUCCEEDED
    assert session.commits == 1
