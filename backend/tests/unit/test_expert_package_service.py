from __future__ import annotations

import pytest

from app.services.expert_packages import (
    CreateExpertPackageCommand,
    CreateExpertPackageVersionCommand,
    ExpertPackageRecord,
    ExpertPackageService,
    ExpertPackageValidationError,
    validate_manifest,
)
from app.services.runtime_workspace import Principal


class FakeExpertPackageStore:
    def __init__(self) -> None:
        self.packages: dict[int, ExpertPackageRecord] = {}
        self.created_packages: list[dict] = []
        self.created_versions: list[dict] = []
        self.next_package_id = 1
        self.next_version_id = 10

    async def create_package(self, *, org_id: int, owner_user_id: int, command: CreateExpertPackageCommand):
        record = ExpertPackageRecord(
            expert_package_id=self.next_package_id,
            org_id=org_id,
            owner_user_id=owner_user_id,
            slug=command.slug,
            name=command.name,
            description=command.description,
            avatar_url=command.avatar_url,
            status="draft",
            visibility=command.visibility,
        )
        self.packages[record.expert_package_id] = record
        self.created_packages.append(
            {
                "org_id": org_id,
                "owner_user_id": owner_user_id,
                "slug": command.slug,
                "visibility": command.visibility,
            }
        )
        self.next_package_id += 1
        return record

    async def get_package(self, *, org_id: int, owner_user_id: int, expert_package_id: int):
        record = self.packages.get(expert_package_id)
        if record and record.org_id == org_id and record.owner_user_id == owner_user_id:
            return record
        return None

    async def create_version(self, *, org_id: int, owner_user_id: int, command: CreateExpertPackageVersionCommand):
        self.created_versions.append(
            {
                "org_id": org_id,
                "owner_user_id": owner_user_id,
                "expert_package_id": command.expert_package_id,
                "plugin_id": command.manifest["capabilities"]["hermes_plugin"]["plugin_id"],
            }
        )
        from app.services.expert_packages import ExpertPackageVersionRecord

        return ExpertPackageVersionRecord(
            expert_package_version_id=self.next_version_id,
            org_id=org_id,
            expert_package_id=command.expert_package_id,
            version=1,
            manifest=command.manifest,
            prompt_snapshot=command.prompt_snapshot,
            skill_refs=command.skill_refs,
            mcp_refs=command.mcp_refs,
            knowledge_refs=command.knowledge_refs,
            model_preferences=command.model_preferences,
            status="draft",
        )


def valid_manifest() -> dict:
    return {
        "schema_version": "expert.package/v1",
        "id": "sales-opportunity-analyst",
        "display_name": "Sales Opportunity Analyst",
        "capabilities": {
            "hermes_plugin": {
                "plugin_id": "sales-opportunity-analyst",
                "entry": "plugin.yaml",
            },
            "skills": [{"skill_id": 1001, "version_id": 2001}],
            "mcps": [{"mcp_id": 3001, "config_ref": "platform-managed"}],
            "knowledge": [{"knowledge_base_id": 5001, "binding_policy": "optional"}],
        },
    }


async def test_create_package_uses_platform_owner_and_normalizes_fields() -> None:
    store = FakeExpertPackageStore()

    result = await ExpertPackageService(store).create_package(
        principal=Principal(org_id=7, user_id=9),
        command=CreateExpertPackageCommand(
            slug="sales-opportunity-analyst",
            name=" Sales Opportunity Analyst ",
            visibility="organization",
        ),
    )

    assert result.expert_package_id == 1
    assert result.owner_user_id == 9
    assert result.name == "Sales Opportunity Analyst"
    assert store.created_packages == [
        {
            "org_id": 7,
            "owner_user_id": 9,
            "slug": "sales-opportunity-analyst",
            "visibility": "organization",
        }
    ]


async def test_create_version_requires_owner_package_and_valid_manifest() -> None:
    store = FakeExpertPackageStore()
    service = ExpertPackageService(store)
    package = await service.create_package(
        principal=Principal(org_id=7, user_id=9),
        command=CreateExpertPackageCommand(slug="sales", name="Sales"),
    )

    result = await service.create_version(
        principal=Principal(org_id=7, user_id=9),
        command=CreateExpertPackageVersionCommand(
            expert_package_id=package.expert_package_id,
            manifest=valid_manifest(),
            prompt_snapshot="persona",
        ),
    )

    assert result.expert_package_version_id == 10
    assert store.created_versions == [
        {
            "org_id": 7,
            "owner_user_id": 9,
            "expert_package_id": package.expert_package_id,
            "plugin_id": "sales-opportunity-analyst",
        }
    ]


async def test_create_version_rejects_other_user_package() -> None:
    store = FakeExpertPackageStore()
    package = await ExpertPackageService(store).create_package(
        principal=Principal(org_id=7, user_id=9),
        command=CreateExpertPackageCommand(slug="sales", name="Sales"),
    )

    with pytest.raises(ExpertPackageValidationError, match="expert package not found"):
        await ExpertPackageService(store).create_version(
            principal=Principal(org_id=7, user_id=10),
            command=CreateExpertPackageVersionCommand(
                expert_package_id=package.expert_package_id,
                manifest=valid_manifest(),
            ),
        )


@pytest.mark.parametrize(
    "key",
    ["api_key", "runtime_id", "workspace_id", "session_history", "host_path", "mcp_secret"],
)
def test_validate_manifest_rejects_runtime_secret_and_old_workspace_keys(key: str) -> None:
    manifest = valid_manifest()
    manifest["capabilities"]["hermes_plugin"][key] = "forbidden"

    with pytest.raises(ExpertPackageValidationError, match="forbidden key"):
        validate_manifest(manifest)


def test_validate_manifest_requires_hermes_plugin_projection_target() -> None:
    manifest = valid_manifest()
    manifest["capabilities"]["hermes_plugin"] = {}

    with pytest.raises(ExpertPackageValidationError, match="plugin_id"):
        validate_manifest(manifest)
