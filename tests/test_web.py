"""Tests for the web fetch engine (no network access)."""

import httpx
import pytest

from archie.web import (
    FetchError,
    classify_content,
    fetch,
    html_to_markdown,
    rewrite_github_url,
)

# --- GitHub URL rewriting ---


class TestRewriteGithubUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://github.com/owner/repo/blob/main/README.md",
                "https://raw.githubusercontent.com/owner/repo/main/README.md",
            ),
            (
                "https://github.com/owner/repo/blob/abc123/src/deep/file.py",
                "https://raw.githubusercontent.com/owner/repo/abc123/src/deep/file.py",
            ),
            (
                "https://github.com/owner/repo/raw/main/data.json",
                "https://raw.githubusercontent.com/owner/repo/main/data.json",
            ),
            (
                "https://gist.github.com/user/abc123def456",
                "https://gist.github.com/user/abc123def456/raw",
            ),
            (
                "https://gist.github.com/user/abc123def456/",
                "https://gist.github.com/user/abc123def456/raw",
            ),
        ],
    )
    def test_rewrites_file_views(self, url, expected):
        result, changed = rewrite_github_url(url)
        assert result == expected
        assert changed is True

    def test_strips_line_fragment(self):
        result, changed = rewrite_github_url("https://github.com/owner/repo/blob/main/x.py#L10-L20")
        assert result == "https://raw.githubusercontent.com/owner/repo/main/x.py"
        assert changed is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/owner/repo",
            "https://github.com/owner/repo/issues/42",
            "https://github.com/owner/repo/pull/7",
            "https://github.com/owner/repo/tree/main/src",
            "https://github.com/owner/repo/releases",
            "https://example.com/blob/main/file.txt",
            "https://docs.python.org/3/library/os.html",
        ],
    )
    def test_leaves_non_file_views(self, url):
        result, changed = rewrite_github_url(url)
        assert result == url
        assert changed is False


# --- Content classification ---


class TestClassifyContent:
    @pytest.mark.parametrize(
        "ct,expected",
        [
            ("text/html", "html"),
            ("text/html; charset=utf-8", "html"),
            ("application/xhtml+xml", "html"),
            ("text/plain", "text"),
            ("text/markdown", "text"),
            ("application/json", "text"),
            ("application/yaml", "text"),
            ("application/vnd.api+json", "text"),
            ("image/svg+xml", "text"),
            ("image/png", "binary"),
            ("application/pdf", "binary"),
            ("application/zip", "binary"),
            ("audio/mpeg", "binary"),
        ],
    )
    def test_by_content_type(self, ct, expected):
        assert classify_content(ct, b"") == expected

    def test_sniff_generic_text(self):
        assert classify_content("application/octet-stream", b"hello world") == "text"

    def test_sniff_generic_binary(self):
        assert classify_content("application/octet-stream", b"PK\x03\x04\x00\x00") == "binary"

    def test_sniff_missing_header_html(self):
        assert classify_content("", b"<!DOCTYPE html><html><body>x") == "html"

    def test_sniff_missing_header_text(self):
        assert classify_content("", b"just some text") == "text"


# --- Fetching (via MockTransport) ---


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"User-Agent": "test"},
    )


class TestFetch:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(FetchError, match="scheme"):
            fetch("file:///etc/passwd")

    def test_successful_text(self):
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="hi")

        result = fetch("https://example.com/x", client=_client(handler))
        assert result.status == 200
        assert result.content_type == "text/plain"
        assert result.body == b"hi"

    def test_non_2xx_raises_readable(self):
        def handler(request):
            return httpx.Response(404, text="nope")

        with pytest.raises(FetchError, match=r"HTTP 404.*example.com"):
            fetch("https://example.com/missing", client=_client(handler))

    def test_size_cap_aborts(self):
        def handler(request):
            return httpx.Response(200, content=b"x" * 5000)

        with pytest.raises(FetchError, match="size limit"):
            fetch("https://example.com/big", max_bytes=1000, client=_client(handler))

    def test_network_error_readable(self):
        def handler(request):
            raise httpx.ConnectError("dns fail")

        with pytest.raises(FetchError, match="Network error"):
            fetch("https://nope.invalid/x", client=_client(handler))

    def test_strips_content_type_params(self):
        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=UTF-8"}, text="<html>"
            )

        result = fetch("https://example.com/x", client=_client(handler))
        assert result.content_type == "text/html"


# --- HTML extraction ---


class TestHtmlToMarkdown:
    def test_extracts_main_content_drops_boilerplate(self):
        html = """
        <html><body>
          <nav>Home About Contact</nav>
          <header>Site Header Junk</header>
          <article>
            <h1>Real Title</h1>
            <p>This is the actual article content that matters.</p>
          </article>
          <aside>Sidebar ad nonsense</aside>
          <footer>Copyright footer</footer>
        </body></html>
        """
        md, extracted = html_to_markdown(html, "https://example.com/a")
        assert extracted is True
        assert "Real Title" in md
        assert "actual article content" in md
        assert "Sidebar ad" not in md
        assert "Copyright footer" not in md

    def test_preserves_code_and_tables(self):
        html = """
        <html><body><article>
          <h1>Doc</h1>
          <pre><code>def foo():\n    return 1</code></pre>
          <table>
            <tr><th>A</th><th>B</th></tr>
            <tr><td>1</td><td>2</td></tr>
          </table>
        </article></body></html>
        """
        md, extracted = html_to_markdown(html, "https://example.com/b")
        assert extracted is True
        assert "foo" in md

    def test_fallback_on_empty_extraction(self):
        # A bare JS shell with no extractable article content.
        html = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"
        md, extracted = html_to_markdown(html, "https://example.com/spa")
        assert extracted is False
