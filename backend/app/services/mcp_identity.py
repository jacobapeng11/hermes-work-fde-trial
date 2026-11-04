from __future__ import annotations


def runtime_mcp_server_key(mcp_id: int) -> str:
    """Return the stable Runtime alias for an MCP asset identity."""

    normalized_id = int(mcp_id)
    if normalized_id <= 0:
        raise ValueError("mcp_id must be positive")
    return f"asset-{normalized_id}"


def mcp_slug_namespace(*, visibility: str, owner_user_id: int) -> str:
    """Return the business namespace in which an active slug is reserved."""

    if visibility == "private":
        normalized_owner_id = int(owner_user_id)
        if normalized_owner_id <= 0:
            raise ValueError("owner_user_id must be positive")
        return f"user:{normalized_owner_id}"
    return "organization"


__all__ = ["mcp_slug_namespace", "runtime_mcp_server_key"]
