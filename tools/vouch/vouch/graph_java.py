"""Java (Maven multi-module, Spring Boot) scanner: the same ImportGraph shape as the Python one.

No compiler and no parser generator: ``package``/``import`` lines give the graph, a brace-matching
pass gives method bodies for duplicate detection, and the string literals ``"ts-xxx-service"``
give the cross-service REST call graph that Java imports cannot show (services are separate
Maven modules that only share ``ts-common``).
"""

from __future__ import annotations

import re
from pathlib import Path

from .graph import FunctionInfo, ModuleInfo

LAYER_SEGMENTS = {
    "controller": "controller", "web": "controller", "rest": "controller", "resource": "controller", "api": "controller",
    "service": "service", "impl": "service", "serivce": "service", "client": "client",
    "repository": "repository", "dao": "repository", "entity": "entity", "domain": "entity", "model": "entity",
    "dto": "dto", "config": "config", "configuration": "config", "init": "init", "mq": "mq", "async": "mq",
    "messaging": "mq", "util": "util", "utils": "util", "exception": "util", "converter": "util",
    "security": "config", "jwt": "config", "constant": "util", "gateway": "config",
}
JAVA_KEYWORDS = frozenset(
    "abstract assert boolean break byte case catch char class const continue default do double else enum "
    "extends final finally float for goto if implements import instanceof int interface long native new "
    "package private protected public return short static strictfp super switch synchronized this throw "
    "throws transient try void volatile while var record true false null String List Map Set Integer Long "
    "Boolean Object Response ResponseEntity HttpHeaders HttpEntity HttpMethod LOGGER info error warn debug "
    "get set add put size isEmpty equals toString getBody getStatus getMsg getData setStatus setMsg setData "
    "exchange ok new".split()
)
_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_$][\w$]*|\d[\w.]*|\S')
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+)(\.\*)?\s*;", re.M)
_TYPE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
_METHOD = re.compile(
    r"(?:public|protected|private|static|final|synchronized|abstract|default|\s)+"
    r"[\w<>\[\],.? ]+?\s+([A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*(?:throws\s+[\w., ]+)?\s*\{"
)
_SERVICE_NAME = re.compile(r'"(ts-[a-z0-9-]+-service)"')
_STR_LITERAL = re.compile(r'"([A-Za-z0-9_./:@-]{2,80})"')
_ANNOTATION = re.compile(r"@(RestController|Controller|Service|Repository|Component|Configuration|RabbitListener)\b")
_IMPLEMENTS = re.compile(r"\b(?:class|enum|record)\s+[A-Za-z_$][\w$]*(?:\s*<[^>]*>)?(?:\s+extends\s+([\w.<>, ]+?))?(?:\s+implements\s+([\w.<>, ]+?))?\s*\{")
_REF = re.compile(r"\b([A-Z][A-Za-z0-9_$]*)\b")


