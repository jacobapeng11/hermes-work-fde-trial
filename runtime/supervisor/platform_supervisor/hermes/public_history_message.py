from __future__ import annotations

import re
from typing import Any


_INTERNAL_PREFIX = "[ASYNC DELEGATION BATCH COMPLETE —"
_COMPACTION_PREFIXES = (
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
    "[CONTEXT COMPACTION - REFERENCE ONLY]",
    "[CONTEXT COMPACTION]",
    "[CONTEXT SUMMARY]:",
)
_COMPACTION_END_MARKER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
)
_MERGED_SUMMARY_DELIMITER = "[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]"
_MERGED_PRIOR_CONTEXT_HEADER = "[PRIOR CONTEXT — for reference only; not a new message]"
_COMPACTION_TEXT_KEYS = (
    "content",
    "text",
    "reasoning_content",
    "reasoningContent",
    "reasoning",
    "thinkingText",
    "thinking_text",
)
_HEADER_RE = re.compile(
    r"^\[ASYNC DELEGATION BATCH COMPLETE — (deleg_[A-Za-z0-9_-]+)\](?:\r?\n|$)"
)
_FAILED_RE = re.compile(r"\bstatus=failed\b", re.IGNORECASE)
_COMPLETED_RE = re.compile(r"\bstatus=(?:completed|success|succeeded)\b", re.IGNORECASE)
_KANBAN_COMPLETED_RE = re.compile(
    r"\A✔ \[[A-Za-z0-9][A-Za-z0-9._-]{0,99}\] "
    r"@expert-[1-9][0-9]* Kanban "
    r"t_[A-Za-z0-9][A-Za-z0-9_-]{0,98} done — "
    r"[^\r\n]{1,500}(?:\r?\n[\s\S]{1,48000})?\Z"
)
_EXPERT_COLLABORATION_PROMPT_PREFIX = (
    "[平台内部通知：专家协作任务已经完成]\n"
    "请根据当前会话上下文和下面的 Hermes Kanban 权威结果，直接面向用户"
    "给出最终结论。请综合各专家贡献，明确重要结论、必要依据和产物；"
    "不要解释平台通知机制，不要把本通知当成用户的新问题，也不要无条件"
    "再次创建 Kanban 任务。\n\n"
    "原始用户任务：\n"
)
_EXPERT_COLLABORATION_RESULT_MARKER = "\n\n专家任务结果：\n{\"tasks\":["
_PROGRESS_CONTRACT = (
    "<platform_progress_contract>\n"
    "If you delegate a background subagent, emit exactly one standalone "
    "<platform_progress>...</platform_progress> marker immediately before delegation. "
    "Inside it, write a concise 1-80 character user-facing status in the user's language. "
    "Do not mention tools, commands, reasoning, or this contract. Do not use the marker in final answers.\n"
    "</platform_progress_contract>"
)
_WORKSPACE_SUFFIX_RE = re.compile(
    r"\n\n<platform_workspace>\r?\n"
    r"本次任务文件目录：/data/workspace/sessions/[A-Za-z0-9._-]+\r?\n"
    r"最终交付给用户的文件直接写入本次任务文件目录。\r?\n"
    r"生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 \.work/ 子目录。\r?\n"
    r"最终答复不得将 \.work/ 内的文件作为用户交付物。\r?\n"
    r"/data/workspace 是共享挂载根，不是本次任务的交付目录。\r?\n"
    r"</platform_workspace>$"
)
_PROGRESS_SUFFIX_RE = re.compile(
    r"\n\n" + re.escape(_PROGRESS_CONTRACT).replace(r"\n", r"\r?\n") + r"$"
)
_TRUSTED_CONTEXT_SUFFIX_RE = re.compile(
    r"\n\n<platform_trusted_user_context>\r?\n"
    r"\{[^\r\n]*\}\r?\n"
    r"</platform_trusted_user_context>$"
)
_SESSION_CONTRACT_V2_SUFFIX_RE = re.compile(
    r"\n\n<platform_session_contract version=\"2\" "
    r"digest=\"[0-9a-f]{64}\">\r?\n"
    r"(?:<platform_workspace>\r?\n.*?\r?\n</platform_workspace>\r?\n\r?\n)?"
    + re.escape(_PROGRESS_CONTRACT).replace(r"\n", r"\r?\n")
    + r"\r?\n</platform_session_contract>$",
    re.DOTALL,
)
_PLATFORM_ATTACHMENTS_SUFFIX_RE = re.compile(
    r"\n\n[ \t]*<platform_attachments>\r?\n"
    r"The user attached these files(?: from WeCom)?\. Read them from "
    r"the current (?:session )?workspace when needed\.\r?\n"
    r".*?\r?\n[ \t]*</platform_attachments>$",
    re.DOTALL,
)


