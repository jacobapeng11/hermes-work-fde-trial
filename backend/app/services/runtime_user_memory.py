from __future__ import annotations

import fcntl
import os
import tempfile
import unicodedata
from pathlib import Path


PLATFORM_IDENTITY_BEGIN = "<!-- PLATFORM_IDENTITY_BEGIN -->"
PLATFORM_IDENTITY_END = "<!-- PLATFORM_IDENTITY_END -->"
PLATFORM_FILE_DELIVERY_BEGIN = "<!-- PLATFORM_FILE_DELIVERY_BEGIN -->"
PLATFORM_FILE_DELIVERY_END = "<!-- PLATFORM_FILE_DELIVERY_END -->"

_LEGACY_UNSAFE_FILE_DELIVERY_PREFERENCE = (
    "用户家目录是 /opt/data，所有交付给用户的文件应放在此目录下，而非 /data/workspace。"
)


class RuntimeUserMemoryError(RuntimeError):
    """Raised when platform-managed runtime memory cannot be updated safely."""


def _validate_identity_value(label: str, value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or any(
            unicodedata.category(character).startswith("C")
            for character in normalized
        )
        or PLATFORM_IDENTITY_BEGIN in normalized
        or PLATFORM_IDENTITY_END in normalized
    ):
        raise RuntimeUserMemoryError(f"invalid {label}")
    return normalized


def render_platform_identity(*, name: str, user_code: str) -> str:
    normalized_name = _validate_identity_value("user name", name)
    normalized_user_code = _validate_identity_value("user code", user_code)
    return (
        f"{PLATFORM_IDENTITY_BEGIN}\n"
        f"姓名：{normalized_name}\n"
        f"工号：{normalized_user_code}\n"
        f"显示身份：{normalized_name}（{normalized_user_code}）\n"
        f"{PLATFORM_IDENTITY_END}"
    )


def render_platform_file_delivery_guidance() -> str:
    return (
        f"{PLATFORM_FILE_DELIVERY_BEGIN}\n"
        "## 文件交付规则\n\n"
        "- 默认将交付文件保存到当前会话工作目录；优先使用相对路径。\n"
        "- 不要把 `HOME`、`HERMES_HOME` 或 `/opt/data` 作为交付目录。\n"
        "- 仅在用户明确指定其他目标路径时才使用该路径。\n"
        f"{PLATFORM_FILE_DELIVERY_END}"
    )


def _managed_block_bounds(
    content: str,
    *,
    begin_marker: str,
    end_marker: str,
    label: str,
) -> tuple[int, int] | None:
    begin_count = content.count(begin_marker)
    end_count = content.count(end_marker)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise RuntimeUserMemoryError(f"invalid {label} markers")

    begin = content.find(begin_marker)
    end_marker_index = content.find(end_marker)
    if begin < 0 or end_marker_index <= begin:
        raise RuntimeUserMemoryError(f"invalid {label} markers")
    return begin, end_marker_index + len(end_marker)


def _replace_managed_block(
    content: str,
    *,
    begin_marker: str,
    end_marker: str,
    label: str,
    replacement: str,
) -> str:
    bounds = _managed_block_bounds(
        content,
        begin_marker=begin_marker,
        end_marker=end_marker,
        label=label,
    )
    if bounds is None:
        return content
    begin, end = bounds
    return content[:begin] + replacement + content[end:]


def merge_platform_soul(content: str, *, name: str, user_code: str) -> str:
    """Put platform-owned identity blocks into Hermes' stable prompt file."""

    identity = render_platform_identity(name=name, user_code=user_code)
    delivery = render_platform_file_delivery_guidance()
    merged = _replace_managed_block(
        content,
        begin_marker=PLATFORM_IDENTITY_BEGIN,
        end_marker=PLATFORM_IDENTITY_END,
        label="platform identity",
        replacement=identity,
    )
    merged = _replace_managed_block(
        merged,
        begin_marker=PLATFORM_FILE_DELIVERY_BEGIN,
        end_marker=PLATFORM_FILE_DELIVERY_END,
        label="platform file delivery",
        replacement=delivery,
    )
    identity_bounds = _managed_block_bounds(
        merged,
        begin_marker=PLATFORM_IDENTITY_BEGIN,
        end_marker=PLATFORM_IDENTITY_END,
        label="platform identity",
    )
    delivery_bounds = _managed_block_bounds(
        merged,
        begin_marker=PLATFORM_FILE_DELIVERY_BEGIN,
        end_marker=PLATFORM_FILE_DELIVERY_END,
        label="platform file delivery",
    )
    if (identity_bounds is None) != (delivery_bounds is None):
        raise RuntimeUserMemoryError("incomplete platform identity markers")
    if identity_bounds is not None:
        return merged

    title, separator, remainder = merged.partition("\n\n")
    if not separator:
        return f"{merged}\n\n{identity}\n\n{delivery}\n"
    return f"{title}\n\n{identity}\n\n{delivery}\n\n{remainder}"


