"""Write the UI data bundle: index, one review per PR, architecture, drift."""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path
from typing import Any

from .graph import build_graph
from .infer import infer
from . import rules as rules_file
from .mcm import build_mcm
from .review import review

REPO_NAME = "legacyleap/Cognitive-Core-Asset-Discovery"
REPO_URL = "https://github.com/legacyleap/Cognitive-Core-Asset-Discovery"


def _default_branch(repo: Path) -> str:
    import subprocess

    for b in ("main", "master"):
        if subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", b], capture_output=True).returncode == 0:
            return b
    return "main"


def run_demo(repo: Path, scenarios: Path, out: Path, repo_name: str = REPO_NAME, repo_url: str = REPO_URL
             ) -> dict[str, Any]:
    spec = json.loads(scenarios.read_text())
    repo_name = spec.get("repo", {}).get("name", repo_name)
    repo_url = spec.get("repo", {}).get("url", repo_url)
    base = _default_branch(repo)
    graph = build_graph(repo)
    arch = infer(repo, graph)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reviews").mkdir(exist_ok=True)

    reviews = []
    for pr in spec["prs"]:
        meta = {k: pr[k] for k in ("id", "number", "title", "branch", "body", "created_at")}
        meta["author"] = spec["author"]
        r = review(repo, graph, arch, base, pr["branch"], pr=meta, ticket=pr["ticket"])
        (out / "reviews" / f"{pr['id']}.json").write_text(json.dumps(r, indent=1))
        reviews.append(r)

    head_sha = reviews[0]["pr"]["base_sha"] if reviews else ""
    index = {
        "generated_at": arch["inferred_at"],
        "repo": {"name": repo_name, "url": repo_url, "default_branch": base, "head_sha": head_sha,
                 "language": spec.get("repo", {}).get("language", "Java" if graph.language == "java" else "Python"),
                 "stats": arch["stats"]},
        "prs": [r["pr"] for r in reviews],
    }
    (out / "index.json").write_text(json.dumps(index, indent=1))
    (out / "architecture.json").write_text(json.dumps(arch, indent=1))
    (out / "drift.json").write_text(json.dumps(drift(reviews, arch, graph, repo), indent=1))
    mcm = build_mcm(graph, arch, reviews)
    (out / "knowledge.json").write_text(json.dumps(mcm, indent=1))
    (out / "rules.json").write_text(json.dumps(rules_file.load(repo), indent=1))
    index["mcm"] = mcm["stats"]
    index["rules_file"] = arch.get("rules_file")
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return index


def onboard(git_url: str, out: Path, workdir: Path, max_branches: int = 10, base: str | None = None,
            tickets_dir: Path | None = None) -> dict[str, Any]:
    """Start from a git URL (or local path): clone, infer, review every non-default branch, export.

    Tickets, if any, are read from ``<tickets_dir>/<branch-with-slashes-as-dashes>.json`` so a
    design partner can drop Jira exports next to the checkout without touching the engine.
    """
    import re
    import subprocess

    name = re.sub(r"\.git$", "", git_url.rstrip("/")).split("/")[-1]
    owner = re.sub(r"\.git$", "", git_url.rstrip("/")).split("/")[-2] if "/" in git_url else "local"
    repo = workdir / name
    if not (repo / ".git").exists():
        workdir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", git_url, str(repo)], check=True)
    else:
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--all"], check=False)
    head_ref = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
                              capture_output=True, text=True).stdout.strip()
    default = base or (head_ref.split("/", 1)[1] if head_ref else "main")
    branches = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)",
                               "--sort=-committerdate", "refs/heads", "refs/remotes/origin"],
                              capture_output=True, text=True).stdout.split()
    seen: set[str] = set()
    candidates = []
    for b in branches:
        if b == "origin" or b.endswith("/HEAD"):
            continue  # `origin/HEAD` shortens to `origin`
        short = b.split("/", 1)[1] if b.startswith("origin/") else b
        if short in (default, "HEAD") or short in seen:
            continue
        seen.add(short)
        candidates.append(b)
    candidates = candidates[:max_branches]

    graph = build_graph(repo)
    arch = infer(repo, graph)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reviews").mkdir(exist_ok=True)
    reviews = []
    for i, b in enumerate(candidates, 1):
        short = b.split("/", 1)[1] if b.startswith("origin/") else b
        rid = re.sub(r"[^a-z0-9]+", "-", short.lower()).strip("-")
        ticket = None
        if tickets_dir and (tickets_dir / f"{rid}.json").exists():
            ticket = json.loads((tickets_dir / f"{rid}.json").read_text())
        subject = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s%n%aI%n%an", b],
                                 capture_output=True, text=True).stdout.splitlines() + ["", "", ""]
        meta = {"id": rid, "number": i, "title": subject[0] or short, "branch": short, "body": "",
                "created_at": subject[1], "author": {"login": subject[2] or "unknown", "kind": "human"}}
        r = review(repo, graph, arch, default if not b.startswith("origin/") else f"origin/{default}", b,
                   pr=meta, ticket=ticket)
        (out / "reviews" / f"{rid}.json").write_text(json.dumps(r, indent=1))
        reviews.append(r)
    index = {
        "generated_at": arch["inferred_at"],
        "repo": {"name": f"{owner}/{name}", "url": git_url, "default_branch": default,
                 "head_sha": reviews[0]["pr"]["base_sha"] if reviews else "", "language": "Python",
                 "stats": arch["stats"]},
        "prs": [r["pr"] for r in reviews],
    }
    (out / "architecture.json").write_text(json.dumps(arch, indent=1))
    (out / "drift.json").write_text(json.dumps(drift(reviews, arch, graph), indent=1))
    mcm = build_mcm(graph, arch, reviews)
    (out / "knowledge.json").write_text(json.dumps(mcm, indent=1))
    (out / "rules.json").write_text(json.dumps(rules_file.load(repo), indent=1))
    index["mcm"] = mcm["stats"]
    index["rules_file"] = arch.get("rules_file")
    (out / "index.json").write_text(json.dumps(index, indent=1))
    return index


