from __future__ import annotations

import argparse
import ctypes
import errno
import os
import sys
from typing import NoReturn, Sequence


_PR_SET_DUMPABLE = 4
_PR_SET_KEEPCAPS = 8
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522


class HermesChildIsolationError(RuntimeError):
    pass


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("pid", ctypes.c_int),
    ]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def drop_to_hermes_child(
    *,
    uid: int,
    gid: int,
    supplemental_gid: int,
) -> None:
    """Drop the Supervisor-only identity and every capability in fixed order."""

    if sys.platform != "linux":
        raise HermesChildIsolationError("Hermes child isolation requires Linux")
    if min(uid, gid, supplemental_gid) <= 0 or uid == 10000 or gid == 10000:
        raise HermesChildIsolationError("Hermes child identity is invalid")
    libc = ctypes.CDLL(None, use_errno=True)
    _prctl(libc, _PR_SET_KEEPCAPS, 0, label="disable keepcaps")
    try:
        os.setgroups([supplemental_gid])
        os.setgid(gid)
        os.setuid(uid)
    except OSError as exc:
        raise HermesChildIsolationError(
            "cannot drop to the Hermes child identity"
        ) from exc
    header = _CapabilityHeader(version=_LINUX_CAPABILITY_VERSION_3, pid=0)
    data = (_CapabilityData * 2)()
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        error_number = ctypes.get_errno()
        raise HermesChildIsolationError(
            "cannot clear Hermes child capabilities"
        ) from OSError(error_number or errno.EPERM, os.strerror(error_number))
    ambient_result = libc.prctl(
        _PR_CAP_AMBIENT,
        _PR_CAP_AMBIENT_CLEAR_ALL,
        0,
        0,
        0,
    )
    if ambient_result != 0 and ctypes.get_errno() not in {0, errno.EINVAL}:
        error_number = ctypes.get_errno()
        raise HermesChildIsolationError(
            "cannot clear Hermes child ambient capabilities"
        ) from OSError(error_number or errno.EPERM, os.strerror(error_number))
    _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1, label="set no-new-privileges")
    _prctl(libc, _PR_SET_DUMPABLE, 0, label="disable child dumpability")
    os.umask(0o007)
    if (
        os.getuid() != uid
        or os.geteuid() != uid
        or os.getgid() != gid
        or os.getegid() != gid
        or os.getgroups() != [supplemental_gid]
    ):
        raise HermesChildIsolationError(
            "Hermes child identity did not settle to the closed contract"
        )


def exec_hermes_child(
    argv: Sequence[str],
    *,
    uid: int,
    gid: int,
    supplemental_gid: int,
) -> NoReturn:
    if not argv or not argv[0].startswith("/"):
        raise HermesChildIsolationError(
            "Hermes child executable must be an absolute path"
        )
    drop_to_hermes_child(
        uid=uid,
        gid=gid,
        supplemental_gid=supplemental_gid,
    )
    os.execve(argv[0], list(argv), dict(os.environ))


def _prctl(
    libc: ctypes.CDLL,
    operation: int,
    value: int,
    *,
    label: str,
) -> None:
    if libc.prctl(operation, value, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise HermesChildIsolationError(f"cannot {label}") from OSError(
            error_number or errno.EPERM,
            os.strerror(error_number or errno.EPERM),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--supplemental-gid", required=True, type=int)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main() -> NoReturn:
    arguments = _parser().parse_args()
    argv = list(arguments.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    exec_hermes_child(
        argv,
        uid=arguments.uid,
        gid=arguments.gid,
        supplemental_gid=arguments.supplemental_gid,
    )


if __name__ == "__main__":
    main()
