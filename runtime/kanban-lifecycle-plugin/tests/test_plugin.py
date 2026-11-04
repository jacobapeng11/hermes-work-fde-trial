import json

from hermes_kanban_lifecycle import platform_client
from hermes_kanban_lifecycle.platform_client import PlatformClient
from hermes_kanban_lifecycle.plugin import register


class FakeContext:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_registers_tool_and_native_lifecycle_hooks(monkeypatch) -> None:
    emitted = []

    class Client:
        def emit(self, payload):
            emitted.append(payload)
            return {"accepted": True}

    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.PlatformClient", Client)
    context = FakeContext()
    register(context)

    assert set(context.hooks) == {
        "pre_tool_call",
        "post_tool_call",
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
    }
    assert context.hooks["pre_tool_call"](
        tool_name="kanban_create", tool_call_id="call-1", task_id=None
    ) is None
    context.hooks["post_tool_call"](
        tool_name="kanban_create", tool_call_id="call-1", status="ok"
    )
    context.hooks["kanban_task_completed"](
        task_id="t-1",
        run_id=2,
        profile_name="expert-7",
        assignee="expert-7",
        board="default",
    )

    assert [item["event_name"] for item in emitted] == [
        "pre_tool_call",
        "post_tool_call",
        "kanban_task_completed",
    ]
    assert emitted[-1]["event_id"] == "kanban_task_completed:t-1:2:expert-7"
    assert emitted[-1]["assignee"] == "expert-7"


def test_successful_create_post_extracts_native_task_and_chat_identity(
    monkeypatch,
) -> None:
    emitted = []

    class Client:
        def emit(self, payload):
            emitted.append(payload)
            return {"accepted": True}

    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.PlatformClient", Client)
    context = FakeContext()
    register(context)

    context.hooks["post_tool_call"](
        tool_name="kanban_create",
        tool_call_id="call-9",
        turn_id="turn-2",
        session_id="rs-10",
        tool_input={"assignee": "expert-7", "board": "default"},
        result='{"ok": true, "task_id": "t_native"}',
        status="ok",
    )

    assert emitted[-1] == {
        "event_id": "post_tool_call:call-9",
        "event_name": "post_tool_call",
        "tool_name": "kanban_create",
        "task_id": "t_native",
        "board": "default",
        "runtime_session_id": "rs-10",
        "tool_call_id": "call-9",
        "turn_id": "turn-2",
        "assignee": "expert-7",
        "outcome": "ok",
    }


def test_pre_create_fails_closed_but_post_event_fails_open(monkeypatch) -> None:
    class Client:
        def emit(self, _payload):
            from hermes_kanban_lifecycle.platform_client import PlatformClientError

            raise PlatformClientError("offline")

    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.PlatformClient", Client)
    context = FakeContext()
    register(context)

    assert context.hooks["pre_tool_call"](
        tool_name="kanban_create", tool_call_id="call-1"
    )["action"] == "block"
    assert context.hooks["post_tool_call"](
        tool_name="kanban_complete", tool_call_id="call-2", status="ok"
    ) is None


def test_claim_retries_until_worker_route_is_ready(monkeypatch) -> None:
    responses = [
        {"accepted": True, "worker_route_ready": False},
        {"accepted": True, "worker_route_ready": True},
    ]
    emitted = []

    class Client:
        def emit(self, payload):
            emitted.append(payload)
            return responses.pop(0)

    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.PlatformClient", Client)
    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.time.sleep", lambda _s: None)
    context = FakeContext()
    register(context)

    context.hooks["kanban_task_claimed"](
        task_id="t-route",
        run_id=9,
        profile_name="default",
        assignee="expert-75",
        board="default",
    )

    assert len(emitted) == 2
    assert emitted[0]["event_id"] == emitted[1]["event_id"]


def test_pre_create_blocks_task_level_skills_for_installed_experts(
    monkeypatch,
) -> None:
    emitted = []

    class Client:
        def emit(self, payload):
            emitted.append(payload)
            return {"accepted": True}

    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.PlatformClient", Client)
    context = FakeContext()
    register(context)

    blocked = context.hooks["pre_tool_call"](
        tool_name="kanban_create",
        tool_call_id="call-expert-skills",
        tool_input={
            "assignee": "expert-21",
            "skills": ["web-search-mcp"],
        },
    )

    assert blocked["action"] == "block"
    assert "omit the skills parameter entirely" in blocked["message"]
    assert emitted == []


def test_pre_create_allows_expert_without_skills_and_ordinary_assignees(
    monkeypatch,
) -> None:
    emitted = []

    class Client:
        def emit(self, payload):
            emitted.append(payload)
            return {"accepted": True}

    monkeypatch.setattr("hermes_kanban_lifecycle.plugin.PlatformClient", Client)
    context = FakeContext()
    register(context)

    assert context.hooks["pre_tool_call"](
        tool_name="kanban_create",
        tool_call_id="call-expert",
        args={"assignee": "expert-21"},
    ) is None
    assert context.hooks["pre_tool_call"](
        tool_name="kanban_create",
        tool_call_id="call-expert-empty",
        tool_input={"assignee": "expert-21", "skills": []},
    ) is None
    assert context.hooks["pre_tool_call"](
        tool_name="kanban_create",
        tool_call_id="call-ordinary",
        tool_input={"assignee": "researcher", "skills": ["web-search"]},
    ) is None

    assert [item["event_id"] for item in emitted] == [
        "pre_tool_call:call-expert",
        "pre_tool_call:call-expert-empty",
        "pre_tool_call:call-ordinary",
    ]


def test_platform_client_sends_only_control_metadata(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"accepted": true, "lease_state": "temporary"}'

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(platform_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(platform_client, "current_runtime_agent_profile_id", lambda: 7)
    result = PlatformClient(
        base_url="http://platform.invalid/api/v1",
        workspace_id=10,
        token="secret-token",
    ).emit(
        {
            "event_id": "pre_tool_call:1",
            "event_name": "pre_tool_call",
            "tool_name": "kanban_create",
        }
    )

    assert result["accepted"] is True
    assert captured["url"].endswith("/runtime-kanban/10/events")
    assert captured["body"]["runtime_agent_profile_id"] == 7
    assert set(captured["body"]) == {
        "event_id",
        "event_name",
        "tool_name",
        "runtime_agent_profile_id",
    }
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
