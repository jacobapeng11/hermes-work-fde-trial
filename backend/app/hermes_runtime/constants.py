from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

HERMES_DASHBOARD_PORT = 9119
RUNTIME_SUPERVISOR_PORT = 9120
RUNTIME_MODEL_RELAY_PORT = 9121
RUNTIME_LOCAL_MODEL_RELAY_BASE_URL = (
    f"http://127.0.0.1:{RUNTIME_MODEL_RELAY_PORT}/model-relay"
)
HERMES_HOME = "/opt/data"
HERMES_WORKSPACE = "/data/workspace"
HERMES_ENTERPRISE_KNOWLEDGE_MOUNT = "/knowledge/enterprise"
HERMES_RUNTIME_USER_UID = 10000
HERMES_RUNTIME_SHARED_GID = 1000
PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID = (
    "platform-main-conversation-boundary-v1"
)
HERMES_0191_STATE_SCHEMA = 23
HERMES_0191_CONFIG_SCHEMA = 33
HERMES_0201_STATE_SCHEMA = 25
HERMES_0201_CONFIG_SCHEMA = 34

# The Release Worker uses these aliases as the schema contract implemented by
# the current platform revision.  A Hermes release with the same schemas can
# be verified automatically.  When upstream changes either schema, the
# platform adaptation must update these values together with its renderer and
# migration logic before the candidate may continue.
HERMES_PLATFORM_STATE_SCHEMA = HERMES_0201_STATE_SCHEMA
HERMES_PLATFORM_CONFIG_SCHEMA = HERMES_0201_CONFIG_SCHEMA


def runtime_session_workspace_path(
    root: str | None,
    runtime_session_id: str,
    *,
    runtime_agent_profile_id: int | None = None,
    hermes_profile_name: str = "default",
) -> str | None:
    """Return the stable session directory without depending on Dashboard."""

    if not root:
        return None
    safe_session_id = re.sub(r"[^A-Za-z0-9._-]", "_", runtime_session_id)
    base = root.rstrip("/")
    if hermes_profile_name != "default" and runtime_agent_profile_id is not None:
        base = f"{base}/experts/{int(runtime_agent_profile_id)}"
    return f"{base}/sessions/{safe_session_id}"


def append_url_path(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PUBLIC_API_BASE must be an absolute HTTP URL")
    path = f"{parsed.path.rstrip('/')}/{suffix.lstrip('/')}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
