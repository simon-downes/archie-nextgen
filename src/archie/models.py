"""Model constants — pricing, context limits, display names.

These are properties of the models themselves, not user configuration.
Users can't change a model's context window or pricing, so these live in
code rather than config files.

Model IDs use the Bedrock inference profile format. The prefix (eu./us.)
determines which cross-region inference profile is used. This is required
by Bedrock for on-demand throughput — you can't use bare model IDs anymore.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    """Static properties of a Bedrock model.

    Attributes:
        name: Human-readable display name (shown in status bar).
        max_context_tokens: Maximum tokens the model can process in one request.
            This is the total of input + output for a single API call.
        input_price_per_m: Cost in USD per 1 million input tokens.
        output_price_per_m: Cost in USD per 1 million output tokens.
        context_warning_threshold: Fraction (0-1) of max_context at which to warn.
            At 0.8, the UI warns when context usage exceeds 80% of the limit.
    """

    name: str
    max_context_tokens: int
    input_price_per_m: float
    output_price_per_m: float
    context_warning_threshold: float = 0.8


# Registry of known models keyed by their Bedrock inference profile ID.
# Add new models here as they become available.
MODELS: dict[str, ModelInfo] = {
    "eu.anthropic.claude-fable-5": ModelInfo(
        name="Claude Fable 5",
        max_context_tokens=1_000_000,
        input_price_per_m=10.0,
        output_price_per_m=50.0,
    ),
    "eu.anthropic.claude-sonnet-4-6": ModelInfo(
        name="Claude Sonnet 4.6",
        max_context_tokens=1_000_000,
        input_price_per_m=3.0,
        output_price_per_m=15.0,
    ),
    "eu.anthropic.claude-haiku-3-20250305-v1:0": ModelInfo(
        name="Claude Haiku",
        max_context_tokens=200_000,
        input_price_per_m=0.80,
        output_price_per_m=4.0,
    ),
    "eu.anthropic.claude-opus-4-6-v1": ModelInfo(
        name="Claude Opus 4.6",
        max_context_tokens=1_000_000,
        input_price_per_m=15.0,
        output_price_per_m=75.0,
    ),
}


def get_model_info(model_id: str) -> ModelInfo:
    """Look up model info by inference profile ID.

    Raises KeyError with a helpful message listing available models
    if the ID isn't found.
    """
    if model_id not in MODELS:
        available = ", ".join(MODELS.keys())
        raise KeyError(f"Unknown model '{model_id}'. Available: {available}")
    return MODELS[model_id]


def calculate_cost(model: ModelInfo, input_tokens: int, output_tokens: int) -> float:
    """Calculate cost in USD for given token counts.

    Bedrock charges separately for input and output tokens.
    Pricing is per million tokens, so we divide by 1,000,000.
    """
    return (
        input_tokens * model.input_price_per_m / 1_000_000
        + output_tokens * model.output_price_per_m / 1_000_000
    )
