"""Review one pull request: diff → checks → verdict → review.json."""

from __future__ import annotations

import datetime as dt
import itertools
import time
from pathlib import Path
from typing import Any

from . import checks
from . import rules as rules_file
from .diff import changed_files, rev
from .graph import ImportGraph

SEVERITY_WEIGHT = {"high": 2.0, "medium": 1.0, "low": 0.5}
KIND_LABEL = {
    "layer_violation": "architecture", "wrong_context": "architecture", "transitive_impact": "transitive",
    "duplicate_logic": "consistency", "scope_drift": "functional", "integration_pattern": "integration",
}


def review(repo: Path, graph: ImportGraph, arch: dict[str, Any], base: str, head: str,
           pr: dict[str, Any] | None = None, ticket: dict[str, Any] | None = None) -> dict[str, Any]:
    t0 = time.time()
    files = changed_files(repo, base, head)
    ctx = checks.PRContext(repo, graph, arch, files, base, head)
    counter = itertools.count(1)
    next_id = lambda: f"F{next(counter)}"  # noqa: E731

    findings: list[dict[str, Any]] = []
    conformant: list[dict[str, Any]] = []
    for fn in (checks.check_layers, checks.check_wrong_context, checks.check_transitive_impact,
               checks.check_duplicates, checks.check_naming, checks.check_integration, checks.check_architect_rules):
        f, c = fn(ctx, next_id)
        findings += f
        conformant += c
    f, c = checks.check_scope(ctx, ticket, next_id)
    findings += f
    conformant += c

    rejected = rules_file.rejected_ids(arch)
    muted = [f for f in findings if f.get("rule") and f["rule"]["id"] in rejected]
    findings = [f for f in findings if not (f.get("rule") and f["rule"]["id"] in rejected)]
    for f in muted:
        conformant.append({"check": f"rejected:{f['rule']['id']}", "label": f"{f['rule']['id']} rejected by the architect",
                           "detail": f"would have reported: {f['title']}", "rule": f["rule"]["id"]})
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda x: (order[x["severity"]], -x["confidence"], order[x["stakes"]]))
    for i, f in enumerate(findings, 1):
        f["id"] = f"F{i}"
        f["label"] = KIND_LABEL[f["kind"]]

    verdict = _verdict(findings)
    file_rows = [_file_row(ctx, fc, findings) for fc in files]
    pr = dict(pr or {})
    pr.update({
        "base": base, "head_sha": rev(repo, head), "base_sha": rev(repo, base),
        "files_changed": len(files),
        "additions": sum(f.additions for f in files), "deletions": sum(f.deletions for f in files),
        "ticket": ticket and {"key": ticket.get("key"), "type": ticket.get("type"), "summary": ticket.get("summary")},
        "verdict": verdict,
        "counts": {"findings": len(findings), "conformant_checks": len(conformant),
                   "high": sum(f["severity"] == "high" for f in findings),
                   "medium": sum(f["severity"] == "medium" for f in findings),
                   "low": sum(f["severity"] == "low" for f in findings)},
    })
    summary = _summary(pr, findings, conformant, ticket)
    out = {
        "id": pr.get("id", head),
        "pr": pr,
        "analysed_at": _now(),
        "duration_ms": int((time.time() - t0) * 1000),
        "model": {"architecture_version": arch["inferred_at"], "expensive_layer_reused": True,
                  "modules_in_graph": len(graph.modules)},
        "summary": summary,
        "files": file_rows,
        "findings": findings,
        "conformant": conformant,
        "diagram": _diagram(findings),
        "comment_markdown": "",
    }
    from .render import comment_markdown  # late import: render needs the shapes above

    out["comment_markdown"] = comment_markdown(out)
    return out


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _verdict(findings: list[dict[str, Any]]) -> dict[str, Any]:
    penalty = sum(SEVERITY_WEIGHT[f["severity"]] * (1 if f["confidence"] >= 0.8 else 0.6) for f in findings)
    score = max(1, min(5, round(5 - penalty)))
    arch_f = [f for f in findings if f["axis"] == "architectural"]
    func_f = [f for f in findings if f["axis"] == "functional"]

    def axis(fs: list[dict[str, Any]], hard_kinds: set[str]) -> str:
        if any(f["kind"] in hard_kinds and f["severity"] == "high" for f in fs):
            return "violated"
        if fs:
            return "uncertain"
        return "conformant"

    architectural = axis(arch_f, {"layer_violation", "wrong_context", "integration_pattern"})
    functional = axis(func_f, {"scope_drift"})
    if architectural == "violated" or functional == "violated":
        status = "violates"
        score = min(score, 2)
    elif findings:
        status = "needs_judgement"
    else:
        status = "conformant"
    headline = findings[0]["title"] if findings else "Conformant — nothing needs your attention"
    rationale = {
        5: "Checked and conformant; safe to merge on architectural grounds.",
        4: "Minor points only; merge after a glance at the findings.",
        3: "Needs a reviewer's judgement on the findings before merge.",
        2: "Violates the intended architecture; needs changes or an explicit waiver.",
        1: "Multiple high-severity violations; do not merge as is.",
    }[score]
    return {"status": status, "confidence_score": score, "architectural": architectural,
            "functional": functional, "headline": headline, "rationale": rationale}


