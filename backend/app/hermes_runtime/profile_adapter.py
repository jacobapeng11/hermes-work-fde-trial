from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import yaml

from app.hermes_runtime.toolset_policy import build_platform_cli_toolsets

from app.hermes_runtime.constants import (
    HERMES_WORKSPACE,
    PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
)
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint
from app.hermes_runtime.skill_visibility_policy import (
    OFFICIAL_HERMES_SKILL_ROOT,
    PLATFORM_DEFAULT_SKILL_NAMES,
    READONLY_HERMES_SKILL_ROOT,
    RETIRED_PLATFORM_SKILL_NAMES,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
    load_legacy_upstream_skill_names,
    resolve_versioned_skill_visibility,
)


PLATFORM_BASELINE_PROFILE_PLUGINS = (
    "kanban-lifecycle",
    "clarify-lifecycle",
    PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
)


class DashboardJsonClient(Protocol):
    async def health(self, endpoint: RuntimeEndpoint) -> dict[str, Any]: ...

    async def dashboard_json_request(
        self,
        endpoint: RuntimeEndpoint,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HermesProfileInfo:
    name: str
    is_default: bool
    description: str
    skill_count: int


class HermesProfileAdapter:
    """Narrow compatibility boundary around Hermes' public Profile API."""

    def __init__(
        self,
        dashboard: DashboardJsonClient,
        *,
        workspace_root: str = HERMES_WORKSPACE,
    ) -> None:
        self.dashboard = dashboard
        self.workspace_root = workspace_root.rstrip("/")

    async def list_profiles(
        self,
        endpoint: RuntimeEndpoint,
    ) -> list[HermesProfileInfo]:
        payload = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            "/api/profiles",
        )
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            raise HermesProtocolError(
                "runtime profile list returned invalid payload",
                path="/api/profiles",
            )
        result: list[HermesProfileInfo] = []
        for index, item in enumerate(profiles):
            if not isinstance(item, dict):
                raise HermesProtocolError(
                    f"runtime profile list item {index} is invalid",
                    path="/api/profiles",
                )
            name = str(item.get("name") or "").strip()
            if not name:
                raise HermesProtocolError(
                    f"runtime profile list item {index} has no name",
                    path="/api/profiles",
                )
            result.append(
                HermesProfileInfo(
                    name=name,
                    is_default=bool(item.get("is_default")),
                    description=str(item.get("description") or ""),
                    skill_count=max(0, int(item.get("skill_count") or 0)),
                )
            )
        return result

    async def has_nonterminal_kanban_task(
        self,
        endpoint: RuntimeEndpoint,
        *,
        profile_name: str,
    ) -> bool:
        """Check every native Kanban board before a Profile is destroyed.

        Kanban is process/PVC scoped and intentionally shared by Profiles, so
        this must use its public dashboard plugin API instead of maintaining a
        second Backend task table.
        """
        assignees = await self._kanban_assignees(endpoint)
        return profile_name in assignees

    async def has_any_nonterminal_kanban_task(
        self,
        endpoint: RuntimeEndpoint,
    ) -> bool:
        """Return whether Hermes' native Board contains any unfinished card."""
        return bool(
            await self._kanban_assignees(endpoint, include_unassigned=True)
        )

    async def has_any_executable_kanban_task(
        self,
        endpoint: RuntimeEndpoint,
    ) -> bool:
        """Return whether the Board has work that still requires a warm Runtime.

        Blocked cards need user recovery or reassignment rather than a resident
        worker. They remain nonterminal for Profile safety checks, but must not
        keep a Knative Runtime awake indefinitely.
        """
        return bool(
            await self._kanban_assignees(
                endpoint,
                include_unassigned=True,
                runtime_requiring_only=True,
            )
        )

    async def get_kanban_task_detail(
        self,
        endpoint: RuntimeEndpoint,
        *,
        task_id: str,
        board: str | None = None,
    ) -> dict[str, Any]:
        """Read one authoritative native Kanban task through Dashboard API."""
        path = f"/api/plugins/kanban/tasks/{quote(task_id, safe='')}"
        if board:
            path += f"?board={quote(board, safe='')}"
        payload = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            path,
        )
        task = payload.get("task")
        if not isinstance(task, dict) or str(task.get("id") or "") != task_id:
            raise HermesProtocolError(
                "runtime Kanban task detail returned invalid payload",
                path="/api/plugins/kanban/tasks/{task_id}",
            )
        return payload

    async def _kanban_assignees(
        self,
        endpoint: RuntimeEndpoint,
        *,
        include_unassigned: bool = False,
        runtime_requiring_only: bool = False,
    ) -> set[str]:
        board_slugs = {"default"}
        assignees: set[str] = set()
        try:
            payload = await self.dashboard.dashboard_json_request(
                endpoint,
                "GET",
                "/api/plugins/kanban/boards",
            )
        except HermesProtocolError as exc:
            # A runtime without the Kanban plugin cannot hold native Kanban
            # assignments. Other protocol failures stay fail-closed.
            if exc.status_code == 404:
                return set()
            raise
        boards = payload.get("boards")
        if isinstance(boards, list):
            for item in boards:
                if isinstance(item, dict):
                    slug = str(item.get("slug") or "").strip()
                    if slug:
                        board_slugs.add(slug)
        for slug in sorted(board_slugs):
            payload = await self.dashboard.dashboard_json_request(
                endpoint,
                "GET",
                (f"/api/plugins/kanban/board?board={quote(slug, safe='')}&include_archived=true"),
            )
            columns = payload.get("columns")
            if not isinstance(columns, list):
                raise HermesProtocolError(
                    "runtime Kanban board returned invalid payload",
                    path="/api/plugins/kanban/board",
                )
            for column in columns:
                if not isinstance(column, dict):
                    continue
                tasks = column.get("tasks")
                if not isinstance(tasks, list):
                    continue
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    status = str(task.get("status") or "")
                    if status in {"done", "archived"}:
                        continue
                    if runtime_requiring_only and status == "blocked":
                        continue
                    assignee = str(task.get("assignee") or "")
                    if assignee or include_unassigned:
                        assignees.add(assignee or "__unassigned__")
        return assignees

    async def create_profile(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        description: str,
        soul_content: str,
        clone_from: str | None = None,
    ) -> HermesProfileInfo:
        existing = await self._find(endpoint, name)
        if existing is None:
            body: dict[str, Any] = {
                "name": name,
                "description": description,
                "clone_all": False,
                "no_skills": clone_from is None,
            }
            if clone_from is not None:
                body["clone_from"] = clone_from
            await self.dashboard.dashboard_json_request(
                endpoint,
                "POST",
                "/api/profiles",
                json_body=body,
            )
        await self.update_description(
            endpoint,
            name=name,
            description=description,
        )
        await self.update_soul(
            endpoint,
            name=name,
            soul_content=soul_content,
        )
        created = await self._find(endpoint, name)
        if created is None:
            raise HermesProtocolError(
                "runtime profile creation was not observable",
                path="/api/profiles",
            )
        return created

    async def update_soul(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        soul_content: str,
    ) -> None:
        await self.dashboard.dashboard_json_request(
            endpoint,
            "PUT",
            f"/api/profiles/{quote(name, safe='')}/soul",
            json_body={"content": soul_content},
        )

    async def read_soul(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
    ) -> str:
        payload = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/profiles/{quote(name, safe='')}/soul",
        )
        content = payload.get("content")
        if not isinstance(content, str):
            raise HermesProtocolError(
                "runtime profile SOUL returned invalid payload",
                path=f"/api/profiles/{quote(name, safe='')}/soul",
            )
        return content

    async def update_description(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        description: str,
    ) -> None:
        await self.dashboard.dashboard_json_request(
            endpoint,
            "PUT",
            f"/api/profiles/{quote(name, safe='')}/description",
            json_body={"description": description},
        )

    async def configure_platform_profile(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        config_generation: int,
        plugin_ids: list[str],
        default_model_entry_id: int,
        provider_models: dict[str, dict[str, int | bool]],
        installed_skill_names: list[str] | None = None,
    ) -> None:
        """Project the platform-owned identity and model baseline to a Profile."""
        if (
            runtime_agent_profile_id <= 0
            or runtime_workspace_id <= 0
            or default_model_entry_id <= 0
        ):
            raise HermesProtocolError("runtime profile identity is invalid")
        default_config = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            "/api/config?profile=default",
        )
        default_providers = default_config.get("providers")
        default_platform_provider = (
            default_providers.get("platform") if isinstance(default_providers, dict) else None
        )
        if not isinstance(default_platform_provider, dict):
            raise HermesProtocolError(
                "runtime default profile has no platform model provider",
                path="/api/config",
            )
        proxy_url = str(
            default_platform_provider.get("base_url") or default_platform_provider.get("api") or ""
        ).rstrip("/")
        expected_proxy_suffix = f"/{int(runtime_workspace_id)}/v1"
        if not proxy_url or not proxy_url.endswith(expected_proxy_suffix):
            raise HermesProtocolError(
                "runtime platform model proxy is invalid",
                path="/api/config",
            )
        default_model = str(int(default_model_entry_id))
        normalized_models: dict[str, dict[str, int | bool]] = {}
        for model_id, metadata in sorted(
            provider_models.items(),
            key=lambda item: int(item[0]) if str(item[0]).isdigit() else -1,
        ):
            normalized_id = str(model_id).strip()
            if (
                not normalized_id.isdigit()
                or int(normalized_id) <= 0
                or not isinstance(metadata, dict)
            ):
                raise HermesProtocolError("runtime platform model catalog is invalid")
            context_length = metadata.get("context_length")
            supports_vision = metadata.get("supports_vision")
            if (
                isinstance(context_length, bool)
                or not isinstance(context_length, int)
                or context_length <= 0
                or not isinstance(supports_vision, bool)
            ):
                raise HermesProtocolError("runtime platform model metadata is invalid")
            normalized_models[normalized_id] = {
                "context_length": context_length,
                "supports_vision": supports_vision,
            }
        if default_model not in normalized_models:
            raise HermesProtocolError("runtime platform default model is not visible")
        profile_workspace = f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}/shared"
        profile_skills = f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}/skills"
        default_skills = default_config.get("skills")
        default_disabled = (
            default_skills.get("disabled") if isinstance(default_skills, dict) else []
        )
        if not (
            isinstance(default_disabled, list)
            and all(isinstance(item, str) for item in default_disabled)
        ):
            raise HermesProtocolError(
                "runtime default Skill disabled policy is invalid",
                path="/api/config",
            )
        default_platform = default_config.get("platform")
        default_visibility = (
            default_platform.get("skill_visibility") if isinstance(default_platform, dict) else None
        )
        policy_version = (
            default_visibility.get("policy_version")
            if isinstance(default_visibility, dict)
            else SKILL_VISIBILITY_POLICY_V1
        )
        if policy_version not in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            raise HermesProtocolError(
                "runtime default Skill policy version is invalid",
                path="/api/config",
            )
        prior_platform_disabled = (
            default_visibility.get("platform_disabled")
            if isinstance(default_visibility, dict)
            else None
        )
        legacy_names = load_legacy_upstream_skill_names()
        if not (
            isinstance(prior_platform_disabled, list)
            and all(isinstance(item, str) for item in prior_platform_disabled)
        ):
            prior_platform_disabled = sorted(
                (legacy_names | RETIRED_PLATFORM_SKILL_NAMES)
                - PLATFORM_DEFAULT_SKILL_NAMES
            )
        declared_user_disabled = (
            default_visibility.get("user_disabled")
            if isinstance(default_visibility, dict)
            else None
        )
        if declared_user_disabled is not None and not (
            isinstance(declared_user_disabled, list)
            and all(isinstance(item, str) for item in declared_user_disabled)
        ):
            raise HermesProtocolError(
                "runtime default user-disabled Skill policy is invalid",
                path="/api/config",
            )
        user_disabled = (
            set(declared_user_disabled)
            if declared_user_disabled is not None
            else set(default_disabled) - set(prior_platform_disabled)
        )
        skill_visibility = resolve_versioned_skill_visibility(
            policy_version=policy_version,
            legacy_upstream_names=legacy_names,
            explicitly_installed_names=installed_skill_names or (),
            user_disabled_names=user_disabled,
        )
        selected: dict[str, Any] = {}
        agent = default_config.get("agent")
        if isinstance(agent, dict):
            disabled_toolsets = agent.get("disabled_toolsets")
            if isinstance(disabled_toolsets, list) and all(
                isinstance(item, str) for item in disabled_toolsets
            ):
                selected["agent"] = {
                    "disabled_toolsets": list(disabled_toolsets),
                }
        # Hermes' public config endpoint normalizes a structured model block
        # to a bare string. Rebuild the Backend-owned transport envelope so a
        # numeric platform model ID can never be interpreted as OpenRouter.
        selected["model"] = {
            "provider": "custom:platform",
            "default": default_model,
            "base_url": proxy_url,
            "api_mode": "chat_completions",
            "default_model_entry_id": int(default_model_entry_id),
            "ollama_num_ctx": 0,
        }
        selected["providers"] = {
            "platform": {
                "name": "platform",
                "base_url": proxy_url,
                "api": proxy_url,
                "key_env": "OPENAI_API_KEY",
                "transport": "chat_completions",
                "default_model": default_model,
                "discover_models": False,
                "models": normalized_models,
            }
        }
        selected.update(
            {
                # Named Profiles may own cold remote MCP connections.  Keep
                # their first Agent snapshot inside a bounded discovery
                # window; the default interactive profile remains unchanged.
                "mcp_discovery_timeout": 30,
                "terminal": {"cwd": profile_workspace},
                "workspace_dir": profile_workspace,
                "skills": {
                    "external_dirs": [
                        OFFICIAL_HERMES_SKILL_ROOT,
                        *(
                            [READONLY_HERMES_SKILL_ROOT]
                            if policy_version == SKILL_VISIBILITY_POLICY_V3
                            else []
                        ),
                        profile_skills,
                    ],
                    "disabled": sorted(skill_visibility.effective_disabled),
                },
                "platform": {
                    "runtime_workspace_id": int(runtime_workspace_id),
                    "runtime_agent_profile_id": int(runtime_agent_profile_id),
                    "runtime_id": f"rt-{int(runtime_workspace_id)}",
                    "config_generation": int(config_generation),
                    "skill_visibility": {
                        "policy_version": policy_version,
                        "user_disabled": sorted(user_disabled),
                        "platform_disabled": sorted(skill_visibility.platform_disabled),
                    },
                },
                "plugins": {
                    "enabled": list(
                        dict.fromkeys([*PLATFORM_BASELINE_PROFILE_PLUGINS, *plugin_ids])
                    ),
                },
            }
        )
        encoded_name = quote(name, safe="")
        await self.dashboard.dashboard_json_request(
            endpoint,
            "PUT",
            f"/api/config?profile={encoded_name}",
            json_body={"config": selected, "profile": name},
        )

    async def replace_mcp_servers(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        servers: dict[str, dict[str, Any]],
    ) -> None:
        encoded_name = quote(name, safe="")
        await self.dashboard.dashboard_json_request(
            endpoint,
            "PUT",
            f"/api/mcp/servers?profile={encoded_name}",
            json_body={"servers": servers, "profile": name},
        )
        payload = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/mcp/servers?profile={encoded_name}",
        )
        observed = payload.get("servers")
        if not isinstance(observed, list) or not all(
            isinstance(item, dict) for item in observed
        ):
            raise HermesProtocolError(
                "runtime profile MCP verification returned invalid payload",
                path="/api/mcp/servers",
            )

        observed_by_name: dict[str, dict[str, Any]] = {}
        for item in observed:
            server_name = str(item.get("name") or "").strip()
            if not server_name or server_name in observed_by_name:
                raise HermesProtocolError(
                    "runtime profile MCP verification returned invalid server identity",
                    path="/api/mcp/servers",
                )
            observed_by_name[server_name] = item

        expected_by_name: dict[str, dict[str, Any]] = {}
        for server_name, config in servers.items():
            if not isinstance(server_name, str) or not server_name or not isinstance(config, dict):
                raise HermesProtocolError("runtime profile MCP configuration is invalid")
            headers = config.get("headers")
            auth = config.get("auth")
            if not auth and isinstance(headers, dict) and any(
                str(key).lower() == "authorization" for key in headers
            ):
                auth = "header"
            env = config.get("env")
            if env is not None and not isinstance(env, dict):
                raise HermesProtocolError("runtime profile MCP configuration is invalid")
            expected_by_name[server_name] = {
                "transport": (
                    "http"
                    if config.get("url")
                    else "stdio"
                    if config.get("command")
                    else "unknown"
                ),
                "url": config.get("url"),
                "command": config.get("command"),
                "args": list(config.get("args") or []),
                "env_keys": set(map(str, (env or {}).keys())),
                "auth": auth,
                "enabled": config.get("enabled", True) is not False,
                "tools": config.get("tools"),
            }

        if set(observed_by_name) != set(expected_by_name):
            raise HermesProtocolError(
                "runtime profile MCP verification failed",
                path="/api/mcp/servers",
            )
        for server_name, expected in expected_by_name.items():
            actual = observed_by_name[server_name]
            actual_env = actual.get("env")
            if not isinstance(actual_env, dict) or (
                {
                    "transport": actual.get("transport"),
                    "url": actual.get("url"),
                    "command": actual.get("command"),
                    "args": actual.get("args"),
                    "env_keys": set(map(str, actual_env.keys())),
                    "auth": actual.get("auth"),
                    "enabled": actual.get("enabled"),
                    "tools": actual.get("tools"),
                }
                != expected
            ):
                raise HermesProtocolError(
                    "runtime profile MCP verification failed",
                    path="/api/mcp/servers",
                )
        # The public MCP endpoint preserves the existing generic discovery
        # timeout (1.5s).  Persist the named Profile override through the raw
        # config endpoint so a cold Profile Agent waits for its own MCP set.
        raw_path = f"/api/config/raw?profile={encoded_name}"
        raw = await self.dashboard.dashboard_json_request(endpoint, "GET", raw_path)
        yaml_text = raw.get("yaml")
        if not isinstance(yaml_text, str):
            raise HermesProtocolError("runtime profile raw config is invalid", path=raw_path)
        document = yaml.safe_load(yaml_text) or {}
        if not isinstance(document, dict):
            raise HermesProtocolError("runtime profile raw config is invalid", path=raw_path)
        document["mcp_discovery_timeout"] = 30
        if name != "default":
            toolsets = document.get("platform_toolsets")
            if not isinstance(toolsets, dict):
                toolsets = {}
            cli = toolsets.get("cli")
            preserved = [
                str(item)
                for item in (cli if isinstance(cli, list) else [])
                if isinstance(item, str)
                and not item.startswith("mcp-")
                and item != "no_mcp"
            ]
            toolsets["cli"] = build_platform_cli_toolsets(
                preserved_toolsets=preserved,
                mcp_toolsets=(
                    f"mcp-{server_name}" for server_name in sorted(expected_by_name)
                ),
            )
            document["platform_toolsets"] = toolsets
        await self.dashboard.dashboard_json_request(
            endpoint,
            "PUT",
            raw_path,
            json_body={
                "yaml_text": yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
                "profile": name,
            },
        )

    async def replace_profile_skills(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        runtime_agent_profile_id: int,
        files: dict[str, bytes],
    ) -> None:
        """Replace one expert's externally managed Skill tree.

        Hosted Hermes intentionally confines the managed Files API to the
        Runtime workspace.  Keep platform packages there and bind the bounded
        directory to the named Profile through ``skills.external_dirs`` rather
        than reaching into Hermes Home.
        """
        if name == "default":
            raise HermesProtocolError("default profile skills use the generation projector")
        if runtime_agent_profile_id <= 0:
            raise HermesProtocolError("runtime agent profile id is invalid")
        validated_files: list[tuple[str, bytes]] = []
        for relative_path, content in sorted(files.items()):
            if (
                not relative_path
                or relative_path.startswith("/")
                or ".." in relative_path.split("/")
            ):
                raise HermesProtocolError("runtime skill path is invalid")
            if not isinstance(content, bytes):
                raise HermesProtocolError("runtime skill content is invalid")
            validated_files.append((relative_path, content))

        skills_root = f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}/skills"
        try:
            await self.dashboard.dashboard_json_request(
                endpoint,
                "DELETE",
                "/api/files",
                json_body={"path": skills_root, "recursive": True},
            )
        except HermesProtocolError as exc:
            if exc.status_code != 404:
                raise
        for relative_path, content in validated_files:
            encoded = base64.b64encode(content).decode("ascii")
            await self.dashboard.dashboard_json_request(
                endpoint,
                "POST",
                "/api/files/upload",
                json_body={
                    "path": f"{skills_root}/{relative_path}",
                    "data_url": f"data:application/octet-stream;base64,{encoded}",
                    "overwrite": True,
                },
            )
        raw_path = f"/api/config/raw?profile={quote(name, safe='')}"
        raw = await self.dashboard.dashboard_json_request(endpoint, "GET", raw_path)
        yaml_text = raw.get("yaml")
        if not isinstance(yaml_text, str):
            raise HermesProtocolError("runtime profile raw config is invalid", path=raw_path)
        document = yaml.safe_load(yaml_text) or {}
        if not isinstance(document, dict):
            raise HermesProtocolError("runtime profile raw config is invalid", path=raw_path)
        skills = document.get("skills")
        if not isinstance(skills, dict):
            skills = {}
        external_dirs = skills.get("external_dirs")
        if not isinstance(external_dirs, list):
            external_dirs = []
        platform_section = document.get("platform")
        profile_policy_version = (
            platform_section.get("skill_visibility", {}).get("policy_version")
            if isinstance(platform_section, dict)
            else None
        )
        readonly_dirs = (
            [READONLY_HERMES_SKILL_ROOT]
            if profile_policy_version == SKILL_VISIBILITY_POLICY_V3
            else []
        )
        skills["external_dirs"] = list(
            dict.fromkeys(
                [
                    *[str(item) for item in external_dirs if isinstance(item, str)],
                    OFFICIAL_HERMES_SKILL_ROOT,
                    *readonly_dirs,
                    skills_root,
                ]
            )
        )
        document["skills"] = skills
        await self.dashboard.dashboard_json_request(
            endpoint,
            "PUT",
            raw_path,
            json_body={
                "yaml_text": yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
                "profile": name,
            },
        )

    async def ensure_workspace(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_agent_profile_id: int,
    ) -> None:
        """Create and observe the bounded shared root for one expert."""
        if runtime_agent_profile_id <= 0:
            raise HermesProtocolError("runtime agent profile id is invalid")
        shared_root = f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}/shared"
        await self.dashboard.dashboard_json_request(
            endpoint,
            "POST",
            "/api/files/mkdir",
            json_body={"path": shared_root},
        )
        observed = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/files?path={quote(shared_root, safe='')}",
        )
        if not isinstance(observed.get("entries"), list):
            raise HermesProtocolError(
                "runtime expert workspace was not observable",
                path="/api/files",
            )

    async def verify_profile_projection(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
        description: str,
        soul_content: str,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int,
        config_generation: int,
        plugin_ids: list[str],
        default_model_entry_id: int,
    ) -> None:
        """Fail closed unless the public Hermes APIs expose the target state."""
        profile = await self._find(endpoint, name)
        if profile is None or profile.description != description:
            raise HermesProtocolError(
                "runtime profile identity verification failed",
                path="/api/profiles",
            )
        if await self.read_soul(endpoint, name=name) != soul_content:
            raise HermesProtocolError(
                "runtime profile SOUL verification failed",
                path=f"/api/profiles/{quote(name, safe='')}/soul",
            )
        config = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/config?profile={quote(name, safe='')}",
        )
        platform = config.get("platform")
        if not isinstance(platform, dict) or (
            int(platform.get("runtime_workspace_id") or 0) != runtime_workspace_id
            or int(platform.get("runtime_agent_profile_id") or 0) != runtime_agent_profile_id
            or int(platform.get("config_generation") or 0) != config_generation
        ):
            raise HermesProtocolError(
                "runtime profile platform identity verification failed",
                path="/api/config",
            )
        plugins = config.get("plugins")
        enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
        expected_plugins = set(PLATFORM_BASELINE_PROFILE_PLUGINS) | set(plugin_ids)
        if not isinstance(enabled, list) or set(map(str, enabled)) != expected_plugins:
            raise HermesProtocolError(
                "runtime profile Plugin verification failed",
                path="/api/config",
            )
        skills = config.get("skills")
        external_dirs = skills.get("external_dirs") if isinstance(skills, dict) else None
        expected_skills_root = (
            f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}/skills"
        )
        profile_policy_version = (
            platform.get("skill_visibility", {}).get("policy_version")
            if isinstance(platform.get("skill_visibility"), dict)
            else None
        )
        expected_external_dirs = (
            [OFFICIAL_HERMES_SKILL_ROOT, READONLY_HERMES_SKILL_ROOT, expected_skills_root]
            if profile_policy_version == SKILL_VISIBILITY_POLICY_V3
            else [OFFICIAL_HERMES_SKILL_ROOT, expected_skills_root]
        )
        # v3 Profiles created before the ppt-master rollout keep the legacy
        # two-directory shape until the next expert skill replacement; accept
        # both forms so reconnect verification does not break old Profiles.
        legacy_v3_external_dirs = [
            OFFICIAL_HERMES_SKILL_ROOT,
            expected_skills_root,
        ]
        if external_dirs not in (expected_external_dirs, legacy_v3_external_dirs):
            raise HermesProtocolError(
                "runtime profile Skill directory verification failed",
                path="/api/config",
            )
        model_info = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/model/info?profile={quote(name, safe='')}",
        )
        if str(model_info.get("provider") or "") != "custom:platform" or str(
            model_info.get("model") or ""
        ) != str(int(default_model_entry_id)):
            raise HermesProtocolError(
                "runtime profile platform model verification failed",
                path="/api/model/info",
            )
        shared_root = f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}/shared"
        observed = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/files?path={quote(shared_root, safe='')}",
        )
        if not isinstance(observed.get("entries"), list):
            raise HermesProtocolError(
                "runtime expert workspace verification failed",
                path="/api/files",
            )

    async def delete_profile(
        self,
        endpoint: RuntimeEndpoint,
        *,
        name: str,
    ) -> None:
        if name == "default":
            raise HermesProtocolError("the default Hermes profile cannot be deleted")
        try:
            await self.dashboard.dashboard_json_request(
                endpoint,
                "DELETE",
                f"/api/profiles/{quote(name, safe='')}",
            )
        except HermesProtocolError as exc:
            if exc.status_code != 404:
                raise
        if await self._find(endpoint, name) is not None:
            raise HermesProtocolError(
                "runtime profile deletion was not observable",
                path=f"/api/profiles/{quote(name, safe='')}",
            )

    async def delete_workspace(
        self,
        endpoint: RuntimeEndpoint,
        *,
        runtime_agent_profile_id: int,
    ) -> None:
        if runtime_agent_profile_id <= 0:
            raise HermesProtocolError("runtime agent profile id is invalid")
        try:
            await self.dashboard.dashboard_json_request(
                endpoint,
                "DELETE",
                "/api/files",
                json_body={
                    "path": (f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}"),
                    "recursive": True,
                },
            )
        except HermesProtocolError as exc:
            # Deletion is an idempotent reconciliation step. An absent bounded
            # expert directory is already the desired state.
            if exc.status_code != 404:
                raise
        expert_root = f"{self.workspace_root}/experts/{int(runtime_agent_profile_id)}"
        try:
            await self.dashboard.dashboard_json_request(
                endpoint,
                "GET",
                f"/api/files?path={quote(expert_root, safe='')}",
            )
        except HermesProtocolError as exc:
            if exc.status_code == 404:
                return
            raise
        raise HermesProtocolError(
            "runtime expert workspace deletion was not observable",
            path="/api/files",
        )

    async def assert_compatible(self, endpoint: RuntimeEndpoint) -> None:
        await self.dashboard.health(endpoint)
        profiles = await self.list_profiles(endpoint)
        if not any(profile.name == "default" and profile.is_default for profile in profiles):
            raise HermesProtocolError(
                "runtime does not expose the canonical default profile",
                path="/api/profiles",
            )
        config = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            "/api/config?profile=default",
        )
        if not isinstance(config, dict):
            raise HermesProtocolError(
                "runtime does not expose Profile-scoped config",
                path="/api/config",
            )
        files = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            f"/api/files?path={quote(self.workspace_root, safe='')}",
        )
        if not isinstance(files.get("entries"), list):
            raise HermesProtocolError(
                "runtime does not expose workspace Files",
                path="/api/files",
            )
        mcp = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            "/api/mcp/servers?profile=default",
        )
        if not isinstance(mcp.get("servers"), list):
            raise HermesProtocolError(
                "runtime does not expose Profile-scoped MCP servers",
                path="/api/mcp/servers",
            )
        kanban = await self.dashboard.dashboard_json_request(
            endpoint,
            "GET",
            "/api/plugins/kanban/boards",
        )
        if not isinstance(kanban.get("boards"), list):
            raise HermesProtocolError(
                "runtime does not expose native Kanban boards",
                path="/api/plugins/kanban/boards",
            )

    async def _find(
        self,
        endpoint: RuntimeEndpoint,
        name: str,
    ) -> HermesProfileInfo | None:
        return next(
            (profile for profile in await self.list_profiles(endpoint) if profile.name == name),
            None,
        )
