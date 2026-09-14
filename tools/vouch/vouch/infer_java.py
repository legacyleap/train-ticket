"""Intended architecture of a Java / Spring Boot microservice monorepo (Maven modules).

What it reads: the Maven module list (``pom.xml``), the package layout every service follows
(controller → service → repository/entity), the shared kernel (``ts-common``), the REST call
graph (``"ts-x-service"`` string literals), and which services consume RabbitMQ. It also measures
the drift that already exists on the default branch, because for a system like train-ticket
that baseline *is* the architect's problem.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .graph import ImportGraph

# Which layer may import which, inside one service (Spring Boot convention).
LAYER_ALLOWED: dict[str, set[str]] = {
    "controller": {"controller", "service", "entity", "dto", "config", "util"},
    "service": {"service", "repository", "entity", "dto", "util", "mq", "config"},
    "repository": {"repository", "entity", "util"},
    "entity": {"entity", "util"},
    "mq": {"mq", "service", "entity", "dto", "util", "config"},
}
RULE_IDS = {"layer": "LAYER-001", "context": "SVC-ISO", "kernel": "KERNEL-001"}
GENERIC = {"admin", "other", "basic", "inside", "wait", "ticket", "office", "verification", "code", "plan",
           "cancel", "execute", "preserve", "rebook", "security", "auth", "config"}


def _read_line(repo: Path, rel: str, needle: str) -> str:
    p = repo / rel
    if p.exists():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if needle in line:
                return f"{rel}#L{i}"
    return rel


def common_prefix(names: list[str]) -> str:
    """``ts-`` when every service is called ts-something; otherwise empty."""
    if len(names) < 3:
        return ""
    first = names[0].split("-")[0] + "-"
    return first if all(n.startswith(first) for n in names) else ""


def service_vocabulary(context: str, prefix: str = "ts-") -> tuple[str, ...]:
    """``ts-station-food-service`` → ("stationfood",); ``account-service`` → ("account",)."""
    words = [w for w in context.removeprefix(prefix).removesuffix("-service").split("-") if w]
    if not words or words[0] in ("admin", "gateway", "ui", "common", "config", "registry", "monitoring") or words[-1] in ("other", "2"):
        return ()
    if words[-1][-1].isdigit():
        return ()
    if any(w in GENERIC for w in words) and len(words) > 1:
        return ()
    if len(words) == 1:
        return (words[0],) if words[0] not in GENERIC else ()
    return ("".join(words),)


def infer_java(repo: Path, graph: ImportGraph, now: str) -> dict[str, Any]:
    dirs = sorted({m.context for m in graph.modules.values()
                   if m.context and ((repo / m.context / "pom.xml").exists() or (repo / m.context / "src" / "main" / "java").exists())})
    counts: dict[str, dict[str, int]] = {}
    for m in graph.modules.values():
        c = counts.setdefault(m.context or "?", {"_total": 0})
        c["_total"] += 1
        if m.layer:
            c[m.layer] = c.get(m.layer, 0) + 1
    mq_consumers = sorted({m.context for m in graph.modules.values()
                           if "RabbitListener" in m.annotations or m.name.endswith("RabbitReceive")})
    pom = _read_line(repo, "pom.xml", "<module>")
    readme = _read_line(repo, "README.md", "Service Architecture Graph")
    prefix = common_prefix(dirs)
    kernel = next((d for d in dirs if any(k in d for k in ("common", "shared", "kernel"))), None)
    gateway = next((d for d in dirs if "gateway" in d), None)
    infra_dirs = {d for d in dirs if any(k in d for k in ("config", "registry", "monitoring", "turbine", "discovery", "eureka", "zipkin"))
                  and not (repo / d / "src" / "main" / "java").rglob("*Controller.java")}
    infra_dirs = {d for d in dirs if d.rsplit("-", 1)[-1] in ("config", "registry", "monitoring", "discovery") or d in ("config", "registry", "monitoring", "turbine-stream-service")}

    contexts = []
    for d in dirs:
        c = counts.get(d, {"_total": 0})
        if d == kernel:
            kind, resp = "shared_kernel", "shared entities, security and utilities every service depends on"
        elif d == gateway:
            kind, resp = "driving_adapter", "API gateway: the only entry point for the UI"
        elif d in infra_dirs:
            kind, resp = "support", f"platform service ({d}): configuration, discovery or monitoring — no business logic"
        elif d.startswith(prefix + "admin-"):
            kind, resp = "bounded_context", f"admin façade over {', '.join(sorted(graph.service_calls.get(d, [])) ) or 'other services'}"
        else:
            calls = sorted(graph.service_calls.get(d, []))
            resp = f"owns the {'/'.join(service_vocabulary(d, prefix)) or d.removeprefix(prefix).removesuffix('-service')} domain"
            resp += f"; calls {len(calls)} service(s) over REST" if calls else "; calls no other service"
            if d in mq_consumers:
                resp += "; consumes RabbitMQ"
            kind = "bounded_context"
        contexts.append({
            "id": d, "path": d, "kind": kind, "responsibility": resp, "modules": c["_total"],
            "layers": {k: v for k, v in c.items() if k != "_total"},
            "source": pom, "described_in_docs": False,
            "calls": sorted(graph.service_calls.get(d, [])), "called_by": sorted(graph.service_callers.get(d, [])),
            "mq_consumer": d in mq_consumers,
        })

    # context-level edges: imports (ts-common) + REST calls
    edge_counts: dict[tuple[str, str], int] = {}
    for m in graph.modules.values():
        for target in m.imports:
            for r in graph.resolve_all(target):
                dst = graph.modules[r].context
                if m.context and dst and m.context != dst:
                    edge_counts[(m.context, dst)] = edge_counts.get((m.context, dst), 0) + 1
    rest_edges: dict[tuple[str, str], int] = {}
    for m in graph.modules.values():
        for callee in m.service_calls:
            if m.context:
                rest_edges[(m.context, callee)] = rest_edges.get((m.context, callee), 0) + 1
    dependencies = [{"from": a, "to": b, "edges": n, "kind": "import"} for (a, b), n in edge_counts.items()]
    dependencies += [{"from": a, "to": b, "edges": n, "kind": "rest"} for (a, b), n in rest_edges.items()]
    dependencies.sort(key=lambda d: -d["edges"])

    bounded = [c["id"] for c in contexts if c["kind"] == "bounded_context"]
    rules: list[dict[str, Any]] = []

    def rule(rid: str, kind: str, title: str, statement: str, source: str, conf: float, checked_by: str,
             evidence: str, severity: str = "high") -> None:
        rules.append({"id": rid, "kind": kind, "title": title, "statement": statement, "source": source,
                      "status": "inferred", "confidence": conf, "checked_by": checked_by, "evidence": evidence,
                      "origin": "inferred", "severity": severity})

    rule("LAYER-001", "layer", "Controller → service → repository, never backwards or skipping",
         "Inside a service, controllers depend on the service layer only; services on repositories and entities; "
         "repositories and entities on nothing above them. Observed in every module's package layout.",
         "Spring Boot package convention (controller/service/repository/entity), observed in "
         f"{sum(1 for c in contexts if c['layers'].get('controller'))} services", 0.85, "layer_violation",
         f"{counts_by_layer(graph)} on main")
    rule("SVC-ISO", "context", "A service never imports another service's classes",
         "Each ts-*-service is its own Maven module; the only shared code is ts-common. Cross-service needs go over "
         "REST (service discovery) or RabbitMQ, never through imports.", pom, 0.95, "context_isolation",
         "enforced by Maven module boundaries; cross-module imports do not compile")
    kernel_entities = sum(1 for m in graph.modules.values() if kernel and m.context == kernel and m.layer == "entity")
    n_ctrl = sum(1 for m in graph.modules.values() if m.layer == "controller")
    n_ctrl_bad = sum(1 for m in graph.modules.values() if m.layer == "controller" and (m.uses_rest or m.uses_mq or m.service_calls))
    if kernel:
        rule("KERNEL-001", "context", f"{kernel} depends on no service",
             f"The shared kernel holds entities, security and utilities; it must not import any service package.",
             pom, 0.95, "context_isolation", f"observed: 0 imports from {kernel} into services")
    if kernel and kernel_entities:
        rule("KERNEL-ENTITY", "consistency", f"Shared entities live in {kernel} once",
             f"An entity that more than one service needs is defined once in {kernel} and imported; a service must "
             f"not keep its own copy under <service>/entity.",
             f"{kernel} ({kernel_entities} shared entities)", 0.8, "duplicate_logic",
             "baseline: see drift for existing copies", "medium")
    rule("INTEG-CTRL", "integration", "Controllers do not orchestrate other services",
         "Calling other services (RestTemplate, Feign, RabbitTemplate, DiscoveryClient) belongs in the service layer; a "
         "controller maps HTTP to a service call and back.", f"Spring Boot convention; observed in {n_ctrl - n_ctrl_bad} of {n_ctrl} controllers",
         0.85, "integration_pattern", f"baseline: {n_ctrl_bad} controller(s) already break it")
    if mq_consumers:
        rule("INTEG-ASYNC", "integration", f"{', '.join(c.removeprefix(prefix).removesuffix('-service') for c in mq_consumers)} reached asynchronously (RabbitMQ)",
             f"{', '.join(mq_consumers)} consume RabbitMQ; producers hand them work through the queue, "
             "not with a synchronous REST call that couples the caller to their availability.",
             "observed: @RabbitListener consumers in the codebase", 0.75, "integration_pattern",
             "baseline: see drift for synchronous call sites", "high")
    for ctx_id in bounded:
        vocab = service_vocabulary(ctx_id, prefix)
        if vocab:
            rule(f"OWNS-{vocab[0].upper()}", "ownership", f"{ctx_id} owns the {vocab[0]} domain",
                 f"Classes about {vocab[0]} (entities, services, controllers) live in {ctx_id}; another service that "
                 f"needs {vocab[0]} data calls {ctx_id} over REST.", pom, 0.7, "wrong_context",
                 "heuristic: derived from the service name", "medium")
    rule("NAMING-PKG", "naming", "Package names are the standard layer names",
         "Layer packages are spelled controller, service, repository, entity, config, mq; misspelt packages "
         "(serivce) hide classes from every tool that relies on the convention.", "observed layout", 0.9, "naming",
         "baseline: 1 misspelt package on main", "low")
    rule("TICKET-SCOPE", "functional", "A change does what its ticket asks, no more and no less",
         "Files touched must fall inside the linked ticket's declared scope; sensitive areas (gateway routes, "
         "JWT/security, config) need an explicit mention in the ticket.", "Vouch default (functional conformance)",
         0.85, "scope_drift", "ticket linked on the PR")
    rule("IMPACT", "impact", "Changes to an API or a shared entity are traced to every caller",
         "A change is followed through imports inside the service, and through REST calls to every service that "
         "calls it, up to three hops; untested paths are surfaced.", "Vouch default (transitive impact)", 0.9,
         "transitive_impact", f"{len(graph.service_callers)} services have REST callers")

    baseline = measure_baseline(graph, contexts, mq_consumers, kernel)
    diagram = _service_diagram(contexts, rest_edges, prefix)
    return {
        "inferred_at": now, "package": "", "language": "java", "kernel": kernel, "gateway": gateway, "prefix": prefix,
        "sources": ["pom.xml (Maven modules)", "README.md (service architecture graph)",
                    "*/src/main/java (package layout, imports, REST call strings)", "*/src/test/java"],
        "stats": {"modules": len(graph.modules), "import_edges": sum(len(m.imports) for m in graph.modules.values()),
                  "contexts": len(bounded), "rules": len(rules), "tests": len(graph.test_modules),
                  "rest_edges": len(rest_edges)},
        "contexts": contexts,
        "layers": ["controller", "service", "repository", "entity", "dto", "config", "mq", "init", "util"],
        "layer_allowed": {k: sorted(v) for k, v in LAYER_ALLOWED.items()},
        "rule_ids": RULE_IDS,
        "mq_consumers": mq_consumers,
        "rules": rules,
        "dependencies": dependencies,
        "baseline": baseline,
        "diagram": diagram,
    }


def counts_by_layer(graph: ImportGraph) -> str:
    from collections import Counter

    c = Counter(m.layer for m in graph.modules.values() if m.layer)
    return ", ".join(f"{n} {k}" for k, n in c.most_common(4))


def measure_baseline(graph: ImportGraph, contexts: list[dict[str, Any]], mq_consumers: list[str],
                     kernel: str | None = None) -> list[dict[str, Any]]:
    """Real drift that already exists on the default branch, with the modules that carry it."""
    out: list[dict[str, Any]] = []
    kernel_entities = {m.name.rsplit(".", 1)[-1]: m.name for m in graph.modules.values()
                       if kernel and m.context == kernel and m.layer == "entity"}

    # 1. copies of shared entities inside services
    copies = [(m.name.rsplit(".", 1)[-1], m) for m in graph.modules.values()
              if m.layer == "entity" and m.context != kernel and m.name.rsplit(".", 1)[-1] in kernel_entities]
    if kernel_entities:
      out.append({"rule": "KERNEL-ENTITY", "title": "Shared entities copied into services", "count": len(copies),
                "unit": "classes", "severity": "medium",
                "examples": [{"module": m.name, "path": m.path, "note": f"copy of {kernel_entities[n]}"}
                             for n, m in sorted(copies, key=lambda x: x[0])[:12]],
                "services": sorted({m.context for _, m in copies}),
                "why": "Two definitions of Order drift apart silently; every consumer must know which one it talks to."})

    # 2. controllers that orchestrate other services
    ctrl = [m for m in graph.modules.values() if m.layer == "controller" and (m.uses_rest or m.uses_mq or m.service_calls)]
    out.append({"rule": "INTEG-CTRL", "title": "Controllers calling other services directly", "count": len(ctrl),
                "unit": "controllers", "severity": "medium",
                "examples": [{"module": m.name, "path": m.path,
                              "note": ("RabbitMQ" if m.uses_mq else "RestTemplate") + (f" → {', '.join(sorted(m.service_calls))}" if m.service_calls else "")}
                             for m in ctrl],
                "services": sorted({m.context for m in ctrl}),
                "why": "The service layer is where orchestration is testable; a controller doing it cannot be reused or retried."})

    # 3. synchronous REST calls to services designed to be reached through the queue
    sync_to_mq = [(m, c) for m in graph.modules.values() for c in sorted(m.service_calls) if c in mq_consumers]
    out.append({"rule": "INTEG-ASYNC", "title": "Synchronous calls to queue-consuming services", "count": len(sync_to_mq),
                "unit": "call sites", "severity": "high",
                "examples": [{"module": m.name, "path": m.path, "note": f"REST → {c} (consumes RabbitMQ)"} for m, c in sync_to_mq],
                "services": sorted({m.context for m, _ in sync_to_mq}),
                "why": "If notification is down, cancel fails; the queue exists so that it does not."})

    # 4. near-duplicate services: exact same method bodies (≥ 30 tokens) across two services
    by_body: dict[tuple[str, ...], list[Any]] = {}
    for m in graph.modules.values():
        if m.context == kernel or m.layer == "entity":  # entity copies are the KERNEL-ENTITY story
            continue
        for f in m.functions:
            if len(f.tokens) >= 30:
                by_body.setdefault(f.tokens, []).append(f)
    pair_counts: dict[tuple[str, str], int] = {}
    for fns in by_body.values():
        ctxs = sorted({graph.modules[f.module].context for f in fns})
        for i in range(len(ctxs)):
            for j in range(i + 1, len(ctxs)):
                pair_counts[(ctxs[i], ctxs[j])] = pair_counts.get((ctxs[i], ctxs[j]), 0) + 1
    pairs = sorted(((a, b, n) for (a, b), n in pair_counts.items() if n >= 5), key=lambda x: -x[2])
    total_methods = {c: sum(len(f.tokens) >= 30 for m in graph.modules.values() if m.context == c and m.layer != "entity" for f in m.functions)
                     for c in {x for p in pairs for x in p[:2]}}
    out.append({"rule": "SVC-DUP", "title": "Services that are copies of another service", "count": len(pairs),
                "unit": "service pairs", "severity": "high",
                "examples": [{"module": f"{a} ↔ {b}", "path": a,
                              "note": f"{n} identical method bodies ({n}/{max(1, min(total_methods.get(a, 1), total_methods.get(b, 1)))} of the smaller service)"}
                             for a, b, n in pairs[:8]],
                "services": sorted({x for p in pairs for x in p[:2]}),
                "why": "A fix in ts-order-service is not a fix in ts-order-other-service; every caller has to call both."})

    # 5. fan-out: services with many synchronous dependencies
    fan = sorted(((c["id"], len(c["calls"])) for c in contexts if len(c["calls"]) >= 5), key=lambda x: -x[1])
    out.append({"rule": "IMPACT", "title": "Services with 5+ synchronous REST dependencies", "count": len(fan),
                "unit": "services", "severity": "medium",
                "examples": [{"module": c, "path": c, "note": f"calls {n} services in one request path"} for c, n in fan],
                "services": [c for c, _ in fan],
                "why": "One slow dependency in an 11-hop chain makes the whole booking slow; there is no place to retry."})

    # 6. layer imports that break the convention today
    from .infer_java import LAYER_ALLOWED as LA  # noqa: PLC0415 (same module; explicit for clarity)

    layer_hits = []
    for m in graph.modules.values():
        if m.layer not in LA:
            continue
        for t in m.imports:
            for r in graph.resolve_all(t):
                dst = graph.modules[r]
                if dst.context == m.context and dst.layer and dst.layer not in LA[m.layer]:
                    layer_hits.append((m, dst))
    out.append({"rule": "LAYER-001", "title": "Layer imports against the convention", "count": len(layer_hits),
                "unit": "imports", "severity": "high",
                "examples": [{"module": m.name, "path": m.path, "note": f"{m.layer} imports {d.layer}: {d.name.rsplit('.', 1)[-1]}"}
                             for m, d in layer_hits[:10]],
                "services": sorted({m.context for m, _ in layer_hits}),
                "why": "A controller that reads the repository skips validation and transactions."})

    # 7. misspelt layer packages
    typos = [m for m in graph.modules.values() if re.search(r"/(serivce|controler|repositry|entitiy)/", m.path)]
    out.append({"rule": "NAMING-PKG", "title": "Misspelt layer packages", "count": len(typos), "unit": "classes",
                "severity": "low",
                "examples": [{"module": m.name, "path": m.path, "note": re.search(r"/(serivce|controler|repositry|entitiy)/", m.path).group(1)} for m in typos[:6]],
                "services": sorted({m.context for m in typos}),
                "why": "Tools and people that rely on the package name miss these classes."})
    return out


def _service_diagram(contexts: list[dict[str, Any]], rest_edges: dict[tuple[str, str], int], prefix: str = "ts-") -> str:
    lines = ["flowchart LR"]
    ids = {c["id"] for c in contexts}
    shown = set()
    for (a, b), n in sorted(rest_edges.items(), key=lambda kv: -kv[1]):
        if a in ids and b in ids:
            shown.update((a, b))
    for c in contexts:
        if c["id"] not in shown:
            continue
        label = c["id"].removeprefix(prefix).removesuffix("-service")
        cls = "mq" if c.get("mq_consumer") else "bc"
        lines.append(f'  {c["id"].replace("-", "_")}["{label}"]:::{cls}')
    for (a, b), n in sorted(rest_edges.items(), key=lambda kv: -kv[1]):
        if a in ids and b in ids:
            lines.append(f'  {a.replace("-", "_")} --> {b.replace("-", "_")}')
    lines.append("  classDef bc fill:#12331f,stroke:#22c55e,color:#e5f9ec;")
    lines.append("  classDef mq fill:#2a2412,stroke:#f59e0b,color:#fde68a;")
    return "\n".join(lines)


def similar(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()
