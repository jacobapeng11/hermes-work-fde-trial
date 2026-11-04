from __future__ import annotations

from functools import partial

from .context import build_context
from .platform_client import PlatformClient
from .tools import TOOL_SCHEMAS, handlers


def register(ctx):
    client = PlatformClient()
    ctx.register_hook("pre_llm_call", partial(build_context, client))
    for name, handler in handlers(client).items():
        schema = {"name": name, **TOOL_SCHEMAS[name]}
        ctx.register_tool(
            name=name,
            toolset="personal_knowledge",
            schema=schema,
            handler=handler,
            description=TOOL_SCHEMAS[name]["description"],
        )
