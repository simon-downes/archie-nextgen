"""web_search tool — search the web via DuckDuckGo metasearch.

Uses the `ddgs` library to perform a metasearch across multiple backends
(Google, Bing, DuckDuckGo, Brave, etc.) and returns structured results
with title, URL, and snippet.

Runs on the host (same as web_fetch), not in the sandbox.
"""

from archie.tools import ToolSpec, tool_error, tool_result


def make_web_search_spec() -> ToolSpec:
    """Create a web_search ToolSpec. No arguments needed."""

    def handler(params: dict) -> str:
        query = params.get("query", "").strip()
        if not query:
            return tool_error("'query' is required.")

        # Lazy import — ddgs pulls in httpx, lxml, etc.
        from ddgs import DDGS

        try:
            results = DDGS(timeout=10).text(query, safesearch="off", backend="auto", max_results=8)
        except Exception as e:
            return tool_error(f"Search failed: {e}")

        if not results:
            return tool_result("No results found.")

        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "(no title)")
            url = r.get("href", "")
            snippet = r.get("body", "")
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}")

        return tool_result("\n\n".join(lines))

    return ToolSpec(
        name="web_search",
        description=(
            "Search the web. Use for: finding documentation, checking library versions, "
            "looking up error messages, researching APIs and tools. Returns top 8 results "
            "with title, URL, and snippet. Include the current year in your query for recent "
            "results (e.g. 'pydantic python 3.14 2025')."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search query. Be specific — include library names, versions, error messages."
                    ),
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )
