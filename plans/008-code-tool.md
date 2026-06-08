# Plan 008: Code Intelligence Tool

## Objective

Add a `code` tool that provides structural code understanding via tree-sitter — symbol search, file outlines, and codebase overview. Lets the model explore code by structure rather than reading entire files, saving significant tokens on navigation tasks.

## Context

- Current tools (read_file, search_files, list_files) work at the text level — the model reads full files to understand structure
- The $0.13 "summarise all files" session could have been $0.02 with a code outline tool
- Claude Code's #1 recommended extension is structural code search (ast-grep)
- Kiro CLI has a full tree-sitter code tool supporting 18 languages
- We want multi-language support from day one (Python, TypeScript, PHP, Terraform, CSS)
- No LSP required — tree-sitter parsing is fast and self-contained
- No ast-grep binary — py-tree-sitter handles everything in-process

## Design Decisions

### py-tree-sitter with mtime-cached parsing

Parse files on demand, cache the parse tree and extracted symbols keyed on `(path, mtime)`. This handles:
- Normal edits → mtime changes → reparse
- Branch switches → git updates mtimes of changed files → reparse
- Unchanged files → instant cache hit (just a stat call)

No filesystem watchers, no background indexer, no persistence across sessions. Cold scan of a 500-file project is ~300ms. Warm lookups are sub-millisecond.

### Single tool with an `operation` parameter

Rather than separate tools (`code_outline`, `search_symbols`, etc.), use one `code` tool with an `operation` field. This:
- Reduces tool schema overhead in the API request (one tool definition instead of three)
- Matches Kiro's approach (one `code` tool with operations)
- The model learns one tool with clear operations

### Operations

1. **`outline`** — get all symbols in a file (classes, functions, methods with signatures)
2. **`search`** — find symbol definitions by name across the project (fuzzy)
3. **`overview`** — high-level project structure (languages, key directories, entry points)

### Language support

Initial: Python, TypeScript/JavaScript, PHP, Go, Rust, CSS, HCL (Terraform)

Tree-sitter grammars are installed as Python packages (`tree-sitter-python`, `tree-sitter-javascript`, etc.). Each grammar is ~100KB.

### Output format

Designed for token efficiency — the model gets structure without bodies:

```
class Engine:
    def __init__(self, llm_client, session, tool_registry, system_prompt, sandbox): ...
    def run(self, user_message) -> Generator[EngineEvent]: ...
    def _execute_tool(self, name, args) -> tuple[str, bool]: ...

def _hash_args(args: dict) -> str: ...
```

For `search`, results include file path + line number:
```
src/archie/engine.py:55 — class Engine
src/archie/engine.py:90 — def run(self, user_message) -> Generator[EngineEvent]
tests/test_engine.py:35 — class TestEnginePlainResponse
```

## Schema

### Tool definition

```json
{
  "name": "code",
  "description": "Structural code intelligence. Use 'outline' to see all symbols in a file (classes, functions, signatures) without reading the full content. Use 'search' to find where symbols are defined by name. Use 'overview' for high-level project structure.",
  "schema": {
    "type": "object",
    "properties": {
      "operation": {
        "type": "string",
        "enum": ["outline", "search", "overview"],
        "description": "outline: symbols in a file. search: find definitions by name. overview: project structure."
      },
      "path": {
        "type": "string",
        "description": "File path (for outline) or directory path (for search/overview). Relative to project root."
      },
      "name": {
        "type": "string",
        "description": "Symbol name to search for (for search operation). Supports fuzzy matching."
      },
      "language": {
        "type": "string",
        "description": "Filter by language (python, typescript, php, go, rust, css, hcl). Optional."
      }
    },
    "required": ["operation"]
  }
}
```

### Operation: outline

**Input**: `{"operation": "outline", "path": "src/archie/engine.py"}`

