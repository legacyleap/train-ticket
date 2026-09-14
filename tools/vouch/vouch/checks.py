"""The cheap layer: deterministic checks of one pull request against the inferred architecture.

Every function returns findings as plain dicts in the shape ``ui/DATA-SCHEMA.md`` documents.
Findings say in plain English what they found first; the trace is for whoever wants it.
"""

from __future__ import annotations

import fnmatch
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .diff import FileChange, show, snippet
from .graph import FunctionInfo, ImportGraph, ModuleInfo, parse_module, resolve
from . import rules as rules_file
from .infer import LAYER_ALLOWED, OWNERSHIP_VOCAB, SENSITIVE_PATHS

Finding = dict[str, Any]


def _label(conf: float) -> str:
    return "high" if conf >= 0.85 else "medium" if conf >= 0.6 else "low"


def _rule(arch: dict[str, Any], rid: str) -> dict[str, Any] | None:
    for r in arch["rules"]:
        if r["id"] == rid:
            return {"id": r["id"], "title": r["title"], "source": r["source"]}
    return None


def _finding(fid: str, kind: str, axis: str, severity: str, confidence: float, stakes: str,
             title: str, explanation: str, rule: dict[str, Any] | None, location: dict[str, Any],
             trace: list[dict[str, Any]], evidence: list[str], suggested_fix: str,
             fix_prompt: str, diff_snippet: str) -> Finding:
    return {
        "id": fid, "kind": kind, "axis": axis, "severity": severity, "confidence": round(confidence, 2),
        "confidence_label": _label(confidence), "stakes": stakes, "title": title,
        "explanation": explanation, "rule": rule, "location": location, "trace": trace,
        "evidence": evidence, "suggested_fix": suggested_fix, "fix_prompt": fix_prompt,
        "diff_snippet": diff_snippet,
    }


class PRContext:
    """Everything the checks need about one PR, parsed once."""

    def __init__(self, repo: Path, graph: ImportGraph, arch: dict[str, Any], files: list[FileChange],
                 base: str, head: str) -> None:
        self.repo = repo
        self.graph = graph
        self.arch = arch
        self.files = files
        self.base = base
        self.head = head
        self.pkg = graph.package
        self.head_modules: dict[str, ModuleInfo] = {}
        self.base_modules: dict[str, ModuleInfo] = {}
        ext = ".java" if graph.language == "java" else ".py"
        for fc in files:
            if not fc.path.endswith(ext):
                continue
            if fc.status != "deleted":
                src = show(repo, head, fc.path)
                if src is not None:
                    m = self._parse(fc.path, src)
                    if m:
                        self.head_modules[fc.path] = m
            if fc.status in ("modified", "renamed", "deleted"):
                src = show(repo, base, fc.old_path or fc.path)
                if src is not None:
                    m = self._parse(fc.old_path or fc.path, src)
                    if m:
                        self.base_modules[fc.path] = m

    def _parse(self, rel_path: str, src: str) -> ModuleInfo | None:
        if self.graph.language == "java":
            from .graph_java import parse_java

            if "/src/main/java/" not in rel_path and "/src/test/java/" not in rel_path:
                return None
            return parse_java(rel_path, src)
        return parse_module(self.repo, rel_path, self.pkg, src)

    def change(self, path: str) -> FileChange:
        return next(f for f in self.files if f.path == path)

    def src_files(self) -> list[FileChange]:
        return [f for f in self.files if f.path in self.head_modules
                and not f.path.startswith("tests/") and "/src/test/" not in f.path]

    def changed_functions(self, path: str) -> list[FunctionInfo]:
        """Functions in the head file whose lines were touched, plus brand-new ones."""
        m = self.head_modules.get(path)
        if not m:
            return []
        fc = self.change(path)
        base_fns = {f.qualname: f for f in self.base_modules.get(path, ModuleInfo("", "")).functions}
        out = []
        for fn in m.functions:
            touched = any(fn.line <= ln <= fn.end_line for ln in fc.added_lines)
            if fn.qualname not in base_fns:
                out.append(fn)
            elif touched and base_fns[fn.qualname].tokens != fn.tokens:
                out.append(fn)  # body changed, not just the docstring or formatting
        return out

    def new_functions(self, path: str) -> list[FunctionInfo]:
        m = self.head_modules.get(path)
        if not m:
            return []
        base_names = {f.qualname for f in self.base_modules.get(path, ModuleInfo("", "")).functions}
        return [f for f in m.functions if f.qualname not in base_names]


# --------------------------------------------------------------------------- layer + context rules


def _forbidden_imports(ctx: PRContext, m: ModuleInfo) -> list[tuple[str, str, str]]:
    """(target, rule_id, why) for each import that breaks a layer or context rule."""
    src_ctx, src_layer = _classify_any(ctx, m)
    out = []
    seen: set[str] = set()
    allowed = {k: set(v) for k, v in ctx.arch.get("layer_allowed", {}).items()} or LAYER_ALLOWED
    rid_of = ctx.arch.get("rule_ids", {"layer_domain": "DDD-012", "layer_app": "DDD-013", "layer": "DDD-013",
                                       "context": "DDD-010", "kernel": "SHARED-KERNEL"})
    kernel = (ctx.arch.get("kernel") or "") if ctx.graph.language == "java" else "shared"
    bounded = {c["id"] for c in ctx.arch["contexts"] if c["kind"] == "bounded_context"}
    for target in sorted(m.imports):
        if target in m.type_checking_only:
            continue
        resolved_all = ctx.graph.resolve_all(target) or _resolve_head(ctx, target)
        for resolved in resolved_all:
            if resolved == m.name or resolved in seen:
                continue
            seen.add(resolved)
            dst_ctx, dst_layer = ctx.graph.classify(resolved)
            if src_layer and dst_layer and src_ctx == dst_ctx and src_layer in allowed and dst_layer not in allowed[src_layer]:
                rid = rid_of.get("layer_domain", rid_of["layer"]) if src_layer == "domain" else rid_of.get("layer_app", rid_of["layer"])
                out.append((resolved, rid, f"{src_layer} imports {dst_layer}"))
            elif src_ctx and dst_ctx and src_ctx != dst_ctx:
                if src_ctx in bounded and dst_ctx in bounded:
                    out.append((resolved, rid_of["context"], f"{src_ctx} imports {dst_ctx}"))
                elif src_ctx == kernel and dst_ctx in bounded:
                    out.append((resolved, rid_of["kernel"], f"{kernel} imports {dst_ctx}"))
                elif src_layer in ("domain", "application") and dst_layer == "infrastructure":
                    out.append((resolved, rid_of["layer"], f"{src_ctx}.{src_layer} imports {dst_ctx}.infrastructure"))
    return out


