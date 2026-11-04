#!/usr/bin/env python3
"""Offline unit tests for benchmark-three-model-ttft.py."""

from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError


SCRIPT = Path(__file__).with_name("benchmark-three-model-ttft.py")
SPEC = importlib.util.spec_from_file_location("three_model_ttft", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self):
        return iter(self.lines)


class Clock:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class BenchmarkTests(unittest.TestCase):
    def test_requires_both_keys(self) -> None:
        with self.assertRaisesRegex(MODULE.ConfigError, "DEEPSEEK_API_KEY"):
            MODULE.load_keys({"QWEN_API_KEY": "qwen"})

    def test_builds_expected_endpoints(self) -> None:
        self.assertEqual(
            MODULE.chat_completions_url("https://api.deepseek.com"),
            "https://api.deepseek.com/chat/completions",
        )
        self.assertEqual(
            MODULE.chat_completions_url("https://host.test/v1/"),
            "https://host.test/v1/chat/completions",
        )

    def test_cache_busters_make_equal_length_distinct_contexts(self) -> None:
        first = MODULE.build_context(10_000, "a" * 64)
        second = MODULE.build_context(10_000, "b" * 64)
        self.assertEqual(len(first), len(second))
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("cache-buster-" + "a" * 64))
        self.assertEqual(first.count("a" * 64), 1)
        self.assertEqual(second.count("b" * 64), 1)

    def test_measures_first_event_and_semantic_ttft(self) -> None:
        response = FakeResponse(
            [
                b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
                b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"answer"}}]}\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":20,"prompt_tokens_details":{"cached_tokens":16}}}\n',
                b"data: [DONE]\n",
            ]
        )
        sample, assistant = MODULE.run_request(
            spec=MODULE.MODEL_SPECS[0],
            api_key="secret",
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=8,
            timeout=3,
            open_url=lambda request, timeout: response,
            clock=Clock([1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6]),
            secrets=("secret",),
        )
        self.assertAlmostEqual(sample["header_ttft_ms"], 100)
        self.assertAlmostEqual(sample["first_event_ms"], 200)
        self.assertAlmostEqual(sample["semantic_ttft_ms"], 300)
        self.assertAlmostEqual(sample["completed_ms"], 600)
        self.assertEqual(MODULE.cached_tokens(sample), 16)
        self.assertEqual(assistant, "answer")

    def test_redacts_key_from_http_error(self) -> None:
        key = "do-not-leak"
        error = HTTPError(
            "https://host.test/chat/completions",
            401,
            "unauthorized",
            {},
            io.BytesIO(f"bad key {key}".encode()),
        )
        with self.assertRaises(MODULE.ProbeError) as raised:
            MODULE.run_request(
                spec=MODULE.MODEL_SPECS[0],
                api_key=key,
                messages=[],
                max_tokens=8,
                timeout=3,
                open_url=lambda request, timeout: (_ for _ in ()).throw(error),
                secrets=(key,),
            )
        self.assertNotIn(key, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_summary_and_json_output(self) -> None:
        samples = [
            {
                "kind": "first",
                "semantic_ttft_ms": 500.0,
                "completed_ms": 800.0,
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
            {
                "kind": "followup",
                "semantic_ttft_ms": 200.0,
                "completed_ms": 400.0,
                "usage": {
                    "prompt_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 96},
                },
            },
            {
                "kind": "followup",
                "semantic_ttft_ms": 300.0,
                "completed_ms": 600.0,
                "usage": {
                    "prompt_tokens": 120,
                    "prompt_tokens_details": {"cached_tokens": 112},
                },
            },
        ]
        summary = MODULE.summarize_samples(samples)
        self.assertEqual(summary["followup_mean_ms"], 250.0)
        self.assertEqual(summary["followup_median_ms"], 250.0)
        self.assertEqual(summary["last_prompt_tokens"], 120)
        self.assertEqual(summary["last_cached_tokens"], 112)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            MODULE.write_json(path, {"summary": summary})
            self.assertTrue(path.is_file())
            with self.assertRaises(FileExistsError):
                MODULE.write_json(path, {"summary": summary})

    def test_model_failure_is_returned_instead_of_raised(self) -> None:
        def fail_request(**kwargs):
            raise MODULE.ProbeError("provider unavailable")

        result = MODULE.run_model(
            spec=MODULE.MODEL_SPECS[0],
            api_key="secret",
            context_chars=1_000,
            run_nonce="run-nonce",
            cold_attempts=3,
            cache_target=896,
            cache_tolerance=0,
            rounds=3,
            max_tokens=8,
            timeout=3,
            secrets=("secret",),
            request_runner=fail_request,
        )
        self.assertEqual(result["samples"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["round"], 1)

    def test_retries_cached_first_turn_before_followups(self) -> None:
        calls: list[list[dict[str, object]]] = []
        sleeps: list[float] = []

        def sample(cache: int, semantic: float) -> dict[str, object]:
            return {
                "header_ttft_ms": semantic - 10,
                "first_event_ms": semantic - 5,
                "semantic_ttft_ms": semantic,
                "completed_ms": semantic + 100,
                "event_count": 2,
                "output_chars": 2,
                "usage": {
                    "prompt_tokens": 1_000,
                    "prompt_tokens_details": {"cached_tokens": cache},
                },
            }

        def request(**kwargs):
            calls.append(kwargs["messages"])
            if len(calls) == 1:
                return sample(128, 300), "cached"
            if len(calls) == 2:
                return sample(0, 400), "cold"
            return sample(896, 200), "warm"

        result = MODULE.run_model(
            spec=MODULE.MODEL_SPECS[0],
            api_key="secret",
            context_chars=1_000,
            run_nonce="run-nonce",
            cold_attempts=3,
            cache_target=896,
            cache_tolerance=0,
            rounds=3,
            max_tokens=8,
            timeout=3,
            secrets=("secret",),
            followup_delay=10,
            sleeper=sleeps.append,
            request_runner=request,
        )
        self.assertEqual(len(calls), 4)
        self.assertNotEqual(calls[0][0]["content"], calls[1][0]["content"])
        self.assertEqual(len(calls[2]), 4)
        self.assertEqual(len(calls[3]), 4)
        self.assertEqual(calls[2][:3], calls[3][:3])
        self.assertNotEqual(calls[2][3]["content"], calls[3][3]["content"])
        self.assertEqual(sleeps, [10, 10])
        self.assertEqual(len(result["samples"]), 3)
        self.assertEqual(result["samples"][0]["cold_attempt"], 2)
        self.assertEqual(
            [item["status"] for item in result["cold_first_attempts"]],
            ["rejected", "accepted"],
        )
        self.assertEqual(result["errors"], [])

    def test_followup_outside_cache_target_is_invalid(self) -> None:
        calls = 0

        def request(**kwargs):
            nonlocal calls
            calls += 1
            cache = 0 if calls == 1 else 1_152
            return {
                "header_ttft_ms": 90.0,
                "first_event_ms": 95.0,
                "semantic_ttft_ms": 100.0,
                "completed_ms": 200.0,
                "event_count": 2,
                "output_chars": 2,
                "usage": {
                    "prompt_tokens": 1_200,
                    "prompt_tokens_details": {"cached_tokens": cache},
                },
            }, "visible"

        result = MODULE.run_model(
            spec=MODULE.MODEL_SPECS[0],
            api_key="secret",
            context_chars=1_000,
            run_nonce="run-nonce",
            cold_attempts=1,
            cache_target=896,
            cache_tolerance=128,
            rounds=2,
            max_tokens=8,
            timeout=3,
            secrets=("secret",),
            request_runner=request,
        )
        self.assertEqual(len(result["samples"]), 1)
        self.assertEqual(len(result["invalid_samples"]), 1)
        self.assertFalse(result["invalid_samples"][0]["cache_target_valid"])
        self.assertIn("outside cache target", result["errors"][-1]["error"])


if __name__ == "__main__":
    unittest.main()
