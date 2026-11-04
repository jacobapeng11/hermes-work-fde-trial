from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.services.knowledge_assertion import KnowledgeIdentity
from app.services.runtime_knowledge import (
    RuntimeKnowledgeAccess,
    RuntimeKnowledgeDownstreamClient,
    RuntimeKnowledgeError,
    RuntimeKnowledgeService,
    SqlAlchemyApplicationAgentRuntimeKnowledgeStore,
)


class FakeStore:
    def __init__(
        self,
        *,
        enabled=True,
        user_code="S00123",
        allowed_scopes=None,
        allowed_knowledge_base_ids=None,
        service_identity=False,
    ):
        self.access = RuntimeKnowledgeAccess(
            KnowledgeIdentity(
                1,
                2,
                user_code,
                3,
                service_identity=service_identity,
            ),
            enabled,
            allowed_scopes,
            allowed_knowledge_base_ids,
        )

    async def authenticate(
        self,
        runtime_workspace_id,
        runtime_agent_profile_id,
        runtime_token,
    ):
        assert runtime_workspace_id == 3
        assert runtime_agent_profile_id == 11
        assert runtime_token == "runtime-token"
        return self.access


class FakeApplicationRuntimeResult:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row

    def scalars(self):
        return iter(())


class FakeApplicationRuntimeSession:
    def __init__(self, row):
        self.row = row
        self.commits = 0

    async def execute(self, _statement):
        return FakeApplicationRuntimeResult(self.row)

    async def commit(self):
        self.commits += 1


class QueuedApplicationRuntimeSession:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    async def execute(self, _statement):
        return FakeApplicationRuntimeResult(self.rows.pop(0))


class FakeApplicationRuntimeCodec:
    def decrypt(self, _value: str) -> str:
        return "runtime-token"


def application_runtime_auth_row(*, turn_id: str | None):
    return (
        SimpleNamespace(api_key_encrypted="encrypted"),
        SimpleNamespace(
            id=19,
            org_id=1,
            config={
                "enterprise_knowledge_enabled": True,
                "knowledge_bindings": [
                    {
                        "knowledge_base_id": 9,
                        "access_mode": "delegated_user",
                    }
                ],
            },
        ),
        SimpleNamespace(
            turn_id=turn_id,
            subject_type="user",
            subject_user_id=8,
            application_client_id=None,
            runtime_run_id="run-19",
        ),
        SimpleNamespace(
            id=8,
            org_id=1,
            user_code="10002",
            status="active",
        ),
    )


def application_service_auth_row():
    row = list(application_runtime_auth_row(turn_id=None))
    row[2] = SimpleNamespace(
        turn_id=None,
        subject_type="service",
        subject_user_id=None,
        subject_service_id="client-service",
        application_client_id=51,
        runtime_run_id="run-19",
    )
    row[3] = None
    return tuple(row)


class FakeIssuer:
    def __init__(self):
        self.calls = []

    def issue(self, **kwargs):
        self.calls.append(kwargs)
        return f"assertion-{len(self.calls)}"


class RejectingIssuer:
    def issue(self, **_kwargs):
        raise AssertionError("documents must not issue an assertion")


class FakeDownstream:
    def __init__(self, *, asserted=None, network_trusted=None):
        self.asserted = list(asserted or [{"ok": True}])
        self.network_trusted = list(network_trusted or [{"ok": True}])
        self.asserted_calls = []
        self.network_trusted_calls = []

    @staticmethod
    def _next(outcomes):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def post_asserted(self, **kwargs):
        self.asserted_calls.append(kwargs)
        return self._next(self.asserted)

    async def post_network_trusted(self, **kwargs):
        self.network_trusted_calls.append(kwargs)
        return self._next(self.network_trusted)


def service(store, downstream, issuer=None):
    return RuntimeKnowledgeService(
        store,
        issuer=issuer or FakeIssuer(),
        downstream=downstream,
    )


@pytest.mark.parametrize("request_turn_id", [None, "turn-other"])
async def test_application_runtime_auth_uses_session_not_transient_turn(
    request_turn_id,
) -> None:
    row = application_runtime_auth_row(turn_id="turn-persisted")
    session = FakeApplicationRuntimeSession(row)
    subject = SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        turn_id=request_turn_id,
        codec=FakeApplicationRuntimeCodec(),  # type: ignore[arg-type]
    )

    access = await subject.authenticate(19, None, "runtime-token")

    assert access.identity.user_id == 8
    assert access.allowed_scopes == frozenset(
        {"sql:context", "sql:query", "documents:search"}
    )
    assert row[2].turn_id == "turn-persisted"
    assert session.commits == 0