def _resolve_head(ctx: "PRContext", target: str) -> list[str]:
    """Imports of files that only exist on the head branch (new modules in the same PR)."""
    heads = ctx.head_modules_by_name()
    if ctx.graph.language == "java":
        from .graph_java import resolve_java

        return resolve_java(heads, target)
    r = resolve(heads, target)
    return [r] if r else []


def _classify_any(ctx: "PRContext", m: ModuleInfo) -> tuple[str | None, str | None]:
    """Placement of a module that may only exist on the head branch (new file)."""
    if m.context is not None or ctx.graph.language == "python":
        return (m.context, m.layer) if m.context is not None else ctx.graph.classify(m.name)
    from .graph_java import context_of, layer_of

    return context_of(m.path), layer_of(m.path)


def _head_modules_by_name(self: PRContext) -> dict[str, ModuleInfo]:
    return {m.name: m for m in self.head_modules.values()}


PRContext.head_modules_by_name = _head_modules_by_name  # type: ignore[attr-defined]


def check_layers(ctx: PRContext, next_id: Any) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    checked = 0
    for fc in ctx.src_files():
        head = ctx.head_modules[fc.path]
        base = ctx.base_modules.get(fc.path)
        before = set(_forbidden_imports(ctx, base)) if base else set()
        checked += 1
        for target, rid, why in _forbidden_imports(ctx, head):
            if (target, rid, why) in before:
                continue  # pre-existing on main; not this PR's doing
            line = head.import_lines.get(target) or next(
                (ln for t, ln in head.import_lines.items() if t.startswith(target)), 1)
            src_ctx, src_layer = _classify_any(ctx, head)
            dst_ctx, dst_layer = ctx.graph.classify(target)
            kind = "layer_violation"
            rid_of = ctx.arch.get("rule_ids", {})
            if rid in ("DDD-010", "SHARED-KERNEL", rid_of.get("context"), rid_of.get("kernel")):
                title = f"{src_ctx} now imports {dst_ctx}: bounded contexts must stay isolated"
                explanation = (
                    f"`{head.path}` imports `{target}`. {src_ctx} and {dst_ctx} are separate bounded "
                    f"contexts; the architecture routes anything they share through `shared/` ports "
                    f"so that either can change without the other. This is the first import edge "
                    f"from {src_ctx} to {dst_ctx} in the repository."
                )
                fix = (f"Move the needed type into `shared/domain/` (or expose it behind a port in "
                       f"`shared/application/ports_out/`) and import it from there, or keep this logic in "
                       f"`{dst_ctx}` where its vocabulary already lives.")
            elif ctx.graph.language == "java":
                title = f"{src_layer} layer imports {dst_layer}: `{target.rsplit('.', 1)[-1]}`"
                explanation = (
                    f"`{head.path}` ({src_ctx}/{src_layer}) imports `{target}` ({dst_ctx}/{dst_layer}). "
                    f"In this system the direction is controller → service → repository/entity: the service layer is "
                    f"where validation, transactions and orchestration live and where they are tested. A {src_layer} "
                    f"that reaches into the {dst_layer} skips all of that."
                )
                fix = (f"Call the {src_ctx} service layer instead: add the method you need to the service interface "
                       f"and implementation, and drop the direct import of `{target.rsplit('.', 1)[-1]}`.")
            else:
                title = f"{src_layer} layer imports {dst_layer}: `{target.rsplit('.', 1)[-1]}`"
                explanation = (
                    f"`{head.path}` ({src_ctx}/{src_layer}) imports `{target}` ({dst_ctx}/{dst_layer}). "
                    f"The dependency direction is domain ← application ← infrastructure; concretions such as "
                    f"Neo4j repositories reach use cases only through a port injected by `infra_factory`. "
                    f"The existing architecture test will fail CI on this import."
                )
                fix = (f"Depend on the port instead: add or reuse a `Protocol` in "
                       f"`{src_ctx}/application/ports_out/`, inject the adapter from `infra_factory/{src_ctx}.py`, "
                       f"and drop the direct import.")
            findings.append(_finding(
                next_id(), kind, "architectural", "high", 0.98, "high", title, explanation,
                _rule(ctx.arch, rid),
                {"path": head.path, "line": line, "symbol": target},
                [
                    {"hop": 0, "module": head.name, "symbol": "import", "path": head.path, "line": line,
                     "note": f"new import added in this PR ({why})"},
                    {"hop": 1, "module": target, "symbol": target.rsplit(".", 1)[-1],
                     "path": _path_of(ctx, target), "line": 1,
                     "note": f"lives in {dst_ctx}/{dst_layer or 'package'}"},
                ],
                [f"rule {rid}: {why}", "import is at runtime (not under TYPE_CHECKING)",
                 "absent on the base branch"],
                fix,
                (f"In {head.path}, remove the direct import of {target}; add the needed method to the {src_ctx} service "
                 f"interface/implementation and call it from here. Keep behaviour identical."
                 if ctx.graph.language == "java" else
                 f"In {head.path}, remove the direct import of {target} and depend on an outbound port in "
                 f"{src_ctx}/application/ports_out/ instead; wire the adapter in infra_factory. Keep behaviour identical."),
                snippet(fc.patch, line),
            ))
    conformant = []
    if checked and not findings:
        rid_of = ctx.arch.get("rule_ids", {})
        conformant.append({"check": "layer_rules", "label": "Layer dependency direction",
                           "detail": f"{checked} file(s), 0 forbidden imports", "rule": rid_of.get("layer_domain", rid_of.get("layer", "DDD-012"))})
        conformant.append({"check": "context_isolation", "label": "Service / bounded-context isolation",
                           "detail": f"{checked} file(s), no new cross-context edges", "rule": rid_of.get("context", "DDD-010")})
    elif checked:
        kinds = {f["rule"]["id"] for f in findings if f["rule"]}
        rid_of = ctx.arch.get("rule_ids", {})
        if not kinds & {"DDD-010", "SHARED-KERNEL", rid_of.get("context"), rid_of.get("kernel")}:
            conformant.append({"check": "context_isolation", "label": "Bounded-context isolation",
                               "detail": "no new cross-context edges", "rule": "DDD-010"})
        if not kinds & {"DDD-012", "DDD-013", rid_of.get("layer")}:
            conformant.append({"check": "layer_rules", "label": "Layer dependency direction",
                               "detail": "no forbidden layer imports", "rule": "DDD-012"})
    return findings, conformant