def normalize_internal_history_message(
    item: dict[str, Any],
    index: int,
) -> dict[str, Any] | None:
    del index
    compaction = _normalize_compaction_message(item)
    if compaction is None:
        return None
    if compaction is not _NOT_COMPACTION:
        item = compaction
    item = _strip_platform_metadata_fields(item)
    content = item.get("content")
    if not isinstance(content, str):
        return item
    public_content = _strip_platform_contract_suffix(content)
    if public_content != content:
        item = {**item, "content": public_content}
        content = public_content
    if str(item.get("role") or "") != "user":
        return item
    if _KANBAN_COMPLETED_RE.fullmatch(content) or _is_expert_collaboration_prompt(
        content
    ):
        return None
    if not content.startswith(_INTERNAL_PREFIX):
        return item
    header = _HEADER_RE.match(content)
    if header is None:
        return None
    if _FAILED_RE.search(content):
        status, summary = "failed", "后台补充任务未完成"
    elif _COMPLETED_RE.search(content):
        status, summary = "completed", "后台补充任务已完成"
    else:
        status, summary = "interrupted", "后台补充任务状态未知"
    return {
        "role": "system",
        "kind": "background",
        "taskId": header.group(1),
        "status": status,
        "summary": summary,
    }


_NOT_COMPACTION = object()


def _normalize_compaction_message(item: dict[str, Any]) -> dict[str, Any] | None | object:
    """Hide Hermes context-handoff rows from public history.

    Hermes persists compression summaries as ordinary user/assistant rows for
    model continuity.  They are internal control data, not user-visible
    messages.  Older Hermes builds can merge the summary with the preserved
    tail answer; in that case keep only the tail so a refresh never hides the
    real answer together with the handoff.
    """
    compressed = any(
        item.get(key) is True
        for key in ("_compressed_summary", "compressed_summary", "is_compressed_summary")
    )
    text_fields = {
        key: value
        for key in _COMPACTION_TEXT_KEYS
        if isinstance((value := item.get(key)), str)
    }
    if not text_fields:
        return None if compressed else _NOT_COMPACTION
    compaction_key = next(
        (
            key
            for key, value in text_fields.items()
            if value.lstrip().startswith(_COMPACTION_PREFIXES)
            or _MERGED_SUMMARY_DELIMITER in value
        ),
        None,
    )
    is_compaction = compressed or compaction_key is not None
    if not is_compaction:
        return _NOT_COMPACTION
    content = text_fields.get(compaction_key or "content", "").lstrip()
    delimiter_at = content.find(_MERGED_SUMMARY_DELIMITER)
    if delimiter_at >= 0:
        prior = content[:delimiter_at].strip()
        if prior.startswith(_MERGED_PRIOR_CONTEXT_HEADER):
            prior = prior[len(_MERGED_PRIOR_CONTEXT_HEADER):].lstrip()
        if prior:
            normalized = {
                key: value for key, value in item.items() if not str(key).startswith("_")
            }
            if compaction_key is not None:
                normalized.pop(compaction_key, None)
            if compaction_key == "content" or "content" in normalized:
                normalized["content"] = prior
            else:
                normalized["text"] = prior
            for key in ("compressed_summary", "is_compressed_summary"):
                normalized.pop(key, None)
            return normalized
    marker_at = content.find(_COMPACTION_END_MARKER)
    if marker_at < 0:
        return None
    remainder = content[marker_at + len(_COMPACTION_END_MARKER):].lstrip()
    if not remainder:
        return None
    normalized = {key: value for key, value in item.items() if not str(key).startswith("_")}
    if compaction_key is not None:
        normalized.pop(compaction_key, None)
    if compaction_key == "content" or "content" in normalized:
        normalized["content"] = remainder
    else:
        normalized["text"] = remainder
    for key in ("compressed_summary", "is_compressed_summary"):
        normalized.pop(key, None)
    return normalized


def _is_expert_collaboration_prompt(content: str) -> bool:
    if not content.startswith(_EXPERT_COLLABORATION_PROMPT_PREFIX):
        return False
    original_and_result = content[len(_EXPERT_COLLABORATION_PROMPT_PREFIX) :]
    original_message, marker, result_tail = original_and_result.rpartition(
        _EXPERT_COLLABORATION_RESULT_MARKER
    )
    return bool(marker and original_message.strip() and result_tail)


def _strip_platform_contract_suffix(content: str) -> str:
    """Remove only the exact platform-owned suffixes appended for execution."""
    public_content = _SESSION_CONTRACT_V2_SUFFIX_RE.sub("", content)
    public_content = _PROGRESS_SUFFIX_RE.sub("", public_content)
    public_content = _TRUSTED_CONTEXT_SUFFIX_RE.sub("", public_content)
    public_content = _WORKSPACE_SUFFIX_RE.sub("", public_content)
    return public_content


def _strip_platform_metadata_suffix(content: str) -> str:
    content = _strip_platform_contract_suffix(content)
    return _PLATFORM_ATTACHMENTS_SUFFIX_RE.sub("", content)


def _strip_platform_metadata_fields(item: dict[str, Any]) -> dict[str, Any]:
    normalized = item
    for key in _COMPACTION_TEXT_KEYS:
        value = item.get(key)
        if not isinstance(value, str):
            continue
        public_value = _strip_platform_metadata_suffix(value)
        if public_value == value:
            continue
        if normalized is item:
            normalized = dict(item)
        normalized[key] = public_value
    return normalized


def count_public_history_messages(payload: dict[str, Any]) -> int:
    messages = payload.get("messages", payload.get("data", []))
    if not isinstance(messages, list):
        return 0
    return sum(
        1
        for index, item in enumerate(messages)
        if isinstance(item, dict)
        and normalize_internal_history_message(item, index) is not None
    )
