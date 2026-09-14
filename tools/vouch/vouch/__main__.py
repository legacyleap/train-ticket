"""Vouch command line.

    python3 -m vouch infer   <repo> [--out architecture.json]
    python3 -m vouch review  <repo> --base main --head <branch> [--ticket ticket.json] [--arch architecture.json]
                             [--out review.json] [--markdown]
    python3 -m vouch comment review.json               # print the PR comment
    python3 -m vouch demo    --repo target-repo --out ui/data [--scenarios scenarios/scenarios.json]
    python3 -m vouch hook    install <repo>            # pre-push hook: review before you push
    python3 -m vouch mcm     fetch --project <uuid> --data ui/data/<project>   # pull the real MCM from the LegacyLeap MCP server
    python3 -m vouch mcm     projects                  # list projects indexed on the server
    python3 -m vouch rules   list <repo>               # inferred + architect rules and their status
    python3 -m vouch rules   import <repo> exported.json [--by "Rajat S."]   # merge a UI export into .vouch/rules.json
    python3 -m vouch onboard <git-url|path> --out ui/data [--workdir .vouch-repos] [--max-branches 10]
                             [--tickets tickets/]      # start from any repository: clone → infer → review branches
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from . import rules as rules_file
from .export import onboard, run_demo
from .graph import build_graph
from .infer import infer
from .render import comment_markdown
from .review import review

HERE = Path(__file__).resolve().parent


def cmd_infer(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    arch = infer(repo)
    text = json.dumps(arch, indent=1)
    if a.out:
        Path(a.out).write_text(text)
        s = arch["stats"]
        print(f"inferred {s['contexts']} bounded contexts, {s['rules']} rules from {s['modules']} modules → {a.out}")
    else:
        print(text)
    return 0


def cmd_review(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    graph = build_graph(repo)
    arch = json.loads(Path(a.arch).read_text()) if a.arch else infer(repo, graph)
    ticket = json.loads(Path(a.ticket).read_text()) if a.ticket else None
    head = a.head or "HEAD"
    r = review(repo, graph, arch, a.base, head, pr={"id": head, "title": head, "branch": head}, ticket=ticket)
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=1))
    if a.markdown or not a.out:
        print(r["comment_markdown"])
    v = r["pr"]["verdict"]
    print(f"\nvouch: {v['status']} · confidence {v['confidence_score']}/5 · "
          f"{len(r['findings'])} finding(s), {len(r['conformant'])} checks passed · {r['duration_ms']} ms",
          file=sys.stderr)
    return 0 if v["status"] != "violates" else 3


def cmd_comment(a: argparse.Namespace) -> int:
    r = json.loads(Path(a.review).read_text())
    print(comment_markdown(r))
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    scenarios = Path(a.scenarios or HERE.parent / "scenarios" / "scenarios.json")
    index = run_demo(repo, scenarios, Path(a.out))
    for pr in index["prs"]:
        v = pr["verdict"]
        print(f"  #{pr['number']} {v['status']:<15} {v['confidence_score']}/5  {pr['counts']['findings']} finding(s)  {pr['title']}")
    print(f"wrote {len(index['prs'])} reviews → {a.out}")
    return 0


def cmd_onboard(a: argparse.Namespace) -> int:
    index = onboard(a.url, Path(a.out), Path(a.workdir), a.max_branches, a.base,
                    Path(a.tickets) if a.tickets else None)
    s = index["repo"]["stats"]
    print(f"{index['repo']['name']}: {s['modules']} modules, {s['contexts']} bounded contexts, {s['rules']} rules")
    for pr in index["prs"]:
        v = pr["verdict"]
        print(f"  {pr['branch']:<40} {v['status']:<15} {v['confidence_score']}/5  {pr['counts']['findings']} finding(s)")
    print(f"wrote {len(index['prs'])} reviews → {a.out}")
    return 0


def cmd_mcm(a: argparse.Namespace) -> int:
    from .mcm_client import MCMClient
    from .mcm_remote import fetch_bundle

    if a.action == "projects":
        c = MCMClient()
        c.connect()
        for p in c.projects():
            print(f"  {p.get('project_id')}  {p.get('name'):<32} parts={p.get('parts')} assemblies={p.get('assemblies')}")
        return 0
    b = fetch_bundle(a.project, Path(a.data))
    h = b.get("health", {}) or {}
    d = b.get("drift", {}) or {}
    print(f"{b['server'].get('name')} {b['server'].get('version')} · project {a.project}")
    print(f"  statistics: {b.get('statistics')}")
    print(f"  health: grade {h.get('health_grade')} ({h.get('health_score')}) · drift score {d.get('score')} ({d.get('compliance')})")
    print(f"  assemblies: {len(b.get('assemblies', []))} · PR impacts: {len(b.get('pr_impact', {}))} · components: {len(b.get('components', {}))}")
    print(f"wrote {a.data}/mcm-remote.json and knowledge-remote.json")
    return 0


def cmd_rules(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    if a.action == "list":
        arch = infer(repo)
        for r in arch["rules"]:
            print(f"  {r['id']:<18} {r.get('status','inferred'):<10} {r.get('origin','inferred'):<9} {r['kind']:<16} {r['title']}")
        rf = arch.get("rules_file", {})
        print(f"{len(arch['rules'])} rules; {rf.get('count', 0)} entries in {rf.get('path')}")
        return 0
    exported = json.loads(Path(a.file).read_text())
    path, added, updated = rules_file.import_export(repo, exported, a.by)
    print(f"{path}: {added} added, {updated} updated — commit it and every PR after that is checked against it")
    return 0


HOOK = """#!/usr/bin/env bash
# Vouch pre-push: check this branch against the intended architecture before it becomes a PR.
# Installed by `python3 -m vouch hook install`. Remove this file to uninstall.
set -u
VOUCH_DIR="{vouch_dir}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] && exit 0
TICKET=""
[ -f ".vouch/ticket.json" ] && TICKET="--ticket .vouch/ticket.json"
PYTHONPATH="$VOUCH_DIR" python3 -m vouch review "$(git rev-parse --show-toplevel)" --base main --head "$BRANCH" $TICKET --markdown
STATUS=$?
if [ $STATUS -eq 3 ]; then
  echo "vouch: this branch violates the intended architecture. Push anyway with --no-verify." >&2
  exit 1
