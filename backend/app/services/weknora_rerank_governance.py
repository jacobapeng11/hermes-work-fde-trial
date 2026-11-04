from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

WEKNORA_RERANK_CONFIG_UNAVAILABLE = "weknora_rerank_config_unavailable"
WEKNORA_RERANK_MODEL_INVALID = "weknora_rerank_model_invalid"


class WeKnoraRerankGovernanceError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class WeKnoraRerankModel:
    id: str
    name: str
    display_name: str
    source: str
    status: str


@dataclass(frozen=True)
class WeKnoraRerankConfigSnapshot:
    configured_model_id: str
    effective_model_id: str
    config_source: str
    models: tuple[WeKnoraRerankModel, ...]


def _unavailable(
    message: str = "知识库底座 Rerank 配置暂不可用",
    *,
    status_code: int = 502,
) -> WeKnoraRerankGovernanceError:
    return WeKnoraRerankGovernanceError(
        code=WEKNORA_RERANK_CONFIG_UNAVAILABLE,
        message=message,
        status_code=status_code,
    )


def _invalid_model() -> WeKnoraRerankGovernanceError:
    return WeKnoraRerankGovernanceError(
        code=WEKNORA_RERANK_MODEL_INVALID,
        message="请选择一个可用的 WeKnora Rerank 模型",
        status_code=409,
    )


class WeKnoraRerankGovernanceService:
    """Read and update the workspace-level WeKnora rerank default."""

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_base_url = (
            api_base_url or settings.WEKNORA_API_BASE_URL
        ).strip().rstrip("/")
        self.api_key = (api_key or settings.WEKNORA_API_KEY).strip()
        self.timeout_seconds = timeout_seconds or settings.WEKNORA_TIMEOUT_SECONDS
        self.client = client

    async def _request(
        self,
        action: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.api_base_url or not self.api_key:
            raise _unavailable(
                "知识库底座 Rerank 配置未启用",
                status_code=503,
            )

        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        try:
            request = client.build_request(
                method,
                f"{self.api_base_url}{path}",
                json=payload,
                headers={
                    "X-API-Key": self.api_key,
                    "Content-Type": "application/json",
                },
            )
            request.headers.pop("Authorization", None)
            response = await client.send(request)
        except httpx.HTTPError as exc:
            logger.warning(
                "weknora rerank governance %s http failure: %s",
                action,
                type(exc).__name__,
            )
            raise _unavailable() from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code // 100 != 2:
            logger.warning(
                "weknora rerank governance %s rejected status=%s",
                action,
                response.status_code,
            )
            raise _unavailable() from None
        try:
            body = response.json()
        except ValueError as exc:
            logger.warning(
                "weknora rerank governance %s returned non-json",
                action,
            )
            raise _unavailable() from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            logger.warning(
                "weknora rerank governance %s returned error payload",
                action,
            )
            raise _unavailable() from None
        return body

    async def _list_models(self) -> tuple[WeKnoraRerankModel, ...]:
        body = await self._request("list_models", "GET", "/models")
        data = body.get("data")
        if not isinstance(data, list):
            logger.warning("weknora rerank governance list models returned invalid data")
            raise _unavailable() from None

        models: list[WeKnoraRerankModel] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            model_type = str(item.get("type") or "").strip()
            status = str(item.get("status") or "").strip()
            if not model_id or not name or model_type != "Rerank" or status != "active":
                continue
            models.append(
                WeKnoraRerankModel(
                    id=model_id,
                    name=name,
                    display_name=str(item.get("display_name") or name).strip() or name,
                    source=str(item.get("source") or "").strip(),
                    status=status,
                )
            )
        return tuple(
            sorted(models, key=lambda model: (model.display_name, model.name, model.id))
        )

    async def _get_retrieval_config(self) -> dict[str, Any]:
        body = await self._request(
            "get_retrieval_config",
            "GET",
            "/tenants/kv/retrieval-config",
        )
        data = body.get("data")
        if not isinstance(data, dict):
            logger.warning("weknora rerank governance get config returned invalid data")
            raise _unavailable() from None
        return data

    @staticmethod
    def _snapshot(
        *,
        config: dict[str, Any],
        models: tuple[WeKnoraRerankModel, ...],
    ) -> WeKnoraRerankConfigSnapshot:
        configured_model_id = str(config.get("rerank_model_id") or "").strip()
        if configured_model_id:
            effective_model_id = configured_model_id
            config_source = "tenant"
        elif models:
            effective_model_id = models[0].id
            config_source = "auto"
        else:
            effective_model_id = ""
            config_source = "none"
        return WeKnoraRerankConfigSnapshot(
            configured_model_id=configured_model_id,
            effective_model_id=effective_model_id,
            config_source=config_source,
            models=models,
        )

    async def get_config(self) -> WeKnoraRerankConfigSnapshot:
        models = await self._list_models()
        config = await self._get_retrieval_config()
        return self._snapshot(config=config, models=models)

    async def update_config(self, *, rerank_model_id: str) -> WeKnoraRerankConfigSnapshot:
        normalized_model_id = rerank_model_id.strip()
        models = await self._list_models()
        if not any(model.id == normalized_model_id for model in models):
            raise _invalid_model()

        config = await self._get_retrieval_config()
        next_config = dict(config)
        next_config["rerank_model_id"] = normalized_model_id
        body = await self._request(
            "update_retrieval_config",
            "PUT",
            "/tenants/kv/retrieval-config",
            payload=next_config,
        )
        updated_config = body.get("data")
        if not isinstance(updated_config, dict):
            logger.warning("weknora rerank governance update config returned invalid data")
            raise _unavailable() from None
        return self._snapshot(config=updated_config, models=models)
