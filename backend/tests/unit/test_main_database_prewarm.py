from __future__ import annotations

from types import SimpleNamespace

import app.main as main


def test_cors_is_the_outer_asgi_boundary() -> None:
    middleware = [item.cls.__name__ for item in main.create_app().user_middleware]

    assert middleware[0] == "CORSMiddleware"


async def test_database_pool_prewarm_fills_base_pool(
    monkeypatch,
) -> None:
    state = SimpleNamespace(
        created=0,
        active=0,
        max_active=0,
        executed=0,
        exited=0,
    )

    class Connection:
        async def execute(self, _statement: object) -> None:
            state.executed += 1

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            state.active += 1
            state.max_active = max(state.max_active, state.active)
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            state.active -= 1
            state.exited += 1

    class Engine:
        pool = SimpleNamespace(size=lambda: 3)

        def connect(self) -> ConnectionContext:
            state.created += 1
            return ConnectionContext()

    monkeypatch.setattr(main, "engine", Engine())

    await main._prewarm_database_pool()

    assert state.created == 3
    assert state.executed == 3
    assert state.max_active == 3
    assert state.exited == 3
    assert state.active == 0


async def test_database_pool_prewarm_failure_does_not_block_startup(
    monkeypatch,
) -> None:
    state = SimpleNamespace(created=0, exited=0)

    class Connection:
        async def execute(self, _statement: object) -> None:
            if state.created == 2:
                raise RuntimeError("database unavailable")

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            state.exited += 1

    class Engine:
        pool = SimpleNamespace(size=lambda: 2)

        def connect(self) -> ConnectionContext:
            state.created += 1
            return ConnectionContext()

    monkeypatch.setattr(main, "engine", Engine())

    await main._prewarm_database_pool()

    assert state.created == 2
    assert state.exited == 2


async def test_database_pool_prewarm_pool_size_failure_uses_one_connection(
    monkeypatch,
) -> None:
    state = SimpleNamespace(created=0, executed=0)

    class Connection:
        async def execute(self, _statement: object) -> None:
            state.executed += 1

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class BrokenPool:
        def size(self) -> int:
            raise RuntimeError("boom")

    class Engine:
        pool = BrokenPool()

        def connect(self) -> ConnectionContext:
            state.created += 1
            return ConnectionContext()

    monkeypatch.setattr(main, "engine", Engine())

    await main._prewarm_database_pool()

    assert state.created == 1
    assert state.executed == 1
