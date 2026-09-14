"""The expensive layer: infer the intended architecture of a repository.

Reads what the organisation already wrote down (``CLAUDE.md`` bounded-context list, the
``agent-os/standards`` rule lines) and what the code actually does (the import graph), and
produces a draft the architect corrects rather than authors. Deterministic; no LLM.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from . import rules as rules_file
from .graph import ImportGraph, build_graph, resolve

LAYERS = ("domain", "application", "infrastructure", "interface", "infra_factory")
CORE_LAYERS = ("domain", "application", "infrastructure")

# Which layer may import which (inside a context). Anything not listed is forbidden.
LAYER_ALLOWED: dict[str, set[str]] = {
    "domain": {"domain"},
    "application": {"domain", "application"},
    "infrastructure": {"domain", "application", "infrastructure"},
    "interface": {"domain", "application", "infrastructure", "interface", "infra_factory"},
    "infra_factory": {"domain", "application", "infrastructure", "infra_factory"},
}

# Vocabulary that betrays which context a piece of code belongs to (from CLAUDE.md §Bounded
# contexts). Used by the wrong_context heuristic; every hit is reported as a heuristic.
OWNERSHIP_VOCAB: dict[str, tuple[str, ...]] = {
    "comprehension": ("glossary", "lineage", "complexity", "llm_summary", "summaris", "enrich",
                      "cluster", "embedding"),
    "discovery": ("oracle", "change_set", "changeset", "scan", "discover", "inventory",
                  "missing_asset"),
    "reporting": ("report", "template", "render_report"),
}

SENSITIVE_PATHS: tuple[tuple[str, str], ...] = (
    ("gateway", "API gateway routes (every request passes through)"),
    ("ts-common/src/main/java/edu/fudan/common/security", "JWT / security shared by every service"),
    ("application.yml", "service configuration"),
    ("feature_toggles.py", "feature toggle registry (TBD-021: toggles change runtime behaviour)"),
    ("interface/cli/entrypoint.py", "CLI entrypoint (new user-facing surface)"),
    ("redaction", "secret redaction"),
    ("auth", "authentication"),
    ("secret", "secrets handling"),
    ("infra_factory/", "wiring (selects adapters for every run)"),
)


def classify(module: str, package: str) -> tuple[str | None, str | None]:
    """``pkg.discovery.application.x`` → ("discovery", "application")."""
    parts = module.split(".")
    if not parts or parts[0] != package:
        return None, None
    if len(parts) < 2:
        return None, None
    context = parts[1]
    if context in ("interface", "infra_factory"):
        return context, context
    layer = parts[2] if len(parts) > 2 and parts[2] in CORE_LAYERS else None
    return context, layer


def _find_line(text: str, needle: str) -> int | None:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return None


def _read(repo: Path, rel: str) -> str:
    p = repo / rel
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def _contexts_from_claude_md(text: str) -> dict[str, tuple[str, int]]:
    """Parse ``- **`discovery/`** — description`` bullets; returns name → (description, line)."""
    out: dict[str, tuple[str, int]] = {}
    pat = re.compile(r"^- \*\*`([a-z_]+)/`\*\*\s+[—-]+\s+(.*)$")
    for i, line in enumerate(text.splitlines(), 1):
        m = pat.match(line.strip())
        if m:
            desc = re.sub(r"`", "", m.group(2))
            desc = re.sub(r"\*\*", "", desc)
            out[m.group(1)] = (desc.strip(), i)
    return out


def _rule_text(text: str, rule_id: str) -> tuple[str, int] | None:
    pat = re.compile(rf"^- {re.escape(rule_id)}:\s*(.*)$")
    for i, line in enumerate(text.splitlines(), 1):
        m = pat.match(line.strip())
        if m:
            statement = re.sub(r"\*\*|`", "", m.group(1)).strip()
            return statement, i
    return None


def infer(repo: Path, graph: ImportGraph | None = None) -> dict[str, Any]:
    graph = graph or build_graph(repo)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if graph.language == "java":
        from .infer_java import infer_java

        arch = infer_java(repo, graph, now)
        return rules_file.merge_into(arch, rules_file.load(repo))
    pkg = graph.package
    claude_md = _read(repo, "CLAUDE.md")
    ddd_path = "agent-os/standards/ddd-hexagonal-standards.md"
    ads_path = "agent-os/standards/asset-discovery-standards.md"
    tbd_path = "agent-os/standards/trunk-based-development-standards.md"
    ddd = _read(repo, ddd_path)
    ads = _read(repo, ads_path)
    tbd = _read(repo, tbd_path)
    described = _contexts_from_claude_md(claude_md)

    # --- contexts and layer counts from the code itself
    counts: dict[str, dict[str, int]] = {}
    for m in graph.modules.values():
        ctx, layer = graph.classify(m.name)
        if not ctx:
            continue
        c = counts.setdefault(ctx, {"_total": 0})
        c["_total"] += 1
        if layer:
            c[layer] = c.get(layer, 0) + 1
    # bounded contexts = what the docs describe, minus the kernel and the driving/wiring dirs
    bounded = {c for c in described if c not in ("shared", "interface", "infra_factory")} or {
        c for c in counts if c not in ("shared", "interface", "infra_factory", "utils", "neo4j_core", "feature_toggles")
    }
    contexts = []
    for ctx, c in sorted(counts.items(), key=lambda kv: -kv[1]["_total"]):
        desc, line = described.get(ctx, ("", 0))
        kind = ("bounded_context" if ctx in bounded else
                "shared_kernel" if ctx == "shared" else
                "driving_adapter" if ctx == "interface" else
                "wiring" if ctx == "infra_factory" else "support")
        contexts.append({
            "id": ctx,
            "path": f"src/{pkg}/{ctx}",
            "kind": kind,
            "responsibility": desc or _default_responsibility(ctx),
            "modules": c["_total"],
            "layers": {k: v for k, v in c.items() if k != "_total"},
            "source": f"CLAUDE.md#L{line}" if line else "src/ (directory layout)",
            "described_in_docs": bool(desc),
        })

    # --- context-level dependency edges observed in code
    edge_counts: dict[tuple[str, str], int] = {}
    for m in graph.modules.values():
        src_ctx, _ = graph.classify(m.name)
        for target in m.imports:
            r = resolve(graph.modules, target)
            if not r:
                continue
            dst_ctx, _ = graph.classify(r)
            if src_ctx and dst_ctx and src_ctx != dst_ctx:
                edge_counts[(src_ctx, dst_ctx)] = edge_counts.get((src_ctx, dst_ctx), 0) + 1
    dependencies = [
        {"from": a, "to": b, "edges": n}
        for (a, b), n in sorted(edge_counts.items(), key=lambda kv: -kv[1])
    ]

    # --- rules, each citing the line it was read from
    def cite(path: str, text: str, rule_id: str, fallback: str) -> tuple[str, str]:
        found = _rule_text(text, rule_id)
        if found:
            return found[0], f"{path}#L{found[1]}"
        return fallback, path

    test_evidence = "enforced by tests/test_architecture_ddd_compliance.py" if (
        repo / "tests/test_architecture_ddd_compliance.py"
    ).exists() else "not enforced by tests"

    rules: list[dict[str, Any]] = []

    s, src = cite(ddd_path, ddd, "DDD-012", "Dependency direction MUST be domain ← application ← infrastructure.")
    rules.append(_rule("DDD-012", "layer", "Domain must not import application or infrastructure", s, src,
                       0.98, "layer_violation", test_evidence))
    s, src = cite(ddd_path, ddd, "DDD-013", "Application MUST NOT import from infrastructure.")
    rules.append(_rule("DDD-013", "layer", "Application must not import infrastructure or interface", s, src,
                       0.98, "layer_violation", test_evidence))
    s, src = cite(ddd_path, ddd, "DDD-010", "One directory per bounded context; shared kernel depends on no context.")
    rules.append(_rule("DDD-010", "context", "Bounded contexts do not import each other", s, src,
                       0.9, "context_isolation",
                       "observed: no discovery↔comprehension↔reporting import edges on main"
                       if not any(d["from"] in bounded and d["to"] in bounded for d in dependencies)
                       else "observed: cross-context edges already exist on main"))
    line = _find_line(claude_md, "Must not depend on any bounded context")
    rules.append(_rule("SHARED-KERNEL", "context", "shared/ must not depend on any bounded context",
                       "The shared kernel MUST NOT depend on discovery, comprehension or reporting.",
                       f"CLAUDE.md#L{line}" if line else "CLAUDE.md", 0.95, "context_isolation", test_evidence))
    for ctx in ("discovery", "comprehension", "reporting"):
        desc, ln = described.get(ctx, ("", 0))
        if desc:
            rules.append(_rule(f"OWNS-{ctx.upper()}", "ownership",
                               f"{ctx} owns: {_short_owner(desc)}", desc, f"CLAUDE.md#L{ln}",
                               0.7, "wrong_context", "heuristic: vocabulary and imports of new code"))
    s, src = cite(ddd_path, ddd, "DDD-033", "Use case class names MUST follow <Subject><Action>UseCase.")
    rules.append(_rule("DDD-033", "naming", "Use-case classes are named <Subject><Action>UseCase", s, src,
                       0.95, "naming", "checked on every new class under use_cases/"))
    s, src = cite(ddd_path, ddd, "DDD-034", "Use case modules MUST end with _use_case.py under application/use_cases/.")
    rules.append(_rule("DDD-034", "naming", "Use-case modules end with _use_case.py", s, src,
                       0.95, "naming", "checked on every new module under use_cases/"))
    s, src = cite(ads_path, ads, "ADS-010", "FQNs MUST be represented by the FQN type.")
    rules.append(_rule("ADS-010", "consistency", "FQNs are the FQN value object, never raw strings", s, src,
                       0.85, "duplicate_logic", "heuristic: new helpers that re-implement FQN handling"))
    s, src = cite(ads_path, ads, "ADS-015", "FQNs are opaque identifiers; do not parse them.")
    rules.append(_rule("ADS-015", "consistency", "Do not parse or split FQNs outside the value object", s, src,
                       0.85, "duplicate_logic", "heuristic"))
    s, src = cite(tbd_path, tbd, "TBD-012", "All new changes MUST use feature toggles.")
    rules.append(_rule("TBD-012", "process", "Behaviour-changing work sits behind a feature toggle", s, src,
                       0.8, "scope_drift", "toggle registry edits are a sensitive area for ticket scope"))
    s, src = cite(tbd_path, tbd, "TBD-022", "Application code MUST consume toggles only via feature_toggles.")
    rules.append(_rule("TBD-022", "process", "Toggles are read only in feature_toggles.py", s, src,
                       0.9, "scope_drift", "sensitive path"))
    rules.append(_rule("TICKET-SCOPE", "functional", "A change does what its ticket asks, no more and no less",
                       "Files touched must fall inside the linked ticket's declared scope; sensitive "
                       "areas (toggles, entrypoints, secrets) need an explicit mention in the ticket.",
                       "Vouch default (functional conformance)", 0.85, "scope_drift",
                       "Jira ticket linked on the PR"))
    rules.append(_rule("IMPACT", "impact", "Changes to shared utilities are traced to every dependant",
                       "A change to a function is followed through the reverse import graph up to three "
                       "hops; untested paths that cross a context boundary are surfaced.",
                       "Vouch default (transitive impact)", 0.9, "transitive_impact",
                       f"{len(graph.reverse)} modules have dependants in the graph"))

    diagram = _context_diagram(contexts, dependencies)
    arch = {
        "inferred_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "package": pkg,
        "sources": ["CLAUDE.md", ddd_path, ads_path, tbd_path, f"src/{pkg}/ (AST import graph)",
                    "tests/ (which modules each test imports)"],
        "stats": {
            "modules": len(graph.modules),
            "import_edges": sum(len(m.imports) for m in graph.modules.values()),
            "contexts": len([c for c in contexts if c["kind"] == "bounded_context"]),
            "rules": len(rules),
            "tests": len(graph.test_modules),
        },
        "contexts": contexts,
        "layers": list(LAYERS),
        "layer_allowed": {k: sorted(v) for k, v in LAYER_ALLOWED.items()},
        "rule_ids": {"layer_domain": "DDD-012", "layer_app": "DDD-013", "layer": "DDD-013", "context": "DDD-010",
                     "kernel": "SHARED-KERNEL"},
        "language": "python",
        "rules": rules,
        "dependencies": dependencies,
        "diagram": diagram,
    }
    return rules_file.merge_into(arch, rules_file.load(repo))


def _rule(rid: str, kind: str, title: str, statement: str, source: str, confidence: float,
          checked_by: str, evidence: str) -> dict[str, Any]:
    return {"id": rid, "kind": kind, "title": title, "statement": statement, "source": source,
            "status": "inferred", "confidence": confidence, "checked_by": checked_by,
            "evidence": evidence}


def _short_owner(desc: str) -> str:
    d = desc.split(". ")[0]
    return d[:110]


def _default_responsibility(ctx: str) -> str:
    return {
        "utils": "small cross-cutting helpers (strings, datetime, redaction)",
        "neo4j_core": "generated registry, constants and saved Cypher for the graph",
        "feature_toggles": "feature toggle registry",
    }.get(ctx, "")


def _context_diagram(contexts: list[dict[str, Any]], deps: list[dict[str, Any]]) -> str:
    ids = {c["id"] for c in contexts}
    lines = ["flowchart LR"]
    for c in contexts:
        label = c["id"]
        if c["kind"] == "bounded_context":
            lines.append(f'  {label}["{label}<br/><small>{c["modules"]} modules</small>"]:::bc')
        elif c["kind"] == "shared_kernel":
            lines.append(f'  {label}(["{label} kernel"]):::kernel')
        else:
            lines.append(f'  {label}[/"{label}"/]:::support')
    for d in deps:
        if d["from"] in ids and d["to"] in ids and d["edges"] >= 3:
            lines.append(f'  {d["from"]} -->|{d["edges"]}| {d["to"]}')
    lines.append("  classDef bc fill:#12331f,stroke:#22c55e,color:#e5f9ec;")
    lines.append("  classDef kernel fill:#1a1f2e,stroke:#60a5fa,color:#e0ecff;")
    lines.append("  classDef support fill:#1f1f1f,stroke:#525252,color:#d4d4d4;")
    return "\n".join(lines)
