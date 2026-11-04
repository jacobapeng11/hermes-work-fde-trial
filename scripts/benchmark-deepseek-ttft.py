#!/usr/bin/env python3
"""Benchmark TTFT for two OpenAI-compatible DeepSeek V4 Flash endpoints.

The script uses only the Python standard library and reads credentials from
environment variables. It intentionally never prints model responses or API
keys.

Pod usage:

    export DEEPSEEK_ZY_API_KEY='...'
    export OPENAI_COMPATIBLE_API_KEY='...'
    python3 benchmark-deepseek-ttft.py
"""

from __future__ import annotations

import codecs
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_DEEPSEEK_ZY_BASE_URL = "https://api.deepseek.com"
DEFAULT_OPENAI_COMPATIBLE_BASE_URL = "https://provider.example.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROMPT = "你好"
DEFAULT_RUNS = 5
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_ERROR_BODY_BYTES = 2048

OpenUrl = Callable[..., Any]
Clock = Callable[[], int]

# Do not let host-level HTTP_PROXY/HTTPS_PROXY silently change the measured
# network path. Kubernetes egress routing still applies as usual.
_DIRECT_OPENER = build_opener(ProxyHandler({}))
_open_url = _DIRECT_OPENER.open


class ConfigError(ValueError):
    """Invalid or incomplete benchmark configuration."""


