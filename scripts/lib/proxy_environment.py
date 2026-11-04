from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


_VARIABLE_FILE = Path(__file__).with_name("proxy-env-vars.txt")
PROXY_ENVIRONMENT_VARIABLES = tuple(
    line.strip()
    for line in _VARIABLE_FILE.read_text(encoding="utf-8").splitlines()
    if line.strip()
)


def clear_proxy_environment() -> None:
    for variable in PROXY_ENVIRONMENT_VARIABLES:
        os.environ.pop(variable, None)


def proxy_free_environment(
    source: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for variable in PROXY_ENVIRONMENT_VARIABLES:
        environment.pop(variable, None)
    if overrides:
        environment.update(overrides)
    return environment
