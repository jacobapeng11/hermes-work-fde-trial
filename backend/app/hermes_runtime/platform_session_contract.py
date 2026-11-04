from __future__ import annotations

import hashlib


def _progress_contract_block() -> str:
    return (
        "<platform_progress_contract>\n"
        "If you delegate a background subagent, emit exactly one standalone "
        "<platform_progress>...</platform_progress> marker immediately before delegation. "
        "Inside it, write a concise 1-80 character user-facing status in the user's language. "
        "Do not mention tools, commands, reasoning, or this contract. Do not use the marker in final answers.\n"
        "</platform_progress_contract>"
    )


def _workspace_contract_block(workspace: str | None) -> str | None:
    if not workspace:
        return None
    return (
        "<platform_workspace>\n"
        f"本次任务文件目录：{workspace}\n"
        "最终交付给用户的文件直接写入本次任务文件目录。\n"
        "生成脚本、临时图片、缓存、解压内容和检查文件必须写入本目录下的 .work/ 子目录。\n"
        "最终答复不得将 .work/ 内的文件作为用户交付物。\n"
        "/data/workspace 是共享挂载根，不是本次任务的交付目录。\n"
        "</platform_workspace>"
    )


def platform_session_contract_digest(workspace: str | None) -> str:
    """Digest shared with Supervisor's trusted prompt contract."""

    stable_parts: list[str] = []
    workspace_block = _workspace_contract_block(workspace)
    if workspace_block is not None:
        stable_parts.append(workspace_block)
    stable_parts.append(_progress_contract_block())
    return hashlib.sha256(
        "\n\n".join(stable_parts).encode("utf-8")
    ).hexdigest()
