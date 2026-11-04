from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.hermes_runtime.constants import PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID
from app.hermes_runtime.skill_visibility_policy import (
    BUNDLED_HERMES_SKILL_ROOT,
    OFFICIAL_HERMES_SKILL_ROOT,
    READONLY_HERMES_SKILL_ROOT,
    SKILL_VISIBILITY_POLICY_V1,
    SKILL_VISIBILITY_POLICY_V2,
    SKILL_VISIBILITY_POLICY_V3,
    explicitly_installed_skill_names,
    load_legacy_upstream_skill_names,
    resolve_versioned_skill_visibility,
)
from app.services.model_defaults import DEFAULT_MODEL_CONTEXT_LENGTH


DEFAULT_RUNTIME_SOUL = (
    "# 默认数字人\n\n"
    "这是平台托管的个人数字人。\n"
    "当前用户身份以本文件中的平台身份区块为准。\n"
    "当用户使用中文时，所有面向用户公开展示的内容，包括 reasoning 摘要和进度说明，"
    "统一使用简体中文。\n"
    "\n"
    "## 专家协作路由\n\n"
    "当用户明确指定已安装专家，或要求召唤、委派、协同专家时，必须先调用 "
    "`expert_list` 获取当前可用专家及其真实 assignee，再使用 Hermes 原生 "
    "`kanban_create` 把任务派给该 assignee。\n"
    "不得使用 `delegate_task` 代替已安装专家；`delegate_task` 只用于与专家无关的"
    "当前 Run 内普通临时推理子任务。\n"
    "如果专家目录查询或 Kanban 创建失败，应明确告知用户派工失败，不得猜测 "
    "assignee，也不得静默退化为临时子 Agent。\n"
    "\n"
    "## 桌面本地能力\n\n"
    "桌面客户端在线时，只要请求涉及用户电脑上的文件、目录、命令或浏览器，默认必须使用 "
    "desktop_workspace_*、desktop_command_run、desktop_browser_open 或 desktop_browser_* 自动化工具。"
    "此时平台会在服务器 terminal、服务器文件写入和云端浏览器工具前自动拦截并引导改用桌面工具；"
    "确实需要云端执行（本地缺少依赖、用户明确要求）时，按拦截提示说明原因后再次调用即可继续。"
    "desktop_workspace_* 和 desktop_command_run 在已选本地工作目录内直接执行；访问目录外内容时，"
    "桌面端会在聊天区请求确认。desktop_browser_open 会调用用户操作系统的默认浏览器，不使用云端或内置浏览器。"
    "需要网页自动化时，使用 desktop_browser_session_start 启动用户本机已安装的 Chrome/Edge，"
    "再使用 desktop_browser_navigate、desktop_browser_snapshot、desktop_browser_click、"
    "desktop_browser_type、desktop_browser_press、desktop_browser_scroll 或 desktop_browser_screenshot。"
    "这些工具操作的是桌面端的可见浏览器会话。"
    "不要把服务器 terminal 的路径当作用户电脑路径；如果桌面客户端不可用，应明确说明，"
    "不要假装已经访问或修改了本地文件。\n"
    "\n"
    "### 本地文件产物\n\n"
    "用户要求生成文件产物（PPT、Word、Excel、PDF、图表、图片等）时，默认在用户本地生成。"
    "纯文本类产物（HTML、Markdown、TXT、CSV）无需探测本地库，直接 desktop_workspace_write "
    "写入目标路径即可（写入不存在的子目录会自动创建）。"
    "标准流程：先用 desktop_command_run 探测本地库"
    "（例如 python -c \"import pptx\"）；库可用时用 desktop_workspace_write 把生成脚本"
    "写成工作目录内的 .py 文件（命令行长度有限，禁止把长脚本直接塞进 command 参数），"
    "再用 desktop_command_run 执行该脚本，输出文件用相对路径保存，产物就直接落在"
    "用户的工作文件夹；完成后用 desktop_workspace_list 确认文件存在，并告知用户位置"
    "（list 响应的 root 字段是工作文件夹的绝对路径，报位置时给出完整路径）。"
    "数据较多的任务（网络检索、页面导出等来源）应先把数据分批写成工作目录内的中间数据文件"
    "（例如 data1.csv、data2.csv），由脚本读取合并生成最终产物，不要把大量数据"
    "塞进单个脚本。"
    "本地缺少所需库时不要擅自安装依赖（内网可能不通且属于变更操作），改在服务器生成，"
    "并说明文件在云端任务文件面板、需要用户手动下载。本地单条命令有 120 秒超时，"
    "大批量生成应拆分成多次执行。\n"
    "\n"
    "### Skill 脚本的执行位置\n\n"
    "技能默认在云端 Runtime 中按 SKILL.md 的指示执行。判断标准是输入输出位置："
    "当技能的输入文件来自本地工作文件夹，或产物需要落到本地时，把脚本改为本地执行——"
    "先在云端读取技能目录里的脚本与必要的小型资源文件，用 desktop_workspace_write "
    "复制到工作区子目录（例如 .skill-work/<技能名>/，保持包内相对路径结构），"
    "再用 desktop_command_run 在本地执行；纯计算、数据在上下文或云端的技能保持云端执行。"
    "本地执行前先探测依赖（例如 python -c 导入）：bash 入口的脚本在 Windows 桌面"
    "通常无法直接执行，依赖云端镜像内 CLI、字体或缺失库的技能保持云端执行，"
    "产物按上文本地文件产物规则交付。技能附带的大体积资源文件无法经上下文转写，"
    "此类任务保持云端执行。\n"
    "\n"
    "### 浏览器自动化流程\n\n"
    "执行需要读取或操作网页的任务时，只使用 desktop_browser_* 自动化通道，"
    "从 desktop_browser_session_start 开始。不要先调用 desktop_browser_open："
    "它只在用户默认浏览器里展示页面，你无法读取或控制那个页面；"
    "仅当用户明确要求用默认浏览器打开且不需要自动化时才使用它。\n"
    "desktop_browser_navigate 只导航到用户任务给出的原始 URL，不要自行替换成登录页地址"
    "（例如 userlogin.aspx）；同一 URL 已导航过时不要重复导航，先用 desktop_browser_snapshot"
    "确认当前页面状态。\n"
    "navigate 之后页面可能经过零信任网关认证或站点登录页重定向。snapshot 显示停在认证页"
    "或登录页、或返回 loginRequired 标记时，这不是通道故障：用中文提示用户在自动化浏览器"
    "窗口完成登录，然后用 desktop_browser_wait 等待页面变化（超时是正常结果，可再次等待），"
    "最多等待约 10 分钟。浏览器登录态由本地配置目录持久保存，下次会话通常无需重复登录。\n"
    "需要页面内容时用 desktop_browser_read 读正文，需要操作元素时用 desktop_browser_snapshot，"
    "元素很多时用 snapshot 的 offset/limit 翻页；点击、输入等动作的返回自带最新快照，不必每次额外 snapshot。"
    "下拉框用 desktop_browser_select，"
    "悬停菜单用 desktop_browser_hover，上传工作文件夹内的文件用 desktop_browser_upload，"
    "页面触发的下载用 desktop_browser_downloads 查询结果，多标签页用 desktop_browser_tab_* 管理。\n"
    "不要要求用户把账号、密码或验证码发送到聊天中，也不要用 desktop_browser_type 代填凭据，"
    "除非用户主动明确要求。\n"
    "页面元素引用来自最近一次 snapshot；页面跳转或切换标签页后旧引用会失效，此时重新 snapshot，"
    "不要凭记忆猜测元素。任务完成或用户取消后调用 desktop_browser_session_close；"
    "任务中途不要重启自动化会话，重启会丢失当前页面状态。\n"
)

KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID = "kanban-orchestrator-guard"
DESKTOP_LOCAL_PLUGIN_ID = "desktop-local-bridge"
KANBAN_ORCHESTRATOR_SYSTEM_PROMPT = (
    "当前 default Profile 是平台交互式 Kanban Orchestrator，不是 Dispatcher "
    "分配的单任务 Worker。\n\n"
    "只有运行时存在具体 HERMES_KANBAN_TASK 的专家 Worker 才适用 "
    "“You have been assigned ONE task”生命周期提示；该提示不适用于当前 "
    "default Profile 的普通交互会话。\n\n"
    "普通问候、闲聊、知识问答和不涉及任务管理的请求不得调用 Kanban 工具。"
    "当用户要求专家派工或管理已有专家任务时，应主动使用原生 Kanban 工具。"
    "Orchestrator 调用 show、complete、block、heartbeat 等任务级工具时，"
    "必须提供明确 task_id；不得先无参数调用 kanban_show 猜测任务。"
)
KANBAN_ORCHESTRATOR_GUARD_MANIFEST = (
    "name: kanban-orchestrator-guard\n"
    'version: "1.0.0"\n'
    "description: Guard the default Kanban orchestrator from worker-only calls.\n"
)
KANBAN_ORCHESTRATOR_GUARD_SOURCE = """\
from __future__ import annotations

import logging
import os
from typing import Any


logger = logging.getLogger(__name__)
_WORKER_DEFAULT_TOOLS = {
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_heartbeat",
}
_BLOCK_MESSAGE = (
    "当前 Profile 是 Kanban Orchestrator，不是单任务 Worker。"
    "普通对话请直接响应用户；如需管理任务，请先使用 kanban_list，"
    "或使用带明确 task_id 的任务级调用。"
)


def _tool_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    value = kwargs.get("args") or kwargs.get("tool_input")
    return value if isinstance(value, dict) else {}


def register(ctx) -> None:
    def pre_tool_call(**kwargs: Any):
        if os.environ.get("HERMES_KANBAN_TASK"):
            return None
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name not in _WORKER_DEFAULT_TOOLS:
            return None
        task_id = str(_tool_args(kwargs).get("task_id") or "").strip()
        if task_id:
            return None
        logger.warning(
            "kanban orchestrator guard blocked worker-default call "
            "tool=%s session_id=%s tool_call_id=%s",
            tool_name,
            str(kwargs.get("session_id") or ""),
            str(kwargs.get("tool_call_id") or ""),
        )
        return {"action": "block", "message": _BLOCK_MESSAGE}

    ctx.register_hook("pre_tool_call", pre_tool_call)
"""


def kanban_orchestrator_guard_artifacts() -> dict[str, str]:
    root = f"plugins/{KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID}"
    return {
        f"{root}/plugin.yaml": KANBAN_ORCHESTRATOR_GUARD_MANIFEST,
        f"{root}/__init__.py": KANBAN_ORCHESTRATOR_GUARD_SOURCE,
    }


