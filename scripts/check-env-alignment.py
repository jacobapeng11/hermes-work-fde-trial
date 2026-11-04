#!/usr/bin/env python3
"""Read-only, value-free environment contract and Git-boundary check."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import stat
import subprocess
import sys


KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
DEV_REQUIRED = {
    "APP_ENV",
    "RUNTIME_CHAT_MODE",
    "HERMES_AUTH_MODE",
    "HERMES_ADMIN_AUTH_MODE",
    "BACKEND_HOST",
    "BACKEND_LOCAL_HOST",
    "BACKEND_PORT",
    "USER_WEB_HOST",
    "USER_WEB_PORT",
    "ADMIN_WEB_HOST",
    "ADMIN_WEB_PORT",
    "PUBLIC_WEB_BASE",
    "PUBLIC_ADMIN_BASE",
    "PUBLIC_API_BASE",
    "PUBLIC_NGINX_CONFIG",
    "PUBLIC_NGINX_DEV_CONFIG",
    "ENTERPRISE_KNOWLEDGE_HOST_PATH",
}
PROD_REQUIRED = DEV_REQUIRED | {
    "PUBLIC_RELEASE_ROOT",
    "PUBLIC_NGINX_CONFIG",
    "PUBLIC_NGINX_DEV_CONFIG",
    "HERMES_RUNTIME_STORAGE_SOURCE",
    "HERMES_RUNTIME_STORAGE_UUID",
    "HERMES_RUNTIME_STORAGE_FSTYPE",
    "HERMES_RUNTIME_STORAGE_TARGET",
    "HERMES_RUNTIME_IMAGE",
}


def _keys(path: Path) -> tuple[set[str], set[str]]:
    found: set[str] = set()
    duplicates: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = KEY_RE.match(line.strip())
        if not match:
            continue
        key = match.group(1)
        if key in found:
            duplicates.add(key)
        found.add(key)
    return found, duplicates


def _tracked_env_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    paths: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        name = raw.decode("utf-8", errors="replace").rsplit("/", 1)[-1]
        if name == ".env" or name.startswith(".env."):
            paths.append(raw.decode("utf-8", errors="replace"))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Check one private env contract without printing values")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--mode", choices=("dev", "prod"), default="prod")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()

    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    env_file = (args.env_file or root / "backend/.env").resolve()
    errors = 0
    if not env_file.is_file():
        print(f"missing: {env_file}")
        return 2

    mode = stat.S_IMODE(env_file.stat().st_mode)
    if mode & 0o077:
        print(f"permissions: {env_file} must not be group/other readable or writable")
        errors = 1

    keys, duplicates = _keys(env_file)
    required = PROD_REQUIRED if args.mode == "prod" else DEV_REQUIRED
    for key in sorted(required - keys):
        print(f"missing: {key}")
        errors = 1
    for key in sorted(duplicates):
        print(f"duplicate: {key}")
        errors = 1

    tracked = _tracked_env_paths(root)
    for path in tracked:
        print(f"git-tracked-env: {path}")
        errors = 1

    if errors:
        return 1
    print(f"PASS: {env_file} ({args.mode}, {len(keys)} keys; values withheld)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
