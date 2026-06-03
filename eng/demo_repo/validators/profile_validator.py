"""Customer profile validation.

Enforces data quality rules before profiles are written to the data product store.

FRAGILE: The allowed risk_level values are duplicated from risk_classifier.py.
Migration to a RiskCategory enum will remove this duplication and make the
contract enforceable at the type level.
"""
from dataclasses import dataclass
from typing import List

from demo_repo.models.customer_profile import CustomerProfile

# Duplicated from risk_classifier.py — the exact tech debt driving the enum migration.
_ALLOWED_RISK_LEVELS = {"low", "medium", "high", "unknown"}


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str]


def validate_profile(profile: CustomerProfile) -> ValidationResult:
    """Run all validation rules against a CustomerProfile."""
    errors: List[str] = []

    if not profile.customer_id:
        errors.append("customer_id is required")

    if not profile.customer_unique_id:
        errors.append("customer_unique_id is required")

    # This block exists solely because risk_level is an untyped string.
    # Once migrated to RiskCategory enum, this check becomes unnecessary.
    if profile.risk_level not in _ALLOWED_RISK_LEVELS:
        errors.append(
            f"risk_level '{profile.risk_level}' is not valid. "
            f"Must be one of: {sorted(_ALLOWED_RISK_LEVELS)}"
        )

    if profile.total_spend_brl < 0:
        errors.append("total_spend_brl cannot be negative")

    if profile.avg_review_score is not None and not (1.0 <= profile.avg_review_score <= 5.0):
        errors.append(f"avg_review_score {profile.avg_review_score} must be between 1 and 5")

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


def validate_all(profiles) -> List[ValidationResult]:
    """Validate every profile and return a result per profile."""
    return [validate_profile(p) for p in profiles.values()]
