"""Render a review as the pull-request comment Vouch would post (Greptile-style markdown)."""

from __future__ import annotations

from typing import Any

DOTS = {"high": "🔴", "medium": "🟠", "low": "🟡"}
AXIS = {"conformant": "✅ conformant", "uncertain": "🟠 needs judgement", "violated": "🔴 violated"}


def comment_markdown(r: dict[str, Any]) -> str:
    pr, v = r["pr"], r["pr"]["verdict"]
    ticket = pr.get("ticket") or {}
    lines: list[str] = []
    lines.append("## Vouch review" + (f" · {ticket.get('key')}" if ticket.get("key") else ""))
    lines.append("")
    lines.append("### Summary")
    lines.append(r["summary"])
    lines.append("")
    meter = "●" * v["confidence_score"] + "○" * (5 - v["confidence_score"])
    lines.append(f"**Confidence Score: {v['confidence_score']}/5** {meter}  ")
    lines.append(f"{v['rationale']}")
    lines.append("")
    lines.append(f"**Architectural conformance:** {AXIS[v['architectural']]} · "
                 f"**Functional conformance:** {AXIS[v['functional']]}")
    lines.append("")
    if r["files"]:
        lines.append("### Important files changed")
        lines.append("")
        lines.append("| Filename | Score | Overview |")
        lines.append("|---|---|---|")
        for f in sorted(r["files"], key=lambda x: x["score"])[:8]:
            lines.append(f"| `{f['path']}` | {f['score']}/5 | {f['overview']} |")
        lines.append("")
    if r["findings"]:
        lines.append(f"### Needs your judgement ({len(r['findings'])})")
        lines.append("")
        for f in r["findings"]:
            rule = f" · rule `{f['rule']['id']}`" if f.get("rule") else ""
            lines.append(f"#### {DOTS[f['severity']]} `{f['label']}` {f['title']}")
            lines.append(f"<sub>confidence {f['confidence_label']} ({f['confidence']:.2f}) · stakes {f['stakes']}{rule} · "
                         f"`{f['location']['path']}:{f['location']['line']}`</sub>")
            lines.append("")
            lines.append(f["explanation"])
            lines.append("")
            if f["trace"]:
                lines.append("<details><summary>Trace</summary>")
                lines.append("")
                for h in f["trace"]:
                    where = f"`{h['path']}:{h['line']}`" if h["line"] else f"`{h['path']}`"
                    lines.append(f"- hop {h['hop']} — `{h['symbol'] or h['module']}` {where} — {h['note']}")
                if f.get("diff_snippet"):
                    lines.append("")
                    lines.append("```diff")
                    lines.append(f["diff_snippet"])
                    lines.append("```")
                lines.append("")
                lines.append("</details>")
                lines.append("")
            lines.append(f"**Suggested fix.** {f['suggested_fix']}")
            lines.append("")
            lines.append("<details><summary>Prompt to fix with AI</summary>")
            lines.append("")
            lines.append("```")
            lines.append(f["fix_prompt"])
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")
    if r["conformant"]:
        lines.append(f"<details><summary>Checked and conformant ({len(r['conformant'])})</summary>")
        lines.append("")
        for c in r["conformant"]:
            lines.append(f"- **{c['label']}** — {c['detail']}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    lines.append("---")
    lines.append(f"<sub>Vouch checked against the architecture model of {r['model']['architecture_version']} "
                 f"({r['model']['modules_in_graph']} modules, reused — not rebuilt for this PR) in "
                 f"{r['duration_ms']} ms. 👎 dismisses a finding in one click; `/vouch pattern` proposes it as an "
                 f"organisation rule for the architect to approve. Nothing is merged automatically.</sub>")
    return "\n".join(lines)
