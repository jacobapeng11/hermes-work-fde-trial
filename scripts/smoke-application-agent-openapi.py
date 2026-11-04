#!/usr/bin/env python3
"""Call a published Application Agent through its external OpenAPI.

The command intentionally accepts credentials only through process arguments or
environment variables. It never reads the platform database and never persists
credentials. The current public responses endpoint returns one JSON document;
SSE streaming is not supported by this smoke client or by that endpoint.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.proxy_environment import clear_proxy_environment


clear_proxy_environment()
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))
_open_url = _NO_PROXY_OPENER.open


DEFAULT_AGENT_SLUG = "test1"
DEFAULT_INPUT = "个人知识库里面有啥内容"
KNOWN_KNOWLEDGE_ERROR = "runtime knowledge platform request failed"


class SmokeError(RuntimeError):
    """A safe-to-report failure from one smoke-test stage."""

    def __init__(
        self,
        *,
        stage: str,
        code: str,
        message: str,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str
    client_id: str
    client_secret: str
    agent_token: str | None
    authorization_code: str | None
    agent_slug: str
    input_text: str
    conversation_key: str | None
    request_id: str
    timeout_seconds: float

    def redacted(self, text: str) -> str:
        result = text
        for secret in (
            self.client_secret,
            self.agent_token,
            self.authorization_code,
        ):
            if secret:
                result = result.replace(secret, "<redacted>")
        return result


OpenUrl = Callable[..., Any]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "通过外部 POST 接口调用已发布的应用 Agent，并验证用户委托身份与回答。"
        ),
        epilog=(
            "当前 /responses 接口仅返回普通 JSON，不支持 stream=true 或 SSE 流式输出。"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("HERMES_BASE_URL"),
        help="平台根地址；也可使用 HERMES_BASE_URL",
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("HERMES_APPLICATION_CLIENT_ID"),
        help="ApplicationClient ID；也可使用 HERMES_APPLICATION_CLIENT_ID",
    )
    parser.add_argument(
        "--client-secret",
        default=os.getenv("HERMES_APPLICATION_CLIENT_SECRET"),
        help=(
            "ApplicationClient Secret；推荐使用"
            " HERMES_APPLICATION_CLIENT_SECRET，避免出现在命令历史中"
        ),
    )
    parser.add_argument(
        "--agent-token",
        default=os.getenv("HERMES_APPLICATION_AGENT_TOKEN"),
        help="短期用户委托 Agent Token；也可使用 HERMES_APPLICATION_AGENT_TOKEN",
    )
    parser.add_argument(
        "--authorization-code",
        default=os.getenv("HERMES_APPLICATION_AUTHORIZATION_CODE"),
        help=(
            "Enterprise SSO 用户授权产生的一次性 code；也可使用"
            " HERMES_APPLICATION_AUTHORIZATION_CODE"
        ),
    )
    parser.add_argument(
        "--agent-slug",
        default=DEFAULT_AGENT_SLUG,
        help=f"应用 Agent slug，默认 {DEFAULT_AGENT_SLUG}",
    )
    parser.add_argument(
        "--input",
        dest="input_text",
        default=DEFAULT_INPUT,
        help=f"测试问题，默认“{DEFAULT_INPUT}”",
    )
    parser.add_argument(
        "--conversation-key",
        default=None,
        help="可选长期会话键；不传时创建独立单次会话",
    )
    parser.add_argument(
        "--request-id",
        default=None,
        help="可选请求跟踪 ID；默认自动生成 smoke- 前缀 UUID",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=180.0,
        help="请求超时秒数，默认 180",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> SmokeConfig:
    missing = [
        label
        for label, value in (
            ("--base-url/HERMES_BASE_URL", args.base_url),
            ("--client-id/HERMES_APPLICATION_CLIENT_ID", args.client_id),
            (
                "--client-secret/HERMES_APPLICATION_CLIENT_SECRET",
                args.client_secret,
            ),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise SmokeError(
            stage="arguments",
            code="missing_required_argument",
            message=f"缺少必填参数：{', '.join(missing)}",
        )
    if bool(args.agent_token) == bool(args.authorization_code):
        raise SmokeError(
            stage="arguments",
            code="delegated_identity_required",
            message="必须且只能提供 --agent-token 或 --authorization-code 之一",
        )
    if not str(args.agent_slug or "").strip():
        raise SmokeError(
            stage="arguments",
            code="agent_slug_required",
            message="应用 Agent slug 不能为空",
        )
    if not str(args.input_text or "").strip():
        raise SmokeError(
            stage="arguments",
            code="input_required",
            message="测试问题不能为空",
        )
    if args.timeout_seconds <= 0:
        raise SmokeError(
            stage="arguments",
            code="invalid_timeout",
            message="--timeout-seconds 必须大于 0",
        )
    return SmokeConfig(
        base_url=str(args.base_url).strip().rstrip("/"),
        client_id=str(args.client_id).strip(),
        client_secret=str(args.client_secret),
        agent_token=str(args.agent_token).strip() if args.agent_token else None,
        authorization_code=(
            str(args.authorization_code).strip()
            if args.authorization_code
            else None
        ),
        agent_slug=str(args.agent_slug).strip(),
        input_text=str(args.input_text).strip(),
        conversation_key=(
            str(args.conversation_key).strip()
            if args.conversation_key
            else None
        ),
        request_id=(
            str(args.request_id).strip()
            if args.request_id
            else f"smoke-{uuid.uuid4()}"
        ),
        timeout_seconds=float(args.timeout_seconds),
    )


def _basic_authorization(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {encoded}"


def _error_detail(raw: bytes, fallback: str) -> tuple[str, str]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback, fallback
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        code = detail if re.fullmatch(r"[a-z0-9_]+", detail) else fallback
        return code, detail
    if isinstance(detail, dict):
        raw_code = str(detail.get("code") or "")
        code = (
            raw_code
            if re.fullmatch(r"[a-z0-9_]+", raw_code)
            else fallback
        )
        message = str(detail.get("message") or code)
        return code, message
    return fallback, fallback


def _post_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
    stage: str,
    open_url: OpenUrl,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "hermes-application-agent-smoke/1",
            **headers,
        },
        method="POST",
    )
    try:
        with open_url(request, timeout=timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        code, message = _error_detail(
            raw,
            f"http_{exc.code}",
        )
        raise SmokeError(
            stage=stage,
            code=code,
            message=f"HTTP {exc.code}: {message}",
            http_status=int(exc.code),
        ) from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise SmokeError(
            stage=stage,
            code="transport_error",
            message=f"请求平台失败：{type(exc).__name__}",
        ) from exc
    if status != 200:
        code, message = _error_detail(raw, f"http_{status}")
        raise SmokeError(
            stage=stage,
            code=code,
            message=f"HTTP {status}: {message}",
            http_status=status,
        )
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(
            stage=stage,
            code="invalid_json_response",
            message="平台返回的响应不是有效 JSON",
            http_status=status,
        ) from exc
    if not isinstance(result, dict):
        raise SmokeError(
            stage=stage,
            code="invalid_json_response",
            message="平台 JSON 响应必须是对象",
            http_status=status,
        )
    return result


def _exchange_token(
    config: SmokeConfig,
    *,
    open_url: OpenUrl,
) -> str:
    payload = _post_json(
        url=f"{config.base_url}/openapi/v1/application-agents/token",
        headers={
            "Authorization": _basic_authorization(
                config.client_id,
                config.client_secret,
            )
        },
        payload={
            "grant_type": "authorization_code",
            "code": config.authorization_code,
        },
        timeout_seconds=config.timeout_seconds,
        stage="token_exchange",
        open_url=open_url,
    )
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise SmokeError(
            stage="token_exchange",
            code="agent_token_missing",
            message="Token 交换成功响应中缺少 access_token",
        )
    return token


def _validate_response(
    payload: dict[str, Any],
    *,
    expected_agent_slug: str,
) -> None:
    if payload.get("application_agent") != expected_agent_slug:
        raise SmokeError(
            stage="response_validation",
            code="unexpected_application_agent",
            message="响应中的 application_agent 与请求目标不一致",
        )
    identity = payload.get("identity")
    if (
        not isinstance(identity, dict)
        or identity.get("type") != "user"
        or not str(identity.get("key") or "").startswith("user:")
    ):
        raise SmokeError(
            stage="response_validation",
            code="delegated_user_identity_missing",
            message="响应没有确认有效的 user 委托身份",
        )
    if payload.get("status") != "completed":
        raise SmokeError(
            stage="response_validation",
            code="run_not_completed",
            message="应用 Agent Run 未正常完成",
        )
    for field in ("request_id", "response_id", "runtime_session_id"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise SmokeError(
                stage="response_validation",
                code=f"{field}_missing",
                message=f"响应缺少非空 {field}",
            )
    output_text = payload.get("output_text")
    if not isinstance(output_text, str) or not output_text.strip():
        raise SmokeError(
            stage="response_validation",
            code="output_text_missing",
            message="应用 Agent 没有返回回答文本",
        )
    if KNOWN_KNOWLEDGE_ERROR in output_text.lower():
        raise SmokeError(
            stage="response_validation",
            code="runtime_knowledge_platform_request_failed",
            message="应用 Agent 回答表明个人知识库调用失败",
        )


def run_smoke(
    config: SmokeConfig,
    *,
    open_url: OpenUrl = _open_url,
) -> dict[str, Any]:
    started = time.monotonic()
    token = config.agent_token or _exchange_token(config, open_url=open_url)
    request_payload: dict[str, Any] = {
        "input": config.input_text,
        "request_id": config.request_id,
    }
    if config.conversation_key:
        request_payload["conversation_key"] = config.conversation_key
    response = _post_json(
        url=(
            f"{config.base_url}/openapi/v1/application-agents/"
            f"{config.agent_slug}/responses"
        ),
        headers={
            "Authorization": _basic_authorization(
                config.client_id,
                config.client_secret,
            ),
            "X-Hermes-Agent-Token": token,
        },
        payload=request_payload,
        timeout_seconds=config.timeout_seconds,
        stage="agent_response",
        open_url=open_url,
    )
    _validate_response(response, expected_agent_slug=config.agent_slug)
    return {
        "success": True,
        "response_mode": "json",
        "application_agent": response["application_agent"],
        "request_id": response["request_id"],
        "response_id": response["response_id"],
        "runtime_session_id": response["runtime_session_id"],
        "conversation_key": response.get("conversation_key"),
        "identity": response["identity"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "output_text": response["output_text"],
    }


def _hint(code: str) -> str:
    return {
        "application_client_credentials_required": "检查 client_id/client_secret。",
        "application_client_credentials_invalid": "检查 client_id/client_secret。",
        "application_client_unauthorized": "确认 Client 已授权调用 test1。",
        "agent_token_invalid": "重新通过 Enterprise SSO 用户授权流程取得短期 Agent Token。",
        "agent_token_scope_mismatch": "确认 Token、Client 和 Agent slug 相互匹配。",
        "delegated_user_token_required": "个人知识库测试必须提供用户委托 Agent Token。",
        "application_agent_not_found": "确认 test1 已发布且 slug 正确。",
        "conversation_busy": "等待当前长期会话完成或换一个 conversation_key。",
        "application_agent_busy": "等待 Runtime 释放并发容量后重试。",
        "runtime_knowledge_platform_request_failed": "检查 test1 的个人知识库绑定及 Runtime 知识插件。",
        "transport_error": "检查平台地址、网络和网关状态。",
    }.get(code, "查看平台日志和错误 code 后重试。")


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    open_url: OpenUrl = _open_url,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config: SmokeConfig | None = None
    try:
        config = config_from_args(args)
        result = run_smoke(config, open_url=open_url)
    except SmokeError as exc:
        message = config.redacted(str(exc)) if config else str(exc)
        code = exc.code
        if config and config.redacted(code) != code:
            code = "platform_error"
        failure = {
            "success": False,
            "stage": exc.stage,
            "code": code,
            "http_status": exc.http_status,
            "message": message,
            "hint": _hint(code),
        }
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
