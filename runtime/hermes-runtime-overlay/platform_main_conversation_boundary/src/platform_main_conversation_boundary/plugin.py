"""Register exactly one official Hermes ``llm_request`` middleware."""

from __future__ import annotations

from .marker import BoundaryMarkerSigner


def _load_signer() -> BoundaryMarkerSigner:
    return BoundaryMarkerSigner.from_fixed_fd()


def register(ctx) -> None:
    signer = _load_signer()
    ctx.register_middleware("llm_request", signer.apply)