def _path_of(ctx: PRContext, module: str) -> str:
    m = ctx.graph.modules.get(module) or ctx.head_modules_by_name().get(module)
    return m.path if m else module.replace(".", "/") + ".py"


# --------------------------------------------------------------------------- wrong context


def check_wrong_context(ctx: PRContext, next_id: Any) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    checked = 0
    # files that call another service first, so the strongest evidence leads the finding
    for fc in sorted(ctx.src_files(), key=lambda f: -len(ctx.head_modules[f.path].service_calls)):
        head = ctx.head_modules[fc.path]
        src_ctx, src_layer = _classify_any(ctx, head)
        bounded = {c["id"] for c in ctx.arch["contexts"] if c["kind"] == "bounded_context"}
        if src_ctx not in bounded:
            continue
        new_fns = ctx.new_functions(fc.path)
        if fc.status != "added" and not new_fns:
            continue
        checked += 1
        hits: dict[str, list[str]] = {}
        vocab: dict[str, tuple[str, ...]]
        if ctx.graph.language == "java":
            # class names only: a service legitimately *mentions* orders; it must not *define* Order things
            base_classes = set(ctx.base_modules[fc.path].classes) if fc.path in ctx.base_modules else set()
            kernel_names = _kernel_entity_names(ctx)
            text = " ".join(c.lower() for c in head.classes if c not in base_classes and c not in kernel_names)
            vocab = {}
            for ar in ctx.arch["rules"]:
                if ar["kind"] == "ownership" and ar["id"].startswith("OWNS-") and ar.get("status") != "rejected":
                    owner = next((c["id"] for c in ctx.arch["contexts"] if ar["title"].startswith(c["id"] + " ")), None)
                    if owner:
                        vocab[owner] = (ar["id"][5:].lower(),)
        else:
            # only the part of the path below the context directory: the package name itself
            # contains "discovery", which must not count as vocabulary.
            below_ctx = fc.path.lower().split(f"/{src_ctx}/", 1)[-1]
            text = " ".join([below_ctx] + [f.qualname.lower() for f in new_fns] + [c.lower() for c in head.classes])
            vocab = dict(OWNERSHIP_VOCAB)
        for ar in rules_file.active(ctx.arch, "ownership"):
            o = ar["params"].get("context")
            if o:
                vocab[o] = tuple(vocab.get(o, ())) + tuple(ar["params"].get("vocabulary", []))
        for owner, words in vocab.items():
            for w in words:
                if w in text:
                    hits.setdefault(owner, []).append(w)
        foreign = {o: ws for o, ws in hits.items() if o != src_ctx}
        own = hits.get(src_ctx, [])
        if not foreign or (own and len(own) >= max(len(v) for v in foreign.values())):
            continue
        owner, words = max(foreign.items(), key=lambda kv: len(kv[1]))
        prior = next((f for f in findings if f["kind"] == "wrong_context" and f["rule"] and f["rule"]["id"] == f"OWNS-{words[0].upper()}"), None)
        if prior is not None:
            prior["evidence"].append(f"also: {fc.path}")
            prior["trace"].append({"hop": 0, "module": head.name, "symbol": head.classes[0] if head.classes else "",
                                   "path": fc.path, "line": 1, "note": "same PR, same foreign vocabulary"})
            continue
        if ctx.graph.language == "java":
            cross_imports = [t for t in sorted(head.service_calls) if t == owner]
            rule = _rule(ctx.arch, f"OWNS-{words[0].upper()}")
        else:
            cross_imports = [t for t in head.imports if t.startswith(f"{ctx.pkg}.{owner}.")]
            rule = _rule(ctx.arch, f"OWNS-{owner.upper()}")
        conf = 0.7 + (0.15 if cross_imports else 0) + (0.05 if fc.status == "added" else 0)
        sym = new_fns[0].qualname if new_fns else head.name.rsplit(".", 1)[-1]
        line = new_fns[0].line if new_fns else 1
        findings.append(_finding(
            next_id(), "wrong_context", "architectural", "medium" if not cross_imports else "high",
            min(conf, 0.92), "medium",
            f"{owner.capitalize()} logic added to the {src_ctx} context",
            (f"`{fc.path}` introduces `{sym}`, which is about {', '.join(sorted(set(words)))}. By the "
             f"intended architecture that responsibility belongs to **{owner}** "
             f"({rule['title'] if rule else owner}). The code may be fine; it is in the wrong service, "
             f"and the next person will look for it in {owner}."
             + (f" It also imports {len(cross_imports)} module(s) from {owner} to do its work."
                if cross_imports else "")),
            rule,
            {"path": fc.path, "line": line, "symbol": sym},
            [{"hop": 0, "module": head.name, "symbol": sym, "path": fc.path, "line": line,
              "note": f"new in this PR, inside {src_ctx}/{src_layer or ''}"}]
            + [{"hop": 1, "module": t, "symbol": t.rsplit(".", 1)[-1], "path": _path_of(ctx, t), "line": 1,
                "note": f"imported from {owner}" if ctx.graph.language != "java" else f"calls {owner} over REST to do it"}
               for t in sorted(cross_imports)[:3]],
            [f"vocabulary: {', '.join(sorted(set(words)))}",
             f"ownership rule from CLAUDE.md: {owner}",
             "heuristic — vocabulary and imports, not semantics"],
            (f"Move `{sym}` into `{owner}` and expose what {src_ctx} needs as an endpoint there."
             if ctx.graph.language == "java" else
             f"Move `{sym}` under `src/{ctx.pkg}/{owner}/application/` (a service or use case) and have "
             f"{src_ctx} call it through a shared port, or record a waiver in the PR (DDD-003)."),
            f"Relocate {sym} from {fc.path} into the {owner} bounded context under application/services, "
            f"expose it via a port in shared/application/ports_out if {src_ctx} must call it, and update imports.",
            snippet(fc.patch, line),
        ))
    conformant = []
    if checked and not findings:
        conformant.append({"check": "ownership", "label": "Code placed in the owning context",
                           "detail": f"{checked} file(s) with new code, vocabulary matches the context",
                           "rule": "OWNS-*"})
    return findings, conformant


