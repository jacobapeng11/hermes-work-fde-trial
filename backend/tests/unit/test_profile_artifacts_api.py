from __future__ import annotations

from datetime import datetime
from io import BytesIO
from typing import Any
import zipfile

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.api.internal.profile_artifacts import router
from app.config import settings
from app.database import get_db
from app.models import (
    Base,
    HermesRuntimeBinding,
    RuntimeAgentProfile,
    RuntimeAgentProfileRevision,
    SkillVersion,
)
from app.services.plaza.public_catalog.package_parser import sha256_directory
from app.services.runtime_workspace import RuntimeApiKeyCodec


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "INTEGER"


@compiles(LONGTEXT, "sqlite")
def _compile_longtext_as_sqlite_text(
    _type: LONGTEXT,
    _compiler: Any,
    **_kwargs: Any,
) -> str:
    return "TEXT"


@pytest.mark.asyncio
async def test_runtime_downloads_only_artifact_pinned_by_profile_revision(
    tmp_path,
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source = tmp_path / "public/skill-101/v1"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("# CRM\n", encoding="utf-8")
    (source / "refs").mkdir()
    (source / "refs/a.txt").write_text("A", encoding="utf-8")
    digest = sha256_directory(source)
    monkeypatch.setattr(settings, "HERMES_PUBLIC_ASSET_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "HERMES_RUNTIME_ROOT", str(tmp_path / "runtime"))
    async with factory() as db:
        db.add(
            HermesRuntimeBinding(
                id=119,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                provider="knative",
                runtime_id="rt-119",
                image="runtime:test",
                k8s_namespace="test",
                knative_service="runtime-119",
                hermes_home_sub_path="home",
                workspace_sub_path="workspace",
                hermes_home_mount_path="/opt/data",
                workspace_mount_path="/workspace",
                api_key_encrypted=RuntimeApiKeyCodec().encrypt("runtime-secret"),
                status="ready",
            )
        )
        db.add(
            RuntimeAgentProfile(
                id=173,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                hermes_profile_name="expert-173",
                profile_kind="expert",
                display_name="CRM",
                ownership_type="self_created",
                desired_revision=7,
                applied_revision=6,
                status="updating",
            )
        )
        db.add(
            SkillVersion(
                id=201,
                org_id=1,
                skill_id=101,
                version=1,
                package_uri=str(source),
                package_sha256=digest,
                manifest={"entrypoint": "SKILL.md"},
                status="published",
                published_at=datetime.utcnow(),
            )
        )
        db.add(
            RuntimeAgentProfileRevision(
                id=701,
                org_id=1,
                user_id=2,
                runtime_workspace_id=3,
                runtime_agent_profile_id=173,
                revision=7,
                profile_digest=f"sha256:{'a' * 64}",
                skill_digest=f"sha256:{'b' * 64}",
                mcp_digest=f"sha256:{'c' * 64}",
                knowledge_digest=f"sha256:{'d' * 64}",
                snapshot={
                    "skills": [
                        {
                            "skill_id": 101,
                            "skill_version_id": 201,
                            "package_sha256": digest,
                        }
                    ]
                },
                source_type="private_expert",
            )
        )
        await db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/internal")

    async def database_override():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://backend.test",
    ) as client:
        response = await client.get(
            "/internal/v1/profile-artifacts/173/7/101/201",
            headers={
                "Authorization": "Bearer runtime-secret",
                "X-Hermes-Runtime-Id": "rt-119",
                "X-Hermes-Binding-Id": "119",
                "X-Hermes-Artifact-Sha256": digest,
            },
        )
        assert response.status_code == 200
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            assert archive.namelist() == ["SKILL.md", "refs/a.txt"]
            assert archive.read("SKILL.md") == b"# CRM\n"

        stale = await client.get(
            "/internal/v1/profile-artifacts/173/6/101/201",
            headers={
                "Authorization": "Bearer runtime-secret",
                "X-Hermes-Runtime-Id": "rt-119",
                "X-Hermes-Binding-Id": "119",
                "X-Hermes-Artifact-Sha256": digest,
            },
        )
        assert stale.status_code == 404
    await engine.dispose()
