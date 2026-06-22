# Plan 030: code_intel.py Refactor

## Problem

`code_intel.py` is 800+ lines with ~400 lines of near-duplicate extraction
logic across 7 language extractors. Each extractor repeats the same pattern:
match node type → get name/params/return fields → build Symbol. The
duplication makes adding languages tedious and bugs get fixed per-language
instead of once.

Additionally, the research phase identified real bugs in the current extractors:
- Go: missing method return types, no interface method children, no generic params
- Rust: traits not extracted, impl method sigs truncated to `fn name(...)`, no type aliases
- PHP: class methods don't extract parameters (functions do)

## Goals

1. Eliminate structural duplication via a data-driven extraction engine
2. Fix the identified bugs in Go, Rust, and PHP extractors
3. Keep the code understandable — avoid building a complex DSL
4. Produce identical output for existing test cases (plus improved output where bugs are fixed)
5. Keep Python, CSS, and HCL extractors unchanged (Python is too complex, CSS/HCL too simple)

## Design

### Approach: Rule-Based with Escape Hatches

A `LangConfig` per language declares:
- **Rules**: node types to match and how to extract name/params/return fields
- **Wrappers**: transparent node types to unwrap before applying rules (export_statement, PHP namespaces)
- **Custom hooks**: per-node-type callables for cases that don't fit field extraction

```python
@dataclass(frozen=True)
class SymbolRule:
    """Declarative rule for extracting a symbol from an AST node type."""
    node_types: tuple[str, ...]       # AST node type(s) this rule matches
    kind: str                         # Symbol kind ("function", "class", etc.)
    name_field: str = "name"          # Field holding the name
    sig_parts: tuple[tuple[str, str], ...] = (("parameters", "params"),)
    # ↑ Ordered list of (ast_field_name, template_key) pairs to extract.
    # Each pair reads a field from the AST node and maps it to a placeholder
    # in sig_template. If the field is absent/None, the placeholder resolves
    # to empty string. This makes the rule system field-agnostic — any number
    # of fields (receiver, type_params, params, return_type) without new attrs.
    sig_template: str = "{name}{params}"  # Signature format with {name} + sig_parts keys
    body_field: str | None = None     # If set, recurse into this for children
    child_rules: tuple["SymbolRule", ...] = ()  # Rules to apply inside body

@dataclass(frozen=True)
class LangConfig:
    """Complete extraction config for a language."""
    rules: tuple[SymbolRule, ...]
    wrappers: frozenset[str] = frozenset()  # Node types to unwrap transparently
    custom: tuple[tuple[str, Callable], ...] = ()  # (node_type, handler) pairs
```

**Why `sig_parts` instead of fixed `params_field`/`return_field`:**

Languages vary in how many fields contribute to a signature. Go methods need
receiver + params + return (4 parts). Go generics need type_params + params +
return. Java/C#/Kotlin would need visibility + generics + params + return.
Rather than adding optional fields to SymbolRule for each case, `sig_parts`
lets each rule declare exactly which AST fields to extract and what template
key they map to. The engine stays a single loop that never needs modification
when new languages need more fields.

**Why `frozenset` and `tuple` instead of `dict`:**

`LangConfig` is frozen. Using mutable containers (`dict`, `set`) on a frozen
dataclass is technically allowed but semantically inconsistent. `frozenset` for
wrappers gives O(1) lookup. `tuple[tuple[str, Callable], ...]` for custom hooks
is immutable and iterated linearly (never more than 3-4 entries per language).

### Generic Engine (~50 lines)

