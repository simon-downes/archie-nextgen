"""UI summary formatters for tool calls.

Produces compact summaries with Rich markup for the iteration block display.
Two functions per tool call lifecycle:
- format_tool_pending: shown while tool is running (params only)
- format_tool_complete: shown after tool finishes (params + result metadata)
- format_tool_detail: multi-line detail (diffs for edit/write, error output for shell)

These return Rich markup strings. Key parameters (paths, patterns, commands)
are highlighted. The IterationBlock renders them directly.
"""

import difflib
from pathlib import Path

_DIFF_LINE_CAP = 30


def _esc(text: str) -> str:
    """Escape Rich markup characters in arbitrary text."""
    return text.replace("[", r"\[").replace("]", r"\]")


def _hi(text: str) -> str:
    """Highlight a key parameter (path, pattern, command) — bold white."""
    return f"[bold]{_esc(text)}[/]"


def _dim(text: str) -> str:
    """Dim text for secondary info."""
    return f"[dim]{_esc(text)}[/]"


def _rel_path(path_str: str, cwd: Path) -> str:
    """Convert absolute path to relative-to-cwd if possible."""
    try:
        return str(Path(path_str).resolve().relative_to(cwd.resolve()))
    except (ValueError, OSError):
        return path_str


def _parse_shell_exit(result: str) -> int | None:
    """Extract exit code from shell result format '$ cmd\\n[exit: N]\\n...'."""
    for line in result.split("\n"):
        if "[exit:" in line:
            try:
                return int(line.split("[exit:")[1].split("]")[0].strip())
            except (IndexError, ValueError):
                pass
    return None


def _shell_output_lines(result: str) -> list[str]:
    """Get output lines from shell result (everything after [exit: N] line)."""
    lines = result.split("\n")
    for i, line in enumerate(lines):
        if "[exit:" in line:
            return [ln for ln in lines[i + 1 :] if ln]
    return []


def _count_result_lines(result: str) -> int:
    """Count content lines in a read_file result (after header)."""
    lines = result.split("\n")
    # Skip header lines (before the numbered content)
    for i, line in enumerate(lines):
        if line and line.lstrip()[:1].isdigit() and "|" in line:
            return len(lines) - i
    return result.count("\n")


def _search_match_count(result: str) -> int:
    """Extract match count from search_files result header."""
    if result.startswith("No matches"):
        return 0
    # "Found N match(es) for pattern: ..."
    if result.startswith("Found "):
        try:
            return int(result.split()[1])
        except (IndexError, ValueError):
            pass
    return 0


def _list_file_count(result: str) -> tuple[int, int | None]:
    """Extract shown count and total from list_files result.

    Returns (shown, total) where total is None if not truncated.
    """
    if result.startswith("No files"):
        return (0, None)
    # "Found N file(s)..." or "Showing first M of N"
    lines = result.split("\n")
    total = 0
    shown = 0
    for line in lines[:3]:
        if "Found" in line and "file" in line:
            try:
                total = int(line.split()[1])
                shown = total
            except (IndexError, ValueError):
                pass
        if "Showing first" in line:
            try:
                parts = line.split()
                shown = int(parts[2])
                total = int(parts[4])
            except (IndexError, ValueError):
                pass
    return (shown, total if total != shown else None)


def _code_symbol_count(result: str) -> int:
    """Count symbols in code outline result."""
    # Each symbol line starts with spaces then a type indicator
    return sum(1 for line in result.split("\n") if line.strip() and "|" not in line[:6])


def _code_result_count(result: str) -> int:
    """Count results in code search output."""
    return sum(1 for line in result.split("\n") if line.strip() and line[0] != " ")


def _brain_result_count(result: str) -> int:
    """Count results from brain search."""
    return sum(1 for line in result.split("\n") if line.startswith("- ") or line.startswith("* "))


def _recall_result_count(result: str) -> int:
    """Count results from recall tool."""
    if "No memory" in result or "No searchable" in result:
        return 0
    return sum(1 for line in result.split("\n") if line.startswith("##") or line.startswith("- "))


def _debug_record_count(result: str) -> int:
    """Count records in self_debug output."""
    return sum(1 for line in result.split("\n") if line.strip().startswith("{"))


