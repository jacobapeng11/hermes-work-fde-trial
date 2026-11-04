from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from app.models import RuntimePlatformControlKey
from app.services.managed_secret import ManagedSecretCodec
from app.services.runtime_platform_control import (
    PlatformControlRootKeyCodec,
    RuntimePlatformControlKeyError,
    RuntimePlatformControlKeyMaterial,
    SqlAlchemyRuntimePlatformControlKeyStore,
    bind_persisted_pvc_identity,
    platform_control_key_document,
    runtime_pvc_storage_identity_hash,
)


class _ScalarRows:
    def __init__(self, rows: list[RuntimePlatformControlKey]) -> None:
        self.rows = rows

    def all(self) -> list[RuntimePlatformControlKey]:
        return list(self.rows)


class _KeySession:
    def __init__(self) -> None:
        self.rows: list[RuntimePlatformControlKey] = []
        self.flushes = 0

    async def scalars(self, _statement):
        return _ScalarRows(self.rows)

    def add(self, row: RuntimePlatformControlKey) -> None:
        row.id = len(self.rows) + 1
        self.rows.append(row)

    async def flush(self) -> None:
        self.flushes += 1


@pytest.mark.asyncio
async def test_key_store_persists_one_active_version_and_rotates_with_cas() -> None:
    session = _KeySession()
    store = SqlAlchemyRuntimePlatformControlKeyStore(
        session,  # type: ignore[arg-type]
        codec=PlatformControlRootKeyCodec(ManagedSecretCodec("test-kek")),
    )

    provision = await store.ensure_active_key(
        binding_type="personal_runtime",
        binding_id=19,
        runtime_id="rt-19",
    )
    reused = await store.ensure_active_key(
        binding_type="personal_runtime",
        binding_id=19,
        runtime_id="rt-19",
    )
    rotated = await store.rotate_active_key(
        binding_type="personal_runtime",
        binding_id=19,
        runtime_id="rt-19",
        expected_key_version=1,
        rotated_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    assert provision.created is True
    assert reused.created is False
    assert reused.material == provision.material
    assert rotated.key_version == 2
    assert rotated.root_key != provision.material.root_key
    assert [(row.key_version, row.status) for row in session.rows] == [
        (1, "retired"),
        (2, "active"),
    ]
    assert session.rows[0].retired_at == datetime(2026, 8, 15)
    with pytest.raises(RuntimePlatformControlKeyError, match="CAS failed"):
        await store.rotate_active_key(
            binding_type="personal_runtime",
            binding_id=19,
            runtime_id="rt-19",
            expected_key_version=1,
            rotated_at=datetime(2026, 8, 15),
        )


def test_root_key_codec_is_encrypted_and_kek_bound() -> None:
    codec = PlatformControlRootKeyCodec(ManagedSecretCodec("primary-kek"))
    encrypted = codec.encrypt(b"r" * 32)

    assert encrypted.startswith("platform-control-root-v1:aesgcm-v1:")
    assert "rrrr" not in encrypted
    assert codec.decrypt(encrypted) == b"r" * 32
    with pytest.raises(RuntimePlatformControlKeyError):
        PlatformControlRootKeyCodec(ManagedSecretCodec("different-kek")).decrypt(encrypted)


def test_key_document_and_pvc_identity_are_closed_and_canonical() -> None:
    material = RuntimePlatformControlKeyMaterial(
        binding_type="application_runtime",
        binding_id=7,
        runtime_id="application-agent-runtime-7",
        key_version=4,
        root_key=b"z" * 32,
    )
    identity = runtime_pvc_storage_identity_hash(
        namespace="hermes-runtimes",
        pvc_uid="immutable-pvc-uid",
    )

    document = platform_control_key_document(
        material=material,
        pvc_storage_identity_hash=identity,
    )

    assert tuple(document) == (
        "contract_version",
        "runtime_id",
        "binding_type",
        "binding_id",
        "pvc_storage_identity_hash",
        "current_key_version",
        "keys",
    )
    assert json.loads(json.dumps(document)) == document
    assert document["keys"] == {"4": "enp6enp6enp6enp6enp6enp6enp6enp6enp6enp6eno"}


def test_pvc_identity_is_write_once_and_never_path_derived() -> None:
    binding = SimpleNamespace(
        runtime_root_pvc_uid=None,
        pvc_storage_identity_hash=None,
        runtime_root_host_path="/same/path",
    )
    identity = runtime_pvc_storage_identity_hash(
        namespace="hermes-runtimes",
        pvc_uid="pvc-uid-a",
    )

    bind_persisted_pvc_identity(
        binding,
        pvc_uid="pvc-uid-a",
        pvc_storage_identity_hash=identity,
    )
    bind_persisted_pvc_identity(
        binding,
        pvc_uid="pvc-uid-a",
        pvc_storage_identity_hash=identity,
    )

    with pytest.raises(RuntimePlatformControlKeyError, match="changed"):
        bind_persisted_pvc_identity(
            binding,
            pvc_uid="pvc-uid-b",
            pvc_storage_identity_hash=runtime_pvc_storage_identity_hash(
                namespace="hermes-runtimes",
                pvc_uid="pvc-uid-b",
            ),
        )


def test_control_key_model_has_binding_version_uniqueness() -> None:
    constraints = {
        constraint.name for constraint in RuntimePlatformControlKey.__table__.constraints
    }

    assert "uq_runtime_platform_control_key_version" in constraints
    assert RuntimePlatformControlKey.__table__.c.root_key_encrypted.nullable is False
