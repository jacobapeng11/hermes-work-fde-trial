import json
from pathlib import Path

from app.schemas.experts import (
    EXPERT_ERROR_CODES,
    ApproveExpertReviewRequest,
    ExpertMarketDetail,
    ExpertMarketItem,
)


FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "skill-expert-integration-v1"
)


def _fixture(name: str) -> object:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_market_contracts_expose_only_approved_detail_configuration() -> None:
    item = ExpertMarketItem.model_validate(_fixture("expert-market-item.json"))
    detail = ExpertMarketDetail.model_validate(_fixture("expert-market-detail.json"))

    item_payload = item.model_dump()
    assert "skills" not in item_payload
    assert "mcps" not in item_payload
    assert "knowledge_bases" not in item_payload
    assert "soul_content" not in item_payload

    detail_payload = detail.model_dump()
    assert detail.soul_content.startswith("# 写作专家")
    assert detail.system_prompt == "先确认文章目标和读者，再生成内容。"
    assert detail.skills[0].name == "结构化写作"
    assert detail.mcps[0].name == "公开资料检索"
    assert detail.knowledge_bases[0].name == "品牌写作规范"
    for forbidden in (
        "payload",
        "source_asset_id",
        "source_version_id",
        "source_metadata",
        "storage_uri",
        "sha256",
    ):
        assert forbidden not in str(detail_payload)


def test_approve_contract_uses_frozen_scope() -> None:
    request = ApproveExpertReviewRequest.model_validate(
        _fixture("expert-review-approve.json")
    )

    assert request.review_comment == "内容与权限范围符合发布要求"
    assert "share_scope" not in request.model_dump()


def test_expert_error_code_fixture_matches_backend_contract() -> None:
    assert set(_fixture("expert-error-codes.json")) == EXPERT_ERROR_CODES
