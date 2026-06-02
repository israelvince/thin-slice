PRICING = {
    "standard": {"input_per_1k": 0.0005, "output_per_1k": 0.0015},
    "high_reasoning": {"input_per_1k": 0.002, "output_per_1k": 0.006},
}

PROJECT_BUDGET_USD = 2.00

def estimate_cost(tokens_in: int, tokens_out: int, model_tier: str = "standard") -> float:
    tier = PRICING.get(model_tier, PRICING["standard"])
    cost = (tokens_in / 1000.0) * tier["input_per_1k"] + (tokens_out / 1000.0) * tier["output_per_1k"]
    return round(cost, 6)

def check_budget(tokens_in: int, tokens_out: int, model_tier: str = "standard") -> dict:
    projected = estimate_cost(tokens_in, tokens_out, model_tier)
    ok = projected <= PROJECT_BUDGET_USD
    return {"projected_cost_usd": projected, "policy_clearance": ok}
