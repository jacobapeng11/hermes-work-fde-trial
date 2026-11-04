#!/usr/bin/env python3
"""Negative proof that an immutable Runtime cannot lazy-install packages."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any


class LazyInstallPolicyError(RuntimeError):
    pass


def verify() -> dict[str, Any]:
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") != "1":
        raise LazyInstallPolicyError("HERMES_DISABLE_LAZY_INSTALLS is not enabled")
    if os.environ.get("HERMES_LAZY_INSTALL_TARGET"):
        raise LazyInstallPolicyError("durable lazy install target must not be configured")
    from tools import lazy_deps

    attempted = False
    original = lazy_deps._venv_pip_install

    def forbidden_installer(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal attempted
        attempted = True
        raise AssertionError("lazy installer was invoked")

    lazy_deps._venv_pip_install = forbidden_installer
    try:
        result = lazy_deps.install_specs(["platform-negative-probe==0"])
    finally:
        lazy_deps._venv_pip_install = original
    if attempted or result.ok or not result.blocked:
        raise LazyInstallPolicyError("sealed Runtime did not block a lazy install")
    report: dict[str, Any] = {
        "status": "passed",
        "lazy_installs_disabled": True,
        "durable_target_present": False,
        "installer_invoked": False,
    }
    report["report_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return report


def _main() -> int:
    report = verify()
    json.dump(report, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except LazyInstallPolicyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
