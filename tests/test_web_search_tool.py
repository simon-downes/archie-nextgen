"""Tests for the web_search tool wrapper (DDGS mocked)."""

from unittest.mock import patch

from archie.tools.web_search import make_web_search_spec


class TestWebSearchTool:
    def test_result_formatting(self):
        spec = make_web_search_spec()
        fake_results = [
            {
                "title": "Pydantic v2.12 Release",
                "href": "https://pydantic.dev/articles/pydantic-v2-12-release",
                "body": "Pydantic V1 core functionality will not work properly with Python 3.14 or greater.",
            },
            {
                "title": "Add initial support for Python 3.14 · Issue #11613",
                "href": "https://github.com/pydantic/pydantic/issues/11613",
                "body": "Reload to refresh your session...",
            },
        ]

        def fake_text(query, safesearch="off", backend="auto", max_results=8):
            return fake_results

        with patch("ddgs.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text = fake_text
            out = spec.handler({"query": "pydantic python 3.14"})

        assert "Pydantic v2.12 Release" in out
        assert "https://pydantic.dev/articles/pydantic-v2-12-release" in out
        assert "github.com" in out
        assert "Issue #11613" in out

    def test_empty_query_returns_error(self):
        spec = make_web_search_spec()
        out = spec.handler({"query": ""})
        assert out.startswith("Error:")
        assert "required" in out.lower()

    def test_missing_query_key_returns_error(self):
        spec = make_web_search_spec()
        out = spec.handler({})
        assert out.startswith("Error:")
        assert "required" in out.lower()

    def test_empty_results(self):
        spec = make_web_search_spec()

        def fake_text(query, safesearch="off", backend="auto", max_results=8):
            return []

        with patch("ddgs.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text = fake_text
            out = spec.handler({"query": "zxcvbnm no results"})

        assert "No results found" in out

    def test_network_error_returns_error(self):
        spec = make_web_search_spec()

        def fake_text(query, safesearch="off", backend="auto", max_results=8):
            raise ConnectionError("Connection timed out")

        with patch("ddgs.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text = fake_text
            out = spec.handler({"query": "something"})

        assert out.startswith("Error:")
        assert "Search failed" in out
        assert "Connection timed out" in out

    def test_max_results_respected(self):
        spec = make_web_search_spec()
        fake_results = [{"title": f"Result {i}", "href": f"https://example.com/{i}", "body": f"Body {i}"} for i in range(8)]

        def fake_text(query, safesearch="off", backend="auto", max_results=8):
            return fake_results

        with patch("ddgs.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text = fake_text
            out = spec.handler({"query": "test"})

        # 8 results should produce 8 numbered items
        for i in range(1, 9):
            assert f"{i}." in out

    def test_missing_fields_in_result_uses_defaults(self):
        spec = make_web_search_spec()
        fake_results = [{}]

        def fake_text(query, safesearch="off", backend="auto", max_results=8):
            return fake_results

        with patch("ddgs.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text = fake_text
            out = spec.handler({"query": "test"})

        assert "(no title)" in out
        assert "\n   \n" in out  # empty href between lines

    def test_tool_metadata(self):
        spec = make_web_search_spec()
        assert spec.name == "web_search"
        assert "Search the web" in spec.description
        assert "Include the current year" in spec.description
        assert "2025" in spec.description or "recent" in spec.description
        assert spec.schema["required"] == ["query"]
        assert "safesearch" not in spec.schema["properties"]
        assert "backend" not in spec.schema["properties"]
