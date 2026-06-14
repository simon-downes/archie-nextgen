"""Tests for the web_fetch tool wrapper (fetch mocked)."""

from unittest.mock import patch

from archie.tools.web_fetch import make_web_fetch_spec
from archie.web import FetchError, FetchResult


def _spec(cwd, allowed=None):
    return make_web_fetch_spec(cwd, allowed or [])


def _fetch_returning(result):
    """Build a fake fetch() that ignores args and returns `result`."""

    def fake(url, *args, **kwargs):
        return result

    return fake


class TestWebFetchTool:
    def test_text_passthrough(self, tmp_path):
        spec = _spec(tmp_path)
        result = FetchResult(
            final_url="https://example.com/x.txt",
            status=200,
            content_type="text/plain",
            body=b"hello world",
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/x.txt"})
        assert "URL: https://example.com/x.txt" in out
        assert "text/plain" in out
        assert "hello world" in out

    def test_html_to_markdown(self, tmp_path):
        spec = _spec(tmp_path)
        html = "<html><body><article><h1>Hi</h1><p>Body text here.</p></article></body></html>"
        result = FetchResult(
            final_url="https://example.com/page",
            status=200,
            content_type="text/html",
            body=html.encode(),
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/page"})
        assert "from HTML" in out
        assert "Body text here." in out

    def test_binary_saved_to_default_dir(self, tmp_path):
        spec = _spec(tmp_path)
        result = FetchResult(
            final_url="https://example.com/diagram.png",
            status=200,
            content_type="image/png",
            body=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/diagram.png"})
        assert "Saved to:" in out
        assert "image/png" in out
        saved = tmp_path / ".archie/downloads/diagram.png"
        assert saved.exists()
        assert saved.read_bytes() == result.body
        # No binary in the returned text.
        assert "\x89PNG" not in out

    def test_binary_filename_collision_suffix(self, tmp_path):
        spec = _spec(tmp_path)
        existing = tmp_path / ".archie/downloads/diagram.png"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old")
        result = FetchResult(
            final_url="https://example.com/diagram.png",
            status=200,
            content_type="image/png",
            body=b"new",
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/diagram.png"})
        assert (tmp_path / ".archie/downloads/diagram-1.png").exists()
        assert "diagram-1.png" in out

    def test_save_path_honoured_for_text(self, tmp_path):
        spec = _spec(tmp_path)
        result = FetchResult(
            final_url="https://example.com/data.json",
            status=200,
            content_type="application/json",
            body=b'{"k": 1}',
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler(
                {"url": "https://example.com/data.json", "save_path": "out/data.json"}
            )
        saved = tmp_path / "out/data.json"
        assert saved.exists()
        assert saved.read_text() == '{"k": 1}'
        assert "Saved to:" in out
        assert '{"k": 1}' in out  # preview still returned

    def test_save_path_outside_allowlist_rejected(self, tmp_path):
        spec = _spec(tmp_path)
        result = FetchResult(
            final_url="https://example.com/x.txt",
            status=200,
            content_type="text/plain",
            body=b"hi",
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/x.txt", "save_path": "/etc/evil.txt"})
        assert out.startswith("Error:")
        assert "outside allowed directories" in out

    def test_http_error_formatting(self, tmp_path):
        spec = _spec(tmp_path)

        def boom(url, *a, **k):
            raise FetchError("HTTP 404 Not Found for https://example.com/missing")

        with patch("archie.tools.web_fetch.fetch", boom):
            out = spec.handler({"url": "https://example.com/missing"})
        assert out.startswith("Error:")
        assert "404" in out

    def test_scheme_rejection(self, tmp_path):
        spec = _spec(tmp_path)

        def boom(url, *a, **k):
            raise FetchError("Unsupported URL scheme 'file': only http and https are allowed.")

        with patch("archie.tools.web_fetch.fetch", boom):
            out = spec.handler({"url": "file:///etc/passwd"})
        assert out.startswith("Error:")
        assert "scheme" in out

    def test_github_rewrite_shown_in_metadata(self, tmp_path):
        spec = _spec(tmp_path)
        result = FetchResult(
            final_url="https://raw.githubusercontent.com/o/r/main/README.md",
            status=200,
            content_type="text/markdown",
            body=b"# Readme",
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://github.com/o/r/blob/main/README.md"})
        assert "rewritten from https://github.com/o/r/blob/main/README.md" in out
        assert "# Readme" in out

    def test_missing_url(self, tmp_path):
        spec = _spec(tmp_path)
        out = spec.handler({"url": ""})
        assert out.startswith("Error:")

    def test_html_fallback_notice_surfaces(self, tmp_path):
        spec = _spec(tmp_path)
        # JS-shell page: trafilatura extraction yields nothing -> fallback path.
        html = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"
        result = FetchResult(
            final_url="https://example.com/spa",
            status=200,
            content_type="text/html",
            body=html.encode(),
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/spa"})
        assert "[main-content extraction failed; showing full page text]" in out

    def test_filename_derived_when_no_segment(self, tmp_path):
        spec = _spec(tmp_path)
        # URL ends in '/', no usable last segment -> 'download' + ext from type.
        result = FetchResult(
            final_url="https://example.com/",
            status=200,
            content_type="image/png",
            body=b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/"})
        saved = tmp_path / ".archie/downloads/download.png"
        assert saved.exists()
        assert "download.png" in out

    def test_final_url_after_redirect_shown(self, tmp_path):
        spec = _spec(tmp_path)
        # Request URL differs from final_url (redirect followed by fetch()).
        result = FetchResult(
            final_url="https://example.com/landed",
            status=200,
            content_type="text/plain",
            body=b"hi",
        )
        with patch("archie.tools.web_fetch.fetch", _fetch_returning(result)):
            out = spec.handler({"url": "https://example.com/start"})
        assert "URL: https://example.com/landed" in out
