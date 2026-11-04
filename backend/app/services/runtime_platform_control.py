from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
from typing import Any, Literal, Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuntimePlatformControlKey
from app.services.managed_secret import ManagedSecretCodec, ManagedSecretError


RuntimeBindingType = Literal["personal_runtime", "application_runtime"]
PLATFORM_CONTROL_KEY_FILE_PATH = "/run/secrets/hermes-platform-control/control-keys.json"
PLATFORM_CONTROL_KEY_FILE_NAME = "control-keys.json"
PLATFORM_CONTROL_KEY_VOLUME_NAME = "platform-control-key"
PLATFORM_CONTROL_SECRET_SUFFIX = "-platform-control"

_PREFIXED_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class RuntimePlatformControlKeyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimePlatformControlKeyMaterial:
    binding_type: RuntimeBindingType
    binding_id: int
    runtime_id: str
    key_version: int
    root_key: bytes

    def __post_init__(self) -> None:
        _validate_binding_identity(
            binding_type=self.binding_type,
            binding_id=self.binding_id,
            runtime_id=self.runtime_id,
        )
        if isinstance(self.key_version, bool) or self.key_version <= 0:
            raise ValueError("platform control key version is invalid")
        if not isinstance(self.root_key, bytes) or len(self.root_key) < 32:
            raise ValueError("platform control root key is too short")


@dataclass(frozen=True, slots=True)
class RuntimePlatformControlKeyProvision:
    material: RuntimePlatformControlKeyMaterial
    created: bool


class RuntimePlatformControlKeyStore(Protocol):
    async def ensure_active_key(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
    ) -> RuntimePlatformControlKeyProvision: ...

    async def load_active_key(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
    ) -> RuntimePlatformControlKeyMaterial: ...


class PlatformControlRootKeyCodec:
    """Encrypt raw Supervisor control roots with the managed-secret KEK."""

    prefix = "platform-control-root-v1:"

    def __init__(self, managed_codec: ManagedSecretCodec | None = None) -> None:
        self._managed_codec = managed_codec or ManagedSecretCodec()

    def encrypt(self, root_key: bytes) -> str:
        if not isinstance(root_key, bytes) or len(root_key) < 32:
            raise RuntimePlatformControlKeyError("platform control root key is too short")
        encoded = _base64url_encode(root_key)
        return self.prefix + self._managed_codec.encrypt(encoded)

    def decrypt(self, encrypted: str) -> bytes:
        if not isinstance(encrypted, str) or not encrypted.startswith(self.prefix):
            raise RuntimePlatformControlKeyError("platform control root key format is unsupported")
        try:
            encoded = self._managed_codec.decrypt(encrypted[len(self.prefix) :])
            root_key = _base64url_decode(encoded)
        except (ManagedSecretError, ValueError) as exc:
            raise RuntimePlatformControlKeyError(
                "platform control root key cannot be decrypted"
            ) from exc
        if len(root_key) < 32:
            raise RuntimePlatformControlKeyError("platform control root key is too short")
        return root_key