DESKTOP_LOCAL_PLUGIN_MANIFEST = (
    "name: desktop-local-bridge\n"
    'version: "1.0.1"\n'
    "description: Request approved local capabilities from a connected desktop client.\n"
)
DESKTOP_LOCAL_PLUGIN_SOURCE = """\
from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4


_CAPABILITY_PATH_FIELDS = {
    "workspace.list": "path",
    "workspace.read": "path",
    "workspace.write": "path",
    "workspace.mkdir": "path",
    "workspace.trash": "path",
    "browser.upload": "path",
}


def _text(value: Any, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _request(capability: str, params: dict[str, Any], **context: Any) -> str:
    session_id = _text(context.get("session_id"), 256)
    if not session_id:
        return json.dumps({"error": "desktop local capability requires a session"}, ensure_ascii=False)
    args = dict(params or {})
    path_field = _CAPABILITY_PATH_FIELDS.get(capability)
    if path_field is not None:
        path = args.get(path_field)
        if capability == "workspace.list" and path in (None, ""):
            args[path_field] = ""
            path = ""
        if not isinstance(path, str) or (capability != "workspace.list" and not path.strip()) or "\\x00" in path:
            return json.dumps({"error": "desktop path is invalid"}, ensure_ascii=False)
    if capability == "browser.open" and not _text(args.get("url"), 2048).startswith(("http://", "https://")):
        return json.dumps({"error": "desktop browser only accepts http or https URLs"}, ensure_ascii=False)
    if capability == "browser.navigate" and not _text(args.get("url"), 4096).startswith(("http://", "https://")):
        return json.dumps({"error": "desktop browser only accepts http or https URLs"}, ensure_ascii=False)
    if capability == "browser.tab_new" and args.get("url") not in (None, "") and not _text(args.get("url"), 4096).startswith(("http://", "https://")):
        return json.dumps({"error": "desktop browser only accepts http or https URLs"}, ensure_ascii=False)
    tool_call_id = _text(context.get("tool_call_id"), 128)
    request_id = tool_call_id if len(tool_call_id) >= 8 else f"desktop-{uuid4().hex}"
    body = {
        "request_id": request_id,
        "session_id": session_id,
        "capability": capability,
        "args": args,
        "reason": _text(context.get("turn_id"), 512) or None,
    }
    base = os.environ.get("HERMES_RUNTIME_SUPERVISOR_URL", "http://127.0.0.1:9120").rstrip("/")
    request = Request(
        base + "/internal/v1/local-capabilities/request",
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + os.environ.get("API_SERVER_KEY", ""),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        # A directory-outside request is displayed as a chat Clarify card in
        # the desktop. Keep the supervisor bridge open while the user answers.
        with urlopen(request, timeout=600) as response:
            payload = response.read(256 * 1024)
        return payload.decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as exc:
        return json.dumps({"error": "desktop local capability unavailable", "detail": str(exc)[:256]}, ensure_ascii=False)


# Cloud-side substitutes are invisible to the desktop: files written by the
# server terminal / write_file land in the cloud workspace with no approval,
# audit, or local visibility. While the session's desktop is online the gate
# below blocks them once and steers the agent to the desktop_* equivalents;
# a deliberate second call goes through (missing local deps, explicit user
# request for server execution). Probe failures keep the gate open, so the
# worst case degrades to the previous prompt-only discipline.
_CLOUD_TOOL_GUIDANCE = {
    "terminal": (
        "本会话绑定了桌面客户端，命令默认在用户本地执行："
        "查看用户本地目录结构、读取文件用 desktop_workspace_list / desktop_workspace_read；"
        "生成产物先用 desktop_workspace_write 把脚本写成工作目录内的 .py 文件，"
        "再用 desktop_command_run 执行。若本地缺少依赖或用户明确要求服务器执行，"
        "请说明原因后再次调用本工具继续。"
    ),
    "read_file": (
        "本会话绑定了桌面客户端，用户提到的文件/项目通常在其选择的本地工作文件夹："
        "请改用 desktop_workspace_list 列出目录、desktop_workspace_read 读取文件内容。"
        "若确实要读云端任务文件，请说明原因后再次调用本工具继续。"
    ),
    "search_files": (
        "本会话绑定了桌面客户端，搜索用户电脑上的文件请改用 desktop_workspace_list "
        "遍历已选本地工作文件夹；若确实要搜索云端任务目录，"
        "请说明原因后再次调用本工具继续。"
    ),
    "write_file": (
        "本会话绑定了桌面客户端，文件默认写入用户本地工作文件夹："
        "请改用 desktop_workspace_write（相对路径）。若本地无法生成或用户明确要求云端文件，"
        "请说明原因后再次调用本工具继续。"
    ),
    "browser_": (
        "本会话绑定了桌面客户端，浏览器操作默认使用用户本机的可见浏览器："
        "请改用 desktop_browser_session_start 启动，再配合 desktop_browser_navigate、"
        "desktop_browser_snapshot、desktop_browser_click 等工具。若确需云端沙盒浏览器"
        "（本地浏览器不可用且用户知情），请说明原因后再次调用本工具继续。"
    ),
}

_DESKTOP_STATUS_TTL_SECONDS = 60.0
_desktop_status_cache: dict[str, tuple[float, bool]] = {}
_cloud_tool_block_seen: set[tuple[str, str]] = set()


def _probe_desktop_online(session_id: str) -> bool:
    base = os.environ.get("HERMES_RUNTIME_SUPERVISOR_URL", "http://127.0.0.1:9120").rstrip("/")
    request = Request(
        base + "/internal/v1/desktop-status?session_id=" + quote(session_id, safe=""),
        headers={"Authorization": "Bearer " + os.environ.get("API_SERVER_KEY", "")},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(8192).decode("utf-8"))
        return bool(payload.get("online"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError):
        return False


def _desktop_online(session_id: str) -> bool:
    now = time.monotonic()
    cached = _desktop_status_cache.get(session_id)
    if cached is not None and now - cached[0] < _DESKTOP_STATUS_TTL_SECONDS:
        return cached[1]
    online = _probe_desktop_online(session_id)
    if not online:
        # 桌面离线后清掉该会话的首拦标记，重新上线时闸门恢复完整引导。
        for key in [key for key in _cloud_tool_block_seen if key[0] == session_id]:
            _cloud_tool_block_seen.discard(key)
    if len(_desktop_status_cache) > 512:
        for stale in sorted(_desktop_status_cache, key=lambda item: _desktop_status_cache[item][0])[:256]:
            _desktop_status_cache.pop(stale, None)
    _desktop_status_cache[session_id] = (now, online)
    return online


def _cloud_tool_guidance(tool_name: str) -> str | None:
    if tool_name in _CLOUD_TOOL_GUIDANCE:
        return _CLOUD_TOOL_GUIDANCE[tool_name]
    for prefix, message in _CLOUD_TOOL_GUIDANCE.items():
        if prefix.endswith("_") and tool_name.startswith(prefix):
            return message
    return None


def _pre_tool_call(**kwargs: Any):
    tool_name = _text(kwargs.get("tool_name"), 128)
    guidance = _cloud_tool_guidance(tool_name)
    if guidance is None:
        return None
    session_id = _text(kwargs.get("session_id"), 256)
    if not session_id or not _desktop_online(session_id):
        # Web 会话或桌面离线：云端工具是正确路径，放行。
        return None
    key = (session_id, tool_name)
    if key in _cloud_tool_block_seen:
        # 首拦放行：被引导一次后仍再次调用，视为显式确认走云端
        # （本地缺库回落、用户明确要求服务器执行）。
        return None
    _cloud_tool_block_seen.add(key)
    return {"action": "block", "message": guidance}


def register(ctx) -> None:
    schemas = {
        "desktop_workspace_list": ({"path": {"type": "string"}}, []),
        "desktop_workspace_read": ({"path": {"type": "string"}}, ["path"]),
        "desktop_workspace_write": ({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        "desktop_workspace_mkdir": ({"path": {"type": "string"}}, ["path"]),
        "desktop_workspace_trash": ({"path": {"type": "string"}}, ["path"]),
        "desktop_command_run": ({"command": {"type": "string"}}, ["command"]),
        "desktop_browser_open": ({"url": {"type": "string", "format": "uri"}}, ["url"]),
        "desktop_browser_session_start": ({"browser": {"type": "string", "enum": ["chrome", "edge"]}}, []),
        "desktop_browser_navigate": ({"session_id": {"type": "string"}, "url": {"type": "string", "format": "uri"}, "wait_until": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"]}, "timeout_ms": {"type": "number"}}, ["session_id", "url"]),
        "desktop_browser_snapshot": ({"session_id": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}}, ["session_id"]),
        "desktop_browser_click": ({"session_id": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"}, "confirm": {"type": "boolean", "description": "Set true only for irreversible actions (paying, placing orders, deleting data, publishing or sending content to other people); omit it for ordinary clicks."}, "with_snapshot": {"type": "boolean"}}, ["session_id"]),
        "desktop_browser_type": ({"session_id": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"}, "mode": {"type": "string", "enum": ["replace", "keystrokes"]}, "with_snapshot": {"type": "boolean"}}, ["session_id", "text"]),
        "desktop_browser_press": ({"session_id": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"}, "key": {"type": "string"}, "with_snapshot": {"type": "boolean"}}, ["session_id", "key"]),
        "desktop_browser_scroll": ({"session_id": {"type": "string"}, "direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "number"}}, ["session_id"]),
        "desktop_browser_screenshot": ({"session_id": {"type": "string"}}, ["session_id"]),
        "desktop_browser_session_close": ({"session_id": {"type": "string"}}, ["session_id"]),
        "desktop_browser_read": ({"session_id": {"type": "string"}, "max_chars": {"type": "number"}, "ref": {"type": "string"}, "selector": {"type": "string"}}, ["session_id"]),
        "desktop_browser_wait": ({"session_id": {"type": "string"}, "until": {"type": "string", "enum": ["element", "text", "url", "load", "hidden"]}, "value": {"type": "string"}, "timeout_ms": {"type": "number"}}, ["session_id", "until"]),
        "desktop_browser_hover": ({"session_id": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"}, "with_snapshot": {"type": "boolean"}}, ["session_id"]),
        "desktop_browser_select": ({"session_id": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}, "index": {"type": "number"}}, ["session_id"]),
        "desktop_browser_upload": ({"session_id": {"type": "string"}, "ref": {"type": "string"}, "selector": {"type": "string"}, "path": {"type": "string"}}, ["session_id", "path"]),
        "desktop_browser_downloads": ({"session_id": {"type": "string"}}, ["session_id"]),
        "desktop_browser_back": ({"session_id": {"type": "string"}, "wait_until": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"]}, "timeout_ms": {"type": "number"}}, ["session_id"]),
        "desktop_browser_reload": ({"session_id": {"type": "string"}, "wait_until": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"]}, "ignore_cache": {"type": "boolean"}, "timeout_ms": {"type": "number"}}, ["session_id"]),
        "desktop_browser_tab_list": ({"session_id": {"type": "string"}}, ["session_id"]),
        "desktop_browser_tab_new": ({"session_id": {"type": "string"}, "url": {"type": "string", "format": "uri"}}, ["session_id"]),
        "desktop_browser_tab_select": ({"session_id": {"type": "string"}, "tab_index": {"type": "number"}}, ["session_id", "tab_index"]),
        "desktop_browser_tab_close": ({"session_id": {"type": "string"}, "tab_index": {"type": "number"}}, ["session_id"]),
    }
    capabilities = {
        "desktop_workspace_list": "workspace.list",
        "desktop_workspace_read": "workspace.read",
        "desktop_workspace_write": "workspace.write",
        "desktop_workspace_mkdir": "workspace.mkdir",
        "desktop_workspace_trash": "workspace.trash",
        "desktop_command_run": "command.run",
        "desktop_browser_open": "browser.open",
        "desktop_browser_session_start": "browser.session_start",
        "desktop_browser_navigate": "browser.navigate",
        "desktop_browser_snapshot": "browser.snapshot",
        "desktop_browser_click": "browser.click",
        "desktop_browser_type": "browser.type",
        "desktop_browser_press": "browser.press",
        "desktop_browser_scroll": "browser.scroll",
        "desktop_browser_screenshot": "browser.screenshot",
        "desktop_browser_session_close": "browser.session_close",
        "desktop_browser_read": "browser.read",
        "desktop_browser_wait": "browser.wait",
        "desktop_browser_hover": "browser.hover",
        "desktop_browser_select": "browser.select",
        "desktop_browser_upload": "browser.upload",
        "desktop_browser_downloads": "browser.downloads",
        "desktop_browser_back": "browser.back",
        "desktop_browser_reload": "browser.reload",
        "desktop_browser_tab_list": "browser.tab_list",
        "desktop_browser_tab_new": "browser.tab_new",
        "desktop_browser_tab_select": "browser.tab_select",
        "desktop_browser_tab_close": "browser.tab_close",
    }
    descriptions = {
        "desktop_workspace_list": "Desktop-first: list entries in the user's selected local desktop workspace. Do not use the cloud Runtime filesystem for this.",
        "desktop_workspace_read": "Desktop-first: read a UTF-8 text file from the user's selected local desktop workspace. Do not use the cloud Runtime filesystem for this.",
        "desktop_workspace_write": "Desktop-first: write a UTF-8 text file in the user's selected local workspace. Also use it to stage generator scripts (.py) that desktop_command_run executes locally to produce binary deliverables (PPTX/XLSX/PDF). Do not use cloud files; outside paths are confirmed in the desktop chat.",
        "desktop_workspace_mkdir": "Desktop-first: create a directory on the user's local desktop. Do not use the cloud Runtime filesystem.",
        "desktop_workspace_trash": "Desktop-first: move a local desktop item to the recycle bin. Do not use the cloud Runtime filesystem.",
        "desktop_command_run": "Desktop-first: run a shell command on the user's local desktop with the selected workspace as working directory, so generated files land directly in the user's folder. For file deliverables (PPT/Word/Excel/PDF/charts) generate locally: stage a .py script with desktop_workspace_write first (command length is limited; 120s timeout), then run it here. Do not install missing packages without asking the user; fall back to server-side generation and say where to download. Do not use the cloud Runtime terminal unless the user explicitly requests server execution.",
        "desktop_browser_open": "Desktop-first: open an HTTP or HTTPS URL with the user's operating system default browser. For showing a page to the user only; you cannot read or control that page. Do not use it for automation tasks.",
        "desktop_browser_session_start": "Desktop-first: start a visible local Chrome or Edge automation session for reading and operating web pages. The local profile keeps login state between runs. Reuse the running session instead of restarting it.",
        "desktop_browser_navigate": "Desktop-first: navigate a local browser automation session to an HTTP or HTTPS URL. Use the exact URL the user provided; do not substitute login-page URLs. Snapshot first when the session may already show the target.",
        "desktop_browser_snapshot": "Desktop-first: inspect the current local browser page (including iframes and open shadow roots) and return accessible element refs for later actions. Supports offset/limit paging for large pages; password values are never returned.",
        "desktop_browser_click": "Desktop-first: click an element in the local browser using a snapshot ref or CSS selector; returns an after-snapshot. Set confirm=true only for clicks with irreversible external effects: paying, placing orders, deleting data, or publishing/sending content to other people on the user's behalf. Ordinary clicks (focusing an input, search, pagination, login, sending a query to an AI chat page) must omit confirm — every confirm=true interrupts the user with an approval card.",
        "desktop_browser_type": "Desktop-first: fill a field in the local browser using a snapshot ref or CSS selector; mode=keystrokes types key by key for autocomplete fields. Do not expose secrets in reasoning.",
        "desktop_browser_press": "Desktop-first: press a key in the local browser session, optionally targeting a snapshot ref; returns an after-snapshot.",
        "desktop_browser_scroll": "Desktop-first: scroll the visible local browser page up or down.",
        "desktop_browser_screenshot": "Desktop-first: capture a compact screenshot of the visible local browser page.",
        "desktop_browser_session_close": "Desktop-first: close a local browser automation session when the task is finished.",
        "desktop_browser_read": "Desktop-first: read the visible text content of the current local browser page, or pass ref/selector to get that element's HTML structure. Prefer this over repeated snapshots when you need content or markup details.",
        "desktop_browser_wait": "Desktop-first: wait until an element appears/disappears, a text or URL shows, or the page load state settles. Use after login, navigation or slow async pages instead of polling snapshots; a timeout is an expected outcome, not a failure.",
        "desktop_browser_hover": "Desktop-first: hover over an element in the local browser (menus that expand on hover); returns an after-snapshot.",
        "desktop_browser_select": "Desktop-first: choose an option of a <select> dropdown by value, label or index.",
        "desktop_browser_upload": "Desktop-first: upload a local file from the user's selected workspace folder into a file input on the page. The desktop asks the user to confirm every upload.",
        "desktop_browser_downloads": "Desktop-first: list downloads triggered by the local browser session, with state and saved path (workspace .downloads folder).",
        "desktop_browser_back": "Desktop-first: go back to the previous page in the local browser history.",
        "desktop_browser_reload": "Desktop-first: reload the current local browser page, optionally ignoring cache.",
        "desktop_browser_tab_list": "Desktop-first: list the tabs of the local browser automation session; popups and target=_blank pages are attached automatically.",
        "desktop_browser_tab_new": "Desktop-first: open a new tab in the local browser automation session and make it active.",
        "desktop_browser_tab_select": "Desktop-first: switch the active tab of the local browser automation session; all other tools operate on the active tab.",
        "desktop_browser_tab_close": "Desktop-first: close one tab of the local browser automation session.",
    }
    for name, (properties, required) in schemas.items():
        schema = {
            "name": name,
            "description": descriptions[name],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }
        capability = capabilities[name]
        ctx.register_tool(
            name=name,
            toolset="desktop_local",
            schema=schema,
            handler=lambda params, _capability=capability, **context: _request(_capability, params, **context),
            description=descriptions[name],
        )
    ctx.register_hook("pre_tool_call", _pre_tool_call)
"""


