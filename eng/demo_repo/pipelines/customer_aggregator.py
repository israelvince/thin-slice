"""Customer aggregation pipeline.

Reads the olist ecommerce CSVs and builds CustomerProfile objects from raw data.
"""
import csv
import os
from collections import defaultdict
from typing import Dict, List

from demo_repo.models.customer_profile import CustomerProfile
from demo_repo.models.transaction import Order, OrderPayment, OrderReview

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "ecommerce")


def _csv_path(name: str) -> str:
    return os.path.join(_DATA_DIR, name)


def load_customers() -> Dict[str, CustomerProfile]:
    """Load all customers from the olist customers dataset."""
    profiles = {}
    with open(_csv_path("olist_customers_dataset.csv"), newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            profiles[row["customer_id"]] = CustomerProfile(
                customer_id=row["customer_id"],
                customer_unique_id=row["customer_unique_id"],
                city=row["customer_city"],
                state=row["customer_state"],
            )
    return profiles


def load_orders(profiles: Dict[str, CustomerProfile]) -> Dict[str, Order]:
    """Load orders and attach payment + review data."""
    orders: Dict[str, Order] = {}
    with open(_csv_path("olist_orders_dataset.csv"), newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            orders[row["order_id"]] = Order(
                order_id=row["order_id"],
                customer_id=row["customer_id"],
                status=row["order_status"],
            )
            if row["customer_id"] in profiles:
                profiles[row["customer_id"]].order_ids.append(row["order_id"])

    # Attach payments
    with open(_csv_path("olist_order_payments_dataset.csv"), newline="", encoding="utf-8") as fh:
        seen: set = set()
        for row in csv.DictReader(fh):
            oid = row["order_id"]
            if oid in orders and oid not in seen:
                orders[oid].payment = OrderPayment(
                    order_id=oid,
                    payment_type=row["payment_type"],
                    payment_installments=int(row["payment_installments"]),
                    payment_value=float(row["payment_value"]),
                )
                seen.add(oid)

    # Attach reviews
    with open(_csv_path("olist_order_reviews_dataset.csv"), newline="", encoding="utf-8") as fh:
        seen = set()
        for row in csv.DictReader(fh):
            oid = row["order_id"]
            if oid in orders and oid not in seen:
                orders[oid].review = OrderReview(
                    order_id=oid,
                    review_score=int(row["review_score"]),
                )
                seen.add(oid)

    return orders


def aggregate_profiles(profiles: Dict[str, CustomerProfile], orders: Dict[str, Order]) -> None:
    """Compute spend, review score, and preferred payment per customer — in place."""
    spend: Dict[str, float] = defaultdict(float)
    scores: Dict[str, List[int]] = defaultdict(list)
    payments: Dict[str, List[str]] = defaultdict(list)

    for order in orders.values():
        cid = order.customer_id
        if order.payment:
            spend[cid] += order.payment.payment_value
            payments[cid].append(order.payment.payment_type)
        if order.review:
            scores[cid].append(order.review.review_score)

    for cid, profile in profiles.items():
        profile.total_orders = len(profile.order_ids)
        profile.total_spend_brl = round(spend.get(cid, 0.0), 2)
        sc = scores.get(cid)
        profile.avg_review_score = round(sum(sc) / len(sc), 2) if sc else None
        plist = payments.get(cid, [])
        if plist:
            profile.preferred_payment_type = max(set(plist), key=plist.count)