class SqlAlchemyRuntimePlatformControlKeyStore:
    def __init__(
        self,
        session: AsyncSession,
        *,
        codec: PlatformControlRootKeyCodec | None = None,
    ) -> None:
        self.session = session
        self.codec = codec or PlatformControlRootKeyCodec()

    async def ensure_active_key(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
    ) -> RuntimePlatformControlKeyProvision:
        _validate_binding_identity(
            binding_type=binding_type,
            binding_id=binding_id,
            runtime_id=runtime_id,
        )
        rows = await self._locked_rows(
            binding_type=binding_type,
            binding_id=binding_id,
        )
        active = self._one_active(rows)
        if active is not None:
            self._require_runtime(active, runtime_id)
            return RuntimePlatformControlKeyProvision(
                material=self._material(active),
                created=False,
            )
        if rows and any(str(row.runtime_id) != runtime_id for row in rows):
            raise RuntimePlatformControlKeyError(
                "platform control key history belongs to another Runtime"
            )
        version = max((int(row.key_version) for row in rows), default=0) + 1
        root_key = secrets.token_bytes(32)
        row = RuntimePlatformControlKey(
            binding_type=binding_type,
            binding_id=binding_id,
            runtime_id=runtime_id,
            key_version=version,
            root_key_encrypted=self.codec.encrypt(root_key),
            status="active",
        )
        self.session.add(row)
        await self.session.flush()
        return RuntimePlatformControlKeyProvision(
            material=RuntimePlatformControlKeyMaterial(
                binding_type=binding_type,
                binding_id=binding_id,
                runtime_id=runtime_id,
                key_version=version,
                root_key=root_key,
            ),
            created=True,
        )

    async def load_active_key(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
    ) -> RuntimePlatformControlKeyMaterial:
        _validate_binding_identity(
            binding_type=binding_type,
            binding_id=binding_id,
            runtime_id=runtime_id,
        )
        rows = list(
            (
                await self.session.scalars(
                    select(RuntimePlatformControlKey).where(
                        RuntimePlatformControlKey.binding_type == binding_type,
                        RuntimePlatformControlKey.binding_id == binding_id,
                        RuntimePlatformControlKey.status == "active",
                    )
                )
            ).all()
        )
        active = self._one_active(rows)
        if active is None:
            raise RuntimePlatformControlKeyError(
                "binding-scoped platform control key is unavailable"
            )
        self._require_runtime(active, runtime_id)
        return self._material(active)

    async def rotate_active_key(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
        runtime_id: str,
        expected_key_version: int,
        rotated_at: datetime,
    ) -> RuntimePlatformControlKeyMaterial:
        if rotated_at.tzinfo is not None:
            rotated_at = rotated_at.astimezone(timezone.utc).replace(tzinfo=None)
        rows = await self._locked_rows(
            binding_type=binding_type,
            binding_id=binding_id,
        )
        active = self._one_active(rows)
        if active is None:
            raise RuntimePlatformControlKeyError(
                "platform control key rotation has no active predecessor"
            )
        self._require_runtime(active, runtime_id)
        if int(active.key_version) != expected_key_version:
            raise RuntimePlatformControlKeyError("platform control key rotation CAS failed")
        next_version = max(int(row.key_version) for row in rows) + 1
        root_key = secrets.token_bytes(32)
        active.status = "retired"
        active.retired_at = rotated_at
        replacement = RuntimePlatformControlKey(
            binding_type=binding_type,
            binding_id=binding_id,
            runtime_id=runtime_id,
            key_version=next_version,
            root_key_encrypted=self.codec.encrypt(root_key),
            status="active",
        )
        self.session.add(replacement)
        await self.session.flush()
        return RuntimePlatformControlKeyMaterial(
            binding_type=binding_type,
            binding_id=binding_id,
            runtime_id=runtime_id,
            key_version=next_version,
            root_key=root_key,
        )

    async def _locked_rows(
        self,
        *,
        binding_type: RuntimeBindingType,
        binding_id: int,
    ) -> list[RuntimePlatformControlKey]:
        return list(
            (
                await self.session.scalars(
                    select(RuntimePlatformControlKey)
                    .where(
                        RuntimePlatformControlKey.binding_type == binding_type,
                        RuntimePlatformControlKey.binding_id == binding_id,
                    )
                    .order_by(RuntimePlatformControlKey.key_version)
                    .with_for_update()
                )
            ).all()
        )

    @staticmethod
    def _one_active(
        rows: list[RuntimePlatformControlKey],
    ) -> RuntimePlatformControlKey | None:
        active = [row for row in rows if str(row.status) == "active"]
        if len(active) > 1:
            raise RuntimePlatformControlKeyError(
                "binding has multiple active platform control keys"
            )
        return active[0] if active else None

    @staticmethod
    def _require_runtime(
        row: RuntimePlatformControlKey,
        runtime_id: str,
    ) -> None:
        if str(row.runtime_id) != runtime_id:
            raise RuntimePlatformControlKeyError(
                "platform control key Runtime identity does not match"
            )

    def _material(
        self,
        row: RuntimePlatformControlKey,
    ) -> RuntimePlatformControlKeyMaterial:
        return RuntimePlatformControlKeyMaterial(
            binding_type=cast(RuntimeBindingType, str(row.binding_type)),
            binding_id=int(row.binding_id),
            runtime_id=str(row.runtime_id),
            key_version=int(row.key_version),
            root_key=self.codec.decrypt(str(row.root_key_encrypted)),
        )


