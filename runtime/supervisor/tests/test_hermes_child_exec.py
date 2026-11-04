from __future__ import annotations

import ctypes
import os

import pytest

from platform_supervisor.hermes_child_exec import (
    HermesChildIsolationError,
    drop_to_hermes_child,
    exec_hermes_child,
)


class _FakePrctl:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def prctl(self, operation: int, value: int, *_args: int) -> int:
        self.calls.append((operation, value))
        return 0

    def capset(self, _header: object, _data: object) -> int:
        return 0


def test_child_identity_drop_orders_groups_ids_caps_and_no_new_privs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePrctl()
    calls: list[tuple[str, object]] = []
    identity = {"uid": 0, "gid": 0, "groups": []}
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: fake)
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
    monkeypatch.setattr(os, "getuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "geteuid", lambda: identity["uid"])
    monkeypatch.setattr(os, "getgid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getegid", lambda: identity["gid"])
    monkeypatch.setattr(os, "getgroups", lambda: identity["groups"])
    monkeypatch.setattr(os, "umask", lambda value: calls.append(("umask", value)))

    drop_to_hermes_child(uid=10001, gid=10001, supplemental_gid=1000)

    assert calls == [
        ("groups", [1000]),
        ("gid", 10001),
        ("uid", 10001),
        ("umask", 0o007),
    ]
    assert fake.calls == [(8, 0), (47, 4), (38, 1), (4, 0)]


def test_child_exec_rejects_relative_executable_before_identity_drop() -> None:
    with pytest.raises(
        HermesChildIsolationError,
        match="absolute path",
    ):
        exec_hermes_child(
            ["hermes", "dashboard"],
            uid=10001,
            gid=10001,
            supplemental_gid=1000,
        )


@pytest.mark.parametrize(
    ("uid", "gid", "supplemental_gid"),
    [(0, 10001, 1000), (10001, 0, 1000), (10000, 10001, 1000)],
)
def test_child_identity_rejects_privileged_or_supervisor_ids(
    uid: int,
    gid: int,
    supplemental_gid: int,
) -> None:
    with pytest.raises(HermesChildIsolationError, match="identity is invalid"):
        drop_to_hermes_child(
            uid=uid,
            gid=gid,
            supplemental_gid=supplemental_gid,
        )
