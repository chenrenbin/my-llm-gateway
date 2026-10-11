from ..domain.config import PriceConfig


def calculate_cost(
    price: PriceConfig | None,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """cost_usd using per-1M-token prices, with a separate cached-input rate.

    Cached input tokens are billed at `cached_input_per_million` when configured,
    otherwise at the normal input rate.
    """
    if price is None:
        return 0.0
    fresh_input = max(0, input_tokens - cached_tokens)
    cached_rate = (
        price.cached_input_per_million
        if price.cached_input_per_million is not None
        else price.input_per_million
    )
    cost = fresh_input * price.input_per_million / 1_000_000
    cost += cached_tokens * cached_rate / 1_000_000
    cost += output_tokens * price.output_per_million / 1_000_000
    return round(cost, 10)
