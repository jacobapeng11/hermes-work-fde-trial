#!/usr/bin/env python3
"""Read-only timing trace for one Application Agent run."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
LIB = ROOT / "scripts" / "lib"


def _ensure_backend_python() -> None:
    expected_prefix = BACKEND / ".venv"
    if Path(sys.prefix).resolve() == expected_prefix.resolve():
        return
    target = expected_prefix / "bin" / "python"
    if not target.is_file() or not os.access(target, os.X_OK):
        print(
            "追踪失败: Backend Python 环境不存在，请先准备 backend/.venv",
            file=sys.stderr,
        )
        raise SystemExit(4)
    os.execv(str(target), [str(target), str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    _ensure_backend_python()


from sqlalchemy import or_, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402


os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(LIB))

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    ApplicationAgent,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
)
from application_agent_trace import (  # noqa: E402
    RUN_ID_RE,
    TraceInputError,
    TraceNotFoundError,
    build_trace,
    render_call_list,
    render_trace,
    select_call,
    trace_json,
    validate_json_output_path,
    validate_safe_id,
    validate_trace_id,
)


EXIT_INPUT = 2
EXIT_NOT_FOUND = 3
EXIT_ENVIRONMENT = 4
EXIT_RUNTIME_DATA = 5


class CommandError(RuntimeError):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "只读追踪应用 Agent 某次调用的控制面、Runtime、模型和工具耗时；"
            "默认不输出问题、回答、SQL 或工具内容，可显式查看工具摘要或详情。"
        )
    )
    commands = result.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="列出某个 Agent 最近的调用")
    listing.add_argument("--agent", required=True, help="Agent slug 或数字 ID")
    listing.add_argument("--limit", type=int, default=20, help="返回条数，默认 20")
    listing.add_argument(
        "--include-question",
        action="store_true",
        help="显式读取并显示 SQLBot 问题摘要",
    )

    showing = commands.add_parser("show", help="生成某次调用的完整耗时报告")
    showing.add_argument("--agent", help="可选 Agent slug 或数字 ID 约束")
    selector = showing.add_mutually_exclusive_group(required=True)
    selector.add_argument("--trace-id", help="list 输出的短调用编号")
    selector.add_argument("--request-id", help="Application Agent request_id")
    selector.add_argument("--run-id", help="完整 Runtime Run 标识")
    selector.add_argument("--latest", action="store_true", help="指定 Agent 的最近调用")
    showing.add_argument(
        "--include-question",
        action="store_true",
        help="显式读取并显示 SQLBot 问题摘要",
    )
    showing.add_argument(
        "--tool-detail",
        choices=("none", "summary", "full"),
        default="none",
        help="工具详情：none 不显示、summary 安全摘要、full 脱敏后的记录内容",
    )
    showing.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式，默认 text",
    )
    showing.add_argument("--output", help="JSON 输出文件，必须是新的 /tmp/*.json")
    return result


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _call_dict(
    run: ApplicationAgentRun,
    agent: ApplicationAgent,
    binding: ApplicationAgentRuntimeBinding,
) -> dict[str, Any]:
    return {
        "run_row_id": int(run.id),
        "agent_id": int(agent.id),
        "agent_slug": str(agent.slug),
        "channel": str(run.channel),
        "status": str(run.status),
        "runtime_run_id": str(run.runtime_run_id),
        "runtime_session_id": str(run.runtime_session_id),
        "request_id": str(run.request_id),
        "created_at": _iso(run.created_at),
        "submit_intent_at": _iso(run.submit_intent_at),
        "dashboard_accepted_at": _iso(run.dashboard_accepted_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "error_code": str(run.error_code) if run.error_code else None,
        "runtime_id": str(binding.runtime_id),
        "k8s_namespace": str(binding.k8s_namespace),
        "knative_service": str(binding.knative_service),
    }


async def _resolve_agent_id(raw: str) -> int:
    value = raw.strip()
    if not value:
        raise TraceInputError("--agent 不能为空")
    async with AsyncSessionLocal() as session:
        statement = select(ApplicationAgent.id)
        if value.isdigit():
            statement = statement.where(ApplicationAgent.id == int(value))
        else:
            validate_safe_id(value, "agent")
            statement = statement.where(ApplicationAgent.slug == value)
        ids = [int(item) for item in (await session.scalars(statement)).all()]
    if not ids:
        raise TraceNotFoundError("没有找到指定的应用 Agent")
    if len(ids) != 1:
        raise TraceNotFoundError("Agent slug 在多个组织中重复，请使用数字 Agent ID")
    return ids[0]


async def _query_calls(
    *,
    agent_raw: str | None,
    limit: int,
    run_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 100:
        raise TraceInputError("--limit 必须在 1～100 之间")
    agent_id = await _resolve_agent_id(agent_raw) if agent_raw else None
    statement = (
        select(ApplicationAgentRun, ApplicationAgent, ApplicationAgentRuntimeBinding)
        .join(ApplicationAgent, ApplicationAgent.id == ApplicationAgentRun.application_agent_id)
        .join(
            ApplicationAgentRuntimeBinding,
            ApplicationAgentRuntimeBinding.id == ApplicationAgentRun.runtime_binding_id,
        )
        .order_by(ApplicationAgentRun.created_at.desc())
        .limit(limit)
    )
    if agent_id is not None:
        statement = statement.where(ApplicationAgentRun.application_agent_id == agent_id)
    if run_id is not None:
        if not RUN_ID_RE.fullmatch(run_id):
            raise TraceInputError("run_id 格式无效")
        statement = statement.where(ApplicationAgentRun.runtime_run_id == run_id)
    if request_id is not None:
        validate_safe_id(request_id, "request_id")
        statement = statement.where(ApplicationAgentRun.request_id == request_id)
    if trace_id is not None:
        prefix = validate_trace_id(trace_id)
        statement = statement.where(
            or_(
                ApplicationAgentRun.runtime_run_id.startswith(
                    f"app_run_{prefix}",
                    autoescape=True,
                ),
                ApplicationAgentRun.runtime_run_id.startswith(
                    f"run-{prefix}",
                    autoescape=True,
                ),
                ApplicationAgentRun.runtime_run_id.startswith(
                    f"run_{prefix}",
                    autoescape=True,
                ),
            )
        )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(statement)).all()
    return [_call_dict(run, agent, binding) for run, agent, binding in rows]


def _kubectl_json(arguments: Sequence[str]) -> dict[str, Any]:
    completed = subprocess.run(
        ["kubectl", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CommandError("Kubernetes 只读查询失败", EXIT_ENVIRONMENT)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError("Kubernetes 返回了无效数据", EXIT_ENVIRONMENT) from exc
    if not isinstance(payload, dict):
        raise CommandError("Kubernetes 返回结构无效", EXIT_ENVIRONMENT)
    return payload


def _ready_pod(call: Mapping[str, Any]) -> str:
    namespace = str(call["k8s_namespace"])
    service = str(call["knative_service"])
    payload = _kubectl_json(
        [
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            f"serving.knative.dev/service={service}",
            "-o",
            "json",
        ]
    )
    candidates: list[str] = []
    for item in payload.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        if metadata.get("deletionTimestamp") or status.get("phase") != "Running":
            continue
        containers = status.get("containerStatuses") or []
        user_ready = any(
            entry.get("name") == "user-container" and entry.get("ready") is True
            for entry in containers
            if isinstance(entry, Mapping)
        )
        name = metadata.get("name")
        if user_ready and isinstance(name, str):
            candidates.append(name)
    if not candidates:
        raise CommandError(
            "目标 Runtime 当前没有 Ready Pod；脚本不会自动唤醒或重建服务",
            EXIT_ENVIRONMENT,
        )
    if len(candidates) != 1:
        raise CommandError("目标 Runtime 存在多个 Ready Pod，拒绝猜测", EXIT_ENVIRONMENT)
    return candidates[0]


RUNTIME_PROBE = r'''
import json
import re
import sqlite3
import sys

run_ids = json.loads(sys.argv[1])
include_question = sys.argv[2] == "1"
tool_detail = sys.argv[3]
if not isinstance(run_ids, list) or not all(isinstance(item, str) for item in run_ids):
    raise SystemExit(2)
if tool_detail not in {"none", "summary", "full"}:
    raise SystemExit(2)
connection = sqlite3.connect("file:/data/platform/supervisor.db?mode=ro", uri=True)
connection.row_factory = sqlite3.Row

def rows(sql, parameters):
    return [dict(item) for item in connection.execute(sql, parameters).fetchall()]

def sqlbot_question(prompt):
    if not include_question or not isinstance(prompt, str):
        return None
    match = re.search(r"<sqlbot-request>\s*(\{.*?\})\s*</sqlbot-request>", prompt, re.S)
    if match is None:
        return None
    try:
        value = json.loads(match.group(1)).get("question")
    except (AttributeError, json.JSONDecodeError):
        return None
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:120]

output = {}
for run_id in run_ids:
    run = connection.execute(
        "SELECT run_id,session_id,state,enqueued_at,accepted_at,acknowledged_at,"
        "terminal_at,profile_id,model_id,prompt FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        output[run_id] = {"run": None, "events": [], "metrics": [], "tools": []}
        continue
    run_dict = dict(run)
    prompt = run_dict.pop("prompt", None)
    events = rows(
        "SELECT event_type,occurred_at FROM run_events WHERE run_id=? ORDER BY seq",
        (run_id,),
    )
    metrics = []
    for row in rows(
        "SELECT projection_type,payload_json,created_at FROM projection_outbox "
        "WHERE projection_type IN ('run.metric','run.usage') "
        "AND json_extract(payload_json,'$.run_id')=? ORDER BY outbox_id",
        (run_id,),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        safe = {
            "projection_type": row["projection_type"],
            "created_at": row["created_at"],
            "request_id": payload.get("request_id"),
            "metric": payload.get("metric"),
            "value": payload.get("value"),
            "unit": payload.get("unit"),
        }
        if row["projection_type"] == "run.usage" and isinstance(payload.get("usage"), dict):
            usage = payload["usage"]
            safe["usage"] = {
                key: usage.get(key)
                for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                    "prompt_tokens_details", "completion_tokens_details",
                )
                if key in usage
            }
        metrics.append(safe)
    tools = []
    for row in rows(
        "SELECT payload_json,occurred_at FROM run_events "
        "WHERE run_id=? AND event_type='chat.tool.event' ORDER BY seq",
        (run_id,),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        tool = {
            "call_id": payload.get("callId"),
            "name": payload.get("name"),
            "status": payload.get("status"),
            "occurred_at": row["occurred_at"],
            "input_chars": len(str(payload.get("input", ""))) if "input" in payload else None,
            "result_chars": len(str(payload.get("result", ""))) if "result" in payload else None,
        }
        if tool_detail != "none":
            if "input" in payload:
                tool["input"] = payload.get("input")
            if "result" in payload:
                tool["result"] = payload.get("result")
        tools.append(tool)
    output[run_id] = {
        "run": run_dict,
        "events": events,
        "metrics": metrics,
        "tools": tools,
    }
    question = sqlbot_question(prompt)
    if question:
        output[run_id]["question"] = question
print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
'''


def _runtime_snapshots(
    call: Mapping[str, Any],
    run_ids: Sequence[str],
    *,
    include_question: bool,
    tool_detail: str = "none",
) -> dict[str, dict[str, Any]]:
    pod = _ready_pod(call)
    completed = subprocess.run(
        [
            "kubectl",
            "-n",
            str(call["k8s_namespace"]),
            "exec",
            pod,
            "-c",
            "user-container",
            "--",
            "/opt/hermes/.venv/bin/python",
            "-c",
            RUNTIME_PROBE,
            json.dumps(list(run_ids), separators=(",", ":")),
            "1" if include_question else "0",
            tool_detail,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CommandError("Runtime SQLite 只读查询失败", EXIT_RUNTIME_DATA)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError("Runtime SQLite 返回了无效数据", EXIT_RUNTIME_DATA) from exc
    if not isinstance(payload, dict):
        raise CommandError("Runtime SQLite 返回结构无效", EXIT_RUNTIME_DATA)
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


async def _list_command(args: argparse.Namespace) -> str:
    calls = await _query_calls(agent_raw=args.agent, limit=args.limit)
    if not calls:
        raise TraceNotFoundError("该 Agent 暂无调用记录")
    if args.include_question:
        snapshots = _runtime_snapshots(
            calls[0],
            [str(item["runtime_run_id"]) for item in calls],
            include_question=True,
            tool_detail="none",
        )
        for call in calls:
            snapshot = snapshots.get(str(call["runtime_run_id"])) or {}
            question = snapshot.get("question")
            if isinstance(question, str):
                call["question"] = question
    return render_call_list(calls)


async def _show_command(args: argparse.Namespace) -> str:
    if args.latest and not args.agent:
        raise TraceInputError("--latest 必须同时指定 --agent")
    if args.output and args.format != "json":
        raise TraceInputError("--output 只能与 --format json 一起使用")
    calls = await _query_calls(
        agent_raw=args.agent,
        limit=2 if args.trace_id or args.request_id else 1,
        run_id=args.run_id,
        request_id=args.request_id,
        trace_id=args.trace_id,
    )
    call = select_call(
        calls,
        run_id=args.run_id,
        request_id=args.request_id,
        trace_id=args.trace_id,
        latest=args.latest,
    )
    snapshots = _runtime_snapshots(
        call,
        [str(call["runtime_run_id"])],
        include_question=args.include_question,
        tool_detail=args.tool_detail,
    )
    snapshot = snapshots.get(str(call["runtime_run_id"]))
    if snapshot is None:
        raise CommandError("Runtime SQLite 未返回目标 Run", EXIT_RUNTIME_DATA)
    report = build_trace(call, snapshot, tool_detail=args.tool_detail)
    output = trace_json(report) if args.format == "json" else render_trace(report) + "\n"
    if args.output:
        path = validate_json_output_path(args.output)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(output)
        return f"已写入脱敏追踪报告: {path}\n"
    return output


async def run(args: argparse.Namespace) -> str:
    try:
        if args.command == "list":
            return await _list_command(args)
        return await _show_command(args)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output = asyncio.run(run(args))
    except (TraceInputError, argparse.ArgumentError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return EXIT_INPUT
    except TraceNotFoundError as exc:
        print(f"未找到: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except CommandError as exc:
        print(f"追踪失败: {exc}", file=sys.stderr)
        return exc.exit_code
    except SQLAlchemyError:
        print("追踪失败: Backend 数据库只读查询失败", file=sys.stderr)
        return EXIT_ENVIRONMENT
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
