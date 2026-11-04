from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelEntry, ModelProvider, ModelUpgradeLog
from app.schemas.model_runtime_capabilities import normalize_runtime_capabilities
from app.schemas.provider_routes import ProviderRouteGrantV1
from app.services.provider_credentials import ProviderCredentialCodec
from app.services.runtime_platform_control import (
    RuntimePlatformControlKeyError,
    RuntimePlatformControlKeyStore,
    SqlAlchemyRuntimePlatformControlKeyStore,
)


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


class RuntimeProviderRouteError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
        normalized = normalize_provider_base_url(self.provider_base_url)
        if normalized != self.provider_base_url:
            raise ValueError("provider route base URL is not canonical")
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


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeProviderRoute:
    upstream_model_id: str
    context_length: int
    runtime_capabilities: dict[str, str]
    provider_id: int
    provider_route: ProviderRouteGrantV1


class ProviderRouteCapsuleCodec:
    def issue(
        self,
        *,
        root_key: bytes,
        control_key_version: int,
        binding: ProviderRouteBindingIdentity,
        source: ProviderRouteSource,
        issued_at: datetime | None = None,
        nonce: bytes | None = None,
    ) -> ProviderRouteGrantV1:
        now = _aware_utc(issued_at or datetime.now(timezone.utc))
        nonce_bytes = secrets.token_bytes(12) if nonce is None else nonce
        if len(nonce_bytes) != 12:
            raise ValueError("provider route nonce must be 12 bytes")
        master = _derive_master_key(
            root_key,
            binding=binding,
            control_key_version=control_key_version,
        )
        fingerprint_key = _derive_subkey(master, b"fingerprint")
        digest = _route_digest(
            binding=binding,
            source=source,
            credential_fingerprint=hmac.new(
                fingerprint_key,
                source.provider_api_key.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        )
        plaintext = _canonical_json(
            {
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
                "provider_api_key": source.provider_api_key,
                "model_catalog_version": source.model_catalog_version,
                "provider_catalog_version": source.provider_catalog_version,
                "provider_route_digest": digest,
                "issued_at": int(now.timestamp()),
                "expires_at": int((now + PROVIDER_ROUTE_TTL).timestamp()),
            }
        )
        ciphertext = AESGCM(_derive_subkey(master, b"aead")).encrypt(
            nonce_bytes,
            plaintext,
            _aad(binding=binding, control_key_version=control_key_version),
        )
        capsule = ".".join(
            (
                "prc1",
                str(control_key_version),
                _base64url_encode(nonce_bytes),
                _base64url_encode(ciphertext),
            )
        )
        return ProviderRouteGrantV1(capsule=capsule, digest=digest)

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
            expected_model = int(expected_model_entry_id)
            if (
                document["version"] != PROVIDER_ROUTE_CONTRACT_VERSION
                or document["runtime_id"] != binding.runtime_id
                or document["binding_type"] != binding.binding_type
                or document["binding_id"] != binding.binding_id
                or document["pvc_storage_identity_hash"]
                != binding.pvc_storage_identity_hash
                or document["org_id"] != expected_org_id
                or document["model_entry_id"] != expected_model
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


class RuntimeProviderRouteResolver:
    def __init__(
        self,
        db: AsyncSession,
        *,
        credential_codec: ProviderCredentialCodec | None = None,
        control_key_store: RuntimePlatformControlKeyStore | None = None,
        capsule_codec: ProviderRouteCapsuleCodec | None = None,
    ) -> None:
        self.db = db
        self.credential_codec = credential_codec or ProviderCredentialCodec()
        self.control_key_store = control_key_store or (
            SqlAlchemyRuntimePlatformControlKeyStore(db)
        )
        self.capsule_codec = capsule_codec or ProviderRouteCapsuleCodec()

    async def resolve(
        self,
        *,
        org_id: int,
        model_entry_id: int,
        binding: ProviderRouteBindingIdentity,
        issued_at: datetime | None = None,
    ) -> ResolvedRuntimeProviderRoute:
        published_version = (
            select(func.max(ModelUpgradeLog.catalog_version))
            .where(
                ModelUpgradeLog.org_id == org_id,
                ModelUpgradeLog.action == "model_catalog_publish",
                ModelUpgradeLog.status == "published",
            )
            .scalar_subquery()
        )
        row = (
            await self.db.execute(
                select(ModelEntry, ModelProvider)
                .join(ModelProvider, ModelProvider.id == ModelEntry.provider_id)
                .where(
                    ModelEntry.id == model_entry_id,
                    ModelEntry.org_id == org_id,
                    ModelProvider.org_id == org_id,
                    ModelEntry.status == "active",
                    ModelProvider.status == "active",
                    ModelEntry.catalog_version == published_version,
                    ModelProvider.catalog_version == published_version,
                )
            )
        ).first()
        if row is None:
            raise RuntimeProviderRouteError("RUNTIME_MODEL_PROVIDER_UNAVAILABLE")
        model, provider = row
        model_context_length = getattr(model, "context_length", None)
        if not provider.api_endpoint or not provider.api_key_encrypted or not model.model_key:
            raise RuntimeProviderRouteError("RUNTIME_MODEL_PROVIDER_UNAVAILABLE")
        try:
            base_url = normalize_provider_base_url(str(provider.api_endpoint))
            api_key = self.credential_codec.decrypt(str(provider.api_key_encrypted))
            if not api_key:
                raise ValueError("provider credential is empty")
            key_material = await self.control_key_store.load_active_key(
                binding_type=binding.binding_type,
                binding_id=binding.binding_id,
                runtime_id=binding.runtime_id,
            )
            source = ProviderRouteSource(
                org_id=org_id,
                model_entry_id=int(model.id),
                provider_id=int(provider.id),
                upstream_model_id=str(model.model_key),
                provider_base_url=base_url,
                provider_api_key=api_key,
                model_catalog_version=int(model.catalog_version),
                provider_catalog_version=int(provider.catalog_version),
            )
            grant = self.capsule_codec.issue(
                root_key=key_material.root_key,
                control_key_version=key_material.key_version,
                binding=binding,
                source=source,
                issued_at=issued_at,
            )
        except RuntimeProviderRouteError:
            raise
        except (RuntimePlatformControlKeyError, ValueError, TypeError) as exc:
            raise RuntimeProviderRouteError(
                "RUNTIME_PROVIDER_ROUTE_ISSUANCE_FAILED"
            ) from exc
        return ResolvedRuntimeProviderRoute(
            upstream_model_id=source.upstream_model_id,
            context_length=(
                int(model_context_length)
                if int(model_context_length or 0) > 0
                else 128_000
            ),
            runtime_capabilities=normalize_runtime_capabilities(
                model.runtime_capabilities
            ),
            provider_id=source.provider_id,
            provider_route=grant,
        )


def normalize_provider_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ValueError("provider endpoint is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError("provider endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("provider endpoint must be a canonical HTTPS URL")
    decoded_segments = unquote(parsed.path).split("/")
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise ValueError("provider endpoint path is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider endpoint port is invalid") from exc
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    canonical = urlunsplit(("https", authority, path, "", ""))
    if len(canonical) > 500:
        raise ValueError("provider endpoint is too long")
    return canonical


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
    "ProviderRouteSource",
    "ResolvedRuntimeProviderRoute",
    "RuntimeProviderRouteError",
    "RuntimeProviderRouteResolver",
    "normalize_provider_base_url",
]
