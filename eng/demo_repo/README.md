# Customer Transaction Intelligence — Data Product

A data product that builds unified Customer Transaction Profiles from the
Brazilian ecommerce (olist) dataset. Profiles combine spend history, review
scores, and payment behaviour into a single queryable model used downstream
by risk, marketing, and operations teams.

## Domain model

```
CustomerProfile
  customer_id          — unique customer in the orders system
  total_orders         — number of completed orders
  total_spend_brl      — lifetime spend in BRL
  avg_review_score     — mean review score (1–5)
  preferred_payment    — most-used payment method
  risk_level           — "low" | "medium" | "high" | "unknown"  ← PENDING MIGRATION
```

## Known tech debt: risk_level schema

`risk_level` is currently a raw `str`. The allowed values (`low`, `medium`,
`high`, `unknown`) are duplicated in both `pipelines/risk_classifier.py` and
`validators/profile_validator.py`, which caused two data quality incidents
where downstream producers introduced new string values that bypassed
validation.

**Pending change:** replace `risk_level: str` with a `RiskCategory` enum so
the contract is enforced at the type level across the entire product boundary.

## Pipelines

| Pipeline | What it does |
|---|---|
| `customer_aggregator.py` | Reads olist CSVs → builds CustomerProfile objects |
| `risk_classifier.py` | Assigns risk_level from spend + review heuristics |

## Validators

| Validator | What it does |
|---|---|
| `profile_validator.py` | Enforces schema rules before store writes |

## Data source

Raw CSVs live in `eng/data/ecommerce/` (Brazilian e-commerce public dataset).
