# Plan 015: Web Fetch Tool

## Objective

Add a `web_fetch` tool that downloads content from http(s) URLs. Text-like content is returned directly to the model; HTML pages get main-content extraction (drop nav, headers, footers, sidebars) and conversion to markdown; binary content (images, documents, archives) is saved to disk and reported by path. GitHub web URLs are rewritten to their raw equivalents so the model gets file contents instead of GitHub's HTML chrome.

## Context

- The roadmap (Phase 4) lists "Web fetch/search tools" — this is the fetch half. Search is out of scope.
- Today the model's only route to the web is `curl` via the shell tool. That works but:
  - HTML pages come back as raw markup — enormous token waste (a typical docs page is 100-500KB of HTML for 2-5KB of actual content)
  - GitHub blob URLs return the full GitHub web app (~1MB+) instead of the file
  - Binary downloads via curl are fine, but there's no consistent path/size/type reporting
- Tool results are capped at 4KB by `truncate_result()` in `agent.py`, with full content preserved in the `ArtifactStore` — so a clean markdown conversion matters: it determines what survives into context.
- The fetch runs on the **host**, not in the sandbox. The HTML extraction needs Python libraries, and the tool framework's handlers are host-side Python (same as read_file/search_files). The sandbox is for arbitrary command execution; this is a structured, bounded operation.

## Requirements

### Fetching

- MUST fetch content from http and https URLs with sane limits
  - AC: configurable timeout (default 30s), follows redirects (max 5)
  - AC: download size capped (default 10MB) — abort with a clear error beyond that, don't buffer unbounded
  - AC: non-2xx responses return `Error: HTTP 404 Not Found for <url>` style messages, not exceptions
  - AC: network errors (DNS failure, timeout, TLS) return readable error strings
- MUST send a real User-Agent header (some sites reject default client UAs)
- MUST reject non-http(s) schemes (`file://`, `ftp://`, etc.)
  - AC: `file:///etc/passwd` returns an error — this is the path-allowlist equivalent for URLs

### Content handling

- MUST detect content type from the `Content-Type` response header, falling back to sniffing (magic bytes / null-byte check) when the header is missing or generic (`application/octet-stream`)
- MUST return text-like content (plain text, code, JSON, XML, YAML, CSV, markdown) directly as the tool result
  - AC: result includes a metadata header: final URL (post-redirect), content type, size
  - AC: oversized text is truncated by the existing `truncate_result()` path; full content lands in the artifact store as usual
- MUST extract main content from HTML and convert to markdown
  - AC: navigation, headers, footers, sidebars, cookie banners are absent from output
  - AC: links, headings, lists, code blocks, and tables survive conversion
  - AC: if extraction yields nothing (JS-rendered SPA, paywall), fall back to a stripped-tags text conversion and say so in the result
- MUST save binary content (images, PDFs, archives, audio/video, executables) to disk instead of returning bytes
  - AC: result reports saved path, content type, and size — no binary in context
  - AC: optional `save_path` parameter lets the model choose the destination (validated via `validate_path`, same allowlist as write_file)
  - AC: without `save_path`, binaries go to `<cwd>/.archie/downloads/<filename>` with filename derived from the URL (sanitised, collision-suffixed)
- SHOULD honour `save_path` for text content too — "download this file to X" should work regardless of type (content is saved AND a preview returned)

### GitHub URL rewriting

- MUST rewrite GitHub file URLs to their raw equivalents before fetching:
  - `github.com/{owner}/{repo}/blob/{ref}/{path}` → `raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}`
  - `github.com/{owner}/{repo}/raw/{ref}/{path}` → same raw host form
  - `gist.github.com/{user}/{id}` → `gist.github.com/{user}/{id}/raw` (latest revision, first file)
- MUST NOT rewrite GitHub URLs that aren't file views — repo roots, `/tree/` directory listings, issues, PRs, releases, wiki pages all fetch as-is (HTML → markdown path handles them)
  - AC: `github.com/owner/repo/issues/42` is fetched as HTML and converted to markdown
- AC: the result metadata shows the rewritten URL so the model knows what was actually fetched

## Design

### Overview

One new tool module (`tools/web_fetch.py`) plus a small support module (`web.py`) holding the fetch/extract/rewrite logic — same engine/wrapper split as `code_intel.py` / `tools/code.py`. The engine is pure functions (no tool framework imports), unit-testable without network via injected responses.

### Code structure

- `src/archie/web.py` — `rewrite_github_url()`, `fetch()` (returns a `FetchResult` dataclass: final_url, status, content_type, body bytes), `html_to_markdown()`, `classify_content()` (text / html / binary)
- `src/archie/tools/web_fetch.py` — `make_web_fetch_spec(cwd, allowed_directories)`: parameter handling, save-to-disk logic, result formatting
- `src/archie/tools/__init__.py` — register in `create_default_registry()`
- `pyproject.toml` — new dependencies

### Key decisions

