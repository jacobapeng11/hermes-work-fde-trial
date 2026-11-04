from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from application_agent_tool_trace import build_tool_details, render_tool_value


RUN_ID_RE = re.compile(r"(?:app_run_|run[-_])[A-Za-z0-9._:-]{8,200}\Z")
TRACE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{8,200}\Z")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")


class TraceInputError(ValueError):
    pass


class TraceNotFoundError(LookupError):
    pass


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def elapsed_ms(start: object, end: object) -> float | None:
    left = parse_time(start)
    right = parse_time(end)
    if left is None or right is None:
        return None
    return round(max(0.0, (right - left).total_seconds() * 1000), 3)


def short_trace_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        preview = "".join(
            character if character.isascii() and character.isalnum() else "-"
            for character in run_id[:20]
        )
        raise TraceInputError(
            f"run_id 格式无效（安全预览 {preview!r}，长度 {len(run_id)}）"
        )
    return _run_suffix(run_id)[:12]


def _run_suffix(run_id: str) -> str:
    if run_id.startswith("app_run_"):
        return run_id.removeprefix("app_run_")
    if run_id.startswith("run-"):
        return run_id.removeprefix("run-")
    if run_id.startswith("run_"):
        return run_id.removeprefix("run_")
    return run_id


def validate_trace_id(trace_id: str) -> str:
    value = trace_id.strip()
    if not TRACE_ID_RE.fullmatch(value):
        raise TraceInputError("trace_id 必须是 8～200 位安全标识字符")
    return value


def validate_safe_id(value: str, label: str) -> str:
    candidate = value.strip()
    if not SAFE_ID_RE.fullmatch(candidate):
        raise TraceInputError(f"{label} 格式无效")
    return candidate


def select_call(
    calls: Sequence[Mapping[str, Any]],
    *,
    run_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    latest: bool = False,
) -> dict[str, Any]:
    selectors = sum(
        value is not None and value is not False
        for value in (run_id, request_id, trace_id, latest)
    )
    if selectors != 1:
        raise TraceInputError("必须且只能指定一种调用选择方式")
    if run_id is not None:
        if not RUN_ID_RE.fullmatch(run_id):
            raise TraceInputError("run_id 格式无效")
        matches = [item for item in calls if item.get("runtime_run_id") == run_id]
    elif request_id is not None:
        validate_safe_id(request_id, "request_id")
        matches = [item for item in calls if item.get("request_id") == request_id]
    elif trace_id is not None:
        prefix = validate_trace_id(trace_id)
        matches = [
            item
            for item in calls
            if _run_suffix(str(item.get("runtime_run_id") or "")).startswith(prefix)
        ]
    else:
        matches = list(calls[:1])
    if not matches:
        raise TraceNotFoundError("没有找到匹配的应用 Agent 调用")
    if len(matches) != 1:
        raise TraceNotFoundError("调用标识匹配到多条记录，请使用更长的编号")
    return dict(matches[0])


def validate_json_output_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or path.parent != Path("/tmp") or path.suffix != ".json":
        raise TraceInputError("--output 必须是 /tmp 下的显式 .json 文件")
    if path.is_symlink():
        raise TraceInputError("--output 不能是符号链接")
    if path.exists():
        raise TraceInputError("--output 已存在，拒绝覆盖")
    return path


def _event_time(
    events: Sequence[Mapping[str, Any]],
    event_type: str,
) -> str | None:
    for event in events:
        if event.get("event_type") == event_type:
            value = event.get("occurred_at")
            return str(value) if value else None
    return None


