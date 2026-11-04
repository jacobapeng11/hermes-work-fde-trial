from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.hermes_runtime.constants import (
    HERMES_HOME,
    HERMES_WORKSPACE,
    PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    RUNTIME_LOCAL_MODEL_RELAY_BASE_URL,
)
from app.hermes_runtime.projection import (
    DEFAULT_RUNTIME_SOUL,
    KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
    KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
    DESKTOP_LOCAL_PLUGIN_ID,
    desktop_local_plugin_artifacts,
    ProfileConfigRenderer,
    ProjectionInput,
    kanban_orchestrator_guard_artifacts,
)
from app.hermes_runtime.skill_visibility_policy import (
    BUNDLED_HERMES_SKILL_ROOT,
    INITIAL_DEFAULT_SKILL_POLICY_VERSION,
    OFFICIAL_HERMES_SKILL_ROOT,
    READONLY_HERMES_SKILL_ROOT,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
    explicitly_installed_skill_names,
    load_legacy_upstream_skill_names,
    resolve_versioned_skill_visibility,
)
from app.hermes_runtime.providers.knative import KnativeRuntimeSpec
from app.services.runtime_user_memory import (
    RuntimeUserMemoryWriter,
    merge_platform_soul,
)
from scripts.hermes_019_pvc_preflight import MARKER_RELATIVE_PATH, expected_marker, marker_matches


logger = logging.getLogger(__name__)
_RETIRED_SKILL_HOOK_COMMAND = str(
    Path(HERMES_HOME) / ".platform" / "hooks" / "skill-runtime-sync.py"
)
_RETIRED_SKILL_HOOK_SHA256 = "9f60813a8046f656addc74aa4813edcb59fb69ba397761522602025ada616350"
_COMMON_BASELINE_PLUGINS = (
    "enterprise-knowledge",
    "personal-knowledge",
    "clarify-lifecycle",
    PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
    DESKTOP_LOCAL_PLUGIN_ID,
)
_KANBAN_ORCHESTRATOR_PLUGINS = (
    "kanban-lifecycle",
    "expert-roster",
    KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
)
_PLATFORM_BASELINE_PLUGINS = (
    *_COMMON_BASELINE_PLUGINS,
    *_KANBAN_ORCHESTRATOR_PLUGINS,
)


class RuntimeConfigPreflightError(RuntimeError):
    """Raised before KService apply when PVC config is missing or unsafe."""


class RuntimeVisibleModelStore(Protocol):
    async def list_visible_models(self, *, org_id: int, user_id: int) -> list[Any]: ...


