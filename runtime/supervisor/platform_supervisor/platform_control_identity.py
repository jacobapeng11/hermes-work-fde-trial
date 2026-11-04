from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from platform_supervisor.settings import SupervisorSettings
from platform_supervisor.secret_handoff import (
    PLATFORM_CONTROL_KEY_FD_ENV,
    RootSecretHandoffError,
    consume_root_secret_handoff,
)


MAX_PLATFORM_CONTROL_KEY_DOCUMENT_BYTES = 64 * 1024
RuntimeBindingType = Literal["personal_runtime", "application_runtime"]


class PlatformControlIdentityError(RuntimeError):
    """The mounted Binding-scoped control identity is absent or untrusted."""


@dataclass(frozen=True, slots=True)
class PlatformControlIdentity:
    runtime_id: str
    binding_type: RuntimeBindingType
    binding_id: int
    pvc_storage_identity_hash: str
    supervisor_boot_id: str
    pod_uid: str
    revision_name: str

    def __post_init__(self) -> None:
        if not self.runtime_id or len(self.runtime_id) > 128:
            raise ValueError("runtime_id is invalid")
        if self.binding_type not in {
            "personal_runtime",
            "application_runtime",
        }:
            raise ValueError("binding_type is invalid")
        if self.binding_id <= 0:
            raise ValueError("binding_id is invalid")
        if not _is_prefixed_sha256(self.pvc_storage_identity_hash):
            raise ValueError("pvc_storage_identity_hash is invalid")
        for label, value in (
            ("supervisor_boot_id", self.supervisor_boot_id),
            ("pod_uid", self.pod_uid),
            ("revision_name", self.revision_name),
        ):
            if len(value) < 8 or len(value) > 256:
                raise ValueError(f"{label} is invalid")


class _StrictControlIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlatformControlKeyDocumentV1(_StrictControlIdentityModel):
    contract_version: Literal[1]
    runtime_id: str = Field(min_length=1, max_length=128)
    binding_type: RuntimeBindingType
    binding_id: int = Field(gt=0)
    pvc_storage_identity_hash: str = Field(
        pattern=r"^sha256:[a-f0-9]{64}$"
    )
    current_key_version: int = Field(gt=0)
    keys: dict[str, str] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_key_ring(self) -> PlatformControlKeyDocumentV1:
        versions: list[int] = []
        for raw_version, encoded in self.keys.items():
            try:
                version = int(raw_version)
            except ValueError as exc:
                raise ValueError("platform control key version is invalid") from exc
            if version <= 0 or str(version) != raw_version:
                raise ValueError("platform control key version is not canonical")
            _decode_root_key(encoded)
            versions.append(version)
        if len(versions) != len(set(versions)):
            raise ValueError("platform control key versions are not unique")
        if self.current_key_version not in versions:
            raise ValueError("current platform control key is absent")
        return self


@dataclass(frozen=True, slots=True)
class LoadedPlatformControlIdentity:
    identity: PlatformControlIdentity
    control_root_keys: dict[int, bytes]
    current_key_version: int


def load_platform_control_identity(
    settings: SupervisorSettings,
    *,
    supervisor_boot_id: str,
) -> LoadedPlatformControlIdentity:
    """Load a root-owned, read-only key ring and bind it to the live Pod.

    The document is intentionally read without following a symlink.  Nothing
    from it is copied into the Hermes child environment or inherited file
    descriptors.
    """

    binding_type: RuntimeBindingType = cast(
        RuntimeBindingType,
        "application_runtime"
        if settings.RUNTIME_MODE == "application"
        else "personal_runtime",
    )
    try:
        binding_id = int(settings.RUNTIME_BINDING_ID)
    except ValueError as exc:
        raise PlatformControlIdentityError(
            "Runtime binding id is not numeric"
        ) from exc
    pvc_identity = settings.RUNTIME_PVC_STORAGE_IDENTITY_HASH
    pod_uid = settings.HERMES_SUPERVISOR_POD_UID
    revision_name = settings.HERMES_KNATIVE_REVISION_NAME
    if pvc_identity is None or pod_uid is None or revision_name is None:
        raise PlatformControlIdentityError(
            "live Pod control identity is incomplete"
        )

    try:
        inherited_document = consume_root_secret_handoff(
            PLATFORM_CONTROL_KEY_FD_ENV,
            max_bytes=MAX_PLATFORM_CONTROL_KEY_DOCUMENT_BYTES,
        )
    except RootSecretHandoffError as exc:
        raise PlatformControlIdentityError(
            "platform control key handoff is invalid"
        ) from exc
    document = (
        _parse_key_document(inherited_document)
        if inherited_document is not None
        else _read_key_document(settings.PLATFORM_CONTROL_KEY_FILE)
    )
    if (
        document.runtime_id != settings.RUNTIME_ID
        or document.binding_type != binding_type
        or document.binding_id != binding_id
        or document.pvc_storage_identity_hash != pvc_identity
    ):
        raise PlatformControlIdentityError(
            "mounted platform control identity does not match this Runtime"
        )
    keys = {
        int(version): _decode_root_key(encoded)
        for version, encoded in document.keys.items()
    }
    return LoadedPlatformControlIdentity(
        identity=PlatformControlIdentity(
            runtime_id=settings.RUNTIME_ID,
            binding_type=binding_type,
            binding_id=binding_id,
            pvc_storage_identity_hash=pvc_identity,
            supervisor_boot_id=supervisor_boot_id,
            pod_uid=pod_uid,
            revision_name=revision_name,
        ),
        control_root_keys=keys,
        current_key_version=document.current_key_version,
    )


def _read_key_document(path: Path) -> PlatformControlKeyDocumentV1:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PlatformControlIdentityError(
            "platform control key file is unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PlatformControlIdentityError(
                "platform control key path is not a regular file"
            )
        if metadata.st_size <= 0 or metadata.st_size > (
            MAX_PLATFORM_CONTROL_KEY_DOCUMENT_BYTES
        ):
            raise PlatformControlIdentityError(
                "platform control key document size is invalid"
            )
        if metadata.st_mode & 0o027:
            raise PlatformControlIdentityError(
                "platform control key file permissions are too broad"
            )
        chunks: list[bytes] = []
        remaining = MAX_PLATFORM_CONTROL_KEY_DOCUMENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > MAX_PLATFORM_CONTROL_KEY_DOCUMENT_BYTES:
            raise PlatformControlIdentityError(
                "platform control key document is too large"
            )
    finally:
        os.close(descriptor)
    return _parse_key_document(encoded)


def _parse_key_document(encoded: bytes) -> PlatformControlKeyDocumentV1:
    try:
        raw = json.loads(encoded)
        return PlatformControlKeyDocumentV1.model_validate(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PlatformControlIdentityError(
            "platform control key document is invalid"
        ) from exc


def _decode_root_key(value: str) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError("platform control root key encoding is invalid")
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)),
            altchars=b"-_",
            validate=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("platform control root key encoding is invalid") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if canonical != value or len(decoded) < 32:
        raise ValueError("platform control root key is invalid")
    return decoded


def _is_prefixed_sha256(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])


__all__ = [
    "LoadedPlatformControlIdentity",
    "PlatformControlIdentity",
    "PlatformControlIdentityError",
    "PlatformControlKeyDocumentV1",
    "RuntimeBindingType",
    "load_platform_control_identity",
]
