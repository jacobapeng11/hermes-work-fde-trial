from __future__ import annotations

import argparse
import ctypes
import errno
import os
import sys
from typing import NoReturn, Sequence

from platform_supervisor.secret_handoff import prepare_root_secret_handoffs


SUPERVISOR_UID = 10000
SUPERVISOR_GID = 10000
WORKSPACE_GID = 1000
SUPERVISOR_EXECUTABLE = "/opt/hermes/.venv/bin/platform-supervisor"

_CAP_SETGID = 6
_CAP_SETUID = 7
_CAP_CHOWN = 0
_CAP_DAC_READ_SEARCH = 2
_CAP_FOWNER = 3
_CAP_KILL = 5
_RUNTIME_CAPABILITIES = (_CAP_KILL, _CAP_SETGID, _CAP_SETUID)
_LEGACY_METADATA_CAPABILITIES = (
    _CAP_CHOWN,
    _CAP_DAC_READ_SEARCH,
    _CAP_FOWNER,
)
_CAPABILITY_MASK = sum(1 << capability for capability in _RUNTIME_CAPABILITIES)
_LEGACY_GATE_CAPABILITY_MASK = _CAPABILITY_MASK | sum(
    1 << capability for capability in _LEGACY_METADATA_CAPABILITIES
)
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_IS_SET = 1
_PR_CAP_AMBIENT_RAISE = 2
_PR_CAP_AMBIENT_LOWER = 3
_PR_SET_DUMPABLE = 4
_PR_SET_KEEPCAPS = 8
_PR_SET_NO_NEW_PRIVS = 38


class SupervisorPrivilegeBootstrapError(RuntimeError):
    pass


class LegacyMetadataCapabilitySealError(RuntimeError):
    pass


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def drop_to_supervisor(
    *,
    retain_legacy_metadata_capabilities: bool = False,
) -> None:
    """Leave root while retaining the closed boot-specific capability set."""

    if sys.platform != "linux":
        raise SupervisorPrivilegeBootstrapError(
            "Supervisor privilege bootstrap requires Linux"
        )
    if os.getuid() != 0 or os.geteuid() != 0:
        raise SupervisorPrivilegeBootstrapError(
            "Supervisor privilege bootstrap must start as root"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    _prctl(libc, _PR_SET_KEEPCAPS, 1, label="retain capabilities across uid drop")
    try:
        os.setgroups([WORKSPACE_GID])
        os.setgid(SUPERVISOR_GID)
        os.setuid(SUPERVISOR_UID)
    except OSError as exc:
        raise SupervisorPrivilegeBootstrapError(
            "cannot drop to the Supervisor identity"
        ) from exc

    header = _CapabilityHeader(version=_LINUX_CAPABILITY_VERSION_3, pid=0)
    data = (_CapabilityData * 2)()
    capability_mask = (
        _LEGACY_GATE_CAPABILITY_MASK
        if retain_legacy_metadata_capabilities
        else _CAPABILITY_MASK
    )
    data[0] = _CapabilityData(
        effective=capability_mask,
        permitted=capability_mask,
        inheritable=capability_mask,
    )
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        error_number = ctypes.get_errno()
        raise SupervisorPrivilegeBootstrapError(
            "cannot set the closed Supervisor capability set"
        ) from OSError(
            error_number or errno.EPERM,
            os.strerror(error_number or errno.EPERM),
        )
    capabilities = (
        (*_LEGACY_METADATA_CAPABILITIES, *_RUNTIME_CAPABILITIES)
        if retain_legacy_metadata_capabilities
        else _RUNTIME_CAPABILITIES
    )
    for capability in capabilities:
        if libc.prctl(
            _PR_CAP_AMBIENT,
            _PR_CAP_AMBIENT_RAISE,
            capability,
            0,
            0,
        ) != 0:
            error_number = ctypes.get_errno()
            raise SupervisorPrivilegeBootstrapError(
                f"cannot raise ambient capability {capability}"
            ) from OSError(
                error_number or errno.EPERM,
                os.strerror(error_number or errno.EPERM),
            )
    _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1, label="set no-new-privileges")
    _prctl(libc, _PR_SET_DUMPABLE, 0, label="disable Supervisor dumpability")
    os.umask(0o007)
    if (
        os.getuid() != SUPERVISOR_UID
        or os.geteuid() != SUPERVISOR_UID
        or os.getgid() != SUPERVISOR_GID
        or os.getegid() != SUPERVISOR_GID
        or os.getgroups() != [WORKSPACE_GID]
    ):
        raise SupervisorPrivilegeBootstrapError(
            "Supervisor identity did not settle to the closed contract"
        )