**Output**:
```
src/archie/engine.py (Python, 245 lines)

def _hash_args(args: dict) -> str [line 49]

class Engine [line 55]
    def __init__(self, llm_client, session, tool_registry, system_prompt, sandbox) [line 70]
    def run(self, user_message) -> Generator[EngineEvent] [line 90]
    def _execute_tool(self, name, args) -> tuple[str, bool] [line 210]
```

Shows: symbol type, name, parameters, return type, line number, nesting (methods indented under class).

### Operation: search

**Input**: `{"operation": "search", "name": "Engine"}`

**Output**:
```
Found 4 matches for "Engine":

src/archie/engine.py:55 — class Engine
src/archie/types.py:88 — class EngineEvent
tests/test_engine.py:35 — class TestEnginePlainResponse
tests/test_engine.py:95 — class TestEngineToolUse
```

Searches across all project files. Fuzzy matching (case-insensitive substring by default).

### Operation: overview

**Input**: `{"operation": "overview"}`

**Output**:
```
Project: archie-nextgen
Languages: Python (98%), TCSS (2%)
Files: 38 source files

src/archie/         — main package (14 files)
  engine.py         — Engine (class), _hash_args (function)
  session.py        — Session (class), TurnLog (class), summarise_tool_output (function)
  config.py         — Config (class), load_config (function)
  tools/            — tool framework + implementations (7 files)
  ui/               — Textual TUI (6 files)
  llm/              — LLM provider (2 files)
tests/              — test suite (12 files)
sandbox/            — Dockerfile
```

Shows directory structure with key symbols per file. One-shot project understanding.

## Review Resolutions

1. **Tree-sitter API (0.25+)**: Use `tree_sitter.Query(language, pattern)` → `QueryCursor(query)` → `cursor.matches(node)`. Not the deprecated `Language.query()` API. Pin `tree-sitter>=0.25`.

2. **Grammar package naming variants**:
   - Python: `tree_sitter_python.language()`
   - TypeScript: `tree_sitter_typescript.language_typescript()`
   - TSX: `tree_sitter_typescript.language_tsx()`
   - PHP: `tree_sitter_php.language_php()`
   - Go: `tree_sitter_go.language()`
   - Rust: `tree_sitter_rust.language()`
   - CSS: `tree_sitter_css.language()`
   - HCL: `tree_sitter_hcl.language()`
   
   Map these in a language registry dict: `{extension → (module, function_name, query_patterns)}`.

3. **Search — file discovery and matching**:
   - Walk files using the same approach as `list_files` (respects .gitignore via `rg --files`)
   - Case-insensitive substring match (simple, predictable)
   - Cap results at 50 (enough for navigation, prevents flooding context)
   - First call parses all project files (~300ms cold), subsequent calls use mtime cache

4. **Edge cases**:
   - Binary detection: same as read_file (check first 8KB for null bytes), skip silently
   - Large files: skip files >500KB (generated code, minified bundles)
   - Symlinks: don't follow (same as ripgrep default)
   - Excluded dirs: respect .gitignore (handled by `rg --files` for file discovery). Also hardcode skip for `node_modules`, `.venv`, `build`, `dist`, `__pycache__`
   - Ambiguous extensions: `.h` → C, `.tsx` → TSX. Map explicitly.

5. **Return types and method nesting**:
   - Use tree-sitter node traversal (not just queries) for full extraction. Walk children of function nodes to find `return_type` annotations.
   - Method nesting: walk class body → find function_definition children → indent in output. Tree-sitter gives us the AST hierarchy naturally.
   - Each language needs a dedicated `extract_symbols(tree)` function that walks the AST appropriately.

6. **Other language queries**: Each language has different node types (Go uses `function_declaration`, Rust uses `function_item`, etc.). Milestone 2 will implement per-language extractors. The pattern is the same — walk top-level nodes, find definition nodes, extract name/params/return.

7. **Graceful degradation**: If a grammar package isn't installed, that language is simply unavailable. Log a warning on first attempt. Don't crash.

