from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPOSITORY_ROOT / "backend" / "scripts" / "offboard_user_cleanup.py"


def _load_cleanup_module():
    spec = importlib.util.spec_from_file_location("offboard_user_cleanup_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_profile_revision_deletion_keeps_cross_user_revisions() -> None:
    module = _load_cleanup_module()
    statements = [
        statement
        for statement in module.DELETES
        if statement.startswith("DELETE FROM runtime_agent_profile_revision")
    ]
    assert len(statements) == 1
    statement = statements[0]
    assert "created_by_user_id" not in statement

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    CREATE TABLE cloud_hermes_profile (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE runtime_agent_profile (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        runtime_workspace_id INTEGER NOT NULL
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE runtime_agent_profile_revision (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        runtime_workspace_id INTEGER NOT NULL,
                        runtime_agent_profile_id INTEGER NOT NULL,
                        created_by_user_id INTEGER
                    )
                    """
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO cloud_hermes_profile VALUES (10, 990095), (30, 7)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO runtime_agent_profile VALUES (20, 990095, 10), (40, 7, 30)"
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO runtime_agent_profile_revision VALUES
                        (101, 990095, 10, 20, 990095),
                        (102, 7, 30, 40, 990095)
                    """
                )
            )
            await connection.execute(text(statement), {"u": 990095})
            remaining = await connection.execute(
                text("SELECT id FROM runtime_agent_profile_revision ORDER BY id")
            )
            assert remaining.scalars().all() == [102]
    finally:
        await engine.dispose()
