from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
from uuid import uuid4
from typing import Iterable, Mapping, Protocol
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from platform_supervisor.contracts import (
    ConfigBundlePayload,
    ConfigBundleReference,
)
from platform_supervisor.execution import SessionManager


CONFIG_BUNDLE_SCHEMA_VERSION = 1
MAX_CONFIG_BUNDLE_FILES = 2_000
MAX_CONFIG_BUNDLE_BYTES = 32 * 1024 * 1024
_ALLOWED_ROOT_FILES = frozenset({"config.yaml", "SOUL.md"})
_ALLOWED_ROOT_DIRS = frozenset(
    {"PERSONAL", "memories", "platform", "plugins", "skills"}
)
_RUNTIME_GENERATED_CONFIG_PATHS = frozenset(
    {
        "skills/.bundled_manifest",
        # Hermes owns this usage telemetry sidecar.  It is written by the
        # Dashboard uid and can be unreadable to the root-squashed Supervisor
        # process on shared storage; it is runtime state, not ConfigBundle
        # authority.
        "skills/.usage.json",
    }
)
_RUNTIME_CACHE_DIR_NAMES = frozenset(
    {"cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_RUNTIME_CACHE_FILE_SUFFIXES = frozenset(
    {".pyc", ".pyo", ".so", ".dll", ".dylib", ".class"}
)
_PERSONAL_HERMES_DYNAMIC_FILES = frozenset(
    {"memories/USER.md", "memories/MEMORY.md"}
)
_APPLICATION_AGENT_CONTRACT_PATH = "platform/application-agent.json"
_APPLICATION_SKILLS_PREFIX = "skills/application/"
_APPLICATION_RUNTIME_MUTATION_NAMES = frozenset({".venv", "work"})
_APPLICATION_SKILL_DIRECTORY_MODE = 0o750
_APPLICATION_SKILL_FILE_MODE = 0o640


logger = logging.getLogger(__name__)


def _is_runtime_cache_path(relative: str) -> bool:
    """Return whether a path belongs to a runtime-owned skill cache."""

    parts = PurePosixPath(relative).parts
    return (
        len(parts) >= 2
        and parts[0] == "skills"
        and (
            any(part in _RUNTIME_CACHE_DIR_NAMES for part in parts[1:])
            or PurePosixPath(relative).suffix.casefold()
            in _RUNTIME_CACHE_FILE_SUFFIXES
        )
    )


def _hermes_dynamic_files(home: Path) -> frozenset[str]:
    # Application Runtime still owns its application-level USER.md projection.
    # Personal Runtime USER.md/MEMORY.md are Hermes dynamic data, not config.
    if (home / "platform" / "application-agent.json").exists():
        return frozenset()
    return _PERSONAL_HERMES_DYNAMIC_FILES


def _application_skill_root(relative: str) -> str | None:
    if not relative.startswith(_APPLICATION_SKILLS_PREFIX):
        return None
    remainder = relative[len(_APPLICATION_SKILLS_PREFIX) :]
    if not remainder:
        return None
    return f"{_APPLICATION_SKILLS_PREFIX}{remainder.split('/', 1)[0]}"


def _is_relative_below(relative: str, root: str) -> bool:
    return relative == root or relative.startswith(f"{root}/")


def _is_application_removal_bundle(files: Mapping[str, str]) -> bool:
    return _APPLICATION_AGENT_CONTRACT_PATH in files


def _application_removal_paths(
    *,
    previous: Mapping[str, str] | Iterable[str],
    desired: Mapping[str, str],
    home: Path | None = None,
) -> frozenset[str]:
    """Return application paths that a removal bundle must reclaim.

    Runtime-created ``.venv`` and ``work`` trees are not configuration and may
    contain symlinks or exceed the bundle limit.  They are moved atomically to
    the apply backup instead of being read or copied.
    """

    if not _is_application_removal_bundle(desired):
        return frozenset()
    previous_paths = (
        previous.keys() if isinstance(previous, Mapping) else iter(previous)
    )
    previous_roots = {
        root
        for relative in previous_paths
        if (root := _application_skill_root(relative)) is not None
    }
    desired_roots = {
        root
        for relative in desired
        if (root := _application_skill_root(relative)) is not None
    }
    live_roots = _live_application_skill_roots(home) if home else set()
    removed_roots = (previous_roots | live_roots) - desired_roots
    mutations = {
        f"{root}/{name}"
        for root in desired_roots
        for name in _APPLICATION_RUNTIME_MUTATION_NAMES
    }
    return frozenset(removed_roots | mutations)


def _live_application_skill_roots(home: Path | None) -> frozenset[str]:
    if home is None:
        return frozenset()
    application_root = home / "skills" / "application"
    if not application_root.exists() and not application_root.is_symlink():
        return frozenset()
    return frozenset(
        f"{_APPLICATION_SKILLS_PREFIX}{item.name}"
        for item in application_root.iterdir()
    )


class RuntimeConfigError(RuntimeError):
    pass


class RuntimeConfigUnavailable(RuntimeConfigError):
    pass


class ConfigApplyAdapter(Protocol):
    async def apply_config(
        self,
        *,
        config_revision: str,
        config_digest: str,
        reload_skills: bool,
        reload_mcp: bool,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class AppliedConfig:
    config_revision: str
    config_digest: str


def digest_managed_runtime_config(home: Path) -> str:
    """Digest existing managed files for first-start compatibility."""
    return config_bundle_digest(_read_managed_files(home))


def validate_config_bundle_files(
    files: Mapping[str, str],
) -> dict[str, str]:
    if not files or len(files) > MAX_CONFIG_BUNDLE_FILES:
        raise RuntimeConfigError("CONFIG_BUNDLE_FILE_COUNT_INVALID")
    normalized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, content in files.items():
        if not isinstance(raw_path, str) or not isinstance(content, str):
            raise RuntimeConfigError("CONFIG_BUNDLE_FILE_INVALID")
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
            raise RuntimeConfigError("CONFIG_BUNDLE_PATH_FORBIDDEN")
        canonical = path.as_posix()
        if canonical in normalized:
            raise RuntimeConfigError("CONFIG_BUNDLE_PATH_DUPLICATE")
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > MAX_CONFIG_BUNDLE_BYTES:
            raise RuntimeConfigError("CONFIG_BUNDLE_TOO_LARGE")
        normalized[canonical] = content
    if "config.yaml" not in normalized or "SOUL.md" not in normalized:
        raise RuntimeConfigError("CONFIG_BUNDLE_BASELINE_MISSING")
    return normalized


def config_bundle_digest(files: Mapping[str, str]) -> str:
    normalized = validate_config_bundle_files(files)
    digest = hashlib.sha256()
    for relative in sorted(normalized):
        path_bytes = relative.encode("utf-8")
        content_bytes = normalized[relative].encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(content_bytes).to_bytes(8, "big"))
        digest.update(content_bytes)
    return digest.hexdigest()


def _read_managed_files(
    home: Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
) -> dict[str, str]:
    roots = (
        home / "config.yaml",
        home / "SOUL.md",
        home / "PERSONAL",
        home / "memories",
        home / "platform",
        home / "plugins",
        home / "skills",
    )
    files: dict[str, str] = {}
    dynamic_files = _hermes_dynamic_files(home)
    for root in roots:
        if root.is_symlink():
            raise RuntimeConfigError("CONFIG_BUNDLE_SYMLINK_FORBIDDEN")
        candidates = (
            [root]
            if root.is_file()
            else sorted(root.rglob("*"))
            if root.is_dir()
            else []
        )
        for path in candidates:
            relative = path.relative_to(home).as_posix()
            if any(
                _is_relative_below(relative, excluded)
                for excluded in excluded_paths
            ):
                continue
            if _is_runtime_cache_path(relative):
                continue
            if relative in dynamic_files:
                continue
            if path.is_symlink():
                raise RuntimeConfigError("CONFIG_BUNDLE_SYMLINK_FORBIDDEN")
            if not path.is_file():
                continue
            if relative in _RUNTIME_GENERATED_CONFIG_PATHS:
                continue
            try:
                files[relative] = path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                # ConfigBundle v1 transports text only.  Skill/plugin trees may
                # legitimately contain immutable binary assets (PDF templates,
                # images, archives); they are not managed config and must not
                # make a sealed schema-34 boot fail or be deleted later.
                if relative not in _ALLOWED_ROOT_FILES:
                    continue
                raise RuntimeConfigError(
                    "CONFIG_BUNDLE_FILE_INVALID"
                ) from exc
            except OSError as exc:
                raise RuntimeConfigError(
                    "CONFIG_BUNDLE_FILE_INVALID"
                ) from exc
    return validate_config_bundle_files(files)


def config_bundle_signature(
    *,
    secret: str,
    bundle: ConfigBundleReference,
) -> str:
    payload = {
        "binding_id": bundle.binding_id,
        "config_digest": bundle.config_digest,
        "config_revision": bundle.config_revision,
        "download_url": bundle.download_url,
        "expires_at": bundle.expires_at.astimezone(timezone.utc).isoformat(),
        "minimum_protocol_version": bundle.minimum_protocol_version,
        "runtime_id": bundle.runtime_id,
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


class RuntimeConfigController:
    def __init__(
        self,
        *,
        home: Path,
        state_path: Path,
        initial_revision: str,
        adapter: ConfigApplyAdapter,
        sessions: SessionManager,
        runtime_id: str,
        binding_id: str,
        api_server_key: str,
        backend_base_url: str,
        initial_download_url: str | None = None,
        initial_digest: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.home = home
        self.state_path = state_path
        self.initial_revision = initial_revision
        self.adapter = adapter
        self.sessions = sessions
        self.runtime_id = runtime_id
        self.binding_id = binding_id
        self.api_server_key = api_server_key
        self.backend_base_url = backend_base_url.rstrip("/")
        self.initial_download_url = initial_download_url
        self.initial_digest = initial_digest
        self._client = client
        self._owns_client = client is None
        self._current = AppliedConfig(initial_revision, "")
        self._managed_files: tuple[str, ...] = ()
        self._lock = asyncio.Lock()

    @property
    def current_revision(self) -> str:
        return self._current.config_revision

    @property
    def current_digest(self) -> str:
        return self._current.config_digest

    async def start(self, *, preserve_migrated_local: bool = False) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._recover_incomplete_apply()
        if preserve_migrated_local:
            # A signed schema migration has already made the local schema-34
            # bundle authoritative and sealed.  The predecessor Backend
            # bundle is schema 33 and must not overwrite it during the first
            # target boot.  Re-anchor only the Supervisor-side revision state;
            # no Hermes-home bytes are changed here.
            files = _read_managed_files(self.home)
            digest = config_bundle_digest(files)
            self._current = AppliedConfig(self.initial_revision, digest)
            managed_files = set(files) | self._derived_managed_paths(files)
            if self.initial_download_url and self.initial_digest:
                initial = self._initial_bundle()
                downloaded = await self._download(initial)
                initial_files = validate_config_bundle_files(downloaded.files)
                initial_digest = config_bundle_digest(initial_files)
                if (
                    initial_digest != self.initial_digest
                    or downloaded.config_digest != self.initial_digest
                ):
                    raise RuntimeConfigError("CONFIG_BUNDLE_DIGEST_MISMATCH")
                managed_files = self._narrow_migrated_managed_paths(
                    previous=managed_files,
                    authoritative=initial_files,
                )
            self._managed_files = tuple(sorted(managed_files))
            self._persist()
            self._finish_apply()
            return
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            revision = str(payload["config_revision"])
            digest = str(payload["config_digest"])
            managed_files = tuple(
                str(item)
                for item in payload.get("managed_files", [])
                if not _is_runtime_cache_path(str(item))
                and str(item) not in _hermes_dynamic_files(self.home)
            )
        except FileNotFoundError:
            if self.initial_download_url and self.initial_digest:
                initial = self._initial_bundle()
                downloaded = await self._download(initial)
                files = validate_config_bundle_files(downloaded.files)
                digest = config_bundle_digest(files)
                if digest != self.initial_digest:
                    raise RuntimeConfigError(
                        "CONFIG_BUNDLE_DIGEST_MISMATCH"
                    )
                self.home.mkdir(parents=True, exist_ok=True)
                initial_managed = set(files) | self._derived_managed_paths(
                    files
                )
                self._managed_files = tuple(
                    sorted(
                        relative
                        for relative in initial_managed
                        if self._safe_home_path(relative).is_file()
                    )
                )
                self._publish_files(files, initial)
            else:
                files = _read_managed_files(self.home)
                digest = config_bundle_digest(files)
            self._current = AppliedConfig(
                self.initial_revision,
                digest,
            )
            self._managed_files = tuple(
                sorted(
                    set(files)
                    | self._derived_managed_paths(files)
                )
            )
            self._persist()
            self._finish_apply()
            return
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeConfigError("CURRENT_CONFIG_STATE_INVALID") from exc
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RuntimeConfigError("CURRENT_CONFIG_DIGEST_INVALID")
        for relative in managed_files:
            self._safe_home_path(relative)
        self._current = AppliedConfig(revision, digest)
        self._managed_files = tuple(sorted(set(managed_files)))
        # Migrate legacy state written before Hermes-owned dynamic files were
        # removed from ConfigBundle management. Never read/backup MEMORY.md.
        self._persist()
        await self._refresh_initial_bundle_if_needed()

    async def _refresh_initial_bundle_if_needed(self) -> None:
        """Repair a persisted config state when a new Pod has a newer bundle.

        ``current-config.json`` is stored on the Runtime PVC and therefore
        survives a Pod/image replacement.  It must not make the Supervisor
        trust an old ``config.yaml`` when the KService supplies a different
        authoritative initial bundle (for example, after a platform baseline
        plugin is added).  This runs before Hermes starts, so publishing the
        files does not require a live Dashboard reload.
        """

        if not self.initial_download_url or not self.initial_digest:
            return
        initial = self._initial_bundle()
        downloaded = await self._download(initial)
        files = validate_config_bundle_files(downloaded.files)
        digest = config_bundle_digest(files)
        if (
            digest != self.initial_digest
            or downloaded.config_digest != self.initial_digest
        ):
            raise RuntimeConfigError("CONFIG_BUNDLE_DIGEST_MISMATCH")
        # The initial bundle is a KService template hint, not a newer source
        # of truth than the PVC journal.  A user can save a desired config
        # while a Pod is being replaced; the replacement may still carry the
        # previous template generation.  Never let that older template
        # downgrade a newer, already persisted generation on the PVC.
        current_generation = self._generation(self._current.config_revision)
        initial_generation = self._generation(initial.config_revision)
        if (
            current_generation is not None
            and initial_generation is not None
            and initial_generation < current_generation
        ):
            self._reclaim_application_runtime_mutations(files)
            self._enforce_application_skill_readonly(files)
            return
        self._managed_files = tuple(
            sorted(
                self._narrow_migrated_managed_paths(
                    previous=set(self._managed_files),
                    authoritative=files,
                )
            )
        )
        config_path = self._safe_home_path("config.yaml")
        try:
            current_config = config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current_config = None
        except (OSError, UnicodeError) as exc:
            raise RuntimeConfigError("CURRENT_CONFIG_STATE_INVALID") from exc
        if (
            self._current.config_revision == initial.config_revision
            and self._current.config_digest == self.initial_digest
            and current_config == files["config.yaml"]
        ):
            self._reclaim_application_runtime_mutations(files)
            self._enforce_application_skill_readonly(files)
            return
        self._publish_files(files, initial)
        self._current = AppliedConfig(
            initial.config_revision,
            self.initial_digest,
        )
        self._managed_files = tuple(
            sorted(set(files) | self._derived_managed_paths(files))
        )
        self._persist()
        self._finish_apply()

    def require_revision(self, config_revision: str) -> None:
        if config_revision != self.current_revision:
            raise RuntimeConfigError("CONFIG_REVISION_STALE")

    async def apply(self, bundle: ConfigBundleReference) -> AppliedConfig:
        async with self._lock:
            now = datetime.now(timezone.utc)
            if bundle.expires_at <= now:
                raise RuntimeConfigError("CONFIG_BUNDLE_EXPIRED")
            if bundle.minimum_protocol_version > CONFIG_BUNDLE_SCHEMA_VERSION:
                raise RuntimeConfigError("SUPERVISOR_PROTOCOL_TOO_OLD")
            if (
                bundle.runtime_id != self.runtime_id
                or bundle.binding_id != self.binding_id
            ):
                raise RuntimeConfigError("CONFIG_BUNDLE_TARGET_MISMATCH")
            if not hmac.compare_digest(
                bundle.signature,
                config_bundle_signature(
                    secret=self.api_server_key,
                    bundle=bundle,
                ),
            ):
                raise RuntimeConfigError("CONFIG_BUNDLE_SIGNATURE_INVALID")
            current_generation = self._generation(
                self._current.config_revision
            )
            requested_generation = self._generation(
                bundle.config_revision
            )
            if (
                current_generation is not None
                and requested_generation is not None
            ):
                if requested_generation < current_generation:
                    raise RuntimeConfigError("CONFIG_REVISION_STALE")
                if requested_generation == current_generation:
                    if bundle.config_digest != self._current.config_digest:
                        raise RuntimeConfigError(
                            "CONFIG_REVISION_DIGEST_CONFLICT"
                        )
                    return self._current
            payload = await self._download(bundle)
            files = validate_config_bundle_files(payload.files)
            observed = config_bundle_digest(files)
            if (
                observed != bundle.config_digest
                or payload.config_digest != bundle.config_digest
            ):
                raise RuntimeConfigError("CONFIG_BUNDLE_DIGEST_MISMATCH")
            previous = self._current
            previous_files = self._managed_files
            reload_skills, reload_mcp = self._reload_plan(files)
            self._publish_files(files, bundle)
            try:
                await self.adapter.apply_config(
                    config_revision=bundle.config_revision,
                    config_digest=bundle.config_digest,
                    reload_skills=reload_skills,
                    reload_mcp=reload_mcp,
                )
            except Exception:
                self._rollback_incomplete_apply()
                self._current = previous
                self._managed_files = previous_files
                raise
            self._current = AppliedConfig(
                bundle.config_revision,
                bundle.config_digest,
            )
            self._managed_files = tuple(
                sorted(
                    set(files)
                    | self._derived_managed_paths(files)
                )
            )
            self._persist()
            self._finish_apply()
            await self.sessions.invalidate_idle_for_config(
                bundle.config_revision
            )
            return self._current

    @staticmethod
    def _generation(revision: str) -> int | None:
        prefix = "generation-"
        bootstrap_prefix = "bootstrap-generation-"
        if revision.startswith(bootstrap_prefix):
            value = revision[len(bootstrap_prefix):]
        elif revision.startswith(prefix):
            value = revision[len(prefix):]
        else:
            return None
        if not value.isdigit():
            return None
        generation = int(value)
        return generation if generation > 0 else None

    def _reload_plan(
        self,
        files: Mapping[str, str],
    ) -> tuple[bool, bool]:
        removal_paths = _application_removal_paths(
            previous=self._managed_files,
            desired=files,
            home=self.home,
        )
        previous = _read_managed_files(
            self.home,
            excluded_paths=removal_paths,
        )
        paths = set(previous) | set(files)
        reload_skills = bool(removal_paths) or any(
            path == "skills" or path.startswith("skills/")
            for path in paths
            if previous.get(path) != files.get(path)
        )
        try:
            previous_config = yaml.safe_load(previous["config.yaml"])
            desired_config = yaml.safe_load(files["config.yaml"])
            if not isinstance(previous_config, dict) or not isinstance(
                desired_config,
                dict,
            ):
                raise ValueError("Runtime config root must be a mapping")
            reload_mcp = previous_config.get("mcp_servers") != (
                desired_config.get("mcp_servers")
            )
        except (KeyError, TypeError, ValueError, yaml.YAMLError):
            reload_mcp = True
        return reload_skills, reload_mcp

    async def close(self) -> None:
        if (
            self._owns_client
            and self._client is not None
            and not self._client.is_closed
        ):
            await self._client.aclose()

    async def _download(
        self,
        bundle: ConfigBundleReference,
    ) -> ConfigBundlePayload:
        base = urlparse(f"{self.backend_base_url}/")
        target_url = urljoin(
            f"{self.backend_base_url}/",
            bundle.download_url,
        )
        target = urlparse(target_url)
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or not target.path.startswith(
                "/internal/v1/config-bundles/"
            )
            or target.username
            or target.password
            or target.fragment
        ):
            raise RuntimeConfigError("CONFIG_BUNDLE_REFERENCE_FORBIDDEN")
        try:
            response = await self._shared_client().get(
                target_url,
                headers={
                    "Authorization": f"Bearer {self.api_server_key}",
                    "X-Hermes-Runtime-Id": self.runtime_id,
                    "X-Hermes-Binding-Id": self.binding_id,
                    "X-Hermes-Config-Revision": (
                        bundle.config_revision
                    ),
                    "X-Hermes-Config-Digest": bundle.config_digest,
                    "X-Hermes-Config-Signature": bundle.signature,
                },
            )
            if response.status_code in {409, 410, 422}:
                raise RuntimeConfigError(
                    "CONFIG_BUNDLE_REVISION_STALE"
                )
            response.raise_for_status()
            payload = ConfigBundlePayload.model_validate(response.json())
        except RuntimeConfigError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeConfigUnavailable(
                "CONFIG_BUNDLE_DOWNLOAD_FAILED"
            ) from exc
        if (
            payload.runtime_id != self.runtime_id
            or payload.binding_id != self.binding_id
            or payload.config_revision != bundle.config_revision
        ):
            raise RuntimeConfigError("CONFIG_BUNDLE_TARGET_MISMATCH")
        return payload

    def _initial_bundle(self) -> ConfigBundleReference:
        if not self.initial_download_url or not self.initial_digest:
            raise RuntimeConfigError(
                "CONFIG_BUNDLE_BASELINE_MISSING"
            )
        unsigned = ConfigBundleReference(
            runtime_id=self.runtime_id,
            binding_id=self.binding_id,
            config_revision=self.initial_revision,
            config_digest=self.initial_digest,
            download_url=self.initial_download_url,
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=5),
            signature="0" * 64,
        )
        return unsigned.model_copy(
            update={
                "signature": config_bundle_signature(
                    secret=self.api_server_key,
                    bundle=unsigned,
                )
            }
        )

    def _shared_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(
                    connect=5,
                    pool=1,
                    write=10,
                    read=30,
                ),
                limits=httpx.Limits(
                    max_connections=4,
                    max_keepalive_connections=2,
                    keepalive_expiry=30,
                ),
            )
        return self._client

    @property
    def _journal_path(self) -> Path:
        return self.state_path.with_name(
            "config-apply-journal.json"
        )

    @property
    def _backup_root(self) -> Path:
        return self.home / ".platform-config-backup"

    def _publish_files(
        self,
        files: Mapping[str, str],
        bundle: ConfigBundleReference,
    ) -> None:
        if self._journal_path.exists():
            raise RuntimeConfigError("CONFIG_APPLY_RECOVERY_REQUIRED")
        if self._backup_root.exists():
            shutil.rmtree(self._backup_root)
        self._backup_root.mkdir(parents=True, exist_ok=False)
        removal_paths = _application_removal_paths(
            previous=self._managed_files,
            desired=files,
            home=self.home,
        )
        old_files = tuple(
            relative
            for relative in self._managed_files
            if not _is_runtime_cache_path(relative)
            and not any(
                _is_relative_below(relative, removed)
                for removed in removal_paths
            )
        )
        new_files = tuple(
            sorted(
                relative
                for relative in set(files) | self._derived_managed_paths(files)
                if not _is_runtime_cache_path(relative)
            )
        )
        for relative in old_files:
            source = self._safe_home_path(relative)
            if source.is_symlink():
                raise RuntimeConfigError(
                    "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                )
            if source.is_file():
                backup = self._backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup)
        self._write_json_atomic(
            self._journal_path,
            {
                "binding_id": bundle.binding_id,
                "config_digest": bundle.config_digest,
                "config_revision": bundle.config_revision,
                "new_files": list(new_files),
                "old_files": list(old_files),
                "removed_directories": sorted(removal_paths),
                "runtime_id": bundle.runtime_id,
            },
        )
        try:
            for relative in sorted(removal_paths, reverse=True):
                source = self._safe_home_path(relative)
                if not source.exists() and not source.is_symlink():
                    continue
                backup = self._backup_root / "removed-paths" / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, backup)
                logger.warning(
                    "application Skill runtime mutation isolated",
                    extra={
                        "binding_id": bundle.binding_id,
                        "runtime_id": bundle.runtime_id,
                        "relative_path": relative,
                    },
                )
            for relative, content in sorted(files.items()):
                if _is_runtime_cache_path(relative):
                    continue
                self._write_text_atomic(
                    self._safe_home_path(relative),
                    content,
                    mode=(
                        _APPLICATION_SKILL_FILE_MODE
                        if relative.startswith("skills/")
                        else 0o660
                    ),
                )
            for relative in sorted(set(old_files) - set(new_files)):
                target = self._safe_home_path(relative)
                if target.is_symlink():
                    raise RuntimeConfigError(
                        "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                    )
                target.unlink(missing_ok=True)
            self._enforce_application_skill_readonly(files)
        except Exception:
            self._rollback_incomplete_apply()
            raise

    def _enforce_application_skill_readonly(
        self,
        files: Mapping[str, str],
    ) -> None:
        if not _is_application_removal_bundle(files):
            return
        application_root = self.home / "skills" / "application"
        try:
            if application_root.is_symlink():
                raise RuntimeConfigError(
                    "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                )
            if application_root.exists():
                application_root.chmod(_APPLICATION_SKILL_DIRECTORY_MODE)
            skill_roots = {
                root
                for relative in files
                if (root := _application_skill_root(relative)) is not None
            }
            for relative_root in skill_roots:
                root = self._safe_home_path(relative_root)
                if root.is_symlink() or not root.is_dir():
                    raise RuntimeConfigError(
                        "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                    )
                for directory in (root, *root.rglob("*")):
                    if directory.is_symlink():
                        raise RuntimeConfigError(
                            "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                        )
                    metadata = os.lstat(directory)
                    if metadata.st_uid != os.getuid():
                        logger.warning(
                            "application Skill runtime-owned path left unchanged",
                            extra={"relative_path": str(directory)},
                        )
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        directory.chmod(
                            _APPLICATION_SKILL_DIRECTORY_MODE
                        )
                    elif stat.S_ISREG(metadata.st_mode):
                        directory.chmod(_APPLICATION_SKILL_FILE_MODE)
        except OSError as exc:
            raise RuntimeConfigError(
                "CONFIG_SKILL_READ_ONLY_ENFORCEMENT_FAILED"
            ) from exc

    def _reclaim_application_runtime_mutations(
        self,
        files: Mapping[str, str],
    ) -> None:
        """Reclaim startup mutations without republishing an equal bundle."""
        if not _is_application_removal_bundle(files):
            return
        skill_roots = {
            root
            for relative in files
            if (root := _application_skill_root(relative)) is not None
        }
        quarantine_root = self.home / ".platform-runtime-quarantine"

        def quarantine(source: Path) -> None:
            relative = source.relative_to(self.home).as_posix()
            target = quarantine_root / relative / uuid4().hex
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            logger.warning(
                "application Skill runtime mutation quarantined",
                extra={"relative_path": relative},
            )

        for relative_root in (
            _live_application_skill_roots(self.home) - skill_roots
        ):
            source = self._safe_home_path(relative_root)
            if source.exists() or source.is_symlink():
                quarantine(source)
        for relative_root in skill_roots:
            root = self._safe_home_path(relative_root)
            if root.is_symlink() or not root.is_dir():
                continue
            try:
                root_metadata = os.lstat(root)
                if root_metadata.st_uid == os.getuid():
                    root.chmod(_APPLICATION_SKILL_DIRECTORY_MODE)
            except OSError as exc:
                raise RuntimeConfigError(
                    "CONFIG_SKILL_READ_ONLY_ENFORCEMENT_FAILED"
                ) from exc
            for name in _APPLICATION_RUNTIME_MUTATION_NAMES:
                source = root / name
                if not source.exists() and not source.is_symlink():
                    continue
                quarantine(source)

    def _rollback_incomplete_apply(self) -> None:
        if not self._journal_path.exists():
            return
        try:
            payload = json.loads(
                self._journal_path.read_text(encoding="utf-8")
            )
            old_files = {
                str(item)
                for item in payload.get("old_files", [])
                if not _is_runtime_cache_path(str(item))
            }
            new_files = {
                str(item)
                for item in payload.get("new_files", [])
                if not _is_runtime_cache_path(str(item))
            }
            removed_directories = {
                str(item)
                for item in payload.get("removed_directories", [])
            }
            for relative in sorted(new_files - old_files):
                self._safe_home_path(relative).unlink(missing_ok=True)
            for relative in sorted(old_files):
                backup = self._backup_root / relative
                target = self._safe_home_path(relative)
                if backup.is_file():
                    # A failed publish can leave a legacy child-owned
                    # directory unwritable to the Supervisor.  If the target
                    # already has the exact pre-publish bytes, replacing it
                    # is both unnecessary and can turn an otherwise complete
                    # rollback into a permanent startup failure.
                    if target.is_file() and self._same_file_contents(
                        backup, target
                    ):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            for relative in sorted(removed_directories, reverse=True):
                backup = self._backup_root / "removed-paths" / relative
                target = self._safe_home_path(relative)
                if backup.exists() or backup.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RuntimeConfigError,
        ) as exc:
            raise RuntimeConfigError(
                "CONFIG_APPLY_RECOVERY_FAILED"
            ) from exc
        self._finish_apply()

    def _recover_incomplete_apply(self) -> None:
        self._rollback_incomplete_apply()

    def _finish_apply(self) -> None:
        self._journal_path.unlink(missing_ok=True)
        if self._backup_root.exists():
            shutil.rmtree(self._backup_root)

    def _safe_home_path(self, relative: str) -> Path:
        validate_config_bundle_files(
            {
                "config.yaml": "",
                "SOUL.md": "",
                relative: "",
            }
        )
        target = self.home.joinpath(*PurePosixPath(relative).parts)
        try:
            target.relative_to(self.home)
        except ValueError as exc:
            raise RuntimeConfigError(
                "CONFIG_BUNDLE_PATH_FORBIDDEN"
            ) from exc
        parent = target.parent
        while parent != self.home:
            if parent.is_symlink():
                raise RuntimeConfigError(
                    "CONFIG_BUNDLE_SYMLINK_FORBIDDEN"
                )
            parent = parent.parent
        return target

    @staticmethod
    def _derived_managed_paths(
        files: Mapping[str, str],
    ) -> set[str]:
        del files
        return set()

    def _narrow_migrated_managed_paths(
        self,
        *,
        previous: set[str],
        authoritative: Mapping[str, str],
    ) -> set[str]:
        managed = {
            relative
            for relative in set(authoritative)
            | self._derived_managed_paths(authoritative)
            if not _is_runtime_cache_path(relative)
        }
        if "platform/application-agent.json" in authoritative:
            # Application Runtime generation replacement owns the previous
            # application subtree, but never a broad legacy Skill inventory.
            # The latter may contain Hermes-managed or user-created files
            # which were over-recorded by predecessor ConfigBundle state.
            managed.update(
                relative
                for relative in previous
                if not _is_runtime_cache_path(relative)
                if relative in _ALLOWED_ROOT_FILES
                or relative.startswith("memories/")
                or relative.startswith("platform/")
                or relative.startswith("skills/application/")
            )
        return managed

    @staticmethod
    def _same_file_contents(left: Path, right: Path) -> bool:
        """Compare regular files without exposing their contents in errors."""

        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(64 * 1024)
                right_chunk = right_handle.read(64 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    @staticmethod
    def _write_text_atomic(
        path: Path,
        content: str,
        *,
        mode: int = 0o660,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_metadata = os.stat(path.parent, follow_symlinks=False)
        temporary = path.with_name(f".{path.name}.config-tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fchown(handle.fileno(), os.getuid(), parent_metadata.st_gid)
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _write_json_atomic(
        path: Path,
        payload: Mapping[str, object],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _persist(self) -> None:
        payload = json.dumps(
            {
                "schema_version": CONFIG_BUNDLE_SCHEMA_VERSION,
                "config_revision": self._current.config_revision,
                "config_digest": self._current.config_digest,
                "managed_files": list(self._managed_files),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary = self.state_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
