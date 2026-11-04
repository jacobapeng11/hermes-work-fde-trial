from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


class PersonalKnowledgeProviderError(RuntimeError):
    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class WeKnoraKnowledgeDocument:
    document_id: str
    parse_status: str


def normalize_wiki_slug(slug: str) -> str:
    value = slug.strip()
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise PersonalKnowledgeProviderError(
            "read_wiki_page_failed",
            status_code=400,
        )
    return value


class PersonalKnowledgeLifecycleProvider(Protocol):
    async def create_dataset(self, name: str) -> str:
        ...

    async def delete_dataset(self, dataset_id: str) -> None:
        ...


class FilebasePersonalKnowledgeClient:
    def __init__(
        self,
        *,
        sync_base_url: str | None = None,
        search_url: str | None = None,
        timeout_seconds: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.sync_base_url = (
            sync_base_url or settings.PERSONAL_KNOWLEDGE_SYNC_BASE_URL
        ).rstrip("/")
        self.search_url = search_url or settings.PERSONAL_KNOWLEDGE_SEARCH_URL
        self.timeout_seconds = (
            timeout_seconds or settings.PERSONAL_KNOWLEDGE_TIMEOUT_SECONDS
        )
        self.client = client

    async def _request(
        self,
        action: str,
        method: str,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        try:
            request = client.build_request(
                method,
                url,
                json=payload,
                headers=headers,
            )
            request.headers.pop("Authorization", None)
            response = await client.send(request)
        except httpx.HTTPError as exc:
            logger.warning("personal knowledge provider %s http failure: %s", action, type(exc).__name__)
            raise PersonalKnowledgeProviderError(f"{action}_failed") from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code // 100 != 2:
            logger.warning(
                "personal knowledge provider %s rejected status=%s",
                action,
                response.status_code,
            )
            raise PersonalKnowledgeProviderError(f"{action}_failed")
        try:
            body = response.json()
        except ValueError as exc:
            logger.warning("personal knowledge provider %s returned non-json", action)
            raise PersonalKnowledgeProviderError(f"{action}_failed") from exc
        if not isinstance(body, dict) or body.get("error"):
            logger.warning("personal knowledge provider %s returned error payload", action)
            raise PersonalKnowledgeProviderError(f"{action}_failed")
        return body

    async def create_dataset(self, name: str) -> str:
        body = await self._request(
            "create_dataset",
            "POST",
            f"{self.sync_base_url}/create-dataset",
            payload={"name": name},
            headers={"Content-Type": "application/json"},
        )
        dataset_id = str(body.get("dataset_id") or "").strip()
        if not dataset_id:
            raise PersonalKnowledgeProviderError("create_dataset_failed")
        return dataset_id

    async def delete_dataset(self, dataset_id: str) -> None:
        if not dataset_id:
            return
        body = await self._request(
            "delete_dataset",
            "DELETE",
            f"{self.sync_base_url}/delete-dataset",
            payload={"ids": [dataset_id]},
            headers={"Content-Type": "application/json"},
        )
        if int(body.get("ok") or 0) < 1:
            raise PersonalKnowledgeProviderError("delete_dataset_failed")

    async def search(
        self,
        *,
        question: str,
        dataset_ids: list[str],
    ) -> dict[str, Any]:
        return await self._request(
            "search",
            "POST",
            self.search_url,
            payload={"question": question, "dataset_ids": dataset_ids},
            headers={"Content-Type": "application/json"},
        )


class WeKnoraPersonalKnowledgeClient:
    """Lifecycle adapter for the WeKnora personal knowledge provider."""

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        api_key: str | None = None,
        embedding_model_id: str | None = None,
        summary_model_id: str | None = None,
        wiki_synthesis_model_id: str | None = None,
        wiki_extraction_granularity: str | None = None,
        timeout_seconds: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base_url = (
            api_base_url or settings.WEKNORA_API_BASE_URL
        ).strip().rstrip("/")
        self.api_key = (api_key or settings.WEKNORA_API_KEY).strip()
        self.embedding_model_id = (
            embedding_model_id or settings.WEKNORA_EMBEDDING_MODEL_ID
        ).strip()
        self.summary_model_id = (
            summary_model_id or settings.WEKNORA_SUMMARY_MODEL_ID
        ).strip()
        self.wiki_synthesis_model_id = (
            wiki_synthesis_model_id or settings.WEKNORA_WIKI_SYNTHESIS_MODEL_ID
        ).strip()
        self.wiki_extraction_granularity = (
            wiki_extraction_granularity
            or settings.WEKNORA_WIKI_EXTRACTION_GRANULARITY
        ).strip()
        self.timeout_seconds = (
            timeout_seconds or settings.WEKNORA_TIMEOUT_SECONDS
        )
        self.client = client
        if not self.api_base_url or not self.api_key:
            raise PersonalKnowledgeProviderError(
                "weknora_provider_not_configured"
            )

    async def _request(
        self,
        action: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, Any] | None = None,
        require_success: bool = True,
    ) -> dict[str, Any]:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        try:
            headers = {"X-API-Key": self.api_key}
            if files is None:
                headers["Content-Type"] = "application/json"
            request = client.build_request(
                method,
                f"{self.api_base_url}{path}",
                json=payload if files is None else None,
                data=data,
                files=files,
                headers=headers,
            )
            request.headers.pop("Authorization", None)
            response = await client.send(request)
        except httpx.HTTPError as exc:
            logger.warning(
                "personal knowledge provider %s http failure: %s",
                action,
                type(exc).__name__,
            )
            raise PersonalKnowledgeProviderError(f"{action}_failed") from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code // 100 != 2:
            logger.warning(
                "personal knowledge provider %s rejected status=%s",
                action,
                response.status_code,
            )
            raise PersonalKnowledgeProviderError(
                f"{action}_failed", status_code=response.status_code
            )
        try:
            body = response.json()
        except ValueError as exc:
            logger.warning(
                "personal knowledge provider %s returned non-json",
                action,
            )
            raise PersonalKnowledgeProviderError(f"{action}_failed") from exc
        if not isinstance(body, dict) or (
            require_success and body.get("success") is not True
        ):
            logger.warning(
                "personal knowledge provider %s returned error payload",
                action,
            )
            raise PersonalKnowledgeProviderError(f"{action}_failed")
        return body

    async def create_dataset(self, name: str) -> str:
        if not self.embedding_model_id:
            raise PersonalKnowledgeProviderError(
                "weknora_embedding_model_not_configured"
            )
        if not self.summary_model_id:
            raise PersonalKnowledgeProviderError(
                "weknora_summary_model_not_configured"
            )
        if not self.wiki_synthesis_model_id:
            raise PersonalKnowledgeProviderError(
                "weknora_wiki_synthesis_model_not_configured"
            )
        body = await self._request(
            "create_dataset",
            "POST",
            "/knowledge-bases",
            payload={
                "name": name,
                "type": "document",
                "embedding_model_id": self.embedding_model_id,
                "summary_model_id": self.summary_model_id,
                "indexing_strategy": {
                    "vector_enabled": True,
                    "keyword_enabled": True,
                    "wiki_enabled": True,
                    "graph_enabled": False,
                },
                "wiki_config": {
                    "synthesis_model_id": self.wiki_synthesis_model_id,
                    "extraction_granularity": self.wiki_extraction_granularity,
                },
            },
        )
        data = body.get("data")
        dataset_id = (
            str(data.get("id") or "").strip()
            if isinstance(data, dict)
            else ""
        )
        if not dataset_id:
            raise PersonalKnowledgeProviderError("create_dataset_failed")
        return dataset_id

    async def delete_dataset(self, dataset_id: str) -> None:
        if not dataset_id:
            raise PersonalKnowledgeProviderError("delete_dataset_failed")
        await self._request(
            "delete_dataset",
            "DELETE",
            f"/knowledge-bases/{quote(dataset_id, safe='')}",
        )

    async def search(
        self,
        *,
        question: str,
        dataset_ids: list[str],
        match_count: int = 10,
        rerank_enabled: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": question,
            "knowledge_base_ids": dataset_ids,
            "match_count": match_count,
        }
        if not rerank_enabled:
            payload["rerank"] = {"enabled": False}
        body = await self._request(
            "search",
            "POST",
            "/knowledge-search",
            payload=payload,
        )
        if not isinstance(body.get("data"), list):
            logger.warning("personal knowledge provider search returned invalid data")
            raise PersonalKnowledgeProviderError("search_failed")
        return body

    async def search_wiki(
        self,
        *,
        query: str,
        dataset_ids: list[str],
        limit: int = 10,
    ) -> dict[str, Any]:
        body = await self._request(
            "wiki_search",
            "POST",
            "/wiki-search",
            payload={
                "query": query,
                "knowledge_base_ids": dataset_ids,
                "limit": limit,
            },
        )
        if not isinstance(body.get("data"), list):
            logger.warning(
                "personal knowledge provider wiki_search returned invalid data"
            )
            raise PersonalKnowledgeProviderError("wiki_search_failed")
        return body

    async def read_wiki_page(
        self,
        *,
        dataset_id: str,
        slug: str,
    ) -> dict[str, Any]:
        normalized_slug = normalize_wiki_slug(slug)
        encoded_slug = "/".join(
            quote(part, safe="") for part in normalized_slug.split("/")
        )
        body = await self._request(
            "read_wiki_page",
            "GET",
            (
                f"/knowledgebase/{quote(dataset_id, safe='')}"
                f"/wiki/pages/{encoded_slug}"
            ),
            require_success=False,
        )
        if body.get("success") is False:
            logger.warning(
                "personal knowledge provider read_wiki_page returned error payload"
            )
            raise PersonalKnowledgeProviderError("read_wiki_page_failed")
        data = body.get("data") if body.get("success") is True else body
        if not isinstance(data, dict):
            logger.warning(
                "personal knowledge provider read_wiki_page returned invalid data"
            )
            raise PersonalKnowledgeProviderError("read_wiki_page_failed")
        return {"data": data}

    async def list_datasets(self) -> list[dict[str, Any]]:
        body = await self._request(
            "list_datasets",
            "GET",
            "/knowledge-bases",
        )
        data = body.get("data")
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            logger.warning(
                "personal knowledge provider list_datasets returned invalid data"
            )
            raise PersonalKnowledgeProviderError("list_datasets_failed")
        return data

    async def upload_document(
        self,
        *,
        dataset_id: str,
        filename: str,
        content_type: str | None,
        content: bytes,
    ) -> WeKnoraKnowledgeDocument:
        body = await self._request(
            "upload_document",
            "POST",
            f"/knowledge-bases/{quote(dataset_id, safe='')}/knowledge/file",
            data={"fileName": filename, "channel": "hermes"},
            files={
                "file": (
                    filename,
                    content,
                    content_type or "application/octet-stream",
                )
            },
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise PersonalKnowledgeProviderError("upload_document_failed")
        document_id = str(data.get("id") or "").strip()
        parse_status = str(data.get("parse_status") or "").strip()
        if not document_id or not parse_status:
            raise PersonalKnowledgeProviderError("upload_document_failed")
        return WeKnoraKnowledgeDocument(
            document_id=document_id,
            parse_status=parse_status,
        )

    async def get_document(self, document_id: str) -> WeKnoraKnowledgeDocument:
        body = await self._request(
            "get_document",
            "GET",
            f"/knowledge/{quote(document_id, safe='')}",
        )
        data = body.get("data")
        if not isinstance(data, dict):
            raise PersonalKnowledgeProviderError("get_document_failed")
        returned_id = str(data.get("id") or "").strip()
        parse_status = str(data.get("parse_status") or "").strip()
        if returned_id != document_id or not parse_status:
            raise PersonalKnowledgeProviderError("get_document_failed")
        return WeKnoraKnowledgeDocument(
            document_id=returned_id,
            parse_status=parse_status,
        )

    async def delete_document(self, document_id: str) -> str | None:
        body = await self._request(
            "delete_document",
            "DELETE",
            f"/knowledge/{quote(document_id, safe='')}",
        )
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        task_id = str(data.get("task_id") or "").strip()
        return task_id or None

    @asynccontextmanager
    async def stream_document(
        self,
        document_id: str,
        *,
        download: bool,
    ) -> AsyncIterator[Any]:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        action = "download_document" if download else "preview_document"
        suffix = "download" if download else "preview"
        response: httpx.Response | None = None
        try:
            request = client.build_request(
                "GET",
                f"{self.api_base_url}/knowledge/{quote(document_id, safe='')}/{suffix}",
                headers={"X-API-Key": self.api_key},
            )
            request.headers.pop("Authorization", None)
            try:
                response = await client.send(request, stream=True)
            except httpx.HTTPError as exc:
                logger.warning(
                    "personal knowledge provider %s http failure: %s",
                    action,
                    type(exc).__name__,
                )
                raise PersonalKnowledgeProviderError(f"{action}_failed") from exc
            if response.status_code != 200:
                await response.aread()
                logger.warning(
                    "personal knowledge provider %s rejected status=%s",
                    action,
                    response.status_code,
                )
                raise PersonalKnowledgeProviderError(
                    f"{action}_failed", status_code=response.status_code
                )
            yield response
        finally:
            if response is not None:
                await response.aclose()
            if owns_client:
                await client.aclose()


def build_personal_knowledge_provider(
    provider_name: str,
) -> PersonalKnowledgeLifecycleProvider:
    if provider_name == "filebase":
        return FilebasePersonalKnowledgeClient()
    if provider_name == "weknora":
        return WeKnoraPersonalKnowledgeClient()
    raise PersonalKnowledgeProviderError(
        "personal_knowledge_provider_unsupported"
    )