# --------------------------------------------------------------------------- transitive impact


def check_transitive_impact(ctx: PRContext, next_id: Any, max_hops: int = 3
                            ) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    traced = 0
    for fc in ctx.src_files():
        if fc.status == "added":
            continue
        head = ctx.head_modules[fc.path]
        fns = [f for f in ctx.changed_functions(fc.path) if f.qualname in
               {b.qualname for b in ctx.base_modules.get(fc.path, ModuleInfo("", "")).functions}]
        if not fns:
            continue
        for fn in fns:
            traced += 1
            symbol = fn.qualname.split(".")[0]
            # hop 1: modules that import this module *and* name the symbol (or import the module wholesale)
            hop1 = sorted(
                d for d in ctx.graph.dependants(head.name)
                if symbol in ctx.graph.modules[d].imported_symbols.get(head.name, set())
                or head.name in ctx.graph.modules[d].imports
                or ctx.graph.language == "java"
            )
            hop1 = sorted(set(hop1) | _rest_callers(ctx, head.name))
            if not hop1:
                continue
            layers: list[list[str]] = [hop1]
            seen = set(hop1) | {head.name}
            for _ in range(max_hops - 1):
                nxt = sorted({d for m in layers[-1] for d in (ctx.graph.dependants(m) | _rest_callers(ctx, m)) if d not in seen})
                if not nxt:
                    break
                seen |= set(nxt)
                layers.append(nxt)
            total = sum(len(l) for l in layers)
            contexts = sorted({ctx.graph.classify(m)[0] or "?" for l in layers for m in l})
            direct_tests = ctx.graph.tests_importing(head.name)
            far = layers[-1]
            far_tests = {t for m in far for t in ctx.graph.tests_importing(m)}
            if direct_tests and (len(layers) < 3 or len(contexts) < 2 or total < 10):
                continue  # covered and local: nothing a reviewer needs to hear
            # pick an illustrative chain: prefer a use case / bounded-context endpoint
            chain = _chain(ctx, head.name, layers, fn.qualname, fn.line)
            untested = not direct_tests
            severity = "high" if (len(contexts) >= 2 and untested) else "medium"
            conf = 0.9 if untested else 0.75
            title = (f"`{symbol}` change reaches {total} module(s) across {len(contexts)} context(s)"
                     + (", on a path with no direct test" if untested else ""))
            explanation = (
                f"The diff to `{symbol}` in `{fc.path}` looks local, but {len(hop1)} module(s) import it and "
                f"{total} depend on it within {len(layers)} hop(s): {', '.join(contexts)}. "
                + (f"No test imports `{head.name}` directly, so a changed return value would surface first in "
                   f"`{chain[-1]['module'].rsplit('.', 1)[-1]}`." if untested else
                   f"{len(direct_tests)} test module(s) cover it directly; {len(far_tests)} cover the far end.")
            )
            findings.append(_finding(
                next_id(), "transitive_impact", "architectural", severity, conf,
                "high" if len(contexts) >= 2 else "medium", title, explanation,
                _rule(ctx.arch, "IMPACT"),
                {"path": fc.path, "line": fn.line, "symbol": fn.qualname},
                chain,
                [f"{len(hop1)} direct importer(s), {total} within {len(layers)} hops",
                 f"contexts reached: {', '.join(contexts)}",
                 f"direct tests: {len(direct_tests)}; tests at the far end: {len(far_tests)}"],
                (f"Add a unit test for `{symbol}` covering the new input shape, and run "
                 f"`{', '.join(sorted(far_tests)[:2]) or 'the affected context suites'}` before merge. "
                 + ("Callers reach this over REST, so their contract tests are the only net; " if ctx.graph.language == "java" else "")
                 + "if the return value changes for existing inputs, state it in the PR."),
                (f"Write a {'JUnit test' if ctx.graph.language == 'java' else 'pytest'} for {fn.qualname} that pins the old and new behaviour of the change in "
                 f"{fc.path}, and grep the {len(hop1)} importer(s) of {head.name} for call sites that depend on the old output."),
                snippet(fc.patch, fn.line),
            ))
    conformant = []
    if traced and not findings:
        conformant.append({"check": "impact", "label": "Transitive impact",
                           "detail": f"{traced} changed function(s); dependants are covered by tests or none exist",
                           "rule": "IMPACT"})
    return findings, conformant


def _rest_callers(ctx: PRContext, module: str) -> set[str]:
    """Java: the service classes in other services that call this module's service over REST.

    Only API-facing modules propagate over REST (controllers, and the service classes behind them).
    """
    if ctx.graph.language != "java":
        return set()
    m = ctx.graph.modules.get(module)
    if not m or not m.context or m.layer not in ("controller", "service"):
        return set()
    out = set()
    for caller_ctx in ctx.graph.service_callers.get(m.context, set()):
        for cm in ctx.graph.modules.values():
            if cm.context == caller_ctx and m.context in cm.service_calls:
                out.add(cm.name)
    return out


def _main_symbol(mi: ModuleInfo) -> str:
    base = mi.name.rsplit(".", 1)[-1].replace("_", "").lower()
    for c in mi.classes:
        if c.lower() == base or base.endswith(c.lower()):
            return c
    return mi.classes[0] if mi.classes else mi.name.rsplit(".", 1)[-1]


