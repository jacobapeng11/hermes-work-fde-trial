from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "smoke-application-agent-openapi.py"
)
SPEC = importlib.util.spec_from_file_location(
    "application_agent_openapi_smoke",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | bytes, status: int = 200) -> None:
        self.status = status
        self.raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.raw


class FakeOpenUrl:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Any, float]] = []

    def __call__(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def config(**overrides):
    values = {
        "base_url": "https://hermes.example.test",
        "client_id": "client-test1",
        "client_secret": "secret-value",
        "agent_token": None,
        "authorization_code": "one-time-code",
        "agent_slug": "test1",
        "input_text": "个人知识库里面有啥内容",
        "conversation_key": None,
        "request_id": "request-1",
        "timeout_seconds": 180.0,
    }
    values.update(overrides)
    return smoke.SmokeConfig(**values)


def completed_response(**overrides) -> dict[str, Any]:
    result = {
        "response_id": "response-1",
        "request_id": "request-1",
        "application_agent": "test1",
        "conversation_key": None,
        "runtime_session_id": "application-1-session-1",
        "identity": {"type": "user", "key": "user:7"},
        "output_text": "个人知识库中有项目说明和测试文档。",
        "status": "completed",
    }
    result.update(overrides)
    return result


def request_headers(request) -> dict[str, str]:
    return {key.lower(): value for key, value in request.header_items()}


def request_json(request) -> dict[str, Any]:
    return json.loads(request.data.decode("utf-8"))


def test_exchanges_authorization_code_then_posts_delegated_user_request() -> None:
    opener = FakeOpenUrl(
        FakeResponse(
            {
                "access_token": "delegated-agent-token",
                "token_type": "Agent",
                "expires_in": 900,
                "identity": {
                    "type": "user",
                    "user_code": "61715",
                },
            }
        ),
        FakeResponse(completed_response()),
    )

    result = smoke.run_smoke(config(), open_url=opener)

    assert result["success"] is True
    assert result["response_mode"] == "json"
    assert result["identity"] == {"type": "user", "key": "user:7"}
    assert len(opener.requests) == 2

    token_request, token_timeout = opener.requests[0]
    assert token_request.full_url.endswith(
        "/openapi/v1/application-agents/token"
    )
    token_headers = request_headers(token_request)
    encoded_basic = token_headers["authorization"].removeprefix("Basic ")
    assert base64.b64decode(encoded_basic).decode("utf-8") == (
        "client-test1:secret-value"
    )
    assert "x-hermes-agent-token" not in token_headers
    assert request_json(token_request) == {
        "grant_type": "authorization_code",
        "code": "one-time-code",
    }
    assert token_timeout == 180.0

    response_request, response_timeout = opener.requests[1]
    assert response_request.full_url.endswith(
        "/openapi/v1/application-agents/test1/responses"
    )
    response_headers = request_headers(response_request)
    assert response_headers["x-hermes-agent-token"] == "delegated-agent-token"
    assert request_json(response_request) == {
        "input": "个人知识库里面有啥内容",
        "request_id": "request-1",
    }
    assert response_timeout == 180.0


def test_uses_existing_token_and_preserves_long_term_conversation_key() -> None:
    opener = FakeOpenUrl(
        FakeResponse(
            completed_response(conversation_key="ticket-10086")
        )
    )

    result = smoke.run_smoke(
        config(
            agent_token="existing-agent-token",
            authorization_code=None,
            conversation_key="ticket-10086",
        ),
        open_url=opener,
    )

    assert result["conversation_key"] == "ticket-10086"
    assert len(opener.requests) == 1
    request, _timeout = opener.requests[0]
    assert request_headers(request)["x-hermes-agent-token"] == (
        "existing-agent-token"
    )
    assert request_json(request)["conversation_key"] == "ticket-10086"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (
            completed_response(
                identity={"type": "service", "key": "service:client-test1"}
            ),
            "delegated_user_identity_missing",
        ),
        (
            completed_response(
                output_text='{"error":"runtime knowledge platform request failed"}'
            ),
            "runtime_knowledge_platform_request_failed",
        ),
    ],
)
def test_rejects_non_user_identity_and_knowledge_platform_error(
    response: dict[str, Any],
    expected_code: str,
) -> None:
    opener = FakeOpenUrl(FakeResponse(response))

    with pytest.raises(smoke.SmokeError) as captured:
        smoke.run_smoke(
            config(
                agent_token="existing-agent-token",
                authorization_code=None,
            ),
            open_url=opener,
        )

    assert captured.value.stage == "response_validation"
    assert captured.value.code == expected_code


def test_rejects_invalid_json_response() -> None:
    opener = FakeOpenUrl(FakeResponse(b"not-json"))

    with pytest.raises(smoke.SmokeError) as captured:
        smoke.run_smoke(
            config(
                agent_token="existing-agent-token",
                authorization_code=None,
            ),
            open_url=opener,
        )

    assert captured.value.stage == "agent_response"
    assert captured.value.code == "invalid_json_response"


def test_main_returns_nonzero_and_redacts_credentials_from_http_error() -> None:
    client_secret = "must-not-leak-secret"
    agent_token = "must-not-leak-token"
    error_body = json.dumps(
        {"detail": f"rejected {client_secret} {agent_token}"}
    ).encode("utf-8")
    error = HTTPError(
        url="https://hermes.example.test/openapi/v1/application-agents/test1/responses",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=io.BytesIO(error_body),
    )
    opener = FakeOpenUrl(error)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = smoke.main(
        [
            "--base-url",
            "https://hermes.example.test",
            "--client-id",
            "client-test1",
            "--client-secret",
            client_secret,
            "--agent-token",
            agent_token,
        ],
        stdout=stdout,
        stderr=stderr,
        open_url=opener,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert client_secret not in stderr.getvalue()
    assert agent_token not in stderr.getvalue()
    assert "<redacted>" in stderr.getvalue()


def test_help_states_that_external_responses_are_json_only() -> None:
    help_text = smoke.build_parser().format_help()

    assert "普通 JSON" in help_text
    assert "不支持 stream=true 或 SSE 流式输出" in help_text