def _overview_summary(result: str) -> tuple[int, list[tuple[str, int]]]:
    """Extract total file count and language breakdown from code overview result.

    Returns (total_files, [(lang, pct), ...]) or (0, []) for empty results.
    """
    total = 0
    langs: list[tuple[str, int]] = []
    for line in result.split("\n")[:4]:
        stripped = line.strip()
        if stripped.startswith("Files:") and "source file" in stripped:
            try:
                total = int(stripped.split()[1])
            except (IndexError, ValueError):
                pass
        elif stripped.startswith("Languages:"):
            after = stripped.removeprefix("Languages: ").strip()
            if after:
                for part in after.split(", "):
                    part = part.strip().lower()
                    try:
                        pct = int(part.rsplit("(")[1].rstrip("%)"))
                        lang = part.split(" (")[0]
                        langs.append((lang, pct))
                    except (IndexError, ValueError):
                        pass
    return total, langs


# --- read_file helpers ---


def _read_file_range(params: dict, result: str) -> str:
    """Compute the line range string for read_file."""
    offset = params.get("offset", 0)
    limit = params.get("limit", None)

    if not offset and not limit:
        # Full file read — count actual lines from result
        lines = _count_result_lines(result)
        return f"({lines} lines)"

    start = offset + 1 if offset else 1
    # Compute end from result content
    content_lines = result.split("\n")
    end = start
    for line in reversed(content_lines):
        if line and "|" in line[:8]:
            try:
                end = int(line.split("|")[0].strip())
                break
            except ValueError:
                pass
    if end <= start:
        end = start + (limit or 0) - 1

    return f"(L{start}\u2013{end})"


# --- Public API ---


def format_tool_pending(name: str, params: dict, cwd: Path) -> str:
    """Produce the summary shown while the tool is running (Rich markup)."""
    match name:
        case "read":
            path = _rel_path(params.get("path", ""), cwd)
            if params.get("offset", 1) == 1:
                return f"Read {_hi(path)}"
            offset = params.get("offset", 1)
            limit = params.get("limit")
            try:
                start = max(int(float(offset)), 1)
                end = start + int(float(limit)) - 1 if limit else None
            except (ValueError, TypeError):
                return f"Read {_hi(path)}"
            if end:
                return f"Read {_hi(path)} {_dim(f'(L{start}\u2013{end})')}"
            return f"Read {_hi(path)} {_dim(f'(L{start}\u2013?)')}"

        case "write_file":
            path = _rel_path(params.get("path", ""), cwd)
            return f"Write {_hi(path)}"

        case "edit_file":
            path = _rel_path(params.get("path", ""), cwd)
            return f"Edit {_hi(path)}"

        case "list_files":
            path = params.get("path", ".")
            glob_val = params.get("glob", "")
            if glob_val:
                combined = path.rstrip("/") + "/" + glob_val if path != "." else glob_val
                return f"List {_hi(combined)}"
            return f"List {_hi(_rel_path(path, cwd))}"

        case "search_files":
            pattern = params.get("pattern", "")
            path = params.get("path", ".")
            glob = params.get("glob", "")
            target = _rel_path(path, cwd)
            if glob:
                target = target.rstrip("/") + "/" + glob if target != "." else glob
            return f"Search {_hi(pattern)} in {_hi(target)}"

        case "shell":
            command = params.get("command", "")
            return f"Shell {_hi(command)}"

        case "code":
            op = params.get("operation", "")
            match op:
                case "outline":
                    path = _rel_path(params.get("path", ""), cwd)
                    return f"Code outline {_hi(path)}"
                case "search":
                    name_param = params.get("name", "")
                    return f"Code search {_hi(name_param)}"
                case "overview":
                    path = _rel_path(params.get("path", "."), cwd)
                    return f"Code overview {_hi(path)}"
                case _:
                    return f"Code {_esc(op)}"

        case "brain":
            op = params.get("operation", "")
            match op:
                case "read":
                    return f"Brain read {_hi(params.get('path', ''))}"
                case "write":
                    return f"Brain write {_hi(params.get('path', ''))}"
                case "search":
                    query = params.get("query", "")
                    scope = params.get("scope", "")
                    if scope:
                        return f"Brain search {_hi(query)} in {_hi(scope)}"
                    return f"Brain search {_hi(query)}"
                case "commit":
                    msg = params.get("message", "")
                    return f"Brain commit {_hi(msg)}"
                case _:
                    return f"Brain {_esc(op)}"

        case "recall":
            query = params.get("query", "")
            project = params.get("project", "")
            if project:
                return f"Recall {_hi(query)} {_dim(f'[{project}]')}"
            return f"Recall {_hi(query)}"

        case "retrieve_artifact":
            tid = params.get("tool_use_id", "")
            short = tid[:16] + "\u2026" if len(tid) > 16 else tid
            return f"Retrieve artifact {_dim(short)}"

        case "self_debug":
            return "Debug log"

        case "web_search":
            query = params.get("query", "")
            return f"Web search {_hi(query)}"

        case "web_fetch":
            url = params.get("url", "")
            return f"Fetch {_dim(url)}"

        case _:
            return _esc(name)


