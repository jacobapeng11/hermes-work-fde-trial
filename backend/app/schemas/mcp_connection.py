from __future__ import annotations

from urllib.parse import urlparse


def validate_mcp_connection_target(transport: str, config: object) -> None:
    if not isinstance(config, dict):
        raise ValueError("MCP connection config must be an object")
    if transport in {"streamable_http", "sse"}:
        url = config.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("MCP HTTP connection target requires a URL")
        parsed = urlparse(url)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("MCP HTTP connection target requires a valid URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(character.isspace() for character in url)
        ):
            raise ValueError("MCP HTTP connection target requires a safe HTTP(S) URL")
    elif transport == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("MCP stdio connection target requires a command")
    else:
        raise ValueError("MCP transport is unsupported")
