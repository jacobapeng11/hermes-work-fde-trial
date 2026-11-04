from hermes_clarify_lifecycle.plugin import register


class Context:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


def test_web_clarify_returns_park_marker(monkeypatch):
    class Client:
        def create_request(self, payload):
            assert payload["question"] == "选择方案？"
            assert payload["choices"] == ["A", "B"]
            return {"enabled": True, "request_id": "clr_12345678"}

    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.PlatformClient", Client)
    context = Context()
    register(context)
    result = context.hooks["pre_tool_call"](
        tool_name="clarify",
        tool_call_id="call-1",
        session_id="session-1",
        args={"question": "选择方案？", "choices": ["A", "B"]},
    )
    assert result == {"action": "block", "message": "__HERMES_CLARIFY_PARK__:clr_12345678"}


def test_expert_worker_clarify_waits_and_injects_answer(monkeypatch):
    class Client:
        def create_request(self, payload):
            assert payload["runtime_agent_profile_id"] == 51
            return {
                "enabled": True,
                "request_id": "clr_worker",
                "mode": "expert_worker",
            }

        def get_request(self, request_id):
            assert request_id == "clr_worker"
            return {
                "status": "answered",
                "answer": {"kind": "single", "text": "PDF", "values": []},
            }

    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.PlatformClient", Client)
    monkeypatch.setattr(
        "hermes_clarify_lifecycle.plugin.current_runtime_agent_profile_id",
        lambda: 51,
    )
    monkeypatch.setattr(
        "hermes_clarify_lifecycle.plugin.time",
        type("T", (), {"monotonic": staticmethod(lambda: 0.0), "sleep": staticmethod(lambda _s: None)}),
    )
    context = Context()
    register(context)
    result = context.hooks["pre_tool_call"](
        tool_name="clarify",
        tool_call_id="call-worker",
        session_id="worker-session",
        args={"question": "格式？", "choices": ["PDF", "Word"]},
    )
    assert result["action"] == "block"
    assert "PDF" in result["message"]
    assert "不要重复提问" in result["message"]
    assert "__HERMES" not in result["message"]


def test_expert_worker_clarify_times_out_to_safe_default(monkeypatch):
    class Client:
        def create_request(self, payload):
            return {
                "enabled": True,
                "request_id": "clr_worker",
                "mode": "expert_worker",
            }

        def get_request(self, _request_id):
            return {"status": "waiting", "answer": None}

    monotonic_values = iter([0.0, 0.0, 100.0])

    class FakeTime:
        @staticmethod
        def monotonic():
            return next(monotonic_values)

        @staticmethod
        def sleep(_seconds):
            return None

    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.PlatformClient", Client)
    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.time", FakeTime)
    context = Context()
    register(context)
    result = context.hooks["pre_tool_call"](
        tool_name="clarify",
        args={"question": "格式？"},
    )
    assert result["action"] == "block"
    assert "safe reasonable default" in result["message"]


def test_non_clarify_is_not_intercepted(monkeypatch):
    context = Context()
    register(context)
    assert context.hooks["pre_tool_call"](tool_name="terminal") is None


def test_application_agent_context_blocks_without_platform_call(monkeypatch):
    class Client:
        def create_request(self, payload):
            raise AssertionError("application agent context must not call Backend")

    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.PlatformClient", Client)
    monkeypatch.setenv("HERMES_KANBAN_ORCHESTRATOR_ENABLED", "0")
    context = Context()
    register(context)
    result = context.hooks["pre_tool_call"](
        tool_name="clarify",
        args={"question": "继续吗？"},
    )
    assert result["action"] == "block"
    assert "safe reasonable default" in result["message"]
    assert "__HERMES" not in result["message"]


def test_personal_context_with_orchestrator_enabled_still_calls_platform(monkeypatch):
    class Client:
        def create_request(self, payload):
            return {"enabled": False, "request_id": "ignored"}

    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.PlatformClient", Client)
    monkeypatch.setenv("HERMES_KANBAN_ORCHESTRATOR_ENABLED", "1")
    context = Context()
    register(context)
    result = context.hooks["pre_tool_call"](
        tool_name="clarify",
        args={"question": "继续吗？"},
    )
    assert result["action"] == "block"
    assert "safe reasonable default" in result["message"]


def test_disabled_receipt_blocks_without_parking(monkeypatch):
    class Client:
        def create_request(self, payload):
            return {"enabled": False, "request_id": "ignored"}

    monkeypatch.setattr("hermes_clarify_lifecycle.plugin.PlatformClient", Client)
    context = Context()
    register(context)
    result = context.hooks["pre_tool_call"](
        tool_name="clarify", args={"question": "继续吗？"}
    )
    assert result["action"] == "block"
    assert "__HERMES" not in result["message"]