def _chain(ctx: PRContext, start: str, layers: list[list[str]], symbol: str = "", line0: int = 1) -> list[dict[str, Any]]:
    """One readable path through the layers, preferring use cases and other contexts."""
    start_ctx = ctx.graph.classify(start)[0]

    def score(m: str) -> tuple[int, int, str]:
        c, layer = ctx.graph.classify(m)
        if ctx.graph.language == "java":
            return (0 if c != start_ctx else 1, 0 if layer == "service" else 1, m)
        return (0 if "use_case" in m else 1, 0 if c in ("discovery", "reporting") else 1, m)

    chain = [{"hop": 0, "module": start, "symbol": symbol, "path": _path_of(ctx, start), "line": line0, "note": "changed here"}]
    prev = start
    for i, layer in enumerate(layers, 1):
        prev_ctx = ctx.graph.classify(prev)[0]
        rest = [m for m in layer if prev_ctx and prev_ctx in ctx.graph.modules[m].service_calls
                and ctx.graph.modules[m].context != prev_ctx]
        candidates = [m for m in layer if prev in ctx.graph.modules[m].imports
                      or any(t.startswith(prev + ".") for t in ctx.graph.modules[m].imports)] or rest or layer
        m = sorted(candidates, key=score)[0]
        mi = ctx.graph.modules[m]
        via_rest = m in rest and prev not in mi.imports
        line = mi.import_lines.get(prev) or next((ln for t, ln in mi.import_lines.items() if t.startswith(prev)), 1)
        if via_rest and prev_ctx:
            src_text = (ctx.repo / mi.path).read_text(encoding="utf-8", errors="replace") if (ctx.repo / mi.path).exists() else ""
            line = next((ln for ln, l in enumerate(src_text.splitlines(), 1) if f'"{prev_ctx}"' in l), line)
        c, lyr = ctx.graph.classify(m)
        how = f"calls {prev_ctx} over REST" if via_rest else f"imports hop {i - 1}"
        chain.append({"hop": i, "module": m, "symbol": _main_symbol(mi),
                      "path": mi.path, "line": line,
                      "note": f"{how}; {c}/{lyr or 'package'}; {len(layer)} module(s) at this depth"})
        prev = m
    tests = ctx.graph.tests_importing(chain[-1]["module"])
    chain[-1]["note"] += "; " + (f"covered by {len(tests)} test module(s)" if tests else "no test imports this module")
    return chain


# --------------------------------------------------------------------------- duplicate logic


def check_duplicates(ctx: PRContext, next_id: Any, threshold: float = 0.85
                     ) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    checked = 0
    existing: list[FunctionInfo] = [f for m in ctx.graph.modules.values() for f in m.functions if len(f.tokens) >= 6]
    for fc in ctx.src_files():
        if ctx.graph.language == "java":
            head_m = ctx.head_modules[fc.path]
            h_ctx, h_layer = _classify_any(ctx, head_m)
            kernel_names = _kernel_entity_names(ctx)
            base_classes = set(ctx.base_modules[fc.path].classes) if fc.path in ctx.base_modules else set()
            for cls in head_m.classes:
                if cls in base_classes or cls not in kernel_names or h_layer != "entity" or h_ctx == ctx.arch.get("kernel"):
                    continue
                checked += 1
                kernel = kernel_names[cls]
                baseline = next((b for b in ctx.arch.get("baseline", []) if b["rule"] == "KERNEL-ENTITY"), None)
                findings.append(_finding(
                    next_id(), "duplicate_logic", "architectural", "medium", 0.92, "medium",
                    f"`{cls}` copied into {h_ctx} — it already exists in ts-common",
                    (f"`{fc.path}` defines `{cls}`, which is already `{kernel}` in the shared kernel that every service "
                     f"imports. A second definition drifts silently; consumers must know which one they are talking to."
                     + (f" The repository already carries {baseline['count']} such copies (see Drift → existing drift)." if baseline else "")),
                    _rule(ctx.arch, "KERNEL-ENTITY"), {"path": fc.path, "line": 1, "symbol": cls},
                    [{"hop": 0, "module": head_m.name, "symbol": cls, "path": fc.path, "line": 1, "note": "new in this PR"},
                     {"hop": 1, "module": kernel, "symbol": cls, "path": _path_of(ctx, kernel), "line": 1,
                      "note": f"the shared definition, imported by {len(ctx.graph.dependants(kernel))} module(s)"}],
                    [f"same class name as {kernel}", "shared kernel ts-common is on every service's classpath",
                     f"baseline: {baseline['count']} existing copies" if baseline else ""],
                    f"Delete `{cls}` here and `import {kernel};`. If the shared one lacks a field, add it there.",
                    f"Remove {fc.path} and replace usages with {kernel}; add missing fields to the shared entity.",
                    snippet(fc.patch, 1)))
            if h_layer == "controller":
                continue  # controller methods are one-line pass-throughs by design; copies there are the pattern
        for fn in ctx.new_functions(fc.path):
            if len(fn.tokens) < (30 if ctx.graph.language == "java" else 6):
                continue
            checked += 1
            best, best_ratio = None, 0.0
            for other in existing:
                if other.module == ctx.head_modules[fc.path].name and other.qualname == fn.qualname:
                    continue
                if abs(len(other.tokens) - len(fn.tokens)) > max(len(fn.tokens), 8):
                    continue
                r = SequenceMatcher(None, fn.tokens, other.tokens, autojunk=False).ratio()
                if r > best_ratio:
                    best, best_ratio = other, r
            parses_fqn = _parses_fqn(fn)
            if best and best_ratio >= threshold:
                findings.append(_finding(
                    next_id(), "duplicate_logic", "architectural", "medium", min(0.6 + best_ratio * 0.35, 0.95),
                    "medium",
                    f"`{fn.qualname}` re-implements `{best.qualname}` from `{best.module.rsplit('.', 1)[-1]}`",
                    (f"The new function in `{fc.path}` is {int(best_ratio * 100)}% token-identical to "
                     f"`{best.qualname}` in `{best.path}`, which the rest of the codebase already uses. "
                     f"Two copies drift apart silently. The choice is: use the existing helper, or move it to "
                     f"`shared/` if the current location is the problem."),
                    _rule(ctx.arch, "ADS-010"),
                    {"path": fc.path, "line": fn.line, "symbol": fn.qualname},
                    [{"hop": 0, "module": ctx.head_modules[fc.path].name, "symbol": fn.qualname, "path": fc.path,
                      "line": fn.line, "note": "new in this PR"},
                     {"hop": 1, "module": best.module, "symbol": best.qualname, "path": best.path, "line": best.line,
                      "note": f"existing implementation, used by {len(ctx.graph.dependants(best.module))} module(s)"}],
                    [f"token similarity {best_ratio:.2f} (threshold {threshold})",
                     f"existing helper has {len(ctx.graph.dependants(best.module))} importer(s)"],
                    f"Delete `{fn.qualname}` and import `{best.qualname}` from `{best.module}`.",
                    f"Replace the body of {fn.qualname} in {fc.path} with a call to {best.module}.{best.qualname} "
                    f"(or remove it and update callers), keeping the signature.",
                    snippet(fc.patch, fn.line),
                ))
            elif parses_fqn:
                findings.append(_finding(
                    next_id(), "duplicate_logic", "architectural", "medium", 0.8, "medium",
                    f"`{fn.qualname}` parses FQN structure that the `FQN` value object owns",
                    (f"`{fn.qualname}` in `{fc.path}` splits an FQN string to read its parts. FQNs are opaque "
                     f"identifiers (ADS-015); their structure is known only to "
                     f"`shared/domain/value_objects/fqn.py`. A second parser will break the day the format changes."),
                    _rule(ctx.arch, "ADS-015"),
                    {"path": fc.path, "line": fn.line, "symbol": fn.qualname},
                    [{"hop": 0, "module": ctx.head_modules[fc.path].name, "symbol": fn.qualname, "path": fc.path,
                      "line": fn.line, "note": "new in this PR"},
                     {"hop": 1, "module": f"{ctx.pkg}.shared.domain.value_objects.fqn", "symbol": "FQN",
                      "path": f"src/{ctx.pkg}/shared/domain/value_objects/fqn.py", "line": 27,
                      "note": "the value object that owns FQN structure"}],
                    ["body splits on '//' or '.' with an fqn-named argument", "ADS-015: FQNs are opaque"],
                    "Add the accessor you need to the `FQN` value object (or use the one that exists) and delete the parser.",
                    f"Remove {fn.qualname} from {fc.path}; add a method on FQN in shared/domain/value_objects/fqn.py "
                    f"that exposes the needed component, with a unit test.",
                    snippet(fc.patch, fn.line),
                ))
    conformant = []
    if checked and not findings:
        conformant.append({"check": "duplication", "label": "No duplicated helpers",
                           "detail": f"{checked} new function(s) compared against {len(existing)} existing",
                           "rule": "ADS-010"})
    return findings, conformant


