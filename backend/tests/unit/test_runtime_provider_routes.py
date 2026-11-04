from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.runtime_provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteCapsuleCodec,
    ProviderRouteCapsuleError,
    ProviderRouteSource,
    RuntimeProviderRouteResolver,
    normalize_provider_base_url,
)
from app.services.runtime_platform_control import RuntimePlatformControlKeyMaterial


ROOT_KEY = b"runtime-provider-route-root-key-32bytes!!"
PVC_IDENTITY = "sha256:" + "a" * 64


def binding(**overrides: object) -> ProviderRouteBindingIdentity:
    values: dict[str, object] = {
        "runtime_id": "runtime-10002",
        "binding_type": "personal_runtime",
        "binding_id": 193,
        "pvc_storage_identity_hash": PVC_IDENTITY,
    }
    values.update(overrides)
    return ProviderRouteBindingIdentity(**values)


def source(**overrides: object) -> ProviderRouteSource:
    values: dict[str, object] = {
        "org_id": 1,
        "model_entry_id": 21,
        "provider_id": 17,
        "upstream_model_id": "qwen3.7-plus",
        "provider_base_url": "https://coding.dashscope.aliyuncs.com/v1",
        "provider_api_key": "dashscope-secret-value",
        "model_catalog_version": 35,
        "provider_catalog_version": 35,
    }
    values.update(overrides)
    return ProviderRouteSource(**values)


def test_provider_route_capsule_is_random_but_digest_is_stable() -> None:
    codec = ProviderRouteCapsuleCodec()
    issued_at = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
    first = codec.issue(
        root_key=ROOT_KEY,
        control_key_version=3,
        binding=binding(),
        source=source(),
        issued_at=issued_at,
        nonce=b"\x01" * 12,
    )
    second = codec.issue(
        root_key=ROOT_KEY,
        control_key_version=3,
        binding=binding(),
        source=source(),
        issued_at=issued_at + timedelta(seconds=1),
        nonce=b"\x02" * 12,
    )

    assert first.digest == second.digest
    assert first.capsule != second.capsule
    assert "dashscope-secret-value" not in first.capsule
    assert "coding.dashscope.aliyuncs.com" not in first.capsule

    opened = codec.open(
        grant=first,
        control_root_keys={3: ROOT_KEY},
        binding=binding(),
        expected_org_id=1,
        expected_model_entry_id=21,
        now=issued_at + timedelta(minutes=1),
    )
    assert opened.provider_base_url == "https://coding.dashscope.aliyuncs.com/v1"
    assert opened.provider_api_key.get_secret_value() == "dashscope-secret-value"
    assert opened.upstream_model_id == "qwen3.7-plus"


def test_provider_route_capsule_fails_closed_on_tamper_and_binding_mismatch() -> None:
    codec = ProviderRouteCapsuleCodec()
    issued_at = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)
    grant = codec.issue(
        root_key=ROOT_KEY,
        control_key_version=3,
        binding=binding(),
        source=source(),
        issued_at=issued_at,
        nonce=b"\x03" * 12,
    )
    tampered = grant.model_copy(
        update={"capsule": grant.capsule[:-1] + ("A" if grant.capsule[-1] != "A" else "B")}
    )

    with pytest.raises(ProviderRouteCapsuleError):
        codec.open(
            grant=tampered,
            control_root_keys={3: ROOT_KEY},
            binding=binding(),
            expected_org_id=1,
            expected_model_entry_id=21,
            now=issued_at + timedelta(minutes=1),
        )
    with pytest.raises(ProviderRouteCapsuleError):
        codec.open(
            grant=grant,
            control_root_keys={3: ROOT_KEY},
            binding=binding(runtime_id="runtime-other"),
            expected_org_id=1,
            expected_model_entry_id=21,
            now=issued_at + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    "value",
    [
        "http://provider.example/v1",
        "https://user:pass@provider.example/v1",
        "https://provider.example/v1?api-version=1",
        "https://provider.example/v1#fragment",
        "https://provider.example/v1/../admin",
    ],
)
def test_provider_base_url_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_provider_base_url(value)


def test_provider_base_url_is_canonical_without_changing_base_path() -> None:
    assert normalize_provider_base_url(
        "https://coding.dashscope.aliyuncs.com/v1///"
    ) == "https://coding.dashscope.aliyuncs.com/v1"


class _ResolverResult:
    def first(self):
        return (
            SimpleNamespace(
                id=21,
                provider_id=17,
                model_key="qwen3.7-plus",
                context_length=256_000,
                runtime_capabilities={"thinking_mode": "enabled"},
                catalog_version=35,
            ),
            SimpleNamespace(
                id=17,
                api_endpoint="https://coding.dashscope.aliyuncs.com/v1/",
                api_key_encrypted="encrypted-provider-key",
                catalog_version=35,
            ),
        )


class _ResolverDb:
    async def execute(self, _statement):
        return _ResolverResult()


class _CredentialCodec:
    def decrypt(self, value: str) -> str:
        assert value == "encrypted-provider-key"
        return "dashscope-secret-value"


class _ControlKeyStore:
    async def load_active_key(self, **kwargs):
        assert kwargs == {
            "binding_type": "personal_runtime",
            "binding_id": 193,
            "runtime_id": "runtime-10002",
        }
        return RuntimePlatformControlKeyMaterial(
            binding_type="personal_runtime",
            binding_id=193,
            runtime_id="runtime-10002",
            key_version=3,
            root_key=ROOT_KEY,
        )


@pytest.mark.asyncio
async def test_resolver_issues_route_from_published_model_provider_pair() -> None:
    resolved = await RuntimeProviderRouteResolver(
        _ResolverDb(),
        credential_codec=_CredentialCodec(),
        control_key_store=_ControlKeyStore(),
    ).resolve(
        org_id=1,
        model_entry_id=21,
        binding=binding(),
        issued_at=datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc),
    )

    assert resolved.upstream_model_id == "qwen3.7-plus"
    assert resolved.context_length == 256_000
    assert resolved.runtime_capabilities == {
        "thinking_mode": "enabled",
        "reasoning_replay": "not_supported",
    }
    opened = ProviderRouteCapsuleCodec().open(
        grant=resolved.provider_route,
        control_root_keys={3: ROOT_KEY},
        binding=binding(),
        expected_org_id=1,
        expected_model_entry_id=21,
        now=datetime(2026, 8, 18, 8, 1, tzinfo=timezone.utc),
    )
    assert opened.provider_base_url == (
        "https://coding.dashscope.aliyuncs.com/v1"
    )
