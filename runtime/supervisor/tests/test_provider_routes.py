from __future__ import annotations

import base64
from datetime import datetime
import json
from pathlib import Path

import pytest

from platform_supervisor.provider_routes import (
    ProviderRouteBindingIdentity,
    ProviderRouteCapsuleCodec,
    ProviderRouteCapsuleError,
)


FIXTURE = (
    Path(__file__).parents[3]
    / "packages"
    / "schemas"
    / "fixtures"
    / "runtime-execution-plane-v1"
    / "provider-route-capsule.json"
)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def decode_root(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_supervisor_opens_backend_provider_route_golden_vector() -> None:
    fixture = load_fixture()
    binding = ProviderRouteBindingIdentity(**fixture["binding"])

    material = ProviderRouteCapsuleCodec().open(
        grant=fixture["grant"],
        control_root_keys={3: decode_root(fixture["root_key_base64url"])},
        binding=binding,
        expected_org_id=fixture["expected"]["org_id"],
        expected_model_entry_id=fixture["expected"]["model_entry_id"],
        now=datetime.fromisoformat(fixture["now"].replace("Z", "+00:00")),
    )

    assert material.org_id == fixture["expected"]["org_id"]
    assert material.model_entry_id == fixture["expected"]["model_entry_id"]
    assert material.provider_id == fixture["expected"]["provider_id"]
    assert material.upstream_model_id == fixture["expected"]["upstream_model_id"]
    assert material.provider_base_url == fixture["expected"]["provider_base_url"]
    assert material.provider_api_key.get_secret_value() == fixture["expected"][
        "provider_api_key"
    ]
    assert repr(material).find(fixture["expected"]["provider_api_key"]) == -1


def test_supervisor_rejects_wrong_model_and_unknown_key_version() -> None:
    fixture = load_fixture()
    binding = ProviderRouteBindingIdentity(**fixture["binding"])
    codec = ProviderRouteCapsuleCodec()
    now = datetime.fromisoformat(fixture["now"].replace("Z", "+00:00"))

    with pytest.raises(ProviderRouteCapsuleError):
        codec.open(
            grant=fixture["grant"],
            control_root_keys={3: decode_root(fixture["root_key_base64url"])},
            binding=binding,
            expected_org_id=1,
            expected_model_entry_id=19,
            now=now,
        )
    with pytest.raises(ProviderRouteCapsuleError):
        codec.open(
            grant=fixture["grant"],
            control_root_keys={},
            binding=binding,
            expected_org_id=1,
            expected_model_entry_id=21,
            now=now,
        )
