"""Runtime Token Budget Monitor (Agent 5)

Provides a TokenBudgetTracker that can be installed as a global tracker
for generator code to report token usage to. The tracker enforces a token
cap and a spend cap (USD) and supports a cheap model switch mitigation.

This is implemented conservatively so it works without a concrete LLM SDK;
generators should call `token_monitor.get_current_tracker()` and invoke
`consume(tokens)` during streaming or heavy operations. If the budget is
exceeded a BudgetExceeded exception is raised and callers should abort and
route to the unhappy path.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from ..services.cost_policy import estimate_cost, PRICING, PROJECT_BUDGET_USD

logger = logging.getLogger("thin_slice.token_monitor")


class BudgetExceeded(Exception):
    pass


class TokenBudgetTracker:
    def __init__(
        self,
        token_cap: Optional[int] = None,
        spend_cap_usd: Optional[float] = None,
        initial_model_tier: str = "standard",
    ) -> None:
        # Allow demo override via environment variables for easier local testing
        env_token_cap = None
        try:
            env_token_cap = int(os.environ.get('THIN_SLICE_TOKEN_CAP')) if os.environ.get('THIN_SLICE_TOKEN_CAP') else None
        except Exception:
            env_token_cap = None

        env_spend_cap = None
        try:
            env_spend_cap = float(os.environ.get('THIN_SLICE_SPEND_CAP_USD')) if os.environ.get('THIN_SLICE_SPEND_CAP_USD') else None
        except Exception:
            env_spend_cap = None

        self.token_cap = token_cap if token_cap is not None else (env_token_cap if env_token_cap is not None else 20000)
        self.spend_cap_usd = spend_cap_usd if spend_cap_usd is not None else (env_spend_cap if env_spend_cap is not None else PROJECT_BUDGET_USD)
        self.current_model_tier = initial_model_tier
        self.tokens_used = 0
        self.usd_spent = 0.0
        logger.info("TokenBudgetTracker initialized: token_cap=%s spend_cap_usd=%.6f tier=%s", self.token_cap, self.spend_cap_usd, self.current_model_tier)

    def consume(self, tokens: int) -> None:
        """Record consumption of tokens. Raises BudgetExceeded if limits are breached."""
        if tokens <= 0:
            return
        inc_cost = estimate_cost(tokens, 0, self.current_model_tier)
        self.tokens_used += tokens
        self.usd_spent += inc_cost
        logger.info(
            "TokenMonitor: consumed %d tokens (tier=%s) -> +$%.6f; total=$%.6f",
            tokens,
            self.current_model_tier,
            inc_cost,
            self.usd_spent,
        )

        if self.tokens_used > self.token_cap or self.usd_spent > self.spend_cap_usd:
            raise BudgetExceeded(
                f"Budget exceeded: tokens={self.tokens_used}/{self.token_cap} usd={self.usd_spent:.6f}/{self.spend_cap_usd:.6f}"
            )

    def switch_to_cheaper(self) -> bool:
        """Attempt to switch to a cheaper model tier. Returns True if switched."""
        # Heuristic: if currently high_reasoning and a cheaper 'standard' tier exists, switch.
        if self.current_model_tier == "high_reasoning" and "standard" in PRICING:
            self.current_model_tier = "standard"
            logger.info("TokenMonitor: switched model tier to 'standard' to conserve budget")
            return True
        return False


# Global tracker (simple, process-local)
_CURRENT_TRACKER: TokenBudgetTracker | None = None


def set_current_tracker(t: Optional[TokenBudgetTracker]) -> None:
    global _CURRENT_TRACKER
    _CURRENT_TRACKER = t


def get_current_tracker() -> Optional[TokenBudgetTracker]:
    return _CURRENT_TRACKER


def clear_current_tracker() -> None:
    set_current_tracker(None)
