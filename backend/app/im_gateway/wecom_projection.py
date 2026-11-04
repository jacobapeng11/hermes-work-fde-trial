from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from app.hermes_runtime.public_tool_payload import sanitize_public_tool_text


ACTIVITY_STATE_PREFIX = "wecom_projection_v2:"
MAX_VISIBLE_NON_FAILED_TOOLS = 8

_BACKTICK_INTERNAL_PATH_RE = re.compile(
    r"`(?P<path>(?:/data/workspace/sessions/|/workspace/|<path>/)"
    r"[^`\r\n]+)`"
)
_PLAIN_INTERNAL_PATH_RE = re.compile(
    r"(?P<path>"
    r"(?:/data/workspace/sessions/|/workspace/|<path>/)"
    r"[^\s`\"'，。；：！？、,;:!?\)\]\}]+"
    r")"
)
_PUBLIC_PATH_RE = re.compile(
    r"(?:<path>/|/data/workspace/sessions/[^\s`\"']+/|/workspace/)"
    r"(?P<name>[^\s`\"'，。；：！？、,;:!?\)\]\}]+)"
)
_MULTIPLE_BLANK_LINES_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_WHITESPACE_RE = re.compile(r"\s+")

_FILE_KEYS = (
    "resolved_path",
    "path",
    "file_path",
    "output_path",
    "filename",
    "display_name",
    "files_modified",
    "files_created",
)


@dataclass
class ToolProjection:
    key: str
    name: str
    status: str
    order: int
    filename: str | None = None
    input_summary: str | None = None
    unstable: bool = False