```python
def _extract_by_config(root, source: bytes, config: LangConfig) -> list[Symbol]:
    """Walk root children, apply config rules, handle wrappers and customs."""
    symbols = []
    for node in root.children:
        symbols.extend(_extract_node(node, source, config))
    return symbols

def _extract_node(node, source: bytes, config: LangConfig) -> list[Symbol]:
    # Wrapper unwrapping (recurse into children)
    if node.type in config.wrappers:
        results = []
        for child in node.children:
            results.extend(_extract_node(child, source, config))
        return results
    # Custom handler (linear scan, max 3-4 entries)
    for node_type, handler in config.custom:
        if node.type == node_type:
            return handler(node, source)
    # Rule matching
    for rule in config.rules:
        if node.type in rule.node_types:
            return [_build_symbol(node, source, rule, config)]
    return []

def _build_symbol(node, source: bytes, rule: SymbolRule, config: LangConfig) -> Symbol:
    """Build a Symbol from an AST node using a SymbolRule.

    1. Extract name from rule.name_field
    2. For each (field, key) in rule.sig_parts, extract field text (empty if absent)
    3. Format sig_template with {name} + all sig_parts keys
    4. If body_field set, recurse into body applying child_rules
    """
    name = _field_text(node, rule.name_field, source)

    # Build template context from sig_parts
    parts = {"name": name}
    for field_name, template_key in rule.sig_parts:
        field_node = node.child_by_field_name(field_name)
        parts[template_key] = _text(field_node, source) if field_node else ""
    
    # Format — collapse double spaces from empty optionals, strip trailing
    sig = rule.sig_template.format(**parts)
    sig = " ".join(sig.split())  # normalise whitespace from empty parts

    # Extract children from body
    children = []
    if rule.body_field and rule.child_rules:
        body = node.child_by_field_name(rule.body_field)
        if body:
            child_config = LangConfig(rules=rule.child_rules)
            children = _extract_by_config(body, source, child_config)

    return Symbol(
        name=name,
        kind=rule.kind,
        line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        signature=sig,
        children=children,
    )

def _field_text(node, field_name: str, source: bytes) -> str:
    """Extract text from a named field, or '?' if field missing."""
    child = node.child_by_field_name(field_name)
    return _text(child, source) if child else "?"
```

### Per-Language Configs

**Go** (~15 lines of config + 1 custom for type_declaration):
```python
GO_CONFIG = LangConfig(
    rules=(
        SymbolRule(("function_declaration",), "function",
                   sig_parts=(("type_parameters", "tparams"), ("parameters", "params"), ("result", "ret")),
                   sig_template="func {name}{tparams}{params} {ret}"),
        SymbolRule(("method_declaration",), "method",
                   sig_parts=(("receiver", "recv"), ("parameters", "params"), ("result", "ret")),
                   sig_template="func {recv} {name}{params} {ret}"),
    ),
    custom=(("type_declaration", _go_type_declaration),),
)
```

**Rust** (~15 lines of config + 1 custom for impl_item):
```python
RUST_CONFIG = LangConfig(
    rules=(
        SymbolRule(("function_item",), "function",
                   sig_parts=(("type_parameters", "tparams"), ("parameters", "params"), ("return_type", "ret")),
                   sig_template="fn {name}{tparams}{params} -> {ret}"),
        SymbolRule(("struct_item",), "struct", sig_parts=(), sig_template="struct {name}"),
        SymbolRule(("enum_item",), "enum", sig_parts=(), sig_template="enum {name}"),
        SymbolRule(("trait_item",), "trait", sig_parts=(), body_field="body",
                   sig_template="trait {name}",
                   child_rules=(
                       SymbolRule(("function_item", "function_signature_item"), "method",
                                  sig_parts=(("parameters", "params"), ("return_type", "ret")),
                                  sig_template="fn {name}{params} -> {ret}"),
                   )),
    ),
    custom=(("impl_item", _rust_impl_item),),
)
```

**JavaScript/TypeScript** (~10 lines of config + 1 custom for lexical_declaration):
```python
JS_CONFIG = LangConfig(
    rules=(
        SymbolRule(("function_declaration", "generator_function_declaration"), "function",
                   sig_parts=(("parameters", "params"), ("return_type", "ret")),
                   sig_template="function {name}{params}{ret}"),
        SymbolRule(("class_declaration",), "class", sig_parts=(), body_field="body",
                   sig_template="class {name}",
                   child_rules=(
                       SymbolRule(("method_definition",), "method",
                                  sig_parts=(("parameters", "params"),),
                                  sig_template="{name}{params}"),
                   )),
    ),
    wrappers=frozenset(("export_statement",)),
    custom=(("lexical_declaration", _js_fn_variables),),
)
```

