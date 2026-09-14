"""Module import graph of a Python repository, built with ``ast`` (no imports executed).

The graph is the substrate for every check: layer rules are edges that must not exist,
transitive impact is a reverse walk over edges, duplicate logic compares function bodies
that the same scan collected.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FunctionInfo:
    module: str
    qualname: str
    path: str
    line: int
    end_line: int
    tokens: tuple[str, ...]  # normalised body tokens, for similarity


@dataclass
class ModuleInfo:
    name: str
    path: str  # repo-relative, posix
    imports: set[str] = field(default_factory=set)  # module names inside the package
    import_lines: dict[str, int] = field(default_factory=dict)  # module -> first line
    imported_symbols: dict[str, set[str]] = field(default_factory=dict)  # module -> names
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    type_checking_only: set[str] = field(default_factory=set)
    # language-aware placement (java sets these at scan time; python derives from the name)
    context: str | None = None
    layer: str | None = None
    service_calls: set[str] = field(default_factory=set)  # java: "ts-x-service" strings in the file
    uses_rest: bool = False
    uses_mq: bool = False
    annotations: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)        # java: capitalised identifiers used (same-package refs)
    str_literals: set[str] = field(default_factory=set)  # java: short string literals (service names, urls)
    implements: set[str] = field(default_factory=set)  # java: simple names of extended/implemented types


PY_CORE_LAYERS = ("domain", "application", "infrastructure")


@dataclass
class ImportGraph:
    package: str
    modules: dict[str, ModuleInfo]
    reverse: dict[str, set[str]]  # module -> modules importing it
    test_modules: dict[str, ModuleInfo]
    language: str = "python"
    service_calls: dict[str, set[str]] = field(default_factory=dict)  # java: context -> called contexts
    service_callers: dict[str, set[str]] = field(default_factory=dict)  # java: context -> calling contexts

    def dependants(self, module: str) -> set[str]:
        return self.reverse.get(module, set())

    def classify(self, module: str) -> tuple[str | None, str | None]:
        """(context, layer) of a module, whatever the language."""
        m = self.modules.get(module) or self.test_modules.get(module)
        if self.language == "java":
            if m:
                return m.context, m.layer
            return None, None
        parts = module.split(".")
        if len(parts) < 2 or parts[0] != self.package:
            return None, None
        context = parts[1]
        if context in ("interface", "infra_factory"):
            return context, context
        layer = parts[2] if len(parts) > 2 and parts[2] in PY_CORE_LAYERS else None
        return context, layer

    def resolve(self, target: str) -> str | None:
        if self.language == "java":
            from .graph_java import resolve_java

            hits = resolve_java(self.modules, target)
            return hits[0] if len(hits) == 1 else (None if not hits else target)
        return resolve(self.modules, target)

    def resolve_all(self, target: str) -> list[str]:
        if self.language == "java":
            from .graph_java import resolve_java

            return resolve_java(self.modules, target)
        r = resolve(self.modules, target)
        return [r] if r else []

    def tests_importing(self, module: str) -> list[str]:
        if self.language == "java":
            pkg, _, simple = module.rpartition(".")
            return sorted(t for t, m in self.test_modules.items()
                          if module in m.imports or (t.rpartition(".")[0] == pkg and simple in m.refs))
        return sorted(t for t, m in self.test_modules.items() if module in m.imports)

    def find_module_for_path(self, rel_path: str) -> ModuleInfo | None:
        for m in self.modules.values():
            if m.path == rel_path:
                return m
        for m in self.test_modules.values():
            if m.path == rel_path:
                return m
        return None


def _normalise_tokens(source: str) -> tuple[str, ...]:
    """Token stream with identifiers/strings/numbers folded, so renames do not hide copies."""
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            if tok.type == tokenize.NAME:
                out.append(tok.string if tok.string in _KEYWORDS_AND_BUILTINS else "NAME")
            elif tok.type == tokenize.STRING:
                out.append("STR")
            elif tok.type == tokenize.NUMBER:
                out.append("NUM")
            else:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        pass
    return tuple(out)


_KEYWORDS_AND_BUILTINS = frozenset(
    "and as assert async await break class continue def del elif else except finally for from "
    "global if import in is lambda nonlocal not or pass raise return try while with yield "
    "lower upper strip replace split join startswith endswith len str int list dict set tuple "
    "isinstance sorted enumerate zip map filter any all min max sum abs".split()
)


def _module_name(package_root: Path, file: Path) -> str:
    rel = file.relative_to(package_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _collect(file: Path, module: str, rel_path: str, package: str, source: str) -> ModuleInfo:
    info = ModuleInfo(name=module, path=rel_path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return info
    lines = source.splitlines()

    def add_import(target: str, names: set[str], line: int, in_type_checking: bool) -> None:
        if not target.startswith(package + ".") and target != package:
            return
        info.imports.add(target)
        info.import_lines.setdefault(target, line)
        info.imported_symbols.setdefault(target, set()).update(names)
        if in_type_checking:
            info.type_checking_only.add(target)

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.in_tc = 0
            self.stack: list[str] = []

        def visit_If(self, node: ast.If) -> None:
            is_tc = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
                isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            )
            if is_tc:
                self.in_tc += 1
                for n in node.body:
                    self.visit(n)
                self.in_tc -= 1
                for n in node.orelse:
                    self.visit(n)
            else:
                self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                add_import(alias.name, {alias.name.rsplit(".", 1)[-1]}, node.lineno, self.in_tc > 0)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level:  # relative import
                base = module.split(".")
                if not file.name == "__init__.py":
                    base = base[:-1]
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            names = {a.name for a in node.names}
            add_import(target, names, node.lineno, self.in_tc > 0)
            # `from pkg.mod import name` may address a submodule; record both.
            for a in node.names:
                add_import(f"{target}.{a.name}", {a.name}, node.lineno, self.in_tc > 0)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            info.classes.append(node.name)
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            body_nodes = node.body
            if body_nodes and isinstance(body_nodes[0], ast.Expr) and isinstance(
                getattr(body_nodes[0], "value", None), ast.Constant
            ):
                body_nodes = body_nodes[1:]  # drop docstring
            if body_nodes:
                start = body_nodes[0].lineno
                end = max(getattr(n, "end_lineno", n.lineno) for n in body_nodes)
                body_src = "\n".join(lines[start - 1 : end])
            else:
                body_src = ""
            info.functions.append(
                FunctionInfo(
                    module=module,
                    qualname=".".join(self.stack + [node.name]),
                    path=rel_path,
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    tokens=_normalise_tokens(body_src),
                )
            )
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _func  # type: ignore[assignment]
        visit_AsyncFunctionDef = _func  # type: ignore[assignment]

    Visitor().visit(tree)
    return info


def parse_module(
    repo: Path, rel_path: str, package: str, source: str | None = None
) -> ModuleInfo | None:
    """Parse one file at ``rel_path`` (optionally with ``source`` from another git ref)."""
    file = repo / rel_path
    if source is None:
        if not file.exists():
            return None
        source = file.read_text(encoding="utf-8", errors="replace")
    parts = Path(rel_path).with_suffix("").parts
    if package in parts:
        parts = parts[parts.index(package) :]
    elif parts and parts[0] == "tests":
        pass
    else:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    module = ".".join(parts)
    return _collect(file, module, rel_path, package, source)


def build_graph(repo: Path, src_dir: str = "src", package: str | None = None,
                tests_dir: str = "tests") -> ImportGraph:
    """Scan the repository and return the import graph (Python package or Java Maven modules)."""
    src = repo / src_dir
    if not src.exists() and any(repo.glob("*/src/main/java")):
        return _build_java_graph(repo)
    if package is None:
        candidates = [p for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists()]
        if not candidates:
            msg = f"no package under {src}"
            raise FileNotFoundError(msg)
        package = candidates[0].name
    modules: dict[str, ModuleInfo] = {}
    for file in sorted((src / package).rglob("*.py")):
        if "__pycache__" in file.parts or "generated" in file.parts:
            continue
        rel = file.relative_to(repo).as_posix()
        info = parse_module(repo, rel, package)
        if info:
            modules[info.name] = info
    tests: dict[str, ModuleInfo] = {}
    tdir = repo / tests_dir
    if tdir.exists():
        for file in sorted(tdir.rglob("test_*.py")):
            rel = file.relative_to(repo).as_posix()
            info = parse_module(repo, rel, package)
            if info:
                tests[info.name] = info
    reverse: dict[str, set[str]] = {}
    for m in modules.values():
        for target in m.imports:
            resolved = resolve(modules, target)
            if resolved and resolved != m.name:
                reverse.setdefault(resolved, set()).add(m.name)
    g = ImportGraph(package=package, modules=modules, reverse=reverse, test_modules=tests)
    for m in list(modules.values()) + list(tests.values()):
        m.context, m.layer = g.classify(m.name)
    return g


def resolve(modules: dict[str, ModuleInfo], target: str) -> str | None:
    """Map an imported dotted name to the module that defines it (``pkg.mod.Name`` → ``pkg.mod``)."""
    if target in modules:
        return target
    head, _, _ = target.rpartition(".")
    if head in modules:
        return head
    return None


def _build_java_graph(repo: Path) -> ImportGraph:
    from .graph_java import resolve_java, scan

    modules, tests = scan(repo)
    by_pkg: dict[str, dict[str, str]] = {}
    for m in modules.values():
        pkg, _, simple = m.name.rpartition(".")
        by_pkg.setdefault(pkg, {})[simple] = m.name
    reverse: dict[str, set[str]] = {}
    for m in modules.values():
        for target in m.imports:
            for resolved in resolve_java(modules, target):
                if resolved != m.name:
                    reverse.setdefault(resolved, set()).add(m.name)
        pkg = m.name.rpartition(".")[0]
        for simple in m.refs & set(by_pkg.get(pkg, {})):  # same package, no import statement needed
            target = by_pkg[pkg][simple]
            if target != m.name:
                reverse.setdefault(target, set()).add(m.name)
                m.imports.add(target)
                m.import_lines.setdefault(target, 1)
                m.imported_symbols.setdefault(target, set()).add(simple)
    for m in modules.values():  # a change to an implementation reaches whoever depends on its interface
        for simple in m.implements:
            iface = by_pkg.get(m.name.rpartition(".")[0], {}).get(simple) or next(
                (r for t in m.imports for r in resolve_java(modules, t) if r.rsplit(".", 1)[-1] == simple), None)
            if iface and iface in modules:
                reverse.setdefault(m.name, set()).update(reverse.get(iface, set()) - {m.name})
                reverse[m.name].add(iface)
    calls: dict[str, set[str]] = {}
    callers: dict[str, set[str]] = {}
    for m in modules.values():
        if m.context and m.service_calls:
            calls.setdefault(m.context, set()).update(m.service_calls)
            for c in m.service_calls:
                callers.setdefault(c, set()).add(m.context)
    return ImportGraph(package="", modules=modules, reverse=reverse, test_modules=tests, language="java",
                       service_calls=calls, service_callers=callers)