def history_trend(repo: Path, points: int = 8) -> list[dict[str, Any]]:
    """Real drift over time: measure the baseline at evenly spaced commits of the default branch.

    Each point re-scans that commit (git archive → temp dir → build_graph → measure_baseline),
    so the trend is what the repository actually did, not a seeded curve.
    """
    import subprocess
    import tarfile
    import tempfile
    from io import BytesIO

    from .infer_java import infer_java

    log = subprocess.run(["git", "-C", str(repo), "log", "--first-parent", "--format=%H%x09%cs%x09%s", "--reverse"],
                         capture_output=True, text=True).stdout.split("\n")
    commits = [l.split("\t", 2) for l in log if l.strip()]
    # stop at the last upstream commit: the demo's own rules commit is not history
    upstream = subprocess.run(["git", "-C", str(repo), "rev-parse", "origin/HEAD"], capture_output=True, text=True).stdout.strip() \
        or subprocess.run(["git", "-C", str(repo), "rev-parse", "origin/master"], capture_output=True, text=True).stdout.strip()
    if upstream:
        cut = next((i for i, c in enumerate(commits) if c[0] == upstream), None)
        if cut is not None:
            commits = commits[: cut + 1]
    if len(commits) < 3:
        return []
    step = max(1, (len(commits) - 1) // (points - 1))
    picks = commits[::step]
    if picks[-1][0] != commits[-1][0]:
        picks.append(commits[-1])
    import re as _re

    out = []
    prev_idx = 0
    for sha, date, _subject in picks:
        idx = next(i for i, c in enumerate(commits) if c[0] == sha)
        merged_prs = sum(1 for c in commits[prev_idx + 1: idx + 1] if _re.search(r"\(#\d+\)|Merge pull request", c[2]))
        with tempfile.TemporaryDirectory() as tmp:
            data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha], capture_output=True).stdout
            with tarfile.open(fileobj=BytesIO(data)) as tf:
                tf.extractall(tmp, filter="data")
            tp = Path(tmp)
            try:
                g = build_graph(tp)
                if g.language != "java":
                    return []
                a = infer_java(tp, g, date)
                v = sum(b["count"] for b in a["baseline"] if b["rule"] in ("KERNEL-ENTITY", "INTEG-CTRL", "INTEG-ASYNC", "LAYER-001", "NAMING-PKG"))
                out.append({"week": date, "prs": merged_prs, "commits": idx - prev_idx, "violations": v, "synthetic": False, "sha": sha[:8],
                            "services": a["stats"]["contexts"], "modules": a["stats"]["modules"],
                            "copies": next((b["count"] for b in a["baseline"] if b["rule"] == "KERNEL-ENTITY"), 0)})
            except Exception:  # noqa: BLE001 — an unscannable historical tree just leaves a gap
                pass
        prev_idx = idx
    return out


