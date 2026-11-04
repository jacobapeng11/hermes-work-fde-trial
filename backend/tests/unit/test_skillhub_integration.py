from __future__ import annotations

import io
import base64
import hashlib
import json
import zipfile
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings
from app.models import (
    Base,
    ProfileSkillBinding,
    RuntimeAgentProfile,
    Skill,
    UserSkillHubInstallation,
)
from app.services.skillhub.client import SkillHubClient, SkillHubPage, SkillHubSkill, _content_hash
from app.services.skillhub.market import SkillHubMarketService
from app.services.skillhub.installations import SkillHubInstallationOperationService
from app.services.skillhub.package_storage import UserSkillHubPackageStorage
from app.services.plaza.user_skill_operations.operation_queue import (
    OperationSnapshot,
    UserSkillOperationQueue,
)
from tests.unit.test_profile_desired import _database, _profile


def _config(**overrides) -> Settings:
    values = {
        "SKILLHUB_API_BASE_URL": "https://api.skillhub.cn",
        "SKILLHUB_ALLOWED_FEEDS": "featured,hot,trending",
        "SKILLHUB_CATEGORY_MAPPING_JSON": '{"ai-agent":"agent-automation"}',
    }
    values.update(overrides)
    return Settings(**values)


def _zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: Community Search\ndescription: test\n---\n")
    return output.getvalue()


def test_skillhub_model_contract_is_private_and_source_is_extended() -> None:
    assert "user_skillhub_installation" in Base.metadata.tables
    names = {constraint.name for constraint in UserSkillHubInstallation.__table__.constraints}
    assert "uq_user_skillhub_installation_profile_coordinate" in names
    assert "skill_id" in UserSkillHubInstallation.__table__.columns
    assert "user_runtime_skill_id" not in UserSkillHubInstallation.__table__.columns


def test_skillhub_settings_fail_closed_for_http_and_unknown_category() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        _config(SKILLHUB_API_BASE_URL="http://api.skillhub.cn")
    with pytest.raises(ValidationError, match="unknown category"):
        _config(SKILLHUB_CATEGORY_MAPPING_JSON='{"ai-agent":"not-a-platform-category"}')


def test_skillhub_proxy_settings_are_paired_and_loopback_only() -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        _config(
            SKILLHUB_PROXY_URL="socks5://127.0.0.1:7891",
            SKILLHUB_PROXY_CONNECT_IP="",
        )
    with pytest.raises(ValidationError, match="loopback"):
        _config(
            SKILLHUB_PROXY_URL="socks5://192.0.2.15:7891",
            SKILLHUB_PROXY_CONNECT_IP="192.0.2.202",
        )
    with pytest.raises(ValidationError, match="must not contain credentials"):
        _config(
            SKILLHUB_PROXY_URL="socks5://user:password@127.0.0.1:7891",
            SKILLHUB_PROXY_CONNECT_IP="192.0.2.202",
        )
    configured = _config(
        SKILLHUB_PROXY_URL="socks5://127.0.0.1:7891",
        SKILLHUB_PROXY_CONNECT_IP="192.0.2.202",
    )
    assert configured.SKILLHUB_PROXY_URL == "socks5://127.0.0.1:7891"
    assert configured.SKILLHUB_PROXY_CONNECT_IP == "192.0.2.202"


@pytest.mark.asyncio
async def test_provider_filters_cold_or_non_public_assets() -> None:
    market = SkillHubMarketService(
        session=None,
        operation_queue=None,
        config=_config(SKILLHUB_MIN_DOWNLOADS=1000, SKILLHUB_MIN_INSTALLS=100),
    )  # type: ignore[arg-type]
    from app.services.skillhub.client import SkillHubSkill

    acceptable = SkillHubSkill(
        "community", "search", "1.0.0", "Search", None, "ai-agent", 1000, 100, "public", "free"
    )
    assert market.eligible(acceptable) is not None
    assert (
        market.eligible(
            SkillHubSkill(
                "community", "cold", "1", "Cold", None, "ai-agent", 999, 100, "public", "free"
            )
        )
        is None
    )
    assert (
        market.eligible(
            SkillHubSkill(
                "community", "paid", "1", "Paid", None, "ai-agent", 1000, 100, "public", "paid"
            )
        )
        is None
    )
    assert (
        market.eligible(
            SkillHubSkill(
                "community", "unknown", "1", "Unknown", None, "unknown", 1000, 100, "public", "free"
            )
        ).category_key
        == "other"
    )  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_provider_applies_the_approved_500_download_50_install_thresholds() -> None:
    market = SkillHubMarketService(
        session=None,
        operation_queue=None,
        config=_config(SKILLHUB_MIN_DOWNLOADS=500, SKILLHUB_MIN_INSTALLS=50),
    )  # type: ignore[arg-type]
    base = dict(
        namespace="community",
        version="1",
        name="Eligible",
        description=None,
        category="ai-agent",
        visibility="public",
        pricing="free",
    )
    assert (
        market.eligible(SkillHubSkill(public_slug="eligible", downloads=500, installs=50, **base))
        is not None
    )
    assert (
        market.eligible(
            SkillHubSkill(public_slug="downloads-low", downloads=499, installs=50, **base)
        )
        is None
    )
    assert (
        market.eligible(
            SkillHubSkill(public_slug="installs-low", downloads=500, installs=49, **base)
        )
        is None
    )
    assert (
        market.eligible(SkillHubSkill(public_slug="unknown", downloads=None, installs=50, **base))
        is None
    )


