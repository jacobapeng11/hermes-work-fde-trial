from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from app.network_environment import proxy_free_environment


_DNS_SUBDOMAIN = re.compile(
    r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$"
)
_LABEL_KEY = re.compile(
    r"^(?:[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/)?"
    r"[A-Za-z0-9](?:[-_.A-Za-z0-9]{0,61}[A-Za-z0-9])?$"
)
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9](?:[-_.A-Za-z0-9]{0,61}[A-Za-z0-9])?$")
_RESOURCE = re.compile(r"^[a-z][a-z0-9./-]{0,127}$")
_BEARER = re.compile(r"(?i)(Bearer\s+)[^\s,;]+")
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*:\s*)(?!Bearer\s)[^\s,;]+")
_AUTH_VALUE = re.compile(r"(?i)((?:api[_ -]?key|token|secret)\s*[:=]\s*)[^\s,;]+")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")


class AdminKubernetesError(RuntimeError):
    code = "kubernetes_error"


class AdminKubernetesValidationError(AdminKubernetesError, ValueError):
    code = "invalid_kubernetes_argument"


class AdminKubernetesCommandError(AdminKubernetesError):
    code = "kubernetes_command_failed"


class AdminKubernetesTimeoutError(AdminKubernetesCommandError):
    code = "kubernetes_command_timeout"


