from __future__ import annotations

from dataclasses import dataclass
import hmac
import logging
import time
from typing import Any, Protocol
from uuid import uuid4

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentRun,
    ApplicationAgentRuntimeBinding,
    ApplicationClient,
    CloudHermesProfile,
    HermesRuntimeBinding,
    KnowledgeBase,
    RuntimeAgentProfile,
    User,
)
from app.services.enterprise_knowledge_settings import enterprise_knowledge_enabled
from app.services.knowledge_assertion import KnowledgeAssertionIssuer, KnowledgeIdentity
from app.services.runtime_agent_profiles import executable_runtime_agent_profile_clauses
from app.services.runtime_workspace import Principal, RuntimeApiKeyCodec
from app.services.personal_knowledge_acl import PersonalKnowledgeAclService


knowledge_logger = logging.getLogger("hermes.knowledge")


KNOWN_DOWNSTREAM_ERROR_CODES = frozenset(
    {
        "sql_empty",
        "sql_too_long",
        "sql_parse_failed",
        "sql_single_select_required",
        "sql_requires_configured_table",
        "sql_union_not_supported",
        "sql_cte_not_supported",
        "sql_subquery_not_supported",
        "sql_permission_table_forbidden",
        "sql_table_not_configured",
        "sql_lookup_first_not_supported",
        "sql_multiple_protected_tables",
        "sql_metric_contract_violation",
        "knowledge_request_invalid",
        "knowledge_request_forbidden",
    }
)

_ERROR_MESSAGES = {
    "runtime_knowledge_unauthorized": "Runtime 知识身份无效。",
    "application_agent_active_user_run_required": "当前应用 Agent 没有可用的用户运行身份。",
    "knowledge_user_delegation_required": "该知识操作需要用户委派身份。",
    "knowledge_subject_disabled": "当前知识访问主体不可用。",
    "enterprise_knowledge_disabled": "当前 Agent 未启用企业知识能力。",
    "knowledge_identity_missing": "当前 Runtime 缺少企业知识用户身份。",
    "knowledge_scope_not_allowed": "当前 Runtime 身份无权调用该知识能力。",
    "knowledge_service_not_configured": "企业知识服务尚未配置。",
    "knowledge_service_unavailable": "企业知识服务当前不可用。",
    "knowledge_assertion_rejected": "企业知识服务拒绝了短期身份断言。",
    "knowledge_request_rejected": "企业知识服务拒绝了该请求。",
}


