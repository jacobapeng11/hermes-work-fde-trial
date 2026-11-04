from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "benchmark-deepseek-ttft.py"
)
SPEC = importlib.util.spec_from_file_location(
    "deepseek_ttft_benchmark",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        status: int = 200,
        body: bytes = b"",
    ) -> None:
        self.chunks = list(chunks or [])
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read1(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class FakeOpenUrl:
    def __init__(self, *responses: FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout: float):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        value = self.value
        self.value += 10_000_000
        return value


def config(*, runs: int = 1):
    return benchmark.BenchmarkConfig(
        providers=(
            benchmark.Provider(
                name="deepseek-zy",
                base_url="https://api.deepseek.example",
                api_key="deepseek-secret",
            ),
            benchmark.Provider(
                name="openai-compatible",
                base_url="https://oneapi.example/v1",
                api_key="compatible-secret",
            ),
        ),
        model="deepseek-v4-flash",
        prompt="你好",
        runs=runs,
        timeout_seconds=120.0,
    )


def sse_event(delta: dict) -> bytes:
    payload = json.dumps(
        {"choices": [{"delta": delta}]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"data: {payload}\n\n".encode()


def success_response() -> FakeResponse:
    return FakeResponse(
        [
            sse_event({"role": "assistant"}),
            sse_event({"content": "你好"}),
            b"data: [DONE]\n\n",
        ]
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (
            "https://api.deepseek.com",
            "https://api.deepseek.com/chat/completions",
        ),
        (
            "https://provider.example.com/v1/",
            "https://provider.example.com/v1/chat/completions",
        ),
        (
            "https://example.test/v1/chat/completions",
            "https://example.test/v1/chat/completions",
        ),
    ],
)
def test_chat_completions_url(base_url: str, expected: str) -> None:
    assert benchmark.chat_completions_url(base_url) == expected


def test_measure_sample_uses_first_semantic_delta_and_handles_split_utf8() -> None:
    role = sse_event({"role": "assistant"})
    whitespace = sse_event({"reasoning_content": "  "})
    reasoning = sse_event({"reasoning_content": "你"})
    answer = sse_event({"content": "好"})
    split_at = answer.index("好".encode()) + 1
    response = FakeResponse(
        [
            role,
            whitespace,
            reasoning,
            answer[:split_at],
            answer[split_at:],
            b"data: [DONE]\n\n",
        ]
    )

    result = benchmark.measure_sample(
        config().providers[0],
        config(),
        1,
        open_url=FakeOpenUrl(response),
        clock=FakeClock(),
    )

    assert result.success is True
    assert result.headers_ms == 10.0
    assert result.semantic_ttft_ms == 40.0
    assert result.answer_ttft_ms == 60.0


def test_main_alternates_provider_order_and_continues_after_failure() -> None:
    secret = "compatible-secret"
    error = HTTPError(
        url="https://oneapi.example/v1/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=io.BytesIO(
            f"Authorization: Bearer {secret}".encode()
        ),
    )
    opener = FakeOpenUrl(
        success_response(),
        error,
        success_response(),
        success_response(),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    env = {
        "DEEPSEEK_ZY_API_KEY": "deepseek-secret",
        "OPENAI_COMPATIBLE_API_KEY": secret,
        "DEEPSEEK_ZY_BASE_URL": "https://deepseek.example",
        "OPENAI_COMPATIBLE_BASE_URL": "https://oneapi.example/v1",
        "TTFT_RUNS": "2",
    }

    exit_code = benchmark.main(
        environ=env,
        stdout=stdout,
        stderr=stderr,
        open_url=opener,
        clock=FakeClock(),
    )

    assert exit_code == 1
    assert [request.full_url for request, _ in opener.requests] == [
        "https://deepseek.example/chat/completions",
        "https://oneapi.example/v1/chat/completions",
        "https://oneapi.example/v1/chat/completions",
        "https://deepseek.example/chat/completions",
    ]
    output = stdout.getvalue()
    assert "HTTP 429" in output
    assert secret not in output
    assert "[deepseek-zy] run=2 status=ok" in output
    assert stderr.getvalue() == ""


def test_missing_keys_fails_before_any_request() -> None:
    opener = FakeOpenUrl()
    stderr = io.StringIO()

    exit_code = benchmark.main(
        environ={},
        stdout=io.StringIO(),
        stderr=stderr,
        open_url=opener,
    )

    assert exit_code == 2
    assert "DEEPSEEK_ZY_API_KEY" in stderr.getvalue()
    assert "OPENAI_COMPATIBLE_API_KEY" in stderr.getvalue()
    assert opener.requests == []


def test_stream_without_semantic_delta_is_a_failed_sample() -> None:
    response = FakeResponse(
        [
            sse_event({"role": "assistant"}),
            b"data: [DONE]\n\n",
        ]
    )

    result = benchmark.measure_sample(
        config().providers[0],
        config(),
        1,
        open_url=FakeOpenUrl(response),
        clock=FakeClock(),
    )

    assert result.success is False
    assert "未收到非空" in str(result.error)


def test_real_standard_library_http_stream_path() -> None:
    request_details: dict[str, object] = {}
    answer = sse_event({"content": "好"})
    split_at = answer.index("好".encode()) + 1
    chunks = [
        sse_event({"role": "assistant"}),
        sse_event({"reasoning_content": "你"}),
        answer[:split_at],
        answer[split_at:],
        b"data: [DONE]\n\n",
    ]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            request_details["path"] = self.path
            request_details["authorization"] = self.headers.get(
                "Authorization"
            )
            request_details["body"] = json.loads(
                self.rfile.read(length).decode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(0.005)
            self.close_connection = True

        def log_message(self, _format: str, *_args) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        provider = benchmark.Provider(
            name="local",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="local-secret",
        )
        local_config = benchmark.BenchmarkConfig(
            providers=(provider, provider),
            model="deepseek-v4-flash",
            prompt="你好",
            runs=1,
            timeout_seconds=5.0,
        )

        result = benchmark.measure_sample(provider, local_config, 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.success is True
    assert result.semantic_ttft_ms is not None
    assert result.answer_ttft_ms is not None
    assert result.semantic_ttft_ms <= result.answer_ttft_ms
    assert request_details == {
        "path": "/v1/chat/completions",
        "authorization": "Bearer local-secret",
        "body": {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
            "temperature": 0,
            "max_tokens": 64,
        },
    }
