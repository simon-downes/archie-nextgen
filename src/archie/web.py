"""Web fetch engine — pure functions for fetching, rewriting, and classifying URLs.

This module holds the network/parsing logic for the web_fetch tool, kept
separate from the tool wrapper (tools/web_fetch.py) so it can be unit-tested
without the tool framework — same engine/wrapper split as code_intel.py /
tools/code.py.

Responsibilities:
- rewrite_github_url: rewrite GitHub file-view URLs to their raw equivalents
- fetch: download a URL with timeout, redirect, and size-cap handling
- classify_content: decide whether a response is text, html, or binary
- html_to_markdown: extract main content from HTML and convert to markdown

Security boundary: scheme allowlist (http/https only). No private-IP/SSRF
blocking — this is a personal tool on a dev machine and the model can already
reach the network via the shell tool (see plan 015 design notes).
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

# A real browser-ish User-Agent — some sites reject default client UAs.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 archie-web-fetch/1.0"
)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_MAX_REDIRECTS = 5

# Content types that should be returned to the model as text. Anything not
# matching these (and not text/* or +json/+xml suffixes) is treated as binary.
_TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/csv",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-sh",
    "application/toml",
    "application/x-toml",
    "image/svg+xml",  # SVG is XML text, useful to read (so classified text, never binary)
}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}

# Content types that are generic enough that we should sniff the body instead.
_GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", ""}

ContentClass = Literal["text", "html", "binary"]


class FetchError(Exception):
    """Raised for fetch failures that should surface as readable tool errors."""


@dataclass
class FetchResult:
    """Outcome of a successful fetch.

    Attributes:
        final_url: URL after following redirects.
        status: HTTP status code.
        content_type: The bare content-type (no charset), lowercased.
        body: Raw response bytes.
    """

    final_url: str
    status: int
    content_type: str
    body: bytes


# --- GitHub URL rewriting ---

# github.com/{owner}/{repo}/blob/{ref}/{path...}  -> raw.githubusercontent.com/{owner}/{repo}/{ref}/{path...}
# github.com/{owner}/{repo}/raw/{ref}/{path...}   -> same
_GH_BLOB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/(?:blob|raw)/(.+)$",
)
# gist.github.com/{user}/{id}  (no trailing path) -> .../raw
_GH_GIST_RE = re.compile(
    r"^https?://gist\.github\.com/([^/]+)/([0-9a-fA-F]+)/?$",
)


def rewrite_github_url(url: str) -> tuple[str, bool]:
    """Rewrite GitHub file-view URLs to their raw equivalents.

    Only file views are rewritten:
    - blob/raw file URLs -> raw.githubusercontent.com
    - gist root URLs -> gist .../raw (latest revision, first file)

    Repo roots, /tree/ listings, issues, PRs, releases, wiki pages etc. are
    returned unchanged (the HTML->markdown path handles them).

    Returns:
        (url, changed) — the (possibly rewritten) URL and whether it changed.
    """
    m = _GH_BLOB_RE.match(url)
    if m:
        owner, repo, rest = m.groups()
        # `rest` is "{ref}/{path}". Strip any fragment (e.g. #L10-L20).
        rest = rest.split("#", 1)[0]
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}", True

    m = _GH_GIST_RE.match(url)
    if m:
        user, gist_id = m.groups()
        return f"https://gist.github.com/{user}/{gist_id}/raw", True

    return url, False


# --- Fetching ---


def fetch(
    url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Fetch a URL with sane limits.

    Args:
        url: The http(s) URL to fetch.
        timeout: Per-request timeout in seconds.
        max_bytes: Abort the download past this many body bytes.
        client: Optional pre-built httpx.Client (used for testing via
            MockTransport). When None, a client is created and closed here.

    Returns:
        A FetchResult on a 2xx response.

    Raises:
        FetchError: scheme rejection, non-2xx status, network error, or the
            body exceeding max_bytes. Message is human-readable.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise FetchError(
            f"Unsupported URL scheme '{scheme or '(none)'}': only http and https are allowed."
        )

    own_client = client is None
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )

    try:
        with client.stream("GET", url) as response:
            if not (200 <= response.status_code < 300):
                reason = response.reason_phrase or ""
                raise FetchError(f"HTTP {response.status_code} {reason}".rstrip() + f" for {url}")

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(
                        f"Response exceeds size limit ({max_bytes} bytes) for {url}. "
                        "Use the shell tool with curl if you need the full download."
                    )
                chunks.append(chunk)

            content_type = response.headers.get("content-type", "")
            # Strip charset/parameters and lowercase.
            content_type = content_type.split(";", 1)[0].strip().lower()
            return FetchResult(
                final_url=str(response.url),
                status=response.status_code,
                content_type=content_type,
                body=b"".join(chunks),
            )
    except httpx.HTTPError as e:
        raise FetchError(f"Network error fetching {url}: {e}") from e
    finally:
        if own_client:
            client.close()


# --- Content classification ---


def classify_content(content_type: str, body: bytes) -> ContentClass:
    """Classify a response as text, html, or binary.

    Uses the content-type header first; falls back to sniffing the body when
    the header is missing or generic (application/octet-stream).

    Sniffing mirrors read_file's binary detection: a null byte in the first
    8KB means binary; otherwise treat decodable content as text.
    """
    ct = content_type.split(";", 1)[0].strip().lower()

    if ct in _HTML_TYPES:
        return "html"
    if ct in _TEXT_TYPES or ct.startswith("text/"):
        # text/html is caught above; other text/* is text.
        return "text"
    if ct.endswith("+json") or ct.endswith("+xml"):
        return "text"

    if ct in _GENERIC_TYPES:
        return _sniff(body)

    # A specific, non-text content type (image/png, application/pdf, ...).
    return "binary"


def _sniff(body: bytes) -> ContentClass:
    """Sniff body bytes when the content-type is unhelpful."""
    sample = body[:8192]
    if b"\x00" in sample:
        return "binary"
    # Cheap HTML detection on the decoded head.
    try:
        head = sample.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    lowered = head.lstrip().lower()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html") or "<body" in lowered:
        return "html"
    return "text"


# --- HTML extraction ---


def html_to_markdown(html: str, url: str | None = None) -> tuple[str, bool]:
    """Extract main content from HTML and convert to markdown.

    Uses trafilatura for boilerplate removal + markdown output. Falls back to
    a strip-tags text conversion when extraction yields nothing (JS-rendered
    SPA, paywall, etc.).

    Args:
        html: The raw HTML string.
        url: Source URL — passed to trafilatura to resolve relative links.

    Returns:
        (markdown, extracted) — the converted text and whether main-content
        extraction succeeded (False means the fallback path was used).
    """
    # Imported lazily: trafilatura (and its lxml dependency) is heavy to import,
    # and most agent turns never fetch HTML — keeps `archie` startup fast.
    import trafilatura

    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
    )
    if extracted:
        return extracted, True

    # Fallback: strip-tags conversion.
    fallback = trafilatura.html2txt(html) or ""
    return fallback, False
