# Pre‑PR Cost Estimator — one‑page design

Goal
- Predict the USD cost of running the LLM for a given sliced context and planned output size.

Responsibilities
- Estimate input tokens for the provided context.
- Assume a target output token budget (e.g., 2k tokens) or estimate based on change complexity.
- Apply a pricing table per model tier and add a safety buffer.

Success criteria
- Estimator returns a projected_cost_usd and a boolean policy_clearance given a configurable project budget.
