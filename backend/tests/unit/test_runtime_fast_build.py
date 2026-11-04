"""Isolated tests for the test fleet build entry; no Docker or cluster writes."""

import json
from pathlib import Path
import runpy
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def builder():
    return runpy.run_path(str(ROOT / "scripts/build-publish-runtime-code-image-dev.py"))


def test_code_base_resolves_chain_to_same_fixed_image(builder, monkeypatch):
    images = {
        "current": {
            "hermes.platform.build-strategy": "supervisor-code-only",
            "hermes.platform.capabilities-image": "previous",
        },
        "previous": {
            "hermes.platform.build-strategy": "supervisor-code-only",
            "hermes.platform.base-image": "v3",
        },
        "v3": {"hermes.platform.build-strategy": "complete-runtime-overlay"},
    }
    calls = []

    def inspect(command):
        calls.append(command[-1])
        return json.dumps([{"Id": "sha256:fixed", "Config": {"Labels": images[command[-1]]}}])

    resolve = builder["_resolve_code_base"]
    monkeypatch.setitem(resolve.__globals__, "_checked", inspect)
    assert resolve("current") == "v3"
    assert calls == ["current", "previous", "v3"]


@pytest.mark.parametrize(
    "labels,match",
    [
        ({"hermes.platform.active-mcp-reload-test": "1"}, "test-only"),
        ({"hermes.platform.build-strategy": "supervisor-code-only"}, "no recorded base"),
        (
            {
                "hermes.platform.build-strategy": "supervisor-code-only",
                "hermes.platform.base-image": "current",
            },
            "cycle",
        ),
    ],
)
def test_invalid_base_fails_before_build(builder, monkeypatch, labels, match):
    resolve = builder["_resolve_code_base"]
    monkeypatch.setitem(
        resolve.__globals__,
        "_checked",
        lambda cmd: json.dumps([{"Id": "sha256:fixed", "Config": {"Labels": labels}}]),
    )
    with pytest.raises(RuntimeError, match=match):
        resolve("current")


def test_context_contains_only_supervisor_and_dockerfile(builder, monkeypatch, tmp_path):
    source = tmp_path / "repo"
    supervisor = source / "runtime/supervisor"
    (supervisor / "platform_supervisor").mkdir(parents=True)
    (supervisor / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (supervisor / "platform_supervisor/code.py").write_text("pass\n")
    (supervisor / "platform_supervisor/__pycache__").mkdir()
    (supervisor / "platform_supervisor/__pycache__/code.pyc").write_bytes(b"cache")
    (source / "Dockerfile.runtime-code-overlay").write_text("FROM base\n")
    (source / "backend").mkdir()
    (source / "backend/.env").write_text("must-not-enter-context")
    context = tmp_path / "context"
    context.mkdir()
    prepare = builder["_prepare_context"]
    monkeypatch.setitem(prepare.__globals__, "REPOSITORY_ROOT", source)
    prepare(context)
    assert sorted(str(p.relative_to(context)) for p in context.rglob("*") if p.is_file()) == [
        "Dockerfile.runtime-code-overlay",
        "runtime/supervisor/platform_supervisor/code.py",
        "runtime/supervisor/pyproject.toml",
    ]


@pytest.mark.parametrize(
    "option,build_exit,expected",
    [
        ([], 0, ["build", "fleet"]),
        ([], 9, ["build"]),
        (["--resume"], 0, ["fleet"]),
        (["--help"], 0, []),
        (["bad-option"], 0, []),
    ],
)
def test_entry_builds_once_then_entire_fleet(tmp_path, option, build_exit, expected):
    scripts = tmp_path / "scripts"
    (scripts / "lib").mkdir(parents=True)
    entry = scripts / "rebuild-runtime-test.sh"
    entry.write_text((ROOT / "scripts/rebuild-runtime-test.sh").read_text())
    (scripts / "lib/no-proxy-env.sh").write_text(":\n")
    (scripts / "lib/project-lifecycle.sh").write_text(
        "project_lifecycle_acquire_lock() { exec 6>/dev/null; }\n"
    )
    log = tmp_path / "calls"
    for name, marker, status in [
        ("build-runtime-code-image-dev.sh", "build", build_exit),
        ("reload-all-runtimes-from-env-dev.sh", "fleet", 0),
    ]:
        (scripts / name).write_text(f'echo "{marker} $*" >> "{log}"\nexit {status}\n')
    result = subprocess.run(["bash", str(entry), *option], capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    assert [line.split()[0] for line in calls] == expected
    if expected and expected[-1] == "fleet":
        assert result.returncode == 0, result.stderr
        assert "--force" in calls[-1]
        assert "--backend-lifecycle auto" in calls[-1]
        assert "--runtime-id" not in calls[-1]
    if build_exit and not option:
        assert result.returncode == build_exit
