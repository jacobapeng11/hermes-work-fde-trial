from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


CONTRACT_VERSION = "hermes-posix-child-access-probe-v1"
MAX_REQUEST_BYTES = 128 * 1024


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _load_request() -> dict[str, object]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("probe request size is invalid")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {
        "contract_version",
        "expected_gid",
        "expected_groups",
        "expected_uid",
        "paths",
        "pvc_root",
    }:
        raise ValueError("probe request shape is invalid")
    if value["contract_version"] != CONTRACT_VERSION:
        raise ValueError("probe request contract is invalid")
    return value


def _probe(
    value: dict[str, object],
    *,
    normalize_owned: bool = False,
) -> dict[str, object]:
    expected_uid = value["expected_uid"]
    expected_gid = value["expected_gid"]
    expected_groups = value["expected_groups"]
    if (
        type(expected_uid) is not int
        or type(expected_gid) is not int
        or not isinstance(expected_groups, list)
        or any(type(item) is not int for item in expected_groups)
        or os.getuid() != expected_uid
        or os.geteuid() != expected_uid
        or os.getgid() != expected_gid
        or os.getegid() != expected_gid
        or os.getgroups() != expected_groups
    ):
        raise PermissionError("probe process identity does not match")
    pvc_root = Path(str(value["pvc_root"]))
    if not pvc_root.is_absolute():
        raise ValueError("probe PVC root is not absolute")
    raw_paths = value["paths"]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("probe path list is invalid")
    results: list[dict[str, object]] = []
    for item in raw_paths:
        if not isinstance(item, dict) or set(item) != {
            "expected_gid",
            "expected_mode",
            "expected_uid",
            "path_key",
            "path_kind",
            "relative_path",
        }:
            raise ValueError("probe path shape is invalid")
        relative = Path(str(item["relative_path"]))
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ValueError("probe path is unsafe")
        path = pvc_root if str(relative) == "." else pvc_root / relative
        metadata = os.lstat(path)
        directory = item["path_kind"] in {
            "runtime_home",
            "profiles_parent",
            "profile_home",
            "managed_directory",
        }
        if directory is not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("probe path kind does not match")
        if normalize_owned:
            if (
                metadata.st_uid != expected_uid
                or item["expected_uid"] != expected_uid
                or item["expected_gid"] not in {expected_gid, *expected_groups}
                or type(item["expected_mode"]) is not int
                or item["expected_mode"] < 0
                or item["expected_mode"] > 0o3777
            ):
                raise PermissionError("owned normalization target is invalid")
        flags = os.O_CLOEXEC | os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= os.O_DIRECTORY
        elif item["path_kind"] in {"soul", "sqlite", "managed_file"}:
            flags = os.O_CLOEXEC | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeError("probe path changed during open")
            if normalize_owned:
                os.fchown(descriptor, -1, int(item["expected_gid"]))
                os.fchmod(descriptor, int(item["expected_mode"]))
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if normalize_owned:
            parent = path.parent
            parent_fd = os.open(
                parent,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            metadata = os.lstat(path)
        if (
            metadata.st_uid != item["expected_uid"]
            or metadata.st_gid != item["expected_gid"]
            or stat.S_IMODE(metadata.st_mode) != item["expected_mode"]
        ):
            raise PermissionError("probe path metadata does not match")
        results.append(
            {
                "device": metadata.st_dev,
                "gid": metadata.st_gid,
                "inode": metadata.st_ino,
                "mode": stat.S_IMODE(metadata.st_mode),
                "path_key": item["path_key"],
                "uid": metadata.st_uid,
            }
        )
    unsigned = {
        "contract_version": CONTRACT_VERSION,
        "gid": os.getgid(),
        "groups": os.getgroups(),
        "paths": results,
        "uid": os.getuid(),
    }
    return {**unsigned, "probe_digest": _digest(unsigned)}


def main() -> int:
    try:
        if sys.argv[1:] == []:
            normalize_owned = False
        elif sys.argv[1:] == ["--normalize-owned"]:
            normalize_owned = True
        else:
            raise ValueError("probe arguments are invalid")
        response = _probe(
            _load_request(),
            normalize_owned=normalize_owned,
        )
    except Exception as exc:
        sys.stderr.write(type(exc).__name__ + "\n")
        return 1
    sys.stdout.buffer.write(_canonical_json(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