@pytest.mark.asyncio
async def test_market_fetches_at_most_two_cursor_pages_to_fill_filtered_results() -> None:
    config = _config(SKILLHUB_MIN_DOWNLOADS=500, SKILLHUB_MIN_INSTALLS=50)

    def eligible(slug: str) -> SkillHubSkill:
        return SkillHubSkill(
            "community", slug, "1", slug, None, "ai-agent", 500, 50, "public", "free"
        )

    cold = SkillHubSkill(
        "community", "cold", "1", "cold", None, "ai-agent", 10, 1, "public", "free"
    )

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, int]] = []

        async def list_skills(self, *, cursor, limit, **_kwargs):
            self.calls.append((cursor, limit))
            if cursor is None:
                return SkillHubPage(items=(eligible("first"), cold), next_cursor="cursor-2")
            if cursor == "cursor-2":
                return SkillHubPage(
                    items=(eligible("second"), eligible("third")), next_cursor="cursor-3"
                )
            raise AssertionError("a third provider page must not be fetched")

    client = Client()
    market = SkillHubMarketService(session=None, operation_queue=None, client=client, config=config)  # type: ignore[arg-type]
    items, next_cursor = await market.list(
        org_id=1,
        user_id=2,
        workspace_id=3,
        runtime_agent_profile_id=4,
        feed="featured",
        category_key=None,
        query=None,
        cursor=None,
        limit=12,
    )

    assert [item.skill.public_slug for item in items] == ["first", "second", "third"]
    assert next_cursor == "cursor-3"
    assert client.calls == [(None, 24), ("cursor-2", 12)]


@pytest.mark.asyncio
async def test_market_uses_a_single_two_page_window_when_provider_has_no_cursor() -> None:
    config = _config(SKILLHUB_MIN_DOWNLOADS=500, SKILLHUB_MIN_INSTALLS=50)

    def eligible(index: int) -> SkillHubSkill:
        return SkillHubSkill(
            "community",
            f"skill-{index}",
            "1",
            f"skill-{index}",
            None,
            "ai-agent",
            500,
            50,
            "public",
            "free",
        )

    class Client:
        calls: list[int] = []

        async def list_skills(self, *, limit, **_kwargs):
            self.calls.append(limit)
            return SkillHubPage(
                items=tuple(eligible(index) for index in range(20)), next_cursor=None
            )

    client = Client()
    market = SkillHubMarketService(session=None, operation_queue=None, client=client, config=config)  # type: ignore[arg-type]
    items, next_cursor = await market.list(
        org_id=1,
        user_id=2,
        workspace_id=3,
        runtime_agent_profile_id=4,
        feed="featured",
        category_key=None,
        query=None,
        cursor=None,
        limit=12,
    )

    assert len(items) == 12
    assert next_cursor is None
    assert client.calls == [24]


@pytest.mark.asyncio
async def test_client_list_normalizes_provider_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/skills"
        assert dict(request.url.params) == {
            "page": "1",
            "pageSize": "12",
            "sortBy": "downloads",
            "order": "desc",
            "keyword": "搜索",
        }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "skills": [
                        {
                            "namespace": {"handle": "community"},
                            "slug": "search",
                            "version": "1.0.0",
                            "name": "Search",
                            "downloads": 1200,
                            "installs": 300,
                            "source": "community",
                        }
                    ]
                },
            },
        )

    client = SkillHubClient(config=_config(), transport=httpx.MockTransport(handler))
    page = await client.list_skills(feed="hot", query="搜索", cursor=None, limit=12)
    assert page.items[0].namespace == "community"
    assert page.items[0].public_slug == "search"
    assert page.items[0].visibility == "community"
    assert page.items[0].pricing == "free"


@pytest.mark.asyncio
async def test_client_proxy_connects_by_ip_but_preserves_host_and_tls_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "192.0.2.202"
        assert request.headers["host"] == "api.skillhub.cn"
        assert request.extensions["sni_hostname"] == "api.skillhub.cn"
        return httpx.Response(
            200,
            json={
                "data": {
                    "skills": [
                        {
                            "namespace": {"handle": "community"},
                            "slug": "search",
                            "version": "1.0.0",
                            "name": "Search",
                            "downloads": 1200,
                            "installs": 300,
                            "source": "community",
                        }
                    ]
                }
            },
        )

    client = SkillHubClient(
        config=_config(
            SKILLHUB_PROXY_URL="socks5://127.0.0.1:7891",
            SKILLHUB_PROXY_CONNECT_IP="192.0.2.202",
        ),
        transport=httpx.MockTransport(handler),
    )
    page = await client.list_skills(feed="hot", query=None, cursor=None, limit=1)
    assert page.items[0].public_slug == "search"


