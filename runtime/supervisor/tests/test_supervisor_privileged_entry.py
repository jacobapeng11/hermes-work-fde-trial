from __future__ import annotations

import ctypes
import os

import pytest

from platform_supervisor.supervisor_privileged_entry import (
    SUPERVISOR_EXECUTABLE,
    LegacyMetadataCapabilitySealError,
    SupervisorPrivilegeBootstrapError,
    drop_to_supervisor,
    exec_supervisor,
    seal_legacy_metadata_capabilities,
)


class _FakeLibc:
    def __init__(self) -> None:
        self.prctl_calls: list[tuple[int, int, int]] = []
        self.capability_words: tuple[int, int, int] | None = None

    def prctl(
        self,
        operation: int,
        value: int,
        argument: int = 0,
        *_args: int,
    ) -> int:
        self.prctl_calls.append((operation, value, argument))
        return 0

    def capset(self, _header: object, data: object) -> int:
        words = ctypes.cast(data, ctypes.POINTER(ctypes.c_uint32))
        self.capability_words = (words[0], words[1], words[2])
        return 0

    def capget(self, _header: object, data: object) -> int:
        assert self.capability_words is not None
        words = ctypes.cast(data, ctypes.POINTER(ctypes.c_uint32))
        words[0], words[1], words[2] = self.capability_words
        return 0


def test_privileged_entry_drops_root_and_retains_only_id_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLibc()
    calls: list[tuple[str, object]] = []
    identity = {"uid": 0, "gid": 0, "groups": []}
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(os, "getuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "getgid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getegid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getgroups", lambda: identity["groups"])
    monkeypatch.setattr(
        os,
        "setgroups",
        lambda groups: (calls.append(("groups", groups)), identity.update(groups=groups)),
    )
    monkeypatch.setattr(
        os,
        "setgid",
        lambda gid: (calls.append(("gid", gid)), identity.update(gid=gid)),
    )
    monkeypatch.setattr(
        os,
        "setuid",
        lambda uid: (calls.append(("uid", uid)), identity.update(uid=uid)),
    )
    monkeypatch.setattr(os, "umask", lambda value: calls.append(("umask", value)))

    drop_to_supervisor()

    assert calls == [
        ("groups", [1000]),
        ("gid", 10000),
        ("uid", 10000),
        ("umask", 0o007),
    ]
    assert fake.capability_words == (0xE0, 0xE0, 0xE0)
    assert fake.prctl_calls == [
        (8, 1, 0),
        (47, 2, 5),
        (47, 2, 6),
        (47, 2, 7),
        (38, 1, 0),
        (4, 0, 0),
    ]


def test_privileged_entry_requires_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 10000)
    monkeypatch.setattr(os, "geteuid", lambda: 10000)

    with pytest.raises(SupervisorPrivilegeBootstrapError, match="start as root"):
        drop_to_supervisor()


def test_legacy_gate_temporarily_retains_only_metadata_and_id_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLibc()
    identity = {"uid": 0, "gid": 0, "groups": []}
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr(os, "getuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "getgid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getegid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getgroups", lambda: identity["groups"])
    monkeypatch.setattr(os, "setgroups", lambda value: identity.update(groups=value))
    monkeypatch.setattr(os, "setgid", lambda value: identity.update(gid=value))
    monkeypatch.setattr(os, "setuid", lambda value: identity.update(uid=value))
    monkeypatch.setattr(os, "umask", lambda _value: None)

    drop_to_supervisor(retain_legacy_metadata_capabilities=True)

    assert fake.capability_words == (0xED, 0xED, 0xED)
    assert [call[:2] for call in fake.prctl_calls if call[0] == 47] == [
        (47, 2),
        (47, 2),
        (47, 2),
        (47, 2),
        (47, 2),
        (47, 2),
    ]


def test_legacy_metadata_capabilities_are_dropped_and_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLibc()
    fake.capability_words = (0xED, 0xED, 0xED)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)

    seal_legacy_metadata_capabilities()

    assert fake.capability_words == (0xE0, 0xE0, 0xE0)
    assert [call[:3] for call in fake.prctl_calls] == [
        (47, 3, 0),
        (47, 3, 2),
        (47, 3, 3),
        (47, 1, 0),
        (47, 1, 2),
        (47, 1, 3),
    ]


def test_legacy_metadata_capability_seal_fails_closed_on_capget_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeLibc()
    fake.capability_words = (0xED, 0xED, 0xED)
    fake.capget = lambda *_args: -1  # type: ignore[method-assign]
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)

    with pytest.raises(LegacyMetadataCapabilitySealError):
        seal_legacy_metadata_capabilities()


def test_privileged_entry_rejects_any_other_executable() -> None:
    with pytest.raises(SupervisorPrivilegeBootstrapError, match="fixed contract"):
        exec_supervisor(["/bin/sh"])


def test_fixed_supervisor_executable_is_absolute() -> None:
    assert SUPERVISOR_EXECUTABLE == "/opt/hermes/.venv/bin/platform-supervisor"
