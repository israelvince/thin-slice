"""Transaction and payment models for the Customer Transaction Intelligence product."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderPayment:
    order_id: str
    payment_type: str          # credit_card, boleto, voucher, debit_card
    payment_installments: int
    payment_value: float


@dataclass
class OrderReview:
    order_id: str
    review_score: int          # 1–5
    review_comment: Optional[str] = None


@dataclass
class Order:
    order_id: str
    customer_id: str
    status: str                # delivered, shipped, canceled, etc.
    payment: Optional[OrderPayment] = None
    review: Optional[OrderReview] = None

    def is_completed(self) -> bool:
        return self.status == "delivered"
