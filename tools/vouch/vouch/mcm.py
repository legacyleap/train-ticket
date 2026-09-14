"""Meta-Cognitive Model (MCM) projection of what Vouch knows.

Follows `L2-Meta Cognitive Model - Specification v1.0`: Parts, Assemblies (physical and
virtual), Knowledge nodes, Relationships as independent entities, and Views as derived
filters. Vouch's inferred rules are the Knowledge nodes; PR findings become VIOLATES edges,
passed checks become SATISFIES edges. Written to ``knowledge.json`` for the UI.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from .graph import ImportGraph

CATEGORY = {"layer": "Architecture", "context": "Architecture", "ownership": "Architecture",
            "naming": "Coding", "consistency": "Coding", "process": "Process",
            "functional": "Process", "impact": "Architecture"}


def _source_type(source: str) -> str:
    if source.endswith(".pdf"):
        return "pdf"
    if ".md" in source:
        return "file"
    if source.startswith("Vouch default"):
        return "builtin"
    return "file"


def build_mcm(graph: ImportGraph, arch: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    pkg = graph.package
    now = arch["inferred_at"]
    parts: dict[str, dict[str, Any]] = {}
    assemblies: list[dict[str, Any]] = []
    knowledge: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []
    rel_seen: set[tuple[str, str, str]] = set()

    def rel(rtype: str, frm: str, to: str, **meta: Any) -> None:
        key = (rtype, frm, to)
        if key in rel_seen:
            return
        rel_seen.add(key)
        rels.append({"id": f"rel-{len(rels) + 1}", "type": rtype, "from": frm, "to": to, **meta})

    # --- assemblies: one physical per context, plus virtual ones
    for c in arch["contexts"]:
        assemblies.append({
            "id": f"asm-{c['id']}", "type": "Assembly",
            "kind": {"bounded_context": "BoundedContext", "shared_kernel": "SharedKernel",
                     "driving_adapter": "Interface", "wiring": "Wiring"}.get(c["kind"], "Package"),
            "existence": "Physical",
            "identity": {"name": c["id"], "path": c["path"], "language": "Python"},
            "cognitive": {"intent": c["responsibility"] or "support package", "modules": c["modules"]},
            "boundary": {"architecture_layer": "Mixed", "domain": c["id"]},
            "source": c["source"],
        })
    assemblies.append({
        "id": "asm-ai-surface", "type": "Assembly", "kind": "ChangeSurface", "existence": "Virtual",
        "identity": {"name": "AI-touched surface (open PRs)", "language": "Python"},
        "cognitive": {"intent": "Every module an open, AI-assisted pull request changes or reaches. "
                               "Membership is relationship-based; the assembly owns no code."},
        "boundary": {"architecture_layer": "CrossCutting", "domain": "Review"},
    })
    assemblies.append({
        "id": "asm-untested", "type": "Assembly", "kind": "UntestedPaths", "existence": "Virtual",
        "identity": {"name": "Untested dependency paths", "language": "Python"},
        "cognitive": {"intent": "Modules reached by a change with no test importing them."},
        "boundary": {"architecture_layer": "CrossCutting", "domain": "Quality"},
    })

    # --- knowledge nodes: one per inferred rule, plus the tickets that governed PRs
    for r in arch["rules"]:
        path = r["source"].split("#")[0]
        knowledge.append({
            "id": f"kb-{r['id']}", "type": "Knowledge", "category": CATEGORY.get(r["kind"], "Architecture"),
            "rule_id": r["id"], "title": r["title"], "description": r["statement"],
            "source": {"source_type": _source_type(r["source"]), "path": path,
                       "anchor": r["source"], "extracted_at": now},
            "status": r["status"], "confidence": r["confidence"], "checked_by": r["checked_by"],
        })
    for r in reviews:
        t = r["pr"].get("ticket")
        if t:
            knowledge.append({
                "id": f"kb-{t['key']}", "type": "Knowledge", "category": "Ticket",
                "rule_id": t["key"], "title": t["summary"], "description": f"{t['type']} · governs PR #{r['pr']['number']}",
                "source": {"source_type": "jira", "path": f"https://legacyleap.atlassian.net/browse/{t['key']}",
                           "anchor": t["key"], "extracted_at": r["analysed_at"]},
                "status": "confirmed", "confidence": 1.0, "checked_by": "scope_drift",
            })

    # --- parts: modules the PRs touch or reach (bounded so the graph stays readable)
    def add_part(module: str, path: str, line: int = 1, symbol: str = "", changed_in: str | None = None) -> str:
        if module not in graph.modules and module.rpartition(".")[0] in graph.modules:
            symbol = symbol or module.rpartition(".")[2]
            module = module.rpartition(".")[0]  # `pkg.mod.Name` was an imported symbol, not a module
        pid = f"part-{module}"
        if pid not in parts:
            mi = graph.modules.get(module)
            ctx, layer = graph.classify(module)
            deps = len(graph.dependants(module))
            parts[pid] = {
                "id": pid, "type": "Part", "kind": "Module",
                "identity": {"name": module.rsplit(".", 1)[-1], "qualified_name": module, "language": "Python",
                             "repository": "Cognitive-Core-Asset-Discovery", "file_path": path,
                             "line_start": line, "line_end": None},
                "structural": {"ast_type": "Module", "metrics": {"functions": len(mi.functions) if mi else 0,
                                                                 "classes": len(mi.classes) if mi else 0,
                                                                 "imports": len(mi.imports) if mi else 0,
                                                                 "dependants": deps}},
                "cognitive": {"intent": symbol or "", "behavior": "",
                              "impact": f"{deps} module(s) import this; a change here reaches them first.",
                              "vector_id": None},
                "temporal": {"introduced_in": None, "last_modified": None, "changed_in": []},
                "boundary": {"architecture_layer": layer or "package", "domain": ctx or pkg,
                             "security_zone": "internal"},
            }
            if ctx:
                rel("BELONGS_TO", pid, f"asm-{ctx}")
        if changed_in and changed_in not in parts[pid]["temporal"]["changed_in"]:
            parts[pid]["temporal"]["changed_in"].append(changed_in)
        return pid

    for r in reviews:
        prn = f"#{r['pr']['number']}"
        for f in r["files"]:
            mod = _module_of(graph, f["path"], pkg)
            if mod:
                pid = add_part(mod, f["path"], 1, "", prn)
                rel("BELONGS_TO", pid, "asm-ai-surface", pr=prn)
        for fd in r["findings"]:
            trace = [h for h in fd["trace"] if h["module"] != "ticket" and not h["module"].endswith(".py")]
            prev = None
            for h in trace:
                pid = add_part(h["module"], h["path"], h["line"], h["symbol"], prn if h["hop"] == 0 else None)
                rel("BELONGS_TO", pid, "asm-ai-surface", pr=prn)
                if prev:
                    rel("DEPENDS_ON", pid if h["hop"] else prev, prev if h["hop"] else pid, via="import")
                if "no test" in h["note"]:
                    rel("BELONGS_TO", pid, "asm-untested", pr=prn)
                prev = pid
            if fd["rule"]:
                first = trace[0] if trace else None
                if first:
                    rel("VIOLATES", f"part-{first['module']}", f"kb-{fd['rule']['id']}",
                        pr=prn, finding=fd["id"], severity=fd["severity"], confidence=fd["confidence"])
            if fd["kind"] == "scope_drift" and r["pr"].get("ticket"):
                for f in r["files"]:
                    mod = _module_of(graph, f["path"], pkg)
                    if mod:
                        rel("VIOLATES", f"part-{mod}", f"kb-{r['pr']['ticket']['key']}", pr=prn, finding=fd["id"],
                            severity=fd["severity"], confidence=fd["confidence"])
        for c in r["conformant"]:
            rid = c.get("rule")
            if not rid or rid.endswith("*"):
                continue
            for f in r["files"]:
                mod = _module_of(graph, f["path"], pkg)
                if mod and f"kb-{rid}" in {k["id"] for k in knowledge}:
                    rel("SATISFIES", f"part-{mod}", f"kb-{rid}", pr=prn)
            if r["pr"].get("ticket") and c["check"] == "ticket_scope":
                for f in r["files"]:
                    mod = _module_of(graph, f["path"], pkg)
                    if mod:
                        rel("SATISFIES", f"part-{mod}", f"kb-{r['pr']['ticket']['key']}", pr=prn)

    # knowledge GOVERNS assemblies (what each rule applies to)
    ctx_ids = [c["id"] for c in arch["contexts"] if c["kind"] == "bounded_context"]
    for r in arch["rules"]:
        kid = f"kb-{r['id']}"
        if r["id"].startswith("OWNS-"):
            rel("GOVERNS", kid, f"asm-{r['id'][5:].lower()}")
        elif r["id"] == "SHARED-KERNEL":
            rel("GOVERNS", kid, "asm-shared")
        elif r["kind"] in ("layer", "context", "naming", "consistency", "impact"):
            for c in ctx_ids:
                rel("GOVERNS", kid, f"asm-{c}")
            if r["kind"] in ("layer", "context"):
                rel("GOVERNS", kid, "asm-shared")
        elif r["kind"] in ("process", "functional"):
            rel("GOVERNS", kid, "asm-ai-surface")

    # context-level DEPENDS_ON between physical assemblies
    for d in arch["dependencies"]:
        if d["edges"] >= 3:
            rel("DEPENDS_ON", f"asm-{d['from']}", f"asm-{d['to']}", edges=d["edges"])

    views = [
        {"id": "view-architecture", "type": "Architecture", "name": "Architecture view",
         "description": "Bounded contexts, the shared kernel, and the import dependencies between them.",
         "filter": {"node_types": ["Assembly"], "relationship_types": ["DEPENDS_ON"]},
         "question": "Does the system still have the shape the architect intends?"},
        {"id": "view-domain", "type": "Domain", "name": "Domain view",
         "description": "The modules open PRs touch, grouped by the context that owns them.",
         "filter": {"node_types": ["Part", "Assembly"], "relationship_types": ["BELONGS_TO", "DEPENDS_ON"],
                    "assembly_existence": ["Physical"]},
         "question": "Is each change in the service that owns its vocabulary?"},
        {"id": "view-knowledge", "type": "Security", "name": "Knowledge view",
         "description": "Rules and tickets (Knowledge nodes) and the code that violates or satisfies them.",
         "filter": {"node_types": ["Knowledge", "Part"], "relationship_types": ["GOVERNS", "VIOLATES", "SATISFIES"]},
         "question": "Which written rule does this PR break, and where is that rule written?"},
        {"id": "view-impact", "type": "Impact", "name": "Impact view",
         "description": "Changed modules and everything they reach through imports, with untested paths.",
         "filter": {"node_types": ["Part", "Assembly"], "relationship_types": ["DEPENDS_ON", "BELONGS_TO"],
                    "assembly_existence": ["Virtual"]},
         "question": "If this line changes, what breaks three hops away?"},
    ]

    return {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "spec": "L2-Meta Cognitive Model - Specification v1.0 (Parts, Assemblies, Knowledge, Relationships, Views)",
        "nodes": {"parts": list(parts.values()), "assemblies": assemblies, "knowledge_base": knowledge},
        "relationships": rels,
        "views": views,
        "stats": {"parts": len(parts), "assemblies": len(assemblies), "knowledge": len(knowledge),
                  "relationships": len(rels), "views": len(views),
                  "violates": sum(r["type"] == "VIOLATES" for r in rels),
                  "satisfies": sum(r["type"] == "SATISFIES" for r in rels)},
    }


def _module_of(graph: ImportGraph, path: str, pkg: str) -> str | None:
    m = graph.find_module_for_path(path)
    if m:
        return m.name
    parts = path[:-3].split("/") if path.endswith(".py") else []
    if pkg in parts:
        parts = parts[parts.index(pkg):]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    return None
