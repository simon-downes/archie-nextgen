"""web_fetch tool — download and present web content.

Fetches http(s) URLs and presents the content in the most useful form:
- Text/code/JSON/etc. is returned directly.
- HTML is converted to markdown (main content only — nav/footer/sidebar dropped).
- Binary content (images, PDFs, archives) is saved to disk; only the path,
  type, and size are returned (binary never enters the model's context).
- GitHub file URLs are rewritten to their raw equivalents before fetching.

The fetch runs on the host (same as read_file/search_files), not in the
sandbox — it's a structured, bounded operation, not arbitrary execution.
See plan 015 for the security rationale (scheme allowlist, no SSRF blocking).
"""

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from archie.tools import ToolSpec, tool_error, tool_result, validate_path
from archie.web import (
    FetchError,
    classify_content,
    fetch,
    html_to_markdown,
    rewrite_github_url,
)

# Default location for auto-saved binaries: inside the project (visible to
# other tools and inside the sandbox mount) but namespaced as archie-generated.
_DOWNLOAD_SUBDIR = ".archie/downloads"

# Map a handful of common content types to extensions when deriving a filename
# from a URL that has no usable extension.
_EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    # NB: no image/svg+xml here — SVG classifies as text, so it never takes the
    # binary save path where this map is used.
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "audio/mpeg": ".mp3",
    "video/mp4": ".mp4",
}


def make_web_fetch_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a web_fetch ToolSpec bound to the given path constraints."""

    def handler(params: dict) -> str:
        url = params.get("url", "").strip()
        if not url:
            return tool_error("'url' is required.")
        save_path = params.get("save_path")

        # Rewrite GitHub file URLs to raw form before fetching.
        fetch_url, rewritten = rewrite_github_url(url)

        # Fetch (scheme rejection / HTTP errors / network errors come back as
        # FetchError with readable messages).
        try:
            result = fetch(fetch_url)
        except FetchError as e:
            return tool_error(str(e))

        kind = classify_content(result.content_type, result.body)

        # Header line documenting what was actually fetched.
        url_line = f"URL: {result.final_url}"
        if rewritten:
            url_line += f" (rewritten from {url})"
        size_str = _human_size(len(result.body))
        ct = result.content_type or "unknown"

        if kind == "binary":
            return _handle_binary(
                result, save_path, fetch_url, url_line, size_str, cwd, allowed_directories
            )
        return _handle_text(
            result, kind, save_path, url_line, size_str, ct, cwd, allowed_directories
        )

    return ToolSpec(
        name="web_fetch",
        description=(
            "Fetch content from a web URL. HTML pages are converted to markdown "
            "(main content only). Text and code are returned directly. Binary files "
            "(images, PDFs, etc.) are saved to disk and the path returned. GitHub file "
            "URLs are automatically fetched in raw form."
        ),
        schema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The http(s) URL to fetch.",
                },
                "save_path": {
                    "type": "string",
                    "description": (
                        "Optional path to save the downloaded content to (relative to "
                        "working directory). Required only when you want the file on disk; "
                        "binary content is saved automatically."
                    ),
                },
            },
            "required": ["url"],
        },
        handler=handler,
        self_truncating=False,  # let truncate_result cap text/markdown output
    )


def _handle_binary(
    result,
    save_path: str | None,
    fetch_url: str,
    url_line: str,
    size_str: str,
    cwd: Path,
    allowed_directories: list[Path],
) -> str:
    """Binary content: always save to disk, never return bytes."""
    ct = result.content_type or "unknown"
    try:
        dest = _resolve_save_path(save_path, fetch_url, result.content_type, cwd)
        resolved_dest = validate_path(str(dest), cwd, allowed_directories)
    except ValueError as e:
        return tool_error(str(e))
    try:
        resolved_dest.parent.mkdir(parents=True, exist_ok=True)
        resolved_dest.write_bytes(result.body)
    except OSError as e:
        return tool_error(f"Cannot save download: {e}")
    rel = _display_path(resolved_dest, cwd)
    return tool_result(f"{url_line}\nType: {ct} · {size_str}\nSaved to: {rel}")


def _handle_text(
    result,
    kind: str,
    save_path: str | None,
    url_line: str,
    size_str: str,
    ct: str,
    cwd: Path,
    allowed_directories: list[Path],
) -> str:
    """Text/HTML: decode, extract markdown if HTML, optionally save to disk."""
    text = result.body.decode("utf-8", errors="replace")
    note = ""
    if kind == "html":
        markdown, extracted = html_to_markdown(text, result.final_url)
        text = markdown
        if not extracted:
            note = "[main-content extraction failed; showing full page text]\n\n"
        ct = "text/markdown (from HTML)"

    # Honour save_path for text too — "download this to X" should work.
    saved_note = ""
    if save_path:
        try:
            resolved_dest = validate_path(save_path, cwd, allowed_directories)
            resolved_dest.parent.mkdir(parents=True, exist_ok=True)
            resolved_dest.write_text(text, encoding="utf-8")
            saved_note = f"\nSaved to: {_display_path(resolved_dest, cwd)}"
        except ValueError as e:
            return tool_error(str(e))
        except OSError as e:
            return tool_error(f"Cannot save download: {e}")

    header = f"{url_line}\nType: {ct} · {size_str}{saved_note}"
    return tool_result(f"{header}\n\n{note}{text}")


def _human_size(n: int) -> str:
    """Format a byte count as a short human-readable string."""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _display_path(resolved: Path, cwd: Path) -> str:
    """Show a path relative to cwd when possible, else absolute."""
    try:
        return str(resolved.relative_to(cwd.resolve()))
    except ValueError:
        return str(resolved)


def _resolve_save_path(save_path: str | None, url: str, content_type: str, cwd: Path) -> Path:
    """Decide where a binary download lands.

    With save_path: that path (validated by the caller).
    Without: <cwd>/.archie/downloads/<filename>, filename derived from the URL
    (sanitised), with a collision suffix.
    """
    if save_path:
        return cwd / save_path

    download_dir = cwd / _DOWNLOAD_SUBDIR
    filename = _filename_from_url(url, content_type)
    candidate = download_dir / filename
    return _dedupe(candidate)


def _filename_from_url(url: str, content_type: str) -> str:
    """Derive a safe filename from a URL's last path segment.

    Strips query strings and path separators; falls back to 'download' plus an
    extension guessed from the content type when there's no usable segment.
    """
    path = urlparse(url).path
    segment = unquote(path.rsplit("/", 1)[-1]) if path else ""
    # Drop anything that isn't filename-safe.
    segment = re.sub(r"[^A-Za-z0-9._-]", "_", segment).strip("._")

    if not segment or "." not in segment:
        ext = _EXT_BY_TYPE.get(content_type, "")
        base = segment or "download"
        segment = f"{base}{ext}"
    return segment


def _dedupe(path: Path) -> Path:
    """Append -1, -2, ... before the extension on collision."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 1
    while True:
        candidate = parent / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1
