from __future__ import annotations

import os
from collections.abc import Mapping


PROXY_ENVIRONMENT_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "FTP_PROXY",
    "ftp_proxy",
    "RSYNC_PROXY",
    "rsync_proxy",
    "NPM_CONFIG_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
    "npm_config_proxy",
    "npm_config_https_proxy",
    "PIP_PROXY",
    "pip_proxy",
    "GLOBAL_AGENT_HTTP_PROXY",
    "GLOBAL_AGENT_HTTPS_PROXY",
    "global_agent_http_proxy",
    "global_agent_https_proxy",
)


def clear_proxy_environment() -> None:
    """Remove host-only proxy configuration from this project process."""

    for variable in PROXY_ENVIRONMENT_VARIABLES:
        os.environ.pop(variable, None)


def proxy_free_environment(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an isolated child environment without mutating its input."""

    environment = dict(os.environ if source is None else source)
    for variable in PROXY_ENVIRONMENT_VARIABLES:
        environment.pop(variable, None)
    if overrides:
        environment.update(overrides)
    return environment
