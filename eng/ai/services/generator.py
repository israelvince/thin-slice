"""LLM-backed code generation service.

Calls the Anthropic API when ANTHROPIC_API_KEY is set.
Returns None silently when the key is absent, letting callers fall back to mock logic.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger("thin_slice.generator_service")

_MODEL_MAP = {
    "standard": "claude-haiku-4-5-20251001",
    "high_reasoning": "claude-sonnet-4-6",
}

_SYSTEM = (
    "You are a precise code-generation assistant embedded in a CI pipeline. "
    "Given a change request and the relevant repository context, produce the minimal, "
    "production-ready code update for the most relevant file. "
    "Return only the file content — no explanations, no markdown fences."
)


def generate_code(
    user_request: str,
    slice_context: str,
    model_tier: str = "standard",
    max_tokens: int = 2000,
) -> Optional[str]:
    """Generate code via Anthropic API. Returns generated text, or None if unavailable."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.debug("ANTHROPIC_API_KEY not set — skipping LLM, using mock fallback")
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — using mock fallback")
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = _MODEL_MAP.get(model_tier, _MODEL_MAP["standard"])
        user_content = f"Request: {user_request}\n\nRepository context:\n{slice_context[:6000]}"
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        logger.info("LLM generation complete: model=%s", model)
        return text
    except Exception as exc:
        logger.warning("LLM generation failed (%s): %s", type(exc).__name__, exc)
        return None