@dataclass
class WeComProjection:
    """Deterministic public WeCom view of persisted Platform events."""

    reasoning_text: str = ""
    message_text: str = ""
    message_text_from_delta: bool = False
    final_received: bool = False
    reasoning_closed: bool = False
    tools: list[ToolProjection] = field(default_factory=list)
    activity_lines: list[str] = field(default_factory=list)
    error_text: str | None = None
    unstable_counter: int = 0

    def apply(self, event_name: str, data: dict[str, Any]) -> bool:
        if event_name == "chat.reasoning.delta":
            delta = data.get("delta")
            if not isinstance(delta, str) or not delta:
                return False
            self.reasoning_text += delta
            return True

        if event_name == "chat.message.delta" or (
            event_name == "chat.activity.delta"
            and data.get("source") == "agent"
        ):
            delta = data.get("delta")
            if (
                self.final_received
                or not isinstance(delta, str)
                or not delta
            ):
                return False
            self._close_reasoning()
            self.message_text += delta
            self.message_text_from_delta = event_name == "chat.message.delta"
            return True

        if event_name in {"chat.activity.delta", "chat.progress.delta"}:
            delta = data.get("delta")
            if not isinstance(delta, str) or not delta:
                return False
            # Recovered text is only a live snapshot of the in-flight model
            # segment. Keep the native thinking fold open until the terminal
            # message.completed event confirms the final answer.
            if data.get("interim") is True:
                # A prior stream attempt may already have exposed part of the
                # same in-flight snapshot as assistant text. Reclassify it
                # into the execution fold before appending the new suffix.
                self.reasoning_closed = False
                if self.message_text:
                    self._append_activity(self.message_text)
                    self.message_text = ""
            else:
                self._close_reasoning()
            return self._append_activity(delta)

        if event_name == "chat.message.completed":
            reasoning_changed = self._apply_completed_reasoning(
                data.get("reasoning")
            )
            final = data.get("text")
            if not isinstance(final, str):
                final = data.get("content")
            if not isinstance(final, str):
                return reasoning_changed
            self._close_reasoning()
            changed = (
                reasoning_changed
                or not self.final_received
                or final != self.message_text
            )
            self.message_text = final
            self.final_received = True
            return changed

        if event_name == "chat.tool.event":
            # A normal online turn may emit a short assistant preamble before
            # Hermes announces its first tool. That text is execution
            # commentary; move it into the thinking fold before rendering the
            # tool row so it cannot leak into the final answer area.
            if self.message_text and self.message_text_from_delta:
                self._append_activity(self.message_text)
                self.message_text = ""
                self.message_text_from_delta = False
                self.final_received = False
                self.reasoning_closed = False
            self._close_reasoning()
            return self._apply_tool(data)

        if event_name == "chat.subagent.event":
            self._close_reasoning()
            status = _localized_status(
                str(data.get("status") or data.get("type") or "running")
            )
            summary = _compact_line(
                data.get("summary")
                or data.get("text")
                or data.get("goal")
                or ""
            )
            line = f"子 Agent {status}"
            if summary:
                line += f"：{summary}"
            return self._append_activity(line)

        if event_name == "chat.transport.reconnecting":
            self._close_reasoning()
            return self._append_activity(
                "连接正在恢复，回答会自动继续。"
            )

        if event_name == "chat.error":
            self._close_reasoning()
            message = sanitize_visible_text(
                str(data.get("message") or "处理失败，请稍后重试。")
            )
            if message == self.error_text:
                return False
            self.error_text = message
            return True

        if event_name == "chat.done":
            before = self.reasoning_closed
            self._close_reasoning()
            return before != self.reasoning_closed

        return False

    def _apply_completed_reasoning(self, value: object) -> bool:
        if not isinstance(value, str) or not value:
            return False
        current = self.reasoning_text
        if current == value or current.startswith(value):
            return False
        self.reasoning_text = value
        return True

    def render_passive(self) -> str:
        blocks = self._render_blocks(active=False)
        return "\n\n".join(block for block in blocks if block)

    def render_active(self) -> str:
        blocks = self._render_blocks(active=True)
        return "\n\n".join(block for block in blocks if block)

    def split_oversized_terminal(
        self, passive_limit_bytes: int
    ) -> tuple[str, str]:
        """Return a valid passive head and tag-free active continuation."""

        full = self.render_passive()
        if len(full.encode("utf-8")) <= passive_limit_bytes:
            return full, ""

        notice = "回答较长，完整内容将继续发送。"
        active_blocks: list[str] = []
        execution = self._execution_block()
        if self.reasoning_text or execution:
            # Keep the execution trace in the same native fold as reasoning;
            # otherwise WeCom history shows a separate “执行过程” block after
            # the completed-thinking card.
            wrapper = "<think>\n\n</think>\n\n" + notice
            execution_suffix = f"\n\n{execution}" if execution else ""
            budget = max(
                0,
                passive_limit_bytes
                - len(wrapper.encode("utf-8"))
                - len(execution_suffix.encode("utf-8")),
            )
            prefix = _utf8_prefix(self.reasoning_text, budget)
            passive = f"<think>\n{prefix}{execution_suffix}\n</think>\n\n{notice}"
            remainder = self.reasoning_text[len(prefix) :]
            if remainder:
                active_blocks.append(
                    f"**思考过程（续）**\n{remainder}"
                )
        else:
            passive = notice

        active_blocks.extend(self._visible_body_blocks())
        return passive, "\n\n".join(active_blocks)

    def serialize_activity(self) -> str:
        payload = {
            "v": 2,
            "final_received": self.final_received,
            "reasoning_closed": self.reasoning_closed,
            "tools": [asdict(tool) for tool in self.tools],
            "activity_lines": self.activity_lines,
            "error_text": self.error_text,
            "unstable_counter": self.unstable_counter,
        }
        return ACTIVITY_STATE_PREFIX + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def restore(
        cls,
        *,
        reasoning_text: str | None,
        message_text: str | None,
        activity_text: str | None,
    ) -> WeComProjection | None:
        if not isinstance(activity_text, str) or not activity_text.startswith(
            ACTIVITY_STATE_PREFIX
        ):
            return None
        try:
            payload = json.loads(
                activity_text[len(ACTIVITY_STATE_PREFIX) :]
            )
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != 2:
            return None

        projection = cls(
            reasoning_text=str(reasoning_text or ""),
            message_text=str(message_text or ""),
            final_received=bool(payload.get("final_received")),
            reasoning_closed=bool(payload.get("reasoning_closed")),
            error_text=(
                str(payload["error_text"])
                if isinstance(payload.get("error_text"), str)
                else None
            ),
            unstable_counter=max(
                0, _safe_int(payload.get("unstable_counter"))
            ),
        )
        lines = payload.get("activity_lines")
        if isinstance(lines, list):
            projection.activity_lines = [
                str(line)
                for line in lines
                if isinstance(line, str) and line
            ]
        tools = payload.get("tools")
        if isinstance(tools, list):
            for raw in tools:
                tool = _restore_tool(raw)
                if tool is not None:
                    projection.tools.append(tool)
        projection.tools.sort(key=lambda item: item.order)
        return projection

    def _render_blocks(self, *, active: bool) -> list[str]:
        blocks: list[str] = []
        execution = self._execution_block()
        if self.reasoning_text or execution:
            if active:
                fold_parts = []
                if self.reasoning_text:
                    fold_parts.append(
                        f"**思考过程**\n{self.reasoning_text}"
                    )
                if execution:
                    fold_parts.append(execution)
                blocks.append("\n\n".join(fold_parts))
            else:
                reasoning = "<think>\n"
                if self.reasoning_text:
                    reasoning += self.reasoning_text
                if execution:
                    if self.reasoning_text:
                        reasoning += "\n\n"
                    reasoning += execution
                if self.reasoning_closed or (
                    execution and not self.reasoning_text
                ):
                    reasoning += "\n</think>"
                blocks.append(reasoning)
        blocks.extend(self._visible_body_blocks())
        return blocks

    def _visible_body_blocks(self) -> list[str]:
        blocks: list[str] = []
        body = sanitize_visible_text(self.message_text)
        if body:
            blocks.append(body)
        if self.error_text:
            blocks.append(f"**错误**：{self.error_text}")
        return blocks

    def _execution_block(self) -> str:
        lines = self._render_tool_lines()
        lines.extend(self.activity_lines)
        if not lines:
            return ""
        return "**执行过程**\n" + "\n".join(lines)

    def _render_tool_lines(self) -> list[str]:
        if not self.tools:
            return []

        failed = [tool for tool in self.tools if tool.status == "failed"]
        running = [
            tool for tool in self.tools if tool.status == "running"
        ]
        completed = [
            tool for tool in self.tools if tool.status == "completed"
        ]
        budget = MAX_VISIBLE_NON_FAILED_TOOLS
        selected_non_failed: list[ToolProjection] = []
        selected_non_failed.extend(running[-budget:])
        budget -= len(selected_non_failed)
        if budget > 0:
            selected_non_failed.extend(completed[-budget:])

        selected_keys = {
            tool.key for tool in (*failed, *selected_non_failed)
        }
        visible = [
            tool for tool in self.tools if tool.key in selected_keys
        ]
        lines = [
            self._render_tool(tool, index)
            for index, tool in enumerate(self.tools)
            if tool in visible
        ]
        omitted_running = len(running) - sum(
            tool.status == "running" for tool in visible
        )
        omitted_completed = len(completed) - sum(
            tool.status == "completed" for tool in visible
        )
        if omitted_running > 0:
            lines.append(f"另有 {omitted_running} 项正在处理")
        if omitted_completed > 0:
            lines.append(f"另有 {omitted_completed} 项操作已完成")
        return lines

    def _render_tool(self, tool: ToolProjection, index: int) -> str:
        descriptor = _special_tool_descriptor(tool.name)
        if descriptor is not None:
            running, completed, failed, action = descriptor
            suffix = _tool_input_suffix(tool.input_summary, action)
            return _status_line(
                tool.status,
                running=f"正在{running}{suffix}…",
                completed=f"✓ {completed}{suffix}",
                failed=f"⚠ {failed}{suffix}，请重试",
            )
        category = _tool_category(tool.name)
        filename = (
            f"《{tool.filename}》" if tool.filename else "文件"
        )
        prior_write = (
            category == "write"
            and tool.filename is not None
            and any(
                prior.status == "completed"
                and _tool_category(prior.name) == "write"
                and prior.filename == tool.filename
                for prior in self.tools[:index]
            )
        )

        if category == "write":
            verb = "更新" if prior_write else "写入"
            return _status_line(
                tool.status,
                running=f"正在{verb}{filename}…",
                completed=f"✓ 已{verb}{filename}",
                failed=f"⚠ {filename}{verb}失败，请重试",
            )
        if category == "terminal":
            return _status_line(
                tool.status,
                running="正在执行检查…",
                completed="✓ 检查完成",
                failed="⚠ 检查失败，请重试",
            )
        if category == "search":
            suffix = _tool_input_suffix(tool.input_summary, "查询")
            return _status_line(
                tool.status,
                running=f"正在搜索资料{suffix}…",
                completed=f"✓ 资料搜索完成{suffix}",
                failed=f"⚠ 资料搜索失败{suffix}，请重试",
            )
        if category == "read":
            return _status_line(
                tool.status,
                running=f"正在读取{filename}…",
                completed=f"✓ 已读取{filename}",
                failed=f"⚠ {filename}读取失败，请重试",
            )
        if category == "presentation":
            return _status_line(
                tool.status,
                running="正在生成演示文稿…",
                completed="✓ 演示文稿已生成",
                failed="⚠ 演示文稿生成失败，请重试",
            )
        if category == "image":
            return _status_line(
                tool.status,
                running="正在生成图片…",
                completed="✓ 图片已生成",
                failed="⚠ 图片生成失败，请重试",
            )
        if category == "browser":
            return _status_line(
                tool.status,
                running="正在浏览页面…",
                completed="✓ 页面处理完成",
                failed="⚠ 页面处理失败，请重试",
            )
        display_name = _safe_public_tool_name(tool.name)
        tool_kind = "MCP 工具" if "mcp" in tool.name.lower() else "工具调用"
        return _status_line(
            tool.status,
            running=f"正在执行{tool_kind} · {display_name}…",
            completed=f"✓ 已完成{tool_kind} · {display_name}",
            failed=f"⚠ {tool_kind}失败 · {display_name}，请重试",
        )

    def _apply_tool(self, data: dict[str, Any]) -> bool:
        name = str(
            data.get("name") or data.get("tool_name") or "tool"
        )
        status = _tool_status(data.get("status"))
        key, unstable = self._tool_key(name, status, data)
        filename = _extract_filename(data)
        existing = next(
            (tool for tool in self.tools if tool.key == key),
            None,
        )
        if existing is None:
            self.tools.append(
                ToolProjection(
                    key=key,
                    name=name,
                    status=status,
                    order=len(self.tools),
                    filename=filename,
                    input_summary=_tool_input_summary(name, data),
                    unstable=unstable,
                )
            )
            return True

        previous = (
            existing.name,
            existing.status,
            existing.filename,
            existing.input_summary,
        )
        existing.name = name
        if not (
            existing.status in {"completed", "failed"}
            and status == "running"
        ):
            existing.status = status
        if filename:
            existing.filename = filename
        input_summary = _tool_input_summary(name, data)
        if input_summary:
            existing.input_summary = input_summary
        return previous != (
            existing.name,
            existing.status,
            existing.filename,
            existing.input_summary,
        )

    def _tool_key(
        self,
        name: str,
        status: str,
        data: dict[str, Any],
    ) -> tuple[str, bool]:
        call_id = data.get("callId") or data.get("call_id")
        stable_flag = data.get("hasStableCallId")
        if stable_flag is None:
            stable_flag = data.get("has_stable_call_id")
        stable = bool(
            call_id
            and stable_flag is not False
            and not str(call_id).endswith(":unstable")
        )
        if stable:
            return f"stable:{call_id}", False

        if status != "running":
            for tool in self.tools:
                if (
                    tool.unstable
                    and tool.name == name
                    and tool.status == "running"
                ):
                    return tool.key, True
        self.unstable_counter += 1
        return f"unstable:{self.unstable_counter}", True

    def _append_activity(self, value: object) -> bool:
        line = _compact_line(value)
        if not line or (
            self.activity_lines and self.activity_lines[-1] == line
        ):
            return False
        self.activity_lines.append(line)
        return True

    def _close_reasoning(self) -> None:
        if self.reasoning_text:
            self.reasoning_closed = True