def desktop_local_plugin_artifacts() -> dict[str, str]:
    root = f"plugins/{DESKTOP_LOCAL_PLUGIN_ID}"
    return {
        f"{root}/plugin.yaml": DESKTOP_LOCAL_PLUGIN_MANIFEST,
        f"{root}/__init__.py": DESKTOP_LOCAL_PLUGIN_SOURCE,
    }


@dataclass(frozen=True)
class ProjectionInput:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_id: str
    config_generation: int
    model_relay_base_url: str
    hermes_home: Path
    workspace_dir: Path
    name: str
    runtime_agent_profile_id: int | None = None
    default_model_id: int | None = None
    visible_models: tuple[dict[str, Any], ...] = ()
    installed_experts: tuple[dict[str, Any], ...] = ()
    installed_skills: tuple[dict[str, Any], ...] = ()
    user_disabled_skills: tuple[str, ...] = ()
    installed_mcps: tuple[dict[str, Any], ...] = ()
    bound_knowledge: tuple[dict[str, Any], ...] = ()
    config_schema: int = 33
    skill_policy_version: int | None = None
    bootstrap_config: bool = False


@dataclass(frozen=True)
class ProjectionSnapshot:
    org_id: int
    user_id: int
    runtime_workspace_id: int
    runtime_id: str
    config_generation: int
    config_hash: str
    scopes: list[str]
    rendered_at: str
    requires_reload: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "user_id": self.user_id,
            "runtime_workspace_id": self.runtime_workspace_id,
            "runtime_id": self.runtime_id,
            "config_generation": self.config_generation,
            "config_hash": self.config_hash,
            "scopes": self.scopes,
            "rendered_at": self.rendered_at,
            "requires_reload": self.requires_reload,
        }


class _ConfigDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow=flow, indentless=False)


def _represent_config_string(dumper: yaml.SafeDumper, value: str) -> yaml.nodes.ScalarNode:
    node = dumper.represent_scalar("tag:yaml.org,2002:str", value)
    if ProfileConfigRenderer.ENV_PLACEHOLDER_RE.fullmatch(value):
        node.style = '"'
    return node


