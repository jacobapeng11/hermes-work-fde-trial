#!/usr/bin/env python3
"""Probe an OpenAI-compatible Provider from inside a Runtime container.

The API key is never embedded in this file. Provide it with MODEL_PROBE_API_KEY
or pass --api-key-stdin. A real chat completion is sent only when --chat is
explicitly requested.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import socket
import ssl
import sys
from dataclasses import dataclass
from time import monotonic
from typing import Any
from urllib.parse import urlsplit


# Edit these defaults for the target Provider/model under investigation.
# Keep the base URL at the provider root (for example /v1), not /chat/completions.
DEFAULT_BASE_URL = "https://provider.example.com/v1"
DEFAULT_MODEL_ID = "deepseek-v4-flash-h100"
API_KEY_ENV = "MODEL_PROBE_API_KEY"
MAX_RESPONSE_CHARS = 2000


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: int | None
    reason: str
    body: str
    elapsed_ms: float
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Provider DNS/TCP/TLS and OpenAI-compatible HTTP routes"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Provider base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL_ID,
        help=f"Upstream model ID used by optional --chat (default: {DEFAULT_MODEL_ID})",
    )
    parser.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="read one API key line from stdin; avoids putting the key in a command line",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="send one minimal /chat/completions request (requires an API key and may incur cost)",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="include thinking={type:enabled} in the optional chat request",
    )
    parser.add_argument(
        "--require-model-listing",
        action="store_true",
        help="fail when authenticated /models succeeds but does not list --model",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="per DNS/TLS/HTTP operation timeout in seconds (default: 10)",
    )
    return parser.parse_args()


def normalize_base_url(value: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base URL scheme must be http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base URL must include a host and must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not include a query or fragment")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("base URL port is invalid") from exc
    return parsed.scheme, parsed.hostname, port, (parsed.path or "/").rstrip("/")


def dns_probe(host: str, timeout: float) -> list[str]:
    addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in addresses})


def tls_probe(host: str, timeout: float) -> dict[str, Any]:
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=timeout) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            certificate = tls_socket.getpeercert()
            cipher_tuple = tls_socket.cipher()
    return {
        "tls_version": tls_socket.version() or "unknown",
        "cipher": cipher_tuple[0] if cipher_tuple else "unknown",
        "not_after": certificate.get("notAfter"),
        "subject": certificate.get("subject"),
        "issuer": certificate.get("issuer"),
    }


def request(
    *,
    scheme: str,
    host: str,
    port: int,
    base_path: str,
    suffix: str,
    method: str,
    api_key: str | None,
    payload: dict[str, Any] | None,
    timeout: float,
) -> ProbeResult:
    connection: http.client.HTTPConnection
    if scheme == "https":
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            host,
            port,
            timeout=timeout,
            context=context,
        )
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)

    path = f"{base_path}{suffix}"
    headers = {
        "Accept": "application/json",
        "Connection": "close",
    }
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    started = monotonic()
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw_body = response.read(128 * 1024)
        elapsed_ms = (monotonic() - started) * 1000
        return ProbeResult(
            status=response.status,
            reason=response.reason,
            body=redact(raw_body.decode("utf-8", errors="replace")),
            elapsed_ms=elapsed_ms,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic script reports all transport errors.
        elapsed_ms = (monotonic() - started) * 1000
        return ProbeResult(
            status=None,
            reason="transport error",
            body="",
            elapsed_ms=elapsed_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        connection.close()


def redact(value: str) -> str:
    value = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value)
    value = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-[redacted]", value)
    return value[:MAX_RESPONSE_CHARS]


def print_response(title: str, result: ProbeResult) -> None:
    print(f"\n[{title}]")
    if result.error is not None:
        print(f"  transport: FAILED error={result.error}")
        print(f"  elapsed_ms: {result.elapsed_ms:.1f}")
        return
    print(
        f"  transport: reachable status={result.status} "
        f"reason={result.reason} elapsed_ms={result.elapsed_ms:.1f}"
    )
    print(f"  response: {result.body!r}")


def model_ids(body: str) -> list[str]:
    try:
        document = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return []
    items = document.get("data") if isinstance(document, dict) else None
    if not isinstance(items, list):
        return []
    return [
        str(item.get("id"))
        for item in items
        if isinstance(item, dict) and item.get("id") is not None
    ]


def read_api_key(args: argparse.Namespace) -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if args.api_key_stdin:
        line = sys.stdin.readline()
        if line.strip():
            key = line.strip()
    return key


def main() -> int:
    args = parse_args()
    try:
        scheme, host, port, base_path = normalize_base_url(args.base_url)
    except ValueError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    api_key = read_api_key(args)
    print("Provider network probe")
    print(f"  base_url: {args.base_url}")
    print(f"  model: {args.model}")
    print(f"  api_key: {'provided' if api_key else 'not provided'}")

    try:
        addresses = dns_probe(host, args.timeout)
        print(f"\n[DNS] PASS ips={addresses}")
    except Exception as exc:  # noqa: BLE001 - diagnostic script reports all DNS errors.
        print(f"\n[DNS] FAILED error={type(exc).__name__}: {exc}")
        return 2

    if scheme == "https":
        try:
            tls = tls_probe(host, args.timeout)
            print(
                "[TLS] PASS "
                f"version={tls['tls_version']} cipher={tls['cipher']} "
                f"not_after={tls['not_after']}"
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic script reports all TLS errors.
            print(f"[TLS] FAILED error={type(exc).__name__}: {exc}")
            return 2
    else:
        print("[TLS] SKIP base_url uses HTTP (platform Provider route requires HTTPS)")

    models = request(
        scheme=scheme,
        host=host,
        port=port,
        base_path=base_path,
        suffix="/models",
        method="GET",
        api_key=api_key or None,
        payload=None,
        timeout=args.timeout,
    )
    print_response("GET /models", models)
    if models.status is None:
        return 2
    if models.status == 401 and not api_key:
        print("RESULT network=PASS auth=NOT_TESTED http=REACHABLE")
        return 0
    if api_key and not 200 <= models.status < 300:
        print("RESULT network=PASS auth=FAIL http=REACHABLE")
        return 3

    listed_models = model_ids(models.body)
    model_listed = args.model in listed_models
    if listed_models:
        preview = ", ".join(listed_models[:20])
        print(f"  model_listed={model_listed} model_preview={preview}")
    else:
        print("  model_listed=unknown response did not contain a model data list")

    chat_failed = False
    if args.chat:
        if not api_key:
            print("CHAT ERROR: --chat requires an API key", file=sys.stderr)
            return 3
        payload: dict[str, Any] = {
            "model": args.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        if args.thinking:
            payload["thinking"] = {"type": "enabled"}
        chat = request(
            scheme=scheme,
            host=host,
            port=port,
            base_path=base_path,
            suffix="/chat/completions",
            method="POST",
            api_key=api_key,
            payload=payload,
            timeout=max(args.timeout, 30.0),
        )
        print_response("POST /chat/completions", chat)
        chat_failed = chat.status is None or not 200 <= chat.status < 300

    auth_result = "PASS" if not api_key or 200 <= models.status < 300 else "FAIL"
    listing_result = "PASS"
    if args.require_model_listing and not model_listed:
        listing_result = "FAIL"
    chat_result = "PASS" if not args.chat or not chat_failed else "FAIL"
    print(
        f"RESULT network=PASS auth={auth_result} "
        f"model_listing={listing_result} chat={chat_result}"
    )
    if auth_result == "FAIL" or listing_result == "FAIL" or chat_result == "FAIL":
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
