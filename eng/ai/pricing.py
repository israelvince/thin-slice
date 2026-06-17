"""Multi-provider LLM pricing — June 2026. Prices in USD per token."""
from typing import Dict, Tuple

HAIKU_45  = "haiku-4-5"
SONNET_46 = "sonnet-4-6"
GEMINI_15_PRO    = "gemini-1.5-pro"
GEMINI_15_FLASH  = "gemini-1.5-flash"
GPT_4O_MINI      = "gpt-4o-mini"

# (input_price_per_token, output_price_per_token)
MODEL_PRICING: Dict[str, Tuple[float, float]] = {
    HAIKU_45:  (0.0000008,   0.000004),
    SONNET_46: (0.000003,    0.000015),
    GEMINI_15_PRO:    (0.00000125,  0.000005),
    GEMINI_15_FLASH:  (0.000000075, 0.0000003),
    GPT_4O_MINI:      (0.00000015,  0.0000006),
}

OUTPUT_RATIO: float = 1.5


def calculate_cost(input_tokens: int, model: str) -> float:
    input_price, output_price = MODEL_PRICING.get(model, MODEL_PRICING[SONNET_46])
    output_tokens = int(input_tokens * OUTPUT_RATIO)
    return (input_tokens * input_price) + (output_tokens * output_price)


def recommend_model(total_tokens: int) -> Tuple[str, str]:
    """Recommend a model based on total (input + output) token count."""
    if total_tokens < 1_500:
        return HAIKU_45, "annotation/tiny change — fastest and cheapest"
    if total_tokens < 20_000:
        return SONNET_46, "balanced — best quality/cost for this size"
    return GEMINI_15_PRO, "large context — best value above 20 k tokens"
