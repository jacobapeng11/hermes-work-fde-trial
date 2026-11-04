#!/usr/bin/env python3
"""Move uv's root-private Python base behind the production Hermes venv."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat


VENV = Path("/opt/hermes/.venv")
DESTINATION = Path("/opt/hermes-platform/python")
UV_PYTHON_ROOT = Path("/root/.local/share/uv/python")


def main() -> None:
    interpreter_link = VENV / "bin/python"
    source_interpreter = interpreter_link.resolve(strict=True)
    source_base = source_interpreter.parent.parent
    if not stat.S_ISREG(os.stat(source_interpreter).st_mode):
        raise RuntimeError("upstream uv Python identity is not relocatable")

    # A formally published Active Release has already relocated and hardened
    # this interpreter. Re-overlaying that verified parent must preserve the
    # same regular executable instead of trying to relocate the venv again.
    if source_interpreter == interpreter_link:
        config = (VENV / "pyvenv.cfg").read_text(encoding="utf-8")
        if (
            "home = /opt/hermes-platform/python/bin" not in config
            or "executable = /opt/hermes-platform/python/bin/python3.11" not in config
            or not (DESTINATION / "lib").is_dir()
        ):
            raise RuntimeError("existing Hermes venv relocation identity is invalid")
        os.chmod(interpreter_link, 0o555, follow_symlinks=False)
        return

    if source_base.is_relative_to(UV_PYTHON_ROOT):
        if not (source_base / "lib").is_dir() or DESTINATION.exists():
            raise RuntimeError("upstream uv Python identity is not relocatable")
        shutil.copytree(source_base, DESTINATION, symlinks=True)
        relocated = DESTINATION / source_interpreter.relative_to(source_base)
        relocated_home = DESTINATION / "bin"
    elif source_interpreter.is_relative_to(Path("/usr/bin")):
        # The fixed 0.19.1 source-gate base uses the distribution Python.  Its
        # base is already world-readable, so only replace the venv's symlink
        # with an immutable regular executable; copying all of /usr would both
        # be wasteful and change the interpreter's stdlib identity.
        relocated = source_interpreter
        relocated_home = source_interpreter.parent
    else:
        raise RuntimeError("upstream Python identity is not relocatable")
    if not stat.S_ISREG(os.stat(relocated).st_mode):
        raise RuntimeError("relocated Python interpreter is not a regular file")

    staged_interpreter = interpreter_link.with_name(".python.platform-relocate")
    shutil.copy2(relocated, staged_interpreter, follow_symlinks=False)
    interpreter_link.unlink()
    staged_interpreter.replace(interpreter_link)
    os.chmod(interpreter_link, 0o555, follow_symlinks=False)
    if not stat.S_ISREG(os.lstat(interpreter_link).st_mode):
        raise RuntimeError("Hermes venv Python must be a regular file")

    config_path = VENV / "pyvenv.cfg"
    config = config_path.read_text(encoding="utf-8").splitlines()
    replacements = {
        "home": str(relocated_home),
        "executable": str(relocated),
    }
    rewritten: list[str] = []
    seen: set[str] = set()
    for line in config:
        key, separator, _value = line.partition("=")
        normalized = key.strip()
        if separator and normalized in replacements:
            rewritten.append(f"{normalized} = {replacements[normalized]}")
            seen.add(normalized)
        else:
            rewritten.append(line)
    for key in ("home", "executable"):
        if key not in seen:
            rewritten.append(f"{key} = {replacements[key]}")
    config_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    if Path(os.path.realpath(interpreter_link)) != interpreter_link:
        raise RuntimeError("Hermes venv Python unexpectedly resolves through a link")


if __name__ == "__main__":
    main()
