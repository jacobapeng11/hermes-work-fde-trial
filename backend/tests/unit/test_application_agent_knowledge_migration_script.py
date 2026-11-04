from types import SimpleNamespace

import pytest

from scripts.migrate_application_agent_knowledge import (
    CANARY_VERSION,
    RECEIPT_VERSION,
    MigrationBlocked,
    _legacy_personal_ids,
    _load_json,
    _record_canary,
    _sqlbot2_source_blockers,
    _write_json,
)


def _receipt(*, sqlbot2: bool, bound_id: int | None = None) -> dict:
    return {
        "receipt_version": RECEIPT_VERSION,
        "receipt_id": "receipt-1",
        "phase": "bound" if bound_id is not None else "planned",
        "application_agent_id": 31,
        "sqlbot2": sqlbot2,
        "bound_personal_knowledge_id": bound_id,
        "blockers": [],
    }


def test_legacy_personal_source_is_recovered_only_from_exact_revision_ids() -> None:
    revisions = [
        SimpleNamespace(config_snapshot={
            "knowledge_bindings": [
                {
                    "knowledge_base_id": 41,
                    "access_mode": "delegated_user",
                    "name": "名称不能作为迁移依据",
                },
                {
                    "knowledge_base_id": 99,
                    "access_mode": "application",
                },
            ]
        }),
        SimpleNamespace(config_snapshot={
            "knowledge_bindings": [{
                "knowledge_base_id": 41,
                "access_mode": "delegated_user",
            }]
        }),
    ]

    assert _legacy_personal_ids(revisions) == [41]


def test_sqlbot2_source_accepts_current_shared_retrieve_permission() -> None:
    source = SimpleNamespace(
        kind="personal",
        status="active",
        dataset_id="dataset-21",
    )

    assert _sqlbot2_source_blockers(
        source,
        creator_effective_role="retrieve",
        ready_file_count=1,
    ) == []
    assert _sqlbot2_source_blockers(
        source,
        creator_effective_role=None,
        ready_file_count=1,
    ) == ["SQLBOT2_SOURCE_PERMISSION_MISSING"]


def test_migration_receipt_rejects_tampering(tmp_path) -> None:
    path = tmp_path / "migration.json"
    _write_json(path, _receipt(sqlbot2=False))
    document = path.read_text(encoding="utf-8").replace(
        '"application_agent_id": 31',
        '"application_agent_id": 32',
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(MigrationBlocked) as caught:
        _load_json(path, version=RECEIPT_VERSION)

    assert caught.value.code == "MIGRATION_RECEIPT_CHECKSUM_INVALID"


def test_sqlbot2_canary_requires_exact_personal_source_and_delegated_result(
    tmp_path,
) -> None:
    path = tmp_path / "canary.json"

    with pytest.raises(MigrationBlocked) as caught:
        _record_canary(
            receipt=_receipt(sqlbot2=True, bound_id=41),
            canary_path=path,
            personal_mode="selected",
            personal_knowledge_base_id=42,
            studio_verified=True,
            delegated_verified=True,
            retrieval_verified=True,
            disabled_verified=False,
        )

    assert caught.value.code == "SQLBOT2_CANARY_INCOMPLETE"
    assert not path.exists()

    result = _record_canary(
        receipt=_receipt(sqlbot2=True, bound_id=41),
        canary_path=path,
        personal_mode="selected",
        personal_knowledge_base_id=41,
        studio_verified=True,
        delegated_verified=True,
        retrieval_verified=True,
        disabled_verified=False,
    )
    assert result["receipt_version"] == CANARY_VERSION
    assert path.stat().st_mode & 0o777 == 0o600


def test_non_sqlbot_application_canary_may_confirm_personal_mode_disabled(
    tmp_path,
) -> None:
    result = _record_canary(
        receipt=_receipt(sqlbot2=False),
        canary_path=tmp_path / "canary.json",
        personal_mode="disabled",
        personal_knowledge_base_id=None,
        studio_verified=False,
        delegated_verified=False,
        retrieval_verified=False,
        disabled_verified=True,
    )

    assert result["disabled_verified"] is True