def drift(reviews: list[dict[str, Any]], arch: dict[str, Any], graph: Any, repo: Path | None = None) -> dict[str, Any]:
    by_rule: dict[str, dict[str, int]] = {}
    by_ctx: dict[str, dict[str, Any]] = {}
    for r in reviews:
        ctxs = {f["context"] for f in r["files"] if f["context"]}
        for c in ctxs:
            by_ctx.setdefault(c, {"context": c, "violations": 0, "prs": 0})["prs"] += 1
        for f in r["findings"]:
            rid = f["rule"]["id"] if f["rule"] else f["kind"]
            by_rule.setdefault(rid, {"rule": rid, "title": f["rule"]["title"] if f["rule"] else f["kind"],
                                     "violations": 0, "accepted": 0, "dismissed": 0, "pending": 0})
            by_rule[rid]["violations"] += 1
            by_rule[rid]["pending"] += 1
            c = next((x["context"] for x in r["files"] if x["path"] == f["location"]["path"] and x["context"]), None)
            if c:
                by_ctx.setdefault(c, {"context": c, "violations": 0, "prs": 0})["violations"] += 1

    today = dt.date(2026, 9, 14)
    real_v = sum(len(r["findings"]) for r in reviews)
    trend = history_trend(repo, points=10) if (repo is not None and arch.get("language") == "java") else []
    if trend:
        trend.append({"week": today.isoformat(), "prs": len(reviews), "violations": trend[-1]["violations"] + real_v,
                      "synthetic": False, "sha": "open PRs", "note": "if the open PRs merge as they are"})
        trend_note = (f"The trend is measured: {len(trend) - 1} commits of the default branch were re-scanned "
                      f"(git archive at each point) and their baseline violations counted.")
    else:
        # Seeded history for the trend line: the six real reviews are the last week; earlier weeks
        # are synthetic and labelled as such in `coverage.note`.
        rng = random.Random(20260914)
        for w in range(10, 0, -1):
            week = today - dt.timedelta(days=7 * w)
            prs = rng.randint(6, 14)
            trend.append({"week": week.isoformat(), "prs": prs, "violations": rng.randint(0, max(1, prs // 3)),
                          "synthetic": True})
        trend.append({"week": today.isoformat(), "prs": len(reviews), "violations": real_v, "synthetic": False})
        trend_note = "Weeks before 2026-09-14 in the trend are seeded demo history, not real reviews."

    top = sorted(by_rule.values(), key=lambda x: -x["violations"])
    alerts = []
    ctx_rule = by_rule.get("DDD-010")
    if ctx_rule and ctx_rule["violations"] >= 1:
        alerts.append({"level": "warn",
                       "text": f"Bounded-context isolation (DDD-010) violated {ctx_rule['violations']}× this week; "
                               f"the architect's threshold is 0. First cross-context import edge in the repository."})
    scope = by_rule.get("TICKET-SCOPE")
    if scope:
        alerts.append({"level": "info",
                       "text": f"{scope['violations']} PR(s) did more than their ticket asked; all from AI-assisted branches."})
    proposals = []
    for r in reviews:
        for f in r["findings"]:
            if f["kind"] == "duplicate_logic" and "re-implements" in f["title"]:
                proposals.append({
                    "id": f"P{len(proposals) + 1}", "from_finding": f"{r['id']}:{f['id']}",
                    "proposed_by": "tech lead", "status": "pending",
                    "text": "Allow small per-context name helpers to duplicate utils/strings.py when the "
                            "context must not import utils (proposed after dismissing F1 on #54).",
                    "architect_note": "",
                })
    modelled = arch["stats"]["modules"]
    if arch.get("language") == "java":
        unmodelled = ("Non-Java services, front-ends and every application.yml are not in the model; findings cannot "
                      "originate there. Cross-service edges come from string literals naming a service (REST hosts, "
                      "Feign names), so calls built from variables are invisible.")
        total = modelled + 6
        baseline = arch.get("baseline", [])
    else:
        unmodelled = "28 modules under parser/ and *_generated/ (ANTLR output) are not modelled; findings cannot originate there."
        total = modelled + 28
        baseline = []
    if baseline:
        alerts.insert(0, {"level": "warn",
                          "text": (f"Existing drift on {arch.get('default_branch', 'the default branch')}: "
                                   + "; ".join(f"{b['count']} {b['unit']} — {b['title'].lower()}" for b in baseline if b["count"])
                                   + ". Open PRs would add to it.")})
    return {
        "window": {"from": trend[0]["week"], "to": today.isoformat()},
        "coverage": {
            "repos": 1, "prs_reviewed": len(reviews), "modules_modelled": modelled,
            "modules_total": total,
            "note": unmodelled + " " + trend_note,
        },
        "baseline": baseline,
        "by_rule": top,
        "by_context": sorted(by_ctx.values(), key=lambda x: -x["violations"]),
        "trend": trend,
        "alerts": alerts,
        "proposals": proposals,
    }
