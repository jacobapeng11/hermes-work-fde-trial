from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.personal_knowledge_acl import (
    PersonalKnowledgeAclError,
    PersonalKnowledgeAclService,
    highest_role,
    role_allows,
)


class FakePermissionResult:
    def __init__(self, rows: list[tuple[int, str | None]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[int, str | None]]:
        return self.rows


class FakePermissionSession:
    def __init__(self, rows: list[tuple[int, str | None]]) -> None:
        self.rows = rows
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakePermissionResult:
        self.statements.append(statement)
        return FakePermissionResult(self.rows)


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ([], None),
        (["retrieve"], "retrieve"),
        (["retrieve", "preview"], "preview"),
        (["preview", "download"], "download"),
        (["download", "owner"], "owner"),
    ],
)
def test_highest_role_uses_permission_order(roles, expected) -> None:
    assert highest_role(roles) == expected


def test_role_allows_matches_the_approved_permission_matrix() -> None:
    assert role_allows("retrieve", "retrieve") is True
    assert role_allows("retrieve", "preview") is False
    assert role_allows("preview", "preview") is True
    assert role_allows("preview", "download") is False
    assert role_allows("download", "download") is True
    assert role_allows("owner", "manage") is True


def test_unknown_roles_fail_closed() -> None:
    assert role_allows("admin", "retrieve") is False
    assert highest_role(["admin"]) is None


def test_require_role_uses_404_for_invisible_and_403_for_insufficient() -> None:
    with pytest.raises(PersonalKnowledgeAclError) as invisible:
        PersonalKnowledgeAclService.require_role(None, "retrieve")
    with pytest.raises(PersonalKnowledgeAclError) as insufficient:
        PersonalKnowledgeAclService.require_role("retrieve", "preview")

    assert invisible.value.status_code == 404
    assert insufficient.value.status_code == 403


async def test_effective_roles_batches_non_owner_permissions_and_preserves_roles() -> None:
    session = FakePermissionSession(
        [
            (2, "retrieve"),
            (2, "preview"),
            (3, "download"),
            (4, "retrieve"),
            (4, "unknown"),
        ]
    )
    subject = PersonalKnowledgeAclService(session)  # type: ignore[arg-type]
    subject._department_ids = AsyncMock(  # type: ignore[method-assign]
        return_value={"department-child", "department-parent"}
    )
    principal = SimpleNamespace(org_id=7, user_id=9)
    knowledge_bases = [
        SimpleNamespace(id=1, org_id=7, owner_user_id=9),
        SimpleNamespace(id=2, org_id=7, owner_user_id=20),
        SimpleNamespace(id=3, org_id=7, owner_user_id=30),
        SimpleNamespace(id=4, org_id=7, owner_user_id=40),
        SimpleNamespace(id=5, org_id=8, owner_user_id=9),
    ]

    roles = await subject.effective_roles(
        principal=principal,  # type: ignore[arg-type]
        knowledge_bases=knowledge_bases,  # type: ignore[arg-type]
    )

    assert roles == {
        1: "owner",
        2: "preview",
        3: "download",
        4: "retrieve",
    }
    subject._department_ids.assert_awaited_once_with(principal)  # type: ignore[attr-defined]
    assert len(session.statements) == 1
    compiled = session.statements[0].compile()
    assert any(
        isinstance(value, list) and set(value) == {2, 3, 4}
        for value in compiled.params.values()
    )
    sql = str(compiled)
    assert "permission.asset_id IN" in sql
    assert "permission.grantee_type" in sql


async def test_effective_roles_skips_directory_and_permission_queries_when_unneeded() -> None:
    session = FakePermissionSession([])
    subject = PersonalKnowledgeAclService(session)  # type: ignore[arg-type]
    subject._department_ids = AsyncMock(return_value=set())  # type: ignore[method-assign]
    principal = SimpleNamespace(org_id=7, user_id=9)

    empty_roles = await subject.effective_roles(
        principal=principal,  # type: ignore[arg-type]
        knowledge_bases=[],
    )
    owner_roles = await subject.effective_roles(
        principal=principal,  # type: ignore[arg-type]
        knowledge_bases=[  # type: ignore[arg-type]
            SimpleNamespace(id=1, org_id=7, owner_user_id=9),
            SimpleNamespace(id=2, org_id=8, owner_user_id=9),
        ],
    )

    assert empty_roles == {}
    assert owner_roles == {1: "owner"}
    subject._department_ids.assert_not_awaited()  # type: ignore[attr-defined]
    assert session.statements == []
