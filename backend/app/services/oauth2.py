"""Standard OAuth2 authorization-code client; enterprise_idpAES remains a separate protocol."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import httpx

RESERVED_PARAMETERS = {
    "client_id",
    "redirect_uri",
    "response_type",
    "response_mode",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "code_verifier",
    "client_secret",
}
MAX_RESPONSE_BYTES = 1024 * 1024
logger = logging.getLogger(__name__)


def _log_upstream_failure(request_id: str, stage: str, reason: str, **diagnostics: Any) -> None:
    # Values are bounded metadata assembled below; never pass a URL, response,
    # exception message, request headers/body or provider identity into this log.
    safe_id = request_id if isinstance(request_id, str) and re.fullmatch(r"[0-9a-f]{24}", request_id) else "-"
    logger.warning(
        "oauth2_upstream_failed request_id=%s stage=%s reason=%s diagnostics=%s",
        safe_id, stage, reason, json.dumps(diagnostics, sort_keys=True),
    )


class OAuth2Error(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def validate_oauth2_settings(config: Any) -> None:
    """Validate only the selected OAuth2 connection; never include values in errors."""
    required = (
        "PROVIDER_ID",
        "CLIENT_ID",
        "CLIENT_SECRET",
        "AUTHORIZATION_URL",
        "TOKEN_URL",
        "USERINFO_URL",
        "REDIRECT_URI",
        "USER_ID_FIELD",
        "USER_WORK_NO_FIELD",
    )
    for key in required:
        value = getattr(config, f"OAUTH2_{key}")
        if not value.strip() or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError(f"OAUTH2_{key} must be configured with a valid value")
    allow_http = config.OAUTH2_ALLOW_INSECURE_HTTP
    if allow_http and config.APP_ENV.lower() != "dev":
        raise ValueError("OAUTH2_ALLOW_INSECURE_HTTP is only allowed when APP_ENV=dev")
    for key in ("AUTHORIZATION_URL", "TOKEN_URL", "USERINFO_URL", "REDIRECT_URI"):
        raw = getattr(config, f"OAUTH2_{key}")
        schemes = {"https", "http"} if allow_http and key in {"TOKEN_URL", "USERINFO_URL"} else {"https"}
        expected = "HTTP or HTTPS" if "http" in schemes else "HTTPS"
        try:
            url = urlsplit(raw)
            _ = url.port
        except ValueError:
            raise ValueError(f"OAUTH2_{key} must be a valid {expected} URL") from None
        if (
            url.scheme not in schemes
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.fragment
            or "\\" in raw
            or any(k in RESERVED_PARAMETERS for k, _ in parse_qsl(url.query))
        ):
            raise ValueError(
                f"OAUTH2_{key} must be an {expected} URL without credentials or protocol parameters"
            )
    if urlsplit(config.OAUTH2_REDIRECT_URI).path != "/api/v1/auth/oauth2/callback":
        raise ValueError("OAUTH2_REDIRECT_URI must use the OAuth2 callback path")
    if config.OAUTH2_TOKEN_AUTH_METHOD not in {"client_secret_basic", "client_secret_post"}:
        raise ValueError("OAUTH2_TOKEN_AUTH_METHOD is invalid")
    if config.OAUTH2_USERINFO_METHOD not in {"GET", "POST"}:
        raise ValueError("OAUTH2_USERINFO_METHOD is invalid")
    for key in ("USER_ID_FIELD", "USER_WORK_NO_FIELD", "USER_NAME_FIELD", "USER_EMAIL_FIELD"):
        path = getattr(config, f"OAUTH2_{key}")
        if path and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}", path
        ):
            raise ValueError(f"OAUTH2_{key} must be a limited dot path")
    authorization_parameters(config)


def authorization_parameters(config: Any) -> dict[str, str]:
    try:
        params = json.loads(config.OAUTH2_AUTHORIZATION_PARAMS)
    except (ValueError, TypeError):
        raise ValueError("OAUTH2_AUTHORIZATION_PARAMS must be a JSON object") from None
    if not isinstance(params, dict) or len(params) > 16:
        raise ValueError("OAUTH2_AUTHORIZATION_PARAMS must be a limited JSON object")
    for key, value in params.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
            or key.casefold() in RESERVED_PARAMETERS
            or type(value) not in (str, int, bool)
            or len(str(value)) > 512
        ):
            raise ValueError(
                "OAUTH2_AUTHORIZATION_PARAMS contains an invalid or reserved parameter"
            )
    return {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in params.items()
    }


def config_fingerprint(config: Any) -> str:
    keys = (
        "HERMES_AUTH_MODE",
        "DEFAULT_ORG_ID",
        "OAUTH2_PROVIDER_ID",
        "OAUTH2_CLIENT_ID",
        "OAUTH2_AUTHORIZATION_URL",
        "OAUTH2_TOKEN_URL",
        "OAUTH2_USERINFO_URL",
        "OAUTH2_REDIRECT_URI",
        "OAUTH2_SCOPES",
        "OAUTH2_PKCE_ENABLED",
        "OAUTH2_ALLOW_INSECURE_HTTP",
        "OAUTH2_TOKEN_AUTH_METHOD",
        "OAUTH2_USERINFO_METHOD",
        "OAUTH2_USER_ID_FIELD",
        "OAUTH2_USER_WORK_NO_FIELD",
        "OAUTH2_USER_NAME_FIELD",
        "OAUTH2_USER_EMAIL_FIELD",
        "OAUTH2_AUTHORIZATION_PARAMS",
        "OAUTH2_CONFIG_REVISION",
    )
    return hashlib.sha256(
        json.dumps({k: getattr(config, k) for k in keys}, sort_keys=True).encode()
    ).hexdigest()


class OAuth2Client:
    def __init__(self, config: Any, *, client: httpx.AsyncClient | None = None) -> None:
        self.config, self.client = config, client

    def authorization_url(self, *, state: str, verifier: str) -> str:
        config = self.config
        params = authorization_parameters(config)
        params.update(
            response_type="code",
            client_id=config.OAUTH2_CLIENT_ID,
            redirect_uri=config.OAUTH2_REDIRECT_URI,
            state=state,
        )
        if config.OAUTH2_PKCE_ENABLED:
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )
            params.update(code_challenge=challenge, code_challenge_method="S256")
        if config.OAUTH2_SCOPES:
            params["scope"] = config.OAUTH2_SCOPES
        url = urlsplit(config.OAUTH2_AUTHORIZATION_URL)
        return urlunsplit(
            url._replace(query=urlencode(parse_qsl(url.query) + list(params.items())))
        )

    async def user_info(self, *, code: str, verifier: str, request_id: str = "") -> dict:
        config = self.config
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.OAUTH2_REDIRECT_URI,
        }
        if config.OAUTH2_PKCE_ENABLED:
            form["code_verifier"] = verifier
        headers = {"Accept": "application/json"}
        if config.OAUTH2_TOKEN_AUTH_METHOD == "client_secret_basic":
            pair = (
                f"{quote_plus(config.OAUTH2_CLIENT_ID)}:{quote_plus(config.OAUTH2_CLIENT_SECRET)}"
            )
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:
            form.update(
                client_id=config.OAUTH2_CLIENT_ID, client_secret=config.OAUTH2_CLIENT_SECRET
            )
        token = await self._json("POST", config.OAUTH2_TOKEN_URL, stage="token", request_id=request_id,
                                 data=form, headers=headers)
        access = token.get("access_token")
        access_valid = isinstance(access, str) and bool(re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", access))
        bearer = str(token.get("token_type", "")).casefold() == "bearer"
        if not access_valid or not bearer:
            provider_error = token.get("error")
            known_errors = {"invalid_client", "invalid_grant", "invalid_request", "unauthorized_client",
                            "unsupported_grant_type", "invalid_scope", "server_error", "temporarily_unavailable"}
            safe_error = provider_error if isinstance(provider_error, str) and provider_error in known_errors else "other_or_absent"
            _log_upstream_failure(request_id, "token", "invalid_token_response", http_status=200,
                                  access_token_valid=access_valid, token_type_bearer=bearer,
                                  wrapped_data=isinstance(token.get("data"), dict), oauth_error=safe_error)
            raise OAuth2Error("oauth2_upstream_failed", "认证服务返回无效凭据", 502)
        return await self._json(
            config.OAUTH2_USERINFO_METHOD,
            config.OAUTH2_USERINFO_URL,
            stage="userinfo", request_id=request_id,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
        )

    async def _json(self, method: str, url: str, *, stage: str = "unknown", request_id: str = "", **kwargs: Any) -> dict:
        timeout = float(self.config.OAUTH2_HTTP_TIMEOUT_SECONDS)
        http_status, content_type = 0, "unknown"

        def record(reason: str) -> None:
            _log_upstream_failure(request_id, stage, reason, http_status=http_status, content_type=content_type)

        async def read(client: httpx.AsyncClient) -> dict:
            nonlocal http_status, content_type
            async with client.stream(
                method,
                url,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout, connect=min(3.0, timeout)),
                **kwargs,
            ) as response:
                http_status = response.status_code
                mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                content_type = mime if mime in {"application/json", "text/html", "text/plain"} else "other"
                if (
                    response.status_code != 200
                    or mime != "application/json"
                ):
                    record("http_status" if response.status_code != 200 else "content_type")
                    raise OAuth2Error("oauth2_upstream_failed", "认证服务暂时不可用", 502)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        record("response_too_large")
                        raise OAuth2Error("oauth2_upstream_failed", "认证服务响应无效", 502)
                result = json.loads(data)
                if not isinstance(result, dict):
                    record("response_not_object")
                    raise OAuth2Error("oauth2_upstream_failed", "认证服务响应无效", 502)
                return result

        try:
            async with asyncio.timeout(timeout):
                if self.client is not None:
                    return await read(self.client)
                async with httpx.AsyncClient(verify=True, trust_env=False) as client:
                    return await read(client)
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError) as error:
            if isinstance(error, (TimeoutError, httpx.TimeoutException)):
                reason = "timeout"
            elif isinstance(error, httpx.ConnectError):
                reason = "tls_verification" if "CERTIFICATE_VERIFY_FAILED" in str(error).upper() else "connect_error"
            elif isinstance(error, httpx.HTTPError):
                reason = "transport_error"
            else:
                reason = "invalid_json"
            record(reason)
            raise OAuth2Error(
                "oauth2_upstream_failed", "认证服务暂时不可用或响应无效", 502
            ) from None
