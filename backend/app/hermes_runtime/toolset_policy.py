from __future__ import annotations

from collections.abc import Iterable


PLATFORM_REQUIRED_CLI_TOOLSETS: tuple[str, ...] = ("hermes-cli",)


def build_platform_cli_toolsets(
    *,
    mcp_toolsets: Iterable[str],
    preserved_toolsets: Iterable[str] = (),
) -> list[str]:
    """Build the complete Hermes CLI allowlist from platform and asset policy."""

    preserved = [
        item
        for item in _unique_strings(preserved_toolsets)
        if item not in PLATFORM_REQUIRED_CLI_TOOLSETS
        and item != "no_mcp"
        and not item.startswith("mcp-")
    ]
    mcps = [
        item
        for item in _unique_strings(mcp_toolsets)
        if item.startswith("mcp-")
    ]
    return [
        *PLATFORM_REQUIRED_CLI_TOOLSETS,
        *preserved,
        *(mcps or ["no_mcp"]),
    ]


def _unique_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


__all__ = ["PLATFORM_REQUIRED_CLI_TOOLSETS", "build_platform_cli_toolsets"]