def sanitize_visible_text(value: str) -> str:
    if not value:
        return ""
    text = _BACKTICK_INTERNAL_PATH_RE.sub(
        lambda match: _display_filename(match.group("path")),
        value,
    )
    text = _PLAIN_INTERNAL_PATH_RE.sub(
        lambda match: _display_filename(match.group("path")),
        text,
    )
    text = _MULTIPLE_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _display_filename(path: str) -> str:
    filename = _safe_filename(path)
    return f"《{filename}》" if filename else "文件"


def _extract_filename(data: dict[str, Any]) -> str | None:
    for key in ("result", "preview", "input"):
        candidate = _filename_from_value(data.get(key))
        if candidate:
            return candidate
    return None


def _filename_from_value(value: object) -> str | None:
    if isinstance(value, str):
        try:
            structured = json.loads(value)
        except (TypeError, ValueError):
            structured = None
        if structured is not None:
            candidate = _filename_from_value(structured)
            if candidate:
                return candidate
        match = _PUBLIC_PATH_RE.search(value)
        if match:
            return _safe_filename(match.group("name"))
        return None
    if isinstance(value, dict):
        for key in _FILE_KEYS:
            if key not in value:
                continue
            candidate = _filename_from_path_field(value[key])
            if candidate:
                return candidate
        return None
    return None


