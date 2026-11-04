import pytest

from fastapi import HTTPException

from app.api.v1.user.runtime_sessions import create_runtime_session
from app.config import settings
from app.models import RuntimeAgentProfile
from app.schemas.runtime_chat import CreateRuntimeSessionRequest
from app.services.runtime_models import RuntimeModelRecord
from app.services.runtime_sessions import (
    CreateRuntimeSessionCommand,
    RuntimeSessionProfilePreparingError,
    SqlAlchemyRuntimeSessionStore,
)
from app.services.runtime_workspace import Principal


class FakeProfileResult:
    def __init__(self, profile):
        self.profile = profile

    def scalars(self):
        return self

    def first(self):
        return self.profile


class FakeProfileDb:
    def __init__(self, profiles):
        self.profiles = list(profiles)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeProfileResult(self.profiles.pop(0))


class OwnedProfileSessionStore(SqlAlchemyRuntimeSessionStore):
    async def _assert_workspace_owner(self, **_kwargs):
        return None


def preparing_profile() -> RuntimeAgentProfile:
    return RuntimeAgentProfile(
        id=5,
        org_id=1,
        user_id=9,
        runtime_workspace_id=10,
        hermes_profile_name="default",
        profile_kind="default",
        display_name="默认数字人",
        status="active",
        desired_revision=1,
        applied_revision=0,
    )


async def test_first_profile_revision_reports_preparing() -> None:
    db = FakeProfileDb([None, preparing_profile()])

    with pytest.raises(RuntimeSessionProfilePreparingError) as exc_info:
        await OwnedProfileSessionStore(db).create_session(
            org_id=1,
            user_id=9,
            command=CreateRuntimeSessionCommand(
                runtime_workspace_id=10,
                model_entry_id=3,
            ),
        )

    assert exc_info.value.code == "runtime_agent_profile_preparing"
    assert len(db.statements) == 2


async def test_missing_owned_profile_stays_not_found() -> None:
    db = FakeProfileDb([None, None])

    with pytest.raises(PermissionError, match="runtime agent profile not found"):
        await OwnedProfileSessionStore(db).create_session(
            org_id=1,
            user_id=9,
            command=CreateRuntimeSessionCommand(
                runtime_workspace_id=10,
                model_entry_id=3,
            ),
        )


@pytest.mark.asyncio
async def test_session_api_maps_preparing_profile_to_retryable_unavailable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "HERMES_RUNTIME_STORAGE_MAINTENANCE", False)

    class ModelService:
        async def list_models(self, _principal):
            return [
                RuntimeModelRecord(
                    model_entry_id=3,
                    model_key="model-a",
                    display_name="Model A",
                    provider_id=1,
                    provider_display_name="Provider",
                )
            ]

    class SessionService:
        async def create_session(self, **_kwargs):
            raise RuntimeSessionProfilePreparingError(
                RuntimeSessionProfilePreparingError.code
            )

    with pytest.raises(HTTPException) as exc_info:
        await create_runtime_session(
            runtime_workspace_id=10,
            payload=CreateRuntimeSessionRequest(source_type="web"),
            principal=Principal(org_id=1, user_id=9),
            service=SessionService(),
            model_service=ModelService(),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "runtime_agent_profile_preparing"
    assert exc_info.value.headers == {"Retry-After": "3"}
