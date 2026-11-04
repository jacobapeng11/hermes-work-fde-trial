#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import application_agent_trace as trace  # noqa: E402


RUN_ID = "app_run_c33d77c1a1f6aa166655daa903632d081a4f3dca"


def backend_call(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "agent_id": 43,
        "agent_slug": "sqlbot2",
        "channel": "openapi",
        "status": "completed",
        "runtime_run_id": RUN_ID,
        "request_id": "request-safe",
        "created_at": "2026-08-31T07:32:51+00:00",
        "submit_intent_at": "2026-08-31T07:32:51.100000+00:00",
        "dashboard_accepted_at": "2026-08-31T07:32:51.500000+00:00",
        "started_at": "2026-08-31T07:32:51.700000+00:00",
        "finished_at": "2026-08-31T07:39:29.600000+00:00",
    }
    value.update(overrides)
    return value


def metric(
    request_id: str | None,
    name: str,
    value: float,
    created_at: str,
) -> dict[str, object]:
    return {
        "projection_type": "run.metric",
        "request_id": request_id,
        "metric": name,
        "value": value,
        "created_at": created_at,
    }


def usage(
    request_id: str,
    prompt: int,
    completion: int,
    reasoning: int,
    created_at: str,
) -> dict[str, object]:
    return {
        "projection_type": "run.usage",
        "request_id": request_id,
        "created_at": created_at,
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
            "prompt_cache_hit_tokens": prompt - 100,
            "prompt_cache_miss_tokens": 100,
        },
    }


def runtime_payload(*, terminal: bool = True, question: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "run": {
            "run_id": RUN_ID,
            "session_id": "app-43-session-secret-suffix",
            "state": "COMPLETED" if terminal else "RUNNING",
            "enqueued_at": "2026-08-31T07:32:51.700000+00:00",
            "accepted_at": "2026-08-31T07:32:51.720000+00:00",
            "acknowledged_at": "2026-08-31T07:32:53.100000+00:00",
            "terminal_at": "2026-08-31T07:39:29.620000+00:00" if terminal else None,
        },
        "events": [
            {
                "event_type": "chat.run.started",
                "occurred_at": "2026-08-31T07:32:53.110000+00:00",
            },
            *(
                [
                    {
                        "event_type": "chat.run.completed",
                        "occurred_at": "2026-08-31T07:39:29.460000+00:00",
                    }
                ]
                if terminal
                else []
            ),
        ],
        "metrics": [
            metric(None, "model_relay_first_upstream_byte_ms", 136.0, "2026-08-31T07:32:54+00:00"),
            metric(None, "model_relay_first_semantic_token_ms", 2712.0, "2026-08-31T07:32:56+00:00"),
            metric("aux", "model_relay_upstream_body_duration_ms", 137.0, "2026-08-31T07:32:54+00:00"),
            metric("main-a", "model_relay_upstream_body_duration_ms", 104_000.0, "2026-08-31T07:35:39+00:00"),
            metric("main-a", "model_relay_upstream_first_reasoning_delta_ms", 3_000.0, "2026-08-31T07:35:39+00:00"),
            usage("main-a", 50_000, 8_000, 7_500, "2026-08-31T07:35:39+00:00"),
            metric("main-b", "model_relay_upstream_body_duration_ms", 196_000.0, "2026-08-31T07:38:55+00:00"),
            usage("main-b", 66_000, 13_000, 12_000, "2026-08-31T07:38:55+00:00"),
        ],
        "tools": [
            {
                "call_id": "call-1",
                "name": "enterprise_sql_context",
                "status": "running",
                "occurred_at": "2026-08-31T07:33:54.000000+00:00",
                "input": '{"query":"全国各产品线销售额排名","domain":"sales"}',
                "input_chars": 28,
                "result_chars": None,
            },
            {
                "call_id": "call-1",
                "name": "enterprise_sql_context",
                "status": "completed",
                "occurred_at": "2026-08-31T07:33:54.700000+00:00",
                "result": '{"schema":"private schema"}',
                "input_chars": 28,
                "result_chars": 12_008,
            },
            {
                "call_id": "call-2",
                "name": "enterprise_knowledge_read",
                "status": "running",
                "occurred_at": "2026-08-31T07:39:12.630000+00:00",
                "input": '{"relative_path":"departments/sales/mdl.yaml"}',
                "input_chars": 59,
                "result_chars": None,
            },
            {
                "call_id": "call-2",
                "name": "enterprise_knowledge_read",
                "status": "failed",
                "occurred_at": "2026-08-31T07:39:12.650000+00:00",
                "result": '{"error":{"message":"not found"}}',
                "input_chars": 59,
                "result_chars": 124,
            },
        ],
    }
    if question is not None:
        payload["question"] = question
    return payload


