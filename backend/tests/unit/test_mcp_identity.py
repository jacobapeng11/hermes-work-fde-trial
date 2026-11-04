from app.services.mcp_identity import mcp_slug_namespace, runtime_mcp_server_key


def test_runtime_mcp_server_key_uses_stable_asset_identity() -> None:
    assert runtime_mcp_server_key(301) == "asset-301"


def test_mcp_slug_namespace_separates_public_and_private_names() -> None:
    assert mcp_slug_namespace(visibility="organization", owner_user_id=2) == "organization"
    assert mcp_slug_namespace(visibility="public", owner_user_id=9) == "organization"
    assert mcp_slug_namespace(visibility="private", owner_user_id=2) == "user:2"