8. **Own mtime cache**: The code tool maintains its own cache (separate from read_file's). Different granularity — read_file caches content, code tool caches parsed symbols.

9. **Overview language stats**: By file count (simplest, good enough).

## Implementation

### Architecture

```
src/archie/tools/code.py          — tool spec + handler (operation dispatch)
src/archie/code_intel.py          — CodeIndex class (parsing, caching, queries)
```

Separation: `code_intel.py` is the engine (reusable, testable), `tools/code.py` is the thin tool wrapper.

### CodeIndex class

```python
class CodeIndex:
    """Mtime-cached tree-sitter code index."""

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir
        self._cache: dict[Path, tuple[float, list[Symbol]]] = {}  # path → (mtime, symbols)
        self._parsers: dict[str, Parser] = {}  # language → Parser
        self._languages: dict[str, Language] = {}  # extension → Language

    def outline(self, path: Path) -> list[Symbol]:
        """Get all symbols in a file. Cached by mtime."""

    def search(self, name: str, path: Path | None = None, language: str | None = None) -> list[SymbolMatch]:
        """Find symbol definitions matching name across project."""

    def overview(self, path: Path | None = None) -> str:
        """Generate project structure overview."""
```

### Symbol extraction

Tree-sitter queries per language that extract:
- Functions/methods: name, parameters, return type, line number
- Classes/interfaces: name, line number, methods (nested)
- Module-level constants/variables

Each language needs a tree-sitter query file or inline query. For Python:
```scheme
(function_definition name: (identifier) @name parameters: (parameters) @params) @func
(class_definition name: (identifier) @name) @class
```

### Dependencies

```
tree-sitter>=0.23
tree-sitter-python
tree-sitter-javascript  (covers JS + TS via TSX grammar or separate tree-sitter-typescript)
tree-sitter-typescript
tree-sitter-php
tree-sitter-go
tree-sitter-rust
tree-sitter-css
tree-sitter-hcl
```

### Integration with tool registry

The `code` tool is registered like any other. It takes the `project_dir` and creates a `CodeIndex` internally (closure pattern). The index lives for the session duration.

```python
def make_code_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    index = CodeIndex(cwd)

    def handler(params: dict) -> str:
        match params["operation"]:
            case "outline": return _format_outline(index.outline(path))
            case "search": return _format_search(index.search(name, path, language))
            case "overview": return _format_overview(index.overview(path))
    ...
```

## Milestones

### Milestone 1: CodeIndex + outline operation

- Add tree-sitter dependencies to pyproject.toml
- Create `src/archie/code_intel.py` with `CodeIndex` class
- Implement `outline()` for Python (parse + extract symbols)
- Mtime caching
- Format output (hierarchical, with signatures and line numbers)
- Tests: outline of a Python file, cache hit on unchanged file, cache miss on modified file

### Milestone 2: Multi-language support

- Add extraction queries for TypeScript, JavaScript, PHP, Go, Rust, CSS, HCL
- Language detection from file extension
- Handle unsupported languages gracefully (return "unsupported language" not crash)
- Tests: outline for each supported language

### Milestone 3: search operation

- Implement `search()` — scan project files, extract symbols, fuzzy match by name
- Respect allowed_directories / path validation
- Filter by language, filter by directory
- Performance: use mtime cache to skip reparsing, only stat + lookup
- Tests: search across multiple files, fuzzy matching, language filter

### Milestone 4: overview operation

- Implement `overview()` — directory structure with language stats and key symbols per file
- Compact format optimised for token efficiency
- Tests: overview of a multi-file project

### Milestone 5: Tool registration + integration

- Create `src/archie/tools/code.py` (tool spec, handler, operation dispatch)
- Register in `create_default_registry()`
- Update tool descriptions: guide model to use `code` for exploration, `read_file` for editing
- Tests: tool handler routing, error cases
- Run review workflow