class LocalRuntimeConfigBootstrapper:
    """Create the initial PVC-backed Hermes config without storing secrets."""

    def __init__(
        self,
        renderer: ProfileConfigRenderer | None = None,
        user_memory_writer: RuntimeUserMemoryWriter | None = None,
        model_store: RuntimeVisibleModelStore | None = None,
        *,
        kanban_orchestrator_enabled: bool = True,
    ) -> None:
        self.renderer = renderer or ProfileConfigRenderer()
        self.user_memory_writer = user_memory_writer or RuntimeUserMemoryWriter()
        self.model_store = model_store
        self.kanban_orchestrator_enabled = kanban_orchestrator_enabled

    async def ensure_config(self, spec: KnativeRuntimeSpec) -> None:
        visible_models: tuple[dict[str, Any], ...] = ()
        if self.model_store is not None:
            records = await self.model_store.list_visible_models(
                org_id=spec.org_id,
                user_id=spec.user_id,
            )
            visible_models = tuple(
                {
                    "model_entry_id": int(record.model_entry_id),
                    "context_length": int(record.context_length),
                    "tags": list(record.tags or []),
                    "is_default": bool(getattr(record, "is_default", False)),
                }
                for record in records
            )
        await asyncio.to_thread(self._ensure_config, spec, visible_models)

    def _ensure_config(
        self,
        spec: KnativeRuntimeSpec,
        visible_models: tuple[dict[str, Any], ...] = (),
    ) -> None:
        if spec.config_schema not in ProfileConfigRenderer.SUPPORTED_CONFIG_VERSIONS:
            raise RuntimeConfigPreflightError(
                "Runtime Release config schema does not match the platform "
                f"renderer: unsupported schema {spec.config_schema}"
            )
        home = Path(spec.runtime_root_host_path) / spec.hermes_home_sub_path
        home.mkdir(parents=True, exist_ok=True)
        self._remove_retired_skill_hook_artifacts(home)
        self.user_memory_writer.ensure(
            home,
            uid=spec.runtime_user_uid,
            gid=spec.runtime_shared_gid,
        )
        self._atomic_replace(
            home / "SOUL.md",
            merge_platform_soul(
                DEFAULT_RUNTIME_SOUL,
                name=spec.user_name,
                user_code=spec.user_code,
            ),
            spec,
        )
        if self.kanban_orchestrator_enabled:
            self._ensure_kanban_orchestrator_guard(home, spec)
        self._ensure_desktop_local_plugin(home, spec)
        config_path = home / "config.yaml"
        if config_path.exists():
            self._normalize_runtime_config(config_path, spec, visible_models)
            self._remove_retired_skill_hook_config(config_path, spec)
            self._normalize_managed_config_metadata(config_path, spec)
            self._validate_existing(config_path, spec)
            self._require_runtime_migration_marker(home, spec)
            return

        payload = ProjectionInput(
            org_id=spec.org_id,
            user_id=spec.user_id,
            runtime_workspace_id=spec.runtime_workspace_id,
            runtime_id=spec.runtime_id,
            config_generation=0,
            # The baseline provider is local and fail-closed. Before a Turn,
            # SessionModelRouter installs the exact per-Session relay route.
            model_relay_base_url=RUNTIME_LOCAL_MODEL_RELAY_BASE_URL,
            hermes_home=home,
            workspace_dir=Path(HERMES_WORKSPACE),
            name=spec.runtime_id,
            default_model_id=spec.default_model_entry_id,
            visible_models=visible_models,
            skill_policy_version=INITIAL_DEFAULT_SKILL_POLICY_VERSION,
        )
        try:
            rendered = self.renderer.render_config(
                payload,
                config_version=spec.config_schema,
            )
            document = yaml.safe_load(rendered)
            self._normalize_kanban_role(document)
            self._normalize_kanban_plugins(document)
            self._validate_document(
                document,
                source="generated config",
                kanban_orchestrator_enabled=(self.kanban_orchestrator_enabled),
                expected_config_version=spec.config_schema,
            )
            rendered = self.renderer.dump_document(document)
        except (ValueError, yaml.YAMLError) as exc:
            raise RuntimeConfigPreflightError(f"cannot render PVC config: {exc}") from exc
        if not self._atomic_write(config_path, rendered, spec):
            # Another warmup may have published a complete baseline after our
            # initial existence check. Preserve it and validate its contract.
            self._normalize_runtime_config(config_path, spec, visible_models)
        self._remove_retired_skill_hook_config(config_path, spec)
        self._normalize_managed_config_metadata(config_path, spec)
        self._validate_existing(config_path, spec)
        self._require_runtime_migration_marker(home, spec)

    @staticmethod
    def _normalize_managed_config_metadata(
        path: Path,
        spec: KnativeRuntimeSpec,
    ) -> None:
        try:
            os.chown(
                path,
                spec.runtime_user_uid,
                spec.runtime_shared_gid,
            )
            os.chmod(path, 0o660)
        except OSError as exc:
            raise RuntimeConfigPreflightError(
                "cannot normalize Runtime config ownership and permissions"
            ) from exc

    def _remove_retired_skill_hook_config(self, path: Path, spec: KnativeRuntimeSpec) -> None:
        """Remove only the exact platform-managed Hook entry from user config."""
        try:
            contents = path.read_text(encoding="utf-8")
            document = yaml.safe_load(contents)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RuntimeConfigPreflightError(
                "cannot read Runtime config for retired Skill hook cleanup"
            ) from exc
        if not isinstance(document, dict):
            raise RuntimeConfigPreflightError(
                "Runtime config for retired Skill hook cleanup must be a YAML mapping"
            )
        hooks = document.get("hooks")
        if hooks is None:
            return
        if not isinstance(hooks, dict):
            raise RuntimeConfigPreflightError("Runtime config hooks must be a YAML mapping")
        post = hooks.get("post_tool_call")
        if post is None:
            return
        if not isinstance(post, list):
            raise RuntimeConfigPreflightError("Runtime config hooks.post_tool_call must be a list")
        retained = [
            item
            for item in post
            if not (
                isinstance(item, dict)
                and item.get("matcher") == "skill_manage"
                and item.get("command") == _RETIRED_SKILL_HOOK_COMMAND
            )
        ]
        if len(retained) == len(post):
            return
        hooks["post_tool_call"] = retained
        self._atomic_replace(path, self.renderer.dump_document(document), spec)

    @staticmethod
    def _remove_retired_skill_hook_artifacts(home: Path) -> None:
        """Delete only a byte-for-byte platform script and its exact approval."""
        script = home / ".platform" / "hooks" / "skill-runtime-sync.py"
        try:
            if script.exists():
                digest = hashlib.sha256(script.read_bytes()).hexdigest()
                if digest == _RETIRED_SKILL_HOOK_SHA256:
                    script.unlink()
                else:
                    logger.warning(
                        "retired platform Skill hook script was modified; leaving file in place path=%s",
                        script,
                    )
        except OSError:
            logger.warning("cannot inspect retired platform Skill hook script path=%s", script)

        target = home / "shell-hooks-allowlist.json"
        try:
            if not target.exists():
                return
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("cannot inspect retired platform Skill hook allowlist path=%s", target)
            return
        if not isinstance(document, dict):
            return
        approvals = document.get("approvals")
        if not isinstance(approvals, list):
            return
        retained = [
            item
            for item in approvals
            if not (
                isinstance(item, dict)
                and item.get("event") == "post_tool_call"
                and item.get("command") == _RETIRED_SKILL_HOOK_COMMAND
            )
        ]
        if len(retained) == len(approvals):
            return
        document["approvals"] = retained
        try:
            target.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            logger.warning("cannot remove retired platform Skill hook approval path=%s", target)

    def _normalize_runtime_config(
        self,
        path: Path,
        spec: KnativeRuntimeSpec,
        visible_models: tuple[dict[str, Any], ...],
    ) -> None:
        try:
            contents = path.read_text(encoding="utf-8")
            document = yaml.safe_load(contents)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed for {path}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            return
        changed = False
        if self._normalize_schema_invariants(
            document,
            expected_config_version=spec.config_schema,
        ):
            changed = True
        if "api_server" in document:
            document.pop("api_server")
            changed = True
        agent = document.get("agent")
        if agent is None:
            agent = {}
            document["agent"] = agent
            changed = True
        if isinstance(agent, dict):
            disabled_toolsets = agent.get("disabled_toolsets")
            if disabled_toolsets is None:
                agent["disabled_toolsets"] = ["cronjob"]
                changed = True
            elif (
                isinstance(disabled_toolsets, list)
                and all(isinstance(item, str) for item in disabled_toolsets)
                and "cronjob" not in disabled_toolsets
            ):
                disabled_toolsets.append("cronjob")
                changed = True
        if self._normalize_kanban_role(document):
            changed = True
        if self._normalize_kanban_plugins(document):
            changed = True
        # The PVC baseline may predate platform-managed compaction tuning
        # (e.g. application Runtime config.yaml created before the section
        # existed). Converge it to the renderer's authoritative values on the
        # same managed-section pattern as skills/plugins above; without this,
        # bootstrapping an existing config skips re-rendering entirely and
        # old Runtimes silently keep Hermes defaults.
        if document.get("compression") != ProfileConfigRenderer.COMPRESSION_CONFIG:
            document["compression"] = dict(ProfileConfigRenderer.COMPRESSION_CONFIG)
            changed = True
        skills = document.get("skills")
        if skills is None:
            skills = {}
            document["skills"] = skills
            changed = True
        if not isinstance(skills, dict):
            raise RuntimeConfigPreflightError("Runtime config skills must be a YAML mapping")
        if skills.get("creation_nudge_interval") != 0:
            skills["creation_nudge_interval"] = 0
            changed = True
        external_dirs = skills.get("external_dirs", [])
        if not (
            isinstance(external_dirs, list)
            and all(isinstance(item, str) and item.strip() for item in external_dirs)
        ):
            raise RuntimeConfigPreflightError(
                "Runtime config skills.external_dirs must be a string list"
            )
        expected_external_dirs = list(
            dict.fromkeys(
                [
                    OFFICIAL_HERMES_SKILL_ROOT,
                    *(item.strip() for item in external_dirs),
                ]
            )
        )
        if BUNDLED_HERMES_SKILL_ROOT in expected_external_dirs:
            expected_external_dirs = list(
                dict.fromkeys(
                    [
                        *expected_external_dirs,
                        READONLY_HERMES_SKILL_ROOT,
                    ]
                )
            )
        if skills.get("external_dirs") != expected_external_dirs:
            skills["external_dirs"] = expected_external_dirs
            changed = True
        disabled_skills = skills.get("disabled", [])
        if not (
            isinstance(disabled_skills, list)
            and all(isinstance(item, str) for item in disabled_skills)
        ):
            raise RuntimeConfigPreflightError(
                "Runtime config skills.disabled must be a string list"
            )
        platform = document.get("platform")
        if not isinstance(platform, dict):
            raise RuntimeConfigPreflightError("Runtime config platform must be a YAML mapping")
        previous_visibility = platform.get("skill_visibility")
        previous_platform_disabled = (
            previous_visibility.get("platform_disabled")
            if isinstance(previous_visibility, dict)
            else []
        )
        if not (
            isinstance(previous_platform_disabled, list)
            and all(isinstance(item, str) for item in previous_platform_disabled)
        ):
            raise RuntimeConfigPreflightError("Runtime config platform Skill visibility is invalid")
        previous_user_disabled = (
            previous_visibility.get("user_disabled")
            if isinstance(previous_visibility, dict)
            else None
        )
        if previous_user_disabled is not None and not (
            isinstance(previous_user_disabled, list)
            and all(isinstance(item, str) for item in previous_user_disabled)
        ):
            raise RuntimeConfigPreflightError(
                "Runtime config user-disabled Skill visibility is invalid"
            )
        user_disabled = (
            set(previous_user_disabled)
            if previous_user_disabled is not None
            else set(disabled_skills) - set(previous_platform_disabled)
        )
        declared_policy_version = (
            previous_visibility.get("policy_version")
            if isinstance(previous_visibility, dict)
            else None
        )
        policy_version = (
            declared_policy_version
            if declared_policy_version in {
                SKILL_VISIBILITY_POLICY_V1,
                SKILL_VISIBILITY_POLICY_V2,
                SKILL_VISIBILITY_POLICY_V3,
            }
            else (
                SKILL_VISIBILITY_POLICY_V2
                if spec.config_schema >= ProfileConfigRenderer.TARGET_CONFIG_VERSION
                else SKILL_VISIBILITY_POLICY_V1
            )
        )
        installed_skills = self._installed_skill_records(path.parent)
        visibility = resolve_versioned_skill_visibility(
            policy_version=policy_version,
            legacy_upstream_names=load_legacy_upstream_skill_names(),
            explicitly_installed_names=explicitly_installed_skill_names(installed_skills),
            user_disabled_names=user_disabled,
        )
        expected_disabled = sorted(visibility.effective_disabled)
        expected_platform_disabled = sorted(visibility.platform_disabled)
        if skills.get("disabled") != expected_disabled:
            skills["disabled"] = expected_disabled
            changed = True
        expected_visibility: dict[str, Any] = {
            "policy_version": policy_version,
            "platform_disabled": expected_platform_disabled,
        }
        initial_policy_version = (
            previous_visibility.get("initial_policy_version")
            if isinstance(previous_visibility, dict)
            else None
        )
        if initial_policy_version in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            expected_visibility["initial_policy_version"] = initial_policy_version
        if policy_version >= SKILL_VISIBILITY_POLICY_V2:
            expected_visibility["user_disabled"] = sorted(user_disabled)
        if platform.get("skill_visibility") != expected_visibility:
            platform["skill_visibility"] = expected_visibility
            changed = True
        toolsets = document.get("toolsets")
        if toolsets is not None and not (
            isinstance(toolsets, list) and all(isinstance(item, str) for item in toolsets)
        ):
            raise RuntimeConfigPreflightError("Runtime config toolsets must be a string list")
        plugins = document.get("plugins")
        if not isinstance(plugins, dict):
            raise RuntimeConfigPreflightError("Runtime config plugins must be a YAML mapping")
        enabled_plugins = plugins.get("enabled")
        if not (
            isinstance(enabled_plugins, list)
            and all(isinstance(item, str) for item in enabled_plugins)
        ):
            raise RuntimeConfigPreflightError(
                "Runtime config plugins.enabled must be a string list"
            )
        model = document.get("model")
        providers = document.get("providers")
        provider = providers.get("platform") if isinstance(providers, dict) else None
        if not isinstance(model, dict) or not isinstance(provider, dict):
            if changed:
                self._atomic_replace(
                    path,
                    self.renderer.dump_document(document),
                    spec,
                )
            return
        existing_default = str(model.get("default") or "").strip()
        existing_default_id = (
            int(existing_default)
            if existing_default.isdigit() and int(existing_default) > 0
            else None
        )
        default_model, models = self.renderer.resolve_provider_models(
            default_model_id=spec.default_model_entry_id or existing_default_id,
            visible_models=visible_models,
        )
        expected_model_entry_id = None if default_model == "session" else int(default_model)
        model_metadata_current = (
            model.get("default") == default_model
            and model.get("default_model_entry_id") == expected_model_entry_id
            and model.get("ollama_num_ctx") == 0
            and provider.get("default_model") == default_model
            and provider.get("discover_models") is False
            and provider.get("models") == models
        )
        if model_metadata_current and not changed:
            return
        if not model_metadata_current:
            model["default"] = default_model
            if default_model == "session":
                model.pop("default_model_entry_id", None)
            else:
                model["default_model_entry_id"] = int(default_model)
            model["ollama_num_ctx"] = 0
            provider["default_model"] = default_model
            provider["discover_models"] = False
            provider["models"] = models
        rendered = self.renderer.dump_document(document)
        if rendered != contents:
            self._atomic_replace(path, rendered, spec)

    @staticmethod
    def _installed_skill_records(home: Path) -> list[dict[str, Any]]:
        path = home / "platform" / "installed-skills.json"
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeConfigPreflightError(
                f"Runtime installed Skill inventory is invalid: {path}"
            ) from exc
        if not (isinstance(payload, list) and all(isinstance(item, dict) for item in payload)):
            raise RuntimeConfigPreflightError(
                f"Runtime installed Skill inventory is invalid: {path}"
            )
        return payload

    @staticmethod
    def _normalize_schema_invariants(
        document: dict[str, Any],
        *,
        expected_config_version: int,
    ) -> bool:
        actual = document.get("_config_version")
        if expected_config_version == ProfileConfigRenderer.CONFIG_VERSION:
            if actual != expected_config_version:
                raise RuntimeConfigPreflightError(
                    "Runtime config schema cannot be rewritten outside a "
                    "signed forward migration"
                )
            return False
        if expected_config_version != ProfileConfigRenderer.TARGET_CONFIG_VERSION:
            raise RuntimeConfigPreflightError(
                f"unsupported Runtime config schema {expected_config_version}"
            )
        if actual not in {
            ProfileConfigRenderer.CONFIG_VERSION,
            ProfileConfigRenderer.TARGET_CONFIG_VERSION,
        }:
            raise RuntimeConfigPreflightError(
                "Runtime config source schema is not an allowed 33->34 input"
            )

        sessions = document.get("sessions")
        if sessions is None:
            sessions = {}
            document["sessions"] = sessions
        if not isinstance(sessions, dict):
            raise RuntimeConfigPreflightError(
                "Runtime config sessions must be a YAML mapping"
            )
        declared_cjk = sessions.get("cjk_fts", True)
        if not isinstance(declared_cjk, bool):
            raise RuntimeConfigPreflightError(
                "Runtime config sessions.cjk_fts must be boolean"
            )

        changed = actual != expected_config_version
        if changed:
            document["_config_version"] = expected_config_version
        if sessions.get("cjk_fts") is not declared_cjk:
            sessions["cjk_fts"] = declared_cjk
            changed = True

        display = document.get("display")
        if display is None:
            display = {}
            document["display"] = display
            changed = True
        if not isinstance(display, dict):
            raise RuntimeConfigPreflightError(
                "Runtime config display must be a YAML mapping"
            )
        if display.get("busy_input_mode") != "queue":
            display["busy_input_mode"] = "queue"
            changed = True

        desktop = document.get("desktop")
        if desktop is None:
            desktop = {}
            document["desktop"] = desktop
            changed = True
        if not isinstance(desktop, dict):
            raise RuntimeConfigPreflightError(
                "Runtime config desktop must be a YAML mapping"
            )
        auto_continue = desktop.get("auto_continue")
        if auto_continue is None:
            auto_continue = {}
            desktop["auto_continue"] = auto_continue
            changed = True
        if not isinstance(auto_continue, dict):
            raise RuntimeConfigPreflightError(
                "Runtime config desktop.auto_continue must be a YAML mapping"
            )
        if auto_continue.get("enabled") is not False:
            auto_continue["enabled"] = False
            changed = True
        return changed

    @classmethod
    def _require_runtime_migration_marker(cls, home: Path, spec: KnativeRuntimeSpec) -> None:
        state_artifacts = (home / "state.db", home / "state.db-wal", home / "state.db-shm")
        if not any(path.exists() for path in state_artifacts):
            cls._publish_fresh_state_marker(home, spec)
            return
        if marker_matches(
            home,
            config_schema=spec.config_schema,
            state_schema=spec.state_schema,
            image_commit=spec.upstream_commit,
        ):
            return
        raise RuntimeConfigPreflightError(
            "Hermes runtime migration marker is missing or does not match "
            f"state schema {spec.state_schema}, "
            f"config schema {spec.config_schema}, commit {spec.upstream_commit}"
        )

    @classmethod
    def _publish_fresh_state_marker(cls, home: Path, spec: KnativeRuntimeSpec) -> None:
        marker_path = home / MARKER_RELATIVE_PATH
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        os.chown(marker_path.parent, spec.runtime_user_uid, spec.runtime_shared_gid)
        marker_path.parent.chmod(0o770)
        document = expected_marker(
            config_schema=spec.config_schema,
            state_schema=spec.state_schema,
            image_commit=spec.upstream_commit,
        )
        document.update(
            {
                "mode": "fresh",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if cls._atomic_write(marker_path, json.dumps(document, sort_keys=True) + "\n", spec):
            return
        if not marker_matches(
            home,
            config_schema=spec.config_schema,
            state_schema=spec.state_schema,
            image_commit=spec.upstream_commit,
        ):
            raise RuntimeConfigPreflightError(
                "fresh Hermes state marker conflicts with release schema"
            )

    def _validate_existing(self, path: Path, spec: KnativeRuntimeSpec) -> None:
        try:
            contents = path.read_text(encoding="utf-8")
            document = yaml.safe_load(contents)
            self._validate_document(
                document,
                source=str(path),
                kanban_orchestrator_enabled=(self.kanban_orchestrator_enabled),
                expected_config_version=spec.config_schema,
            )
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed for {path}: {exc}"
            ) from exc
        mode = stat.S_IMODE(path.stat().st_mode)
        runtime_readable = self._can_read(
            path,
            uid=spec.runtime_user_uid,
            gid=spec.runtime_shared_gid,
            mode=mode,
        )
        if not runtime_readable:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: runtime cannot read {path}"
            )

    @staticmethod
    def _can_read(path: Path, *, uid: int, gid: int, mode: int | None = None) -> bool:
        mode = stat.S_IMODE(path.stat().st_mode) if mode is None else mode
        metadata = path.stat()
        return bool(
            (metadata.st_uid == uid and mode & stat.S_IRUSR)
            or (metadata.st_gid == gid and mode & stat.S_IRGRP)
            or (mode & stat.S_IROTH)
        )

    @classmethod
    def _validate_document(
        cls,
        document: Any,
        *,
        source: str,
        kanban_orchestrator_enabled: bool = True,
        expected_config_version: int = ProfileConfigRenderer.CONFIG_VERSION,
    ) -> None:
        if not isinstance(document, dict) or not document:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source} must be a YAML mapping"
            )
        if document.get("_config_version") != expected_config_version:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source} must use schema "
                f"{expected_config_version}"
            )
        if expected_config_version >= ProfileConfigRenderer.TARGET_CONFIG_VERSION:
            sessions = document.get("sessions")
            display = document.get("display")
            desktop = document.get("desktop")
            auto_continue = (
                desktop.get("auto_continue") if isinstance(desktop, dict) else None
            )
            if (
                not isinstance(sessions, dict)
                or not isinstance(sessions.get("cjk_fts"), bool)
            ):
                raise RuntimeConfigPreflightError(
                    f"PVC config preflight failed: {source}.sessions.cjk_fts "
                    "must be boolean"
                )
            if (
                not isinstance(display, dict)
                or display.get("busy_input_mode") != "queue"
            ):
                raise RuntimeConfigPreflightError(
                    f"PVC config preflight failed: {source}.display.busy_input_mode "
                    "must be queue"
                )
            if (
                not isinstance(auto_continue, dict)
                or auto_continue.get("enabled") is not False
            ):
                raise RuntimeConfigPreflightError(
                    f"PVC config preflight failed: {source}.desktop.auto_continue.enabled "
                    "must be false"
                )
        required = {
            "agent",
            "toolsets",
            "model",
            "providers",
            "terminal",
            "workspace_dir",
            "platform",
            "mcp_servers",
            "skills",
        }
        missing = sorted(required - document.keys())
        if missing:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source} is missing {', '.join(missing)}"
            )
        if "api_server" in document:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.api_server is retired"
            )
        agent = document["agent"]
        disabled_toolsets = agent.get("disabled_toolsets") if isinstance(agent, dict) else None
        if (
            not isinstance(disabled_toolsets, list)
            or not all(isinstance(item, str) for item in disabled_toolsets)
            or "cronjob" not in disabled_toolsets
        ):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.agent must disable cronjob"
            )
        if kanban_orchestrator_enabled:
            if (
                not isinstance(agent, dict)
                or agent.get("system_prompt") != KANBAN_ORCHESTRATOR_SYSTEM_PROMPT
            ):
                raise RuntimeConfigPreflightError(
                    f"PVC config preflight failed: {source}.agent.system_prompt "
                    "must use the platform Kanban orchestrator contract"
                )
        elif isinstance(agent, dict) and "system_prompt" in agent:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.agent.system_prompt "
                "is not allowed for this Runtime role"
            )
        toolsets = document["toolsets"]
        if not isinstance(toolsets, list) or not all(isinstance(item, str) for item in toolsets):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.toolsets must be a string list"
            )
        skills = document["skills"]
        if not isinstance(skills, dict):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.skills must be a mapping"
            )
        if skills.get("creation_nudge_interval") != 0:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.skills.creation_nudge_interval must be 0"
            )
        disabled_skills = skills.get("disabled") if isinstance(skills, dict) else None
        if not (
            isinstance(disabled_skills, list)
            and all(isinstance(item, str) for item in disabled_skills)
        ):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.skills.disabled must be a string list"
            )
        if kanban_orchestrator_enabled and "kanban" not in toolsets:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.toolsets must include kanban"
            )
        if not kanban_orchestrator_enabled and "kanban" in toolsets:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.toolsets must exclude kanban"
            )
        plugins = document.get("plugins")
        enabled_plugins = plugins.get("enabled") if isinstance(plugins, dict) else None
        if (
            not isinstance(enabled_plugins, list)
            or not all(isinstance(item, str) for item in enabled_plugins)
            or any(
                item not in enabled_plugins
                for item in (
                    _PLATFORM_BASELINE_PLUGINS
                    if kanban_orchestrator_enabled
                    else _COMMON_BASELINE_PLUGINS
                )
            )
        ):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.plugins.enabled "
                "must include all platform baseline plugins"
            )
        if not kanban_orchestrator_enabled and any(
            item in enabled_plugins for item in _KANBAN_ORCHESTRATOR_PLUGINS
        ):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.plugins.enabled "
                "must exclude Kanban orchestrator plugins"
            )
        if not isinstance(document["workspace_dir"], str) or not document["workspace_dir"]:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.workspace_dir must be a non-empty string"
            )
        model = document["model"]
        if (
            not isinstance(model, dict)
            or model.get("provider") != "custom:platform"
            or not isinstance(model.get("default"), str)
            or not model["default"].strip()
        ):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.model.default must be a string for custom:platform"
            )
        terminal = document["terminal"]
        if not isinstance(terminal, dict) or terminal.get("cwd") != document["workspace_dir"]:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.terminal.cwd must match workspace_dir"
            )
        providers = document["providers"]
        provider = providers.get("platform") if isinstance(providers, dict) else None
        if not isinstance(provider, dict):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.providers.platform must be a mapping"
            )
        required_provider_strings = ("base_url", "key_env", "transport", "default_model")
        invalid_provider_fields = [
            field
            for field in required_provider_strings
            if not isinstance(provider.get(field), str) or not provider[field].strip()
        ]
        if invalid_provider_fields:
            raise RuntimeConfigPreflightError(
                "PVC config preflight failed: "
                f"{source}.providers.platform.{invalid_provider_fields[0]} must be a non-empty string"
            )
        if provider["default_model"] != model["default"]:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.providers.platform.default_model must match model.default"
            )
        if model.get("ollama_num_ctx") != 0:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.model.ollama_num_ctx must be 0"
            )
        if provider.get("discover_models") is not False:
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.providers.platform.discover_models must be false"
            )
        provider_models = provider.get("models")
        if not isinstance(provider_models, dict):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.providers.platform.models must be a mapping"
            )
        for model_id, metadata in provider_models.items():
            context_length = metadata.get("context_length") if isinstance(metadata, dict) else None
            supports_vision = (
                metadata.get("supports_vision") if isinstance(metadata, dict) else None
            )
            if (
                not isinstance(model_id, str)
                or not model_id.isdigit()
                or isinstance(context_length, bool)
                or not isinstance(context_length, int)
                or context_length <= 0
                or not isinstance(supports_vision, bool)
            ):
                raise RuntimeConfigPreflightError(
                    f"PVC config preflight failed: {source}.providers.platform.models is invalid"
                )
        platform = document["platform"]
        if not isinstance(platform, dict) or not {
            "runtime_workspace_id",
            "runtime_id",
            "config_generation",
        }.issubset(platform):
            raise RuntimeConfigPreflightError(
                f"PVC config preflight failed: {source}.platform is incomplete"
            )
        cls._reject_secret_fields(document, path=source)
        mcp_servers = document.get("mcp_servers")
        if mcp_servers is not None:
            if not isinstance(mcp_servers, dict):
                raise RuntimeConfigPreflightError(
                    f"PVC config preflight failed: {source}.mcp_servers must be a mapping"
                )
            for slug, entry in mcp_servers.items():
                if not isinstance(entry, dict):
                    raise RuntimeConfigPreflightError(
                        f"PVC config preflight failed: {source}.mcp_servers.{slug} must be a mapping"
                    )
                try:
                    ProfileConfigRenderer.validate_mcp_entry(
                        entry, path=f"{source}.mcp_servers.{slug}"
                    )
                except ValueError as exc:
                    raise RuntimeConfigPreflightError(str(exc)) from exc

    def _ensure_kanban_orchestrator_guard(
        self,
        home: Path,
        spec: KnativeRuntimeSpec,
    ) -> None:
        plugin_root = home / "plugins" / KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID
        for directory in (plugin_root.parent, plugin_root):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chown(
                    directory,
                    spec.runtime_user_uid,
                    spec.runtime_shared_gid,
                )
                os.chmod(directory, 0o770)
            except OSError as exc:
                raise RuntimeConfigPreflightError(
                    "cannot prepare platform Kanban orchestrator guard directory"
                ) from exc
        for relative, content in kanban_orchestrator_guard_artifacts().items():
            target = home / relative
            try:
                current = target.read_text(encoding="utf-8") if target.exists() else None
                if current != content:
                    self._atomic_replace(target, content, spec)
                else:
                    os.chown(target, spec.runtime_user_uid, spec.runtime_shared_gid)
                    os.chmod(target, 0o660)
                if target.read_text(encoding="utf-8") != content:
                    raise RuntimeConfigPreflightError(
                        "platform Kanban orchestrator guard verification failed"
                    )
            except (OSError, UnicodeError) as exc:
                raise RuntimeConfigPreflightError(
                    "cannot verify platform Kanban orchestrator guard"
                ) from exc

    def _ensure_desktop_local_plugin(
        self,
        home: Path,
        spec: KnativeRuntimeSpec,
    ) -> None:
        plugin_root = home / "plugins" / DESKTOP_LOCAL_PLUGIN_ID
        for directory in (plugin_root.parent, plugin_root):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                os.chown(directory, spec.runtime_user_uid, spec.runtime_shared_gid)
                os.chmod(directory, 0o770)
            except OSError as exc:
                raise RuntimeConfigPreflightError(
                    "cannot prepare platform desktop local plugin directory"
                ) from exc
        for relative, content in desktop_local_plugin_artifacts().items():
            target = home / relative
            try:
                current = target.read_text(encoding="utf-8") if target.exists() else None
                if current != content:
                    self._atomic_replace(target, content, spec)
                else:
                    os.chown(target, spec.runtime_user_uid, spec.runtime_shared_gid)
                    os.chmod(target, 0o660)
                if target.read_text(encoding="utf-8") != content:
                    raise RuntimeConfigPreflightError(
                        "platform desktop local plugin verification failed"
                    )
            except (OSError, UnicodeError) as exc:
                raise RuntimeConfigPreflightError(
                    "cannot verify platform desktop local plugin"
                ) from exc
    def _required_baseline_plugins(self) -> tuple[str, ...]:
        if self.kanban_orchestrator_enabled:
            return _PLATFORM_BASELINE_PLUGINS
        return _COMMON_BASELINE_PLUGINS

    def _normalize_kanban_role(self, document: Any) -> bool:
        if not isinstance(document, dict):
            return False
        changed = False
        agent = document.get("agent")
        if isinstance(agent, dict):
            if self.kanban_orchestrator_enabled:
                if agent.get("system_prompt") != KANBAN_ORCHESTRATOR_SYSTEM_PROMPT:
                    agent["system_prompt"] = KANBAN_ORCHESTRATOR_SYSTEM_PROMPT
                    changed = True
            elif "system_prompt" in agent:
                agent.pop("system_prompt")
                changed = True
        toolsets = document.get("toolsets")
        if toolsets is None:
            document["toolsets"] = ["kanban"] if self.kanban_orchestrator_enabled else []
            changed = True
        elif isinstance(toolsets, list):
            if self.kanban_orchestrator_enabled and "kanban" not in toolsets:
                toolsets.append("kanban")
                changed = True
            elif not self.kanban_orchestrator_enabled and "kanban" in toolsets:
                document["toolsets"] = [item for item in toolsets if item != "kanban"]
                changed = True
        kanban = document.get("kanban")
        if self.kanban_orchestrator_enabled:
            if not isinstance(kanban, dict):
                document["kanban"] = {"max_in_progress_per_profile": 1}
                changed = True
            elif kanban.get("max_in_progress_per_profile") != 1:
                kanban["max_in_progress_per_profile"] = 1
                changed = True
        return changed

    def _normalize_kanban_plugins(self, document: Any) -> bool:
        if not isinstance(document, dict):
            return False
        changed = False
        plugins = document.get("plugins")
        if plugins is None:
            document["plugins"] = {"enabled": list(self._required_baseline_plugins())}
            return True
        if not isinstance(plugins, dict):
            return False
        enabled_plugins = plugins.get("enabled")
        if enabled_plugins is None:
            plugins["enabled"] = list(self._required_baseline_plugins())
            return True
        if not (
            isinstance(enabled_plugins, list)
            and all(isinstance(item, str) for item in enabled_plugins)
        ):
            return False
        for plugin_id in self._required_baseline_plugins():
            if plugin_id not in enabled_plugins:
                enabled_plugins.append(plugin_id)
                changed = True
        if not self.kanban_orchestrator_enabled:
            retained_plugins = [
                plugin_id
                for plugin_id in enabled_plugins
                if plugin_id not in _KANBAN_ORCHESTRATOR_PLUGINS
            ]
            if retained_plugins != enabled_plugins:
                plugins["enabled"] = retained_plugins
                changed = True
        return changed

    @classmethod
    def _reject_secret_fields(cls, value: Any, *, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).lower().replace("-", "_")
                # Environment names are identifiers. Their values still must
                # be placeholders when they occur in an MCP env/headers map.
                env_map = path.endswith(".env") or path.endswith(".headers")
                if normalized in ProfileConfigRenderer.SECRETISH_FIELDS and not env_map:
                    raise RuntimeConfigPreflightError(
                        f"PVC config contains secret field: {path}.{key}"
                    )
                if (
                    env_map
                    and isinstance(child, str)
                    and not ProfileConfigRenderer.ENV_PLACEHOLDER_RE.fullmatch(child)
                ):
                    raise RuntimeConfigPreflightError(
                        f"PVC config contains plaintext credential: {path}.{key}"
                    )
                cls._reject_secret_fields(child, path=f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                cls._reject_secret_fields(child, path=f"{path}[{index}]")

    @staticmethod
    def _atomic_replace(
        path: Path,
        content: str,
        spec: KnativeRuntimeSpec,
    ) -> None:
        descriptor: int | None = None
        temp_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temp_path = Path(raw_path)
            os.fchmod(descriptor, 0o660)
            os.fchown(
                descriptor,
                spec.runtime_user_uid,
                spec.runtime_shared_gid,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            temp_path = None
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise RuntimeConfigPreflightError("cannot update platform persona") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _atomic_write(path: Path, content: str, spec: KnativeRuntimeSpec) -> bool:
        descriptor: int | None = None
        temp_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".config.", suffix=".tmp", dir=path.parent
            )
            temp_path = Path(raw_path)
            os.fchmod(descriptor, 0o660)
            try:
                os.fchown(descriptor, spec.runtime_user_uid, spec.runtime_shared_gid)
            except PermissionError as exc:
                raise RuntimeConfigPreflightError(
                    f"cannot assign runtime config ownership {spec.runtime_user_uid}:{spec.runtime_shared_gid}"
                ) from exc
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # A hard link is an atomic no-clobber publication when both
                # paths are in the same directory. ``os.replace`` would allow
                # a concurrent bootstrap to overwrite a valid config.
                os.link(temp_path, path)
            except FileExistsError:
                return False
            temp_path.unlink()
            temp_path = None
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return True
        except OSError as exc:
            raise RuntimeConfigPreflightError(f"cannot atomically write {path}: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
