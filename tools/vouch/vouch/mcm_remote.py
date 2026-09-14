"""Pull the *actual* Meta-Cognitive Model of a project from the LegacyLeap MCP server and project
it into the same shapes the UI already understands (knowledge.json, plus an ``mcm`` bundle).

What comes from the server (real): statistics, health grade, architecture drift by layer pair,
layer distribution, microservice boundaries and cross-service edges, assemblies with their
LLM-written intent (the cognitive layer), and per-PR blast radius from ``mcm_analyze_git_change_impact``.
What stays Vouch's: the rules (Knowledge nodes) and the VIOLATES/SATISFIES edges from the reviews.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .mcm_client import MCMClient


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_bundle(project_id: str, data_dir: Path, max_assemblies: int = 60) -> dict[str, Any]:
    c = MCMClient()
    server = c.connect()
    reviews = [json.loads(p.read_text()) for p in sorted((data_dir / "reviews").glob("*.json"))] if (data_dir / "reviews").exists() else []
    arch = json.loads((data_dir / "architecture.json").read_text()) if (data_dir / "architecture.json").exists() else {"rules": [], "contexts": []}

    def safe(tool: str, **args: Any) -> Any:
        try:
            return c.call(tool, project_id=project_id, **args)
        except Exception as e:  # noqa: BLE001 — a missing tool must not sink the bundle
            return {"_error": str(e)[:300]}

    stats = safe("mcm_get_project_statistics")
    health = safe("mcm_get_health_dashboard")
    drift = safe("mcm_measure_architecture_drift")
    layers = safe("mcm_explain_architecture", fields="essential")
    micro = safe("mcm_list_microservices")
    assemblies_raw = safe("mcm_list_assemblies", limit=0)
    dup = safe("mcm_find_duplicate_code", limit=15)
    debt = safe("mcm_score_technical_debt", top_n=15)

    asm_list: list[dict[str, Any]] = []
    for view, items in (assemblies_raw.get("assemblies_by_view", {}) if isinstance(assemblies_raw, dict) else {}).items():
        for a in items:
            asm_list.append({"id": a.get("id"), "name": a.get("name"), "view": view, "intent": (a.get("intent") or "")[:600],
                             "layer": a.get("architecture_layer"), "domain": a.get("domain"), "parts": a.get("part_count", 0)})
    asm_list.sort(key=lambda a: -(a["parts"] or 0))
    asm_list = asm_list[:max_assemblies]

    # per-PR blast radius from the real MCM
    impacts: dict[str, Any] = {}
    for r in reviews:
        files = [f["path"] for f in r.get("files", [])]
        if not files:
            continue
        # the server indexed each service as its own root: strip the module directory
        stripped = [p.split("/", 1)[1] if "/src/" in p and not p.startswith("src/") else p for p in files]
        res = safe("mcm_analyze_git_change_impact", changed_files="\n".join(stripped))
        imp = _compact_impact(res)
        imp["paths_sent"] = stripped
        impacts[r["id"]] = imp
    # cognitive layer for the component each finding starts from
    components: dict[str, Any] = {}
    for r in reviews:
        for f in r.get("findings", []):
            for h in f.get("trace", [])[:1]:
                if h.get("module") in ("ticket",) or h.get("symbol") in ("import", "controller"):
                    continue
                cls = h.get("module", "").rsplit(".", 1)[-1]
                if not cls or cls in components:
                    continue
                res = safe("mcm_get_component_context", name=cls, fields="standard")
                comp = _compact_component(res)
                # accept only when the server's file matches the PR's file (new files are unknown to the MCM)
                tail = (h.get("path") or "").split("/src/", 1)[-1]
                if comp.get("file_path") and tail and not comp["file_path"].endswith(tail):
                    comp = {"unknown_to_mcm": True, "note": f"'{cls}' resolved to {comp['file_path']} — not this file (new in the PR?)"}
                components[cls] = comp

    bundle = {
        "fetched_at": _now(), "server": server, "project_id": project_id,
        "url": c.url,
        "statistics": stats.get("overview", stats) if isinstance(stats, dict) else stats,
        "relationship_types": stats.get("relationship_types", {}) if isinstance(stats, dict) else {},
        "health": {k: health.get(k) for k in ("health_grade", "health_score", "signals", "recommendations")} if isinstance(health, dict) else health,
        "drift": {"score": drift.get("drift_score"), "compliance": drift.get("compliance"), "summary": drift.get("summary"),
                  "violations": [{"from": v.get("from_layer"), "to": v.get("to_layer"), "count": v.get("count"),
                                  "examples": (v.get("from_examples") or [])[:3]} for v in drift.get("architecture_violations", [])[:12]],
                  "hotspots": drift.get("hotspot_components", [])[:10]} if isinstance(drift, dict) else drift,
        "layers": [{"layer": l.get("layer"), "count": l.get("count")} for l in layers.get("architecture_layers", [])] if isinstance(layers, dict) else layers,
        "top_domains": layers.get("top_domains", [])[:10] if isinstance(layers, dict) else [],
        "microservices": {"total": micro.get("total_services"),
                          "services": [{"name": s.get("name"), "parts": s.get("part_count"), "layers": s.get("layers"),
                                        "calls": s.get("makes_cross_service_calls"), "callers": s.get("cross_service_callers")}
                                       for s in micro.get("services", [])[:60]],
                          "edges": micro.get("cross_service_edges", [])[:80],
                          "function_calls": micro.get("function_level_cross_service_calls", [])[:60]} if isinstance(micro, dict) else micro,
        "assemblies": asm_list,
        "duplicates": dup.get("duplicates", dup.get("candidates", dup)) if isinstance(dup, dict) else dup,
        "tech_debt": debt.get("components", debt.get("top_components", debt)) if isinstance(debt, dict) else debt,
        "pr_impact": impacts,
        "components": components,
    }
    (data_dir / "mcm-remote.json").write_text(json.dumps(bundle, indent=1))
    knowledge = project_knowledge(bundle, arch, reviews)
    (data_dir / "knowledge-remote.json").write_text(json.dumps(knowledge, indent=1))
    return bundle


def _compact_impact(res: Any) -> dict[str, Any]:
    if not isinstance(res, dict):
        return {"raw": str(res)[:500]}
    keep = {}
    for k in ("matched_components", "impacted_components", "blast_radius", "impacted_count", "risk", "risk_level",
              "summary", "total_impacted", "direct_dependents", "transitive_dependents", "components", "unmatched_files"):
        if k in res:
            v = res[k]
            keep[k] = v[:25] if isinstance(v, list) else v
    keep["_keys"] = list(res.keys())[:20]
    return keep or {"raw": json.dumps(res)[:800]}


def _compact_component(res: Any) -> dict[str, Any]:
    if not isinstance(res, dict):
        return {"raw": str(res)[:400]}
    comp = res.get("component", res)
    out = {}
    for k in ("name", "kind", "file_path", "intent", "behavior", "impact", "architecture_layer", "domain", "security_zone",
              "cyclomatic_complexity", "loc", "identity", "structural", "cognitive", "temporal", "boundary"):
        if k in comp:
            v = comp[k]
            out[k] = v if not isinstance(v, str) else v[:600]
    for k in ("dependencies", "dependents", "assemblies"):
        if k in res:
            v = res[k]
            out[k] = v[:12] if isinstance(v, list) else v
    return out or {"raw": json.dumps(res)[:800]}


def project_knowledge(bundle: dict[str, Any], arch: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """MCM bundle → the UI's knowledge.json shape, with the server's assemblies and cognitive layer."""
    parts: dict[str, dict[str, Any]] = {}
    assemblies: list[dict[str, Any]] = []
    knowledge: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def rel(t: str, a: str, b: str, **meta: Any) -> None:
        if (t, a, b) in seen:
            return
        seen.add((t, a, b))
        rels.append({"id": f"rel-{len(rels) + 1}", "type": t, "from": a, "to": b, **meta})

    asm_ids: dict[str, str] = {}
    for a in bundle.get("assemblies", []):
        aid = f"asm-{a['id']}"
        asm_ids[str(a.get("name", "")).lower()] = aid
        assemblies.append({"id": aid, "type": "Assembly", "kind": a.get("layer") or "Assembly", "existence": "Physical",
                           "identity": {"name": a.get("name"), "language": "Java"},
                           "cognitive": {"intent": a.get("intent") or "", "modules": a.get("parts")},
                           "boundary": {"architecture_layer": a.get("layer"), "domain": a.get("domain")},
                           "source": "LegacyLeap MCM (mcm_list_assemblies)"})
    ms = bundle.get("microservices", {})
    for e in ms.get("edges", []) if isinstance(ms, dict) else []:
        fa, ta = _asm_for(e.get("from"), asm_ids), _asm_for(e.get("to"), asm_ids)
        if fa and ta:
            rel("DEPENDS_ON", fa, ta, via="REST (MCM cross_service_edges)")

    # knowledge nodes: Vouch rules (as in the local projection) + MCM layer-boundary rule
    for r in arch.get("rules", []):
        knowledge.append({"id": f"kb-{r['id']}", "type": "Knowledge", "category": "Architecture" if r["kind"] in ("layer", "context", "ownership", "integration", "impact") else "Coding",
                          "rule_id": r["id"], "title": r["title"], "description": r["statement"],
                          "source": {"source_type": "file", "path": r["source"].split("#")[0], "anchor": r["source"], "extracted_at": arch.get("inferred_at")},
                          "status": r.get("status", "inferred"), "confidence": r.get("confidence", 0.8), "checked_by": r.get("checked_by")})
    d = bundle.get("drift", {})
    if isinstance(d, dict) and d.get("score") is not None:
        knowledge.append({"id": "kb-MCM-LAYERS", "type": "Knowledge", "category": "Architecture", "rule_id": "MCM-LAYERS",
                          "title": "Declared layer boundaries (from the MCM)",
                          "description": f"{d.get('summary')} Layers: " + ", ".join(f"{l['layer']} ({l['count']})" for l in bundle.get("layers", [])[:6]),
                          "source": {"source_type": "mcp", "path": bundle.get("url"), "anchor": "mcm_measure_architecture_drift", "extracted_at": bundle.get("fetched_at")},
                          "status": "confirmed", "confidence": 1.0, "checked_by": "mcm_measure_architecture_drift"})
        for h in d.get("hotspots", [])[:8]:
            pid = f"part-mcm-{h.get('name')}"
            parts[pid] = _part(pid, h.get("name"), h.get("kind"), h.get("layer"), h.get("domain"),
                               f"{h.get('violation_count')} boundary-crossing edges (MCM hotspot)")
            rel("VIOLATES", pid, "kb-MCM-LAYERS", count=h.get("violation_count"), source="mcm")

    # parts from the reviews' findings, enriched with the MCM cognitive layer when the server knows them
    comps = bundle.get("components", {})
    for r in reviews:
        prn = f"#{r['pr']['number']}"
        for f in r.get("findings", []):
            first = next((h for h in f.get("trace", []) if h.get("module") not in ("ticket",) and not str(h.get("module", "")).endswith(".py")), None)
            if not first:
                continue
            name = first.get("symbol") or first["module"].rsplit(".", 1)[-1]
            pid = f"part-{first['module']}"
            if pid not in parts:
                cc = comps.get(name, {})
                parts[pid] = _part(pid, name, cc.get("kind", "Module"), cc.get("architecture_layer"), cc.get("domain"),
                                   cc.get("impact") or "", intent=cc.get("intent") or "", behavior=cc.get("behavior") or "",
                                   path=first.get("path"), changed_in=[prn], from_mcm=bool(cc))
            if f.get("rule"):
                rel("VIOLATES", pid, f"kb-{f['rule']['id']}", pr=prn, finding=f["id"], severity=f["severity"], confidence=f["confidence"])
            ctx = first["path"].split("/")[0] if first.get("path") else None
            aid = _asm_for(ctx, asm_ids)
            if aid:
                rel("BELONGS_TO", pid, aid)
        for cchk in r.get("conformant", []):
            rid = cchk.get("rule")
            if rid and any(k["id"] == f"kb-{rid}" for k in knowledge) and r.get("files"):
                p0 = r["files"][0]["path"]
                pid = f"part-file-{p0}"
                if pid not in parts:
                    parts[pid] = _part(pid, p0.rsplit("/", 1)[-1], "File", None, None, "", path=p0, changed_in=[prn])
                rel("SATISFIES", pid, f"kb-{rid}", pr=prn)

    views = [
        {"id": "view-architecture", "type": "Architecture", "name": "Architecture view (MCM assemblies)",
         "description": "The server's assemblies and the cross-service edges it extracted.",
         "filter": {"node_types": ["Assembly"], "relationship_types": ["DEPENDS_ON"]},
         "question": "What does the MCM think the system is made of?"},
        {"id": "view-knowledge", "type": "Security", "name": "Knowledge view",
         "description": "Vouch rules + the MCM's declared layers, and the code that violates or satisfies them.",
         "filter": {"node_types": ["Knowledge", "Part"], "relationship_types": ["GOVERNS", "VIOLATES", "SATISFIES"]},
         "question": "Which rule does this PR break, and what does the MCM already flag as a hotspot?"},
        {"id": "view-domain", "type": "Domain", "name": "Domain view",
         "description": "Findings' components inside the MCM assemblies that own them.",
         "filter": {"node_types": ["Part", "Assembly"], "relationship_types": ["BELONGS_TO", "DEPENDS_ON"], "assembly_existence": ["Physical"]},
         "question": "Is each change in the assembly that owns its vocabulary?"},
        {"id": "view-impact", "type": "Impact", "name": "Impact view",
         "description": "Blast radius per PR as computed by mcm_analyze_git_change_impact.",
         "filter": {"node_types": ["Part", "Assembly"], "relationship_types": ["DEPENDS_ON", "BELONGS_TO"], "assembly_existence": ["Physical"]},
         "question": "If this PR merges, what does the MCM say breaks?"},
    ]
    return {
        "schema_version": "1.0", "generated_at": _now(),
        "spec": "L2-Meta Cognitive Model — projection of the LegacyLeap MCP server (real MCM) + Vouch rules",
        "source": {"kind": "mcp", "url": bundle.get("url"), "project_id": bundle.get("project_id"), "server": bundle.get("server"),
                   "fetched_at": bundle.get("fetched_at")},
        "nodes": {"parts": list(parts.values()), "assemblies": assemblies, "knowledge_base": knowledge},
        "relationships": rels, "views": views,
        "stats": {"parts": len(parts), "assemblies": len(assemblies), "knowledge": len(knowledge), "relationships": len(rels),
                  "views": len(views), "violates": sum(x["type"] == "VIOLATES" for x in rels),
                  "satisfies": sum(x["type"] == "SATISFIES" for x in rels)},
    }


def _asm_for(service: str | None, asm_ids: dict[str, str]) -> str | None:
    if not service:
        return None
    key = f"{service.replace('-', ' ')} project assembly".lower()
    if key in asm_ids:
        return asm_ids[key]
    for k, v in asm_ids.items():
        if service.lower().replace("-", " ") in k:
            return v
    return None


def _part(pid: str, name: str | None, kind: str | None, layer: str | None, domain: str | None, impact: str,
          intent: str = "", behavior: str = "", path: str | None = None, changed_in: list[str] | None = None,
          from_mcm: bool = True) -> dict[str, Any]:
    return {"id": pid, "type": "Part", "kind": kind or "Component",
            "identity": {"name": name, "qualified_name": name, "language": "Java", "repository": "", "file_path": path, "line_start": 1, "line_end": None},
            "structural": {"ast_type": kind, "metrics": {}},
            "cognitive": {"intent": intent, "behavior": behavior, "impact": impact, "vector_id": None, "from_mcm": from_mcm},
            "temporal": {"introduced_in": None, "last_modified": None, "changed_in": changed_in or []},
            "boundary": {"architecture_layer": layer, "domain": domain, "security_zone": None}}