def strip_comments(src: str) -> str:
    """Remove // and /* */ comments without touching string literals ("http://" is not a comment)."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            block = src[i:] if j < 0 else src[i:j + 2]
            out.append("\n" * block.count("\n"))
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def normalise_tokens(body: str) -> tuple[str, ...]:
    out: list[str] = []
    for tok in _TOKEN.findall(body):
        if tok[0] in "\"'":
            out.append("STR")
        elif tok[0].isdigit():
            out.append("NUM")
        elif tok[0].isalpha() or tok[0] in "_$":
            out.append(tok if tok in JAVA_KEYWORDS else "NAME")
        else:
            out.append(tok)
    return tuple(out)


def _method_bodies(src: str) -> list[tuple[str, int, int, str]]:
    """(name, start_line, end_line, body) for each method found by brace matching."""
    out = []
    for m in _METHOD.finditer(src):
        name = m.group(1)
        if name in ("if", "for", "while", "switch", "catch", "synchronized", "return", "new", "else"):
            continue
        start = m.end() - 1  # the '{'
        depth = 0
        i = start
        while i < len(src):
            c = src[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            elif c == '"':
                j = i + 1
                while j < len(src) and src[j] != '"':
                    j += 2 if src[j] == "\\" else 1
                i = j
            i += 1
        body = src[start + 1 : i]
        line = src.count("\n", 0, m.start()) + 1
        end = src.count("\n", 0, i) + 1
        out.append((name, line, end, body))
    return out


def context_of(rel_path: str) -> str:
    return rel_path.split("/", 1)[0]


def layer_of(rel_path: str) -> str | None:
    if "/src/main/java/" not in rel_path:
        return None
    below = rel_path.split("/src/main/java/", 1)[1].split("/")[:-1]
    for seg in below:
        if seg in LAYER_SEGMENTS:
            return LAYER_SEGMENTS[seg]
    return None


def parse_java(rel_path: str, source: str) -> ModuleInfo | None:
    pkg_m = _PACKAGE.search(source)
    cls_names = [m.group(2) for m in _TYPE.finditer(strip_comments(source))]
    if not pkg_m:
        return None
    cls = Path(rel_path).stem
    module = f"{pkg_m.group(1)}.{cls}"
    info = ModuleInfo(name=module, path=rel_path)
    clean = strip_comments(source)
    for m in _IMPORT.finditer(source):
        target, wildcard = m.group(2), bool(m.group(3))
        if target.startswith(("java.", "javax.", "jakarta.", "org.", "com.fasterxml", "lombok", "io.", "net.")):
            continue
        line = source.count("\n", 0, m.start()) + 1
        info.imports.add(target + (".*" if wildcard else ""))
        info.import_lines.setdefault(target + (".*" if wildcard else ""), line)
        info.imported_symbols.setdefault(target + (".*" if wildcard else ""), set()).add(
            "*" if wildcard else target.rsplit(".", 1)[-1])
    info.classes = cls_names
    for name, line, end, body in _method_bodies(clean):
        toks = normalise_tokens(body)
        if len(toks) < 4:
            continue
        info.functions.append(FunctionInfo(module=module, qualname=f"{cls}.{name}", path=rel_path,
                                           line=line, end_line=end, tokens=toks))
    info.context = context_of(rel_path)
    info.layer = layer_of(rel_path)
    info.service_calls = set(_SERVICE_NAME.findall(clean)) - {info.context}
    info.str_literals = set(_STR_LITERAL.findall(clean))
    info.uses_rest = "RestTemplate" in clean or "restTemplate" in clean
    info.uses_mq = "RabbitTemplate" in clean or "rabbitTemplate" in clean or "@RabbitListener" in clean \
        or "RabbitSend" in clean
    info.annotations = set(_ANNOTATION.findall(clean))
    info.refs = set(_REF.findall(clean)) - {cls}
    im = _IMPLEMENTS.search(clean)
    if im:
        supers = []
        for grp in (im.group(1), im.group(2)):
            if grp:
                supers += [re.sub(r"<.*", "", x.strip()).rsplit(".", 1)[-1] for x in grp.split(",")]
        info.implements = {x for x in supers if x}
    return info


def service_names(repo: Path) -> dict[str, str]:
    """logical service name → module directory, from spring.application.name or the directory itself."""
    out: dict[str, str] = {}
    for d in sorted(p for p in repo.iterdir() if p.is_dir() and (p / "src" / "main" / "java").exists()):
        out[d.name] = d.name
        for yml in ("bootstrap.yml", "application.yml", "bootstrap.yaml", "application.yaml", "application.properties"):
            f = d / "src" / "main" / "resources" / yml
            if f.exists():
                txt = f.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"spring\.application\.name\s*[=:]\s*([\w.-]+)", txt) or re.search(
                    r"application:\s*\n\s+name:\s*([\w.-]+)", txt)
                if m:
                    out[m.group(1)] = d.name
    return out


def scan(repo: Path) -> tuple[dict[str, ModuleInfo], dict[str, ModuleInfo]]:
    modules: dict[str, ModuleInfo] = {}
    tests: dict[str, ModuleInfo] = {}
    names = service_names(repo)
    for file in sorted(repo.rglob("*.java")):
        rel = file.relative_to(repo).as_posix()
        if "/target/" in rel or rel.startswith((".", "node_modules")):
            continue
        if "/src/main/java/" not in rel and "/src/test/java/" not in rel:
            continue
        info = parse_java(rel, file.read_text(encoding="utf-8", errors="replace"))
        if not info:
            continue
        resolve_service_calls(info, names)
        (tests if "/src/test/java/" in rel else modules)[info.name] = info
    return modules, tests


def resolve_service_calls(info: ModuleInfo, names: dict[str, str]) -> None:
    """Turn string literals that name another service (Feign name, REST host, lb:// uri) into call edges."""
    found: set[str] = set()
    for lit in info.str_literals:
        core = re.sub(r"^(https?://|lb://)", "", lit).split("/")[0].split(":")[0]
        if core in names and names[core] != info.context:
            found.add(names[core])
    info.service_calls = (info.service_calls | found) - {info.context}


def resolve_java(modules: dict[str, ModuleInfo], target: str) -> list[str]:
    """``a.b.C`` → [a.b.C]; ``a.b.*`` → every module in package a.b."""
    if target.endswith(".*"):
        pkg = target[:-2]
        return [m for m in modules if m.rpartition(".")[0] == pkg]
    if target in modules:
        return [target]
    head, _, _ = target.rpartition(".")  # static import of a member
    return [head] if head in modules else []
