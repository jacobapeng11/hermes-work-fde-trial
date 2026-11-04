from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
from typing import Literal

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from platform_supervisor.contracts import ProviderRouteGrantV1


PROVIDER_ROUTE_CONTRACT_VERSION = 1
PROVIDER_ROUTE_PURPOSE = "model-route-v1"
PROVIDER_ROUTE_HKDF_SALT = b"hermes-platform-control-v1"
PROVIDER_ROUTE_TTL = timedelta(hours=1)
PROVIDER_ROUTE_CLOCK_SKEW_SECONDS = 30
ProviderRouteBindingType = Literal["personal_runtime", "application_runtime"]

_PVC_IDENTITY_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class ProviderRouteCapsuleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderRouteBindingIdentity:
    runtime_id: str
    binding_type: ProviderRouteBindingType
    binding_id: int
    pvc_storage_identity_hash: str

    def __post_init__(self) -> None:
        if not self.runtime_id or len(self.runtime_id) > 128:
            raise ValueError("provider route runtime identity is invalid")
        if self.binding_id <= 0:
            raise ValueError("provider route binding identity is invalid")
        if _PVC_IDENTITY_RE.fullmatch(self.pvc_storage_identity_hash) is None:
            raise ValueError("provider route PVC identity is invalid")


@dataclass(frozen=True, slots=True)
class ProviderRouteSource:
    org_id: int
    model_entry_id: int
    provider_id: int
    upstream_model_id: str
    provider_base_url: str
    provider_api_key: str
    model_catalog_version: int
    provider_catalog_version: int

    def __post_init__(self) -> None:
        if min(
            self.org_id,
            self.model_entry_id,
            self.provider_id,
            self.model_catalog_version,
            self.provider_catalog_version,
        ) <= 0:
            raise ValueError("provider route source identity is invalid")
        if not self.upstream_model_id or len(self.upstream_model_id) > 200:
            raise ValueError("provider route upstream model is invalid")
        if (
            not self.provider_base_url.startswith("https://")
            or len(self.provider_base_url) > 500
        ):
            raise ValueError("provider route base URL is invalid")
        if not self.provider_api_key or len(self.provider_api_key) > 1000:
            raise ValueError("provider route credential is invalid")


@dataclass(frozen=True, slots=True)
class ProviderRouteMaterial:
    org_id: int
    model_entry_id: int
    provider_id: int
    upstream_model_id: str
    provider_base_url: str
    provider_api_key: SecretStr
    model_catalog_version: int
    provider_catalog_version: int
    provider_route_digest: str

    def __post_init__(self) -> None:
        if isinstance(self.provider_api_key, str):
            object.__setattr__(
                self,
                "provider_api_key",
                SecretStr(self.provider_api_key),
            )


