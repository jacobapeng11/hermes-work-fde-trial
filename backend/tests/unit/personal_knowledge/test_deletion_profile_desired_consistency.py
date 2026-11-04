from __future__ import annotations

from sqlalchemy import select

from app.models import (
    KnowledgeBase,
    ProfileKnowledgeBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
)
from app.schemas.profile_desired import DefaultProfileAssetMutation
from app.services.personal_knowledge import PersonalKnowledgeService
from app.services.profile_desired import ProfileDesiredService
from app.services.profile_desired_mutations import ProfileDesiredMutationService
from app.services.runtime_workspace import Principal
from tests.unit.test_profile_desired import (
    _command,
    _database,
    _seed,
    _skill,
)


class FakeDeletingProvider:
    def __init__(self) -> None:
        self.deleted_datasets: list[str] = []

    async def delete_dataset(self, dataset_id: str) -> None:
        self.deleted_datasets.append(dataset_id)


class OwnerAcl:
    def __init__(self, session) -> None:
        self.session = session

    async def accessible(self, *, knowledge_base_id: int, **_kwargs):
        knowledge_base = await self.session.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is None:
            raise AssertionError("test knowledge base disappeared before delete")
        return knowledge_base, "owner"


async def test_delete_advances_default_desired_and_allows_skill_replay() -> None:
    engine, sessions = await _database()
    async with sessions() as session:
        await _seed(session)
        second_skill, second_version = _skill(
            skill_id=102,
            version_id=202,
            owner_user_id=2,
            asset_scope="personal",
        )
        knowledge_base = KnowledgeBase(
            id=501,
            org_id=1,
            owner_user_id=2,
            slug="personal-kb-test",
            name="个人知识库",
            kind="personal",
            provider="filebase",
            dataset_id="dataset-501",
            status="active",
        )
        session.add_all([second_skill, second_version, knowledge_base])
        await session.commit()

        await ProfileDesiredService(session).replace(_command(knowledge_base_ids=[501]))
        await session.commit()

        provider = FakeDeletingProvider()
        service = PersonalKnowledgeService(
            session,
            provider=provider,  # type: ignore[arg-type]
            acl=OwnerAcl(session),  # type: ignore[arg-type]
        )
        await service.delete(
            principal=Principal(org_id=1, user_id=2),
            knowledge_base_id=501,
        )

        profile = await session.get(RuntimeAgentProfile, 10)
        deletion_revision = await session.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id == 10,
                RuntimeAgentProfileRevision.revision == 2,
            )
        )
        assert profile is not None
        assert provider.deleted_datasets == ["dataset-501"]
        assert profile.desired_revision == 2
        assert profile.capability_config is not None
        assert profile.capability_config["knowledge_base_ids"] == []
        assert deletion_revision is not None
        assert deletion_revision.source_type == "default_user"
        assert deletion_revision.source_id == "knowledge-delete-501-1"
        assert deletion_revision.snapshot["knowledge"] == {
            "enterprise_knowledge_enabled": True,
            "personal_knowledge_base_ids": [],
        }
        assert (
            await session.scalar(
                select(ProfileKnowledgeBinding).where(
                    ProfileKnowledgeBinding.runtime_agent_profile_id == 10
                )
            )
            is None
        )
        assert await session.get(KnowledgeBase, 501) is None

        result = await ProfileDesiredMutationService(session).mutate_default_asset(
            DefaultProfileAssetMutation(
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_agent_profile_id=10,
                asset_type="skill",
                action="install",
                asset_id=102,
                operation_id="personal-skill-install-after-kb-delete",
            )
        )
        await session.commit()

        assert result.created_revision is True
        assert result.desired_revision == 3
        replay_revision = await session.scalar(
            select(RuntimeAgentProfileRevision).where(
                RuntimeAgentProfileRevision.runtime_agent_profile_id == 10,
                RuntimeAgentProfileRevision.revision == result.desired_revision,
            )
        )
        assert replay_revision is not None
        assert [item["skill_id"] for item in replay_revision.snapshot["skills"]] == [
            101,
            102,
        ]
        assert replay_revision.snapshot["knowledge"]["personal_knowledge_base_ids"] == []
    await engine.dispose()
