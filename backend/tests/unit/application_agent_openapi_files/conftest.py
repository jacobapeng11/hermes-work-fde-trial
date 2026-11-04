from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models import (
    ApplicationAgent,
    ApplicationAgentClientGrant,
    ApplicationAgentConversation,
    ApplicationAgentConversationFileArtifact,
    ApplicationAgentRuntimeBinding,
    ApplicationAgentRun,
    ApplicationClient,
    Base,
)


ORG_ID = 1
USER_ID = 990101
AGENT_ID = 101
CLIENT_ID = 201
SECOND_CLIENT_ID = 202
BINDING_ID = 301
CONVERSATION_ID = 401
RUN_ID = 501
RUNTIME_SESSION_ID = "app-session-1"
RUNTIME_ID = "application-agent-runtime-101"
CLIENT_SECRET = "client-secret"
SECOND_CLIENT_SECRET = "second-client-secret"


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_integer(
    _type: BigInteger,
    _compiler: object,
    **_kwargs: object,
) -> str:
    return "INTEGER"


def external_subject_key(client_id: str, external_user_id: str) -> str:
    digest = hashlib.sha256(
        f"{client_id}\0{external_user_id}".encode("utf-8")
    ).hexdigest()
    return f"external_user:{digest}"


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                ApplicationAgent.__table__,
                ApplicationClient.__table__,
                ApplicationAgentClientGrant.__table__,
                ApplicationAgentRuntimeBinding.__table__,
                ApplicationAgentConversation.__table__,
                ApplicationAgentRun.__table__,
                ApplicationAgentConversationFileArtifact.__table__,
            ],
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    session = session_factory()
    try:
        session.add(
            ApplicationAgent(
                id=AGENT_ID,
                org_id=ORG_ID,
                slug="report-agent",
                name="报表 Agent",
                status="published",
                created_by_user_id=USER_ID,
                updated_by_user_id=USER_ID,
            )
        )
        for client_id, secret in (
            (CLIENT_ID, CLIENT_SECRET),
            (SECOND_CLIENT_ID, SECOND_CLIENT_SECRET),
        ):
            session.add(
                ApplicationClient(
                    id=client_id,
                    org_id=ORG_ID,
                    client_id=f"client-{client_id}",
                    name=f"Client {client_id}",
                    credential_digest_or_reference=hashlib.sha256(
                        secret.encode("utf-8")
                    ).hexdigest(),
                    created_by_user_id=USER_ID,
                )
            )
            session.add(
                ApplicationAgentClientGrant(
                    org_id=ORG_ID,
                    application_agent_id=AGENT_ID,
                    application_client_id=client_id,
                    scopes=["responses:create", "responses:files:read"],
                    allow_service_invocation=True,
                )
            )
        session.add(
            ApplicationAgentRuntimeBinding(
                id=BINDING_ID,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                runtime_id=RUNTIME_ID,
                k8s_namespace="runtimes",
                knative_service=RUNTIME_ID,
                runtime_root_pvc="runtime-101",
                runtime_root_host_path="/data/hermes/runtimes/1/101",
                config_generation=7,
                image="hermes-runtime:test",
                endpoint_base_url="http://runtime.invalid",
                endpoint_host_header="runtime.invalid",
                api_key_encrypted="runtime-key",
                status="ready",
            )
        )
        for conversation_id, external_user_id in (
            (CONVERSATION_ID, "ext-1"),
            (402, "ext-2"),
        ):
            session.add(
                ApplicationAgentConversation(
                    id=conversation_id,
                    org_id=ORG_ID,
                    application_agent_id=AGENT_ID,
                    application_client_id=CLIENT_ID,
                    subject_type="external_user",
                    subject_service_id=external_user_id,
                    subject_key=external_subject_key(
                        f"client-{CLIENT_ID}", external_user_id
                    ),
                    conversation_key="default",
                    runtime_session_id=RUNTIME_SESSION_ID
                    if conversation_id == CONVERSATION_ID
                    else "app-session-2",
                )
            )
        session.add(
            ApplicationAgentRun(
                id=RUN_ID,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                runtime_binding_id=BINDING_ID,
                application_client_id=CLIENT_ID,
                conversation_id=CONVERSATION_ID,
                runtime_session_id=RUNTIME_SESSION_ID,
                runtime_run_id="run-1",
                subject_type="external_user",
                subject_service_id="ext-1",
                channel="openapi",
                status="running",
                request_id="request-1",
            )
        )
        session.add(
            ApplicationAgentRun(
                id=502,
                org_id=ORG_ID,
                application_agent_id=AGENT_ID,
                runtime_binding_id=BINDING_ID,
                application_client_id=CLIENT_ID,
                conversation_id=402,
                runtime_session_id="app-session-2",
                runtime_run_id="run-2",
                subject_type="external_user",
                subject_service_id="ext-2",
                channel="openapi",
                status="running",
                request_id="request-2",
            )
        )
        await session.commit()
        yield session
    finally:
        await session.close()
        await engine.dispose()
