# Plan 028: Code Tool Rewrite — Unified Interface & Enriched Output

## Objective

Rewrite the `code` tool to remove the explicit `operation` parameter, infer behaviour from inputs (file → outline, directory → recursive outline, name → search), enrich the Symbol model with end_line and new symbol kinds (imports, constants, fields, enum variants, decorators), and introduce an adaptive depth algorithm that keeps directory outlines within a 200-symbol budget.

## Context

The current code tool exposes three operations (`outline`, `search`, `overview`) via an explicit `operation` parameter. This forces the model to choose upfront and produces inconsistent output formats. The underlying `CodeIndex` extracts only functions, classes, methods, and impl blocks — missing imports, constants, fields, and decorators that are essential for understanding a file's structure without reading it.

Key pain points:
- The model frequently misuses `overview` when it wants a recursive outline with symbols
- Single-file outlines lack line ranges, making it hard to target edits
- No way to see imports or constants without reading the full file
- Directory exploration produces either too much detail (every method) or too little (just directory names)
- Small files (≤200 lines) would be more useful returned as full content than as an outline

## Requirements

### Unified interface

- The tool MUST remove the `operation` parameter; behaviour MUST be inferred from inputs
  - AC: passing a file path returns an outline of that file
  - AC: passing a directory path returns a recursive outline with adaptive depth
  - AC: passing `name` without `path` searches the project root; passing both `name` and `path` constrains the search to that subtree
  - AC: passing no arguments outlines the project root directory

- The tool MUST accept only: `path` (optional string), `name` (optional string), `language` (optional string)
  - AC: JSON schema has exactly these three properties, none required

- The tool MUST return full file content with line numbers for files ≤200 lines when no `name` filter is active
  - AC: output includes every line prefixed with its 1-based line number
  - AC: files >200 lines still return the enriched outline

### Adaptive depth (directory mode)

- The tool MUST keep total symbol count ≤200 when outlining a directory
  - AC: when raw symbol count exceeds 200, the deepest level is uniformly stripped from all files until the budget is met
  - AC: at depth 0 (file names only), if file count exceeds 200, the result is capped at 200 files sorted by mtime descending with a note indicating truncation

- The tool MUST show file metadata (language, line count) in directory outlines
  - AC: each file entry includes `[N lines]` suffix

### Symbol enrichment

- The Symbol dataclass MUST include an `end_line: int` field (1-based, inclusive)
  - AC: all extractors set `end_line` using `node.end_point.row + 1`
  - AC: output renders line ranges as `[start-end]`

- The Python extractor MUST capture imports, module-level constants, class fields, and decorators
  - AC: imports are collapsed into a single symbol with kind "imports" listing module names
  - AC: module-level assignments (ALL_CAPS or type-annotated) appear as kind "constant"
  - AC: class body assignments appear as kind "field" with type annotation if present
  - AC: decorators are included in the parent symbol's signature (prefixed `@decorator\n`)

- Extractors for TypeScript, Rust, Go, and PHP MUST capture equivalent enrichments
  - AC: TypeScript — imports, interfaces with fields, enum members, type aliases
  - AC: Rust — use statements, struct fields, enum variants, trait items
  - AC: Go — imports, constants, struct fields, interface methods
  - AC: PHP — use statements, class constants, class properties

### Output format

- Single-file output MUST match the enriched format specified in the objective (imports block, consts, decorated classes with fields, line ranges)
  - AC: imports show as `imports: [start-end]` followed by indented module names
  - AC: class fields show with type annotations inline
  - AC: functions/methods show full signature with line range

- Directory output MUST show file path with line count, then top-level symbols with line ranges
  - AC: stripped children are not shown (adaptive depth)

- Name-filter output MUST show match count, then one line per match with file:line and signature
  - AC: each match includes `[start-end]` range

### Compatibility

- The tool MUST remain self-truncating (`self_truncating=True`)
- The tool MUST use `validate_path()` for all path resolution
- The factory MUST be `make_code_spec(cwd, allowed_directories)` — same signature as current

## Design

### Tool description

```
Structural code intelligence. Returns outlines with line ranges, or full content for small files.

- Use before `read` to understand file/directory structure.
- File path: enriched outline (imports, classes, functions, fields with line ranges).
- Directory path: recursive outline of all files (adaptive depth).
- Add `name` param to search for symbols by name.
- Small files (≤200 lines) return full content automatically.
```

### Schema

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": "File or directory path. Relative to project root. Default: project root."
    },
    "name": {
      "type": "string",
      "description": "Filter/search symbols by name. Case-insensitive substring match."
    },
    "language": {
      "type": "string",
      "description": "Filter by language: python, typescript, javascript, php, go, rust, css, hcl."
    }
  }
}
```

### Dispatch logic (handler)

```
if name is set:
    → search mode (across path or cwd)
elif path resolves to a file:
    if line_count <= 200:
        → return full file content with line numbers
    else:
        → return enriched outline
elif path resolves to a directory (or no path given):
    → collect symbols recursively, apply adaptive depth, format
