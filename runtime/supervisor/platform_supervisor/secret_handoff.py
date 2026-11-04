from __future__ import annotations

from collections.abc import Mapping
import errno
import os
from pathlib import Path
import stat


PLATFORM_CONTROL_KEY_FD_ENV = "PLATFORM_CONTROL_KEY_FD"
PLATFORM_SCHEMA_EXPECTATION_FD_ENV = "PLATFORM_SCHEMA_EXPECTATION_FD"
PLATFORM_CONTROL_KEY_PATH = Path(
    "/run/secrets/hermes-platform-control/control-keys.json"
)
PLATFORM_SCHEMA_EXPECTATION_PATH = Path(
    "/run/config/hermes-schema-startup/expectation.json"
)
MAX_HANDOFF_DOCUMENT_BYTES = 64 * 1024


class RootSecretHandoffError(RuntimeError):
    """A root-only startup document could not be handed to Supervisor safely."""


def prepare_root_secret_handoffs(
    environment: Mapping[str, str],
    *,
    documents: tuple[tuple[str, Path], ...] | None = None,
) -> tuple[dict[str, str], tuple[int, ...]]:
    """Open mounted root-only documents before dropping uid and pass only FD numbers.

    Missing documents are left for the controlled-startup loaders to reject.  A
    present but unsafe document always fails closed.
    """

    selected = documents or (
        (PLATFORM_CONTROL_KEY_FD_ENV, PLATFORM_CONTROL_KEY_PATH),
        (PLATFORM_SCHEMA_EXPECTATION_FD_ENV, PLATFORM_SCHEMA_EXPECTATION_PATH),
    )
    inherited_environment = dict(environment)
    descriptors: list[int] = []
    for variable, _path in selected:
        inherited_environment.pop(variable, None)
    try:
        for variable, path in selected:
            descriptor = _open_root_only_document(path)
            if descriptor is None:
                continue
            descriptors.append(descriptor)
            inherited_environment[variable] = str(descriptor)
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    return inherited_environment, tuple(descriptors)


def consume_root_secret_handoff(
    environment_variable: str,
    *,
    max_bytes: int = MAX_HANDOFF_DOCUMENT_BYTES,
) -> bytes | None:
    """Consume and close a single inherited startup FD.

    The FD number is removed from the environment before validation, is made
    close-on-exec immediately, and is closed on every path.  A second consumer
    therefore cannot recover the document and child processes cannot inherit it.
    """

    raw_descriptor = os.environ.pop(environment_variable, None)
    if raw_descriptor is None:
        return None
    if not raw_descriptor.isascii() or not raw_descriptor.isdigit():
        raise RootSecretHandoffError("startup secret handoff descriptor is invalid")
    descriptor = int(raw_descriptor)
    if descriptor < 3 or str(descriptor) != raw_descriptor:
        raise RootSecretHandoffError("startup secret handoff descriptor is invalid")
    try:
        os.set_inheritable(descriptor, False)
        metadata = os.fstat(descriptor)
        _validate_root_only_document(metadata, max_bytes=max_bytes)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > max_bytes:
            raise RootSecretHandoffError("startup secret document is too large")
        return encoded
    except OSError as exc:
        raise RootSecretHandoffError(
            "startup secret handoff descriptor is unavailable"
        ) from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_root_only_document(path: Path) -> int | None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise RootSecretHandoffError(
            f"cannot open root-only startup document: {path}"
        ) from exc
    try:
        _validate_root_only_document(
            os.fstat(descriptor),
            max_bytes=MAX_HANDOFF_DOCUMENT_BYTES,
        )
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_root_only_document(metadata: os.stat_result, *, max_bytes: int) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise RootSecretHandoffError("startup secret path is not a regular file")
    if metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise RootSecretHandoffError(
            "startup secret ownership or permissions are unsafe"
        )
    if metadata.st_size <= 0 or metadata.st_size > max_bytes:
        raise RootSecretHandoffError("startup secret document size is invalid")


__all__ = [
    "MAX_HANDOFF_DOCUMENT_BYTES",
    "PLATFORM_CONTROL_KEY_FD_ENV",
    "PLATFORM_CONTROL_KEY_PATH",
    "PLATFORM_SCHEMA_EXPECTATION_FD_ENV",
    "PLATFORM_SCHEMA_EXPECTATION_PATH",
    "RootSecretHandoffError",
    "consume_root_secret_handoff",
    "prepare_root_secret_handoffs",
]
