"""Architect-owned rules: ``.vouch/rules.json`` in the repository.

The inferred rules are a draft. This file is where the architect's decisions live, in git,
next to the code: confirm or reject an inferred rule, or add a new one. Rules the UI exports
have exactly this shape, so "Export rules.json → commit" closes the loop without a backend.

Schema (version 1)::

    {"version": 1, "rules": [
      {"id": "CLI-001", "title": "...", "statement": "...",
       "kind": "naming | forbidden_import | ownership | sensitive_path | manual",
       "params": {...},                      # per kind, see KINDS below
       "status": "confirmed | rejected | inferred",
       "severity": "high | medium | low",
       "source": "architect: <name>, <date>", "proposed_by": "...", "approved_by": "...",
       "created_at": "..."},
      {"id": "DDD-013", "status": "confirmed"}   # an override for an inferred rule needs only id + status
    ]}

Kinds and their params:

* ``forbidden_import`` — ``{"from": {"context"?, "layer"?, "path_glob"?}, "to": {"context"?, "layer"?, "module_prefix"?}}``
* ``naming`` — ``{"path_glob", "module_suffix"?, "class_suffix"?, "new_only": true}``
* ``ownership`` — ``{"context", "vocabulary": [..]}`` (extends the wrong-context heuristic)
* ``sensitive_path`` — ``{"paths": [..], "why"}`` (extends ticket-scope drift)
* ``manual`` — no checker; the rule is a Knowledge node reviewers see, not something Vouch enforces yet
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

RULES_PATH = ".vouch/rules.json"
KINDS = ("forbidden_import", "naming", "ownership", "sensitive_path", "manual")
CHECKED_BY = {"forbidden_import": "layer_violation", "naming": "naming", "ownership": "wrong_context",
              "sensitive_path": "scope_drift", "manual": "not machine-checked"}


def load(repo: Path) -> dict[str, Any]:
    p = repo / RULES_PATH
    if not p.exists():
        return {"version": 1, "rules": []}
    data = json.loads(p.read_text())
    data.setdefault("version", 1)
    data.setdefault("rules", [])
    return data


def save(repo: Path, data: dict[str, Any]) -> Path:
    p = repo / RULES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")
    return p


def merge_into(arch: dict[str, Any], file_rules: dict[str, Any]) -> dict[str, Any]:
    """Apply the file to the inferred rules: overrides by id, new rules appended."""
    by_id = {r["id"]: r for r in arch["rules"]}
    for r in arch["rules"]:
        r.setdefault("origin", "vouch" if r["source"].startswith("Vouch default") else "inferred")
        r.setdefault("severity", "high" if r["kind"] in ("layer", "context") else "medium")
    for fr in file_rules.get("rules", []):
        rid = fr.get("id")
        if not rid:
            continue
        if rid in by_id:  # override of an inferred rule
            for k in ("status", "severity", "approved_by", "proposed_by", "note"):
                if k in fr:
                    by_id[rid][k] = fr[k]
            if fr.get("status") in ("confirmed", "rejected"):
                by_id[rid]["decided_in"] = RULES_PATH
            continue
        kind = fr.get("kind", "manual")
        if kind not in KINDS:
            kind = "manual"
        rule = {
            "id": rid, "kind": kind, "title": fr.get("title", rid), "statement": fr.get("statement", ""),
            "source": fr.get("source", f"{RULES_PATH}"), "status": fr.get("status", "confirmed"),
            "confidence": 1.0 if fr.get("status", "confirmed") == "confirmed" else 0.5,
            "checked_by": CHECKED_BY[kind], "evidence": _evidence(kind, fr.get("params", {})),
            "origin": "architect", "severity": fr.get("severity", "medium"), "params": fr.get("params", {}),
            "proposed_by": fr.get("proposed_by"), "approved_by": fr.get("approved_by"),
            "created_at": fr.get("created_at"), "decided_in": RULES_PATH,
        }
        arch["rules"].append(rule)
        by_id[rid] = rule
    arch["rules_file"] = {"path": RULES_PATH, "present": bool(file_rules.get("rules")),
                          "count": len(file_rules.get("rules", [])),
                          "rejected": sorted(r["id"] for r in arch["rules"] if r.get("status") == "rejected"),
                          "confirmed": sorted(r["id"] for r in arch["rules"] if r.get("status") == "confirmed")}
    arch["stats"]["rules"] = len(arch["rules"])
    return arch


def _evidence(kind: str, params: dict[str, Any]) -> str:
    if kind == "forbidden_import":
        return f"deterministic: imports matching {json.dumps(params.get('from', {}))} → {json.dumps(params.get('to', {}))}"
    if kind == "naming":
        return f"deterministic: files matching {params.get('path_glob', '*')}" + (" (new files only)" if params.get("new_only", True) else "")
    if kind == "ownership":
        return f"heuristic: vocabulary {params.get('vocabulary', [])} belongs to {params.get('context')}"
    if kind == "sensitive_path":
        return f"ticket scope: {params.get('paths', [])} need an explicit mention in the ticket"
    return "not machine-checked yet; shown to reviewers as a Knowledge node"


def active(arch: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Architect rules of one kind that are not rejected."""
    return [r for r in arch["rules"] if r.get("origin") == "architect" and r["kind"] == kind
            and r.get("status") != "rejected"]


def rejected_ids(arch: dict[str, Any]) -> set[str]:
    return {r["id"] for r in arch["rules"] if r.get("status") == "rejected"}


def import_export(repo: Path, exported: dict[str, Any], who: str | None = None) -> tuple[Path, int, int]:
    """Merge a UI export (same schema) into the repo's rules file. Returns (path, added, updated)."""
    current = load(repo)
    by_id = {r["id"]: r for r in current["rules"]}
    added = updated = 0
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for r in exported.get("rules", []):
        if not r.get("id"):
            continue
        r = dict(r)
        r.setdefault("created_at", stamp)
        if who and not r.get("approved_by"):
            r["approved_by"] = who
        if r["id"] in by_id:
            by_id[r["id"]].update(r)
            updated += 1
        else:
            current["rules"].append(r)
            by_id[r["id"]] = r
            added += 1
    return save(repo, current), added, updated
