from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Protocol


RUNTIME_IMAGE_KEY = "HERMES_RUNTIME_IMAGE"
RUNTIME_IMAGE_MANIFEST_DIGEST_KEY = "HERMES_RUNTIME_IMAGE_MANIFEST_DIGEST"
RUNTIME_IMAGE_CONFIG_ID_KEY = "HERMES_RUNTIME_IMAGE_CONFIG_ID"
_RUNTIME_IMAGE_KEYS = {
    RUNTIME_IMAGE_KEY,
    RUNTIME_IMAGE_MANIFEST_DIGEST_KEY,
    RUNTIME_IMAGE_CONFIG_ID_KEY,
}
_RETIRED_RUNTIME_KEYS = {"HERMES_RUNTIME_" + "GENERATION"}
_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)
_HERMES_IMAGE_RE = re.compile(
    r"^(?P<repository>[A-Za-z0-9._:/-]*hermes-agent)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})|"
    r"@sha256:(?P<digest>[0-9a-fA-F]{64}))$"
)


class RuntimeImageConfigError(RuntimeError):
    pass


class RuntimeImageConfigConflict(RuntimeImageConfigError):
    pass


@dataclass(frozen=True)
class RuntimeImageConfig:
    image: str
    manifest_digest: str | None = None
    config_id: str | None = None


class RuntimeImageConfigReader(Protocol):
    def read(self) -> RuntimeImageConfig: ...


class StaticRuntimeImageConfigReader:
    """Explicit dependency for isolated unit tests.

    Production wiring always uses :class:`RuntimeImageConfigStore`; keeping a
    tiny reader protocol lets service tests avoid mutating the repository
    ``backend/.env``.
    """

    def __init__(
        self,
        *,
        image: str,
        manifest_digest: str | None = None,
        config_id: str | None = None,
    ) -> None:
        self._config = RuntimeImageConfig(
            image=image,
            manifest_digest=manifest_digest,
            config_id=config_id,
        )

    def read(self) -> RuntimeImageConfig:
        return self._config


