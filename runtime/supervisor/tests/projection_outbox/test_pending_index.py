from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from platform_supervisor.storage import SQLiteStore


QUERY = """
SELECT outbox_id
FROM projection_outbox
WHERE acknowledged_at IS NULL
ORDER BY outbox_id
LIMIT 100
"""


@pytest.mark.asyncio
async def test_pending_outbox_query_uses_partial_index(tmp_path: Path) -> None:
    database = tmp_path / "supervisor.db"
    store = SQLiteStore(database)
    await store.start()
    await store.close()

    connection = sqlite3.connect(database)
    try:
        connection.executemany(
            """
            INSERT INTO projection_outbox(
                projection_id, projection_type, payload_json, created_at,
                acknowledged_at
            ) VALUES (?, 'run.event', '{}', ?, ?)
            """,
            [
                (
                    f"acked-{index}",
                    f"2026-10-01T00:00:{index % 60:02d}+00:00",
                    f"2026-10-01T00:01:{index % 60:02d}+00:00",
                )
                for index in range(250)
            ],
        )
        connection.execute(
            """
            INSERT INTO projection_outbox(
                projection_id, projection_type, payload_json, created_at
            ) VALUES ('pending-1', 'run.event', '{}',
                      '2026-10-02T00:00:00+00:00')
            """
        )
        connection.commit()

        indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('projection_outbox')"
            ).fetchall()
        }
        assert "ix_projection_outbox_pending" in indexes
        plan = " ".join(
            str(row[3])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {QUERY}"
            ).fetchall()
        )
        assert "USING INDEX ix_projection_outbox_pending" in plan
    finally:
        connection.close()

    restarted = SQLiteStore(database)
    await restarted.start()
    try:
        batch = await restarted.load_projection_batch(max_items=100)
        assert [item.projection_id for item in batch] == ["pending-1"]
        snapshot = await restarted.outbox_snapshot()
        assert snapshot["pending_count"] == 1
    finally:
        await restarted.close()
