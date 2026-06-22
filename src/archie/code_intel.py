"""Code intelligence — tree-sitter based structural code understanding.

Provides symbol extraction and search across a project without reading
full file contents. The model uses this to understand code structure
(classes, functions, signatures) at a fraction of the token cost of
reading entire files.

Architecture:
- Parse files on demand with tree-sitter (fast: ~1ms per file)
- Cache parsed symbols by (path, mtime) — unchanged files are free
- Walk AST nodes to extract definitions (language-specific extractors)
- No persistent index, no filesystem watchers — cache lives in memory

Supported languages: Python, TypeScript, JavaScript, TSX, PHP, Go, Rust, CSS, HCL
"""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter

log = logging.getLogger(__name__)

# Maximum file size to parse (skip generated/minified files)
_MAX_FILE_SIZE = 500_000  # 500KB

# Directories to always skip (in addition to .gitignore)
_SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "build", "dist", ".git", ".tox"}


@dataclass
class Symbol:
    """A code symbol (function, class, method, etc.)."""

    name: str
    kind: str  # "function", "class", "method", "interface", etc.
    line: int  # 1-based line number
    end_line: int  # 1-based end line (inclusive)
    signature: str  # e.g. "def run_turn(self, user_message) -> None"
    children: list["Symbol"] = field(default_factory=list)


# --- Language registry ---
# Maps file extensions to (grammar_loader, extractor_function)


def _load_python():
    """Load the Python tree-sitter parser."""
    import tree_sitter_python

    return tree_sitter.Language(tree_sitter_python.language())


def _load_javascript():
    """Load the JavaScript tree-sitter parser."""
    import tree_sitter_javascript

    return tree_sitter.Language(tree_sitter_javascript.language())


def _load_typescript():
    """Load the TypeScript tree-sitter parser."""
    import tree_sitter_typescript

    return tree_sitter.Language(tree_sitter_typescript.language_typescript())


def _load_tsx():
    """Load the TSX tree-sitter parser."""
    import tree_sitter_typescript

    return tree_sitter.Language(tree_sitter_typescript.language_tsx())


def _load_php():
    """Load the PHP tree-sitter parser."""
    import tree_sitter_php

    return tree_sitter.Language(tree_sitter_php.language_php())


def _load_go():
    """Load the Go tree-sitter parser."""
    import tree_sitter_go

    return tree_sitter.Language(tree_sitter_go.language())


def _load_rust():
    """Load the Rust tree-sitter parser."""
    import tree_sitter_rust

    return tree_sitter.Language(tree_sitter_rust.language())


def _load_css():
    """Load the CSS tree-sitter parser."""
    import tree_sitter_css

    return tree_sitter.Language(tree_sitter_css.language())


def _load_hcl():
    """Load the HCL tree-sitter parser."""
    import tree_sitter_hcl

    return tree_sitter.Language(tree_sitter_hcl.language())


# Extension → (language_name, loader_function)
_EXTENSION_MAP: dict[str, tuple[str, callable]] = {
    ".py": ("python", _load_python),
    ".js": ("javascript", _load_javascript),
    ".mjs": ("javascript", _load_javascript),
    ".ts": ("typescript", _load_typescript),
    ".tsx": ("tsx", _load_tsx),
    ".php": ("php", _load_php),
    ".go": ("go", _load_go),
    ".rs": ("rust", _load_rust),
    ".css": ("css", _load_css),
    ".scss": ("css", _load_css),
    ".tf": ("hcl", _load_hcl),
    ".hcl": ("hcl", _load_hcl),
}


