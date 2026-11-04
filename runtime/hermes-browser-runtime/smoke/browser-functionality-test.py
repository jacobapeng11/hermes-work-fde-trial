#!/usr/bin/env python3
"""Offline acceptance for the preinstalled local Hermes browser runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


class AcceptanceFailure(RuntimeError):
    pass


def run(command: list[str], *, env: dict[str, str]) -> str:
    print("$", " ".join(command), flush=True)
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    if result.returncode:
        raise AcceptanceFailure(f"command failed ({result.returncode}): {' '.join(command)}")
    return result.stdout


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def browser(env: dict[str, str], session: str, *args: str) -> dict[str, object]:
    output = run(["agent-browser", "--engine", "chrome", "--session", session, "--json", *args], env=env)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise AcceptanceFailure(f"agent-browser did not return JSON: {output}") from exc


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hermes-browser-container-test <artifacts-dir>", file=sys.stderr)
        return 2
    artifacts = Path(sys.argv[1]).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    page = Path("/tmp/hermes-browser-fixture.html")
    page.write_text(
        "<!doctype html><meta charset='utf-8'><title>中文浏览器验收</title>"
        "<h1>公开中文页面</h1><label>姓名<input id='name'></label>"
        "<button id='ok' onclick=\"document.querySelector('#result').textContent='已完成：'+document.querySelector('#name').value\">确认</button>"
        "<p id='result'>等待输入</p>",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AGENT_BROWSER_SOCKET_DIR"] = "/tmp/agent-browser-acceptance"
    env["HOME"] = "/tmp/home"
    env["XDG_CACHE_HOME"] = "/tmp/cache"
    env["XDG_CONFIG_HOME"] = "/tmp/config"
    try:
        version = run(["agent-browser", "--version"], env=env)
        require("0.26.0" in version, f"unexpected agent-browser version: {version}")
        require(Path("/usr/bin/chromium").is_file(), "system Chromium is missing")
        for session, value in (("hermes-zh-a", "甲"), ("hermes-zh-b", "乙")):
            browser(env, session, "open", page.as_uri())
            snapshot = browser(env, session, "snapshot")
            require("公开中文页面" in json.dumps(snapshot, ensure_ascii=False), "Chinese page snapshot is missing")
            browser(env, session, "fill", "#name", value)
            browser(env, session, "click", "#ok")
            content = browser(env, session, "get", "text", "#result")
            require(value in json.dumps(content, ensure_ascii=False), f"session {session} lost its input")
            browser(env, session, "screenshot", str(artifacts / f"{session}.png"))
            browser(env, session, "close")
        report = "BT-01: PASS (locked CLI and system Chromium)\nBT-02: PASS (offline Chinese open/snapshot/fill/click/screenshot)\nBT-03: PASS (two independent sessions)\n"
        (artifacts / "report.txt").write_text(report, encoding="utf-8")
        return 0
    except AcceptanceFailure as exc:
        (artifacts / "report.txt").write_text(f"FAIL: {exc}\n", encoding="utf-8")
        print(f"ACCEPTANCE FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
