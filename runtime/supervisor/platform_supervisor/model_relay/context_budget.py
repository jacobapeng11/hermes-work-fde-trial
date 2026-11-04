from __future__ import annotations

DEFAULT_CONTEXT_LENGTH = 128_000
MIN_CONTEXT_AWARE_OUTPUT_TOKENS = 8_192
CONTEXT_OUTPUT_TOKEN_RATIO = 8


def context_aware_max_output_tokens(
    configured_limit: int,
    context_length: int,
) -> int:
    """Reserve a bounded slice of small-context models for model input."""
    safe_context = (
        int(context_length)
        if int(context_length) > 0
        else DEFAULT_CONTEXT_LENGTH
    )
    context_limit = max(
        MIN_CONTEXT_AWARE_OUTPUT_TOKENS,
        safe_context // CONTEXT_OUTPUT_TOKEN_RATIO,
    )
    return max(1, min(int(configured_limit), context_limit))
