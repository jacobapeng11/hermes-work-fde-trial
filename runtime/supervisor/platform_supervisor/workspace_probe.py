"""Probe managed Session directories with the real Hermes child identity."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys
from typing import Iterable


class WorkspaceProbeError(RuntimeError):
    """The actual Hermes process identity cannot safely write a directory."""


def probe_directory(path: Path) -> None:
    directory_fd: int | None = None
    probe_fd: int | None = None
    probe_name = ".platform-workspace-probe-" + secrets.token_hex(12)
    created = False
    try:
        directory_fd = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        probe_fd = os.open(
            probe_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        created = True
        os.fsync(probe_fd)
        os.close(probe_fd)
        probe_fd = None
        os.unlink(probe_name, dir_fd=directory_fd)
        created = False
        os.fsync(directory_fd)
    except OSError as exc:
        raise WorkspaceProbeError("managed Session workspace is not writable") from exc
    finally:
        if probe_fd is not None:
            os.close(probe_fd)
        if created and directory_fd is not None:
            try:
                os.unlink(probe_name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)


def probe_directories(paths: Iterable[Path]) -> None:
    for path in paths:
        probe_directory(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="probe managed Session workspace directories"
    )
    parser.add_argument(
        "--directory",
        action="append",
        required=True,
        type=Path,
        dest="directories",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        probe_directories(args.directories)
    except WorkspaceProbeError:
        print("managed Session workspace is not writable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
