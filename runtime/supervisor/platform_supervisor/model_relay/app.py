from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from platform_supervisor.model_relay.relay import (
    ModelRelay,
    ModelRelayBoundaryRedirected,
    ModelRelayBoundaryReplay,
    ModelRelayBoundaryUncertain,
    ModelRelayCapacityExhausted,
    ModelRelayError,
    ModelRelayRequestRejected,
    ModelRouteRegistry,
)


def create_model_relay_app(
    relay: ModelRelay,
    registry: ModelRouteRegistry,
) -> FastAPI:
    app = FastAPI(
        title="Runtime-local Model Relay",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/model-relay/{route_token}/v1/models")
    async def list_models(route_token: str) -> dict[str, object]:
        try:
            model_id = await registry.model_listing(route_token)
        except ModelRelayRequestRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        return {
            "object": "list",
            "data": [
                {
                    # Dashboard validates a custom provider's configured
                    # model through this endpoint before accepting
                    # ``config.set``.  It must see the same upstream-facing
                    # name Hermes sends to the Relay, never the platform
                    # ModelEntry id retained only for authorization/audit.
                    "id": model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "hermes-platform",
                }
            ],
        }

    @app.post("/model-relay/{route_token}/v1/{operation:path}")
    async def forward(route_token: str, operation: str, request: Request):
        body = await request.body()
        try:
            response = await relay.forward(
                route_token=route_token,
                operation=operation,
                body=body,
                content_type=request.headers.get(
                    "content-type",
                    "application/json",
                ),
                request_headers=request.headers,
            )
        except ModelRelayBoundaryRedirected as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="MODEL_RELAY_MAIN_BOUNDARY_REDIRECTED",
                headers={"Connection": "close"},
            ) from exc
        except ModelRelayBoundaryReplay as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="MODEL_RELAY_MAIN_BOUNDARY_REPLAY",
            ) from exc
        except ModelRelayBoundaryUncertain as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MODEL_RELAY_REDIRECT_UNCERTAIN",
            ) from exc
        except ModelRelayCapacityExhausted as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MODEL_RELAY_CAPACITY_EXHAUSTED",
            ) from exc
        except ModelRelayRequestRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ModelRelayError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="model relay upstream request failed",
            ) from exc
        return StreamingResponse(
            response.body_iterator,
            status_code=response.status_code,
            headers={
                **response.headers,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return app
