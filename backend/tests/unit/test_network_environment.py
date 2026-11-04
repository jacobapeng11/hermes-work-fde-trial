from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess

from app.network_environment import (
    PROXY_ENVIRONMENT_VARIABLES,
    clear_proxy_environment,
    proxy_free_environment,
)
from app.im_gateway.node_bridge import WeComNodeBridge
from app.services.admin_kubernetes_client import (
    SubprocessAdminCommandRunner,
)


ROOT = Path(__file__).resolve().parents[3]
SHELL_VARIABLE_FILE = ROOT / "scripts/lib/proxy-env-vars.txt"
SHELL_HELPER = ROOT / "scripts/lib/no-proxy-env.sh"


def _proxy_source() -> dict[str, str]:
    return {
        variable: "socks5://127.0.0.1:9"
        for variable in PROXY_ENVIRONMENT_VARIABLES
    }


def test_python_and_shell_proxy_variable_contracts_match() -> None:
    shell_variables = tuple(
        line.strip()
        for line in SHELL_VARIABLE_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    )

    assert shell_variables == PROXY_ENVIRONMENT_VARIABLES


def test_proxy_free_environment_removes_all_proxy_variables_without_mutation() -> None:
    source = {**_proxy_source(), "HERMES_SENTINEL": "preserved"}

    result = proxy_free_environment(
        source, overrides={"PYTHONUNBUFFERED": "1"}
    )

    assert source["HTTP_PROXY"] == "socks5://127.0.0.1:9"
    assert result["HERMES_SENTINEL"] == "preserved"
    assert result["PYTHONUNBUFFERED"] == "1"
    assert not set(PROXY_ENVIRONMENT_VARIABLES).intersection(result)


def test_clear_proxy_environment_only_mutates_current_process(
    monkeypatch,
) -> None:
    for variable, value in _proxy_source().items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv("HERMES_SENTINEL", "preserved")

    clear_proxy_environment()

    assert os.environ["HERMES_SENTINEL"] == "preserved"
    assert not set(PROXY_ENVIRONMENT_VARIABLES).intersection(os.environ)


def test_shell_helper_clears_all_proxy_variables_for_child_only() -> None:
    parent = {**os.environ, **_proxy_source(), "HERMES_SENTINEL": "preserved"}

    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; source "$1"; env',
            "_",
            str(SHELL_HELPER),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=parent,
    )
    child_keys = {
        line.split("=", 1)[0]
        for line in completed.stdout.splitlines()
        if "=" in line
    }

    assert parent["HTTP_PROXY"] == "socks5://127.0.0.1:9"
    assert "HERMES_SENTINEL" in child_keys
    assert not set(PROXY_ENVIRONMENT_VARIABLES).intersection(child_keys)


def test_admin_subprocess_environment_uses_shared_proxy_contract(
    monkeypatch,
) -> None:
    for variable, value in _proxy_source().items():
        monkeypatch.setenv(variable, value)

    environment = SubprocessAdminCommandRunner._process_env()

    assert environment["PYTHONUNBUFFERED"] == "1"
    assert not set(PROXY_ENVIRONMENT_VARIABLES).intersection(environment)


def test_node_bridge_environment_uses_shared_proxy_contract(
    monkeypatch,
    tmp_path,
) -> None:
    for variable, value in _proxy_source().items():
        monkeypatch.setenv(variable, value)

    environment = WeComNodeBridge._process_environment(tmp_path)

    assert environment["IM_BRIDGE_TEMP_ROOT"] == str(tmp_path)
    assert not set(PROXY_ENVIRONMENT_VARIABLES).intersection(environment)


def test_all_project_shell_entrypoints_load_no_proxy_helper() -> None:
    missing = [
        path.name
        for path in sorted((ROOT / "scripts").glob("*.sh"))
        if "no-proxy-env.sh" not in path.read_text(encoding="utf-8")
    ]

    assert missing == []


def test_backend_clears_proxy_environment_before_api_imports() -> None:
    source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")

    assert source.index("clear_proxy_environment()") < source.index(
        "from app.api."
    )


def test_production_httpx_clients_explicitly_disable_environment_proxy() -> None:
    missing: list[str] = []
    for path in sorted((ROOT / "backend/app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "httpx"
                and function.attr in {"Client", "AsyncClient"}
            ):
                continue
            trust_env = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "trust_env"
                ),
                None,
            )
            if not (
                isinstance(trust_env, ast.Constant)
                and trust_env.value is False
            ):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert missing == []


def test_project_does_not_install_socks_proxy_support() -> None:
    requirements = (ROOT / "backend/requirements.txt").read_text(
        encoding="utf-8"
    ).lower()

    assert "socksio" not in requirements
    assert "httpx[socks]" not in requirements
