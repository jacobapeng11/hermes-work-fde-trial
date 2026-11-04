from __future__ import annotations

from .tools import TOOL_SCHEMAS, handlers


def register(ctx) -> None:
    for name, handler in handlers().items():
        schema = {"name": name, **TOOL_SCHEMAS[name]}
        ctx.register_tool(
            name=name,
            toolset="expert_roster",
            schema=schema,
            handler=handler,
            description=TOOL_SCHEMAS[name]["description"],
        )