class AdminKubernetesOutputLimitError(AdminKubernetesCommandError):
    code = "kubernetes_output_too_large"


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class AdminCommandRunner(Protocol):
    async def run(
        self,
        command: list[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> CommandResult:
        ...


def redact_kubernetes_text(value: str, *, limit: int = 1000) -> str:
    redacted = _BEARER.sub(r"\1[REDACTED]", value)
    redacted = _AUTH_HEADER.sub(r"\1[REDACTED]", redacted)
    redacted = _AUTH_VALUE.sub(r"\1[REDACTED]", redacted)
    redacted = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", redacted)
    return redacted[-limit:]


def validate_kubernetes_name(value: str) -> str:
    if not value or len(value) > 253 or not _DNS_SUBDOMAIN.fullmatch(value):
        raise AdminKubernetesValidationError("invalid Kubernetes resource name")
    return value


def validate_kubernetes_label(key: str, value: str) -> tuple[str, str]:
    if len(key) > 317 or not _LABEL_KEY.fullmatch(key):
        raise AdminKubernetesValidationError("invalid Kubernetes label key")
    if not value or len(value) > 63 or not _LABEL_VALUE.fullmatch(value):
        raise AdminKubernetesValidationError("invalid Kubernetes label value")
    return key, value


class SubprocessAdminCommandRunner:
    @staticmethod
    def _process_env() -> dict[str, str]:
        return proxy_free_environment(
            overrides={"PYTHONUNBUFFERED": "1"}
        )

    async def run(
        self,
        command: list[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._process_env(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AdminKubernetesTimeoutError("Kubernetes query timed out") from exc
        if len(stdout_bytes) + len(stderr_bytes) > output_limit_bytes:
            raise AdminKubernetesOutputLimitError("Kubernetes response exceeded the safe output limit")
        return CommandResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            returncode=int(process.returncode or 0),
        )


class AdminKubectlClient:
    QUERY_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
    LOG_OUTPUT_LIMIT_BYTES = 1024 * 1024
    MAX_LOG_LINES = 2000

    def __init__(
        self,
        *,
        runner: AdminCommandRunner | None = None,
        kubectl: str = "kubectl",
        kubeconfig: str | None = None,
        query_timeout_seconds: float = 5,
    ) -> None:
        self.runner = runner or SubprocessAdminCommandRunner()
        self.kubectl = kubectl
        self.kubeconfig = kubeconfig
        self.query_timeout_seconds = query_timeout_seconds

    async def get_json(
        self,
        resource: str,
        *,
        namespace: str | None = None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict:
        if not _RESOURCE.fullmatch(resource):
            raise AdminKubernetesValidationError("invalid Kubernetes resource type")
        command = [*self._base_command(), "get", resource]
        if name is not None:
            command.append(validate_kubernetes_name(name))
        if namespace is not None:
            command.extend(["-n", validate_kubernetes_name(namespace)])
        elif resource in {"pods", "events", "pods.metrics.k8s.io"}:
            command.append("--all-namespaces")
        if labels:
            selector = ",".join(
                f"{key}={value}"
                for key, value in sorted(
                    validate_kubernetes_label(key, value) for key, value in labels.items()
                )
            )
            command.extend(["-l", selector])
        command.extend(["-o", "json"])
        result = await self._run(command, output_limit_bytes=self.QUERY_OUTPUT_LIMIT_BYTES)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AdminKubernetesCommandError("Kubernetes returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AdminKubernetesCommandError("Kubernetes returned an unexpected payload")
        return payload

    async def logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        tail_lines: int = 500,
        previous: bool = False,
    ) -> str:
        self._validate_tail_lines(tail_lines)
        command = [
            *self._base_command(),
            "logs",
            validate_kubernetes_name(pod),
            "-n",
            validate_kubernetes_name(namespace),
            "-c",
            validate_kubernetes_name(container),
            "--tail",
            str(tail_lines),
            "--limit-bytes",
            str(self.LOG_OUTPUT_LIMIT_BYTES),
        ]
        if previous:
            command.append("--previous")
        command.append("--timestamps=true")
        result = await self._run(
            command,
            output_limit_bytes=self.LOG_OUTPUT_LIMIT_BYTES + 64 * 1024,
        )
        return result.stdout[: self.LOG_OUTPUT_LIMIT_BYTES]

    async def stream_logs(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        tail_lines: int = 100,
        duration_seconds: int = 1800,
    ) -> AsyncIterator[str]:
        self._validate_tail_lines(tail_lines)
        command = [
            *self._base_command(),
            "logs",
            validate_kubernetes_name(pod),
            "-n",
            validate_kubernetes_name(namespace),
            "-c",
            validate_kubernetes_name(container),
            "--tail",
            str(tail_lines),
            "--follow",
            "--timestamps=true",
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=SubprocessAdminCommandRunner._process_env(),
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(max(duration_seconds, 1), 1800)
        total_bytes = 0
        try:
            if process.stdout is None:
                raise AdminKubernetesCommandError("Kubernetes log stream is unavailable")
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                total_bytes += len(line)
                if total_bytes > 32 * 1024 * 1024:
                    raise AdminKubernetesOutputLimitError("Kubernetes log stream exceeded its safe limit")
                yield line.decode("utf-8", errors="replace").rstrip("\r\n")
            await process.wait()
            if process.returncode not in {0, None}:
                stderr = await process.stderr.read() if process.stderr is not None else b""
                raise AdminKubernetesCommandError(
                    redact_kubernetes_text(stderr.decode("utf-8", errors="replace"))
                    or "Kubernetes log stream failed"
                )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def delete_pod(self, *, namespace: str, pod: str) -> None:
        command = [
            *self._base_command(),
            "delete",
            "pod",
            validate_kubernetes_name(pod),
            "-n",
            validate_kubernetes_name(namespace),
            "--wait=false",
        ]
        await self._run(command, output_limit_bytes=64 * 1024)

    async def patch_annotation(
        self,
        *,
        resource: str,
        namespace: str,
        name: str,
        key: str,
        value: str,
    ) -> None:
        if not _RESOURCE.fullmatch(resource):
            raise AdminKubernetesValidationError("invalid Kubernetes resource type")
        validate_kubernetes_label(key, value)
        patch = json.dumps({"metadata": {"annotations": {key: value}}}, separators=(",", ":"))
        command = [
            *self._base_command(),
            "patch",
            resource,
            validate_kubernetes_name(name),
            "-n",
            validate_kubernetes_name(namespace),
            "--type=merge",
            "-p",
            patch,
        ]
        await self._run(command, output_limit_bytes=64 * 1024)


    def _base_command(self) -> list[str]:
        command = [self.kubectl]
        if self.kubeconfig:
            command.extend(["--kubeconfig", self.kubeconfig])
        return command

    async def _run(self, command: list[str], *, output_limit_bytes: int) -> CommandResult:
        result = await self.runner.run(
            command,
            timeout_seconds=self.query_timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )
        if result.returncode != 0:
            message = redact_kubernetes_text(result.stderr or result.stdout)
            raise AdminKubernetesCommandError(message or "Kubernetes command failed")
        return result

    @classmethod
    def _validate_tail_lines(cls, tail_lines: int) -> None:
        if not 1 <= tail_lines <= cls.MAX_LOG_LINES:
            raise AdminKubernetesValidationError("tail_lines must be between 1 and 2000")
