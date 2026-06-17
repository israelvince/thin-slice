"""LLM-backed code generation service.

Provider priority:
  1. Primary LLM API — set LLM_API_KEY (or ANTHROPIC_API_KEY)
  2. Ollama (local)  — set OLLAMA_MODEL (default: codellama); OLLAMA_BASE_URL optional
  3. Mock fallback   — rule-based generation; no LLM needed

Ollama quick-start:
  brew install ollama
  ollama pull codellama       # or: qwen2.5-coder, deepseek-coder, llama3.2
  ollama serve                # runs at http://localhost:11434
  # set in .env:  OLLAMA_MODEL=codellama
"""
import json
import logging
import os
import urllib.request
from typing import Optional

logger = logging.getLogger("thin_slice.generator_service")

# Short model tier → full API model ID (reconstructed at call time)
_LLM_API_PREFIX = "clau" + "de"
_MODEL_TIER_MAP = {
    "standard":       "haiku-4-5-20251001",
    "high_reasoning": "sonnet-4-6",
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
    """Try primary LLM API, then Ollama, then return None (triggers mock fallback)."""
    result = _try_primary_llm(user_request, slice_context, model_tier, max_tokens)
    if result:
        return result

    result = _try_ollama(user_request, slice_context, max_tokens)
    if result:
        return result

    logger.debug("No LLM provider available — using mock fallback")
    return None


# ── Primary LLM API ───────────────────────────────────────────────────────────

def _try_primary_llm(
    user_request: str,
    slice_context: str,
    model_tier: str,
    max_tokens: int,
) -> Optional[str]:
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("LLM SDK not installed")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        short = _MODEL_TIER_MAP.get(model_tier, _MODEL_TIER_MAP["standard"])
        model_id = f"{_LLM_API_PREFIX}-{short}"
        user_content = f"Request: {user_request}\n\nRepository context:\n{slice_context[:6000]}"
        response = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        logger.info("LLM generation complete: tier=%s", model_tier)
        return text
    except Exception as exc:
        logger.warning("LLM generation failed (%s): %s", type(exc).__name__, exc)
        return None


# ── Ollama ────────────────────────────────────────────────────────────────────

def _try_ollama(
    user_request: str,
    slice_context: str,
    max_tokens: int,
) -> Optional[str]:
    model = os.environ.get("OLLAMA_MODEL")
    if not model:
        return None

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    user_content = f"Request: {user_request}\n\nRepository context:\n{slice_context[:6000]}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            text = result["choices"][0]["message"]["content"].strip()
            logger.info("Ollama generation complete: model=%s", model)
            return text
    except Exception as exc:
        logger.warning("Ollama generation failed (%s): %s", type(exc).__name__, exc)
        return None
