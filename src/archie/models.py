"""Model constants — pricing, context limits, display names.

These are properties of the models themselves, not user configuration.
Users can't change a model's context window or pricing, so these live in
code rather than config files.

Model IDs use the Bedrock inference profile format. Geo-inference models
(the eu. Claude profiles) are reached via the configured session region.
Models without a geo/global profile must be called on a specific in-region
endpoint; those carry an explicit `region` field that overrides the session
region.

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
        region: Specific Bedrock region to call for this model. Set only for
            models without geo/global inference profiles (in-region endpoints
            only). Geo-inference models (the eu. Claude profiles) leave this
            None and use the configured session region.
        supports_cache: Whether this model supports Bedrock prompt caching.
    """

    name: str
    max_context_tokens: int
    input_price_per_m: float
    output_price_per_m: float
    cache_read_price_per_m: float = 0.0
    cache_write_price_per_m: float = 0.0
    context_warning_threshold: float = 0.8
    max_output_tokens: int = 32_768
    region: str | None = None
    provider: str = "bedrock"
    supports_cache: bool = True


MODELS: dict[str, ModelInfo] = {
    "eu.anthropic.claude-fable-5": ModelInfo(
        name="Claude Fable 5",
        max_context_tokens=1_000_000,
        input_price_per_m=11.0,
        output_price_per_m=55.0,
        cache_read_price_per_m=1.10,
        cache_write_price_per_m=13.75,
    ),
    "eu.anthropic.claude-sonnet-4-6": ModelInfo(
        name="Claude Sonnet 4.6",
        max_context_tokens=1_000_000,
        input_price_per_m=3.3,
        output_price_per_m=16.5,
        cache_read_price_per_m=0.33,
        cache_write_price_per_m=4.125,
    ),
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0": ModelInfo(
        name="Claude Haiku",
        max_context_tokens=200_000,
        input_price_per_m=1.1,
        output_price_per_m=5.5,
        cache_read_price_per_m=0.11,
        cache_write_price_per_m=1.375,
    ),
    "eu.anthropic.claude-opus-4-6-v1": ModelInfo(
        name="Claude Opus 4.6",
        max_context_tokens=1_000_000,
        input_price_per_m=5.5,
        output_price_per_m=27.5,
        cache_read_price_per_m=0.55,
        cache_write_price_per_m=6.875,
    ),
    "eu.anthropic.claude-opus-4-8": ModelInfo(
        name="Claude Opus 4.8",
        max_context_tokens=1_000_000,
        input_price_per_m=5.5,
        output_price_per_m=27.5,
        cache_read_price_per_m=0.55,
        cache_write_price_per_m=6.875,
    ),
    # --- Non-Anthropic models ---
    "zai.glm-5": ModelInfo(
        name="GLM 5",
        max_context_tokens=200_000,
        max_output_tokens=128_000,
        input_price_per_m=1.55,
        output_price_per_m=4.96,
        region="eu-west-2",  # London — in-region endpoint only
        supports_cache=False,
    ),
    "qwen.qwen3-coder-next": ModelInfo(
        name="Qwen3 Coder Next",
        max_context_tokens=256_000,
        max_output_tokens=16_000,
        input_price_per_m=0.60,
        output_price_per_m=1.44,
        region="eu-west-1",  # Ireland — cheaper region, in-region endpoint only
        supports_cache=False,
    ),
    "qwen.qwen3-coder-480b-a35b-v1:0": ModelInfo(
        name="Qwen3 Coder 480B A35B",
        max_context_tokens=128_000,
        max_output_tokens=16_000,
        input_price_per_m=0.70,
        output_price_per_m=2.79,
        region="eu-west-2",  # London — in-region endpoint only
        supports_cache=False,
    ),
    "moonshotai.kimi-k2.5": ModelInfo(
        name="Kimi K2.5",
        max_context_tokens=256_000,
        max_output_tokens=16_000,
        input_price_per_m=0.72,
        output_price_per_m=3.60,
        region="eu-west-2",  # London — in-region endpoint only
        supports_cache=False,
    ),
    # --- Ollama local models ---
    "qwen3.6:35b": ModelInfo(
        name="Qwen 3.6 35B",
        max_context_tokens=128_000,
        max_output_tokens=16_000,
        input_price_per_m=0.0,
        output_price_per_m=0.0,
        provider="ollama",
        supports_cache=False,
    ),
    "gemma4:31b": ModelInfo(
        name="Gemma 4 31B",
        max_context_tokens=128_000,
        max_output_tokens=16_000,
        input_price_per_m=0.0,
        output_price_per_m=0.0,
        provider="ollama",
        supports_cache=False,
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
