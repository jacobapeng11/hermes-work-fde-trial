from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas.experts import (
    ApproveExpertReviewRequest,
    CreateExpertRequest,
    ExpertShareScope,
)


def test_v2_expert_profile_and_market_routes_replace_legacy_plaza_routes() -> None:
    paths = app.openapi()["paths"]

    assert "/api/v1/user/expert-plaza" not in paths
    assert set(paths["/api/v1/user/experts/market"]) >= {"get"}
    assert "/api/v1/user/experts/installed" not in paths
    assert set(paths["/api/v1/user/experts/runnable"]) >= {"get"}
    assert set(paths["/api/v1/user/experts"]) >= {"post"}
    assert set(paths["/api/v1/user/experts/{runtime_agent_profile_id}"]) >= {
        "get",
        "patch",
        "delete",
    }
    assert set(paths["/api/v1/user/experts/market/{expert_package_id}/install"]) >= {
        "post"
    }
    assert set(paths["/api/v1/admin/experts/reviews/{review_id}/approve"]) >= {
        "post"
    }
    assert set(paths["/api/v1/admin/experts/reviews/{review_id}/reject"]) >= {
        "post"
    }
    assert set(paths["/api/v1/admin/experts/directory/users/search"]) >= {
        "get"
    }
    assert set(paths["/api/v1/admin/experts/directory/departments/tree"]) >= {
        "get"
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        {"hermes_profile_name": "attacker-selected"},
        {"model": "private-model"},
        {"provider": "private-provider"},
        {"base_url": "https://private.invalid"},
        {"api_key": "secret"},
        {"plugin_code": "print('arbitrary code')"},
    ],
)
def test_create_expert_rejects_runtime_identity_private_model_and_code_fields(
    forbidden: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        CreateExpertRequest.model_validate(
            {
                "runtime_workspace_id": 1,
                "name": "安全专家",
                **forbidden,
            }
        )


def test_admin_approval_uses_frozen_scope_and_rejects_scope_override() -> None:
    approval = ApproveExpertReviewRequest(review_comment="符合发布要求")

    assert approval.review_comment == "符合发布要求"

    with pytest.raises(ValidationError):
        ApproveExpertReviewRequest.model_validate(
            {
                "share_scope": {"organization": True},
                "review_comment": "不得覆盖创建者范围",
            }
        )


def test_organization_scope_is_exclusive_and_backward_compatible() -> None:
    legacy = ExpertShareScope.model_validate(
        {"user_ids": [2], "department_ids": []}
    )
    assert legacy.organization is False

    with pytest.raises(ValidationError):
        ExpertShareScope(
            organization=True,
            user_ids=[2],
        )

    with pytest.raises(ValidationError):
        ExpertShareScope(
            organization=True,
            department_ids=["dept-a"],
        )
