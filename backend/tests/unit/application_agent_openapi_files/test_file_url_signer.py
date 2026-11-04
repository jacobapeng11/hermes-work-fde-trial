from __future__ import annotations

from app.services.file_url_signer import (
    sign_file_download_url,
    verify_file_download_signature,
)


SECRET = "unit-test-openapi-file-signing-secret"


def test_signature_binds_agent_slug_file_and_expiry() -> None:
    url = sign_file_download_url(
        base_url="https://api.example/root/",
        slug="report-agent",
        file_id=42,
        secret=SECRET,
        ttl_seconds=600,
        issued_at=1_700_000_000,
    )

    assert url == (
        "https://api.example/root/openapi/v1/application-agents/report-agent"
        "/files/42/content?expires=1700000600&signature="
        "1c39a464464e04b4da8b2e55740bcb5dd627e8b56d11998df53a78d344ddff1b"
    )
    assert verify_file_download_signature(
        slug="report-agent",
        file_id=42,
        expires=1_700_000_600,
        signature=(
            "1c39a464464e04b4da8b2e55740bcb5dd627e8b56d11998df53a78d344ddff1b"
        ),
        secret=SECRET,
        now=1_700_000_100,
    )


def test_signature_rejects_cross_agent_tampering_and_expiry() -> None:
    signature = (
        "1c39a464464e04b4da8b2e55740bcb5dd627e8b56d11998df53a78d344ddff1b"
    )
    common = {
        "file_id": 42,
        "expires": 1_700_000_600,
        "signature": signature,
        "secret": SECRET,
        "now": 1_700_000_100,
    }

    assert not verify_file_download_signature(slug="other-agent", **common)
    assert not verify_file_download_signature(
        **{**common, "slug": "report-agent", "signature": "0" * 64}
    )
    assert not verify_file_download_signature(
        **{**common, "slug": "report-agent", "secret": "another-secret"}
    )
    assert not verify_file_download_signature(
        **{**common, "slug": "report-agent", "now": 1_700_000_601}
    )
