from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.services.runtime_user_memory import (
    PLATFORM_FILE_DELIVERY_BEGIN,
    PLATFORM_FILE_DELIVERY_END,
    PLATFORM_IDENTITY_BEGIN,
    PLATFORM_IDENTITY_END,
    RuntimeUserMemoryError,
    RuntimeUserMemoryWriter,
    merge_platform_soul,
)


def test_writer_initializes_hermes_memory_without_user_profile(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    RuntimeUserMemoryWriter().ensure(
        home,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    memories = home / "memories"
    assert not (memories / "USER.md").exists()
    assert (memories / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Long-term Memory\n"
    )
    assert stat.S_IMODE((memories / "MEMORY.md").stat().st_mode) == 0o660


def test_writer_migrates_legacy_platform_blocks_from_user_profile(
    tmp_path: Path,
) -> None:
    memories = tmp_path / "home" / "memories"
    memories.mkdir(parents=True)
    user_path = memories / "USER.md"
    user_path.write_text(
        "# User Profile\n\n"
        f"{PLATFORM_IDENTITY_BEGIN}\n旧身份\n{PLATFORM_IDENTITY_END}\n\n"
        f"{PLATFORM_FILE_DELIVERY_BEGIN}\n旧规则\n"
        f"{PLATFORM_FILE_DELIVERY_END}\n\n"
        "## User Preferences\n\n喜欢简洁回答。\n",
        encoding="utf-8",
    )

    RuntimeUserMemoryWriter().ensure(
        tmp_path / "home",
        uid=os.getuid(),
        gid=os.getgid(),
    )

    content = user_path.read_text(encoding="utf-8")
    assert content == "# User Profile\n\n## User Preferences\n\n喜欢简洁回答。\n"
    assert PLATFORM_IDENTITY_BEGIN not in content
    assert PLATFORM_FILE_DELIVERY_BEGIN not in content
    assert stat.S_IMODE(user_path.stat().st_mode) == 0o660


def test_writer_removes_legacy_unsafe_delivery_preference(
    tmp_path: Path,
) -> None:
    memories = tmp_path / "home" / "memories"
    memories.mkdir(parents=True)
    user_path = memories / "USER.md"
    user_path.write_text(
        "# User Profile\n\n"
        "## User Preferences\n\n喜欢简洁回答。\n\n"
        "用户家目录是 /opt/data，所有交付给用户的文件应放在此目录下，而非 /data/workspace。",
        encoding="utf-8",
    )

    RuntimeUserMemoryWriter().ensure(
        tmp_path / "home",
        uid=os.getuid(),
        gid=os.getgid(),
    )

    content = user_path.read_text(encoding="utf-8")
    assert "喜欢简洁回答。" in content
    assert "所有交付给用户的文件应放在此目录下" not in content


@pytest.mark.parametrize(
    "contents",
    [
        f"{PLATFORM_IDENTITY_BEGIN}\n未闭合\n",
        PLATFORM_IDENTITY_END,
        (
            f"{PLATFORM_IDENTITY_BEGIN}\na\n{PLATFORM_IDENTITY_END}\n"
            f"{PLATFORM_IDENTITY_BEGIN}\nb\n{PLATFORM_IDENTITY_END}\n"
        ),
        f"{PLATFORM_FILE_DELIVERY_BEGIN}\n未闭合\n",
        f"{PLATFORM_FILE_DELIVERY_END}\n",
    ],
)
def test_writer_rejects_malformed_platform_markers_without_changing_file(
    tmp_path: Path,
    contents: str,
) -> None:
    memories = tmp_path / "home" / "memories"
    memories.mkdir(parents=True)
    user_path = memories / "USER.md"
    user_path.write_text(contents, encoding="utf-8")
    original = user_path.read_bytes()

    with pytest.raises(RuntimeUserMemoryError):
        RuntimeUserMemoryWriter().ensure(
            tmp_path / "home",
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert user_path.read_bytes() == original
    assert not (memories / "MEMORY.md").exists()


def test_writer_is_idempotent_and_leaves_no_temporary_files(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    writer = RuntimeUserMemoryWriter()

    for _ in range(2):
        writer.ensure(home, uid=os.getuid(), gid=os.getgid())

    memories = home / "memories"
    assert list(memories.glob(".USER.*.tmp")) == []
    assert (memories / "MEMORY.md").read_text(encoding="utf-8") == (
        "# Long-term Memory\n"
    )


def test_merge_platform_soul_puts_identity_in_stable_prompt() -> None:
    soul = merge_platform_soul(
        "# 数字人\n\n这是平台托管的数字人。\n",
        name="示例用户乙",
        user_code="employee-7",
    )

    assert soul.startswith("# 数字人\n\n")
    assert "姓名：示例用户乙" in soul
    assert "工号：employee-7" in soul
    assert PLATFORM_IDENTITY_BEGIN in soul
    assert PLATFORM_FILE_DELIVERY_BEGIN in soul
    assert "这是平台托管的数字人。" in soul

    assert merge_platform_soul(
        soul,
        name="新名",
        user_code="employee-8",
    ).count(PLATFORM_IDENTITY_BEGIN) == 1


@pytest.mark.parametrize(
    ("name", "user_code"),
    [
        ("", "employee-7"),
        ("伪造\n工号", "employee-7"),
        ("示例用户乙", "employee\r7"),
        ("张\u200b义", "employee-7"),
        ("示例用户乙", "employee\u200b7"),
        (PLATFORM_IDENTITY_BEGIN, "employee-7"),
        ("示例用户乙", PLATFORM_IDENTITY_END),
    ],
)
def test_merge_platform_soul_rejects_unsafe_identity_without_leaking_value(
    name: str,
    user_code: str,
) -> None:
    with pytest.raises(RuntimeUserMemoryError) as error:
        merge_platform_soul("# 数字人\n", name=name, user_code=user_code)

    if name:
        assert name not in str(error.value)
    if user_code:
        assert user_code not in str(error.value)