```

### Adaptive depth algorithm

```python
def adaptive_outline(files: list[Path], index: CodeIndex) -> tuple[list, int]:
    """Returns (file_symbols_pairs, depth_used)."""
    all_data = [(f, index.outline(f)) for f in files]
    depth = max_depth(all_data)
    while count_symbols(all_data, depth) > 200 and depth > 0:
        depth -= 1
    return truncate_to_depth(all_data, depth), depth
```

Depth levels:
- Full depth: all symbols including nested methods/fields
- Depth 1: top-level symbols only (classes, functions — no methods)
- Depth 0: file names only (no symbols)

### Symbol dataclass changes

```python
@dataclass
class Symbol:
    name: str
    kind: str       # "function", "class", "method", "imports", "constant", "field", "enum_variant"
    line: int       # 1-based start line (includes decorators/doc comments)
    end_line: int   # 1-based end line (inclusive)
    signature: str
    children: list["Symbol"] = field(default_factory=list)
```

### File layout

- `src/archie/code_intel.py` — Symbol dataclass updated, extractors enriched
- `src/archie/tools/code.py` — rewritten handler with unified dispatch
- `tests/test_code_intel.py` — new tests for enriched extractors
- `tests/test_code_tool.py` — new tests for unified interface

## Milestones

### Milestone 1: Symbol dataclass + end_line

**Approach:** Add `end_line` to Symbol, update all extractors to populate it, update output formatting. This is a minimal, non-breaking change that immediately improves output quality.

**Tasks:**
1. Add `end_line: int` field to `Symbol` (default to `line` for backwards compat during transition)
2. Update all extractor functions to set `end_line = node.end_point.row + 1`
3. Update `_format_symbols` in `code.py` to render `[line-end_line]` instead of `[line N]`
4. Update existing tests to include `end_line` in assertions

**Deliverable:** All symbols include accurate end_line; output shows line ranges.

**Verify:**
```bash
uv run pytest tests/test_code_tool.py tests/test_code_intel.py -v
uv run ruff check src/archie/code_intel.py src/archie/tools/code.py
```

### Milestone 2: Unified tool interface + adaptive depth

**Approach:** Rewrite `code.py` handler to remove `operation` dispatch, infer behaviour from inputs. Implement adaptive depth for directory mode. This is the core structural change.

**Tasks:**
1. Replace schema — remove `operation`, keep `path`/`name`/`language` (none required)
2. Implement dispatch logic: name → search, file → outline, directory → adaptive outline
3. Implement adaptive depth: collect → count → strip deepest → repeat until ≤200
4. Update output formatting for all three modes (file, directory, search)
5. Update tool description to reflect unified interface
6. Create `tests/test_code_tool.py` for new interface

**Deliverable:** Single unified tool that infers mode from params; directory outlines respect 200-symbol budget.

**Verify:**
```bash
uv run pytest tests/test_code_tool.py -v
uv run ruff check src/archie/tools/code.py
```

### Milestone 3: Enriched Python extractor

**Approach:** Extend `_extract_python` to capture imports, constants, fields, and decorators. This is the most complex extractor change; other languages follow the same pattern.

**Tasks:**
1. Add import extraction — collect `import_statement` and `import_from_statement` nodes, collapse into single Symbol with kind "imports"
2. Add constant extraction — module-level assignments with ALL_CAPS names or type annotations
3. Add class field extraction — assignments in class body as kind "field", include type if annotated
4. Add decorator handling — prepend `@decorator` to parent symbol's signature, extend line range to include decorator node
5. Include doc comment (expression_statement containing string) in symbol's start line
6. Add tests for each new symbol kind

**Deliverable:** Python extractor produces imports, constants, fields, decorators in output.

**Verify:**
```bash
uv run pytest tests/test_code_intel.py -k python -v
uv run ruff check src/archie/code_intel.py
```

### Milestone 4: Enriched extractors for other languages

**Approach:** Apply the same enrichment pattern to TypeScript, Rust, Go, and PHP extractors. Each language gets its idiomatic equivalent of imports/constants/fields/variants.

**Tasks:**
1. TypeScript/JavaScript — import declarations, interface fields, enum members, type aliases
2. Rust — `use` items, struct fields, enum variants, trait method signatures
3. Go — import declarations, const/var blocks, struct fields, interface method signatures
4. PHP — `use` statements, class constants (`const`), class properties (`public $x`)
5. Add per-language test cases

**Deliverable:** All major extractors produce enriched symbols.

**Verify:**
```bash
uv run pytest tests/test_code_intel.py -v
uv run ruff check src/archie/code_intel.py
```

### Milestone 5: Small file threshold

**Approach:** When a single file is ≤200 lines and no `name` filter is active, return the full file content with line numbers instead of an outline. This eliminates a round-trip for small files.

**Tasks:**
1. In file-mode dispatch, check line count before outlining
2. Read file content, prefix each line with zero-padded line number
3. Include file metadata header (language, line count)
4. Add tests for threshold boundary (200 lines → full content, 201 lines → outline)

**Deliverable:** Small files return full content automatically; large files return enriched outline.

**Verify:**
```bash
uv run pytest tests/test_code_tool.py -k "small_file or threshold" -v
uv run ruff check src/archie/tools/code.py
```
