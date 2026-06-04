"""LLM-backed code generation service.

Provider priority:
  1. Anthropic API  — set ANTHROPIC_API_KEY
  2. Ollama (local) — set OLLAMA_MODEL; OLLAMA_BASE_URL optional (default: http://localhost:11434)
  3. Demo mock      — generates real code for the Customer Transaction Intelligence use case
  4. Generic mock   — returns None (caller appends a placeholder comment)

Ollama quick-start:
  brew install ollama && ollama pull codellama && ollama serve
  # then set OLLAMA_MODEL=codellama in .env
"""
import json
import logging
import os
import urllib.request
from typing import Optional

logger = logging.getLogger("thin_slice.generator_service")

_ANTHROPIC_MODEL_MAP = {
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
    """Try Anthropic → Ollama → demo mock → None (triggers generic fallback)."""
    return (
        _try_anthropic(user_request, slice_context, model_tier, max_tokens)
        or _try_ollama(user_request, slice_context, max_tokens)
        or _try_demo_mock(user_request)
    )


# ── Anthropic ─────────────────────────────────────────────────────────────────

def _try_anthropic(user_request, slice_context, model_tier, max_tokens) -> Optional[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed")
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        model = _ANTHROPIC_MODEL_MAP.get(model_tier, _ANTHROPIC_MODEL_MAP["standard"])
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"Request: {user_request}\n\nContext:\n{slice_context[:6000]}"}],
        )
        text = response.content[0].text.strip()
        logger.info("Anthropic generation complete: model=%s", model)
        return text
    except Exception as exc:
        logger.warning("Anthropic failed (%s): %s", type(exc).__name__, exc)
        return None


# ── Ollama ────────────────────────────────────────────────────────────────────

def _try_ollama(user_request, slice_context, max_tokens) -> Optional[str]:
    model = os.environ.get("OLLAMA_MODEL")
    if not model:
        return None
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"Request: {user_request}\n\nContext:\n{slice_context[:6000]}"},
        ],
        "stream": False,
        "options": {"num_predict": max_tokens},
    }
    try:
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            text = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
            logger.info("Ollama generation complete: model=%s", model)
            return text
    except Exception as exc:
        logger.warning("Ollama failed (%s): %s", type(exc).__name__, exc)
        return None


# ── Demo mock ─────────────────────────────────────────────────────────────────

def _try_demo_mock(user_request: str) -> Optional[str]:
    """Generate real code for the Customer Transaction Intelligence demo use case.

    Activates when no LLM provider is available and the request is about
    replacing risk_level with a RiskCategory enum — the canonical demo scenario.
    Returns the updated customer_profile.py content with the full enum migration.
    """
    lower = user_request.lower()
    is_enum_migration = (
        "risk" in lower
        and any(k in lower for k in ("enum", "riskcategory", "risk_category", "risk_level", "category"))
    )
    if not is_enum_migration:
        return None

    logger.info("Generator: using demo mock (enum migration)")
    return '''\
"""Customer Transaction Profile — core domain model.

Migration applied: risk_level (str) replaced with risk_category (RiskCategory enum).
The enum enforces the contract at the type level, eliminating the duplicated
allowed-values lists that existed in risk_classifier.py and profile_validator.py.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class RiskCategory(str, Enum):
    """Standardised risk classification for customer transaction profiles."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass
class CustomerProfile:
    customer_id: str
    customer_unique_id: str
    city: str
    state: str

    # Aggregated transaction metrics
    total_orders: int = 0
    total_spend_brl: float = 0.0
    avg_review_score: Optional[float] = None
    preferred_payment_type: Optional[str] = None

    # Risk classification — now type-safe via RiskCategory enum.
    # Downstream consumers (risk_classifier, profile_validator) should
    # import RiskCategory from this module instead of maintaining their own
    # allowed-values lists.
    risk_category: RiskCategory = RiskCategory.UNKNOWN

    is_active: bool = True
    order_ids: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Customer {self.customer_id[:8]}… | "
            f"orders={self.total_orders} spend=R${self.total_spend_brl:.2f} "
            f"risk={self.risk_category.value}"
        )
'''