class RuntimeImageConfigStore:
    """Read and atomically update the authoritative Runtime image."""

    def __init__(
        self,
        env_path: Path | None = None,
        *,
        lock_path: Path | None = None,
    ) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        self.env_path = (env_path or repository_root / "backend" / ".env").resolve(strict=False)
        self.lock_path = (
            lock_path or repository_root / ".runtime" / "hermes-runtime-image-config.lock"
        ).resolve(strict=False)

    def read(self) -> RuntimeImageConfig:
        try:
            contents = self.env_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeImageConfigError(
                f"无法读取 Hermes Runtime 配置文件 {self.env_path}: {exc}"
            ) from exc
        return self._parse(contents)

    def replace(
        self,
        *,
        expected: RuntimeImageConfig,
        replacement: RuntimeImageConfig,
    ) -> RuntimeImageConfig:
        if replacement.image == expected.image:
            raise RuntimeImageConfigError("Hermes Runtime 替换镜像必须与当前镜像不同")
        self._validate_image(replacement.image)
        self._validate_identity_pair(replacement)

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    contents = self.env_path.read_text(encoding="utf-8")
                    current_stat = self.env_path.stat()
                except OSError as exc:
                    raise RuntimeImageConfigError(
                        f"无法读取 Hermes Runtime 配置文件 {self.env_path}: {exc}"
                    ) from exc
                current = self._parse(contents)
                if current != expected:
                    raise RuntimeImageConfigConflict(
                        "backend/.env 的 Hermes Runtime 配置已被其他操作修改"
                    )
                rendered = self._replace_assignments(contents, replacement)
                self._atomic_write(
                    rendered,
                    mode=stat.S_IMODE(current_stat.st_mode),
                )
                return replacement
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _parse(cls, contents: str) -> RuntimeImageConfig:
        values: dict[str, str] = {}
        for line_number, line in enumerate(contents.splitlines(), start=1):
            match = _ASSIGNMENT_RE.match(line)
            if match is None:
                continue
            key = match.group("key")
            if key not in _RUNTIME_IMAGE_KEYS:
                continue
            if key in values:
                raise RuntimeImageConfigError(f"backend/.env 第 {line_number} 行重复定义 {key}")
            values[key] = cls._literal_value(
                match.group("value"),
                key=key,
                line_number=line_number,
            )

        if RUNTIME_IMAGE_KEY not in values:
            raise RuntimeImageConfigError(
                f"backend/.env 缺少 Hermes Runtime 配置：{RUNTIME_IMAGE_KEY}"
            )

        image = values[RUNTIME_IMAGE_KEY]
        cls._validate_image(image)
        config = RuntimeImageConfig(
            image=image,
            manifest_digest=values.get(RUNTIME_IMAGE_MANIFEST_DIGEST_KEY),
            config_id=values.get(RUNTIME_IMAGE_CONFIG_ID_KEY),
        )
        cls._validate_identity_pair(config)
        return config

    @staticmethod
    def _literal_value(raw: str, *, key: str, line_number: int) -> str:
        value = raw.strip()
        if not value:
            raise RuntimeImageConfigError(f"backend/.env 第 {line_number} 行的 {key} 不能为空")
        if value[0] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise RuntimeImageConfigError(
                    f"backend/.env 第 {line_number} 行的 {key} 引号不完整"
                )
            value = value[1:-1]
        if not value or any(character.isspace() for character in value):
            raise RuntimeImageConfigError(
                f"backend/.env 第 {line_number} 行的 {key} 必须是单一字面值"
            )
        return value

    @staticmethod
    def _validate_image(image: str) -> None:
        if _HERMES_IMAGE_RE.fullmatch(image) is None:
            raise RuntimeImageConfigError(
                "HERMES_RUNTIME_IMAGE 必须是带 tag 或 sha256 digest 的 hermes-agent 镜像引用"
            )

    @staticmethod
    def _validate_identity_pair(config: RuntimeImageConfig) -> None:
        if bool(config.manifest_digest) != bool(config.config_id):
            raise RuntimeImageConfigError("Runtime 镜像 manifest digest 和 config ID 必须同时配置")
        for label, value in (
            (RUNTIME_IMAGE_MANIFEST_DIGEST_KEY, config.manifest_digest),
            (RUNTIME_IMAGE_CONFIG_ID_KEY, config.config_id),
        ):
            if value is not None and re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
                raise RuntimeImageConfigError(f"{label} 必须是 sha256 identity")

    @staticmethod
    def _replace_assignments(
        contents: str,
        replacement: RuntimeImageConfig,
    ) -> str:
        trailing_newline = contents.endswith("\n")
        lines = contents.splitlines()
        replacements = {RUNTIME_IMAGE_KEY: replacement.image}
        if replacement.manifest_digest and replacement.config_id:
            replacements.update(
                {
                    RUNTIME_IMAGE_MANIFEST_DIGEST_KEY: replacement.manifest_digest,
                    RUNTIME_IMAGE_CONFIG_ID_KEY: replacement.config_id,
                }
            )
        replaced: set[str] = set()
        retired_indexes: set[int] = set()
        for index, line in enumerate(lines):
            match = _ASSIGNMENT_RE.match(line)
            if match is None:
                continue
            key = match.group("key")
            if key in _RETIRED_RUNTIME_KEYS:
                retired_indexes.add(index)
                continue
            if key not in replacements:
                continue
            if key in replaced:
                raise RuntimeImageConfigError(f"backend/.env 重复定义 {key}")
            prefix = "export " if line.lstrip().startswith("export ") else ""
            indent = line[: len(line) - len(line.lstrip())]
            lines[index] = f"{indent}{prefix}{key}={replacements[key]}"
            replaced.add(key)
        missing = set(replacements).difference(replaced)
        identity_keys = {
            RUNTIME_IMAGE_MANIFEST_DIGEST_KEY,
            RUNTIME_IMAGE_CONFIG_ID_KEY,
        }
        missing_identity = missing.intersection(identity_keys)
        if missing_identity and replacement.manifest_digest and replacement.config_id:
            insert_at = next(
                index
                for index, line in enumerate(lines)
                if (
                    (match := _ASSIGNMENT_RE.match(line)) is not None
                    and match.group("key") == RUNTIME_IMAGE_KEY
                )
            )
            lines[insert_at + 1 : insert_at + 1] = [
                f"{RUNTIME_IMAGE_MANIFEST_DIGEST_KEY}={replacement.manifest_digest}",
                f"{RUNTIME_IMAGE_CONFIG_ID_KEY}={replacement.config_id}",
            ]
            missing.difference_update(identity_keys)
        if missing:
            raise RuntimeImageConfigError(
                f"backend/.env 缺少 Hermes Runtime 配置：{', '.join(sorted(missing))}"
            )
        rendered = "\n".join(
            line for index, line in enumerate(lines) if index not in retired_indexes
        )
        return rendered + ("\n" if trailing_newline else "")

    def _atomic_write(self, contents: str, *, mode: int) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.env_path.parent,
                prefix=f".{self.env_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.fchmod(handle.fileno(), mode)
                handle.write(contents)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.env_path)
            temporary_path = None
            directory_fd = os.open(self.env_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise RuntimeImageConfigError(
                f"无法原子更新 Hermes Runtime 配置文件 {self.env_path}: {exc}"
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def shared_runtime_image_config_store() -> RuntimeImageConfigStore:
    return RuntimeImageConfigStore()
