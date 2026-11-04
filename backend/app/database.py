from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,  # Detect stale/corrupt connections before use
    pool_recycle=1800,  # Recycle connections every 30 min (below MySQL wait_timeout)
    pool_size=20,  # Limit persistent connections
    max_overflow=10,  # Allow up to 10 overflow connections
    pool_timeout=30,  # Timeout for getting connections from pool
    pool_reset_on_return="rollback",  # Ensure connections are reset before returning to pool
    connect_args={
        "connect_timeout": 10,
    },
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Patch asyncmy dialect so pool_pre_ping correctly discards corrupted
# connections instead of raising errors to the application.
#
# asyncmy >= 0.2.10 added _close_on_cancel() which closes the transport and
# sets _connected=False when a read is interrupted by CancelledError.  The
# subsequent ping(reconnect=False) raises Error("Already closed"), but
# SQLAlchemy's default is_disconnect() does NOT recognise this message as a
# disconnect — so pool_pre_ping propagates the error instead of discarding
# the connection.  The same gap exists for "Cancelled during execution"
# (the _close_reason) and "Packet sequence number wrong" (protocol desync).
#
# By extending is_disconnect we make pool_pre_ping transparently recycle
# these connections: the pool discards the bad one, creates a fresh
# connection, and the application never sees the error.
# ---------------------------------------------------------------------------
_ASYNCMY_CORRUPTION_MARKERS = (
    "already closed",
    "cancelled during execution",
    "packet sequence number wrong",
)


def _install_is_disconnect_patch() -> None:
    try:
        from sqlalchemy.dialects.mysql.asyncmy import MySQLDialect_asyncmy
    except ImportError:
        return

    _original = MySQLDialect_asyncmy.is_disconnect

    def _patched_is_disconnect(self, e, connection, cursor):  # type: ignore[override]
        if _original(self, e, connection, cursor):
            return True
        str_e = str(e).lower()
        return any(marker in str_e for marker in _ASYNCMY_CORRUPTION_MARKERS)

    MySQLDialect_asyncmy.is_disconnect = _patched_is_disconnect


_install_is_disconnect_patch()


# Safety net: if a "Packet sequence number wrong" error slips past
# pool_pre_ping (e.g. cancellation between pre_ping and the real query),
# invalidate the connection so it is discarded rather than reused.
@event.listens_for(engine.sync_engine, "handle_error")
def _handle_db_error(context):
    """Invalidate connection on packet sequence errors."""
    if context.is_disconnect:
        return
    error_message = str(context.original_exception)
    if "Packet sequence number wrong" in error_message:
        context.connection.invalidate()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def reset_connection_pool() -> None:
    """Dispose all connections in the pool and recreate them.

    Use this to clear corrupted connections after detecting packet sequence errors.
    """
    await engine.dispose()