class CodeIndex:
    """Mtime-cached tree-sitter code index for a project."""

    def __init__(self, project_dir: Path):
        self._project_dir = project_dir
        # Cache: path → (mtime, symbols)
        self._cache: dict[Path, tuple[float, list[Symbol]]] = {}
        # Loaded parsers: language_name → Parser
        self._parsers: dict[str, tree_sitter.Parser] = {}
        # Loaded languages: language_name → Language
        self._languages: dict[str, tree_sitter.Language] = {}

    def outline(self, path: Path) -> list[Symbol]:
        """Get all symbols in a file. Returns from cache if file unchanged."""
        resolved = (
            (self._project_dir / path).resolve() if not path.is_absolute() else path.resolve()
        )

        if not resolved.is_file():
            return []
            return []

        # Check mtime cache
        try:
            mtime = resolved.stat().st_mtime
        except OSError:
            return []

        if resolved in self._cache and self._cache[resolved][0] == mtime:
            return self._cache[resolved][1]

        # Parse and extract
        symbols = self._parse_file(resolved)
        self._cache[resolved] = (mtime, symbols)
        return symbols

    def search(
        self, name: str, path: Path | None = None, language: str | None = None
    ) -> list[tuple[Path, Symbol]]:
        """Find symbols matching name across the project. Case-insensitive substring match."""
        results: list[tuple[Path, Symbol]] = []
        name_lower = name.lower()

        for file_path in self._discover_files(path):
            # Language filter
            if language:
                ext = file_path.suffix
                if ext not in _EXTENSION_MAP or _EXTENSION_MAP[ext][0] != language:
                    continue

            symbols = self.outline(file_path)
            self._search_symbols(file_path, symbols, name_lower, results)

            if len(results) >= 50:
                break

        return results[:50]

    def overview(self, path: Path | None = None) -> dict:
        """Generate project structure overview."""
        base = (self._project_dir / path) if path else self._project_dir
        base = base.resolve()

        lang_counts: dict[str, int] = {}
        dir_info: dict[Path, list[str]] = {}  # dir → [symbol summaries]

        for file_path in self._discover_files(path):
            ext = file_path.suffix
            if ext in _EXTENSION_MAP:
                lang_name = _EXTENSION_MAP[ext][0]
                lang_counts[lang_name] = lang_counts.get(lang_name, 0) + 1

            # Get top-level symbols for the file
            symbols = self.outline(file_path)
            if symbols:
                rel = file_path.relative_to(base)
                parent = rel.parent
                summaries = [f"{s.name} ({s.kind})" for s in symbols[:5]]
                dir_info.setdefault(parent, []).extend(summaries)

        return {"base": base, "languages": lang_counts, "directories": dir_info}

    def _search_symbols(
        self, file_path: Path, symbols: list[Symbol], name_lower: str, results: list
    ) -> None:
        """Recursively search symbols and their children for name matches."""
        for sym in symbols:
            if name_lower in sym.name.lower():
                results.append((file_path, sym))
            # Search nested symbols (methods inside classes)
            self._search_symbols(file_path, sym.children, name_lower, results)

    def _discover_files(self, subpath: Path | None = None) -> list[Path]:
        """Discover source files using ripgrep (respects .gitignore)."""
        base = self._project_dir
        if subpath:
            base = (self._project_dir / subpath).resolve()

        try:
            result = subprocess.run(
                ["rg", "--files"],
                cwd=base,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode not in (0, 1):
                return []
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

        files = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            p = (base / line).resolve()
            # Only include supported extensions
            if p.suffix not in _EXTENSION_MAP:
                continue
            # Skip large files
            try:
                if p.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            files.append(p)
        return files

    def _parse_file(self, path: Path) -> list[Symbol]:
        """Parse a file and extract symbols."""
        ext = path.suffix
        if ext not in _EXTENSION_MAP:
            return []

        lang_name, loader = _EXTENSION_MAP[ext]

        # Load language/parser on first use
        if lang_name not in self._languages:
            try:
                self._languages[lang_name] = loader()
                self._parsers[lang_name] = tree_sitter.Parser(self._languages[lang_name])
            except (ImportError, OSError) as e:
                log.warning("Failed to load grammar for %s: %s", lang_name, e)
                return []

        parser = self._parsers[lang_name]

        # Read and parse
        try:
            content = path.read_bytes()
            if b"\x00" in content[:8192]:
                return []  # Binary file
        except OSError:
            return []

        tree = parser.parse(content)

        # Extract symbols using language-specific logic
        extractor = _EXTRACTORS.get(lang_name, _extract_generic)
        return extractor(tree.root_node, content)


# --- Language-specific symbol extractors ---


def _text(node, source: bytes) -> str:
    """Get the text of a node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _extract_python(root, source: bytes) -> list[Symbol]:
    """Extract symbols from Python AST."""
    symbols = []

    # Collect all imports first
    imports = _extract_python_imports(root, source)
    if imports:
        symbols.append(imports)

    # Then extract other top-level symbols
    for node in root.children:
        if node.type == "function_definition":
            symbols.append(_python_function(node, source))
        elif node.type == "class_definition":
            symbols.append(_python_class(node, source))
        elif node.type == "decorated_definition":
            # Extract decorators first
            decorators = []
            func_or_class_node = None
            for child in node.children:
                if child.type == "decorator":
                    # Extract decorator name (handle foo.bar decorators)
                    dec_name = _extract_decorator_name(child, source)
                    decorators.append(
                        Symbol(
                            name=dec_name,
                            kind="decorator",
                            line=child.start_point.row + 1,
                            end_line=child.end_point.row + 1,
                            signature=f"@{dec_name}",
                        )
                    )
                elif child.type in ("function_definition", "class_definition"):
                    func_or_class_node = child

            if func_or_class_node:
                if func_or_class_node.type == "function_definition":
                    symbols.append(_python_function(func_or_class_node, source, decorators))
                elif func_or_class_node.type == "class_definition":
                    symbols.append(_python_class(func_or_class_node, source, decorators))
        elif node.type == "expression_statement":
            # Check for module-level constant (ALL_CAPS or type-annotated assignment)
            for child in node.children:
                if child.type == "assignment":
                    sym = _python_constant(child, source)
                    if sym:
                        symbols.append(sym)
    return symbols


def _extract_python_imports(root, source: bytes) -> Symbol | None:
    """Collect all import statements into a single Symbol."""
    import_nodes = []
    for node in root.children:
        if node.type == "import_statement":
            import_nodes.append(node)
        elif node.type == "import_from_statement":
            import_nodes.append(node)

    if not import_nodes:
        return None

    # Get first line to last line range
    first_line = import_nodes[0].start_point.row + 1
    last_line = import_nodes[-1].end_point.row + 1

    # Extract module names for signature
    modules = []
    for node in import_nodes:
        if node.type == "import_statement":
            # import a, b, c
            for child in node.children:
                if child.type == "dotted_name":
                    modules.append(_text(child, source))
        elif node.type == "import_from_statement":
            # from module import ...
            for child in node.children:
                if child.type == "dotted_name":
                    modules.append(_text(child, source))
                    break  # Only first dotted_name is the module

    modules_str = ", ".join(modules[:5])  # Limit to 5 for brevity
    if len(modules) > 5:
        modules_str += f", ... (+{len(modules) - 5} more)"

    return Symbol(
        name="imports",
        kind="imports",
        line=first_line,
        end_line=last_line,
        signature="imports",
        children=[Symbol(m, "import", first_line, last_line, f"import {m}") for m in modules[:5]],
    )


def _python_function(node, source: bytes, decorators: list[Symbol] | None = None) -> Symbol:
    """Build a Symbol for a Python function_definition node."""
    name = _text(node.child_by_field_name("name"), source)
    params = _text(node.child_by_field_name("parameters"), source)
    ret_node = node.child_by_field_name("return_type")
    ret = f" -> {_text(ret_node, source)}" if ret_node else ""

    # Get line range - if decorators exist, extend to include them
    start_line = node.start_point.row + 1
    if decorators:
        # Find the decorator nodes
        start_line = min(d.line for d in decorators)

    # Build signature with decorators prepended
    signature = f"def {name}{params}{ret}"
    if decorators:
        decorator_str = "\n".join(f"@{d.name}" for d in decorators)
        signature = f"{decorator_str}\n{signature}"

    return Symbol(
        name=name,
        kind="function",
        line=start_line,
        end_line=node.end_point.row + 1,
        signature=signature,
    )


def _extract_decorator_name(decorator_node, source: bytes) -> str:
    """Extract decorator name from a decorator node (handles @foo.bar.baz or @foo())."""
    # Decorator node: @ followed by identifier, attribute, or call_expression
    for child in decorator_node.children:
        if child.type == "identifier":
            return _text(child, source)
        elif child.type == "attribute":
            return _text(child, source)
        elif child.type == "call_expression":
            # For @decorator(args), extract the function name before the paren
            for sub in child.children:
                if sub.type == "identifier":
                    return _text(sub, source)
                elif sub.type == "attribute":
                    return _text(sub, source)

    return "?"


def _python_class(node, source: bytes, decorators: list[Symbol] | None = None) -> Symbol:
    """Build a Symbol for a Python class_definition node (including methods and fields)."""
    name = _text(node.child_by_field_name("name"), source)

    # Build signature with decorators
    signature = f"class {name}"
    if decorators:
        decorator_str = "\n".join(f"@{d.name}" for d in decorators)
        signature = f"{decorator_str}\n{signature}"

    body = node.child_by_field_name("body")
    children = []
    if body:
        for child in body.children:
            if child.type == "function_definition":
                sym = _python_function(child, source)
                sym.kind = "method"
                children.append(sym)
            elif child.type == "decorated_definition":
                for sub in child.children:
                    if sub.type == "function_definition":
                        sym = _python_function(sub, source)
                        sym.kind = "method"
                        children.append(sym)
            elif child.type == "expression_statement":
                # Check for class field (assignment)
                for expr in child.children:
                    if expr.type == "assignment":
                        sym = _python_field(expr, source)
                        if sym:
                            children.append(sym)

    return Symbol(
        name=name,
        kind="class",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=signature,
        children=children,
    )


def _python_field(node, source: bytes) -> Symbol | None:
    """Build a Symbol for a Python class field (assigned variable in class body)."""
    # Must be an annotation or a typed assignment
    if not node.children or node.children[0].type != "identifier":
        return None

    target_name = _text(node.children[0], source)

    # Check for type annotation
    type_node = None
    if len(node.children) >= 3 and node.children[1].type == ":":
        # Annotated assignment like: x: int = 1
        type_node = node.children[2]

    if not type_node:
        return None

    type_hint = f": {_text(type_node, source)}"

    return Symbol(
        name=target_name,
        kind="field",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=f"{target_name}{type_hint}",
    )


def _python_constant(node, source: bytes) -> Symbol | None:
    """Build a Symbol for a Python module-level constant."""
    # Must be simple assignment with identifier as first child
    if not node.children or node.children[0].type != "identifier":
        return None

    name = _text(node.children[0], source)

    # Check if ALL_CAPS or has type annotation
    # ALL_CAPS check: all uppercase AND at least one underscore or is very short
    is_all_caps = name.isupper() and ("_" in name or len(name) <= 2)

    # Check for type annotation (third child is colon for annotated assignments)
    has_type = False
    if len(node.children) >= 4 and node.children[2].type == ":":
        has_type = True

    if not (is_all_caps or has_type):
        return None

    # Get type annotation if present
    type_node = None
    if len(node.children) >= 4 and node.children[2].type == ":":
        for child in node.children[3:]:
            if child.type not in (" ", "\n"):
                type_node = child
                break

    type_hint = f": {_text(type_node, source)}" if type_node else ""

    return Symbol(
        name=name,
        kind="constant",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=f"{name}{type_hint}",
    )


def _extract_javascript(root, source: bytes) -> list[Symbol]:
    """Extract symbols from JavaScript/TypeScript AST."""
    symbols = []
    for node in root.children:
        if node.type in ("function_declaration", "generator_function_declaration"):
            symbols.append(_js_function(node, source))
        elif node.type == "class_declaration":
            symbols.append(_js_class(node, source))
        elif node.type == "export_statement":
            for child in node.children:
                if child.type in ("function_declaration", "generator_function_declaration"):
                    symbols.append(_js_function(child, source))
                elif child.type == "class_declaration":
                    symbols.append(_js_class(child, source))
                elif child.type == "lexical_declaration":
                    symbols.extend(_js_variable(child, source))
        elif node.type == "lexical_declaration":
            symbols.extend(_js_variable(node, source))
    return symbols


def _js_function(node, source: bytes) -> Symbol:
    """Build a Symbol for a JavaScript/TypeScript function_declaration node."""
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source) if name_node else "anonymous"
    params_node = node.child_by_field_name("parameters")
    params = _text(params_node, source) if params_node else "()"
    ret_node = node.child_by_field_name("return_type")
    ret = f": {_text(ret_node, source)}" if ret_node else ""
    return Symbol(
        name=name,
        kind="function",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=f"function {name}{params}{ret}",
    )


def _js_class(node, source: bytes) -> Symbol:
    """Build a Symbol for a JavaScript/TypeScript class_declaration node (including methods)."""
    name_node = node.child_by_field_name("name")
    name = _text(name_node, source) if name_node else "anonymous"
    children = []
    body = node.child_by_field_name("body")
    if body:
        for child in body.children:
            if child.type == "method_definition":
                mname_node = child.child_by_field_name("name")
                mname = _text(mname_node, source) if mname_node else "?"
                params_node = child.child_by_field_name("parameters")
                params = _text(params_node, source) if params_node else "()"
                children.append(
                    Symbol(
                        name=mname,
                        kind="method",
                        line=child.start_point.row + 1,
                        end_line=child.end_point.row + 1,
                        signature=f"{mname}{params}",
                    )
                )
    return Symbol(
        name=name,
        kind="class",
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=f"class {name}",
        children=children,
    )


def _js_variable(node, source: bytes) -> list[Symbol]:
    """Extract const/let variable declarations (often exported functions/objects)."""
    symbols = []
    for child in node.children:
        if child.type == "variable_declarator":
            name_node = child.child_by_field_name("name")
            if name_node:
                name = _text(name_node, source)
                # Only include if it's assigned a function/class
                value = child.child_by_field_name("value")
                if value and value.type in ("arrow_function", "function_expression", "class"):
                    symbols.append(
                        Symbol(
                            name=name,
                            kind="function",
                            line=child.start_point.row + 1,
                            end_line=child.end_point.row + 1,
                            signature=f"const {name} = ...",
                        )
                    )
    return symbols


def _extract_go(root, source: bytes) -> list[Symbol]:
    """Extract symbols from Go AST."""
    symbols = []
    for node in root.children:
        if node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            result_node = node.child_by_field_name("result")
            name = _text(name_node, source) if name_node else "?"
            params = _text(params_node, source) if params_node else "()"
            ret = f" {_text(result_node, source)}" if result_node else ""
            symbols.append(
                Symbol(
                    name=name,
                    kind="function",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"func {name}{params}{ret}",
                )
            )
        elif node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            recv_node = node.child_by_field_name("receiver")
            name = _text(name_node, source) if name_node else "?"
            params = _text(params_node, source) if params_node else "()"
            recv = _text(recv_node, source) if recv_node else ""
            symbols.append(
                Symbol(
                    name=name,
                    kind="method",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"func {recv} {name}{params}",
                )
            )
        elif node.type == "type_declaration":
            for spec in node.children:
                if spec.type == "type_spec":
                    name_node = spec.child_by_field_name("name")
                    type_node = spec.child_by_field_name("type")
                    name = _text(name_node, source) if name_node else "?"
                    kind = (
                        "interface" if type_node and type_node.type == "interface_type" else "type"
                    )
                    symbols.append(
                        Symbol(
                            name=name,
                            kind=kind,
                            line=spec.start_point.row + 1,
                            end_line=spec.end_point.row + 1,
                            signature=f"type {name}",
                        )
                    )
    return symbols


def _extract_rust(root, source: bytes) -> list[Symbol]:
    """Extract symbols from Rust AST."""
    symbols = []
    for node in root.children:
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            ret_node = node.child_by_field_name("return_type")
            name = _text(name_node, source) if name_node else "?"
            params = _text(params_node, source) if params_node else "()"
            ret = f" -> {_text(ret_node, source)}" if ret_node else ""
            symbols.append(
                Symbol(
                    name=name,
                    kind="function",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"fn {name}{params}{ret}",
                )
            )
        elif node.type in ("struct_item", "enum_item"):
            name_node = node.child_by_field_name("name")
            name = _text(name_node, source) if name_node else "?"
            kind = "struct" if node.type == "struct_item" else "enum"
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"{kind} {name}",
                )
            )
        elif node.type == "impl_item":
            type_node = node.child_by_field_name("type")
            type_name = _text(type_node, source) if type_node else "?"
            children = []
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    if child.type == "function_item":
                        fn_name = (
                            _text(child.child_by_field_name("name"), source)
                            if child.child_by_field_name("name")
                            else "?"
                        )
                        children.append(
                            Symbol(
                                name=fn_name,
                                kind="method",
                                line=child.start_point.row + 1,
                                end_line=child.end_point.row + 1,
                                signature=f"fn {fn_name}(...)",
                            )
                        )
            symbols.append(
                Symbol(
                    name=type_name,
                    kind="impl",
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=f"impl {type_name}",
                    children=children,
                )
            )
    return symbols


def _extract_php(root, source: bytes) -> list[Symbol]:
    """Extract symbols from PHP AST."""
    symbols = []
    # PHP wraps everything in a program > php_tag + statements
    for node in root.children:
        _extract_php_node(node, source, symbols)
    return symbols


def _extract_php_node(node, source: bytes, symbols: list[Symbol]) -> None:
    """Recursively extract function and class symbols from PHP AST nodes."""
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        params_node = node.child_by_field_name("parameters")
        name = _text(name_node, source) if name_node else "?"
        params = _text(params_node, source) if params_node else "()"
        symbols.append(
            Symbol(
                name=name,
                kind="function",
                line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature=f"function {name}{params}",
            )
        )
    elif node.type == "class_declaration":
        name_node = node.child_by_field_name("name")
        name = _text(name_node, source) if name_node else "?"
        children = []
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                if child.type == "method_declaration":
                    mname = (
                        _text(child.child_by_field_name("name"), source)
                        if child.child_by_field_name("name")
                        else "?"
                    )
                    children.append(
                        Symbol(
                            name=mname,
                            kind="method",
                            line=child.start_point.row + 1,
                            end_line=child.end_point.row + 1,
                            signature=f"{mname}()",
                        )
                    )
        symbols.append(
            Symbol(
                name=name,
                kind="class",
                line=node.start_point.row + 1,
                end_line=node.end_point.row + 1,
                signature=f"class {name}",
                children=children,
            )
        )
    else:
        # Recurse for namespace/program nodes
        for child in node.children:
            _extract_php_node(child, source, symbols)


def _extract_css(root, source: bytes) -> list[Symbol]:
    """Extract selectors from CSS."""
    symbols = []
    for node in root.children:
        if node.type == "rule_set":
            selectors = node.child_by_field_name("selectors")
            if selectors:
                text = _text(selectors, source).strip()
                symbols.append(
                    Symbol(
                        name=text,
                        kind="selector",
                        line=node.start_point.row + 1,
                        end_line=node.end_point.row + 1,
                        signature=text,
                    )
                )
    return symbols


def _extract_hcl(root, source: bytes) -> list[Symbol]:
    """Extract resources/blocks from HCL/Terraform."""
    symbols = []
    for node in root.children:
        if node.type == "block":
            # HCL blocks: resource "type" "name" { ... }
            labels = [
                _text(c, source) for c in node.children if c.type in ("identifier", "string_lit")
            ]
            name = " ".join(labels)
            kind = labels[0] if labels else "block"
            symbols.append(
                Symbol(
                    name=name,
                    kind=kind,
                    line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    signature=name,
                )
            )
    return symbols


def _extract_generic(root, source: bytes) -> list[Symbol]:
    """Fallback extractor — looks for common definition patterns."""
    return []


# Map language name → extractor function
_EXTRACTORS: dict[str, callable] = {
    "python": _extract_python,
    "javascript": _extract_javascript,
    "typescript": _extract_javascript,  # Same AST structure
    "tsx": _extract_javascript,
    "php": _extract_php,
    "go": _extract_go,
    "rust": _extract_rust,
    "css": _extract_css,
    "hcl": _extract_hcl,
}
