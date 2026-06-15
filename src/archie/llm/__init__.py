"""LLM client package.

Re-exports the public API so existing imports like `from archie.llm import BedrockClient`
continue to work after the refactor from a single module to a package.

Defines the LLMClient protocol — the provider-agnostic interface that both BedrockClient
and OllamaClient satisfy. The agent loop and other consumers type-hint against this protocol,
never against a concrete provider class.
"""

from collections.abc import Generator
from typing import Protocol

from archie.llm.bedrock import BedrockClient, Done, StreamEvent, TextDelta, ToolUseEvent, Usage
from archie.session import Turn


class LLMClient(Protocol):
    """Provider-agnostic LLM client interface.

    Both stream() and invoke() accept internal Turn objects — each provider
    translates to its own wire format internally.
    """

    model_id: str

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]: ...

    def invoke(self, messages: list[Turn], system: str) -> str: ...


def get_ollama_client_class() -> type:
    """Lazy import of OllamaClient to avoid loading ollama/pydantic at module import time."""
    from archie.llm.ollama import OllamaClient

    return OllamaClient


__all__ = [
    "BedrockClient",
    "Done",
    "LLMClient",
    "StreamEvent",
    "TextDelta",
    "ToolUseEvent",
    "Usage",
    "get_ollama_client_class",
]