def seal_legacy_metadata_capabilities() -> None:
    """Irreversibly remove the gate-only metadata capabilities before Hermes."""

    if sys.platform != "linux":
        raise LegacyMetadataCapabilitySealError(
            "legacy metadata capability sealing requires Linux"
        )
    libc = ctypes.CDLL(None, use_errno=True)
    for capability in _LEGACY_METADATA_CAPABILITIES:
        if libc.prctl(
            _PR_CAP_AMBIENT,
            _PR_CAP_AMBIENT_LOWER,
            capability,
            0,
            0,
        ) != 0:
            error_number = ctypes.get_errno()
            raise LegacyMetadataCapabilitySealError(
                f"cannot lower ambient capability {capability}"
            ) from OSError(
                error_number or errno.EPERM,
                os.strerror(error_number or errno.EPERM),
            )
    header = _CapabilityHeader(version=_LINUX_CAPABILITY_VERSION_3, pid=0)
    data = (_CapabilityData * 2)()
    data[0] = _CapabilityData(
        effective=_CAPABILITY_MASK,
        permitted=_CAPABILITY_MASK,
        inheritable=_CAPABILITY_MASK,
    )
    if libc.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        error_number = ctypes.get_errno()
        raise LegacyMetadataCapabilitySealError(
            "cannot drop legacy metadata capabilities"
        ) from OSError(
            error_number or errno.EPERM,
            os.strerror(error_number or errno.EPERM),
        )
    observed = (_CapabilityData * 2)()
    if libc.capget(ctypes.byref(header), ctypes.byref(observed)) != 0:
        error_number = ctypes.get_errno()
        raise LegacyMetadataCapabilitySealError(
            "cannot verify the sealed capability set"
        ) from OSError(
            error_number or errno.EPERM,
            os.strerror(error_number or errno.EPERM),
        )
    if (
        observed[0].effective != _CAPABILITY_MASK
        or observed[0].permitted != _CAPABILITY_MASK
        or observed[0].inheritable != _CAPABILITY_MASK
        or any(
            getattr(observed[1], field) != 0
            for field in ("effective", "permitted", "inheritable")
        )
    ):
        raise LegacyMetadataCapabilitySealError(
            "legacy metadata capabilities remain after sealing"
        )
    for capability in _LEGACY_METADATA_CAPABILITIES:
        if libc.prctl(
            _PR_CAP_AMBIENT,
            _PR_CAP_AMBIENT_IS_SET,
            capability,
            0,
            0,
        ) != 0:
            raise LegacyMetadataCapabilitySealError(
                f"ambient capability {capability} remains after sealing"
            )


def exec_supervisor(argv: Sequence[str]) -> NoReturn:
    if not argv or argv[0] != SUPERVISOR_EXECUTABLE:
        raise SupervisorPrivilegeBootstrapError(
            "Supervisor executable does not match the fixed contract"
        )
    environment, inherited_descriptors = prepare_root_secret_handoffs(os.environ)
    try:
        drop_to_supervisor(
            retain_legacy_metadata_capabilities=(
                os.environ.get("HERMES_LEGACY_SOURCE_GATE_ENABLED") == "1"
            )
        )
        os.execve(argv[0], list(argv), environment)
    finally:
        for descriptor in inherited_descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _prctl(
    libc: ctypes.CDLL,
    operation: int,
    value: int,
    *,
    label: str,
) -> None:
    if libc.prctl(operation, value, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise SupervisorPrivilegeBootstrapError(f"cannot {label}") from OSError(
            error_number or errno.EPERM,
            os.strerror(error_number or errno.EPERM),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    return parser


def main() -> NoReturn:
    argv = list(_parser().parse_args().argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    exec_supervisor(argv)


if __name__ == "__main__":
    main()