class RuntimeKnowledgeError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status_code: int,
        message: str | None = None,
        retryable: bool = False,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.message = message or _ERROR_MESSAGES.get(
            code,
            "企业知识请求失败。",
        )
        self.retryable = retryable
        self.hint = hint
        self.request_id = request_id

    def as_error(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.request_id:
            payload["request_id"] = self.request_id
        return payload


@dataclass(frozen=True)
class RuntimeKnowledgeAccess:
    identity: KnowledgeIdentity
    enabled: bool
    allowed_scopes: frozenset[str] | None = None
    allowed_knowledge_base_ids: frozenset[int] | None = None


class RuntimeKnowledgeStore(Protocol):
    async def authenticate(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimeKnowledgeAccess:
        ...


class SqlAlchemyRuntimeKnowledgeStore:
    def __init__(self, session: AsyncSession, *, codec: RuntimeApiKeyCodec | None = None) -> None:
        self.session = session
        self.codec = codec or RuntimeApiKeyCodec()

    async def authenticate(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimeKnowledgeAccess:
        result = await self.session.execute(
            select(
                HermesRuntimeBinding,
                CloudHermesProfile,
                User,
                RuntimeAgentProfile,
            )
            .join(
                CloudHermesProfile,
                CloudHermesProfile.id == HermesRuntimeBinding.runtime_workspace_id,
            )
            .join(User, User.id == HermesRuntimeBinding.user_id)
            .join(
                RuntimeAgentProfile,
                RuntimeAgentProfile.runtime_workspace_id
                == HermesRuntimeBinding.runtime_workspace_id,
            )
            .where(
                HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                HermesRuntimeBinding.status.in_(("starting", "ready")),
                CloudHermesProfile.status == "active",
                User.status == "active",
                RuntimeAgentProfile.org_id == HermesRuntimeBinding.org_id,
                RuntimeAgentProfile.user_id == HermesRuntimeBinding.user_id,
                *executable_runtime_agent_profile_clauses(),
                (
                    RuntimeAgentProfile.id == runtime_agent_profile_id
                    if runtime_agent_profile_id is not None
                    else RuntimeAgentProfile.profile_kind == "default"
                ),
            )
        )
        row = result.first()
        if row is None:
            raise RuntimeKnowledgeError("runtime_knowledge_unauthorized", status_code=401)
        binding, profile, user, agent_profile = row
        if not (
            binding.org_id == profile.org_id == user.org_id
            and binding.user_id == profile.user_id == user.id
        ):
            raise RuntimeKnowledgeError("runtime_knowledge_unauthorized", status_code=401)
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimeKnowledgeError("runtime_knowledge_unauthorized", status_code=401)
        return RuntimeKnowledgeAccess(
            identity=KnowledgeIdentity(
                org_id=int(user.org_id),
                user_id=int(user.id),
                user_code=str(user.user_code or ""),
                runtime_workspace_id=int(profile.id),
            ),
            enabled=(
                enterprise_knowledge_enabled(profile.profile_config)
                if agent_profile.profile_kind == "default"
                else "enterprise-knowledge"
                in set(
                    (agent_profile.capability_config or {}).get(
                        "plugin_ids",
                        [],
                    )
                )
            ),
        )

    async def authenticate_for_batch(
        self,
        runtime_workspace_id: int,
        runtime_token: str,
    ) -> RuntimeKnowledgeAccess:
        """Relaxed authentication for batch/service-to-service calls.

        Only validates the API key against the binding. Does not require
        an active application agent run or session context.
        """
        result = await self.session.execute(
            select(HermesRuntimeBinding, User)
            .join(User, User.id == HermesRuntimeBinding.user_id)
            .where(
                HermesRuntimeBinding.runtime_workspace_id == runtime_workspace_id,
                HermesRuntimeBinding.status.in_(("starting", "ready")),
                User.status == "active",
            )
        )
        row = result.first()
        if row is None:
            raise RuntimeKnowledgeError("runtime_knowledge_unauthorized", status_code=401)
        binding, user = row
        if not (binding.org_id == user.org_id and binding.user_id == user.id):
            raise RuntimeKnowledgeError("runtime_knowledge_unauthorized", status_code=401)
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimeKnowledgeError("runtime_knowledge_unauthorized", status_code=401)
        return RuntimeKnowledgeAccess(
            identity=KnowledgeIdentity(
                org_id=int(user.org_id),
                user_id=int(user.id),
                user_code=str(user.user_code or ""),
                runtime_workspace_id=int(runtime_workspace_id),
            ),
            enabled=True,
        )


class SqlAlchemyApplicationAgentRuntimeKnowledgeStore:
    """Resolve knowledge identity from the active application Agent run.

    The Runtime/PVC owner is deliberately irrelevant.  A delegated user is
    accepted only while a matching run is active in the same Runtime session.
    Service identities may read application-shared mounted guidance, but are
    denied all user-code-based downstream scopes.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        runtime_session_id: str,
        turn_id: str | None = None,
        tool_call_id: str | None = None,
        codec: RuntimeApiKeyCodec | None = None,
    ) -> None:
        self.session = session
        self.runtime_session_id = runtime_session_id
        self.turn_id = str(turn_id or "").strip() or None
        self.tool_call_id = str(tool_call_id or "").strip() or None
        self.codec = codec or RuntimeApiKeyCodec()

    async def authenticate(
        self,
        application_agent_id: int,
        _runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> RuntimeKnowledgeAccess:
        row = (
            await self.session.execute(
                select(
                    ApplicationAgentRuntimeBinding,
                    ApplicationAgent,
                    ApplicationAgentRun,
                    User,
                )
                .join(
                    ApplicationAgent,
                    ApplicationAgent.id
                    == ApplicationAgentRuntimeBinding.application_agent_id,
                )
                .join(
                    ApplicationAgentRun,
                    ApplicationAgentRun.runtime_binding_id
                    == ApplicationAgentRuntimeBinding.id,
                )
                .outerjoin(
                    User,
                    User.id == ApplicationAgentRun.subject_user_id,
                )
                .where(
                    ApplicationAgent.id == application_agent_id,
                    or_(
                        ApplicationAgent.status == "published",
                        ApplicationAgentRun.channel == "studio",
                    ),
                    ApplicationAgentRuntimeBinding.status == "ready",
                    ApplicationAgentRun.runtime_session_id
                    == self.runtime_session_id,
                    ApplicationAgentRun.status == "running",
                )
            )
        ).first()
        if row is None:
            raise RuntimeKnowledgeError(
                "application_agent_active_user_run_required",
                status_code=403,
            )
        binding, agent, run, user = row
        # Hermes may create several transient turn identifiers while one
        # platform run is active.  The exact Runtime session and running Run
        # establish identity; turn/tool-call identifiers are diagnostic only.
        expected = self.codec.decrypt(str(binding.api_key_encrypted))
        if not hmac.compare_digest(runtime_token, expected):
            raise RuntimeKnowledgeError(
                "runtime_knowledge_unauthorized",
                status_code=401,
            )
        enterprise_enabled = bool(
            (agent.config or {}).get(
                "enterprise_knowledge_enabled",
                False,
            )
        )
        allowed_scopes = frozenset(
            {"sql:context", "sql:query", "documents:search"}
        )
        if run.subject_type == "service":
            if run.application_client_id is None:
                raise RuntimeKnowledgeError(
                    "knowledge_user_delegation_required",
                    status_code=403,
                )
            client_row = (
                await self.session.execute(
                    select(
                        ApplicationClient,
                        ApplicationAgentClientGrant,
                    )
                    .join(
                        ApplicationAgentClientGrant,
                        ApplicationAgentClientGrant.application_client_id
                        == ApplicationClient.id,
                    )
                    .where(
                        ApplicationClient.id == run.application_client_id,
                        ApplicationClient.org_id == int(agent.org_id),
                        ApplicationClient.status == "active",
                        ApplicationAgentClientGrant.application_agent_id
                        == int(agent.id),
                        ApplicationAgentClientGrant.status == "active",
                        ApplicationAgentClientGrant
                        .allow_service_invocation.is_(True),
                    )
                )
            ).first()
            if (
                client_row is None
                or "responses:create"
                not in set(client_row[1].scopes or [])
            ):
                raise RuntimeKnowledgeError(
                    "knowledge_user_delegation_required",
                    status_code=403,
                )
            return RuntimeKnowledgeAccess(
                identity=KnowledgeIdentity(
                    org_id=int(agent.org_id),
                    user_id=0,
                    user_code=f"service:{run.subject_service_id or 'unknown'}",
                    runtime_workspace_id=int(agent.id),
                    application_agent_id=int(agent.id),
                    application_client_id=int(run.application_client_id),
                    runtime_run_id=str(run.runtime_run_id),
                    service_identity=True,
                ),
                enabled=enterprise_enabled,
                allowed_scopes=allowed_scopes,
                allowed_knowledge_base_ids=None,
            )
        if (
            run.subject_type != "user"
            or user is None
            or int(user.org_id) != int(agent.org_id)
            or user.status != "active"
        ):
            raise RuntimeKnowledgeError(
                "knowledge_subject_disabled",
                status_code=403,
            )
        enterprise_knowledge_bases = list(
            (
                await self.session.execute(
                    select(KnowledgeBase).where(
                        KnowledgeBase.org_id == int(agent.org_id),
                        KnowledgeBase.kind.not_in(
                            ("personal", "application")
                        ),
                        KnowledgeBase.status == "active",
                        KnowledgeBase.dataset_id.is_not(None),
                    )
                )
            ).scalars()
        )
        roles = await PersonalKnowledgeAclService(
            self.session
        ).effective_roles(
            principal=Principal(
                org_id=int(user.org_id),
                user_id=int(user.id),
            ),
            knowledge_bases=enterprise_knowledge_bases,
        )
        allowed_knowledge_base_ids = frozenset(
            int(item.id)
            for item in enterprise_knowledge_bases
            if (
                str(item.visibility).lower()
                in {"organization", "org", "public"}
                or int(item.owner_user_id) == int(user.id)
                or int(item.id) in roles
            )
        )
        return RuntimeKnowledgeAccess(
            identity=KnowledgeIdentity(
                org_id=int(user.org_id),
                user_id=int(user.id),
                user_code=str(user.user_code or ""),
                runtime_workspace_id=int(agent.id),
                application_agent_id=int(agent.id),
                application_client_id=(
                    int(run.application_client_id)
                    if run.application_client_id is not None
                    else None
                ),
                runtime_run_id=str(run.runtime_run_id),
            ),
            enabled=enterprise_enabled,
            allowed_scopes=allowed_scopes,
            allowed_knowledge_base_ids=allowed_knowledge_base_ids,
        )



class RuntimeKnowledgeDownstreamClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeKnowledgeError(
                "knowledge_service_unavailable",
                status_code=503,
            ) from exc
        if not isinstance(body, dict):
            raise RuntimeKnowledgeError(
                "knowledge_service_unavailable",
                status_code=503,
            )
        return body

    @staticmethod
    def _safe_downstream_error(response: httpx.Response) -> RuntimeKnowledgeError:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return RuntimeKnowledgeError(
                "knowledge_request_rejected",
                status_code=response.status_code,
            )
        code = error.get("code")
        if not isinstance(code, str) or code not in KNOWN_DOWNSTREAM_ERROR_CODES:
            return RuntimeKnowledgeError(
                "knowledge_request_rejected",
                status_code=response.status_code,
            )
        message = error.get("message")
        hint = error.get("hint")
        retryable = error.get("retryable")
        return RuntimeKnowledgeError(
            code,
            status_code=response.status_code,
            message=message[:500] if isinstance(message, str) else None,
            retryable=retryable if isinstance(retryable, bool) else False,
            hint=hint[:1_000] if isinstance(hint, str) else None,
        )

    async def post_asserted(
        self,
        *,
        base_url: str,
        path: str,
        assertion: str,
        payload: dict[str, Any],
        timeout_seconds: int,
        verify: bool | str = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not base_url:
            raise RuntimeKnowledgeError("knowledge_service_not_configured", status_code=503)
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=timeout_seconds,
            verify=verify,
            trust_env=False,
        )
        started = time.monotonic()
        try:
            request = client.build_request(
                "POST",
                f"{base_url.rstrip('/')}{path}",
                headers={"Authorization": f"Bearer {assertion}"},
                json=payload,
            )
            if request_id:
                request.headers["X-Hermes-Knowledge-Request-Id"] = request_id
            response = await client.send(request)
        except httpx.HTTPError as exc:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=backend_to_data_service path=%s error_type=%s elapsed_ms=%.1f timeout_s=%s",
                request_id or "-",
                path,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
                timeout_seconds,
            )
            raise RuntimeKnowledgeError(
                "knowledge_service_unavailable",
                status_code=503,
                request_id=request_id,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code == 401:
            raise RuntimeKnowledgeError("knowledge_assertion_rejected", status_code=401)
        if response.status_code >= 500:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=backend_to_data_service path=%s status=%s error_type=downstream_5xx elapsed_ms=%.1f",
                request_id or "-",
                path,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            raise RuntimeKnowledgeError(
                "knowledge_service_unavailable",
                status_code=503,
                request_id=request_id,
            )
        if response.status_code >= 400:
            raise self._safe_downstream_error(response)
        try:
            result = self._json(response)
        except RuntimeKnowledgeError as exc:
            exc.request_id = request_id
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=backend_to_data_service path=%s status=%s error_type=invalid_response elapsed_ms=%.1f",
                request_id or "-",
                path,
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            raise
        knowledge_logger.debug(
            "knowledge request completed request_id=%s stage=backend_to_data_service path=%s status=%s elapsed_ms=%.1f",
            request_id or "-",
            path,
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return result

    async def post_network_trusted(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        timeout_seconds: int,
        verify: bool | str = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not url:
            raise RuntimeKnowledgeError("knowledge_service_not_configured", status_code=503)
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=timeout_seconds,
            verify=verify,
            trust_env=False,
        )
        started = time.monotonic()
        try:
            request = client.build_request(
                "POST",
                url,
                json=payload,
            )
            request.headers.pop("Authorization", None)
            if request_id:
                request.headers["X-Hermes-Knowledge-Request-Id"] = request_id
            response = await client.send(request)
        except httpx.HTTPError as exc:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=backend_to_document_service error_type=%s elapsed_ms=%.1f timeout_s=%s",
                request_id or "-",
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
                timeout_seconds,
            )
            raise RuntimeKnowledgeError(
                "knowledge_service_unavailable",
                status_code=503,
                request_id=request_id,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 500:
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=backend_to_document_service status=%s error_type=downstream_5xx elapsed_ms=%.1f",
                request_id or "-",
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            raise RuntimeKnowledgeError(
                "knowledge_service_unavailable",
                status_code=503,
                request_id=request_id,
            )
        if response.status_code >= 400:
            raise RuntimeKnowledgeError(
                "knowledge_request_rejected",
                status_code=response.status_code,
            )
        try:
            result = self._json(response)
        except RuntimeKnowledgeError as exc:
            exc.request_id = request_id
            knowledge_logger.warning(
                "knowledge request failed request_id=%s stage=backend_to_document_service status=%s error_type=invalid_response elapsed_ms=%.1f",
                request_id or "-",
                response.status_code,
                (time.monotonic() - started) * 1000,
            )
            raise
        knowledge_logger.debug(
            "knowledge request completed request_id=%s stage=backend_to_document_service status=%s elapsed_ms=%.1f",
            request_id or "-",
            response.status_code,
            (time.monotonic() - started) * 1000,
        )
        return result


class RuntimeKnowledgeService:
    def __init__(
        self,
        store: RuntimeKnowledgeStore,
        *,
        issuer: KnowledgeAssertionIssuer,
        downstream: RuntimeKnowledgeDownstreamClient | None = None,
        request_id: str | None = None,
    ) -> None:
        self.store = store
        self.issuer = issuer
        self.downstream = downstream or RuntimeKnowledgeDownstreamClient()
        self.request_id = str(request_id or f"knowledge-{uuid4().hex[:16]}")[:128]

    async def state(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
    ) -> dict[str, Any]:
        access = await self.store.authenticate(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
        )
        return {"enabled": access.enabled, "mode": "full"}

    async def _access(
        self,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        scope: str | None = None,
    ) -> RuntimeKnowledgeAccess:
        access = await self.store.authenticate(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
        )
        if not access.enabled:
            raise RuntimeKnowledgeError("enterprise_knowledge_disabled", status_code=403)
        if not access.identity.user_code:
            raise RuntimeKnowledgeError("knowledge_identity_missing", status_code=403)
        if (
            scope is not None
            and access.allowed_scopes is not None
            and scope not in access.allowed_scopes
        ):
            raise RuntimeKnowledgeError(
                "knowledge_scope_not_allowed",
                status_code=403,
            )
        return access

    async def _forward_asserted(
        self,
        *,
        runtime_workspace_id: int,
        runtime_agent_profile_id: int | None,
        runtime_token: str,
        path: str,
        scope: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        access = await self._access(
            runtime_workspace_id,
            runtime_agent_profile_id,
            runtime_token,
            scope,
        )
        verify: bool | str = (
            settings.KNOWLEDGE_DATA_SERVICE_CA_FILE
            or settings.KNOWLEDGE_DATA_SERVICE_VERIFY_TLS
        )
        for attempt in range(2):
            assertion = self.issuer.issue(
                identity=access.identity,
                audience=settings.DATA_SERVICE_ASSERTION_AUDIENCE,
                scope=scope,
                secret=settings.DATA_SERVICE_ASSERTION_SECRET,
            )
            try:
                return await self.downstream.post_asserted(
                    base_url=settings.KNOWLEDGE_DATA_SERVICE_BASE_URL,
                    path=path,
                    assertion=assertion,
                    payload=payload,
                    timeout_seconds=(
                        settings.KNOWLEDGE_SQL_QUERY_TIMEOUT_SECONDS
                        if scope == "sql:query"
                        else settings.KNOWLEDGE_DATA_SERVICE_TIMEOUT_SECONDS
                    ),
                    verify=verify,
                    request_id=self.request_id,
                )
            except RuntimeKnowledgeError as exc:
                if exc.code != "knowledge_assertion_rejected" or attempt == 1:
                    raise
        raise AssertionError("unreachable")

    async def sql_context(
        self,
        workspace_id: int,
        agent_profile_id: int | None,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._forward_asserted(
            runtime_workspace_id=workspace_id,
            runtime_agent_profile_id=agent_profile_id,
            runtime_token=token,
            path="/v1/sql/context",
            scope="sql:context",
            payload=payload,
        )

    async def sql_query(
        self,
        workspace_id: int,
        agent_profile_id: int | None,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._forward_asserted(
            runtime_workspace_id=workspace_id,
            runtime_agent_profile_id=agent_profile_id,
            runtime_token=token,
            path="/v1/sql/query",
            scope="sql:query",
            payload=payload,
        )

    async def sql_query_batch(
        self,
        workspace_id: int,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute multiple SQL queries in batch without requiring session context.

        Uses relaxed authentication (authenticate_for_batch) that only validates
        the API key, not requiring an active application agent run.
        """
        # Use relaxed authentication for batch/service-to-service calls
        if not hasattr(self.store, "authenticate_for_batch"):
            raise RuntimeKnowledgeError(
                "knowledge_batch_not_supported",
                status_code=501,
                message="Batch SQL execution is not supported by the current store.",
            )
        access = await self.store.authenticate_for_batch(workspace_id, token)
        if not access.enabled:
            raise RuntimeKnowledgeError("enterprise_knowledge_disabled", status_code=403)
        if not access.identity.user_code:
            raise RuntimeKnowledgeError("knowledge_identity_missing", status_code=403)

        queries = payload.get("queries", [])
        verify: bool | str = (
            settings.KNOWLEDGE_DATA_SERVICE_CA_FILE
            or settings.KNOWLEDGE_DATA_SERVICE_VERIFY_TLS
        )

        results = []
        for item in queries:
            query_id = item.get("query_id")
            query_payload = {
                "sql": item.get("sql", ""),
                "page": item.get("page", 1),
                "page_size": item.get("page_size", 500),
            }
            try:
                # Sign assertion and call Data Service
                assertion = self.issuer.issue(
                    identity=access.identity,
                    audience=settings.DATA_SERVICE_ASSERTION_AUDIENCE,
                    scope="sql:query",
                    secret=settings.DATA_SERVICE_ASSERTION_SECRET,
                )
                result = await self.downstream.post_asserted(
                    base_url=settings.KNOWLEDGE_DATA_SERVICE_BASE_URL,
                    path="/v1/sql/query",
                    assertion=assertion,
                    payload=query_payload,
                    timeout_seconds=settings.KNOWLEDGE_SQL_QUERY_TIMEOUT_SECONDS,
                    verify=verify,
                    request_id=self.request_id,
                )
                results.append({
                    "query_id": query_id,
                    "ok": True,
                    "data": result,
                })
            except RuntimeKnowledgeError as exc:
                results.append({
                    "query_id": query_id,
                    "ok": False,
                    "error": exc.as_error(),
                })

        return {"results": results}

    async def document_search(
        self,
        workspace_id: int,
        agent_profile_id: int | None,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        access = await self._access(
            workspace_id,
            agent_profile_id,
            token,
            "documents:search",
        )
        tag = payload.get("tag")
        downstream_payload = {
            "question": payload.get("question"),
            "tags": tag.strip() if isinstance(tag, str) else "",
        }
        if access.allowed_knowledge_base_ids is not None:
            downstream_payload["knowledge_base_ids"] = sorted(
                access.allowed_knowledge_base_ids
            )
        return await self.downstream.post_network_trusted(
            url=settings.KNOWLEDGE_DOCUMENT_SEARCH_URL,
            payload=downstream_payload,
            timeout_seconds=settings.KNOWLEDGE_DOCUMENT_SERVICE_TIMEOUT_SECONDS,
        )
