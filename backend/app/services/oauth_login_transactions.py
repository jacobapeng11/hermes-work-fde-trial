"""One-use login records, separate from platform sessions and AES replay keys."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlsplit

from redis.asyncio import from_url
from redis.exceptions import RedisError

from app.services.oauth2 import OAuth2Error

TRANSACTION_COOKIE_PREFIX = "hermes_oauth2_"
COMPLETION_COOKIE = "hermes_oauth2_completion"
CALLBACK_PATH = "/api/v1/auth/oauth2/callback"
BINDING_COOKIE_PATH = "/api/v1/auth/oauth2"
COMPLETE_PATH = "/api/v1/auth/oauth2/complete"
_TAKE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return false end
local data = cjson.decode(raw)
if data.binding ~= ARGV[1] or data.fingerprint ~= ARGV[2] then return false end
redis.call('DEL', KEYS[1])
return raw
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def valid_nonce(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", value))


def transaction_cookie(state: str) -> str:
    return TRANSACTION_COOKIE_PREFIX + digest(state)[:24]


def safe_return_to(raw: str) -> str:
    if len(raw) > 2048:
        raise OAuth2Error("oauth2_invalid_request", "登录返回路径无效")
    decoded = raw
    for _ in range(3):
        decoded = unquote(decoded)
    try:
        url = urlsplit(decoded)
    except ValueError:
        raise OAuth2Error("oauth2_invalid_request", "登录返回路径无效") from None
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
        or url.scheme
        or url.netloc
        or any(part in {".", ".."} for part in url.path.split("/"))
        or not (url.path.startswith(("/user/", "/admin/")) or url.path == "/admin")
        or any(
            key.casefold()
            in {
                "token",
                "access_token",
                "refresh_token",
                "code",
                "state",
                "session_id",
                "qw_token",
                "ticket",
            }
            for key, _ in parse_qsl(url.query)
        )
    ):
        raise OAuth2Error("oauth2_invalid_request", "登录返回路径无效")
    return raw


class OAuthLoginTransactions:
    def __init__(self, client) -> None:
        self.client = client

    async def put(self, kind: str, data: dict, *, ttl: int) -> str:
        nonce = secrets.token_urlsafe(32)
        try:
            await self.client.set(
                f"hermes:oauth2:{kind}:v1:{digest(nonce)}",
                json.dumps(data, separators=(",", ":")),
                ex=ttl,
            )
        except RedisError:
            raise OAuth2Error("oauth2_store_unavailable", "认证会话存储暂时不可用", 503) from None
        return nonce

    async def take(self, kind: str, nonce: str, *, binding: str, fingerprint: str) -> dict:
        if not valid_nonce(nonce) or not binding:
            raise OAuth2Error("oauth2_transaction_invalid", "登录事务已失效，请重新登录")
        try:
            raw = await self.client.eval(
                _TAKE, 1, f"hermes:oauth2:{kind}:v1:{digest(nonce)}", digest(binding), fingerprint
            )
        except RedisError:
            raise OAuth2Error("oauth2_store_unavailable", "认证会话存储暂时不可用", 503) from None
        if not raw:
            raise OAuth2Error("oauth2_transaction_invalid", "登录事务已失效，请重新登录")
        return json.loads(raw)


@lru_cache(maxsize=1)
def shared_oauth_transactions() -> OAuthLoginTransactions:
    from app.config import settings

    return OAuthLoginTransactions(from_url(settings.REDIS_URL, decode_responses=True))