def _baseline_note(ctx: PRContext, rule: str, what: str) -> str:
    b = next((x for x in ctx.arch.get("baseline", []) if x["rule"] == rule), None)
    if not b or not b["count"]:
        return ""
    names = ", ".join(e["module"].rsplit(".", 1)[-1] for e in b["examples"][:2])
    return f" {b['count']} {what} ({names}) — this would be one more."


def _kernel_entity_names(ctx: PRContext) -> dict[str, str]:
    kernel = ctx.arch.get("kernel")
    return {m.name.rsplit(".", 1)[-1]: m.name for m in ctx.graph.modules.values()
            if kernel and m.context == kernel and m.layer == "entity"}


def _parses_fqn(fn: FunctionInfo) -> bool:
    toks = fn.tokens
    return ("split" in toks or "partition" in toks or "rpartition" in toks) and "fqn" in fn.qualname.lower()


# --------------------------------------------------------------------------- naming


def check_naming(ctx: PRContext, next_id: Any) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    new_use_cases = 0
    for fc in ctx.src_files():
        if "/application/use_cases/" not in fc.path or fc.path.endswith("__init__.py"):
            continue
        head = ctx.head_modules[fc.path]
        base = ctx.base_modules.get(fc.path)
        new_classes = [c for c in head.classes if c.endswith("UseCase") or (not base or c not in base.classes)]
        if fc.status == "added":
            new_use_cases += 1
            if not fc.path.endswith("_use_case.py") and "/base/" not in fc.path:
                findings.append(_finding(
                    next_id(), "layer_violation", "architectural", "low", 0.9, "low",
                    f"Use-case module `{Path(fc.path).name}` does not end with `_use_case.py`",
                    f"`{fc.path}` is new under `application/use_cases/` but its name does not follow DDD-034.",
                    _rule(ctx.arch, "DDD-034"), {"path": fc.path, "line": 1, "symbol": Path(fc.path).name},
                    [], ["DDD-034"], "Rename the module to `<subject>_<action>_use_case.py`.",
                    f"Rename {fc.path} to end with _use_case.py and update imports.", snippet(fc.patch, 1)))
        for c in new_classes:
            if base and c in base.classes:
                continue
            if not c.endswith("UseCase") and "/base/" not in fc.path and c[0].isupper() and "Error" not in c:
                findings.append(_finding(
                    next_id(), "layer_violation", "architectural", "low", 0.85, "low",
                    f"Class `{c}` under use_cases/ lacks the `UseCase` suffix",
                    f"`{c}` in `{fc.path}` is a new class in the use-case layer; DDD-033 requires `<Subject><Action>UseCase`.",
                    _rule(ctx.arch, "DDD-033"), {"path": fc.path, "line": 1, "symbol": c}, [], ["DDD-033"],
                    f"Rename `{c}` to `<Subject><Action>UseCase`.", f"Rename class {c} in {fc.path} per DDD-033.",
                    snippet(fc.patch, 1)))
    conformant = []
    if not findings:
        conformant.append({"check": "naming", "label": "Use-case naming (DDD-033/034)",
                           "detail": f"{new_use_cases} new use-case module(s)" if new_use_cases else "no new use cases",
                           "rule": "DDD-033"})
    return findings, conformant


# --------------------------------------------------------------------------- ticket scope


