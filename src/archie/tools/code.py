"""code tool — structural code intelligence via tree-sitter.

Provides three operations for understanding code structure without
reading full file contents:
- outline: all symbols in a file (classes, functions, signatures)
- search: find symbol definitions by name across the project
- overview: high-level project structure

This dramatically reduces tokens for code exploration tasks. Instead of
reading a 300-line file to understand its structure (~1000 tokens),
the model gets a structured outline (~100 tokens).
"""

from pathlib import Path

from archie.code_intel import CodeIndex, Symbol
from archie.tools import ToolSpec, tool_error, tool_result, validate_path


def make_code_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a code ToolSpec with a project-scoped CodeIndex."""
    index = CodeIndex(cwd)

    def handler(params: dict) -> str:
        operation = params.get("operation", "")

        match operation:
            case "outline":
                return _handle_outline(params, index, cwd, allowed_directories)
            case "search":
                return _handle_search(params, index, cwd)
            case "overview":
                return _handle_overview(params, index, cwd)
            case _:
                return tool_error(f"Unknown operation: {operation}. Use: outline, search, overview")

    return ToolSpec(
        name="code",
        description=(
            "Structural code intelligence. Use 'outline' to see all symbols in a file "
            "(classes, functions, signatures) without reading full content. Use 'search' "
            "to find where symbols are defined by name. Use 'overview' for high-level "
            "project structure. Prefer this over read_file for understanding code structure."
        ),
        schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["outline", "search", "overview"],
                    "description": "outline: symbols in a file. search: find definitions by name. overview: project structure.",
                },
                "path": {
                    "type": "string",
                    "description": "File path (for outline) or directory (for search/overview). Relative to project root.",
                },
                "name": {
                    "type": "string",
                    "description": "Symbol name to search for (search operation). Case-insensitive substring match.",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by language: python, typescript, javascript, php, go, rust, css, hcl.",
                },
            },
            "required": ["operation"],
        },
        handler=handler,
        self_truncating=True,
    )


def _handle_outline(params: dict, index: CodeIndex, cwd: Path, allowed: list[Path]) -> str:
    path_str = params.get("path", "")
    if not path_str:
        return tool_error("'path' is required for outline operation.")

    try:
        resolved = validate_path(path_str, cwd, allowed)
    except ValueError as e:
        return tool_error(str(e))

    if resolved.is_dir():
        return tool_error(
            f"'{path_str}' is a directory. Use operation='overview' with path='{path_str}' "
            "to see project structure, or pass a specific file for outline."
        )

    if not resolved.is_file():
        # Suggest files with matching basename
        basename = Path(path_str).name
        candidates = [f for f in index._discover_files() if f.name == basename]
        if candidates:
            suggestions = []
            for c in candidates[:3]:
                try:
                    suggestions.append(str(c.relative_to(cwd)))
                except ValueError:
                    suggestions.append(str(c))
            return tool_error(
                f"File not found: {path_str}. Did you mean: {', '.join(suggestions)}?"
            )
        return tool_error(f"File not found: {path_str}")

    symbols = index.outline(resolved)
    if not symbols:
        return tool_result(f"No symbols found in {path_str} (unsupported language or empty file).")

    # Count lines for the header
    try:
        line_count = resolved.read_text(errors="replace").count("\n") + 1
    except OSError:
        line_count = 0

    ext = resolved.suffix
    from archie.code_intel import _EXTENSION_MAP

    lang_name = _EXTENSION_MAP.get(ext, ("unknown",))[0]

    lines = [f"{path_str} ({lang_name}, {line_count} lines)", ""]
    _format_symbols(symbols, lines, indent=0)
    return tool_result("\n".join(lines))


def _handle_search(params: dict, index: CodeIndex, cwd: Path) -> str:
    name = params.get("name", "")
    if not name:
        return tool_error("'name' is required for search operation.")

    path_str = params.get("path", "")
    if path_str:
        from archie.tools import CONTAINER_PROJECT_ROOT

        if path_str == CONTAINER_PROJECT_ROOT or path_str.startswith(CONTAINER_PROJECT_ROOT + "/"):
            relative = path_str[len(CONTAINER_PROJECT_ROOT):].lstrip("/")
            path = Path(relative) if relative else None
        else:
            path = Path(path_str)
    else:
        path = None
    language = params.get("language")

    results = index.search(name, path, language)
    if not results:
        return tool_result(f'No symbols found matching "{name}".')

    lines = [f'Found {len(results)} match{"es" if len(results) != 1 else ""} for "{name}":', ""]
    for file_path, sym in results:
        try:
            rel = file_path.relative_to(cwd)
        except ValueError:
            rel = file_path
        lines.append(f"{rel}:{sym.line} — {sym.signature}")

    return tool_result("\n".join(lines))


def _handle_overview(params: dict, index: CodeIndex, cwd: Path) -> str:
    path_str = params.get("path", "")
    if path_str:
        from archie.tools import CONTAINER_PROJECT_ROOT

        if path_str == CONTAINER_PROJECT_ROOT or path_str.startswith(CONTAINER_PROJECT_ROOT + "/"):
            # Strip /workspace prefix — CodeIndex uses host-relative paths
            relative = path_str[len(CONTAINER_PROJECT_ROOT):].lstrip("/")
            path = Path(relative) if relative else None
        else:
            path = Path(path_str)
    else:
        path = None
    data = index.overview(path)

    languages = data["languages"]
    directories = data["directories"]

    total_files = sum(languages.values())
    if not total_files:
        return tool_result("No supported source files found.")

    # Header
    lines = [f"Project: {cwd.name}", f"Files: {total_files} source files"]

    # Language breakdown
    lang_parts = []
    for lang, count in sorted(languages.items(), key=lambda x: -x[1]):
        pct = int(count / total_files * 100)
        lang_parts.append(f"{lang} ({pct}%)")
    lines.append(f"Languages: {', '.join(lang_parts)}")
    lines.append("")

    # Directory structure with symbols
    for dir_path in sorted(directories.keys()):
        syms = directories[dir_path]
        dir_str = str(dir_path) if str(dir_path) != "." else cwd.name
        sym_summary = ", ".join(syms[:5])
        if len(syms) > 5:
            sym_summary += f" (+{len(syms) - 5} more)"
        lines.append(f"  {dir_str}/ — {sym_summary}")

    return tool_result("\n".join(lines))


def _format_symbols(symbols: list[Symbol], lines: list[str], indent: int) -> None:
    """Format symbols hierarchically with indentation."""
    prefix = "    " * indent
    for sym in symbols:
        lines.append(f"{prefix}{sym.signature} [line {sym.line}]")
        if sym.children:
            _format_symbols(sym.children, lines, indent + 1)