def _filename_from_path_field(value: object) -> str | None:
    if isinstance(value, (list, tuple)):
        for item in value:
            candidate = _filename_from_path_field(item)
            if candidate:
                return candidate
        return None
    if not isinstance(value, str):
        return None
    return _safe_filename(value)


def _safe_filename(value: str) -> str | None:
    cleaned = value.strip().strip("`'\"")
    if not cleaned:
        return None
    cleaned = cleaned.replace("\\", "/")
    if "<path>/" in cleaned:
        cleaned = cleaned.rsplit("<path>/", 1)[-1]
    name = PurePosixPath(cleaned).name
    if not name:
        name = PureWindowsPath(value).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if not name or len(name) > 180:
        return None
    return name


def _restore_tool(value: object) -> ToolProjection | None:
    if not isinstance(value, dict):
        return None
    key = value.get("key")
    name = value.get("name")
    status = value.get("status")
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(name, str)
        or not name
        or status not in {"running", "completed", "failed"}
    ):
        return None
    filename = value.get("filename")
    return ToolProjection(
        key=key,
        name=name,
        status=str(status),
        order=max(0, _safe_int(value.get("order"))),
        filename=(
            str(filename)
            if isinstance(filename, str) and filename
            else None
        ),
        input_summary=(
            str(value.get("input_summary"))
            if isinstance(value.get("input_summary"), str)
            and value.get("input_summary")
            else None
        ),
        unstable=bool(value.get("unstable")),
    )


