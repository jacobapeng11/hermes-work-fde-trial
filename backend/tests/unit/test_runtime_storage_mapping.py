from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.runtime_storage_mapping import (
    RuntimeStorageMappingError,
    RuntimeStorageMappingResolver,
)


class _Result:
    def __init__(self, row) -> None:
        self.row = row

    def one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row) -> None:
        self.row = row

    async def execute(self, _statement):
        return _Result(self.row)


def _scope() -> dict[str, int]:
    return {"org_id": 7, "user_id": 9, "runtime_workspace_id": 123}


def _row(root: Path, **overrides):
    profile = SimpleNamespace(id=123, org_id=7, user_id=9, status="active")
    binding = SimpleNamespace(
        org_id=7,
        user_id=9,
        runtime_workspace_id=123,
        status="ready",
        runtime_config_source="pvc",
        runtime_root_host_path=str(root / "7" / "employee-9"),
        hermes_home_sub_path="home",
        workspace_sub_path="workspace",
    )
    for key, value in overrides.items():
        setattr(binding, key, value)
    return binding, profile


@pytest.mark.asyncio
async def test_resolver_uses_persisted_runtime_root_and_subpaths(tmp_path: Path) -> None:
    root = tmp_path / "runtimes"
    resolver = RuntimeStorageMappingResolver(_Session(_row(root)), runtime_root=root)

    location = await resolver.resolve(**_scope())

    assert location.root == root / "7" / "employee-9"
    assert location.home == location.root / "home"
    assert location.workspace == location.root / "workspace"


@pytest.mark.asyncio
async def test_runtime_storage_requires_persisted_host_path(tmp_path: Path) -> None:
    root = tmp_path / "runtimes"
    resolver = RuntimeStorageMappingResolver(
        _Session(_row(root, runtime_root_host_path=None)),
        runtime_root=root,
    )

    with pytest.raises(
        RuntimeStorageMappingError,
        match="persisted runtime storage root is missing",
    ):
        await resolver.resolve(**_scope())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"org_id": 8},
        {"user_id": 10},
        {"runtime_workspace_id": 999},
        {"status": "stopped"},
        {"runtime_config_source": "configmap"},
    ],
)
async def test_resolver_rejects_unowned_or_unready_binding(tmp_path: Path, overrides: dict) -> None:
    resolver = RuntimeStorageMappingResolver(_Session(_row(tmp_path / "runtimes", **overrides)), runtime_root=tmp_path / "runtimes")

    with pytest.raises(RuntimeStorageMappingError):
        await resolver.resolve(**_scope())


@pytest.mark.asyncio
async def test_resolver_rejects_profile_not_active_or_mismatched(tmp_path: Path) -> None:
    root = tmp_path / "runtimes"
    binding, profile = _row(root)
    profile.status = "deleted"
    with pytest.raises(RuntimeStorageMappingError):
        await RuntimeStorageMappingResolver(_Session((binding, profile)), runtime_root=root).resolve(**_scope())

    profile.status = "active"
    profile.id = 999
    with pytest.raises(RuntimeStorageMappingError):
        await RuntimeStorageMappingResolver(_Session((binding, profile)), runtime_root=root).resolve(**_scope())


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["hermes_home_sub_path", "workspace_sub_path"])
@pytest.mark.parametrize("value", [".", "./", "/absolute", "../escape", "home/../../escape"])
async def test_resolver_rejects_unsafe_subpaths(tmp_path: Path, field: str, value: str) -> None:
    root = tmp_path / "runtimes"
    resolver = RuntimeStorageMappingResolver(_Session(_row(root, **{field: value})), runtime_root=root)

    with pytest.raises(RuntimeStorageMappingError):
        await resolver.resolve(**_scope())


@pytest.mark.asyncio
async def test_resolver_rejects_host_path_outside_root_or_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "runtimes"
    outside = tmp_path / "outside"
    outside.mkdir()
    resolver = RuntimeStorageMappingResolver(
        _Session(_row(root, runtime_root_host_path=str(outside))), runtime_root=root
    )
    with pytest.raises(RuntimeStorageMappingError):
        await resolver.resolve(**_scope())

    root.mkdir()
    (root / "7").symlink_to(outside, target_is_directory=True)
    resolver = RuntimeStorageMappingResolver(
        _Session(_row(root, runtime_root_host_path=str(root / "7" / "123"))), runtime_root=root
    )
    with pytest.raises(RuntimeStorageMappingError):
        await resolver.resolve(**_scope())