def _number(value: object) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def aggregate_model_calls(
    metric_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    run_metrics: dict[str, Any] = {}
    for row in metric_rows:
        request_id = row.get("request_id")
        projection_type = row.get("projection_type")
        metric = row.get("metric")
        if not request_id:
            if isinstance(metric, str):
                run_metrics[metric] = _number(row.get("value"))
            continue
        call = calls.setdefault(
            str(request_id),
            {
                "request_id": str(request_id),
                "observed_at": row.get("created_at"),
                "kind": "auxiliary",
                "body_duration_ms": None,
                "first_reasoning_ms": None,
                "first_text_ms": None,
                "max_chunk_gap_ms": None,
                "prompt_tokens": None,
                "completion_tokens": None,
                "reasoning_tokens": None,
                "cache_hit_tokens": None,
                "cache_miss_tokens": None,
            },
        )
        if projection_type == "run.usage":
            usage = row.get("usage")
            if not isinstance(usage, Mapping):
                continue
            call["kind"] = "main"
            call["prompt_tokens"] = _number(usage.get("prompt_tokens"))
            call["completion_tokens"] = _number(usage.get("completion_tokens"))
            details = usage.get("completion_tokens_details")
            if isinstance(details, Mapping):
                call["reasoning_tokens"] = _number(details.get("reasoning_tokens"))
            call["cache_hit_tokens"] = _number(usage.get("prompt_cache_hit_tokens"))
            if call["cache_hit_tokens"] is None:
                prompt_details = usage.get("prompt_tokens_details")
                if isinstance(prompt_details, Mapping):
                    call["cache_hit_tokens"] = _number(prompt_details.get("cached_tokens"))
            call["cache_miss_tokens"] = _number(usage.get("prompt_cache_miss_tokens"))
            continue
        value = _number(row.get("value"))
        field = {
            "model_relay_upstream_body_duration_ms": "body_duration_ms",
            "model_relay_upstream_first_reasoning_delta_ms": "first_reasoning_ms",
            "model_relay_upstream_first_text_delta_ms": "first_text_ms",
            "model_relay_upstream_max_chunk_gap_ms": "max_chunk_gap_ms",
        }.get(str(metric))
        if field:
            call[field] = value
    ordered = sorted(
        calls.values(),
        key=lambda item: str(item.get("observed_at") or ""),
    )
    for index, call in enumerate(ordered, start=1):
        call["index"] = index
        call["request_id_short"] = call["request_id"][:8]
    return ordered, run_metrics


def pair_tool_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    tool_detail: str = "none",
) -> list[dict[str, Any]]:
    active: dict[str, Mapping[str, Any]] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        call_id = str(row.get("call_id") or "")
        status = str(row.get("status") or "")
        if not call_id:
            continue
        if status == "running":
            active[call_id] = row
            continue
        if status not in {"completed", "failed"}:
            continue
        start = active.pop(call_id, None)
        name = str(row.get("name") or (start or {}).get("name") or "unknown")
        input_value = (start or row).get("input")
        result_value = row.get("result")
        output.append(
            {
                "index": len(output) + 1,
                "call_id_short": call_id[:12],
                "name": name,
                "status": status,
                "started_at": (start or {}).get("occurred_at"),
                "finished_at": row.get("occurred_at"),
                "duration_ms": elapsed_ms(
                    (start or {}).get("occurred_at"),
                    row.get("occurred_at"),
                ),
                "input_chars": (start or row).get("input_chars"),
                "result_chars": row.get("result_chars"),
                **build_tool_details(
                    name,
                    input_value,
                    result_value,
                    tool_detail,
                ),
            }
        )
    for call_id, start in active.items():
        name = str(start.get("name") or "unknown")
        output.append(
            {
                "index": len(output) + 1,
                "call_id_short": call_id[:12],
                "name": name,
                "status": "running",
                "started_at": start.get("occurred_at"),
                "finished_at": None,
                "duration_ms": None,
                "input_chars": start.get("input_chars"),
                "result_chars": None,
                **build_tool_details(
                    name,
                    start.get("input"),
                    None,
                    tool_detail,
                ),
            }
        )
    return output


def _sum_metric(items: Iterable[Mapping[str, Any]], field: str) -> float:
    return round(
        sum(float(value) for item in items if (value := item.get(field)) is not None),
        3,
    )


def _sum_int(items: Iterable[Mapping[str, Any]], field: str) -> int:
    return sum(int(value) for item in items if (value := item.get(field)) is not None)