def _tool_status(value: object) -> str:
    status = str(value or "running").lower()
    if status in {"failed", "error"}:
        return "failed"
    if status in {"completed", "complete", "success", "succeeded"}:
        return "completed"
    return "running"


def _tool_category(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if normalized in {
        "write_file",
        "apply_patch",
        "save_file",
        "create_file",
        "edit_file",
    }:
        return "write"
    if normalized in {
        "terminal",
        "exec",
        "exec_command",
        "shell",
        "bash",
        "command",
    }:
        return "terminal"
    if "search" in normalized:
        return "search"
    if normalized in {"read_file", "open_file", "view_file"}:
        return "read"
    if any(
        token in normalized
        for token in ("presentation", "powerpoint", "ppt", "slides")
    ):
        return "presentation"
    if any(
        token in normalized
        for token in ("imagegen", "image_gen", "generate_image")
    ):
        return "image"
    if any(
        token in normalized for token in ("browser", "web_open", "web_click")
    ):
        return "browser"
    return "unknown"


def _special_tool_descriptor(
    name: str,
) -> tuple[str, str, str, str] | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if "sql_context" in normalized:
        return "获取数据口径", "已获取数据口径", "数据口径获取失败", "获取"
    if "sql_query" in normalized:
        return "查询企业数据", "已查询企业数据", "企业数据查询失败", "查询"
    if "enterprise_knowledge_read" in normalized or "knowledge_read" in normalized:
        return "读取企业知识", "已读取企业知识", "企业知识读取失败", "读取"
    if "enterprise_document_search" in normalized:
        return "检索企业知识库", "已检索企业知识库", "企业知识检索失败", "检索"
    if "personal_knowledge_search" in normalized:
        return "检索个人知识库", "已检索个人知识库", "个人知识检索失败", "检索"
    if "skill_manage" in normalized:
        return "管理技能", "已完成技能管理", "技能管理失败", "处理"
    if "skill_view" in normalized or "skill_describe" in normalized:
        return "查看能力说明", "已查看能力说明", "查看能力说明失败", "查看"
    if "expert_list" in normalized:
        return "查看专家列表", "已查看专家列表", "专家列表获取失败", "查看"
    if normalized == "clarify" or normalized.endswith("_clarify"):
        return "等待确认", "已提交确认请求", "确认请求失败", "等待"
    if "kanban_create" in normalized:
        return "创建协作任务", "已创建协作任务", "协作任务创建失败", "创建"
    if "kanban_" in normalized:
        return "更新协作任务", "已更新协作任务", "协作任务更新失败", "更新"
    if "cronjob" in normalized or "scheduled_task" in normalized:
        return "处理定时任务", "已处理定时任务", "定时任务处理失败", "处理"
    return None


def _tool_input_suffix(summary: str | None, action: str) -> str:
    return f" · {action}“{summary}”" if summary else ""


def _safe_public_tool_name(name: str) -> str:
    candidates = [part for part in re.split(r"__|[.:/]", name.strip()) if part]
    candidate = candidates[-1] if candidates else "未命名工具"
    candidate = re.sub(r"[^a-zA-Z0-9_-]+", "_", candidate).strip("_")
    return (candidate or "未命名工具")[:80]


def _tool_input_summary(name: str, data: dict[str, Any]) -> str | None:
    raw = data.get("input")
    if raw is None:
        return None
    safe = sanitize_public_tool_text(raw)
    if not safe:
        return None
    try:
        value: object = json.loads(safe)
    except (TypeError, ValueError):
        return _compact_line(safe)
    if isinstance(value, dict):
        for wrapper in ("arguments", "args", "input", "params", "parameters"):
            nested = value.get(wrapper)
            if isinstance(nested, dict):
                value = nested
                break
    if not isinstance(value, dict):
        return _compact_line(str(value))
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if "knowledge_read" in normalized or "primer" in normalized:
        keys = ("relative_path", "file_path", "path", "name")
    elif "sql_context" in normalized:
        keys = ("query", "question", "domain")
    elif "sql_query" in normalized:
        keys = ("sql", "query")
    elif "document_search" in normalized or "knowledge_search" in normalized:
        keys = ("question", "query", "q", "search")
    elif "clarify" in normalized:
        keys = ("question", "prompt")
    elif "skill_manage" in normalized:
        keys = ("action", "name", "skill_name")
    elif "search" in normalized:
        keys = ("query", "question", "q", "search", "pattern")
    else:
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return _compact_line(candidate)
    return None


def _status_line(
    status: str,
    *,
    running: str,
    completed: str,
    failed: str,
) -> str:
    if status == "completed":
        return completed
    if status == "failed":
        return failed
    return running


def _localized_status(status: str) -> str:
    normalized = status.lower()
    if normalized in {"completed", "complete", "success", "succeeded"}:
        return "已完成"
    if normalized in {"failed", "error"}:
        return "执行失败"
    return "正在执行"


def _compact_line(value: object) -> str:
    text = sanitize_visible_text(str(value or ""))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > 200:
        text = text[:197] + "…"
    return text


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _utf8_prefix(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")
