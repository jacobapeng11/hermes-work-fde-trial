from __future__ import annotations

from collections.abc import Iterable


PLATFORM_REQUIRED_CLI_TOOLSETS: tuple[str, ...] = ("hermes-cli",)


def normalize_platform_cli_toolsets(
    values: Iterable[str],
    *,
    valid_mcp_toolsets: set[str] | None = None,
) -> list[str]:
    """Restore platform-required tools while preserving allowed extensions."""

    unique = list(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )
    extensions = [
        item
        for item in unique
        if item not in PLATFORM_REQUIRED_CLI_TOOLSETS
        and item != "no_mcp"
        and (
            not item.startswith("mcp-")
            or valid_mcp_toolsets is None
            or item in valid_mcp_toolsets
        )
    ]
    has_mcp = any(item.startswith("mcp-") for item in extensions)
    return [
        *PLATFORM_REQUIRED_CLI_TOOLSETS,
        *extensions,
        *([] if has_mcp else ["no_mcp"]),
    ]


__all__ = ["PLATFORM_REQUIRED_CLI_TOOLSETS", "normalize_platform_cli_toolsets"]