def format_tool_complete(name: str, params: dict, result: str, is_error: bool, cwd: Path) -> str:
    """Produce the completed summary with result metadata (Rich markup)."""
    if is_error:
        error_msg = result.removeprefix("Error: ").split("\n")[0][:80]
        base = format_tool_pending(name, params, cwd)
        return f"{base} \u2014 {_esc(error_msg)}"

    match name:
        case "read":
            path = _rel_path(params.get("path", ""), cwd)
            if "unchanged since last read" in result:
                return f"read {_hi(path)} {_dim('(cached)')}"
            # Directory mode
            if "files," in result and "dirs)" in result:
                # Extract from header "path/ (N files, M dirs)"
                try:
                    parts = result.split("(")[1].split(")")[0]
                    return f"read {_hi(path)} {_dim(f'({parts})')}"
                except (IndexError, ValueError):
                    pass
                return f"read {_hi(path)}"
            # File mode — extract line range
            # Parse total lines from header "File: ... (N lines)"
            total = ""
            for line in result.split("\n")[:3]:
                if "lines)" in line:
                    try:
                        total = line.split("(")[1].split(" lines")[0]
                    except (IndexError, ValueError):
                        pass
                    break
            # Determine actual lines shown
            content_lines = [x for x in result.split("\n") if x and "|" in x[:8]]
            if content_lines:
                first = content_lines[0].split("|")[0].strip()
                last = content_lines[-1].split("|")[0].strip()
                range_str = f"L{first}–{last}"
                if total:
                    range_str += f" of {total}"
                return f"read {_hi(path)} {_dim(f'({range_str})')}"
            return f"read {_hi(path)}"

        case "glob":
            pattern = params.get("pattern", "")
            path = params.get("path", ".")
            target = _rel_path(path, cwd).rstrip("/") + "/" + pattern if path != "." else pattern
            # First line is the header "N files, most recent first" or "N shown of M..."
            first_line = result.split("\n")[0] if result else ""
            try:
                count = int(first_line.split()[0])
            except (IndexError, ValueError):
                count = len(
                    [
                        x
                        for x in result.split("\n")
                        if x.strip() and "files" not in x and "shown" not in x
                    ]
                )
            return f"glob {_hi(target)} {_dim(f'({count} files)')}"

        case "grep":
            pattern = params.get("pattern", "")
            path = params.get("path", ".")
            glob_filter = params.get("glob", "")
            target = _rel_path(path, cwd)
            if glob_filter:
                target = target.rstrip("/") + "/" + glob_filter
            if "No matches found" in result:
                return f"grep {_hi(pattern)} in {_hi(target)} {_dim('(no matches)')}"
            # Count matches (lines with |) and files (lines ending with :)
            match_count = sum(1 for x in result.split("\n") if "|" in x[:8])
            file_count = sum(
                1 for x in result.split("\n") if x.rstrip().endswith(":") and not x.startswith(" ")
            )
            return f"grep {_hi(pattern)} in {_hi(target)} {_dim(f'({match_count} matches in {file_count} files)')}"

        case "code":
            path = _rel_path(params.get("path", "."), cwd)
            name_param = params.get("name", "")
            lang = params.get("language", "")
            lang_suffix = f" {_dim(lang)}" if lang else ""
            if name_param:
                # Search mode — "Found N match(es)"
                match_count = 0
                if "Found" in result:
                    try:
                        match_count = int(result.split("Found")[1].split()[0])
                    except (IndexError, ValueError):
                        pass
                # Count unique files in results
                file_set = set()
                for line in result.split("\n"):
                    if ":" in line and "—" in line and not line.startswith("Found"):
                        file_set.add(line.split(":")[0].strip())
                if len(file_set) > 1:
                    return f"code {_hi(name_param)} in {_hi(path)}{lang_suffix} {_dim(f'({match_count} symbols in {len(file_set)} files)')}"
                return f"code {_hi(name_param)} in {_hi(path)}{lang_suffix} {_dim(f'({match_count} symbols)')}"
            else:
                # Outline mode — count symbols
                symbol_lines = [x for x in result.split("\n") if x.strip() and "[line " in x]
                symbol_count = len(symbol_lines)
                # Count unique files (lines containing .py or similar with line ranges)
                file_headers = [
                    x for x in result.split("\n") if x.strip() and "(" in x and "lines)" in x
                ]
                if len(file_headers) > 1:
                    return f"code {_hi(path)} {_dim(f'({symbol_count} symbols in {len(file_headers)} files)')}"
                return f"code {_hi(path)} {_dim(f'({symbol_count} symbols)')}"

        case "write_file":
            path = _rel_path(params.get("path", ""), cwd)
            line_count = params.get("content", "").count("\n") + 1
            return f"Write {_hi(path)} {_dim(f'({line_count} lines)')}"

        case "edit_file":
            path = _rel_path(params.get("path", ""), cwd)
            edits = params.get("edits", [])
            has_replace_all = any(e.get("replace_all") for e in edits)
            if has_replace_all and "replacements" in result:
                try:
                    count = int(result.split("(")[1].split()[0])
                    return f"Edit {_hi(path)} {_dim(f'({count} replacements)')}"
                except (IndexError, ValueError):
                    pass
            count = len(edits)
            if count > 1:
                return f"Edit {_hi(path)} {_dim(f'({count} edits)')}"
            return f"Edit {_hi(path)}"

        case "shell":
            command = params.get("command", "")
            exit_code = _parse_shell_exit(result)
            if exit_code and exit_code != 0:
                return f"Shell {_hi(command)} {_dim(f'(exit {exit_code})')}"
            return f"Shell {_hi(command)}"

        case "brain":
            op = params.get("operation", "")
            match op:
                case "read":
                    return f"Brain read {_hi(params.get('path', ''))}"
                case "write":
                    path = params.get("path", "")
                    if "Updated" in result:
                        return f"Brain update {_hi(path)}"
                    return f"Brain write {_hi(path)}"
                case "search":
                    query = params.get("query", "")
                    scope = params.get("scope", "")
                    count = _brain_result_count(result)
                    if scope:
                        return f"Brain search {_hi(query)} in {_hi(scope)} {_dim(f'({count} results)')}"
                    return f"Brain search {_hi(query)} {_dim(f'({count} results)')}"
                case "commit":
                    msg = params.get("message", "")
                    return f"Brain commit {_hi(msg)}"
                case _:
                    return f"Brain {_esc(op)}"

        case "recall":
            query = params.get("query", "")
            project = params.get("project", "")
            count = _recall_result_count(result)
            if count == 0:
                if project:
                    return f"Recall {_hi(query)} {_dim(f'[{project}]')} {_dim('(no results)')}"
                return f"Recall {_hi(query)} {_dim('(no results)')}"
            if project:
                return f"Recall {_hi(query)} {_dim(f'[{project}]')} {_dim(f'({count} results)')}"
            return f"Recall {_hi(query)} {_dim(f'({count} results)')}"

        case "retrieve_artifact":
            tid = params.get("tool_use_id", "")
            short = tid[:16] + "\u2026" if len(tid) > 16 else tid
            return f"Retrieve artifact {_dim(short)}"

        case "self_debug":
            count = _debug_record_count(result)
            level = params.get("level", "")
            event = params.get("event", "")
            if count == 0:
                return f"Debug log {_dim('(empty)')}"
            if event:
                return f"Debug log {_dim(f'[{event}]')} {_dim(f'({count} records)')}"
            if level and level.upper() != "DEBUG":
                return f"Debug log {_dim(f'[{level.upper()}]')} {_dim(f'({count} records)')}"
            return f"Debug log {_dim(f'({count} records)')}"

        case "web_search":
            query = params.get("query", "")
            if "No results found" in result:
                return f"Web search {_hi(query)} {_dim('(no results)')}"
            count = len([line for line in result.split("\n\n") if line.strip()])
            return f"Web search {_hi(query)} {_dim(f'({count} results)')}"

        case "web_fetch":
            url = params.get("url", "")
            if "Saved to:" in result:
                parts = result.split("Saved to:")
                saved_path = parts[-1].strip()
                return f"Fetch {_dim(url)} {_dim(f'(saved to {saved_path})')}"
            line_count = len(result.splitlines())
            return f"Fetch {_dim(url)} {_dim(f'({line_count} lines)')}"

        case _:
            return f"{_esc(name)} {_dim(f'({len(result)} chars)')}"