class TraceSelectorTests(unittest.TestCase):
    def test_short_trace_id_and_all_selectors(self) -> None:
        calls = [backend_call()]
        self.assertEqual(trace.short_trace_id(RUN_ID), "c33d77c1a1f6")
        self.assertEqual(trace.select_call(calls, run_id=RUN_ID)["runtime_run_id"], RUN_ID)
        self.assertEqual(trace.select_call(calls, request_id="request-safe")["runtime_run_id"], RUN_ID)
        self.assertEqual(trace.select_call(calls, trace_id="c33d77c1")["runtime_run_id"], RUN_ID)
        self.assertEqual(trace.select_call(calls, latest=True)["runtime_run_id"], RUN_ID)

    def test_urlsafe_runtime_run_id_is_supported(self) -> None:
        run_id = "app_run_Qrt5fY_Safe-token-1234"
        self.assertEqual(trace.short_trace_id(run_id), "Qrt5fY_Safe-")
        self.assertEqual(
            trace.select_call(
                [backend_call(runtime_run_id=run_id)],
                trace_id="Qrt5fY_S",
            )["runtime_run_id"],
            run_id,
        )

    def test_uuid_runtime_run_id_is_supported(self) -> None:
        run_id = "run_72691496731b4b9a92b7d7b5190d2f66"
        self.assertEqual(trace.short_trace_id(run_id), "72691496731b")
        self.assertEqual(
            trace.select_call(
                [backend_call(runtime_run_id=run_id)],
                trace_id="72691496",
            )["runtime_run_id"],
            run_id,
        )

    def test_ambiguous_trace_id_fails_closed(self) -> None:
        other = RUN_ID.replace("c33d77c1a1f6", "c33d77c1ffff")
        with self.assertRaisesRegex(trace.TraceNotFoundError, "多条"):
            trace.select_call(
                [backend_call(), backend_call(runtime_run_id=other)],
                trace_id="c33d77c1",
            )

    def test_selector_must_be_exactly_one(self) -> None:
        with self.assertRaisesRegex(trace.TraceInputError, "一种"):
            trace.select_call([backend_call()], run_id=RUN_ID, latest=True)


class TraceAnalysisTests(unittest.TestCase):
    def test_builds_model_tool_and_bottleneck_summary(self) -> None:
        report = trace.build_trace(backend_call(), runtime_payload())
        self.assertEqual(report["summary"]["main_model_call_count"], 2)
        self.assertEqual(report["summary"]["auxiliary_model_call_count"], 1)
        self.assertEqual(report["summary"]["main_model_cumulative_ms"], 300_000.0)
        self.assertEqual(report["summary"]["tool_call_count"], 2)
        self.assertEqual(report["summary"]["tool_cumulative_ms"], 720.0)
        self.assertEqual(report["summary"]["prompt_tokens"], 116_000)
        self.assertEqual(report["summary"]["reasoning_tokens"], 19_500)
        self.assertEqual(report["summary"]["slowest_model_calls"][0]["duration_ms"], 196_000.0)
        self.assertEqual(report["tool_calls"][1]["status"], "failed")

    def test_in_progress_run_is_a_partial_report(self) -> None:
        report = trace.build_trace(
            backend_call(status="running", finished_at=None),
            runtime_payload(terminal=False),
        )
        self.assertFalse(report["overview"]["terminal"])
        self.assertIsNone(report["overview"]["total_ms"])
        self.assertIn("尚未结束", trace.render_trace(report))

    def test_default_report_contains_no_prompt_or_tool_body(self) -> None:
        report = trace.build_trace(backend_call(), runtime_payload())
        encoded = trace.trace_json(report)
        for secret in ("全国各产品线", "SELECT ", "工具返回正文", "session-secret"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("question", report["overview"])
        self.assertEqual(report["overview"]["session_id_suffix"], "ecret-suffix")

    def test_question_is_included_only_when_collector_supplies_summary(self) -> None:
        report = trace.build_trace(
            backend_call(), runtime_payload(question="全国各产品线销售额排名")
        )
        self.assertEqual(report["overview"]["question"], "全国各产品线销售额排名")
        self.assertIn("问题摘要", trace.render_trace(report))

    def test_summary_and_full_tool_details_are_rendered_on_request(self) -> None:
        summary_report = trace.build_trace(
            backend_call(), runtime_payload(), tool_detail="summary"
        )
        summary_text = trace.render_trace(summary_report)
        self.assertIn("检索企业 SQL 上下文", summary_text)
        self.assertIn("读取企业知识文件 departments/sales/mdl.yaml", summary_text)
        self.assertNotIn("private schema", summary_text)

        full_report = trace.build_trace(
            backend_call(), runtime_payload(), tool_detail="full"
        )
        full_text = trace.render_trace(full_report)
        self.assertIn('"query": "全国各产品线销售额排名"', full_text)
        self.assertIn('"schema": "private schema"', full_text)

        default_report = trace.build_trace(backend_call(), runtime_payload())
        self.assertNotIn("detail_summary", trace.trace_json(default_report))
        self.assertNotIn("private schema", trace.trace_json(default_report))


class TraceOutputTests(unittest.TestCase):
    def test_json_output_path_is_tmp_new_regular_path(self) -> None:
        path = Path("/tmp/application-agent-trace-unit.json")
        path.unlink(missing_ok=True)
        self.assertEqual(trace.validate_json_output_path(str(path)), path)
        with self.assertRaises(trace.TraceInputError):
            trace.validate_json_output_path("relative.json")
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            target = Path(directory) / "target.json"
            link = Path("/tmp/application-agent-trace-link.json")
            link.unlink(missing_ok=True)
            link.symlink_to(target)
            try:
                with self.assertRaisesRegex(trace.TraceInputError, "符号链接"):
                    trace.validate_json_output_path(str(link))
            finally:
                link.unlink(missing_ok=True)

    def test_list_renders_stable_short_id_without_question(self) -> None:
        output = trace.render_call_list([backend_call()])
        self.assertIn("c33d77c1a1f6", output)
        self.assertNotIn("全国各产品线", output)


if __name__ == "__main__":
    unittest.main()