class StreamError(RuntimeError):
    """The upstream stream did not satisfy the expected SSE contract."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str

    @property
    def endpoint(self) -> str:
        return chat_completions_url(self.base_url)


@dataclass(frozen=True)
class BenchmarkConfig:
    providers: tuple[Provider, Provider]
    model: str
    prompt: str
    runs: int
    timeout_seconds: float

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(provider.api_key for provider in self.providers)


@dataclass(frozen=True)
class SampleResult:
    provider: str
    run: int
    success: bool
    headers_ms: float | None = None
    semantic_ttft_ms: float | None = None
    answer_ttft_ms: float | None = None
    http_status: int | None = None
    error: str | None = None


class SSEParser:
    """Incrementally turn SSE text chunks into complete data payloads."""

    def __init__(self) -> None:
        self._line_buffer = ""
        self._data_lines: list[str] = []

    def feed(self, text: str) -> list[str]:
        self._line_buffer += text
        payloads: list[str] = []
        while "\n" in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            payload = self._consume_line(line)
            if payload is not None:
                payloads.append(payload)
        return payloads

    def finish(self) -> list[str]:
        payloads: list[str] = []
        if self._line_buffer:
            line = self._line_buffer
            self._line_buffer = ""
            if line.endswith("\r"):
                line = line[:-1]
            payload = self._consume_line(line)
            if payload is not None:
                payloads.append(payload)
        if self._data_lines:
            payloads.append("\n".join(self._data_lines))
            self._data_lines = []
        return payloads

    def _consume_line(self, line: str) -> str | None:
        if not line:
            if not self._data_lines:
                return None
            payload = "\n".join(self._data_lines)
            self._data_lines = []
            return payload
        if line.startswith(":"):
            return None
        if line == "data":
            self._data_lines.append("")
        elif line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            self._data_lines.append(value)
        return None


def chat_completions_url(base_url: str) -> str:
    raw = base_url.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError(f"Base URL 仅支持 http/https：{raw!r}")
    if not parsed.netloc or parsed.username or parsed.password:
        raise ConfigError(f"Base URL 主机无效或包含禁止的用户信息：{raw!r}")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"Base URL 不得包含 query 或 fragment：{raw!r}")

    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _positive_int(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是正整数") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} 必须是正整数")
    return parsed


def _positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是正数") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} 必须是正数")
    return parsed


def load_config(environ: Mapping[str, str] | None = None) -> BenchmarkConfig:
    env = os.environ if environ is None else environ
    deepseek_key = env.get("DEEPSEEK_ZY_API_KEY", "").strip()
    compatible_key = env.get("OPENAI_COMPATIBLE_API_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("DEEPSEEK_ZY_API_KEY", deepseek_key),
            ("OPENAI_COMPATIBLE_API_KEY", compatible_key),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"缺少必需环境变量：{', '.join(missing)}")

    model = env.get("TTFT_MODEL", DEFAULT_MODEL).strip()
    prompt = env.get("TTFT_PROMPT", DEFAULT_PROMPT)
    if not model:
        raise ConfigError("TTFT_MODEL 不能为空")
    if not prompt.strip():
        raise ConfigError("TTFT_PROMPT 不能为空")

    providers = (
        Provider(
            name="deepseek-zy",
            base_url=env.get(
                "DEEPSEEK_ZY_BASE_URL",
                DEFAULT_DEEPSEEK_ZY_BASE_URL,
            ),
            api_key=deepseek_key,
        ),
        Provider(
            name="openai-compatible",
            base_url=env.get(
                "OPENAI_COMPATIBLE_BASE_URL",
                DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
            ),
            api_key=compatible_key,
        ),
    )
    # Validate both URLs before the first network request.
    for provider in providers:
        provider.endpoint

    return BenchmarkConfig(
        providers=providers,
        model=model,
        prompt=prompt,
        runs=_positive_int(
            env.get("TTFT_RUNS", str(DEFAULT_RUNS)),
            "TTFT_RUNS",
        ),
        timeout_seconds=_positive_float(
            env.get(
                "TTFT_TIMEOUT_SECONDS",
                str(DEFAULT_TIMEOUT_SECONDS),
            ),
            "TTFT_TIMEOUT_SECONDS",
        ),
    )


def _milliseconds(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _delta_text_flags(payload: str) -> tuple[bool, bool]:
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise StreamError("收到无法解析的 SSE JSON 事件") from exc
    if not isinstance(event, dict):
        return False, False

    choices = event.get("choices")
    if not isinstance(choices, list):
        return False, False

    reasoning_found = False
    answer_found = False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning_found = reasoning_found or _nonempty_text(
            delta.get("reasoning_content")
        )
        answer_found = answer_found or _nonempty_text(delta.get("content"))
    return reasoning_found, answer_found


def _safe_error(text: str, secrets: tuple[str, ...]) -> str:
    sanitized = text.replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    sanitized = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"\1<redacted>",
        sanitized,
    )
    sanitized = re.sub(r"\bsk-[A-Za-z0-9._-]{8,}\b", "<redacted>", sanitized)
    return " ".join(sanitized.split())[:500]


def _http_error_summary(error: HTTPError, secrets: tuple[str, ...]) -> str:
    try:
        body = error.read(MAX_ERROR_BODY_BYTES).decode(
            "utf-8",
            errors="replace",
        )
    except Exception:
        body = ""
    detail = body.strip() or str(error.reason)
    return _safe_error(f"HTTP {error.code}: {detail}", secrets)


def measure_sample(
    provider: Provider,
    config: BenchmarkConfig,
    run: int,
    *,
    open_url: OpenUrl = _open_url,
    clock: Clock = time.perf_counter_ns,
) -> SampleResult:
    payload = json.dumps(
        {
            "model": config.model,
            "messages": [{"role": "user", "content": config.prompt}],
            "stream": True,
            "temperature": 0,
            "max_tokens": 64,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        provider.endpoint,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "hermes-pod-ttft-benchmark/1.0",
        },
    )

    start_ns = clock()
    try:
        with open_url(request, timeout=config.timeout_seconds) as response:
            headers_ns = clock()
            status = int(
                getattr(response, "status", None)
                or response.getcode()
                or 0
            )
            if not 200 <= status < 300:
                raw = response.read(MAX_ERROR_BODY_BYTES)
                detail = raw.decode("utf-8", errors="replace")
                return SampleResult(
                    provider=provider.name,
                    run=run,
                    success=False,
                    headers_ms=_milliseconds(start_ns, headers_ns),
                    http_status=status,
                    error=_safe_error(
                        f"HTTP {status}: {detail}",
                        config.secrets,
                    ),
                )

            parser = SSEParser()
            decoder = codecs.getincrementaldecoder("utf-8")()
            semantic_ns: int | None = None
            answer_ns: int | None = None
            done = False
            reader = getattr(response, "read1", response.read)

            while not done:
                raw_chunk = reader(4096)
                if not raw_chunk:
                    break
                arrival_ns = clock()
                for event_data in parser.feed(decoder.decode(raw_chunk)):
                    if event_data.strip() == "[DONE]":
                        done = True
                        break
                    reasoning, answer = _delta_text_flags(event_data)
                    if semantic_ns is None and (reasoning or answer):
                        semantic_ns = arrival_ns
                    if answer_ns is None and answer:
                        answer_ns = arrival_ns

            if not done:
                trailing = decoder.decode(b"", final=True)
                trailing_events = parser.feed(trailing) + parser.finish()
                arrival_ns = clock()
                for event_data in trailing_events:
                    if event_data.strip() == "[DONE]":
                        break
                    reasoning, answer = _delta_text_flags(event_data)
                    if semantic_ns is None and (reasoning or answer):
                        semantic_ns = arrival_ns
                    if answer_ns is None and answer:
                        answer_ns = arrival_ns

            if semantic_ns is None:
                raise StreamError("流结束前未收到非空 reasoning/content Delta")

            return SampleResult(
                provider=provider.name,
                run=run,
                success=True,
                headers_ms=_milliseconds(start_ns, headers_ns),
                semantic_ttft_ms=_milliseconds(start_ns, semantic_ns),
                answer_ttft_ms=(
                    _milliseconds(start_ns, answer_ns)
                    if answer_ns is not None
                    else None
                ),
                http_status=status,
            )
    except HTTPError as exc:
        return SampleResult(
            provider=provider.name,
            run=run,
            success=False,
            http_status=exc.code,
            error=_http_error_summary(exc, config.secrets),
        )
    except (StreamError, URLError, TimeoutError, OSError) as exc:
        return SampleResult(
            provider=provider.name,
            run=run,
            success=False,
            error=_safe_error(
                f"{type(exc).__name__}: {exc}",
                config.secrets,
            ),
        )


def _format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def print_sample(result: SampleResult, stdout: TextIO) -> None:
    if result.success:
        print(
            f"[{result.provider}] run={result.run} status=ok "
            f"headers_ms={_format_ms(result.headers_ms)} "
            f"semantic_ttft_ms={_format_ms(result.semantic_ttft_ms)} "
            f"answer_ttft_ms={_format_ms(result.answer_ttft_ms)}",
            file=stdout,
            flush=True,
        )
        return
    status = result.http_status if result.http_status is not None else "-"
    print(
        f"[{result.provider}] run={result.run} status=failed "
        f"http_status={status} error={result.error or 'unknown error'}",
        file=stdout,
        flush=True,
    )


def print_summary(
    config: BenchmarkConfig,
    results: list[SampleResult],
    stdout: TextIO,
) -> None:
    print("\nTTFT summary (semantic_ttft_ms)", file=stdout)
    print(
        "provider             success  failed  mean_ms  median_ms  min_ms  max_ms",
        file=stdout,
    )
    for provider in config.providers:
        provider_results = [
            result for result in results if result.provider == provider.name
        ]
        values = [
            result.semantic_ttft_ms
            for result in provider_results
            if result.success and result.semantic_ttft_ms is not None
        ]
        failed = len(provider_results) - len(values)
        if values:
            row = (
                f"{provider.name:<20} "
                f"{len(values):>2}/{config.runs:<2}   "
                f"{failed:>3}     "
                f"{statistics.fmean(values):>7.1f}  "
                f"{statistics.median(values):>9.1f}  "
                f"{min(values):>6.1f}  "
                f"{max(values):>6.1f}"
            )
        else:
            row = (
                f"{provider.name:<20} "
                f" 0/{config.runs:<2}   "
                f"{failed:>3}           -          -       -       -"
            )
        print(row, file=stdout)


def run_benchmark(
    config: BenchmarkConfig,
    *,
    stdout: TextIO = sys.stdout,
    open_url: OpenUrl = _open_url,
    clock: Clock = time.perf_counter_ns,
) -> list[SampleResult]:
    print(
        "DeepSeek V4 Flash Pod TTFT benchmark\n"
        f"model={config.model} prompt={config.prompt!r} "
        f"runs_per_provider={config.runs} "
        f"timeout_seconds={config.timeout_seconds:g}",
        file=stdout,
    )
    for provider in config.providers:
        print(
            f"provider={provider.name} endpoint={provider.endpoint}",
            file=stdout,
        )

    results: list[SampleResult] = []
    for run in range(1, config.runs + 1):
        order = (
            config.providers
            if run % 2 == 1
            else tuple(reversed(config.providers))
        )
        for provider in order:
            result = measure_sample(
                provider,
                config,
                run,
                open_url=open_url,
                clock=clock,
            )
            results.append(result)
            print_sample(result, stdout)
    print_summary(config, results, stdout)
    return results


def main(
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    open_url: OpenUrl = _open_url,
    clock: Clock = time.perf_counter_ns,
) -> int:
    try:
        config = load_config(environ)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=stderr)
        return 2

    results = run_benchmark(
        config,
        stdout=stdout,
        open_url=open_url,
        clock=clock,
    )
    successful = {
        provider.name: sum(
            result.success
            for result in results
            if result.provider == provider.name
        )
        for provider in config.providers
    }
    return (
        0
        if all(count == config.runs for count in successful.values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
