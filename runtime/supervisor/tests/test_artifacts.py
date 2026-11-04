from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from platform_supervisor.execution.artifacts import (
    ArtifactWatcher,
    ArtifactWatcherPathError,
    session_artifact_root,
)
from platform_supervisor.execution.sessions import PrepareIdentity


def _identity(*, profile_id: str = "default") -> PrepareIdentity:
    return PrepareIdentity(
        runtime_id="runtime-1",
        session_id="session/one",
        profile_id=profile_id,
        model_id="7",
        config_revision="revision-1",
        admission_generation=11,
    )


def test_session_artifact_root_handles_normal_and_expert_paths(tmp_path: Path) -> None:
    assert session_artifact_root(tmp_path, _identity()) == (
        tmp_path / "sessions" / "session_one"
    )
    assert session_artifact_root(tmp_path, _identity(profile_id="expert-69")) == (
        tmp_path / "experts" / "69" / "sessions" / "session_one"
    )
    with pytest.raises(ArtifactWatcherPathError):
        session_artifact_root(tmp_path, _identity(profile_id="expert-invalid"))


@pytest.mark.asyncio
async def test_watcher_emits_stable_create_update_and_delete(tmp_path: Path) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = watcher.root / "report.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first")
    await watcher.observe_path(path)
    await asyncio.sleep(1.1)
    assert [event.event for event in events] == ["chat.artifact.created"]
    assert events[0].data["relative_path"] == "workspace/sessions/session_one/report.pdf"

    path.write_bytes(b"second version")
    await watcher.observe_path(path)
    await asyncio.sleep(1.1)
    assert [event.event for event in events] == [
        "chat.artifact.created",
        "chat.artifact.updated",
    ]

    path.unlink()
    await watcher.observe_path(path)
    await asyncio.sleep(1.1)
    assert [event.event for event in events] == [
        "chat.artifact.created",
        "chat.artifact.updated",
        "chat.artifact.deleted",
    ]
    await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_filters_internal_and_symlink_files(tmp_path: Path) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    watcher.root.mkdir(parents=True)
    for name in (".scratch", "draft.part", "state.sqlite"):
        path = watcher.root / name
        path.write_text("ignored")
        await watcher.observe_path(path)
    work = watcher.root / ".work" / "hidden.pdf"
    work.parent.mkdir()
    work.write_text("ignored")
    await watcher.observe_path(work)
    await asyncio.sleep(1.1)
    assert events == []
    await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_filters_nested_virtualenv_files(tmp_path: Path) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = watcher.root / ".venv" / "lib" / "python3.13" / "site-packages" / "openpyxl" / "__init__.pyc"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"internal")
    await watcher.observe_path(path)
    await asyncio.sleep(1.1)

    assert events == []
    await watcher.stop()


@pytest.mark.asyncio
async def test_unchanged_file_present_at_run_start_is_not_published(
    tmp_path: Path,
) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = watcher.root / "existing.pdf"
    path.parent.mkdir(parents=True)
    path.write_text("before")
    await watcher.start()
    await watcher.stop()

    assert events == []


@pytest.mark.asyncio
async def test_unchanged_file_is_not_republished_across_runs(
    tmp_path: Path,
) -> None:
    events = []

    async def emit(event):
        events.append(event)

    first = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = first.root / "existing.pptx"
    path.parent.mkdir(parents=True)
    path.write_text("before")

    await first.start()
    await first.stop()
    second = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    await second.start()
    await second.stop()

    assert events == []


@pytest.mark.asyncio
async def test_terminal_reconcile_updates_a_file_present_at_run_start(
    tmp_path: Path,
) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = watcher.root / "existing.pptx"
    path.parent.mkdir(parents=True)
    path.write_text("before")
    await watcher.start()
    path.write_text("after update")
    await watcher.stop()

    assert [event.event for event in events] == ["chat.artifact.updated"]


@pytest.mark.asyncio
async def test_terminal_reconcile_removes_a_file_present_at_run_start(
    tmp_path: Path,
) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = watcher.root / "existing.pptx"
    path.parent.mkdir(parents=True)
    path.write_text("before")
    await watcher.start()
    path.unlink()
    await watcher.stop()

    assert [event.event for event in events] == ["chat.artifact.deleted"]


@pytest.mark.asyncio
async def test_terminal_reconcile_creates_a_file_added_during_run(
    tmp_path: Path,
) -> None:
    events = []

    async def emit(event):
        events.append(event)

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    await watcher.start()
    path = watcher.root / "new.pptx"
    path.parent.mkdir(parents=True)
    path.write_text("new presentation")
    await watcher.stop()

    assert [event.event for event in events] == ["chat.artifact.created"]


@pytest.mark.asyncio
async def test_directory_move_out_removes_published_descendants(tmp_path: Path) -> None:
    events = []

    async def emit(event):
        events.append((event.event, event.data["relative_path"]))

    watcher = ArtifactWatcher(
        workspace_dir=tmp_path,
        identity=_identity(),
        emit=emit,
    )
    path = watcher.root / "reports" / "report.pdf"
    path.parent.mkdir(parents=True)
    path.write_text("pdf")
    await watcher.observe_path(path)
    await asyncio.sleep(1.1)
    path.parent.rename(watcher.root / "moved")
    await watcher.observe_path(watcher.root / "reports")
    await asyncio.sleep(1.1)
    assert events == [
        ("chat.artifact.created", "workspace/sessions/session_one/reports/report.pdf"),
        ("chat.artifact.deleted", "workspace/sessions/session_one/reports/report.pdf"),
    ]
    await watcher.stop()
