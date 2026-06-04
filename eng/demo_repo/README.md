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
