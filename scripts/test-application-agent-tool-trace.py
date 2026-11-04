#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import application_agent_tool_trace as tool_trace  # noqa: E402


class ToolSummaryTests(unittest.TestCase):
    def test_known_tools_get_specific_safe_summaries(self) -> None:
        knowledge = tool_trace.build_tool_details(
            "enterprise_knowledge_read",
            '{"relative_path":"departments/sales/mdl.yaml"}',
            '{"content":"private body"}',
            "summary",
        )
        self.assertEqual(
            knowledge["detail_summary"],
            "读取企业知识文件 departments/sales/mdl.yaml",
        )
        self.assertNotIn("private body", str(knowledge))

        context = tool_trace.build_tool_details(
            "enterprise_sql_context",
            '{"query":"全国各产品线销售额排名","domain":"sales"}',
            '{"schema":"private schema"}',
            "summary",
        )
        self.assertIn("检索企业 SQL 上下文", context["detail_summary"])
        self.assertIn("问题 11 字符", context["detail_summary"])
        self.assertIn("业务域 sales", context["detail_summary"])
        self.assertNotIn("全国各产品线", str(context))
        self.assertNotIn("private schema", str(context))

        write = tool_trace.build_tool_details(
            "write_file",
            '{"path":"/tmp/report.json","content":"abcdef"}',
            "ok",
            "summary",
        )
        self.assertIn("写入文件", write["detail_summary"])
        self.assertIn("report.json", write["detail_summary"])
        self.assertIn("内容 6 字符", write["detail_summary"])

    def test_unknown_tool_summary_describes_shape_without_values(self) -> None:
        details = tool_trace.build_tool_details(
            "custom_business_tool",
            '{"customer":"Acme","limit":20}',
            "secret result",
            "summary",
        )
        self.assertIn("输入字段 customer、limit", details["detail_summary"])
        self.assertNotIn("Acme", str(details))
        self.assertNotIn("secret result", str(details))


class ToolFullDetailTests(unittest.TestCase):
    def test_full_mode_parses_content_and_recursively_redacts_credentials(self) -> None:
        details = tool_trace.build_tool_details(
            "custom_business_tool",
            '{"query":"sales","authorization":"Bearer abc.def.ghi",'
            '"nested":{"client_secret":"top-secret"}}',
            '{"access_token":"eyJabcde.abcdefgh.zzz","rows":[{"amount":12}]}',
            "full",
        )
        self.assertEqual(details["detail_input"]["query"], "sales")
        self.assertEqual(details["detail_input"]["authorization"], "••••")
        self.assertEqual(details["detail_input"]["nested"]["client_secret"], "••••")
        self.assertEqual(details["detail_result"]["access_token"], "••••")
        self.assertEqual(details["detail_result"]["rows"][0]["amount"], 12)

    def test_none_mode_retains_no_content(self) -> None:
        details = tool_trace.build_tool_details(
            "enterprise_sql_query",
            '{"sql":"SELECT amount FROM sales"}',
            '{"rows":[{"amount":12}]}',
            "none",
        )
        self.assertEqual(details, {})

    def test_terminal_detail_is_clearly_truncated(self) -> None:
        rendered = tool_trace.render_tool_value("x" * 5000, limit=100)
        self.assertLess(len(rendered), 150)
        self.assertIn("内容已截断", rendered)


class ScriptInterpreterTests(unittest.TestCase):
    def test_system_python_reexecs_into_backend_virtualenv(self) -> None:
        system_python = Path("/usr/bin/python3")
        if not system_python.exists():
            self.skipTest("系统 Python 不存在")
        completed = subprocess.run(
            [
                str(system_python),
                str(SCRIPT_DIR / "trace-application-agent-run.py"),
                "show",
                "--help",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--tool-detail", completed.stdout)
        self.assertNotIn("ModuleNotFoundError", completed.stderr)


if __name__ == "__main__":
    unittest.main()