async def test_application_runtime_enterprise_knowledge_uses_enabled_flag(
) -> None:
    row = application_runtime_auth_row(turn_id=None)
    row[1].config["knowledge_bindings"] = []
    session = FakeApplicationRuntimeSession(row)
    subject = SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        codec=FakeApplicationRuntimeCodec(),  # type: ignore[arg-type]
    )

    access = await subject.authenticate(19, None, "runtime-token")

    assert access.enabled is True
    assert access.identity.user_code == "10002"
    assert access.allowed_scopes == frozenset(
        {"sql:context", "sql:query", "documents:search"}
    )
    assert session.commits == 0


async def test_application_service_identity_requires_active_client_grant() -> None:
    session = QueuedApplicationRuntimeSession([
        application_service_auth_row(),
        (
            SimpleNamespace(id=51, status="active"),
            SimpleNamespace(
                scopes=["responses:create"],
                status="active",
                allow_service_invocation=True,
            ),
        ),
    ])
    subject = SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="service-19-session",
        codec=FakeApplicationRuntimeCodec(),  # type: ignore[arg-type]
    )

    access = await subject.authenticate(19, None, "runtime-token")

    assert access.identity.service_identity is True
    assert access.identity.application_client_id == 51
    assert access.allowed_knowledge_base_ids is None
    assert "documents:search" in (access.allowed_scopes or ())


async def test_application_service_identity_fails_closed_without_active_grant() -> None:
    session = QueuedApplicationRuntimeSession([
        application_service_auth_row(),
        None,
    ])
    subject = SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="service-19-session",
        codec=FakeApplicationRuntimeCodec(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeKnowledgeError) as caught:
        await subject.authenticate(19, None, "runtime-token")

    assert caught.value.code == "knowledge_user_delegation_required"


async def test_application_runtime_auth_rejects_missing_active_session_run(
) -> None:
    session = FakeApplicationRuntimeSession(None)
    subject = SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="wrong-or-finished-session",
        codec=FakeApplicationRuntimeCodec(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeKnowledgeError) as exc:
        await subject.authenticate(19, None, "runtime-token")

    assert exc.value.code == "application_agent_active_user_run_required"
    assert exc.value.status_code == 403
    assert session.commits == 0


