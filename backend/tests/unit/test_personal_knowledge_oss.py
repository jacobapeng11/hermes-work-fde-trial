import re

import httpx
import pytest

from app.services.personal_knowledge_oss import (
    PersonalKnowledgeOssClient,
    PersonalKnowledgeOssError,
)


async def test_upload_preserves_bucket_folder_and_url_rewrite() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        captured["method"] = request.method
        captured["content_type"] = request.headers["Content-Type"]
        captured["body"] = body
        return httpx.Response(
            200,
            json={"url": "http://storage.example.com/hermes-portal/knowledge/a.pdf"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    subject = PersonalKnowledgeOssClient(
        client=http,
        upload_url="https://storage.example.com/upload",
        public_base_url="https://assets.example.com/static",
        internal_host="storage.example.com",
    )
    try:
        result = await subject.upload_bytes(
            content=b"pdf-content",
            original_filename="../a.pdf",
            content_type="application/pdf",
        )
    finally:
        await http.aclose()

    body = captured["body"]
    assert captured["method"] == "POST"
    assert "multipart/form-data" in captured["content_type"]
    assert b'name="bucketName"' in body and b"hermes-portal" in body
    assert b'name="folderName"' in body
    assert re.search(rb"knowledge/\d{8}/", body)
    assert b'name="fileName"' in body
    assert b'filename="a_' in body
    assert result.url == "https://assets.example.com/static/hermes-portal/knowledge/a.pdf"
    assert re.fullmatch(r"knowledge/\d{8}/a_\d{14}_[0-9a-f]{6}\.pdf", result.key)


async def test_upload_rejects_failed_or_empty_oss_response() -> None:
    for response in (
        httpx.Response(500, text="failed"),
        httpx.Response(200, json={}),
    ):
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request, value=response: value)
        )
        subject = PersonalKnowledgeOssClient(
            client=http, upload_url="https://storage.example.com/upload"
        )
        try:
            with pytest.raises(PersonalKnowledgeOssError, match="oss_upload_failed"):
                await subject.upload_bytes(
                    content=b"x",
                    original_filename="a.txt",
                    content_type="text/plain",
                )
        finally:
            await http.aclose()


async def test_upload_without_customer_oss_configuration_fails_cleanly() -> None:
    subject = PersonalKnowledgeOssClient(upload_url="")
    with pytest.raises(PersonalKnowledgeOssError, match="oss_upload_failed"):
        await subject.upload_bytes(content=b"x", original_filename="a.txt")
