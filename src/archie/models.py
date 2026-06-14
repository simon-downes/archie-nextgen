"""Model constants — pricing, context limits, display names.

These are properties of the models themselves, not user configuration.
Users can't change a model's context window or pricing, so these live in
code rather than config files.

Model IDs use the Bedrock inference profile format. The prefix (eu./us.)
determines which cross-region inference profile is used.

Pricing includes all four token categories: fresh input, cache-read (cheap),
cache-write (premium), and output. These map directly to how Bedrock bills
requests that use prompt caching.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """Static properties of a Bedrock model.

    Attributes:
        name: Human-readable display name (shown in status bar).
        max_context_tokens: Maximum tokens the model can process in one request.
        input_price_per_m: Cost in USD per 1 million fresh input tokens.
        output_price_per_m: Cost in USD per 1 million output tokens.
        cache_read_price_per_m: Cost per 1M cache-read tokens (~10x cheaper than input).
        cache_write_price_per_m: Cost per 1M cache-write tokens (premium over input).
        context_warning_threshold: Fraction (0-1) of max_context at which to warn.
        max_output_tokens: Maximum tokens the model can generate per response.
    """

    name: str
    max_context_tokens: int
    input_price_per_m: float
    output_price_per_m: float
    cache_read_price_per_m: float = 0.0
    cache_write_price_per_m: float = 0.0
    context_warning_threshold: float = 0.8
    max_output_tokens: int = 32_768


MODELS: dict[str, ModelInfo] = {
    "eu.anthropic.claude-fable-5": ModelInfo(
        name="Claude Fable 5",
        max_context_tokens=1_000_000,
        input_price_per_m=10.0,
        output_price_per_m=50.0,
        cache_read_price_per_m=1.0,
        cache_write_price_per_m=12.5,
    ),
    "eu.anthropic.claude-sonnet-4-6": ModelInfo(
        name="Claude Sonnet 4.6",
        max_context_tokens=1_000_000,
        input_price_per_m=3.0,
        output_price_per_m=15.0,
        cache_read_price_per_m=0.30,
        cache_write_price_per_m=3.75,
    ),
    "eu.anthropic.claude-haiku-3-20250305-v1:0": ModelInfo(
        name="Claude Haiku",
        max_context_tokens=200_000,
        input_price_per_m=0.80,
        output_price_per_m=4.0,
        cache_read_price_per_m=0.08,
        cache_write_price_per_m=1.0,
    ),
    "eu.anthropic.claude-opus-4-6-v1": ModelInfo(
        name="Claude Opus 4.6",
        max_context_tokens=1_000_000,
        input_price_per_m=15.0,
        output_price_per_m=75.0,
        cache_read_price_per_m=1.50,
        cache_write_price_per_m=18.75,
    ),
    "eu.anthropic.claude-opus-4-8": ModelInfo(
        name="Claude Opus 4.8",
        max_context_tokens=1_000_000,
        input_price_per_m=15.0,
        output_price_per_m=75.0,
        cache_read_price_per_m=1.50,
        cache_write_price_per_m=18.75,
    ),
}


def get_model_info(model_id: str) -> ModelInfo:
    """Look up model info by inference profile ID."""
    if model_id not in MODELS:
        available = ", ".join(MODELS.keys())
        raise KeyError(f"Unknown model '{model_id}'. Available: {available}")
    return MODELS[model_id]


def calculate_cost(
    model: ModelInfo,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Calculate cost in USD using all four token category rates."""
    return (
        input_tokens * model.input_price_per_m / 1_000_000
        + output_tokens * model.output_price_per_m / 1_000_000
        + cache_read_tokens * model.cache_read_price_per_m / 1_000_000
        + cache_write_tokens * model.cache_write_price_per_m / 1_000_000
    )