async def test_application_runtime_auth_rejects_wrong_runtime_token() -> None:
    session = FakeApplicationRuntimeSession(
        application_runtime_auth_row(turn_id=None)
    )
    subject = SqlAlchemyApplicationAgentRuntimeKnowledgeStore(
        session,  # type: ignore[arg-type]
        runtime_session_id="studio-19-8-session",
        codec=FakeApplicationRuntimeCodec(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeKnowledgeError) as exc:
        await subject.authenticate(19, None, "wrong-token")

    assert exc.value.code == "runtime_knowledge_unauthorized"
    assert exc.value.status_code == 401
    assert session.commits == 0


async def test_application_service_identity_searches_documents_without_user_filter() -> None:
    downstream = FakeDownstream()
    subject = service(
        FakeStore(
            allowed_scopes=frozenset({"documents:search"}),
            allowed_knowledge_base_ids=None,
            service_identity=True,
        ),
        downstream,
    )

    response = await subject.document_search(
        3,
        11,
        "runtime-token",
        {"question": "NAP-3825", "tag": "无线产品线"},
    )

    assert response == {"ok": True}
    assert downstream.network_trusted_calls[0]["payload"] == {
        "question": "NAP-3825",
        "tags": "无线产品线",
    }


@pytest.mark.asyncio
async def test_application_knowledge_revoke_is_a_capability_error_not_runtime_failure() -> None:
    downstream = FakeDownstream(
        asserted=[RuntimeKnowledgeError("KNOWLEDGE_BASE_NO_PERMISSION", status_code=403)]
    )
    subject = service(
        FakeStore(allowed_scopes=frozenset({"sql:query"})),
        downstream,
    )

    with pytest.raises(RuntimeKnowledgeError) as exc:
        await subject.sql_query(
            3,
            11,
            "runtime-token",
            {"knowledge_base_id": 9, "query": "工单"},
        )

    assert exc.value.code == "KNOWLEDGE_BASE_NO_PERMISSION"
    assert exc.value.status_code == 403
    assert downstream.network_trusted_calls == []


async def test_disabled_state_makes_zero_downstream_calls() -> None:
    downstream = FakeDownstream()
    subject = service(FakeStore(enabled=False), downstream)

    with pytest.raises(RuntimeKnowledgeError, match="enterprise_knowledge_disabled"):
        await subject.sql_query(3, 11, "runtime-token", {"sql": "SELECT 1"})

    assert downstream.asserted_calls == []
    assert downstream.network_trusted_calls == []


async def test_missing_user_code_is_rejected_before_downstream() -> None:
    downstream = FakeDownstream()
    subject = service(FakeStore(user_code=""), downstream)

    with pytest.raises(RuntimeKnowledgeError, match="knowledge_identity_missing"):
        await subject.document_search(
            3,
            11,
            "runtime-token",
            {"question": "x", "tag": None},
        )

    assert downstream.asserted_calls == []
    assert downstream.network_trusted_calls == []


async def test_state_does_not_require_user_code() -> None:
    result = await service(
        FakeStore(enabled=True, user_code=""),
        FakeDownstream(),
    ).state(3, 11, "runtime-token")

    assert result == {"enabled": True, "mode": "full"}


async def test_sql_query_uses_assertion_and_retries_one_rejected_assertion() -> None:
    downstream = FakeDownstream(
        asserted=[
            RuntimeKnowledgeError("knowledge_assertion_rejected", status_code=401),
            {"rows": [{"value": 1}]},
        ]
    )
    issuer = FakeIssuer()
    subject = service(FakeStore(), downstream, issuer)

    result = await subject.sql_query(
        3,
        11,
        "runtime-token",
        {"sql": "SELECT 1"},
    )

    assert result == {"rows": [{"value": 1}]}
    assert len(issuer.calls) == 2
    assert issuer.calls[0]["audience"] == settings.DATA_SERVICE_ASSERTION_AUDIENCE
    assert issuer.calls[0]["scope"] == "sql:query"
    assert [call["assertion"] for call in downstream.asserted_calls] == [
        "assertion-1",
        "assertion-2",
    ]
    assert downstream.network_trusted_calls == []


async def test_sql_query_stops_after_second_rejected_assertion() -> None:
    downstream = FakeDownstream(
        asserted=[
            RuntimeKnowledgeError("knowledge_assertion_rejected", status_code=401),
            RuntimeKnowledgeError("knowledge_assertion_rejected", status_code=401),
        ]
    )
    issuer = FakeIssuer()
    subject = service(FakeStore(), downstream, issuer)

    with pytest.raises(RuntimeKnowledgeError) as exc:
        await subject.sql_query(3, 11, "runtime-token", {"sql": "SELECT 1"})

    assert exc.value.code == "knowledge_assertion_rejected"
    assert len(issuer.calls) == 2
    assert len(downstream.asserted_calls) == 2


async def test_document_search_uses_network_trust_without_assertion() -> None:
    downstream = FakeDownstream(network_trusted=[{"matches": ["document"]}])
    subject = service(FakeStore(), downstream, RejectingIssuer())
    payload = {"question": "园区网", "tag": " 无线产品线 "}

    result = await subject.document_search(3, 11, "runtime-token", payload)

    assert result == {"matches": ["document"]}
    assert downstream.asserted_calls == []
    assert downstream.network_trusted_calls == [
        {
            "url": settings.KNOWLEDGE_DOCUMENT_SEARCH_URL,
            "payload": {"question": "园区网", "tags": "无线产品线"},
            "timeout_seconds": settings.KNOWLEDGE_DOCUMENT_SERVICE_TIMEOUT_SECONDS,
        }
    ]


@pytest.mark.parametrize("tag", [None, "", "   "])
async def test_document_search_maps_empty_tag_to_all_documents(tag) -> None:
    downstream = FakeDownstream(network_trusted=[{"results": []}])
    subject = service(FakeStore(), downstream, RejectingIssuer())

    await subject.document_search(
        3,
        11,
        "runtime-token",
        {"question": "园区网", "tag": tag},
    )

    assert downstream.network_trusted_calls[0]["payload"] == {
        "question": "园区网",
        "tags": "",
    }


async def test_document_search_does_not_retry_rejected_request() -> None:
    downstream = FakeDownstream(
        network_trusted=[
            RuntimeKnowledgeError("knowledge_request_rejected", status_code=403),
            {"unexpected": "fallback"},
        ]
    )
    subject = service(FakeStore(), downstream, RejectingIssuer())

    with pytest.raises(RuntimeKnowledgeError) as exc:
        await subject.document_search(
            3,
            11,
            "runtime-token",
            {"question": "园区网", "tag": None},
        )

    assert exc.value.code == "knowledge_request_rejected"
    assert exc.value.status_code == 403
    assert len(downstream.network_trusted_calls) == 1


async def test_asserted_client_sends_authorization() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["request_id"] = request.headers.get(
            "X-Hermes-Knowledge-Request-Id"
        )
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = RuntimeKnowledgeDownstreamClient(http)
    try:
        result = await subject.post_asserted(
            base_url="https://data.example",
            path="/v1/sql/query",
            assertion="short-lived",
            payload={"sql": "SELECT 1"},
            timeout_seconds=30,
            request_id="tool-9",
        )
    finally:
        await http.aclose()

    assert result == {"ok": True}
    assert captured["authorization"] == "Bearer short-lived"
    assert captured["request_id"] == "tool-9"


async def test_sql_query_uses_sql_specific_downstream_timeout(monkeypatch) -> None:
    downstream = FakeDownstream()
    subject = service(FakeStore(), downstream)

    await subject.sql_query(3, 11, "runtime-token", {"sql": "SELECT 1"})

    assert downstream.asserted_calls[0]["timeout_seconds"] == settings.KNOWLEDGE_SQL_QUERY_TIMEOUT_SECONDS


async def test_asserted_client_preserves_known_safe_sql_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "sql_subquery_not_supported",
                    "message": "当前权限网关不支持子查询。",
                    "retryable": True,
                    "hint": "改为单层 SELECT。",
                    "details": [{"must_not_escape": "raw downstream detail"}],
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = RuntimeKnowledgeDownstreamClient(http)
    try:
        with pytest.raises(RuntimeKnowledgeError) as raised:
            await subject.post_asserted(
                base_url="https://data.example",
                path="/v1/sql/query",
                assertion="short-lived",
                payload={"sql": "SELECT 1"},
                timeout_seconds=30,
            )
    finally:
        await http.aclose()

    assert raised.value.as_error() == {
        "code": "sql_subquery_not_supported",
        "message": "当前权限网关不支持子查询。",
        "retryable": True,
        "hint": "改为单层 SELECT。",
    }


async def test_asserted_client_safely_downgrades_unknown_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "database_driver_failed",
                    "message": "sensitive database stack",
                    "retryable": True,
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = RuntimeKnowledgeDownstreamClient(http)
    try:
        with pytest.raises(RuntimeKnowledgeError) as raised:
            await subject.post_asserted(
                base_url="https://data.example",
                path="/v1/sql/query",
                assertion="short-lived",
                payload={"sql": "SELECT 1"},
                timeout_seconds=30,
            )
    finally:
        await http.aclose()

    assert raised.value.code == "knowledge_request_rejected"
    assert "sensitive" not in raised.value.message
    assert raised.value.retryable is False


async def test_network_trusted_client_removes_default_authorization() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["has_authorization"] = "Authorization" in request.headers
        return httpx.Response(200, json={"matches": []})

    http = httpx.AsyncClient(
        headers={"Authorization": "Bearer must-not-leak"},
        transport=httpx.MockTransport(handler),
    )
    subject = RuntimeKnowledgeDownstreamClient(http)
    try:
        result = await subject.post_network_trusted(
            url="https://documents.example/sf/api/search/agent-tag-search",
            payload={"question": "园区网"},
            timeout_seconds=30,
        )
    finally:
        await http.aclose()

    assert result == {"matches": []}
    assert captured["has_authorization"] is False


@pytest.mark.parametrize("status_code", [401, 403])
async def test_network_trusted_client_maps_4xx_without_retry(
    status_code: int,
) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status_code, json={"error": "rejected"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = RuntimeKnowledgeDownstreamClient(http)
    try:
        with pytest.raises(RuntimeKnowledgeError) as exc:
            await subject.post_network_trusted(
                url="https://documents.example/sf/api/search/agent-tag-search",
                payload={"question": "园区网"},
                timeout_seconds=30,
            )
    finally:
        await http.aclose()

    assert exc.value.code == "knowledge_request_rejected"
    assert exc.value.status_code == status_code
    assert requests == 1
