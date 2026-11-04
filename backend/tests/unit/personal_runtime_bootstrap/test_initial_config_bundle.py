from types import SimpleNamespace

import pytest
import yaml

import app.services.personal_config_bundle as bundle_module
from app.services.personal_config_bundle import (
    PersonalConfigBundleBuilder,
)


class _Session:
    def __init__(self, values: list[object | None]) -> None:
        self.values = list(values)

    async def scalar(self, _statement):
        return self.values.pop(0)


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        id=41,
        org_id=7,
        user_id=9,
        name="测试工作区",
        default_model_id=13,
        profile_config={},
    )


def _binding() -> SimpleNamespace:
    return SimpleNamespace(
        runtime_id="rt-bootstrap-41",
        config_generation=6,
        config_hash=None,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(name="测试用户", user_code="employee-9")


def _install_stores(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bootstrap: bool,
) -> None:
    class _ProfileStore:
        def __init__(self, _db) -> None:
            pass

        async def ensure_default(self, *, workspace):
            return SimpleNamespace(id=101)

    class _ModelStore:
        def __init__(self, _db) -> None:
            pass

        async def list_visible_models(self, *, org_id: int, user_id: int):
            assert not bootstrap
            assert (org_id, user_id) == (7, 9)
            return [
                SimpleNamespace(
                    model_entry_id=13,
                    context_length=128_000,
                    tags=["chat"],
                    is_default=True,
                )
            ]

    monkeypatch.setattr(
        bundle_module,
        "SqlAlchemyRuntimeAgentProfileStore",
        _ProfileStore,
    )
    monkeypatch.setattr(
        bundle_module,
        "SqlAlchemyRuntimeModelStore",
        _ModelStore,
    )


async def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: SimpleNamespace,
    bootstrap: bool,
):
    _install_stores(monkeypatch, bootstrap=bootstrap)
    return await PersonalConfigBundleBuilder(
        _Session([None, profile, _binding(), _user()])  # type: ignore[arg-type]  # None = 无 exact-generation 工件
    ).build(
        profile_id=41,
        generation=6,
        require_current=True,
        config_schema=34 if bootstrap else 33,
        bootstrap=bootstrap,
    )


@pytest.mark.asyncio
async def test_bootstrap_bundle_digest_ignores_model_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_profile = _profile()
    old_profile.default_model_id = 13
    new_profile = _profile()
    new_profile.default_model_id = 99

    old_bundle = await _build(
        monkeypatch,
        profile=old_profile,
        bootstrap=True,
    )
    new_bundle = await _build(
        monkeypatch,
        profile=new_profile,
        bootstrap=True,
    )

    assert old_bundle.config_revision == "bootstrap-generation-6"
    assert old_bundle.config_digest == new_bundle.config_digest
    assert old_bundle.files == new_bundle.files

    snapshot = old_bundle.files["platform/config.snapshot.json"]
    assert '"config_contract": "bootstrap"' in snapshot
    assert "default_model_id" not in snapshot
    assert "visible_model_ids" not in snapshot
    assert "model_catalog_version" not in snapshot

    config = yaml.safe_load(old_bundle.files["config.yaml"])
    assert config["model"]["default"] == "session"
    assert config["providers"]["platform"]["models"] == {}
    assert config["providers"]["platform"]["discover_models"] is False

    projection = old_bundle.files["platform/projection.json"]
    assert '"model"' not in projection.split('"scopes"')[0]


@pytest.mark.asyncio
async def test_legacy_bundle_keeps_catalog_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = await _build(monkeypatch, profile=_profile(), bootstrap=False)

    assert bundle.config_revision == "generation-6"
    snapshot = bundle.files["platform/config.snapshot.json"]
    assert '"default_model_id": 13' in snapshot
    assert '"visible_model_ids": [\n    13\n  ]' in snapshot
    config = yaml.safe_load(bundle.files["config.yaml"])
    assert config["model"]["default_model_entry_id"] == 13
    assert config["providers"]["platform"]["models"]["13"] == {
        "context_length": 128_000,
        "supports_vision": False,
    }
