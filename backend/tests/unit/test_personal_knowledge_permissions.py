from types import SimpleNamespace

import pytest

from app.services.personal_knowledge import PersonalKnowledgeError, PersonalKnowledgeService
from app.services.runtime_workspace import Principal


class FakeResult:
    def __init__(self, *, scalar_values=(), rows=()) -> None:
        self.scalar_values = list(scalar_values)
        self.rows = list(rows)

    def scalars(self):
        return self.scalar_values

    def all(self):
        return self.rows


class PermissionResponseSession:
    def __init__(self) -> None:
        self.results = [
            FakeResult(
                scalar_values=[
                    SimpleNamespace(
                        kb_role="download",
                        grantee_type="user",
                        grantee_id="12",
                    ),
                    SimpleNamespace(
                        kb_role="preview",
                        grantee_type="user",
                        grantee_id="13",
                    ),
                    SimpleNamespace(
                        kb_role="retrieve",
                        grantee_type="user",
                        grantee_id="99",
                    ),
                ]
            ),
            FakeResult(
                rows=[
                    SimpleNamespace(
                        id=12,
                        name="示例用户乙",
                        user_code="zhangyi",
                        status="active",
                    ),
                    SimpleNamespace(
                        id=13,
                        name="李明",
                        user_code="liming",
                        status="disabled",
                    ),
                ]
            ),
            FakeResult(
                rows=[
                    SimpleNamespace(code="zhangyi", dept_id="5101"),
                    SimpleNamespace(code="liming", dept_id="5102"),
                ]
            ),
            FakeResult(
                rows=[
                    SimpleNamespace(dept_id="2", parent_id=None, name="总部"),
                    SimpleNamespace(dept_id="5101", parent_id="2", name="研发中心"),
                    SimpleNamespace(dept_id="5102", parent_id="2", name="产品中心"),
                ]
            ),
        ]

    async def execute(self, _statement):
        return self.results.pop(0)


class ActiveUsersSession:
    async def execute(self, _statement):
        return FakeResult(scalar_values=[12])


class RevokedBindingSession:
    async def execute(self, _statement):
        return FakeResult(rows=[
            (
                SimpleNamespace(id=31, runtime_agent_profile_id=101),
                SimpleNamespace(
                    id=101,
                    org_id=3,
                    user_id=12,
                    runtime_workspace_id=7,
                    profile_kind="default",
                ),
            ),
            (
                SimpleNamespace(id=32, runtime_agent_profile_id=202),
                SimpleNamespace(
                    id=202,
                    org_id=3,
                    user_id=12,
                    runtime_workspace_id=7,
                    profile_kind="expert",
                ),
            ),
        ])


class RevokedBindingAcl:
    async def effective_role(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_permission_response_hydrates_active_inactive_and_missing_users() -> None:
    service = PersonalKnowledgeService(PermissionResponseSession())  # type: ignore[arg-type]

    response = await service._permission_response(9, org_id=3)

    assert response.download.user_ids == [12]
    assert response.preview.user_ids == [13]
    assert response.retrieve.user_ids == [99]
    assert [user.model_dump() for user in response.selected_users] == [
        {
            "id": 12,
            "name": "示例用户乙",
            "user_code": "zhangyi",
            "status": "active",
        },
        {
            "id": 13,
            "name": "李明",
            "user_code": "liming",
            "status": "inactive",
        },
        {
            "id": 99,
            "name": None,
            "user_code": None,
            "status": "missing",
        },
    ]
    assert [grant.model_dump() for grant in response.grants] == [
        {
            "target_type": "user",
            "target_id": 12,
            "name": "示例用户乙",
            "user_code": "zhangyi",
            "department_id": "5101",
            "department_path": ["总部", "研发中心"],
            "status": "active",
            "role": "download",
        },
        {
            "target_type": "user",
            "target_id": 13,
            "name": "李明",
            "user_code": "liming",
            "department_id": "5102",
            "department_path": ["总部", "产品中心"],
            "status": "inactive",
            "role": "preview",
        },
        {
            "target_type": "user",
            "target_id": 99,
            "name": None,
            "user_code": None,
            "department_id": None,
            "department_path": [],
            "status": "missing",
            "role": "retrieve",
        },
    ]


@pytest.mark.asyncio
async def test_permission_validation_allows_existing_missing_user_but_rejects_new_one() -> None:
    service = PersonalKnowledgeService(ActiveUsersSession())  # type: ignore[arg-type]
    principal = Principal(org_id=3, user_id=8)

    await service._validate_permission_targets(
        principal=principal,
        user_ids={12, 99},
        department_ids=set(),
        existing_user_ids={99},
    )

    with pytest.raises(PersonalKnowledgeError, match="invalid_permission_user"):
        await service._validate_permission_targets(
            principal=principal,
            user_ids={12, 100},
            department_ids=set(),
            existing_user_ids={99},
        )


@pytest.mark.asyncio
async def test_revoked_permissions_keep_expert_profile_configuration(monkeypatch) -> None:
    default_mutations: list[int] = []
    generic_mutations: list[int] = []

    class FakeMutationService:
        def __init__(self, _session) -> None:
            pass

        async def mutate_default_asset(self, mutation) -> None:
            default_mutations.append(mutation.runtime_agent_profile_id)

        async def mutate_profile_asset(self, mutation) -> None:
            generic_mutations.append(mutation.runtime_agent_profile_id)

    monkeypatch.setattr(
        "app.services.personal_knowledge.ProfileDesiredMutationService",
        FakeMutationService,
    )
    service = PersonalKnowledgeService(RevokedBindingSession())  # type: ignore[arg-type]
    service.acl = RevokedBindingAcl()  # type: ignore[assignment]

    await service._disable_revoked_bindings(SimpleNamespace(id=19))

    assert default_mutations == [101]
    assert generic_mutations == []