@pytest.mark.asyncio
async def test_client_search_preserves_a_provider_empty_result() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("keyword") == "啊水水"
        return httpx.Response(200, json={"code": 0, "data": {"skills": [], "total": 0}})

    page = await SkillHubClient(
        config=_config(), transport=httpx.MockTransport(handler)
    ).list_skills(
        feed="featured",
        query="啊水水",
        cursor=None,
        limit=12,
    )
    assert page.items == ()
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_client_verifies_skillhub_content_hash_and_ed25519_signature() -> None:
    content = _zip()
    content_hash = _content_hash(content, _config())
    payload = json.dumps({"content_hash": content_hash}, separators=(",", ":"))
    private_key = Ed25519PrivateKey.generate()
    signature = base64.b64encode(private_key.sign(payload.encode())).decode()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/download":
            return httpx.Response(200, content=content)
        if request.url.path.endswith("/signature"):
            return httpx.Response(
                200,
                json={
                    "content_hash": content_hash,
                    "key_id": "test",
                    "payload": payload,
                    "signature": signature,
                },
            )
        if request.url.path == "/api/v1/open/platform/keys":
            return httpx.Response(
                200, json={"keys": [{"key_id": "test", "public_key_raw_b64": public_key}]}
            )
        raise AssertionError(request.url.path)

    package = await SkillHubClient(
        config=_config(), transport=httpx.MockTransport(handler)
    ).download_and_verify(
        skill=SkillHubSkill(
            "community", "search", "1", "Search", None, None, 1000, 100, "public", "free"
        )
    )
    assert package.package_sha256 == hashlib.sha256(content).hexdigest()
    assert package.verified_content_sha256 == content_hash


def test_private_storage_is_profile_local_and_rejects_symlinks(tmp_path) -> None:
    storage = UserSkillHubPackageStorage(config=_config())
    first = storage.store(
        asset_root=tmp_path / "user-a",
        namespace="community",
        public_slug="search",
        version="1",
        content=_zip(),
    )
    second = storage.store(
        asset_root=tmp_path / "user-b",
        namespace="community",
        public_slug="search",
        version="1",
        content=_zip(),
    )
    assert first.package_uri != second.package_uri
    assert "user-a" in first.package_uri and "user-b" in second.package_uri

    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        link = zipfile.ZipInfo("link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "target")
        archive.writestr("SKILL.md", "---\nname: x\n---\n")
    with pytest.raises(Exception, match="symbolic"):
        storage.store(
            asset_root=tmp_path / "bad",
            namespace="community",
            public_slug="bad",
            version="1",
            content=bad.getvalue(),
        )


@pytest.mark.asyncio
async def test_skillhub_install_creates_stable_skill_and_default_desired(
    tmp_path, monkeypatch
) -> None:
    engine, sessions = await _database()
    content = _zip()
    remote = SimpleNamespace(
        category_key="agent-automation",
        skill=SkillHubSkill(
            "community",
            "search",
            "1.0.0",
            "Community Search",
            "test",
            "ai-agent",
            1000,
            100,
            "public",
            "free",
        ),
    )

    async def resolve(self, **_kwargs):
        return remote

    async def download(self, **_kwargs):
        return SimpleNamespace(
            content=content,
            package_sha256=hashlib.sha256(content).hexdigest(),
            signature_key_id=None,
            verified_content_sha256=hashlib.sha256(content).hexdigest(),
        )

    monkeypatch.setattr(
        "app.services.skillhub.installations.settings.HERMES_PUBLIC_ASSET_ROOT",
        str(tmp_path),
    )
    monkeypatch.setattr(SkillHubMarketService, "resolve_eligible", resolve)
    monkeypatch.setattr(SkillHubClient, "download_and_verify", download)
    try:
        async with sessions() as db:
            db.add(_profile())
            await db.commit()

        service = SkillHubInstallationOperationService(
            session_factory=sessions,
            queue=UserSkillOperationQueue(),
        )
        await service._install(
            snapshot=OperationSnapshot(
                operation_id="skillhub-install-1",
                workspace_id=3,
                action="install",
                target_key="skillhub:community:search",
                status="running",
                submitted_at=datetime.utcnow(),
            ),
            org_id=1,
            user_id=2,
            namespace="community",
            public_slug="search",
            expected_version="1.0.0",
            runtime_agent_profile_id=10,
        )

        async with sessions() as db:
            skill = await db.scalar(select(Skill).where(Skill.owner_user_id == 2))
            profile = await db.get(RuntimeAgentProfile, 10)
            installation = await db.scalar(select(UserSkillHubInstallation))
            assert skill is not None and profile is not None and installation is not None
            assert installation.skill_id == skill.id
            assert profile.desired_revision == 1
            assert (
                await db.scalar(
                    select(func.count(ProfileSkillBinding.id)).where(
                        ProfileSkillBinding.skill_id == skill.id
                    )
                )
                == 1
            )
    finally:
        await engine.dispose()