def _file_row(ctx: checks.PRContext, fc: Any, findings: list[dict[str, Any]]) -> dict[str, Any]:
    m = ctx.head_modules.get(fc.path) or ctx.base_modules.get(fc.path)
    context, layer = ctx.graph.classify(m.name) if m else (None, None)
    mine = [f for f in findings if f["location"]["path"] == fc.path or any(h["path"] == fc.path for h in f["trace"][:2])]
    score = 5 - min(4, int(sum(SEVERITY_WEIGHT[f["severity"]] for f in mine)))
    if mine:
        overview = mine[0]["title"]
    elif fc.status == "added":
        overview = "New module; no rule broken."
    elif fc.path.endswith(".md"):
        overview = "Documentation only."
    else:
        overview = "Touched lines stay inside the module's existing contract."
    ranges: list[list[int]] = []
    for ln in sorted(fc.added_lines):
        if ranges and ln == ranges[-1][1] + 1:
            ranges[-1][1] = ln
        else:
            ranges.append([ln, ln])
    return {"path": fc.path, "status": fc.status, "additions": fc.additions, "deletions": fc.deletions,
            "context": context, "layer": layer, "score": score, "overview": overview, "added_ranges": ranges}


def _summary(pr: dict[str, Any], findings: list[dict[str, Any]], conformant: list[dict[str, Any]],
             ticket: dict[str, Any] | None) -> str:
    n = pr["files_changed"]
    against = f"the intended architecture and {ticket['key']}" if ticket else "the intended architecture"
    head = f"Vouch checked {n} file(s) (+{pr['additions']}/−{pr['deletions']}) against {against}."
    if not findings:
        return f"{head} {len(conformant)} checks passed; nothing needs a reviewer's attention."
    highs = [f for f in findings if f["severity"] == "high"]
    lead = highs[0] if highs else findings[0]
    tail = (f" {len(findings) - 1} further finding(s) below." if len(findings) > 1 else "")
    return f"{head} {len(conformant)} checks passed. What needs judgement: {lead['title']}.{tail}"


def _diagram(findings: list[dict[str, Any]]) -> str:
    """Mermaid sequence/flow diagram for the first traced finding (Greptile shows one per review)."""
    for f in findings:
        if len(f["trace"]) >= 2 and f["kind"] in ("transitive_impact", "layer_violation", "wrong_context", "integration_pattern"):
            lines = ["flowchart LR"]
            prev = None
            for h in f["trace"]:
                node = f"h{h['hop']}"
                label = h["module"].split(".")[-1] if h["module"] != "ticket" else h["symbol"]
                sub = h["symbol"] if h["symbol"] and h["symbol"] != label else ""
                text = f"{label}" + (f"<br/><small>{sub}</small>" if sub else "")
                style = ":::changed" if h["hop"] == 0 else ""
                lines.append(f'  {node}["{text}"]{style}')
                if prev:
                    lines.append(f"  {prev} --> {node}")
                prev = node
            lines.append("  classDef changed fill:#3b1d1d,stroke:#f87171,color:#fee2e2;")
            return "\n".join(lines)
    return ""