def build_trace(
    backend_call: Mapping[str, Any],
    runtime_payload: Mapping[str, Any],
    *,
    tool_detail: str = "none",
) -> dict[str, Any]:
    run = runtime_payload.get("run")
    if not isinstance(run, Mapping):
        raise TraceNotFoundError("目标 Runtime 中没有找到该 Run")
    events = runtime_payload.get("events")
    metric_rows = runtime_payload.get("metrics")
    tool_rows = runtime_payload.get("tools")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        events = []
    if not isinstance(metric_rows, Sequence) or isinstance(metric_rows, (str, bytes)):
        metric_rows = []
    if not isinstance(tool_rows, Sequence) or isinstance(tool_rows, (str, bytes)):
        tool_rows = []
    models, run_metrics = aggregate_model_calls(metric_rows)
    tools = pair_tool_events(tool_rows, tool_detail=tool_detail)
    run_started_at = _event_time(events, "chat.run.started")
    run_completed_at = _event_time(events, "chat.run.completed")
    terminal_at = run.get("terminal_at") or run_completed_at
    total_ms = elapsed_ms(run.get("accepted_at") or run.get("enqueued_at"), terminal_at)
    prepare_ms = elapsed_ms(run.get("accepted_at"), run_started_at)
    main_calls = [item for item in models if item["kind"] == "main"]
    auxiliary_calls = [item for item in models if item["kind"] == "auxiliary"]
    main_model_ms = _sum_metric(main_calls, "body_duration_ms")
    auxiliary_model_ms = _sum_metric(auxiliary_calls, "body_duration_ms")
    tool_ms = _sum_metric(tools, "duration_ms")
    observed_ms = main_model_ms + auxiliary_model_ms + tool_ms
    unobserved_ms = None if total_ms is None else round(max(0.0, total_ms - observed_ms), 3)
    remaining_ms = (
        None
        if unobserved_ms is None
        else round(max(0.0, unobserved_ms - float(prepare_ms or 0)), 3)
    )
    slowest_models = sorted(
        (item for item in models if item.get("body_duration_ms") is not None),
        key=lambda item: float(item["body_duration_ms"]),
        reverse=True,
    )[:3]
    slowest_tools = sorted(
        (item for item in tools if item.get("duration_ms") is not None),
        key=lambda item: float(item["duration_ms"]),
        reverse=True,
    )[:3]
    coverage_pct = (
        None
        if not total_ms
        else round(min(100.0, observed_ms / total_ms * 100), 2)
    )
    model_pct = (
        None
        if not total_ms
        else round(min(100.0, (main_model_ms + auxiliary_model_ms) / total_ms * 100), 2)
    )
    question = runtime_payload.get("question")
    return {
        "version": "1.0",
        "tool_detail": tool_detail,
        "overview": {
            "agent_id": backend_call.get("agent_id"),
            "agent_slug": backend_call.get("agent_slug"),
            "channel": backend_call.get("channel"),
            "status": backend_call.get("status") or run.get("state"),
            "trace_id": short_trace_id(str(backend_call["runtime_run_id"])),
            "run_id": backend_call.get("runtime_run_id"),
            "request_id": backend_call.get("request_id"),
            "session_id_suffix": str(run.get("session_id") or "")[-12:] or None,
            "started_at": run.get("accepted_at") or backend_call.get("started_at"),
            "finished_at": terminal_at or backend_call.get("finished_at"),
            "total_ms": total_ms,
            "terminal": terminal_at is not None,
            **({"question": question} if isinstance(question, str) and question else {}),
        },
        "backend_lifecycle": {
            "created_at": backend_call.get("created_at"),
            "submit_intent_at": backend_call.get("submit_intent_at"),
            "dashboard_accepted_at": backend_call.get("dashboard_accepted_at"),
            "started_at": backend_call.get("started_at"),
            "finished_at": backend_call.get("finished_at"),
            "created_to_submit_ms": elapsed_ms(
                backend_call.get("created_at"), backend_call.get("submit_intent_at")
            ),
            "submit_to_dashboard_accepted_ms": elapsed_ms(
                backend_call.get("submit_intent_at"),
                backend_call.get("dashboard_accepted_at"),
            ),
            "backend_execution_ms": elapsed_ms(
                backend_call.get("started_at"), backend_call.get("finished_at")
            ),
        },
        "runtime_lifecycle": {
            "enqueued_at": run.get("enqueued_at"),
            "accepted_at": run.get("accepted_at"),
            "acknowledged_at": run.get("acknowledged_at"),
            "run_started_at": run_started_at,
            "terminal_at": terminal_at,
            "queue_ms": elapsed_ms(run.get("enqueued_at"), run.get("accepted_at")),
            "accept_to_ack_ms": elapsed_ms(
                run.get("accepted_at"), run.get("acknowledged_at")
            ),
            "ack_to_run_started_ms": elapsed_ms(
                run.get("acknowledged_at"), run_started_at
            ),
            "run_execution_ms": elapsed_ms(run_started_at, terminal_at),
            "first_upstream_byte_ms": run_metrics.get(
                "model_relay_first_upstream_byte_ms"
            ),
            "first_semantic_token_ms": run_metrics.get(
                "model_relay_first_semantic_token_ms"
            ),
        },
        "model_calls": models,
        "tool_calls": tools,
        "summary": {
            "main_model_call_count": len(main_calls),
            "auxiliary_model_call_count": len(auxiliary_calls),
            "main_model_cumulative_ms": main_model_ms,
            "auxiliary_model_cumulative_ms": auxiliary_model_ms,
            "tool_call_count": len(tools),
            "tool_cumulative_ms": tool_ms,
            "prepare_ms": prepare_ms,
            "remaining_unobserved_ms": remaining_ms,
            "observed_coverage_pct": coverage_pct,
            "model_pct_of_total": model_pct,
            "prompt_tokens": _sum_int(main_calls, "prompt_tokens"),
            "completion_tokens": _sum_int(main_calls, "completion_tokens"),
            "reasoning_tokens": _sum_int(main_calls, "reasoning_tokens"),
            "cache_hit_tokens": _sum_int(main_calls, "cache_hit_tokens"),
            "cache_miss_tokens": _sum_int(main_calls, "cache_miss_tokens"),
            "slowest_model_calls": [
                {
                    "index": item["index"],
                    "request_id_short": item["request_id_short"],
                    "kind": item["kind"],
                    "duration_ms": item["body_duration_ms"],
                }
                for item in slowest_models
            ],
            "slowest_tool_calls": [
                {
                    "index": item["index"],
                    "name": item["name"],
                    "status": item["status"],
                    "duration_ms": item["duration_ms"],
                }
                for item in slowest_tools
            ],
        },
    }