def check_scope(ctx: PRContext, ticket: dict[str, Any] | None, next_id: Any
                ) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    if not ticket:
        return findings, [{"check": "ticket_scope", "label": "Ticket scope",
                           "detail": "no ticket linked — functional conformance not checked", "rule": "TICKET-SCOPE"}]
    scope = ticket.get("scope", {})
    globs: list[str] = list(scope.get("paths", []))
    allowed_sensitive: list[str] = [s.lower() for s in scope.get("sensitive_ok", [])]
    outside: list[FileChange] = []
    sensitive: list[tuple[FileChange, str]] = []
    sensitive_paths: list[tuple[str, str]] = list(SENSITIVE_PATHS)
    for ar in rules_file.active(ctx.arch, "sensitive_path"):
        for needle in ar["params"].get("paths", []):
            sensitive_paths.append((needle, f"{ar['params'].get('why', ar['title'])} (rule {ar['id']})"))
    for fc in ctx.files:
        in_scope = any(fnmatch.fnmatch(fc.path, g) or fc.path.startswith(g.rstrip("*")) for g in globs)
        if fc.path.startswith("tests/") or fc.path.endswith(".md"):
            in_scope = True
        if not in_scope:
            outside.append(fc)
        for needle, why in sensitive_paths:
            if (needle in fc.path or fnmatch.fnmatch(fc.path, needle)) and needle.lower() not in allowed_sensitive:
                sensitive.append((fc, why))
                break
    new_surfaces = [fc for fc in ctx.files if fc.status == "added" and "/interface/" in fc.path]
    if not outside and not sensitive:
        return findings, [{"check": "ticket_scope", "label": f"Ticket scope ({ticket.get('key', '?')})",
                           "detail": f"all {len(ctx.files)} file(s) inside the declared scope; no sensitive areas touched",
                           "rule": "TICKET-SCOPE"}]
    ttype = ticket.get("type", "change")
    parts = []
    if outside:
        parts.append(f"{len(outside)} file(s) fall outside the scope the ticket declares "
                     f"({', '.join('`' + f.path + '`' for f in outside[:3])}{'…' if len(outside) > 3 else ''})")
    if sensitive:
        parts.append("it touches " + "; ".join(f"`{f.path}` ({why})" for f, why in sensitive[:3]))
    if new_surfaces:
        parts.append(f"it adds {len(new_surfaces)} new user-facing surface(s) under interface/")
    severity = "high" if sensitive or new_surfaces else "medium"
    title = (f"PR claims a {ttype} for {ticket.get('key', 'the ticket')} but also "
             + ("changes feature toggles and adds a CLI command" if sensitive and new_surfaces
                else "touches sensitive areas" if sensitive
                else "adds a new surface" if new_surfaces
                else f"changes {len(outside)} unrelated file(s)"))
    findings.append(_finding(
        next_id(), "scope_drift", "functional", severity, 0.85 if (sensitive or new_surfaces) else 0.75, "high",
        title,
        (f"{ticket.get('key', 'The ticket')} asks for: *{ticket.get('summary', '')}*. The diff does that, and "
         + "; ".join(parts) + ". Doing more than asked is the characteristic failure of AI-generated changes; "
         "each extra piece needs its own ticket, review and toggle."),
        _rule(ctx.arch, "TICKET-SCOPE"),
        {"path": (sensitive[0][0].path if sensitive else outside[0].path), "line": 1,
         "symbol": ticket.get("key", "")},
        [{"hop": 0, "module": "ticket", "symbol": ticket.get("key", ""), "path": "Jira", "line": 0,
          "note": f"scope: {', '.join(globs) or 'unspecified'}"}]
        + [{"hop": i + 1, "module": f.path, "symbol": f.status, "path": f.path, "line": 1,
            "note": "outside declared scope" + (
                f"; {next(w for n, w in sensitive_paths if n in f.path)}" if any(n in f.path for n, _ in sensitive_paths) else "")}
           for i, f in enumerate(outside[:5])],
        [f"{len(outside)} of {len(ctx.files)} files outside scope",
         f"sensitive areas: {len(sensitive)}", f"new interface surfaces: {len(new_surfaces)}"],
        "Split the PR: keep the ticketed fix here; move the toggle and the new command to their own ticket "
        "behind a feature toggle (TBD-012), with the architect's sign-off on the new surface.",
        f"Split this branch: keep only the changes to {', '.join(g for g in globs) or 'the ticketed files'} for "
        f"{ticket.get('key', '')}; move {', '.join(f.path for f in outside)} to a new branch with its own ticket.",
        snippet(sensitive[0][0].patch if sensitive else outside[0].patch, 1),
    ))
    return findings, []


# --------------------------------------------------------------------------- architect rules (.vouch/rules.json)


def _match_side(spec: dict[str, Any], module: str, path: str, graph: Any) -> bool:
    ctx, layer = graph.classify(module)
    if spec.get("context") and spec["context"] != ctx:
        return False
    if spec.get("layer") and spec["layer"] != layer:
        return False
    if spec.get("path_glob") and not fnmatch.fnmatch(path, spec["path_glob"]):
        return False
    if spec.get("module_prefix") and not module.startswith(spec["module_prefix"]):
        return False
    return True