class ProviderRouteCapsuleCodec:
    def open(
        self,
        *,
        grant: ProviderRouteGrantV1 | Mapping[str, object],
        control_root_keys: Mapping[int, bytes],
        binding: ProviderRouteBindingIdentity,
        expected_org_id: int,
        expected_model_entry_id: int | str,
        now: datetime | None = None,
    ) -> ProviderRouteMaterial:
        try:
            parsed_grant = (
                grant
                if isinstance(grant, ProviderRouteGrantV1)
                else ProviderRouteGrantV1.model_validate(grant)
            )
            prefix, raw_version, encoded_nonce, encoded_ciphertext = (
                parsed_grant.capsule.split(".")
            )
            if prefix != "prc1" or not raw_version.isdigit():
                raise ValueError("provider route capsule prefix is invalid")
            control_key_version = int(raw_version)
            root_key = control_root_keys[control_key_version]
            nonce = _base64url_decode(encoded_nonce)
            ciphertext = _base64url_decode(encoded_ciphertext)
            if len(nonce) != 12 or len(ciphertext) < 16:
                raise ValueError("provider route capsule size is invalid")
            master = _derive_master_key(
                root_key,
                binding=binding,
                control_key_version=control_key_version,
            )
            plaintext = AESGCM(_derive_subkey(master, b"aead")).decrypt(
                nonce,
                ciphertext,
                _aad(binding=binding, control_key_version=control_key_version),
            )
            document = json.loads(plaintext)
            _validate_plaintext_shape(document)
            if (
                document["version"] != PROVIDER_ROUTE_CONTRACT_VERSION
                or document["runtime_id"] != binding.runtime_id
                or document["binding_type"] != binding.binding_type
                or document["binding_id"] != binding.binding_id
                or document["pvc_storage_identity_hash"]
                != binding.pvc_storage_identity_hash
                or document["org_id"] != expected_org_id
                or document["model_entry_id"] != int(expected_model_entry_id)
            ):
                raise ValueError("provider route identity does not match")
            current = _aware_utc(now or datetime.now(timezone.utc))
            current_epoch = int(current.timestamp())
            if document["issued_at"] > (
                current_epoch + PROVIDER_ROUTE_CLOCK_SKEW_SECONDS
            ):
                raise ValueError("provider route capsule is not active")
            if document["expires_at"] < current_epoch:
                raise ValueError("provider route capsule has expired")
            if (
                document["expires_at"] - document["issued_at"]
                != int(PROVIDER_ROUTE_TTL.total_seconds())
            ):
                raise ValueError("provider route capsule lifetime is invalid")
            source = ProviderRouteSource(
                org_id=document["org_id"],
                model_entry_id=document["model_entry_id"],
                provider_id=document["provider_id"],
                upstream_model_id=document["upstream_model_id"],
                provider_base_url=document["provider_base_url"],
                provider_api_key=document["provider_api_key"],
                model_catalog_version=document["model_catalog_version"],
                provider_catalog_version=document["provider_catalog_version"],
            )
            expected_digest = _route_digest(
                binding=binding,
                source=source,
                credential_fingerprint=hmac.new(
                    _derive_subkey(master, b"fingerprint"),
                    source.provider_api_key.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest(),
            )
            if (
                not hmac.compare_digest(parsed_grant.digest, expected_digest)
                or not hmac.compare_digest(
                    document["provider_route_digest"], expected_digest
                )
            ):
                raise ValueError("provider route digest does not match")
        except ProviderRouteCapsuleError:
            raise
        except Exception as exc:
            raise ProviderRouteCapsuleError("provider route capsule is invalid") from exc
        return ProviderRouteMaterial(
            org_id=source.org_id,
            model_entry_id=source.model_entry_id,
            provider_id=source.provider_id,
            upstream_model_id=source.upstream_model_id,
            provider_base_url=source.provider_base_url,
            provider_api_key=SecretStr(source.provider_api_key),
            model_catalog_version=source.model_catalog_version,
            provider_catalog_version=source.provider_catalog_version,
            provider_route_digest=expected_digest,
        )


def binding_identity_from_settings(settings) -> ProviderRouteBindingIdentity:
    return ProviderRouteBindingIdentity(
        runtime_id=settings.RUNTIME_ID,
        binding_type=(
            "application_runtime"
            if settings.RUNTIME_MODE == "application"
            else "personal_runtime"
        ),
        binding_id=int(settings.RUNTIME_BINDING_ID),
        pvc_storage_identity_hash=str(settings.RUNTIME_PVC_STORAGE_IDENTITY_HASH),
    )


def _derive_master_key(
    root_key: bytes,
    *,
    binding: ProviderRouteBindingIdentity,
    control_key_version: int,
) -> bytes:
    if not isinstance(root_key, bytes) or len(root_key) < 32:
        raise ValueError("provider route root key is invalid")
    if control_key_version <= 0:
        raise ValueError("provider route key version is invalid")
    info = _canonical_json(
        {
            "contract_version": PROVIDER_ROUTE_CONTRACT_VERSION,
            "runtime_id": binding.runtime_id,
            "pvc_storage_identity_hash": binding.pvc_storage_identity_hash,
            "purpose": PROVIDER_ROUTE_PURPOSE,
            "key_version": control_key_version,
        }
    )
    pseudorandom_key = hmac.new(
        PROVIDER_ROUTE_HKDF_SALT,
        root_key,
        hashlib.sha256,
    ).digest()
    return hmac.new(
        pseudorandom_key,
        info + b"\x01",
        hashlib.sha256,
    ).digest()


def _derive_subkey(master: bytes, label: bytes) -> bytes:
    return hmac.new(
        master,
        b"hermes-provider-route-v1\0" + label,
        hashlib.sha256,
    ).digest()


def _route_digest(
    *,
    binding: ProviderRouteBindingIdentity,
    source: ProviderRouteSource,
    credential_fingerprint: str,
) -> str:
    payload = {
        "version": PROVIDER_ROUTE_CONTRACT_VERSION,
        "runtime_id": binding.runtime_id,
        "binding_type": binding.binding_type,
        "binding_id": binding.binding_id,
        "pvc_storage_identity_hash": binding.pvc_storage_identity_hash,
        "org_id": source.org_id,
        "model_entry_id": source.model_entry_id,
        "provider_id": source.provider_id,
        "upstream_model_id": source.upstream_model_id,
        "provider_base_url": source.provider_base_url,
        "model_catalog_version": source.model_catalog_version,
        "provider_catalog_version": source.provider_catalog_version,
        "credential_fingerprint": credential_fingerprint,
    }
    return "sha256:" + hashlib.sha256(_canonical_json(payload)).hexdigest()


def _aad(
    *,
    binding: ProviderRouteBindingIdentity,
    control_key_version: int,
) -> bytes:
    return _canonical_json(
        {
            "version": PROVIDER_ROUTE_CONTRACT_VERSION,
            "control_key_version": control_key_version,
            "runtime_id": binding.runtime_id,
            "binding_type": binding.binding_type,
            "binding_id": binding.binding_id,
            "pvc_storage_identity_hash": binding.pvc_storage_identity_hash,
        }
    )


def _validate_plaintext_shape(document: object) -> None:
    expected = {
        "version",
        "runtime_id",
        "binding_type",
        "binding_id",
        "pvc_storage_identity_hash",
        "org_id",
        "model_entry_id",
        "provider_id",
        "upstream_model_id",
        "provider_base_url",
        "provider_api_key",
        "model_catalog_version",
        "provider_catalog_version",
        "provider_route_digest",
        "issued_at",
        "expires_at",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("provider route plaintext shape is invalid")
    integer_fields = (
        "version",
        "binding_id",
        "org_id",
        "model_entry_id",
        "provider_id",
        "model_catalog_version",
        "provider_catalog_version",
        "issued_at",
        "expires_at",
    )
    if any(
        isinstance(document[field], bool) or not isinstance(document[field], int)
        for field in integer_fields
    ):
        raise ValueError("provider route plaintext integer is invalid")
    string_fields = expected.difference(integer_fields)
    if any(not isinstance(document[field], str) for field in string_fields):
        raise ValueError("provider route plaintext string is invalid")
    if _DIGEST_RE.fullmatch(document["provider_route_digest"]) is None:
        raise ValueError("provider route plaintext digest is invalid")


def _canonical_json(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or "=" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("provider route base64 encoding is invalid")
    decoded = base64.b64decode(
        value + ("=" * (-len(value) % 4)),
        altchars=b"-_",
        validate=True,
    )
    if _base64url_encode(decoded) != value:
        raise ValueError("provider route base64 encoding is not canonical")
    return decoded


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("provider route time must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "ProviderRouteBindingIdentity",
    "ProviderRouteCapsuleCodec",
    "ProviderRouteCapsuleError",
    "ProviderRouteMaterial",
    "binding_identity_from_settings",
]