def _fmt_ms(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "无观测"
    if value >= 1000:
        return f"{value / 1000:.3f} 秒"
    return f"{value:.3f} 毫秒"


def render_call_list(calls: Sequence[Mapping[str, Any]]) -> str:
    headers = ("序号", "开始时间", "耗时", "状态", "短调用编号", "request_id")
    rows = [headers]
    for index, call in enumerate(calls, start=1):
        runtime_run_id = str(call.get("runtime_run_id") or "")
        duration = elapsed_ms(call.get("started_at"), call.get("finished_at"))
        rows.append(
            (
                str(index),
                str(call.get("started_at") or call.get("created_at") or "无观测")[:19],
                _fmt_ms(duration),
                str(call.get("status") or "unknown"),
                short_trace_id(runtime_run_id),
                str(call.get("request_id") or "无"),
            )
        )
        question = call.get("question")
        if isinstance(question, str) and question:
            rows.append(("", "问题", question, "", "", ""))
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    return "\n".join(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    )


def render_trace(trace: Mapping[str, Any]) -> str:
    overview = trace["overview"]
    backend = trace["backend_lifecycle"]
    runtime = trace["runtime_lifecycle"]
    summary = trace["summary"]
    lines = [
        "应用 Agent 调用耗时追踪",
        f"Agent: {overview.get('agent_slug')} (ID {overview.get('agent_id')})",
        f"调用: {overview.get('trace_id')}  状态: {overview.get('status')}  渠道: {overview.get('channel')}",
        f"开始: {overview.get('started_at') or '无观测'}",
        f"结束: {overview.get('finished_at') or '仍在执行'}",
        f"总耗时: {_fmt_ms(overview.get('total_ms'))}",
    ]
    if overview.get("question"):
        lines.append(f"问题摘要: {overview['question']}")
    lines.extend(
        [
            "",
            "生命周期",
            f"- Backend 创建到提交: {_fmt_ms(backend.get('created_to_submit_ms'))}",
            f"- Backend 提交到 Dashboard 接受: {_fmt_ms(backend.get('submit_to_dashboard_accepted_ms'))}",
            f"- Runtime 排队: {_fmt_ms(runtime.get('queue_ms'))}",
            f"- Runtime 接受到 ACK: {_fmt_ms(runtime.get('accept_to_ack_ms'))}",
            f"- ACK 到 Run 开始: {_fmt_ms(runtime.get('ack_to_run_started_ms'))}",
            f"- Run 执行: {_fmt_ms(runtime.get('run_execution_ms'))}",
            f"- 首个上游字节: {_fmt_ms(runtime.get('first_upstream_byte_ms'))}",
            f"- 首个语义 Token: {_fmt_ms(runtime.get('first_semantic_token_ms'))}",
            "",
            "模型调用",
        ]
    )
    model_calls = trace.get("model_calls") or []
    if not model_calls:
        lines.append("- 无模型分轮观测")
    for item in model_calls:
        lines.append(
            f"- #{item['index']} {item['kind']} {item['request_id_short']}: "
            f"{_fmt_ms(item.get('body_duration_ms'))}, "
            f"输入 {item.get('prompt_tokens') if item.get('prompt_tokens') is not None else '-'}, "
            f"输出 {item.get('completion_tokens') if item.get('completion_tokens') is not None else '-'}, "
            f"推理 {item.get('reasoning_tokens') if item.get('reasoning_tokens') is not None else '-'}"
        )
    lines.extend(["", "工具调用"])
    tool_calls = trace.get("tool_calls") or []
    if not tool_calls:
        lines.append("- 无工具调用观测")
    for item in tool_calls:
        lines.append(
            f"- #{item['index']} {item['name']} [{item['status']}]: "
            f"{_fmt_ms(item.get('duration_ms'))}, "
            f"输入 {item.get('input_chars') if item.get('input_chars') is not None else '-'} 字符, "
            f"结果 {item.get('result_chars') if item.get('result_chars') is not None else '-'} 字符"
        )
        if item.get("detail_summary"):
            lines.append(f"  做了什么: {item['detail_summary']}")
        if "detail_input" in item:
            lines.append("  实际输入:")
            lines.extend(
                f"    {line}"
                for line in render_tool_value(item["detail_input"]).splitlines()
            )
        if "detail_result" in item:
            lines.append("  实际结果:")
            lines.extend(
                f"    {line}"
                for line in render_tool_value(item["detail_result"]).splitlines()
            )
    lines.extend(
        [
            "",
            "汇总与瓶颈",
            f"- 主模型 {summary['main_model_call_count']} 次，累计 {_fmt_ms(summary['main_model_cumulative_ms'])}",
            f"- 辅助模型 {summary['auxiliary_model_call_count']} 次，累计 {_fmt_ms(summary['auxiliary_model_cumulative_ms'])}",
            f"- 工具 {summary['tool_call_count']} 次，累计 {_fmt_ms(summary['tool_cumulative_ms'])}",
            f"- Runtime 准备: {_fmt_ms(summary.get('prepare_ms'))}",
            f"- 其余未覆盖: {_fmt_ms(summary.get('remaining_unobserved_ms'))}",
            f"- 模型占总耗时: {summary.get('model_pct_of_total') if summary.get('model_pct_of_total') is not None else '-'}%",
            f"- 观测覆盖率: {summary.get('observed_coverage_pct') if summary.get('observed_coverage_pct') is not None else '-'}%",
            f"- 累计 Token: 输入 {summary['prompt_tokens']} / 输出 {summary['completion_tokens']} / 推理 {summary['reasoning_tokens']}",
        ]
    )
    if summary["slowest_model_calls"]:
        slow = summary["slowest_model_calls"][0]
        lines.append(
            f"- 最慢模型: #{slow['index']} {slow['request_id_short']} {_fmt_ms(slow['duration_ms'])}"
        )
    if summary["slowest_tool_calls"]:
        slow = summary["slowest_tool_calls"][0]
        lines.append(
            f"- 最慢工具: #{slow['index']} {slow['name']} {_fmt_ms(slow['duration_ms'])}"
        )
    if overview.get("terminal") is False:
        lines.append("- 当前 Run 尚未结束，本报告是只读时间快照。")
    return "\n".join(lines)


def trace_json(trace: Mapping[str, Any]) -> str:
    return json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