_ConfigDumper.add_representer(str, _represent_config_string)


class ProfileConfigRenderer:
    """Whitelist the non-sensitive runtime shape emitted to ``config.yaml``.

    Asset records can contain catalog/database metadata that Hermes must never
    receive.  Runtime credentials are represented only by ``${ENV_VAR}``
    placeholders; Kubernetes injects their values into the Runtime process.
    """

    CONFIG_VERSION = 33
    TARGET_CONFIG_VERSION = 34
    # Platform-managed Hermes compaction tuning for mixed 128K / 1M catalogs:
    # - max_attempts high enough that overflow recovery keeps retrying;
    # - target_ratio at the Hermes clamp floor halves the protected-tail
    #   budget, so each compaction keeps a large compressible middle window
    #   and clears the <10%-savings anti-thrash breaker instead of tripping
    #   it (runtime-10002). The summary budget is a separate Hermes constant
    #   and is NOT reduced by this value;
    # - proactive_prune_tokens enables the deterministic no-LLM tool-result
    #   prune early on both 128K (50% of the window) and 1M models, with
    #   Hermes-side re-arm plus reclaim gates keeping cache breaks episodic.
    # Deliberately no global threshold_tokens cap: it would clamp the 1M
    # model's ~50% trigger down to the 128K model's needs.
    COMPRESSION_CONFIG: dict[str, Any] = {
        "max_attempts": 10,
        "target_ratio": 0.10,
        "proactive_prune_tokens": 64000,
    }
    # Platform rendering baseline epoch for application Runtime ConfigBundle
    # artifacts. Artifacts are addressed by (agent generation, baseline
    # epoch): bump this whenever a platform-side renderer change must reach
    # EXISTING application Agents without a business config edit (for example
    # COMPRESSION_CONFIG above). Epoch 1 is the legacy layout without a
    # suffix and remains readable for KServices created before this field.
    BASELINE_EPOCH = 2
    SUPPORTED_CONFIG_VERSIONS = frozenset(
        {CONFIG_VERSION, TARGET_CONFIG_VERSION}
    )
    MCP_ALLOWED_FIELDS = frozenset(
        {
            "name",
            "transport",
            "enabled",
            "url",
            "command",
            "args",
            "headers",
            "env",
        }
    )
    MCP_METADATA_FIELDS = frozenset(
        {"slug", "mcp_id", "version", "version_id", "description", "provider", "catalog_id"}
    )
    ENV_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
    SECRETISH_FIELDS = frozenset(
        {
            "api_key",
            "apikey",
            "token",
            "password",
            "secret",
            "private_key",
            "credential",
        }
    )
    FORBIDDEN_ASSET_KEYS = frozenset(
        {
            "api_key",
            "apikey",
            "api_key_encrypted",
            "provider_key",
            "provider_api_key",
            "secret",
            "mcp_secret",
            "password",
            "private_key",
            "token",
            "runtime_token",
            "runtime_id",
            "runtime_workspace_id",
            "workspace_id",
            "business_workspace_id",
            "copaw_agent_id",
            "session_id",
            "session_history",
            "history",
            "host_path",
            "kubeconfig",
        }
    )

    @classmethod
    def render_config(
        cls,
        payload: ProjectionInput,
        *,
        config_version: int | None = None,
    ) -> str:
        """Render the complete non-secret Hermes ``config.yaml`` document."""
        selected_config_version = (
            payload.config_schema
            if config_version is None
            else config_version
        )
        if (
            isinstance(selected_config_version, bool)
            or selected_config_version not in cls.SUPPORTED_CONFIG_VERSIONS
        ):
            raise ValueError(
                f"unsupported Hermes config schema: {selected_config_version}"
            )
        experts = cls._asset_list(payload.installed_experts, label="installed_experts")
        skills = list(payload.installed_skills)
        mcps = cls._asset_list(payload.installed_mcps, label="installed_mcps")
        skill_policy_version = payload.skill_policy_version
        if skill_policy_version is None:
            skill_policy_version = (
                SKILL_VISIBILITY_POLICY_V2
                if selected_config_version >= cls.TARGET_CONFIG_VERSION
                else SKILL_VISIBILITY_POLICY_V1
            )
        if skill_policy_version not in {
            SKILL_VISIBILITY_POLICY_V1,
            SKILL_VISIBILITY_POLICY_V2,
            SKILL_VISIBILITY_POLICY_V3,
        }:
            raise ValueError(f"unsupported Skill visibility policy: {skill_policy_version}")
        skill_visibility = resolve_versioned_skill_visibility(
            policy_version=skill_policy_version,
            legacy_upstream_names=load_legacy_upstream_skill_names(),
            explicitly_installed_names=explicitly_installed_skill_names(skills),
            user_disabled_names=payload.user_disabled_skills,
        )
        base_url = (payload.model_relay_base_url or "").rstrip("/")
        relay_url = f"{base_url}/{payload.runtime_workspace_id}/v1"
        if payload.bootstrap_config:
            if payload.default_model_id is not None or payload.visible_models:
                raise ValueError("bootstrap projection must not contain model catalog data")
            default_model = "session"
            provider_models = {}
        else:
            default_model, provider_models = cls.resolve_provider_models(
                default_model_id=payload.default_model_id,
                visible_models=payload.visible_models,
            )
        model: dict[str, Any] = {
            "provider": "custom:platform",
            "default": default_model,
            "base_url": relay_url,
            "api_mode": "chat_completions",
            # The Pod-local relay uses loopback, which Hermes otherwise
            # probes as a possible local Ollama server on every Agent build.
            # Zero is an explicit disabled override and remains absent from
            # the custom-provider request body because it is falsy.
            "ollama_num_ctx": 0,
        }
        if default_model != "session":
            model["default_model_entry_id"] = int(default_model)
        mcp_servers: dict[str, Any] = {}
        normalized_slugs: set[str] = set()
        for index, mcp in enumerate(mcps):
            slug = cls._safe_identifier(mcp.get("slug") or mcp.get("name") or mcp.get("mcp_id"))
            if slug in normalized_slugs:
                raise ValueError(f"duplicate normalized MCP slug: {slug}")
            normalized_slugs.add(slug)
            runtime_mcp = cls.runtime_entry(mcp, path=f"installed_mcps[{index}]")
            server: dict[str, Any] = {
                "name": mcp.get("name") or slug,
                "transport": mcp.get("transport") or "stdio",
                "enabled": bool(mcp.get("enabled", True)),
            }
            server.update(runtime_mcp)
            mcp_servers[slug] = server
        skill_external_dirs = [OFFICIAL_HERMES_SKILL_ROOT]
        if skill_policy_version == SKILL_VISIBILITY_POLICY_V3:
            skill_external_dirs.append(BUNDLED_HERMES_SKILL_ROOT)
            skill_external_dirs.append(READONLY_HERMES_SKILL_ROOT)
        document: dict[str, Any] = {
            "_config_version": selected_config_version,
            "agent": {
                "disabled_toolsets": ["cronjob"],
                "system_prompt": KANBAN_ORCHESTRATOR_SYSTEM_PROMPT,
            },
            "toolsets": ["kanban"],
            "kanban": {"max_in_progress_per_profile": 1},
            "browser": {
                "cloud_provider": "local",
                "headed": False,
                "allow_private_urls": False,
                "restrict_evaluate": True,
                "inactivity_timeout": 120,
            },
            # Runtime concurrency is owned by the Supervisor.  Keep Hermes on
            # its single, unmodified Dashboard process and never start the
            # Compute Host path that previously required a source overlay to
            # synchronize per-turn model routes.
            "dashboard": {"turn_isolation": False},
            "model": model,
            "providers": {
                "platform": {
                    "name": "platform",
                    "base_url": relay_url,
                    "api": relay_url,
                    "key_env": "OPENAI_API_KEY",
                    "transport": "chat_completions",
                    "default_model": default_model,
                    "discover_models": False,
                    "models": provider_models,
                }
            },
            "terminal": {"cwd": str(payload.workspace_dir)},
            "workspace_dir": str(payload.workspace_dir),
            "skills": {
                "external_dirs": skill_external_dirs,
                "creation_nudge_interval": 0,
                "disabled": sorted(skill_visibility.effective_disabled),
            },
            "platform": {
                "runtime_workspace_id": payload.runtime_workspace_id,
                **(
                    {"runtime_agent_profile_id": payload.runtime_agent_profile_id}
                    if payload.runtime_agent_profile_id is not None
                    else {}
                ),
                "runtime_id": payload.runtime_id,
                "config_generation": payload.config_generation,
                "skill_visibility": {
                    "policy_version": skill_policy_version,
                    "user_disabled": sorted(
                        set(payload.user_disabled_skills)
                    ),
                    "platform_disabled": sorted(skill_visibility.platform_disabled),
                    **(
                        {"initial_policy_version": skill_policy_version}
                        if payload.skill_policy_version is not None
                        else {}
                    ),
                },
            },
            "plugins": {
                "enabled": list(
                    dict.fromkeys(
                        [
                            "enterprise-knowledge",
                            "personal-knowledge",
                            "kanban-lifecycle",
                            "clarify-lifecycle",
                            "expert-roster",
                            KANBAN_ORCHESTRATOR_GUARD_PLUGIN_ID,
                            DESKTOP_LOCAL_PLUGIN_ID,
                            PLATFORM_MAIN_CONVERSATION_BOUNDARY_PLUGIN_ID,
                        ]
                        + [
                            cls._safe_identifier(
                                expert.get("plugin_id") or expert.get("slug") or expert.get("name")
                            )
                            for expert in experts
                        ]
                    )
                )
            },
            "mcp_servers": mcp_servers,
            "compression": dict(cls.COMPRESSION_CONFIG),
        }
        if selected_config_version >= cls.TARGET_CONFIG_VERSION:
            document.update(
                {
                    # Platform submits corrections only through the durable
                    # session.redirect path. Queue is a defensive fallback for
                    # an unexpected upstream busy prompt, never an ACK source.
                    "display": {"busy_input_mode": "queue"},
                    # Cold session.resume must restore history only. Hermes'
                    # crash auto-continue would create an unowned prompt.
                    "desktop": {"auto_continue": {"enabled": False}},
                    # One Dashboard process owns every root/Profile SessionDB;
                    # the root value is therefore the Binding-wide effective
                    # CJK switch. Profiles cannot independently override it.
                    "sessions": {"cjk_fts": True},
                }
            )
        return cls.dump_document(document)

    @classmethod
    def dump_document(cls, document: dict[str, Any]) -> str:
        return yaml.dump(
            document,
            Dumper=_ConfigDumper,
            sort_keys=False,
            allow_unicode=True,
        )

    @classmethod
    def resolve_provider_models(
        cls,
        *,
        default_model_id: int | None,
        visible_models: tuple[dict[str, Any], ...],
    ) -> tuple[str, dict[str, dict[str, int | bool]]]:
        normalized: dict[int, tuple[int, bool]] = {}
        admin_default_ids: list[int] = []
        for index, model in enumerate(visible_models):
            if not isinstance(model, dict):
                raise ValueError(f"visible_models[{index}] must be an object")
            model_entry_id = model.get("model_entry_id")
            context_length = model.get("context_length")
            supports_vision = model.get("supports_vision")
            is_default = model.get("is_default", False)
            if (
                isinstance(model_entry_id, bool)
                or not isinstance(model_entry_id, int)
                or model_entry_id <= 0
            ):
                raise ValueError(
                    f"visible_models[{index}].model_entry_id must be a positive integer"
                )
            if (
                isinstance(context_length, bool)
                or not isinstance(context_length, int)
                or context_length <= 0
            ):
                raise ValueError(
                    f"visible_models[{index}].context_length must be a positive integer"
                )
            if supports_vision is None:
                supports_vision = cls.supports_vision_from_tags(model.get("tags"))
            if not isinstance(supports_vision, bool):
                raise ValueError(f"visible_models[{index}].supports_vision must be a boolean")
            if not isinstance(is_default, bool):
                raise ValueError(f"visible_models[{index}].is_default must be a boolean")
            if model_entry_id in normalized:
                raise ValueError(f"duplicate visible model entry id: {model_entry_id}")
            normalized[model_entry_id] = (context_length, supports_vision)
            if is_default:
                admin_default_ids.append(model_entry_id)

        if not normalized and default_model_id is not None:
            normalized[int(default_model_id)] = (
                DEFAULT_MODEL_CONTEXT_LENGTH,
                False,
            )
        if len(admin_default_ids) > 1:
            raise ValueError("visible_models must contain at most one admin default")
        ordered = {
            str(model_entry_id): {
                "context_length": normalized[model_entry_id][0],
                "supports_vision": normalized[model_entry_id][1],
            }
            for model_entry_id in sorted(normalized)
        }
        requested_default = str(default_model_id) if default_model_id is not None else None
        default_model = (
            str(admin_default_ids[0])
            if admin_default_ids
            else (
                requested_default
                if requested_default in ordered
                else next(iter(ordered), "session")
            )
        )
        return default_model, ordered

    @staticmethod
    def supports_vision_from_tags(tags: Any) -> bool:
        if not isinstance(tags, (list, tuple, set)):
            return False
        capability_tags = {
            "image",
            "image-input",
            "image_input",
            "multimodal",
            "vision",
            "vl",
        }
        return any(str(tag).strip().lower() in capability_tags for tag in tags)

    @classmethod
    def _asset_list(cls, values: tuple[dict[str, Any], ...], *, label: str) -> list[dict[str, Any]]:
        return [
            cls._sanitize_asset(value, path=f"{label}[{index}]")
            for index, value in enumerate(values)
        ]

    @classmethod
    def _sanitize_asset(cls, value: Any, *, path: str) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, child in value.items():
                normalized_key = str(key).lower()
                is_env_name = path.endswith(".env") and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", str(key)
                )
                if normalized_key in cls.FORBIDDEN_ASSET_KEYS and not is_env_name:
                    raise ValueError(f"forbidden asset key in projection: {path}.{key}")
                sanitized[str(key)] = cls._sanitize_asset(child, path=f"{path}.{key}")
            return sanitized
        if isinstance(value, list | tuple):
            return [
                cls._sanitize_asset(child, path=f"{path}[{index}]")
                for index, child in enumerate(value)
            ]
        return value

    @staticmethod
    def _safe_identifier(value: Any) -> str:
        raw = str(value or "asset").strip().lower()
        slug = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip(".-_")
        return slug or "asset"

    @classmethod
    def validate_mcp_entry(cls, entry: dict[str, Any], *, path: str) -> None:
        if not isinstance(entry, dict):
            raise ValueError(f"MCP entry must be an object: {path}")
        for key in entry:
            normalized = str(key).lower().replace("-", "_")
            if normalized in cls.SECRETISH_FIELDS:
                raise ValueError(f"forbidden MCP config key: {path}.{key}")
            if (
                normalized not in cls.MCP_ALLOWED_FIELDS
                and normalized not in cls.MCP_METADATA_FIELDS
            ):
                raise ValueError(f"unknown MCP config key: {path}.{key}")

        transport = entry.get("transport")
        if transport is not None and str(transport).lower().replace("-", "_") not in {
            "stdio",
            "sse",
            "streamable_http",
            "http",
        }:
            raise ValueError(f"unsupported MCP transport: {path}.transport")
        normalized_transport = (
            str(transport).lower().replace("-", "_") if transport is not None else None
        )
        if (
            normalized_transport in {"http", "sse", "streamable_http"}
            and "url" not in entry
        ):
            raise ValueError(f"MCP {normalized_transport} transport requires url: {path}.url")
        if normalized_transport == "stdio" and "command" not in entry:
            raise ValueError(f"MCP stdio transport requires command: {path}.command")

        for field in ("headers", "env"):
            mapping = entry.get(field)
            if mapping is None:
                continue
            if not isinstance(mapping, dict):
                raise ValueError(f"MCP {field} must be an object: {path}.{field}")
            for key, value in mapping.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"MCP {field} key must be a string: {path}.{field}")
                if not isinstance(value, str) or not cls.ENV_PLACEHOLDER_RE.fullmatch(value):
                    raise ValueError(
                        f"MCP {field} values must be ${'{'}ENV_VAR{'}'} placeholders: "
                        f"{path}.{field}.{key}"
                    )

        args = entry.get("args")
        if args is not None and (
            not isinstance(args, list) or any(not isinstance(value, str) for value in args)
        ):
            raise ValueError(f"MCP args must be a list of strings: {path}.args")
        command = entry.get("command")
        if command is not None and (not isinstance(command, str) or not command.strip()):
            raise ValueError(f"MCP command must be a non-empty string: {path}.command")
        url = entry.get("url")
        if url is not None and (
            not isinstance(url, str) or not re.match(r"^https?://[^\s]+$", url)
        ):
            raise ValueError(f"MCP url must be an http or https URL: {path}.url")
        enabled = entry.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError(f"MCP enabled must be boolean: {path}.enabled")

    @classmethod
    def runtime_entry(cls, entry: dict[str, Any], *, path: str) -> dict[str, Any]:
        cls.validate_mcp_entry(entry, path=path)
        return {
            key: entry[key]
            for key in cls.MCP_ALLOWED_FIELDS
            if key in entry and key not in {"name"}
        }