def strip_platform_managed_blocks(content: str) -> str:
    """Remove legacy platform blocks from Hermes-owned USER.md content."""

    for begin, end, label in (
        (PLATFORM_IDENTITY_BEGIN, PLATFORM_IDENTITY_END, "platform identity"),
        (
            PLATFORM_FILE_DELIVERY_BEGIN,
            PLATFORM_FILE_DELIVERY_END,
            "platform file delivery",
        ),
    ):
        bounds = _managed_block_bounds(
            content,
            begin_marker=begin,
            end_marker=end,
            label=label,
        )
        if bounds is not None:
            begin_index, end_index = bounds
            content = content[:begin_index] + content[end_index:]

    content = content.replace(
        f"§\n{_LEGACY_UNSAFE_FILE_DELIVERY_PREFERENCE}\n",
        "",
    )
    content = content.replace(
        f"§\n{_LEGACY_UNSAFE_FILE_DELIVERY_PREFERENCE}",
        "",
    )
    content = content.replace(
        f"{_LEGACY_UNSAFE_FILE_DELIVERY_PREFERENCE}\n",
        "",
    ).replace(_LEGACY_UNSAFE_FILE_DELIVERY_PREFERENCE, "")
    while "\n\n\n" in content:
        content = content.replace("\n\n\n", "\n\n")
    return content.strip()


class RuntimeUserMemoryWriter:
    """Initialize Hermes-owned memory and migrate legacy platform blocks."""

    def ensure(self, home: Path, *, uid: int, gid: int) -> None:
        memories = home / "memories"
        try:
            memories.mkdir(parents=True, exist_ok=True)
            lock_fd = os.open(
                memories / ".platform-memory.lock",
                os.O_RDWR | os.O_CREAT,
                0o660,
            )
            try:
                os.fchmod(lock_fd, 0o660)
                os.fchown(lock_fd, uid, gid)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                self._migrate_user(
                    memories / "USER.md",
                    uid=uid,
                    gid=gid,
                )
                self._create_memory(
                    memories / "MEMORY.md",
                    uid=uid,
                    gid=gid,
                )
            finally:
                os.close(lock_fd)
        except RuntimeUserMemoryError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RuntimeUserMemoryError(
                "cannot update runtime user memory"
            ) from exc

    def _migrate_user(
        self,
        path: Path,
        *,
        uid: int,
        gid: int,
    ) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if metadata.st_mode & 0o170000 != 0o100000:
            raise RuntimeUserMemoryError("USER.md is not a regular file")

        existing = path.read_text(encoding="utf-8")
        migrated = strip_platform_managed_blocks(existing)
        if migrated == existing:
            return
        self._atomic_replace(path, migrated, uid=uid, gid=gid)

    def _create_memory(self, path: Path, *, uid: int, gid: int) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o660,
            )
        except FileExistsError:
            return
        try:
            os.fchmod(descriptor, 0o660)
            os.fchown(descriptor, uid, gid)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write("# Long-term Memory\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._fsync_directory(path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _atomic_replace(
        path: Path,
        content: str,
        *,
        uid: int,
        gid: int,
    ) -> None:
        descriptor: int | None = None
        temp_path: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".USER.",
                suffix=".tmp",
                dir=path.parent,
            )
            temp_path = Path(raw_path)
            os.fchmod(descriptor, 0o660)
            os.fchown(descriptor, uid, gid)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(content)
                if content:
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
            temp_path = None
            RuntimeUserMemoryWriter._fsync_directory(path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
