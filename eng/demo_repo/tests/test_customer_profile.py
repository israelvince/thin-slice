"""Tests for the Customer Transaction Profile domain model and validation."""
import pytest

from demo_repo.models.customer_profile import CustomerProfile
from demo_repo.pipelines.risk_classifier import classify_risk
from demo_repo.validators.profile_validator import validate_profile


def _profile(**kwargs) -> CustomerProfile:
    defaults = dict(
        customer_id="abc123",
        customer_unique_id="uid-abc",
        city="São Paulo",
        state="SP",
    )
    defaults.update(kwargs)
    return CustomerProfile(**defaults)


class TestRiskClassifier:
    def test_unknown_when_no_orders(self):
        assert classify_risk(_profile(total_orders=0)) == "unknown"

    def test_high_risk_on_low_score(self):
        p = _profile(total_orders=3, total_spend_brl=120.0, avg_review_score=1.5)
        assert classify_risk(p) == "high"

    def test_medium_risk_moderate_score(self):
        p = _profile(total_orders=2, total_spend_brl=80.0, avg_review_score=3.0)
        assert classify_risk(p) == "medium"

    def test_low_risk_good_customer(self):
        p = _profile(total_orders=10, total_spend_brl=600.0, avg_review_score=4.5)
        assert classify_risk(p) == "low"


class TestProfileValidator:
    def test_valid_profile_passes(self):
        p = _profile(risk_level="medium", total_spend_brl=50.0, avg_review_score=4.0)
        result = validate_profile(p)
        assert result.is_valid
        assert result.errors == []

    def test_invalid_risk_level_fails(self):
        # This test documents the current tech debt — any string passes the model
        # but the validator catches it. With a RiskCategory enum this test is obsolete.
        p = _profile(risk_level="very_high")
        result = validate_profile(p)
        assert not result.is_valid
        assert any("risk_level" in e for e in result.errors)

    def test_missing_customer_id_fails(self):
        p = _profile(customer_id="")
        result = validate_profile(p)
        assert not result.is_valid

# Updated by Thin-Slice

# Updated by Thin-Slice

# Updated by Thin-Slice
