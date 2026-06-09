"""Churn risk scoring pipeline.

Predicts which customers are at risk of churning based on order recency,
review scores, and spend trajectory. Scores above CHURN_RISK_THRESHOLD
are flagged for intervention by the CRM team.
"""
import logging

from demo_repo.models.customer_profile import CustomerProfile

logger = logging.getLogger(__name__)

CHURN_RISK_THRESHOLD = 0.65


def score_churn_risk(profile: CustomerProfile) -> float:
    """Return a churn risk score between 0.0 and 1.0.

    Higher score means higher churn risk.
    Rules:
      - No orders                        → 1.0  (already churned)
      - Avg review score < 2.0           → 0.8  (very unhappy)
      - Avg review score < 3.0           → 0.6  (at risk)
      - Single order, no repeat signal   → 0.55
      - High spend + strong reviews      → 0.1  (loyal)
      - Default                          → 0.4
    """
    if profile.total_orders == 0:
        return 1.0
    if profile.avg_review_score is None:
        return 0.5
    review = profile.avg_review_score
    if review < 2.0:
        return 0.8
    if review < 3.0:
        return 0.6
    if profile.total_orders == 1:
        return 0.55
    if profile.total_spend_brl >= 500.0 and review >= 4.0:
        return 0.1
    return 0.4


def flag_churn_risks(profiles: dict) -> list:
    """Return customer IDs where churn risk score exceeds CHURN_RISK_THRESHOLD."""
    at_risk = []
    for customer_id, profile in profiles.items():
        risk = score_churn_risk(profile)
        if risk > CHURN_RISK_THRESHOLD:
            at_risk.append(customer_id)
    return at_risk
