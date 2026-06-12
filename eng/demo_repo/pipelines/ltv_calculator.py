"""Customer Lifetime Value (LTV) calculator.

LTV = frequency × recency × spend

- frequency: total number of orders placed by the customer
- recency: days since the last order (lower = more recent = higher value)
- spend: total amount spent in BRL

MIN_ORDERS_FOR_LTV: minimum number of orders required to calculate a
    meaningful LTV. New customers below this threshold receive LTV = 0.0
    because insufficient purchase history makes extrapolation unreliable.

DAYS_RECENT_ORDER: number of days within which an order is considered
    recent. Orders older than this window contribute less to LTV weighting.
"""
from demo_repo.models.customer_profile import CustomerProfile

MIN_ORDERS_FOR_LTV = 2
DAYS_RECENT_ORDER = 90


def calculate_ltv(profile: CustomerProfile, days_since_last_order: int = 0) -> float:
    """Return the estimated LTV for a customer profile.

    New customers (fewer than MIN_ORDERS_FOR_LTV orders) always receive
    LTV = 0.0 because insufficient purchase history makes extrapolation
    unreliable.
    """
    if profile.total_orders < MIN_ORDERS_FOR_LTV:
        return 0.0

    recency_weight = 1.0 if days_since_last_order <= DAYS_RECENT_ORDER else 0.5
    return profile.total_orders * recency_weight * float(profile.total_spend_brl)
