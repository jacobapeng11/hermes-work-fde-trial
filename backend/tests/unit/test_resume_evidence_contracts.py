"""Repeatable contracts behind the resume-facing technical claims.

These tests deliberately exercise product boundaries rather than report an
artificial performance number.  Cluster-scale and external parsing outcomes
are measured by their separate staging smoke runs.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services.personal_knowledge import ALLOWED_EXTENSIONS
from app.services.personal_knowledge_acl import role_allows
from app.services.plaza.user_skill_operations.operation_queue import UserSkillOperationQueue


async def _wait_for(predicate, *, timeout_seconds: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("timed out waiting for queued operations")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_resume_queue_evidence_serializes_mixed_workspace_operations() -> None:
    """One Runtime must not reload/configure two assets at the same time."""

    queue = UserSkillOperationQueue()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    completed: list[str] = []
    started: list[str] = []
    active_handlers = 0
    max_active_handlers = 0

    async def handler(snapshot) -> None:
        nonlocal active_handlers, max_active_handlers
        active_handlers += 1
        max_active_handlers = max(max_active_handlers, active_handlers)
        started.append(snapshot.target_key)
        if snapshot.target_key == "mcp:A":
            first_started.set()
            await release_first.wait()
        await asyncio.sleep(0)
        completed.append(snapshot.target_key)
        active_handlers -= 1

    await queue.submit(
        workspace_id=119,
        action="mcp_install",
        target_key="mcp:A",
        handler=handler,
    )
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await queue.submit(
        workspace_id=119,
        action="mcp_install",
        target_key="mcp:B",
        handler=handler,
    )
    await queue.submit(
        workspace_id=119,
        action="skill_install",
        target_key="skill:C",
        handler=handler,
    )

    assert started == ["mcp:A"]
    release_first.set()
    await _wait_for(lambda: len(completed) == 3)

    assert max_active_handlers == 1
    assert started == ["mcp:A", "mcp:B", "skill:C"]
    assert completed == started
    await asyncio.sleep(0.12)


@pytest.mark.asyncio
async def test_resume_queue_evidence_coalesces_ten_identical_requests() -> None:
    """A burst of identical user actions should produce one real operation."""

    queue = UserSkillOperationQueue()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(snapshot) -> None:
        calls.append(snapshot.operation_id)
        started.set()
        await release.wait()

    submissions = await asyncio.gather(*[
        queue.submit(
            workspace_id=119,
            action="skill_install",
            target_key="asset:8",
            handler=handler,
        )
        for _ in range(10)
    ])
    operation_ids = {item.operation_id for item in submissions}

    assert len(operation_ids) == 1
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert calls == [next(iter(operation_ids))]
    release.set()
    await _wait_for(lambda: not queue.pending(119))
    await asyncio.sleep(0.12)


def test_resume_knowledge_upload_evidence_keeps_format_and_size_contract() -> None:
    assert ALLOWED_EXTENSIONS == {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".txt",
        ".md",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
    }
    assert settings.PERSONAL_KNOWLEDGE_MAX_UPLOAD_MB == 100


@pytest.mark.parametrize(
    ("role", "capability", "allowed"),
    [
        ("retrieve", "retrieve", True),
        ("retrieve", "preview", False),
        ("preview", "preview", True),
        ("preview", "download", False),
        ("download", "download", True),
        ("owner", "manage", True),
    ],
)
def test_resume_knowledge_permission_evidence_covers_capability_ladder(
    role: str,
    capability: str,
    allowed: bool,
) -> None:
    assert role_allows(role, capability) is allowed
