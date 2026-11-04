from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import PurePosixPath
from pathlib import Path
from typing import Mapping

from app.services.runtime_user_memory import merge_platform_soul


MAX_CONFIG_BUNDLE_FILES = 2_000
MAX_CONFIG_BUNDLE_BYTES = 32 * 1024 * 1024
_ALLOWED_ROOT_FILES = frozenset({"config.yaml", "SOUL.md"})
_ALLOWED_ROOT_DIRS = frozenset(
    {"PERSONAL", "memories", "platform", "plugins", "skills"}
)
_NON_TEXT_CONFIG_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_NON_TEXT_CONFIG_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".dll", ".dylib", ".class"}
)


def _is_runtime_cache_path(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return len(parts) >= 2 and parts[0] == "skills" and "cache" in parts[1:]


class RuntimeConfigBundleError(ValueError):
    pass


def validate_config_bundle_files(
    files: Mapping[str, str],
) -> dict[str, str]:
    if not files or len(files) > MAX_CONFIG_BUNDLE_FILES:
        raise RuntimeConfigBundleError("CONFIG_BUNDLE_FILE_COUNT_INVALID")
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, content in files.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise RuntimeConfigBundleError("CONFIG_BUNDLE_FILE_INVALID")
        path = PurePosixPath(raw_path)
        parts = path.parts
        if (
            not raw_path
            or raw_path.startswith("/")
            or "\\" in raw_path
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
            or (
                len(parts) == 1
                and parts[0] not in _ALLOWED_ROOT_FILES
            )
            or (
                len(parts) > 1
                and parts[0] not in _ALLOWED_ROOT_DIRS
            )
        ):
            raise RuntimeConfigBundleError("CONFIG_BUNDLE_PATH_FORBIDDEN")
        canonical = path.as_posix()
        if canonical in normalized:
            raise RuntimeConfigBundleError("CONFIG_BUNDLE_PATH_DUPLICATE")
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > MAX_CONFIG_BUNDLE_BYTES:
            raise RuntimeConfigBundleError("CONFIG_BUNDLE_TOO_LARGE")
        normalized[canonical] = content
    if "config.yaml" not in normalized or "SOUL.md" not in normalized:
        raise RuntimeConfigBundleError("CONFIG_BUNDLE_BASELINE_MISSING")
    return normalized


def config_bundle_digest(files: Mapping[str, str]) -> str:
    normalized = validate_config_bundle_files(files)
    digest = hashlib.sha256()
    for path in sorted(normalized):
        path_bytes = path.encode("utf-8")
        content_bytes = normalized[path].encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(content_bytes).to_bytes(8, "big"))
        digest.update(content_bytes)
    return digest.hexdigest()


def read_config_bundle_files(home: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    roots = (
        home / "config.yaml",
        home / "SOUL.md",
        home / "PERSONAL",
        home / "memories",
        home / "platform",
        home / "plugins",
        home / "skills",
    )
    for root in roots:
        if root.is_symlink():
            raise RuntimeConfigBundleError(
                "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
            )
        candidates = (
            [root]
            if root.is_file()
            else sorted(root.rglob("*"))
            if root.is_dir()
            else []
        )
        for path in candidates:
            if path.is_symlink():
                raise RuntimeConfigBundleError(
                    "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                )
            if not path.is_file():
                continue
            relative_path = path.relative_to(home)
            if _is_runtime_cache_path(relative_path.as_posix()):
                continue
            if (
                any(part in _NON_TEXT_CONFIG_DIRS for part in relative_path.parts)
                or path.suffix.casefold() in _NON_TEXT_CONFIG_SUFFIXES
            ):
                continue
            try:
                content = path.read_bytes()
            except UnicodeError:
                # Skill packages may contain optional binary assets or generated
                # files. ConfigBundle only transports UTF-8 text; omit files
                # that cannot be represented by its text-only contract.
                continue
            except OSError as exc:
                raise RuntimeConfigBundleError("CONFIG_BUNDLE_FILE_INVALID") from exc
            if b"\x00" in content:
                continue
            try:
                files[relative_path.as_posix()] = content.decode("utf-8")
            except UnicodeError:
                # A package may contain binary files without a conventional
                # extension. They are not part of the text-only Bundle contract.
                continue
    return validate_config_bundle_files(files)


def personal_config_bundle_files(
    *,
    artifacts: Mapping[str, str],
    name: str,
    user_code: str,
) -> dict[str, str]:
    files = dict(artifacts)
    files["SOUL.md"] = merge_platform_soul(
        files["SOUL.md"],
        name=name,
        user_code=user_code,
    )
    files["platform/user-identity.json"] = (
        json.dumps(
            {
                "name": name,
                "schema_version": 1,
                "user_code": user_code,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return validate_config_bundle_files(files)


def config_bundle_signature(
    *,
    secret: str,
    runtime_id: str,
    binding_id: str,
    config_revision: str,
    config_digest: str,
    download_url: str,
    minimum_protocol_version: int,
    expires_at: datetime,
) -> str:
    if expires_at.tzinfo is None:
        raise RuntimeConfigBundleError("CONFIG_BUNDLE_EXPIRY_MUST_BE_AWARE")
    payload = {
        "binding_id": binding_id,
        "config_digest": config_digest,
        "config_revision": config_revision,
        "download_url": download_url,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "minimum_protocol_version": minimum_protocol_version,
        "runtime_id": runtime_id,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()


def verify_config_bundle_signature(
    *,
    signature: str,
    **kwargs: object,
) -> bool:
    expected = config_bundle_signature(**kwargs)  # type: ignore[arg-type]
    return hmac.compare_digest(signature, expected)
