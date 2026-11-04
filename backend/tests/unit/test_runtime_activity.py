from __future__ import annotations

from app.services.runtime_activity import SqlAlchemyRuntimeActivityStore


class ScalarResult:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class CountingSession:
    def __init__(self, counts: list[int]) -> None:
        self.counts = list(counts)
        self.statements: list[object] = []
        self.rollbacks = 0

    async def execute(self, statement: object) -> ScalarResult:
        self.statements.append(statement)
        return ScalarResult(self.counts.pop(0))

    async def rollback(self) -> None:
        self.rollbacks += 1


async def test_activity_snapshot_counts_only_drain_blockers() -> None:
    session = CountingSession([5, 2])

    snapshot = await SqlAlchemyRuntimeActivityStore(session).get_snapshot(7)

    assert snapshot.run_count == 5
    assert snapshot.self_evolution_count == 2
    assert snapshot.idle is False
    assert session.rollbacks == 1

    sql = [
        str(statement.compile(compile_kwargs={"literal_binds": True}))
        for statement in session.statements
    ]
    assert "runtime_run.runtime_workspace_id = 7" in sql[0]
    for status in ("accepted", "pending", "running", "canceling", "cancel_failed"):
        assert f"'{status}'" in sql[0]
    assert "runtime_run.self_evolution_status = 'eligible'" in sql[1]
    assert "runtime_run.self_evolution_status = 'running'" in sql[1]
    assert "runtime_run.self_evolution_activation_deadline_at IS NOT NULL" in sql[1]
    assert "runtime_run.self_evolution_activation_deadline_at >=" in sql[1]
    assert sql[1].count("runtime_run.self_evolution_deadline_at IS NOT NULL") == 2
    assert sql[1].count("runtime_run.self_evolution_deadline_at >=") == 2
    assert "runtime_run.self_evolution_last_activity_at IS NOT NULL" in sql[1]
    assert "runtime_run.self_evolution_last_activity_at >=" in sql[1]
    assert "active_run_count" not in "\n".join(sql)


async def test_activity_snapshot_is_idle_when_authoritative_tables_are_empty() -> None:
    session = CountingSession([0, 0])

    snapshot = await SqlAlchemyRuntimeActivityStore(session).get_snapshot(42)

    assert snapshot.run_count == 0
    assert snapshot.self_evolution_count == 0
    assert snapshot.idle is True
    assert session.rollbacks == 1
