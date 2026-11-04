from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check-runtime-vdb-storage-mounts.sh"


def _run(
    tmp_path: Path,
    *,
    source: str = "/dev/vda4",
    uuid: str = "uuid-84",
    fstype: str = "ext4",
    mounted_uuid: str | None = None,
    mounted_fstype: str | None = None,
    mounted_source: str | None = None,
    target: Path | None = None,
    extra_env: dict[str, str] | None = None,
    create_target: bool = True,
) -> subprocess.CompletedProcess[str]:
    target = target or (tmp_path / "data" / "hermes")
    if create_target:
        target.mkdir(parents=True, exist_ok=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    findmnt = fake_bin / "findmnt"
    findmnt.write_text(
        "#!/usr/bin/env bash\n"
        "case \" $* \" in\n"
        "  *' -o SOURCE '* ) printf '%s\\n' \"${SOURCE_VALUE:-}\";;\n"
        "  *' -o UUID '* ) printf '%s\\n' \"${MOUNT_UUID:-}\";;\n"
        "  *' -o FSTYPE '* ) printf '%s\\n' \"${MOUNT_FSTYPE:-}\";;\n"
        "esac\n",
    )
    findmnt.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HERMES_RUNTIME_STORAGE_TARGET": str(target),
        "HERMES_RUNTIME_STORAGE_SOURCE": source,
        "HERMES_RUNTIME_STORAGE_UUID": uuid,
        "HERMES_RUNTIME_STORAGE_FSTYPE": fstype,
        "SOURCE_VALUE": mounted_source or source,
        "MOUNT_UUID": mounted_uuid or uuid,
        "MOUNT_FSTYPE": mounted_fstype or fstype,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), "hermes-data"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_storage_guard_accepts_root_filesystem_directory(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


def test_storage_guard_rejects_identity_mismatch(tmp_path: Path) -> None:
    result = _run(tmp_path, uuid="expected", mounted_uuid="other")
    assert result.returncode == 1
    assert "expected source=/dev/vda4 UUID=expected" in result.stderr
    assert "actual   source=/dev/vda4 UUID=other" in result.stderr


def test_storage_guard_rejects_source_mismatch(tmp_path: Path) -> None:
    result = _run(tmp_path, mounted_source="/dev/other")
    assert result.returncode == 1
    assert "expected source=/dev/vda4" in result.stderr
    assert "actual   source=/dev/other" in result.stderr


def test_storage_guard_rejects_fstype_mismatch(tmp_path: Path) -> None:
    result = _run(tmp_path, mounted_fstype="xfs")
    assert result.returncode == 1
    assert "expected source=/dev/vda4 UUID=uuid-84 FSTYPE=ext4" in result.stderr
    assert "actual   source=/dev/vda4 UUID=uuid-84 FSTYPE=xfs" in result.stderr


def test_storage_guard_rejects_missing_directory(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    target.mkdir()
    target.rmdir()
    result = _run(tmp_path, target=target, create_target=False)
    assert result.returncode == 1
    assert "target directory does not exist" in result.stderr


def test_storage_guard_rejects_unwritable_directory(tmp_path: Path) -> None:
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o555)
    try:
        result = _run(tmp_path, target=target)
    finally:
        target.chmod(0o755)
    assert result.returncode == 1
    assert "not readable and writable" in result.stderr


def test_storage_guard_fails_closed_without_new_identity(tmp_path: Path) -> None:
    target = tmp_path / "data" / "hermes"
    target.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HERMES_RUNTIME_STORAGE_")
        and not key.startswith("HERMES_ITS_")
    }
    env["HERMES_RUNTIME_STORAGE_TARGET"] = str(target)
    # Never allow this negative-path test to discover the checkout's private
    # backend/.env through the production fallback chain.
    env["HERMES_STORAGE_ENV_FILE"] = str(tmp_path / "missing-storage.env")
    result = subprocess.run(
        ["bash", str(SCRIPT), "hermes-data"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "HERMES_RUNTIME_STORAGE_SOURCE" in result.stderr


def test_storage_guard_does_not_read_legacy_its_variables() -> None:
    content = SCRIPT.read_text(encoding="utf-8")
    assert "HERMES_ITS_" not in content
    assert "mountpoint" not in content
    assert "require_its_bind" not in content
