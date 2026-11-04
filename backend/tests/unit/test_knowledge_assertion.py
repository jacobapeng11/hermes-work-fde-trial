import jwt
import pytest

from app.services.knowledge_assertion import KnowledgeAssertionIssuer, KnowledgeIdentity


def test_assertion_has_required_identity_scope_and_audience() -> None:
    token = KnowledgeAssertionIssuer(issuer="hermes-work", ttl_seconds=60).issue(
        identity=KnowledgeIdentity(org_id=7, user_id=9, user_code="S00123", runtime_workspace_id=123),
        audience="enterprise-data",
        scope="sql:query",
        secret="data-secret",
    )
    claims = jwt.decode(token, "data-secret", algorithms=["HS256"], audience="enterprise-data", issuer="hermes-work")
    assert claims["sub"] == "9"
    assert claims["org_id"] == 7
    assert claims["user_code"] == "S00123"
    assert claims["runtime_workspace_id"] == 123
    assert claims["scope"] == "sql:query"
    assert claims["exp"] - claims["iat"] == 60
    assert claims["nbf"] == claims["iat"]
    assert claims["jti"]


def test_assertion_cannot_cross_audience_or_secret() -> None:
    token = KnowledgeAssertionIssuer(issuer="hermes-work", ttl_seconds=60).issue(
        identity=KnowledgeIdentity(1, 2, "S2", 3),
        audience="enterprise-data",
        scope="sql:context",
        secret="data-secret",
    )
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(token, "data-secret", algorithms=["HS256"], audience="enterprise-documents")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "document-secret", algorithms=["HS256"], audience="enterprise-data")
