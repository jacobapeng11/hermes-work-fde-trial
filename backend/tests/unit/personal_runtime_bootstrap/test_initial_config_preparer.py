from types import SimpleNamespace

import pytest

import app.services.personal_config_bundle as bundle_module
from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.hermes_runtime.providers.knative import KnativeRuntimeProvider
from app.services.personal_config_bundle import (
    PersonalRuntimeInitialConfigPreparer,
)


class _Session:
    def __init__(self, values: list[object | None]) -> None:
        self.values = list(values)
        self.commits = 0
        self.added: list[object] = []

    async def scalar(self, _statement):
        return self.values.pop(0)

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _spec(revision: str) -> KnativeRuntimeSpec:
    return KnativeRuntimeSpec(
        org_id=7,
        user_id=9,
        user_name="测试用户",
        user_code="employee-9",
        runtime_workspace_id=41,
        runtime_id="rt-bootstrap-41",
        namespace="hermes-runtimes",
        service_name="runtime-employee-9",
        image="kind.local/hermes-agent:v0.20.1",
        service_account="hermes-runtime-workload",
        api_server_key_secret_name="runtime-employee-9-api-key",
        runtime_root_pvc_name="runtime-employee-9-root",
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
        runtime_root_pv_name="runtime-employee-9-root-pv",
        runtime_root_host_path="/data/hermes/runtimes/7/employee-9",
        runtime_node_name="demo",
        upstream_commit="f80f453a",
        state_schema=25,
        config_schema=34,
        startup_mode="steady_state",
        runtime_binding_id="191",
        runtime_config_revision=revision,
        platform_control_secret_name="runtime-employee-9-platform-control",
        platform_control_key_version=3,
        runtime_root_pvc_uid="pvc-uid-191",
        pvc_storage_identity_hash="sha256:" + "a" * 64,
    )


@pytest.mark.asyncio
async def test_preparer_switches_to_next_bootstrap_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(
        id=41,
        org_id=7,
        user_id=9,
        name="测试工作区",
        default_model_id=13,
        profile_config={},
    )
    binding = SimpleNamespace(
        runtime_id="rt-bootstrap-41",
        org_id=7,
        config_generation=2,
        config_hash="generation-2",
        pending_reload_scopes=None,
    )
    user = SimpleNamespace(name="测试用户", user_code="employee-9")
    # scalar 序列：prepare 锁 binding → build 查工件(None) → profile/binding/user → record 工件查重(None)
    session = _Session([binding, None, profile, binding, user, None])

    class _ProfileStore:
        def __init__(self, _db) -> None:
            pass

        async def ensure_default(self, *, workspace):
            return SimpleNamespace(id=101)

    class _ModelStore:
        def __init__(self, _db) -> None:
            pass

        async def list_visible_models(self, **_kwargs):
            raise AssertionError("bootstrap bundle must not read model catalog")

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

    prepared = await PersonalRuntimeInitialConfigPreparer(
        session  # type: ignore[arg-type]
    ).prepare(_spec("generation-2"))

    assert binding.config_generation == 3
    assert binding.config_hash == "bootstrap-generation-3"
    assert prepared.runtime_config_revision == "bootstrap-generation-3"
    assert prepared.initial_config_download_url == (
        "/internal/v1/config-bundles/personal/41/3"
    )
    assert len(prepared.initial_config_digest) == 64
    assert session.commits == 2  # generation 推进 + 工件落库
    assert len(session.added) == 1  # exact-generation 工件
    manifest = KnativeRuntimeProvider().build_kservice_manifest(prepared)
    env = {
        item["name"]: item.get("value")
        for item in manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["RUNTIME_CONFIG_REVISION"] == "bootstrap-generation-3"
    assert env["RUNTIME_INITIAL_CONFIG_DIGEST"] == prepared.initial_config_digest
