"""Risk classification pipeline.

Assigns a risk_level to each CustomerProfile based on spend, review scores,
and order history.

FRAGILE: risk_level is a raw string. The allowed values are duplicated here
AND in profile_validator.py. Any schema change must be applied to both files.
See customer_profile.py for the planned migration to a RiskCategory enum.
"""
from typing import Optional

from demo_repo.models.customer_profile import CustomerProfile

# Duplicated from profile_validator.py — this is the tech debt we're fixing.
_VALID_RISK_LEVELS = ("low", "medium", "high", "unknown")


def classify_risk(profile: CustomerProfile) -> str:
    """Return a risk_level string based on transaction behaviour.

    Rules (heuristic v1):
      - No orders or zero spend       → "unknown"
      - Avg review score < 2          → "high"
      - Avg review score < 3.5        → "medium"
      - High spend (top tier) + good reviews → "low"
      - Default                       → "medium"
    """
    if profile.total_orders == 0 or profile.total_spend_brl == 0:
        return "unknown"

    score = profile.avg_review_score
    if score is None:
        return "medium"
    if score < 2.0:
        return "high"
    if score < 3.5:
        return "medium"
    if profile.total_spend_brl >= 500.0 and score >= 4.0:
        return "low"
    return "medium"


def apply_risk_classification(profiles) -> None:
    """Classify and set risk_level on all profiles in place."""
    for profile in profiles.values():
        level = classify_risk(profile)
        # Defensive guard — should never fire once we migrate to enum
        if level not in _VALID_RISK_LEVELS:
            level = "unknown"
        profile.risk_level = level


def summarise_risk_distribution(profiles) -> dict:
    """Return counts per risk_level for reporting."""
    counts: dict = {level: 0 for level in _VALID_RISK_LEVELS}
    for p in profiles.values():
        counts[p.risk_level] = counts.get(p.risk_level, 0) + 1
    return counts

# Updated by Thin-Slice

# Updated by Thin-Slice

# Updated by Thin-Slice
