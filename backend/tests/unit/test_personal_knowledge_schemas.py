import pytest
from pydantic import ValidationError

from app.schemas.personal_knowledge import (
    PersonalKnowledgeCreate,
    PersonalKnowledgePermissionResponse,
    PersonalKnowledgePermissionReplace,
    PersonalKnowledgeSummary,
    RuntimePersonalKnowledgeState,
)


def _permissions() -> dict:
    return {
        "download": {"user_ids": [2], "department_ids": [], "all": False},
        "preview": {"user_ids": [], "department_ids": ["dept-1"], "all": False},
        "retrieve": {"user_ids": [], "department_ids": [], "all": True},
    }


def test_permission_payload_rejects_unknown_roles() -> None:
    with pytest.raises(ValidationError):
        PersonalKnowledgePermissionReplace.model_validate(
            {**_permissions(), "admin": {"user_ids": [2]}}
        )


def test_permission_response_includes_selected_user_details() -> None:
    response = PersonalKnowledgePermissionResponse.model_validate(
        {
            **_permissions(),
            "selected_users": [
                {
                    "id": 2,
                    "name": "示例用户乙",
                    "user_code": "zhangyi",
                    "status": "active",
                },
                {
                    "id": 99,
                    "name": None,
                    "user_code": None,
                    "status": "missing",
                },
            ],
            "grants": [
                {
                    "target_type": "department",
                    "target_id": "dept-1",
                    "name": "研发中心",
                    "department_path": ["总部", "研发中心"],
                    "role": "preview",
                },
                {
                    "target_type": "user",
                    "target_id": 2,
                    "name": "示例用户乙",
                    "user_code": "zhangyi",
                    "department_id": "dept-1",
                    "department_path": ["总部", "研发中心"],
                    "status": "active",
                    "role": "download",
                },
            ],
        }
    )

    assert response.selected_users[0].user_code == "zhangyi"
    assert response.selected_users[1].status == "missing"
    assert response.grants[0].target_type == "department"
    assert response.grants[1].target_type == "user"


def test_create_payload_strips_name_and_rejects_blank_name() -> None:
    assert PersonalKnowledgeCreate(name="  案例库  ").name == "案例库"
    with pytest.raises(ValidationError):
        PersonalKnowledgeCreate(name="  ")


def test_public_contracts_do_not_expose_provider_or_oss_fields() -> None:
    summary_fields = set(PersonalKnowledgeSummary.model_fields)
    state_fields = set(RuntimePersonalKnowledgeState.model_fields)

    assert {"dataset_id", "oss_url", "oss_key"}.isdisjoint(summary_fields)
    assert {"dataset_id", "oss_url", "oss_key"}.isdisjoint(state_fields)