**PHP** (~10 lines of config, uses wrappers for namespace recursion):
```python
PHP_CONFIG = LangConfig(
    rules=(
        SymbolRule(("function_definition",), "function",
                   sig_parts=(("parameters", "params"),),
                   sig_template="function {name}{params}"),
        SymbolRule(("class_declaration",), "class", sig_parts=(), body_field="body",
                   sig_template="class {name}",
                   child_rules=(
                       SymbolRule(("method_declaration",), "method",
                                  sig_parts=(("parameters", "params"),),
                                  sig_template="{name}{params}"),
                   )),
    ),
    wrappers=frozenset(("program", "php_tag", "namespace_definition", "compound_statement")),
)
```

### What Stays Imperative

- `_extract_python` — import aggregation, decorator handling, constant heuristics, field detection
- `_extract_css` — 10 lines, entirely different model (selectors not functions)
- `_extract_hcl` — 15 lines, positional multi-label blocks

### Custom Hooks (escape hatches)

Each is a small function (10-20 lines) handling a specific node type:

- `_go_type_declaration(node, source)` — iterate type_specs, infer kind from inner node type, extract interface methods as children
- `_rust_impl_item(node, source)` — handle `impl Trait for Type` vs `impl Type`, extract methods with full signatures
- `_js_fn_variables(node, source)` — filter variable_declarator by RHS type (arrow_function/function_expression/class)

## File Structure

Keep as single file. The refactor removes ~200 lines of duplicate code and
replaces them with ~80 lines of generic engine + config. Total file drops from
~830 to ~650 lines. Not worth a directory split for 9 extractors where 4 are
pure config.

If languages grow beyond ~12-15, revisit splitting into a package.

## Implementation Steps

### Phase 1: Generic Engine + Configs (no behaviour change)
1. Add `SymbolRule` and `LangConfig` dataclasses
2. Implement `_extract_by_config()` and `_build_symbol()` engine
3. Port Go extractor to config — fix method return types and add interface children
4. Port Rust extractor to config — add traits, fix impl method sigs, add type aliases
5. Port JS/TS extractor to config — preserve export unwrapping and variable filtering
6. Port PHP extractor to config — fix method params bug
7. Delete old `_extract_go`, `_extract_rust`, `_extract_javascript`, `_extract_php` and their helpers

### Phase 2: Bug Fixes (enabled by refactor)
8. Go: extract generic type parameters into signatures
9. Rust: extract const_item and static_item
10. PHP: add interface and trait extraction

### Phase 3: Tests
11. Add regression test file per language in tests/fixtures/ (small representative files)
12. Test that outline() produces expected symbols for each fixture
13. Existing tests must still pass

## Risks

- **Over-engineering the rule system**: if more than 3-4 custom hooks per language are needed,
  the declarative approach is worse than the current code. Mitigation: strict rule — if a
  language needs >3 customs, keep it imperative.
- **Empty sig_parts producing malformed signatures**: e.g. `"fn {name}{params} -> {ret}"` where
  `ret` is empty produces `"fn foo() -> "`. Mitigation: `_build_symbol` normalises whitespace
  via `" ".join(sig.split())` and strips trailing ` ->` / `->` if return part is empty.
  Add a small cleanup step that removes dangling connectors.
- **Tree-sitter grammar field names changing**: pyproject.toml uses `>=` lower bounds, not
  exact pins. A grammar update could rename a field and silently break extraction.
  Mitigation: CI test fixtures catch this — outline() output is asserted per-language.
  Consider tightening bounds to `>=x,<x+1` for grammars.

## Success Criteria

- All existing tests pass
- Go, Rust, PHP bugs fixed (new test assertions)
- Net reduction of 100+ lines
- No language needs more than 2 custom hooks
- Adding a new language = 5-15 lines of config + optional custom hook(s)

## Non-Goals

- Splitting into code_intel/ package (not worth it yet)
- Adding new languages (separate work)
- Changing the CodeIndex public API (outline, search, overview stay the same)
