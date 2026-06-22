"""code tool — structural code intelligence via tree-sitter.

Provides structural code understanding without reading full file contents:
- File path: enriched outline (imports, classes, functions, line ranges) or full content for small files
- Directory path: recursive outline with adaptive depth
- Add `name` param to search for symbols by name

This reduces tokens for code exploration tasks. Instead of reading a 300-line file
(~1000 tokens), the model gets a structured outline (~100 tokens).
"""

from pathlib import Path

from archie.code_intel import CodeIndex, Symbol
from archie.tools import ToolSpec, tool_error, tool_result, validate_path


def make_code_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a code ToolSpec with a project-scoped CodeIndex."""
    index = CodeIndex(cwd)

    def handler(params: dict) -> str:
        """Unified handler that infers mode from params."""
        path_str = params.get("path", "")
        name_filter = params.get("name", "")
        language = params.get("language")

        # Parse path
        if path_str:
            try:
                resolved = validate_path(path_str, cwd, allowed_directories)
            except ValueError as e:
                return tool_error(str(e))
        else:
            resolved = cwd

        # Dispatch based on inputs
        if name_filter:
            # Search mode
            if resolved.is_dir():
                search_path = resolved
            else:
                search_path = resolved.parent
            return _handle_search(name_filter, search_path, language, index, cwd)
        elif resolved.is_file():
            # File mode: full content or outline
            return _handle_file(resolved, name_filter, index, cwd, allowed_directories)
        else:
            # Directory mode: recursive outline with adaptive depth
            return _handle_directory(resolved, name_filter, language, index, cwd)

    return ToolSpec(
        name="code",
        description=(
            "Structural code intelligence. Returns outlines with line ranges, or full content for small files."
            "\n\n- Use before `read` to understand file/directory structure."
            "\n- File path: enriched outline (imports, classes, functions, fields with line ranges)."
            "\n- Directory path: recursive outline of all files (adaptive depth)."
            "\n- Add `name` param to search for symbols by name."
            "\n- Small files (≤200 lines) return full content automatically."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory path. Relative to project root. Default: project root.",
                },
                "name": {
                    "type": "string",
                    "description": "Filter/search symbols by name. Case-insensitive substring match.",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by language: python, typescript, javascript, php, go, rust, css, hcl.",
                },
            },
        },
        handler=handler,
        self_truncating=True,
    )


def _handle_file(
    file_path: Path,
    name_filter: str,
    index: CodeIndex,
    cwd: Path,
    allowed: list[Path],
) -> str:
    """Handle file mode: return full content if small, else enriched outline."""
    # Count lines
    try:
        content = file_path.read_text(errors="replace")
        line_count = content.count("\n") + 1
    except OSError:
        return tool_error(f"Could not read file: {file_path}")

    ext = file_path.suffix
    from archie.code_intel import _EXTENSION_MAP

    lang_name = _EXTENSION_MAP.get(ext, ("unknown",))[0]

    # Small file: return full content with line numbers
    if line_count <= 200 and not name_filter:
        lines = [
            f"{file_path} ({lang_name}, {line_count} lines)",
            "",
        ]
        for i, line in enumerate(content.splitlines(), start=1):
            lines.append(f"{i:6d}  {line}")
        return tool_result("\n".join(lines))

    # Large file or name filter: return outline
    symbols = index.outline(file_path)
    if not symbols:
        return tool_result(f"No symbols found in {file_path} (unsupported language or empty file).")

    lines = [f"{file_path} ({lang_name}, {line_count} lines)", ""]
    _format_symbols(symbols, lines, indent=0, name_filter=name_filter)
    return tool_result("\n".join(lines))


def _handle_directory(
    dir_path: Path,
    name_filter: str,
    language: str | None,
    index: CodeIndex,
    cwd: Path,
) -> str:
    """Handle directory mode: recursive outline with adaptive depth."""
    # Discover files
    files = index._discover_files()
    if language:
        # Filter by language extension
        ext_map = {
            ".py": "Python",
            ".ts": "TypeScript",
            ".js": "JavaScript",
            ".tsx": "TSX",
            ".go": "Go",
            ".rs": "Rust",
            ".php": "PHP",
            ".css": "CSS",
            ".hcl": "HCL",
        }
        matching_exts = [ext for ext, lang in ext_map.items() if lang.lower() == language.lower()]
        files = [f for f in files if f.suffix in matching_exts]

    if dir_path != cwd:
        # Filter to files under dir_path
        try:
            files = [f for f in files if dir_path in f.parents or f.parent == dir_path]
        except ValueError:
            # dir_path not relative to cwd
            files = []

    if not files:
        return tool_result(
            f"No files found in {dir_path}" + (f" for language {language}" if language else "")
        )

    # Collect symbols for all files
    all_data: list[tuple[Path, list[Symbol]]] = []
    for f in files:
        symbols = index.outline(f)
        all_data.append((f, symbols))

    # If name filter, just filter results without depth limiting
    if name_filter:
        results = []
        for f, syms in all_data:
            matches = [s for s in syms if _symbol_matches(s, name_filter)]
            if matches:
                results.append((f, matches))
        if not results:
            return tool_result(f'No symbols matching "{name_filter}" found in {dir_path}')
        return _format_search_results(results, cwd)

    # Adaptive depth: limit to 200 symbols total
    total_symbols = sum(len(syms) for _, syms in all_data)
    if total_symbols <= 200:
        # All fits, show full depth
        lines = [f"{dir_path}", ""]
        for f, syms in all_data:
            try:
                rel = f.relative_to(cwd)
            except ValueError:
                rel = f
            try:
                content = f.read_text(errors="replace")
                line_count = content.count("\n") + 1
            except OSError:
                line_count = "?"
            lines.append(f"{rel} [{line_count} lines]")
            if syms:
                _format_symbols(syms, lines, indent=1)
            lines.append("")
    else:
        # Need to truncate
        lines = _format_directory_with_adaptive_depth(all_data, cwd, total_symbols)

    return tool_result("\n".join(lines))


def _format_directory_with_adaptive_depth(
    all_data: list[tuple[Path, list[Symbol]]],
    cwd: Path,
    total_symbols: int,
) -> list[str]:
    """Format directory output with adaptive depth, keeping ≤200 symbols."""
    lines = [f"Directory: {cwd.name}", f"Total files: {len(all_data)}", ""]
    lines.append(
        f"Note: {total_symbols} symbols found. Showing top-level symbols only (adaptive depth)."
    )
    lines.append("")

    # Compute max depth and truncate
    max_depth = _compute_max_depth(all_data, max_symbols=200)
    truncated = _truncate_to_depth(all_data, max_depth)

    for f, syms in truncated:
        try:
            rel = f.relative_to(cwd)
        except ValueError:
            rel = f
        try:
            content = f.read_text(errors="replace")
            line_count = content.count("\n") + 1
        except OSError:
            line_count = "?"
        lines.append(f"{rel} [{line_count} lines]")
        if syms:
            _format_symbols(syms, lines, indent=1)
        lines.append("")

    return lines


def _compute_max_depth(all_data: list[tuple[Path, list[Symbol]]], max_symbols: int) -> int:
    """Compute maximum depth to keep total symbols ≤ max_symbols."""
    # Start at max depth (all symbols)
    depth = _max_depth(all_data)
    while depth >= 0:
        count = _count_symbols_at_depth(all_data, depth)
        if count <= max_symbols:
            return depth
        depth -= 1
    return 0


def _max_depth(all_data: list[tuple[Path, list[Symbol]]]) -> int:
    """Compute maximum depth across all files."""
    max_d = 0
    for _, syms in all_data:
        max_d = max(max_d, _symbol_depth(syms))
    return max_d


def _symbol_depth(symbols: list[Symbol]) -> int:
    """Compute max nesting depth of symbols."""
    if not symbols:
        return 0
    max_child = max((_symbol_depth(s.children) for s in symbols), default=0)
    return 1 + max_child


def _count_symbols_at_depth(all_data: list[tuple[Path, list[Symbol]]], depth: int) -> int:
    """Count symbols at or above given depth."""
    total = 0
    for _, syms in all_data:
        total += _count_at_depth(syms, depth, 0)
    return total


def _count_at_depth(symbols: list[Symbol], max_depth: int, current_depth: int) -> int:
    """Count symbols recursively, stopping at max_depth."""
    count = len(symbols)
    if current_depth >= max_depth:
        return count
    for s in symbols:
        count += _count_at_depth(s.children, max_depth, current_depth + 1)
    return count


def _truncate_to_depth(
    all_data: list[tuple[Path, list[Symbol]]], depth: int
) -> list[tuple[Path, list[Symbol]]]:
    """Truncate symbols to given depth."""
    truncated = []
    for f, syms in all_data:
        truncated.append((f, _truncate_symbols_at_depth(syms, depth, 0)))
    return truncated


def _truncate_symbols_at_depth(
    symbols: list[Symbol], max_depth: int, current_depth: int
) -> list[Symbol]:
    """Truncate symbol children beyond max_depth."""
    result = []
    for s in symbols:
        if current_depth < max_depth:
            new_children = _truncate_symbols_at_depth(s.children, max_depth, current_depth + 1)
            result.append(Symbol(s.name, s.kind, s.line, s.end_line, s.signature, new_children))
        else:
            # At max depth: keep symbol but no children
            result.append(Symbol(s.name, s.kind, s.line, s.end_line, s.signature, []))
    return result


def _handle_search(
    name: str,
    path: Path | None,
    language: str | None,
    index: CodeIndex,
    cwd: Path,
) -> str:
    """Search for symbols by name across the project or a subtree."""
    results = index.search(name, path, language)
    if not results:
        return tool_result(f'No symbols found matching "{name}"' + (f" in {path}" if path else ""))

    return _format_search_results(results, cwd)


def _format_search_results(
    results: list[tuple[Path, list[Symbol]]] | list[tuple[Path, Symbol]],
    cwd: Path,
) -> str:
    """Format search results."""
    # Normalize to list of (file, [symbols])
    if results and isinstance(results[0][1], Symbol):
        # Single symbol per file
        normalized: list[tuple[Path, list[Symbol]]] = [(f, [s]) for f, s in results]  # type: ignore
    else:
        normalized = results  # type: ignore

    total = sum(len(syms) for _, syms in normalized)
    lines = [
        f'Found {total} match{"es" if total != 1 else ""} for "{normalized[0][1][0].name}":',
        "",
    ]

    for file_path, syms in normalized:
        try:
            rel = file_path.relative_to(cwd)
        except ValueError:
            rel = file_path
        for sym in syms:
            lines.append(f"{rel}:{sym.line}-{sym.end_line} — {sym.signature}")

    return tool_result("\n".join(lines))


def _symbol_matches(symbol: Symbol, name: str) -> bool:
    """Check if symbol name or signature contains search term."""
    name_lower = name.lower()
    if name_lower in symbol.name.lower():
        return True
    if name_lower in symbol.signature.lower():
        return True
    if symbol.children:
        return any(_symbol_matches(child, name) for child in symbol.children)
    return False


def _format_symbols(
    symbols: list[Symbol],
    lines: list[str],
    indent: int,
    name_filter: str = "",
) -> None:
    """Format symbols hierarchically with indentation."""
    prefix = "    " * indent
    for sym in symbols:
        if name_filter:
            # Only show matching symbols
            if not _symbol_matches(sym, name_filter):
                continue
        # Collapse multi-line signatures to a single line
        sig = " ".join(sym.signature.split())
        lines.append(f"{prefix}{sig} [line {sym.line}-{sym.end_line}]")
        # Skip children for imports — just show the line range
        if sym.kind == "imports":
            continue
        if sym.children:
            _format_symbols(sym.children, lines, indent + 1, name_filter)
