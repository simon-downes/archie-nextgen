"""LLM client package.

Re-exports the public API so existing imports like `from archie.llm import BedrockClient`
continue to work after the refactor from a single module to a package.
"""

from archie.llm.bedrock import BedrockClient, Done, StreamEvent, TextDelta, ToolUseEvent, Usage

__all__ = [
    "BedrockClient",
    "Done",
    "StreamEvent",
    "TextDelta",
    "ToolUseEvent",
    "Usage",
]