class HermesWorkspaceProjector:
    FORBIDDEN_ASSET_KEYS = {
        "api_key",
        "apikey",
        "api_key_encrypted",
        "provider_key",
        "provider_api_key",
        "secret",
        "mcp_secret",
        "password",
        "private_key",
        "token",
        "runtime_token",
        "runtime_id",
        "runtime_workspace_id",
        "workspace_id",
        "business_workspace_id",
        "copaw_agent_id",
        "session_id",
        "session_history",
        "history",
        "host_path",
        "kubeconfig",
    }

    def render(self, payload: ProjectionInput) -> dict[str, Any]:
        artifacts = self.render_artifacts(payload)
        home = payload.hermes_home
        self._ensure_dirs(home)
        for relative, content in artifacts.items():
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return json.loads(artifacts["platform/projection.json"])

    def render_artifacts(self, payload: ProjectionInput) -> dict[str, str]:
        """Purely render a signed bundle into relative files.

        This method never creates directories or touches the filesystem. The
        caller can validate and hash the artifact map before publishing it.
        """

        installed_experts = self._asset_list(payload.installed_experts, label="installed_experts")
        installed_skills = self._asset_list(payload.installed_skills, label="installed_skills")
        installed_mcps = self._asset_list(payload.installed_mcps, label="installed_mcps")
        bound_knowledge = self._asset_list(payload.bound_knowledge, label="bound_knowledge")

        config = self._config_yaml(
            payload, installed_experts=installed_experts, installed_mcps=installed_mcps
        )
        soul = self._soul(payload)
        personal_kb = self._personal_kb(payload)
        config_snapshot = self._config_snapshot(
            payload,
            installed_experts=installed_experts,
            installed_skills=installed_skills,
            installed_mcps=installed_mcps,
            bound_knowledge=bound_knowledge,
        )
        skill_artifacts = self._skill_artifacts(installed_skills, payload=payload)
        guard_artifacts = kanban_orchestrator_guard_artifacts()
        desktop_local_artifacts = desktop_local_plugin_artifacts()
        runtime_skills = [self._without_host_storage(skill) for skill in installed_skills]
        projection = self._projection_snapshot(
            payload,
            config=config,
            soul=soul,
            personal_kb=personal_kb,
            installed_experts=json.dumps(installed_experts, sort_keys=True),
            installed_skills=json.dumps(runtime_skills, sort_keys=True),
            installed_mcps=json.dumps(installed_mcps, sort_keys=True),
            bound_knowledge=json.dumps(bound_knowledge, sort_keys=True),
            config_snapshot=json.dumps(config_snapshot, sort_keys=True),
            kanban_orchestrator_guard=json.dumps(
                guard_artifacts,
                sort_keys=True,
            ),
            desktop_local_plugin=json.dumps(
                desktop_local_artifacts,
                sort_keys=True,
            ),
        )

        artifacts: dict[str, str] = {
            "config.yaml": config,
            "SOUL.md": soul,
            "PERSONAL/PERSONAL_KB.md": personal_kb,
            "platform/config.snapshot.json": json.dumps(
                config_snapshot, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "platform/installed-experts.json": json.dumps(
                installed_experts, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "platform/installed-skills.json": json.dumps(
                runtime_skills, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "platform/installed-mcps.json": json.dumps(
                installed_mcps, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "platform/bound-knowledge.json": json.dumps(
                bound_knowledge, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "platform/projection.json": json.dumps(
                projection, ensure_ascii=False, indent=2, sort_keys=True
            ),
        }
        artifacts.update(guard_artifacts)
        artifacts.update(desktop_local_artifacts)
        artifacts.update(self._expert_plugin_artifacts(installed_experts))
        artifacts.update(skill_artifacts)
        return artifacts

    def _ensure_dirs(self, home: Path) -> None:
        for relative in (
            "plugins",
            "skills",
            "sessions",
            "memories",
            "cron",
            "logs",
            "platform",
            "PERSONAL",
        ):
            (home / relative).mkdir(parents=True, exist_ok=True)

    def _config_yaml(
        self,
        payload: ProjectionInput,
        *,
        installed_experts: list[dict[str, Any]],
        installed_mcps: list[dict[str, Any]],
    ) -> str:
        return ProfileConfigRenderer.render_config(
            replace(
                payload,
                installed_experts=tuple(installed_experts),
                installed_mcps=tuple(installed_mcps),
            )
        )

    @staticmethod
    def _soul(_payload: ProjectionInput) -> str:
        return DEFAULT_RUNTIME_SOUL

    def _personal_kb(self, payload: ProjectionInput) -> str:
        return f"# Personal Knowledge\n\nCloud Hermes profile: {payload.name}\n"

    def _config_snapshot(
        self,
        payload: ProjectionInput,
        *,
        installed_experts: list[dict[str, Any]],
        installed_skills: list[dict[str, Any]],
        installed_mcps: list[dict[str, Any]],
        bound_knowledge: list[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "org_id": payload.org_id,
            "user_id": payload.user_id,
            "runtime_workspace_id": payload.runtime_workspace_id,
            "runtime_agent_profile_id": payload.runtime_agent_profile_id,
            "runtime_id": payload.runtime_id,
            "config_generation": payload.config_generation,
            "config_schema": payload.config_schema,
            "model_relay_base_url": payload.model_relay_base_url.rstrip("/"),
            "workspace_dir": str(payload.workspace_dir),
            "hermes_home": str(payload.hermes_home),
            "asset_counts": {
                "installed_experts": len(installed_experts),
                "installed_skills": len(installed_skills),
                "installed_mcps": len(installed_mcps),
                "bound_knowledge": len(bound_knowledge),
            },
        }
        if payload.bootstrap_config:
            snapshot["config_contract"] = "bootstrap"
        else:
            snapshot["default_model_id"] = payload.default_model_id
            snapshot["visible_model_ids"] = sorted(
                int(model["model_entry_id"]) for model in payload.visible_models
            )
        return snapshot

    def _projection_snapshot(
        self,
        payload: ProjectionInput,
        **parts: str,
    ) -> dict[str, Any]:
        digest = hashlib.sha256()
        for value in parts.values():
            digest.update(value.encode("utf-8"))
        return ProjectionSnapshot(
            org_id=payload.org_id,
            user_id=payload.user_id,
            runtime_workspace_id=payload.runtime_workspace_id,
            runtime_id=payload.runtime_id,
            config_generation=payload.config_generation,
            config_hash=f"sha256:{digest.hexdigest()}",
            scopes=(
                ["persona", "skills", "mcp", "knowledge", "experts", "security"]
                if payload.bootstrap_config
                else [
                    "persona",
                    "skills",
                    "mcp",
                    "knowledge",
                    "experts",
                    "model",
                    "security",
                ]
            ),
            # A wall-clock timestamp would make a retry of the same generation
            # produce a different projection hash.  Generation is the durable
            # render identity; the worker records activation timing separately.
            rendered_at=f"generation:{payload.config_generation}",
            requires_reload=True,
        ).to_dict()

    def _asset_list(
        self, values: tuple[dict[str, Any], ...], *, label: str
    ) -> list[dict[str, Any]]:
        return [
            self._sanitize_asset(value, path=f"{label}[{index}]")
            for index, value in enumerate(values)
        ]

    def _sanitize_asset(self, value: Any, *, path: str) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, child in value.items():
                normalized_key = str(key).lower()
                # Environment variable names are identifiers, not credential
                # fields.  Their values are validated later by
                # ProfileConfigRenderer and must remain ${ENV_VAR} references.
                is_env_name = path.endswith(".env") and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", str(key)
                )
                is_skill_manifest_runtime_id = normalized_key == "runtime_id" and re.fullmatch(
                    r"installed_skills\[\d+\]\.manifest", path
                )
                if is_skill_manifest_runtime_id:
                    continue
                if normalized_key in self.FORBIDDEN_ASSET_KEYS and not is_env_name:
                    raise ValueError(f"forbidden asset key in projection: {path}.{key}")
                sanitized[str(key)] = self._sanitize_asset(child, path=f"{path}.{key}")
            return sanitized
        if isinstance(value, list | tuple):
            return [
                self._sanitize_asset(child, path=f"{path}[{index}]")
                for index, child in enumerate(value)
            ]
        return value

    def _expert_plugin_artifacts(self, installed_experts: list[dict[str, Any]]) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for expert in installed_experts:
            plugin_id = self._safe_identifier(
                expert.get("plugin_id") or expert.get("slug") or expert.get("name")
            )
            manifest_lines = [
                f"id: {self._yaml_scalar(plugin_id)}",
                f"name: {self._yaml_scalar(expert.get('name') or plugin_id)}",
                f"version: {self._yaml_scalar(expert.get('version') or '0.0.0')}",
                "kind: hermes.plugin",
                "source: platform",
            ]
            if expert.get("description"):
                manifest_lines.append(f"description: {self._yaml_scalar(expert['description'])}")
            if expert.get("expert_package_id") is not None:
                manifest_lines.append(f"expert_package_id: {expert['expert_package_id']}")
            if expert.get("expert_package_version_id") is not None:
                manifest_lines.append(
                    f"expert_package_version_id: {expert['expert_package_version_id']}"
                )
            if expert.get("skills"):
                manifest_lines.append("skills:")
                manifest_lines.extend(
                    f"  - {self._yaml_scalar(skill)}" for skill in expert["skills"]
                )
            if expert.get("mcp_servers"):
                manifest_lines.append("mcp_servers:")
                manifest_lines.extend(
                    f"  - {self._yaml_scalar(mcp)}" for mcp in expert["mcp_servers"]
                )
            if expert.get("knowledge_bases"):
                manifest_lines.append("knowledge_bases:")
                manifest_lines.extend(
                    f"  - {self._yaml_scalar(kb)}" for kb in expert["knowledge_bases"]
                )
            manifest_lines.append("")
            artifacts[f"plugins/{plugin_id}/plugin.yaml"] = "\n".join(manifest_lines)
        return artifacts

    def _skill_artifacts(
        self,
        installed_skills: list[dict[str, Any]],
        *,
        payload: ProjectionInput,
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        for skill in installed_skills:
            slug = self._safe_identifier(
                skill.get("slug") or skill.get("name") or skill.get("skill_id")
            )
            category = self._skill_category(skill)
            skill_root = f"skills/{category}/{slug}"
            artifacts[f"{skill_root}/skill.snapshot.json"] = json.dumps(
                self._without_host_storage(skill), ensure_ascii=False, indent=2, sort_keys=True
            )
            if skill.get("source") not in {"public_asset", "personal_asset"}:
                continue
            manifest = skill.get("manifest") if isinstance(skill.get("manifest"), dict) else {}
            storage_uri = skill.get("storage_uri")
            files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
            if not isinstance(storage_uri, str) or not storage_uri:
                raise ValueError(f"Skill storage_uri is required: {slug}")
            root = Path(storage_uri)
            allowed_root = self._controlled_skill_root(skill, payload)
            try:
                resolved_root = root.resolve(strict=True)
                controlled_root = allowed_root.resolve(strict=True)
                resolved_root.relative_to(controlled_root)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"Skill storage_uri is outside the controlled asset root: {slug}"
                ) from exc
            for raw_relative in files:
                if not isinstance(raw_relative, str):
                    raise ValueError(f"Skill file path must be a string: {slug}")
                relative = raw_relative.replace("\\", "/")
                path = Path(relative)
                if (
                    not relative
                    or relative.startswith("/")
                    or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.split("/"))
                ):
                    raise ValueError(f"Skill file path is unsafe: {relative}")
                source = root / relative
                if not source.is_file():
                    raise ValueError(f"Skill file is missing: {relative}")
                try:
                    source.resolve(strict=True).relative_to(controlled_root)
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        f"Skill file escapes the controlled asset root: {relative}"
                    ) from exc
                artifacts[f"{skill_root}/{relative}"] = source.read_text(encoding="utf-8")
        return artifacts

    def _skill_category(self, skill: dict[str, Any]) -> str:
        if skill.get("source") == "personal_asset":
            return "personal"
        category = self._safe_identifier(skill.get("category_key") or "other")
        if category in {"personal", "uncategorized"}:
            return "other"
        return category

    def _controlled_skill_root(self, skill: dict[str, Any], payload: ProjectionInput) -> Path:
        configured_root = Path(settings.HERMES_PUBLIC_ASSET_ROOT)
        source = skill.get("source")
        if source == "public_asset":
            return configured_root
        if source == "personal_asset":
            return configured_root / "personal-skills" / str(payload.org_id) / str(payload.user_id)
        raise ValueError(f"unsupported skill source for file projection: {source}")

    @classmethod
    def _without_host_storage(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._without_host_storage(child)
                for key, child in value.items()
                if key != "storage_uri"
            }
        if isinstance(value, list):
            return [cls._without_host_storage(child) for child in value]
        return value

    def _safe_identifier(self, value: Any) -> str:
        raw = str(value or "asset").strip().lower()
        slug = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip(".-_")
        return slug or "asset"

    def _yaml_scalar(self, value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, int | float):
            return str(value)
        text = str(value)
        if not text:
            return '""'
        if re.search(r"[\n\r:#\[\]\{\},&*!|>'\"%@`]", text) or text.strip() != text:
            return json.dumps(text, ensure_ascii=False)
        return text
