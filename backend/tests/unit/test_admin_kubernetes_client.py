from __future__ import annotations

import asyncio
import pytest

from app.services.admin_kubernetes_client import (
    AdminKubectlClient,
    AdminKubernetesCommandError,
    AdminKubernetesValidationError,
    CommandResult,
    validate_kubernetes_label,
    validate_kubernetes_name,
    SubprocessAdminCommandRunner,
)


def test_kubernetes_process_env_removes_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    env = SubprocessAdminCommandRunner._process_env()

    assert "HTTP_PROXY" not in env
    assert "https_proxy" not in env
    assert "NO_PROXY" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


class RecordingRunner:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        delay_seconds: float = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.delay_seconds = delay_seconds
        self.commands: list[list[str]] = []

    async def run(self, command: list[str], *, timeout_seconds: float, output_limit_bytes: int) -> CommandResult:
        self.commands.append(command)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return CommandResult(self.stdout, self.stderr, self.returncode)


@pytest.mark.asyncio
async def test_get_json_uses_argument_array_and_scoped_label_selector() -> None:
    runner = RecordingRunner(stdout='{"items":[]}')
    client = AdminKubectlClient(runner=runner, kubeconfig="/safe/config")

    payload = await client.get_json(
        "pods",
        namespace="hermes-runtimes",
        labels={"hermes.platform/runtime-workspace-id": "7"},
    )

    assert payload == {"items": []}
    assert runner.commands == [[
        "kubectl",
        "--kubeconfig",
        "/safe/config",
        "get",
        "pods",
        "-n",
        "hermes-runtimes",
        "-l",
        "hermes.platform/runtime-workspace-id=7",
        "-o",
        "json",
    ]]


@pytest.mark.parametrize("name", ["../secret", "pod;id", "pod name", "", "-pod", "pod-"])
def test_kubernetes_name_validation_rejects_unsafe_values(name: str) -> None:
    with pytest.raises(AdminKubernetesValidationError):
        validate_kubernetes_name(name)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("bad key", "1"),
        ("../../key", "1"),
        ("app", "bad value"),
        ("app", "value;id"),
    ],
)
def test_kubernetes_label_validation_rejects_unsafe_values(key: str, value: str) -> None:
    with pytest.raises(AdminKubernetesValidationError):
        validate_kubernetes_label(key, value)


@pytest.mark.asyncio
async def test_logs_enforces_tail_limit_and_uses_safe_flags() -> None:
    runner = RecordingRunner(stdout="line\n")
    client = AdminKubectlClient(runner=runner)

    result = await client.logs(
        namespace="hermes-runtimes",
        pod="runtime-pod",
        container="user-container",
        tail_lines=2000,
        previous=True,
    )

    assert result == "line\n"
    assert runner.commands == [[
        "kubectl",
        "logs",
        "runtime-pod",
        "-n",
        "hermes-runtimes",
        "-c",
        "user-container",
        "--tail",
        "2000",
        "--limit-bytes",
        "1048576",
        "--previous",
        "--timestamps=true",
    ]]


@pytest.mark.asyncio
async def test_logs_rejects_more_than_2000_lines() -> None:
    with pytest.raises(AdminKubernetesValidationError):
        await AdminKubectlClient(runner=RecordingRunner()).logs(
            namespace="hermes-runtimes",
            pod="runtime-pod",
            container="user-container",
            tail_lines=2001,
        )


@pytest.mark.asyncio
async def test_command_failure_redacts_credentials_and_paths() -> None:
    runner = RecordingRunner(
        returncode=1,
        stderr="Authorization: Bearer top-secret /root/.kube/config",
    )
    client = AdminKubectlClient(runner=runner, kubeconfig="/root/.kube/config")

    with pytest.raises(AdminKubernetesCommandError) as raised:
        await client.get_json("pods", namespace="hermes-runtimes")

    message = str(raised.value)
    assert "top-secret" not in message
    assert "/root/.kube/config" not in message
    assert "Bearer [REDACTED]" in message
