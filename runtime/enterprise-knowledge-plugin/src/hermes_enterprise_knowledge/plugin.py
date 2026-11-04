from __future__ import annotations

from .context import build_context
from .tools import TOOL_SCHEMAS, handlers


def register(ctx):
    ctx.register_hook("pre_llm_call", build_context)
    for name, handler in handlers().items():
        schema = {"name": name, **TOOL_SCHEMAS[name]}
        ctx.register_tool(
            name=name,
            toolset="enterprise_knowledge",
            schema=schema,
            handler=handler,
            description=TOOL_SCHEMAS[name]["description"],
        )
