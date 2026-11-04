#!/usr/bin/env python3
"""Portable three-model direct TTFT benchmark.

Copy this single file to any computer with Python 3.10+ and set two environment
variables before running it:

    QWEN_API_KEY=... DEEPSEEK_API_KEY=... python3 benchmark-three-model-ttft.py

The benchmark bypasses Hermes Cloud Platform and calls each provider directly.
It uses only the Python standard library, disables inherited HTTP proxies, does
not print model responses, and never writes API keys to result files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import secrets as secrets_module
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


DEFAULT_ROUNDS = 10
DEFAULT_CONTEXT_CHARS = 110_000
DEFAULT_MAX_TOKENS = 128
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_COLD_ATTEMPTS = 3
DEFAULT_CACHE_TARGET = 21_248
DEFAULT_CACHE_TOLERANCE = 128
MAX_ERROR_BODY_BYTES = 2_048


@dataclass(frozen=True)
class ModelSpec:
    name: str
    base_url: str
    key_env: str
    context_char_adjustment: int = 0

    @property
    def endpoint(self) -> str:
        return chat_completions_url(self.base_url)


MODEL_SPECS = (
    ModelSpec(
        name="qwen3.7-plus",
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        key_env="QWEN_API_KEY",
    ),
    ModelSpec(
        name="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        key_env="DEEPSEEK_API_KEY",
        context_char_adjustment=-2_000,
    ),
    ModelSpec(
        name="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        key_env="DEEPSEEK_API_KEY",
        context_char_adjustment=-2_000,
    ),
)


class ConfigError(ValueError):
    """Invalid benchmark configuration."""


class ProbeError(RuntimeError):
    """A provider request failed or returned an invalid response."""


OpenUrl = Callable[..., Any]
Clock = Callable[[], float]
RequestRunner = Callable[..., tuple[dict[str, Any], str]]
Sleeper = Callable[[float], None]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(f"ttft-results-{stamp}")


def chat_completions_url(base_url: str) -> str:
    raw = base_url.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError(f"base URL must use http or https: {raw!r}")
    if not parsed.netloc or parsed.username or parsed.password:
        raise ConfigError(f"invalid base URL host: {raw!r}")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"base URL must not contain query or fragment: {raw!r}")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def build_context(target_chars: int, cache_buster: str) -> str:
    words = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")
    seed_digest = hashlib.sha256(cache_buster.encode("utf-8")).digest()
    generator = random.Random(int.from_bytes(seed_digest, "big"))
    prefix = (
        f"cache-buster-{cache_buster}\n"
        "This is unique context used only for a cold/warm cache comparison. "
        "Do not summarize or change it.\n\n"
    )
    chunks: list[str] = []
    length = 0
    while length < target_chars:
        chunk = " ".join(generator.choice(words) for _ in range(256)) + " "
        chunks.append(chunk)
        length += len(chunk)
    return prefix + "".join(chunks)[:target_chars]


def derive_cache_buster(run_nonce: str, model: str, attempt: int) -> str:
    material = f"{run_nonce}:{model}:{attempt}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def semantic_delta(delta: Mapping[str, Any]) -> str:
    for key in ("reasoning_content", "content"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    if delta.get("tool_calls"):
        return "<tool_call>"
    return ""


def sanitize(text: str, secrets: Iterable[str]) -> str:
    cleaned = text.replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def cached_tokens(sample: Mapping[str, Any]) -> int | None:
    usage = sample.get("usage")
    if not isinstance(usage, Mapping):
        return None
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, Mapping):
        return None
    value = details.get("cached_tokens")
    return value if isinstance(value, int) else None


def run_request(
    *,
    spec: ModelSpec,
    api_key: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    timeout: float,
    open_url: OpenUrl | None = None,
    clock: Clock = time.perf_counter,
    secrets: Sequence[str] = (),
) -> tuple[dict[str, Any], str]:
    body = {
        "model": spec.name,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "enable_thinking": True,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        spec.endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
        method="POST",
    )

    # Deliberately bypass HTTP_PROXY/HTTPS_PROXY on every operating system.
    if open_url is None:
        open_url = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        ).open

    started = clock()
    header_ms: float | None = None
    first_event_ms: float | None = None
    semantic_ms: float | None = None
    completed_ms: float | None = None
    visible_parts: list[str] = []
    usage: dict[str, Any] | None = None
    event_count = 0

    try:
        with open_url(request, timeout=timeout) as response:
            header_ms = (clock() - started) * 1000
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                event_count += 1
                elapsed_ms = (clock() - started) * 1000
                if first_event_ms is None:
                    first_event_ms = elapsed_ms
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]

                choices = event.get("choices") or []
                delta = choices[0].get("delta", {}) if choices else {}
                if isinstance(delta, dict):
                    text = semantic_delta(delta)
                    if text:
                        if semantic_ms is None:
                            semantic_ms = elapsed_ms
                    visible = delta.get("content")
                    if isinstance(visible, str) and visible:
                        visible_parts.append(visible)
            completed_ms = (clock() - started) * 1000
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
        message = sanitize(f"HTTP {exc.code}: {detail}", secrets)
        raise ProbeError(message) from exc
    except urllib.error.URLError as exc:
        message = sanitize(f"network error: {exc.reason}", secrets)
        raise ProbeError(message) from exc
    except TimeoutError as exc:
        raise ProbeError(f"request timed out after {timeout:g}s") from exc

    if first_event_ms is None:
        raise ProbeError("stream ended without a parseable SSE event")
    if semantic_ms is None:
        raise ProbeError("stream ended without semantic content")

    assistant_text = "".join(visible_parts)
    sample = {
        "header_ttft_ms": header_ms,
        "first_event_ms": first_event_ms,
        "semantic_ttft_ms": semantic_ms,
        "completed_ms": completed_ms,
        "event_count": event_count,
        "output_chars": len(assistant_text),
        "usage": usage,
    }
    return sample, assistant_text


def run_model(
    *,
    spec: ModelSpec,
    api_key: str,
    context_chars: int,
    run_nonce: str,
    cold_attempts: int,
    cache_target: int,
    cache_tolerance: int,
    rounds: int,
    max_tokens: int,
    timeout: float,
    secrets: Sequence[str],
    followup_delay: float = 0.0,
    sleeper: Sleeper = time.sleep,
    request_runner: RequestRunner = run_request,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    invalid_samples: list[dict[str, Any]] = []
    cold_first_attempts: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    accepted_context = ""

    print(f"\nmodel={spec.name}")
    print(f"endpoint={spec.endpoint}")

    for attempt in range(1, cold_attempts + 1):
        cache_buster = derive_cache_buster(run_nonce, spec.name, attempt)
        context = build_context(context_chars, cache_buster)
        candidate_messages: list[dict[str, Any]] = [
            {"role": "system", "content": context},
            {"role": "user", "content": "首问：请用一句话回复“首问收到”。"},
        ]
        started_at = now_iso()
        try:
            sample, assistant_text = request_runner(
                spec=spec,
                api_key=api_key,
                messages=candidate_messages,
                max_tokens=max_tokens,
                timeout=timeout,
                secrets=secrets,
            )
            sample.update(
                {
                    "round": 1,
                    "kind": "first",
                    "started_at": started_at,
                    "cold_attempt": attempt,
                }
            )
            cache_value = cached_tokens(sample)
            if cache_value != 0:
                cold_first_attempts.append(
                    {
                        "attempt": attempt,
                        "started_at": started_at,
                        "cached_tokens": cache_value,
                        "status": "rejected",
                    }
                )
                cache_text = "missing" if cache_value is None else str(cache_value)
                print(
                    f"cold_attempt={attempt:02d} rejected "
                    f"cached_tokens={cache_text}"
                )
                continue

            cold_first_attempts.append(
                {
                    "attempt": attempt,
                    "started_at": started_at,
                    "cached_tokens": 0,
                    "status": "accepted",
                }
            )
            samples.append(sample)
            candidate_messages.append(
                {"role": "assistant", "content": assistant_text}
            )
            messages = candidate_messages
            accepted_context = context
            print(
                "round=01 first    "
                f"header={fmt_ms(sample['header_ttft_ms'])} "
                f"semantic={fmt_ms(sample['semantic_ttft_ms'])} "
                f"done={fmt_ms(sample['completed_ms'])} "
                "cached_tokens=0"
            )
            break
        except Exception as exc:  # Preserve completed samples for diagnosis.
            error = sanitize(str(exc), secrets)
            errors.append(
                {
                    "round": 1,
                    "cold_attempt": attempt,
                    "error": error,
                    "started_at": started_at,
                }
            )
            print(f"cold_attempt={attempt:02d} ERROR {error}", file=sys.stderr)
            break

    if not samples and not errors:
        error = (
            "unable to obtain a cold first-turn sample after "
            f"{cold_attempts} attempts"
        )
        errors.append({"round": 1, "error": error, "started_at": now_iso()})
        print(f"round=01 ERROR {error}", file=sys.stderr)

    if samples:
        for index in range(1, rounds):
            if followup_delay:
                print(
                    f"waiting={followup_delay:g}s "
                    f"before_followup={index}"
                )
                sleeper(followup_delay)
            followup_messages = [
                *messages,
                {
                    "role": "user",
                    "content": f"追问第{index}次：请只回复“追问{index}收到”。",
                },
            ]
            started_at = now_iso()
            try:
                sample, _assistant_text = request_runner(
                    spec=spec,
                    api_key=api_key,
                    messages=followup_messages,
                    max_tokens=max_tokens,
                    timeout=timeout,
                    secrets=secrets,
                )
                sample.update(
                    {
                        "round": index + 1,
                        "kind": "followup",
                        "started_at": started_at,
                    }
                )
                cache_value = cached_tokens(sample)
                cache_text = "n/a" if cache_value is None else str(cache_value)
                cache_valid = (
                    cache_value is not None
                    and abs(cache_value - cache_target) <= cache_tolerance
                )
                sample["cache_target_valid"] = cache_valid
                if cache_valid:
                    samples.append(sample)
                else:
                    invalid_samples.append(sample)
                print(
                    f"round={index + 1:02d} followup "
                    f"header={fmt_ms(sample['header_ttft_ms'])} "
                    f"semantic={fmt_ms(sample['semantic_ttft_ms'])} "
                    f"done={fmt_ms(sample['completed_ms'])} "
                    f"cached_tokens={cache_text} "
                    f"cache_target_valid={str(cache_valid).lower()}"
                )
            except Exception as exc:  # Preserve completed samples for diagnosis.
                error = sanitize(str(exc), secrets)
                errors.append(
                    {
                        "round": index + 1,
                        "error": error,
                        "started_at": started_at,
                    }
                )
                print(f"round={index + 1:02d} ERROR {error}", file=sys.stderr)
                break

    if invalid_samples:
        errors.append(
            {
                "error": (
                    f"{len(invalid_samples)} followup sample(s) outside cache "
                    f"target {cache_target}±{cache_tolerance}"
                ),
                "started_at": now_iso(),
            }
        )

    return {
        "generated_at": now_iso(),
        "endpoint": spec.endpoint,
        "model": spec.name,
        "thinking": True,
        "rounds_requested": rounds,
        "context_chars": len(accepted_context) if accepted_context else None,
        "cache_target": cache_target,
        "cache_tolerance": cache_tolerance,
        "cold_first_attempts": cold_first_attempts,
        "samples": samples,
        "invalid_samples": invalid_samples,
        "summary": summarize_samples(samples),
        "errors": errors,
    }


def _usage_value(sample: Mapping[str, Any] | None, key: str) -> int | None:
    if sample is None:
        return None
    usage = sample.get("usage")
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(key)
    return value if isinstance(value, int) else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.mean(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _stdev(values: Sequence[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def summarize_samples(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    first = samples[0] if samples else None
    last = samples[-1] if samples else None
    followups = [sample for sample in samples if sample.get("kind") == "followup"]
    semantic = [
        float(sample["semantic_ttft_ms"])
        for sample in followups
        if isinstance(sample.get("semantic_ttft_ms"), (int, float))
        and math.isfinite(float(sample["semantic_ttft_ms"]))
    ]
    completed = [
        float(sample["completed_ms"])
        for sample in followups
        if isinstance(sample.get("completed_ms"), (int, float))
        and math.isfinite(float(sample["completed_ms"]))
    ]
    first_semantic = first.get("semantic_ttft_ms") if first else None
    return {
        "successful_samples": len(samples),
        "first_semantic_ttft_ms": first_semantic,
        "followup_semantic_ttft_ms": semantic,
        "followup_mean_ms": _mean(semantic),
        "followup_median_ms": _median(semantic),
        "followup_min_ms": min(semantic) if semantic else None,
        "followup_max_ms": max(semantic) if semantic else None,
        "followup_stdev_ms": _stdev(semantic),
        "first_completed_ms": first.get("completed_ms") if first else None,
        "followup_completed_median_ms": _median(completed),
        "first_prompt_tokens": _usage_value(first, "prompt_tokens"),
        "last_prompt_tokens": _usage_value(last, "prompt_tokens"),
        "first_cached_tokens": cached_tokens(first or {}),
        "last_cached_tokens": cached_tokens(last or {}),
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def print_comparison(results: Sequence[Mapping[str, Any]]) -> None:
    print("\ncomparison (semantic TTFT)")
    for result in results:
        samples = result["samples"]
        first = samples[0]["semantic_ttft_ms"] if samples else None
        followups = [
            sample["semantic_ttft_ms"]
            for sample in samples
            if sample.get("kind") == "followup"
        ]
        followup_text = ", ".join(fmt_ms(value) for value in followups) or "n/a"
        print(
            f"model={result['model']} first={fmt_ms(first)} "
            f"first_cached_tokens={cached_tokens(samples[0]) if samples else 'n/a'}"
        )
        print(f"followups=[{followup_text}]")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=positive_int, default=DEFAULT_ROUNDS)
    parser.add_argument(
        "--context-chars", type=positive_int, default=DEFAULT_CONTEXT_CHARS
    )
    parser.add_argument(
        "--max-tokens", type=positive_int, default=DEFAULT_MAX_TOKENS
    )
    parser.add_argument(
        "--timeout", type=positive_float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--cold-attempts", type=positive_int, default=DEFAULT_COLD_ATTEMPTS
    )
    parser.add_argument(
        "--cache-target", type=positive_int, default=DEFAULT_CACHE_TARGET
    )
    parser.add_argument(
        "--cache-tolerance",
        type=nonnegative_int,
        default=DEFAULT_CACHE_TOLERANCE,
    )
    parser.add_argument(
        "--followup-delay",
        type=nonnegative_float,
        default=0.0,
        help="seconds to wait before every followup request",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new result directory (default: UTC timestamped directory)",
    )
    return parser.parse_args(argv)


def load_keys(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    required = sorted({spec.key_env for spec in MODEL_SPECS})
    keys = {name: env.get(name, "").strip() for name in required}
    missing = [name for name, value in keys.items() if not value]
    if missing:
        raise ConfigError(
            "missing required environment variables: " + ", ".join(missing)
        )
    return keys


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        keys = load_keys()
        # Validate all endpoints before creating files or sending requests.
        for spec in MODEL_SPECS:
            spec.endpoint
            if args.context_chars + spec.context_char_adjustment < 1:
                raise ConfigError(
                    f"effective context chars must be positive for {spec.name}"
                )
        output_dir = args.output_dir or default_output_dir()
        output_dir.mkdir(parents=True, exist_ok=False)
    except (ConfigError, FileExistsError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    run_nonce = secrets_module.token_hex(32)
    secrets = tuple(dict.fromkeys(keys.values()))
    print(f"rounds={args.rounds}, context_payload_chars={args.context_chars}")
    print(f"cold_attempts={args.cold_attempts}")
    print(f"cache_target={args.cache_target}±{args.cache_tolerance}")
    print(f"followup_delay={args.followup_delay:g}s")
    print("proxy=disabled, thinking=true")
    print(f"output_dir={output_dir}")

    results: list[dict[str, Any]] = []
    for spec in MODEL_SPECS:
        result = run_model(
            spec=spec,
            api_key=keys[spec.key_env],
            context_chars=args.context_chars + spec.context_char_adjustment,
            run_nonce=run_nonce,
            cold_attempts=args.cold_attempts,
            cache_target=args.cache_target,
            cache_tolerance=args.cache_tolerance,
            rounds=args.rounds,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            secrets=secrets,
            followup_delay=args.followup_delay,
        )
        results.append(result)
        write_json(output_dir / f"{spec.name}.json", result)

    comparison = {
        "generated_at": now_iso(),
        "rounds_requested": args.rounds,
        "context_payload_chars": args.context_chars,
        "cold_attempts": args.cold_attempts,
        "cache_target": args.cache_target,
        "cache_tolerance": args.cache_tolerance,
        "followup_delay_seconds": args.followup_delay,
        "max_tokens": args.max_tokens,
        "timeout_seconds": args.timeout,
        "models": [
            {
                "model": result["model"],
                "endpoint": result["endpoint"],
                "summary": result["summary"],
                "error_count": len(result["errors"]),
            }
            for result in results
        ],
    }
    write_json(output_dir / "comparison.json", comparison)
    print_comparison(results)
    print(f"\nresult_file={output_dir / 'comparison.json'}")
    return 1 if any(result["errors"] for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