def check_architect_rules(ctx: PRContext, next_id: Any) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Rules the architect wrote to `.vouch/rules.json` that Vouch can check deterministically."""
    findings: list[Finding] = []
    conformant: list[dict[str, Any]] = []
    for ar in rules_file.active(ctx.arch, "forbidden_import"):
        frm, to = ar["params"].get("from", {}), ar["params"].get("to", {})
        hits = 0
        for fc in ctx.src_files():
            head = ctx.head_modules[fc.path]
            if not _match_side(frm, head.name, fc.path, ctx.graph):
                continue
            base = ctx.base_modules.get(fc.path)
            before = set(base.imports) if base else set()
            for target in sorted(head.imports - before):
                if target in head.type_checking_only:
                    continue
                resolved = resolve(ctx.graph.modules, target) or resolve(ctx.head_modules_by_name(), target) or target
                if not _match_side(to, resolved, _path_of(ctx, resolved), ctx.graph):
                    continue
                hits += 1
                line = head.import_lines.get(target, 1)
                findings.append(_finding(
                    next_id(), "layer_violation", "architectural", ar.get("severity", "high"), 0.95, "high",
                    f"{ar['title']}: `{head.name.rsplit('.', 1)[-1]}` imports `{resolved.rsplit('.', 1)[-1]}`",
                    f"`{fc.path}` adds an import of `{resolved}`. {ar['statement']} This rule was written by the "
                    f"architect ({ar.get('source', rules_file.RULES_PATH)}), not inferred.",
                    _rule(ctx.arch, ar["id"]), {"path": fc.path, "line": line, "symbol": resolved},
                    [{"hop": 0, "module": head.name, "symbol": "import", "path": fc.path, "line": line,
                      "note": "new import in this PR (architect rule)"},
                     {"hop": 1, "module": resolved, "symbol": resolved.rsplit(".", 1)[-1], "path": _path_of(ctx, resolved),
                      "line": 1, "note": "forbidden target"}],
                    [f"rule {ar['id']} from {rules_file.RULES_PATH}", "import absent on the base branch"],
                    "Remove the import or route it through the boundary the rule names.",
                    f"In {fc.path} remove the import of {resolved}; {ar['statement']}", snippet(fc.patch, line)))
        if not hits:
            conformant.append({"check": f"architect:{ar['id']}", "label": ar["title"], "detail": "no new import matches",
                               "rule": ar["id"]})
    for ar in rules_file.active(ctx.arch, "naming"):
        p = ar["params"]
        hits = 0
        for fc in ctx.files:
            if p.get("new_only", True) and fc.status != "added":
                continue
            if not fnmatch.fnmatch(fc.path, p.get("path_glob", "*")):
                continue
            bad = []
            if p.get("module_suffix") and not fc.path.endswith(p["module_suffix"]):
                bad.append(f"module name does not end with `{p['module_suffix']}`")
            if p.get("class_suffix") and fc.path in ctx.head_modules:
                for cname in ctx.head_modules[fc.path].classes:
                    if not cname.endswith(p["class_suffix"]):
                        bad.append(f"class `{cname}` does not end with `{p['class_suffix']}`")
            if not bad:
                continue
            hits += 1
            findings.append(_finding(
                next_id(), "layer_violation", "architectural", ar.get("severity", "low"), 0.95, "low",
                f"{ar['title']}: `{Path(fc.path).name}`",
                f"`{fc.path}` is new and matches `{p.get('path_glob')}`; " + "; ".join(bad) + f". {ar['statement']}",
                _rule(ctx.arch, ar["id"]), {"path": fc.path, "line": 1, "symbol": Path(fc.path).name}, [],
                [f"rule {ar['id']} from {rules_file.RULES_PATH}", "applies to new files only" if p.get("new_only", True) else "applies to all files"],
                f"Rename to match `{p.get('module_suffix') or p.get('class_suffix')}`.",
                f"Rename {fc.path} so it ends with {p.get('module_suffix') or p.get('class_suffix')} and update imports.",
                snippet(fc.patch, 1)))
        if not hits:
            conformant.append({"check": f"architect:{ar['id']}", "label": ar["title"],
                               "detail": "no new file breaks it", "rule": ar["id"]})
    manual = rules_file.active(ctx.arch, "manual")
    if manual:
        conformant.append({"check": "architect:manual", "label": f"{len(manual)} architect rule(s) not machine-checked",
                           "detail": "; ".join(m["title"] for m in manual) + " — for the reviewer's eyes",
                           "rule": manual[0]["id"]})
    return findings, conformant


# --------------------------------------------------------------------------- integration patterns (java)


def check_integration(ctx: PRContext, next_id: Any) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Wrong integration pattern: orchestration in a controller; a synchronous REST call where the
    architecture reaches that service through the queue."""
    findings: list[Finding] = []
    if ctx.graph.language != "java":
        return findings, []
    mq_consumers = set(ctx.arch.get("mq_consumers", []))
    checked = 0
    for fc in ctx.src_files():
        head = ctx.head_modules[fc.path]
        base = ctx.base_modules.get(fc.path)
        src_ctx, src_layer = _classify_any(ctx, head)
        new_calls = head.service_calls - (base.service_calls if base else set())
        newly_rest = head.uses_rest and not (base.uses_rest if base else False)
        checked += 1
        if src_layer == "controller" and (new_calls or newly_rest or (head.uses_mq and not (base.uses_mq if base else False))):
            line = next((ln for ln, l in enumerate(show(ctx.repo, ctx.head, fc.path).splitlines(), 1)
                         if "restTemplate" in l or "RestTemplate" in l or "rabbit" in l.lower() or any(c in l for c in new_calls)), 1)
            findings.append(_finding(
                next_id(), "integration_pattern", "architectural", "high", 0.9, "high",
                f"`{head.name.rsplit('.', 1)[-1]}` orchestrates {', '.join(sorted(new_calls)) or 'another service'} from the controller",
                (f"`{fc.path}` now calls {', '.join(sorted(new_calls)) or 'another service'} directly from the HTTP layer. "
                 f"In this system a controller maps the request to the service layer and back; orchestration, retries "
                 f"and transactions live in `{src_ctx}`'s service classes, where they are testable."
                 + _baseline_note(ctx, "INTEG-CTRL", "controller(s) already do this on the default branch")),
                _rule(ctx.arch, "INTEG-CTRL"), {"path": fc.path, "line": line, "symbol": head.name.rsplit(".", 1)[-1]},
                [{"hop": 0, "module": head.name, "symbol": "controller", "path": fc.path, "line": line, "note": "new REST/MQ call in the controller"}]
                + [{"hop": 1, "module": c, "symbol": c, "path": c, "line": 0, "note": "called over REST from the controller"} for c in sorted(new_calls)],
                ["layer: controller", f"new service calls: {', '.join(sorted(new_calls)) or 'RestTemplate usage'}", "absent on the base branch"],
                f"Move the call into `{src_ctx}`'s service implementation and have the controller call that method.",
                f"In {fc.path} remove the RestTemplate/Rabbit usage; add a method on the {src_ctx} service interface and implementation that performs the call, and invoke it from the controller.",
                snippet(fc.patch, line)))
        for callee in sorted(new_calls & mq_consumers):
            line = next((ln for ln, l in enumerate(show(ctx.repo, ctx.head, fc.path).splitlines(), 1) if callee in l), 1)
            existing = [b for b in ctx.arch.get("baseline", []) if b["rule"] == "INTEG-ASYNC"]
            findings.append(_finding(
                next_id(), "integration_pattern", "architectural", "high", 0.85, "high",
                f"Synchronous REST call to `{callee}`, which the architecture reaches through RabbitMQ",
                (f"`{fc.path}` calls `{callee}` with RestTemplate. `{callee}` consumes a RabbitMQ queue precisely so "
                 f"that producers do not block on it; a synchronous call couples `{src_ctx}` to its availability and "
                 f"latency. Hand the work to the queue instead."
                 + (f" One call site on main already does this ({existing[0]['examples'][0]['module']}); the rule says it is drift, not precedent." if existing and existing[0]['examples'] else "")),
                _rule(ctx.arch, "INTEG-ASYNC"), {"path": fc.path, "line": line, "symbol": callee},
                [{"hop": 0, "module": head.name, "symbol": head.name.rsplit(".", 1)[-1], "path": fc.path, "line": line, "note": "new synchronous call"},
                 {"hop": 1, "module": callee, "symbol": "RabbitReceive", "path": callee, "line": 0, "note": "consumes RabbitMQ; producers should publish"}],
                [f"{callee} has a @RabbitListener consumer", "call is RestTemplate.exchange (blocking)", "absent on the base branch"],
                f"Publish a message to the queue `{callee}` consumes instead of calling it over HTTP.",
                f"Replace the RestTemplate call to {callee} in {fc.path} with a RabbitMQ publish using a RabbitSend component, matching how ts-preserve-service reaches it.",
                snippet(fc.patch, line)))
    conformant = []
    if checked and not findings:
        conformant.append({"check": "integration", "label": "Integration patterns",
                           "detail": f"{checked} file(s): no orchestration in controllers, no sync calls to queue consumers",
                           "rule": "INTEG-CTRL"})
    return findings, conformant
