"""Read a pull request as git sees it: which files, which lines, which sources at each ref."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


@dataclass
class FileChange:
    path: str
    status: str  # added | modified | deleted | renamed
    additions: int = 0
    deletions: int = 0
    added_lines: set[int] = field(default_factory=set)  # line numbers in head
    removed_lines: set[int] = field(default_factory=set)  # line numbers in base
    patch: str = ""
    old_path: str | None = None


def git(repo: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        msg = f"git {' '.join(args)} failed: {res.stderr.strip()}"
        raise RuntimeError(msg)
    return res.stdout


def rev(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--short", ref).strip()


def merge_base(repo: Path, base: str, head: str) -> str:
    return git(repo, "merge-base", base, head).strip()


def show(repo: Path, ref: str, path: str) -> str | None:
    res = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def changed_files(repo: Path, base: str, head: str) -> list[FileChange]:
    mb = merge_base(repo, base, head)
    out: dict[str, FileChange] = {}
    for line in git(repo, "diff", "--name-status", "-M", mb, head).splitlines():
        parts = line.split("\t")
        code = parts[0][0]
        if code == "R":
            fc = FileChange(path=parts[2], status="renamed", old_path=parts[1])
        else:
            fc = FileChange(path=parts[1], status={"A": "added", "M": "modified", "D": "deleted"}.get(code, "modified"))
        out[fc.path] = fc
    for line in git(repo, "diff", "--numstat", "-M", mb, head).splitlines():
        a, d, p = line.split("\t")[:3]
        if " => " in p:
            p = re.sub(r".*\{.* => (.*)\}.*|.* => ", r"\1", p) if "{" in p else p.split(" => ")[1]
        if p in out:
            out[p].additions = int(a) if a != "-" else 0
            out[p].deletions = int(d) if d != "-" else 0
    for fc in out.values():
        patch = git(repo, "diff", "-U0", "-M", mb, head, "--", fc.path, check=False)
        fc.patch = patch
        for m in _HUNK.finditer(patch):
            old_start, old_len = int(m.group(1)), int(m.group(2) or "1")
            new_start, new_len = int(m.group(3)), int(m.group(4) or "1")
            fc.removed_lines.update(range(old_start, old_start + old_len))
            fc.added_lines.update(range(new_start, new_start + new_len))
    return sorted(out.values(), key=lambda f: f.path)


def snippet(patch: str, around_line: int | None = None, max_lines: int = 14) -> str:
    """The hunk containing ``around_line`` (head numbering), trimmed for display."""
    hunks = re.split(r"(?m)^(?=@@ )", patch)
    body = [h for h in hunks if h.startswith("@@")]
    chosen = None
    if around_line is not None:
        for h in body:
            m = _HUNK.match(h)
            if not m:
                continue
            start, ln = int(m.group(3)), int(m.group(4) or "1")
            if start <= around_line < start + max(ln, 1):
                chosen = h
                break
    chosen = chosen or (body[0] if body else "")
    lines = chosen.splitlines()
    return "\n".join(lines[:max_lines]) + ("\n…" if len(lines) > max_lines else "")