fi
exit 0
"""


def cmd_hook(a: argparse.Namespace) -> int:
    repo = Path(a.repo).resolve()
    hooks = repo / ".git" / "hooks"
    if not hooks.exists():
        print(f"{repo} is not a git repository", file=sys.stderr)
        return 1
    target = hooks / "pre-push"
    target.write_text(HOOK.format(vouch_dir=HERE.parent))
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    print(f"installed {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vouch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("infer", help="build the architecture model (expensive layer)")
    s.add_argument("repo")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_infer)

    s = sub.add_parser("review", help="check one branch / PR (cheap layer)")
    s.add_argument("repo")
    s.add_argument("--base", default="main")
    s.add_argument("--head")
    s.add_argument("--ticket")
    s.add_argument("--arch")
    s.add_argument("--out")
    s.add_argument("--markdown", action="store_true")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("comment", help="render review.json as the PR comment")
    s.add_argument("review")
    s.set_defaults(fn=cmd_comment)

    s = sub.add_parser("demo", help="review every demo branch and write the UI bundle")
    s.add_argument("--repo", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--scenarios")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("onboard", help="clone any repository, infer its architecture, review its branches")
    s.add_argument("url")
    s.add_argument("--out", required=True)
    s.add_argument("--workdir", default=".vouch-repos")
    s.add_argument("--max-branches", type=int, default=10)
    s.add_argument("--base")
    s.add_argument("--tickets")
    s.set_defaults(fn=cmd_onboard)

    s = sub.add_parser("mcm", help="the real Meta-Cognitive Model, from the LegacyLeap MCP server")
    s.add_argument("action", choices=["fetch", "projects"])
    s.add_argument("--project")
    s.add_argument("--data")
    s.set_defaults(fn=cmd_mcm)

    s = sub.add_parser("rules", help="architect rules in .vouch/rules.json")
    s.add_argument("action", choices=["list", "import"])
    s.add_argument("repo")
    s.add_argument("file", nargs="?")
    s.add_argument("--by")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("hook", help="git integration")
    s.add_argument("action", choices=["install"])
    s.add_argument("repo")
    s.set_defaults(fn=cmd_hook)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
