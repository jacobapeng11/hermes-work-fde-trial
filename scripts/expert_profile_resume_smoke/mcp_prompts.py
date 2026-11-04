"""Deterministic model prompts for dual-MCP visibility and invocation probes."""

from __future__ import annotations

from .mcp_contracts import McpIdentity


class McpPrompts:
    @staticmethod
    def positive(
        identity: McpIdentity,
        *,
        negative: McpIdentity | None = None,
    ) -> str:
        tail = ""
        if negative is not None:
            tail = (
                f"最后再次调用 tool_search 搜索 {negative.marker_tool}，"
                "只报告是否存在，不得调用它。"
            )
        return (
            f"确定性 MCP 隔离验收：必须先调用 tool_search 搜索 "
            f"{identity.marker_tool}，再调用返回的完整 MCP 工具名；"
            f"原样报告 marker 和 boot_id。{tail}"
            "不要使用 terminal、浏览器或网络工具，不要猜测结果。"
        )

    @staticmethod
    def negative(*identities: McpIdentity) -> str:
        searches = "、".join(item.marker_tool for item in identities)
        return (
            f"确定性 MCP 隔离验收：分别调用 tool_search 搜索 {searches}；"
            "只报告每个工具是否存在，不得调用任何 marker 工具，"
            "不要使用 terminal、浏览器或网络工具。"
        )

    @staticmethod
    def hold(identity: McpIdentity) -> str:
        assert identity.hold_tool is not None
        return (
            f"确定性运行中重绑定验收：先调用 tool_search 搜索 {identity.hold_tool}，"
            "再调用返回的完整 hold 工具；hold 返回后调用 tool_search 搜索 "
            f"{identity.marker_tool} 并调用返回的完整 marker 工具，"
            "原样报告两次结果。不要使用其他工具，不要猜测。"
        )
