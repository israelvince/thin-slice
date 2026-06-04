"""Customer Transaction Profile — core domain model.

This is the schema for a customer's aggregated transaction profile,
derived from the ecommerce order and payment history.

KNOWN TECH DEBT:
  risk_level is currently a raw string ("low", "medium", "high", "unknown").
  Downstream consumers (risk_classifier, profile_validator) each duplicate
  their own allowed-values list, which has already caused two data quality
  incidents where unknown values slipped through.

  Planned change: replace risk_level: str with a RiskCategory enum so the
  contract is enforced at the type level.
"""
from dataclasses import dataclass, field
from typing import List, Optional


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

    # Risk classification — STRING today, should become RiskCategory enum.
    # Allowed values: "low", "medium", "high", "unknown"
    # Do NOT add new values here without updating profile_validator.py and
    # risk_classifier.py — they each maintain their own copy of this list.
    risk_level: str = "unknown"

    # Lifecycle flags
    is_active: bool = True
    order_ids: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Customer {self.customer_id[:8]}… | "
            f"orders={self.total_orders} spend=R${self.total_spend_brl:.2f} "
            f"risk={self.risk_level}"
        )

# Updated by Thin-Slice

# Updated by Thin-Slice

# Updated by Thin-Slice