def format_tool_detail(
    name: str,
    params: dict,
    result: str,
    is_error: bool,
    cwd: Path,
    pre_content: str | None = None,
) -> list[str] | None:
    """Produce multi-line detail lines with Rich markup. Returns None if no detail."""
    # Shell non-zero exit: show last 3 lines of output
    if name == "shell" and not is_error:
        exit_code = _parse_shell_exit(result)
        if exit_code and exit_code != 0:
            output = _shell_output_lines(result)
            if output:
                return [f"  [dim]{_esc(line)}[/]" for line in output[-3:]]
    if name == "shell" and is_error:
        return None

    # Edit/write diffs
    if name in ("edit_file", "write_file") and not is_error and pre_content is not None:
        path = _rel_path(params.get("path", ""), cwd)
        filename = Path(path).name
        # Read current content from disk
        try:
            resolved = (cwd / params.get("path", "")).resolve()
            post_content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        return _generate_diff(pre_content, post_content, filename)

    return None


def _generate_diff(old: str, new: str, filename: str) -> list[str] | None:
    """Generate Kiro-style diff lines with Rich markup."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff = list(difflib.unified_diff(old_lines, new_lines, n=1))
    if not diff:
        return None

    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    # Find first change line number
    first_line = 1
    for line in diff:
        if line.startswith("@@"):
            try:
                part = line.split("+")[1].split(",")[0]
                first_line = int(part)
                break
            except (IndexError, ValueError):
                pass

    # Build header — only mention non-zero changes, colour them
    parts = []
    if added:
        parts.append(f"[green]added {added} lines[/]")
    if removed:
        parts.append(f"[red]removed {removed} lines[/]")
    header = ", ".join(parts) + f" at L{first_line} in {_esc(filename)}"
    output = [f"  [dim]{header}[/]"]

    # Render diff lines with line numbers
    line_num_old = 0
    line_num_new = 0
    detail_lines = 0

    for line in diff:
        if detail_lines >= _DIFF_LINE_CAP:
            remaining = sum(
                1
                for d in diff[diff.index(line) :]
                if d[0] in ("+", "-", " ") and not d.startswith(("+++", "---"))
            )
            if remaining > 0:
                output.append(f"  [dim]\u2026 {remaining} more changed lines[/]")
            break

        if line.startswith("@@"):
            try:
                parts_hunk = line.split()
                old_start = int(parts_hunk[1].split(",")[0].lstrip("-"))
                new_start = int(parts_hunk[2].split(",")[0].lstrip("+"))
                line_num_old = old_start - 1
                line_num_new = new_start - 1
            except (IndexError, ValueError):
                pass
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue

        content = _esc(line.rstrip("\n")[1:])
        if line.startswith("-"):
            line_num_old += 1
            output.append(f"  [dim]{line_num_old:>4}[/][on red] {content} [/]")
            detail_lines += 1
        elif line.startswith("+"):
            line_num_new += 1
            output.append(f"  [dim]{line_num_new:>4}[/][on green] {content} [/]")
            detail_lines += 1
        else:
            line_num_old += 1
            line_num_new += 1
            output.append(f"  [dim]{line_num_new:>4}   {content}[/]")
            detail_lines += 1

    return output if len(output) > 1 else None
