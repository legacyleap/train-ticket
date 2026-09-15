"""Post a Vouch review to GitHub: one inline comment per finding, on the line it points at.

Runs inside the GitHub Action (or locally with a token). Stdlib only. Idempotent: comments carry a
hidden marker, and every previous Vouch inline comment on the PR is deleted before the new ones are
posted, so a re-run never duplicates. The sticky summary comment is handled separately by the
workflow; this module is the "GitHub App" half — with an app token, everything appears as `vouch[bot]`
with the app's avatar instead of `github-actions[bot]`.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

MARKER = "<!-- vouch-inline -->"
API = "https://api.github.com"


def _req(method: str, url: str, token: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json", "User-Agent": "vouch",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def _paged(url: str, token: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        st, res = _req("GET", f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}", token)
        if st != 200 or not res:
            break
        out += res
        if len(res) < 100:
            break
        page += 1
    return out


def _anchor_line(finding: dict[str, Any], file_row: dict[str, Any] | None) -> int | None:
    """A line GitHub will accept: inside the diff of that file, nearest to the finding's line."""
    if not file_row or not file_row.get("added_ranges"):
        return None
    want = finding["location"].get("line") or 1
    best, dist = None, 10**9
    for start, end in file_row["added_ranges"]:
        cand = min(max(want, start), end)
        d = abs(cand - want)
        if d < dist:
            best, dist = cand, d
    return best


def _comment_body(f: dict[str, Any], ui_url: str, pr_id: str) -> str:
    dot = {"high": "🔴", "medium": "🟠", "low": "🟡"}[f["severity"]]
    rule = f" · rule `{f['rule']['id']}`" if f.get("rule") else ""
    lines = [
        MARKER,
        f"{dot} **Vouch · {f['id']} · `{f.get('label', f['kind'])}`** — {f['title']}",
        f"<sub>{f['severity']} · confidence {f['confidence_label']} ({f['confidence']:.2f}) · stakes {f['stakes']}{rule}</sub>",
        "",
        f["explanation"],
        "",
    ]
    if f.get("trace") and len(f["trace"]) > 1:
        lines.append("<details><summary>Trace</summary>\n")
        for h in f["trace"]:
            where = f"`{h['path']}:{h['line']}`" if h.get("line") else f"`{h['path']}`"
            lines.append(f"- hop {h['hop']} — `{h['symbol'] or h['module']}` {where} — {h['note']}")
        lines.append("\n</details>\n")
    lines.append(f"**Suggested fix.** {f['suggested_fix']}")
    if f.get("fix_prompt"):
        lines.append(f"\n<details><summary>Prompt to fix with AI</summary>\n\n```\n{f['fix_prompt']}\n```\n\n</details>")
    if ui_url:
        lines.append(f"\n<sub>[Open in Vouch]({ui_url.rstrip('/')}/#/review/{pr_id}) · 👎 to dismiss · `/vouch pattern` to propose a rule</sub>")
    return "\n".join(lines)


def post_inline(review: dict[str, Any], repo: str, pr_number: int, token: str, commit_sha: str,
                ui_url: str = "", pr_id: str = "") -> dict[str, Any]:
    base = f"{API}/repos/{repo}/pulls/{pr_number}"
    # 1. remove our previous inline comments (idempotent re-runs)
    removed = 0
    for c in _paged(f"{base}/comments", token):
        if MARKER in (c.get("body") or ""):
            st, _ = _req("DELETE", f"{API}/repos/{repo}/pulls/comments/{c['id']}", token)
            removed += st in (204, 200)
    # 2. one comment per finding, anchored to a diff line of the finding's file
    files = {f["path"]: f for f in review.get("files", [])}
    posted, skipped = [], []
    for f in review.get("findings", []):
        path = f["location"]["path"]
        line = _anchor_line(f, files.get(path))
        if line is None:
            skipped.append((f["id"], path, "no diff line in this file"))
            continue
        body = {"body": _comment_body(f, ui_url, pr_id), "commit_id": commit_sha, "path": path,
                "line": line, "side": "RIGHT"}
        st, res = _req("POST", f"{base}/comments", token, body)
        if st == 201:
            posted.append((f["id"], path, line))
        else:
            skipped.append((f["id"], path, f"HTTP {st}: {str(res)[:120]}"))
    return {"removed": removed, "posted": posted, "skipped": skipped}


def main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="vouch github-review",
                                description="post one inline comment per finding on a GitHub pull request")
    p.add_argument("review", help="review.json written by `vouch review --out`")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--sha", required=True, help="head commit sha the comments attach to")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    p.add_argument("--ui-url", default=os.environ.get("VOUCH_UI_URL", ""))
    p.add_argument("--pr-id", default=os.environ.get("VOUCH_PR_ID", ""))
    a = p.parse_args(argv)
    if not a.token:
        print("no token (GITHUB_TOKEN or --token)", file=sys.stderr)
        return 2
    review = json.loads(open(a.review, encoding="utf-8").read())
    res = post_inline(review, a.repo, a.pr, a.token, a.sha, a.ui_url, (a.pr_id or "").split("/")[-1])
    print(f"vouch github-review: removed {res['removed']} old, posted {len(res['posted'])} inline comment(s)")
    for fid, path, line in res["posted"]:
        print(f"  {fid} → {path}:{line}")
    for fid, path, why in res["skipped"]:
        print(f"  {fid} skipped ({path}): {why}")
    return 0
