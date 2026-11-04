import json

from hermes_expert_roster import platform_client
from hermes_expert_roster.platform_client import PlatformClient
from hermes_expert_roster.plugin import register
from hermes_expert_roster.tools import handlers


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


class FakeClient:
    def list_experts(self):
        return {
            "experts": [
                {
                    "display_name": "市场洞察专家",
                    "assignee": "expert-19",
                    "description": "擅长市场洞察",
                    "capabilities": {
                        "skills": ["market-research"],
                        "mcps": [],
                        "knowledge_bases": [],
                        "plugins": [],
                    },
                }
            ]
        }


def test_register_adds_read_only_roster_tool() -> None:
    context = FakeContext()
    register(context)

    assert set(context.tools) == {"expert_list"}
    assert context.tools["expert_list"]["toolset"] == "expert_roster"
    assert "kanban_create" in context.tools["expert_list"]["description"]
    assert "delegate_task" in context.tools["expert_list"]["description"]
    assert "exact assignee" in context.tools["expert_list"]["description"]
    assert "omit the skills parameter entirely" in context.tools["expert_list"]["description"]
    assert "never treat an MCP name as a Skill" in context.tools["expert_list"]["description"]


def test_tool_returns_backend_roster_without_parameters() -> None:
    result = json.loads(handlers(FakeClient())["expert_list"]({}))

    assert result["experts"][0]["assignee"] == "expert-19"


def test_explicit_default_profile_identity_is_sent(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"experts": []}'

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    result = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=10,
        token="secret",
        runtime_agent_profile_id=30,
    ).list_experts()

    assert result == {"experts": []}
    assert captured["url"].endswith(
        "/runtime-experts/10/roster?runtime_agent_profile_id=30"
    )