- **`httpx` for fetching** — modern, typed, supports streaming reads (needed for the size cap: read chunks, abort past the limit). `requests` would also work but httpx is the better default for new code.
- **`trafilatura` for HTML extraction** — single dependency that does both main-content extraction (boilerplate removal) and markdown output (`extract(html, output_format="markdown", include_links=True, include_tables=True)`). Alternatives considered: `readability-lxml` + `markdownify` (two deps, more glue code), `beautifulsoup4` alone (no boilerplate removal — we'd be writing heuristics ourselves). Trafilatura is what most agent harnesses converge on.
- **Fallback when trafilatura returns None** — use `trafilatura.html2txt()` (strip-tags conversion) and prefix the result with a note: `[main-content extraction failed; showing full page text]`. The model can decide whether it's useful.
- **Host-side execution, scheme allowlist as the security boundary** — http(s) only. No private-IP/SSRF blocking: this is a personal tool on a dev machine, the model can already `curl` anything from the sandbox, and the threat model doesn't include hostile prompts driving internal network pivots. Noted explicitly so it's a conscious decision, not an oversight.
- **Binary content never enters context** — even small images. Bedrock supports image blocks in tool results, but the tool framework's `handler: Callable[[dict], str]` contract is string-only. Returning images to the model's vision input is a possible future enhancement (would need a framework change); out of scope here.
- **Default download location `<cwd>/.archie/downloads/`** — inside the project so it's visible to other tools (read_file, shell) and inside the sandbox mount, but namespaced so it's obviously archie-generated. Created on demand. Should be in the user's global gitignore or noted in the result.
- **GitHub rewriting is pure URL string manipulation** — no GitHub API calls, no auth. Private repos will 404 on the raw host; the error message suggests using the shell tool with `gh` for private content.
- **No response caching** — pages change, and the artifact store already preserves what was fetched this session. Revisit if usage shows repeated fetches of the same URL.

### Schema

```json
{
  "name": "web_fetch",
  "description": "Fetch content from a web URL. HTML pages are converted to markdown (main content only). Text and code are returned directly. Binary files (images, PDFs, etc.) are saved to disk and the path returned. GitHub file URLs are automatically fetched in raw form.",
  "schema": {
    "type": "object",
    "properties": {
      "url": {
        "type": "string",
        "description": "The http(s) URL to fetch."
      },
      "save_path": {
        "type": "string",
        "description": "Optional path to save the downloaded content to (relative to working directory). Required only when you want the file on disk; binary content is saved automatically."
      }
    },
    "required": ["url"]
  }
}
```

### Result formats

Text/HTML:
```
URL: https://raw.githubusercontent.com/owner/repo/main/README.md (rewritten from github.com/owner/repo/blob/main/README.md)
Type: text/markdown · 4.2KB

<content>
```

Binary:
```
URL: https://example.com/diagram.png
Type: image/png · 213KB
Saved to: .archie/downloads/diagram.png
```

## Milestones

### 1. Fetch engine and GitHub rewriting

Approach:
- Create `src/archie/web.py` with `rewrite_github_url(url) -> tuple[str, bool]` (rewritten URL + whether it changed) and `fetch(url, timeout, max_bytes) -> FetchResult`
- `fetch()` uses `httpx.Client` with `follow_redirects=True`, streams the body with a running byte count, raises a typed error past `max_bytes`
- `classify_content(content_type, body) -> Literal["text", "html", "binary"]` — content-type map first, sniff fallback (decode attempt + null-byte check, mirroring read_file's binary detection)

Tasks:
- Add `httpx` to pyproject dependencies
- Implement `rewrite_github_url` covering blob/raw/gist forms and the non-rewrite cases
- Implement `fetch` with timeout, redirect, and size-cap handling
- Implement `classify_content`
- Unit tests: rewrite table (parametrised — blob, raw, gist, issues, tree, repo root, non-github), classification table; fetch tests via `httpx.MockTransport`

Deliverable: `web.py` engine fetches, rewrites, and classifies — no tool wiring yet.

Verify: `uv run pytest tests/test_web.py` passes with no network access.

### 2. HTML extraction to markdown

Approach:
- Add `html_to_markdown(html, url) -> tuple[str, bool]` to `web.py` — trafilatura extraction with markdown output, `html2txt` fallback, bool indicates whether extraction succeeded
- Pass the source URL to trafilatura (it uses it to resolve relative links)

Tasks:
- Add `trafilatura` to pyproject dependencies
- Implement `html_to_markdown` with fallback path
- Unit tests with small HTML fixtures: page with nav/footer/sidebar (assert boilerplate absent, content present), page with code blocks and tables, empty/JS-shell page (assert fallback notice)

Deliverable: HTML in, clean markdown out, graceful fallback.

Verify: `uv run pytest` passes. Manual spot check in a REPL against a real docs page — output should read like the page's article content.

### 3. Tool wrapper and registration

Approach:
- `src/archie/tools/web_fetch.py` with `make_web_fetch_spec(cwd, allowed_directories)` following the existing closure pattern
- Handler flow: validate scheme → rewrite GitHub URL → fetch → classify → (html? extract to markdown) → (save_path or binary? write to disk via `validate_path`) → format result with metadata header
- Default binary filename: last URL path segment, sanitised (strip query strings, path separators); fall back to `download` + extension guessed from content type; suffix `-1`, `-2` on collision
- Register in `create_default_registry()` (unconditional — needs no sandbox/brain/store)

Tasks:
- Implement handler and spec
- Implement save-to-disk with filename derivation and collision handling
- Register the tool
- Unit tests with a mocked `fetch`: text passthrough, html→markdown, binary→saved file, save_path honoured for text, save_path outside allowlist rejected, http error formatting, scheme rejection

Deliverable: model can call `web_fetch` end to end.

Verify: `uv run pytest` passes. `uv run archie chat`, ask it to fetch (a) a GitHub blob URL — result shows the raw.githubusercontent rewrite and file content; (b) a docs page — result is markdown without nav junk; (c) an image URL — result reports a path under `.archie/downloads/`, and the file opens.
