"""Multi-provider LLM pricing — June 2026. Prices in USD per token."""
from typing import Dict, Tuple

CLAUDE_HAIKU_45  = "claude-haiku-4-5"
CLAUDE_SONNET_46 = "claude-sonnet-4-6"
GEMINI_15_PRO    = "gemini-1.5-pro"
GEMINI_15_FLASH  = "gemini-1.5-flash"
GPT_4O_MINI      = "gpt-4o-mini"

# (input_price_per_token, output_price_per_token)
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    CLAUDE_HAIKU_45:  (0.0000008,   0.000004),
    CLAUDE_SONNET_46: (0.000003,    0.000015),
    GEMINI_15_PRO:    (0.00000125,  0.000005),
    GEMINI_15_FLASH:  (0.000000075, 0.0000003),
    GPT_4O_MINI:      (0.00000015,  0.0000006),
}

OUTPUT_RATIO: float = 1.5


def calculate_cost(input_tokens: int, model: str) -> float:
    input_price, output_price = MODEL_PRICING.get(model, MODEL_PRICING[CLAUDE_SONNET_46])
    output_tokens = int(input_tokens * OUTPUT_RATIO)
    return (input_tokens * input_price) + (output_tokens * output_price)


def recommend_model(input_tokens: int) -> Tuple[str, str]:
    if input_tokens < 500:
        return CLAUDE_HAIKU_45, "small change — fastest and cheapest"
    if input_tokens < 2000:
        return CLAUDE_SONNET_46, "medium change — best quality/cost balance"
    return GEMINI_15_PRO, "large change — best value for high token count"