def runtime_pvc_storage_identity_hash(*, namespace: str, pvc_uid: str) -> str:
    namespace = namespace.strip()
    pvc_uid = pvc_uid.strip()
    if not namespace or not pvc_uid:
        raise ValueError("PVC namespace and immutable UID are required")
    canonical = json.dumps(
        {
            "namespace": namespace,
            "pvc_uid": pvc_uid,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def bind_persisted_pvc_identity(
    binding: Any,
    *,
    pvc_uid: str,
    pvc_storage_identity_hash: str,
) -> None:
    """Persist an immutable PVC identity once, then reject every mismatch."""

    pvc_uid = pvc_uid.strip()
    if not pvc_uid or _PREFIXED_SHA256_RE.fullmatch(pvc_storage_identity_hash) is None:
        raise RuntimePlatformControlKeyError("Runtime PVC identity evidence is invalid")
    stored_uid = getattr(binding, "runtime_root_pvc_uid", None)
    stored_hash = getattr(binding, "pvc_storage_identity_hash", None)
    if (stored_uid is None) != (stored_hash is None):
        raise RuntimePlatformControlKeyError("Runtime binding PVC identity is incomplete")
    if stored_uid is None:
        binding.runtime_root_pvc_uid = pvc_uid
        binding.pvc_storage_identity_hash = pvc_storage_identity_hash
        return
    if str(stored_uid) != pvc_uid or str(stored_hash) != pvc_storage_identity_hash:
        raise RuntimePlatformControlKeyError("Runtime binding PVC identity changed")


def platform_control_secret_name(service_name: str) -> str:
    value = f"{service_name}{PLATFORM_CONTROL_SECRET_SUFFIX}"
    if (
        len(value) > 253
        or re.fullmatch(
            r"[a-z0-9]([-a-z0-9]*[a-z0-9])?",
            value,
        )
        is None
    ):
        raise ValueError("platform control Secret name is invalid")
    return value


def platform_control_key_document(
    *,
    material: RuntimePlatformControlKeyMaterial,
    pvc_storage_identity_hash: str,
    accepted_keys: Mapping[int, bytes] | None = None,
) -> dict[str, object]:
    if _PREFIXED_SHA256_RE.fullmatch(pvc_storage_identity_hash) is None:
        raise ValueError("PVC storage identity hash is invalid")
    keys = dict(accepted_keys or {material.key_version: material.root_key})
    if material.key_version not in keys:
        raise ValueError("current platform control key is not in the key ring")
    encoded: dict[str, str] = {}
    for version, root_key in sorted(keys.items()):
        if isinstance(version, bool) or version <= 0:
            raise ValueError("platform control key version is invalid")
        if not isinstance(root_key, bytes) or len(root_key) < 32:
            raise ValueError("platform control root key is too short")
        encoded[str(version)] = _base64url_encode(root_key)
    return {
        "contract_version": 1,
        "runtime_id": material.runtime_id,
        "binding_type": material.binding_type,
        "binding_id": material.binding_id,
        "pvc_storage_identity_hash": pvc_storage_identity_hash,
        "current_key_version": material.key_version,
        "keys": encoded,
    }


def _validate_binding_identity(
    *,
    binding_type: str,
    binding_id: int,
    runtime_id: str,
) -> None:
    if binding_type not in {"personal_runtime", "application_runtime"}:
        raise ValueError("platform control binding type is invalid")
    if isinstance(binding_id, bool) or binding_id <= 0:
        raise ValueError("platform control binding id is invalid")
    if not runtime_id or len(runtime_id) > 128:
        raise ValueError("platform control runtime id is invalid")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if _BASE64URL_RE.fullmatch(value) is None:
        raise ValueError("value must be unpadded base64url")
    decoded = base64.b64decode(
        value + ("=" * (-len(value) % 4)),
        altchars=b"-_",
        validate=True,
    )
    if _base64url_encode(decoded) != value:
        raise ValueError("value must be canonical unpadded base64url")
    return decoded
