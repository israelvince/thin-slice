"""Transaction domain models — Order, OrderPayment, OrderReview."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderPayment:
    order_id: str
    payment_type: str
    payment_installments: int
    payment_value: float


@dataclass
class OrderReview:
    order_id: str
    review_score: int


@dataclass
class Order:
    order_id: str
    customer_id: str
    status: str
    payment: Optional[OrderPayment] = field(default=None)
    review: Optional[OrderReview] = field(default=None)
