#!/usr/bin/env python3
"""Find real mistakes in a git repository. Standard library only.

This is the part of The Fool that KNOWS things. It reads files and git history
and never runs any code from the repository. Every finding carries the exact
file, line, time and quoted text, so later steps can check any claim.

  python scan.py path/to/repo
  python scan.py path/to/repo --json
  python scan.py path/to/repo --now 2026-10-28     # fix "today" for reproducible results

Secrets are masked in the evidence (first 4 characters only).
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

SEVERITY = {
    "secret_in_tree": 5,
    "secret_in_history_removed": 5,
    "sensitive_file_tracked": 4,
    "syntax_error": 4,
    "old_todo": 2,
    "debug_leftover": 2,
    "bare_except": 2,
    "stub_function": 3,
    "empty_file": 2,
    "debug_print": 2,
    "uncommitted_change": 2,
    "unused_import": 1,
    "late_night_commit": 1,
}

SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("private_key", re.compile(r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")),
    ("generic_secret", re.compile(
        r"""(?i)[\w\-]*(?:api[_-]?key|secret|token|passwd|password)[\w\-]*["']?\s*[:=]\s*["']([A-Za-z0-9_\-/+=]{16,})["']""")),
]
PLACEHOLDER_WORDS = ("example", "your", "placeholder", "changeme", "xxxx", "dummy", "sample", "test")
SENSITIVE_FILE = re.compile(r"(^|/)(\.env(\.[\w-]+)?|id_rsa|id_ed25519|[^/]+\.pem|[^/]+\.p12)$")
# Test fixtures often hold deliberate fake keys and certificates. Not a mistake worth haunting anyone for.
FIXTURE_PATH = re.compile(r"(^|/)(tests?|testdata|fixtures?|examples?)/", re.I)
# Generated or machine-written files: changes there say nothing about the author.
GENERATED = re.compile(
    r"(^|/)(__pycache__|node_modules|\.venv|venv|dist|build|\.idea|\.vscode|\.pytest_cache)/"
    r"|\.(pyc|pyo|db|sqlite3?|log|pkl|npy)$|(^|/)\.DS_Store$", re.I)
SAFE_ENV = re.compile(r"\.env\.(example|sample|template|dist)$")
TODO = re.compile(r"\b(?:TODO|FIXME)\b")
BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*(#.*)?$")
PY_DEBUG = re.compile(r"^\s*(breakpoint\(\)|(import pdb;\s*)?pdb\.set_trace\(\))\s*;?\s*(#.*)?$")
JS_DEBUG = re.compile(r"^\s*debugger;?\s*$")
# print("here") / console.log("test") style leftovers: a trivial literal and nothing else
DEBUG_PRINT = re.compile(
    r"""^\s*(?:print\(|console\.log\()\s*(["'])(?:here|test|testing|hi|hello|debug|asdf|wtf|ok|works?|\d+)\1\s*\)?;?\s*$""", re.I)
EMPTY_OK = {"__init__.py", ".gitkeep", ".keep", ".nojekyll", "py.typed"}
MAX_FILE_BYTES = 300_000
OLD_TODO_DAYS = 14
STALE_UNCOMMITTED_DAYS = 3


def git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.stdout if p.returncode == 0 else ""


def mask(value):
    return value[:4] + "…" if len(value) > 4 else "…"


def secret_hits(line):
    """Yield (kind, secret_value) for secrets found on a line."""
    for kind, pat in SECRET_PATTERNS:
        for m in pat.finditer(line):
            value = m.group(1)
            if kind == "generic_secret":
                low = value.lower()
                if any(w in low for w in PLACEHOLDER_WORDS) or len(set(value)) < 6:
                    continue
            yield kind, value


def masked_line(line, value):
    return line.replace(value, mask(value)).strip()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def blame_time(repo, path, lineno):
    """Epoch seconds of the commit that last touched this line, or None if uncommitted."""
    out = git(repo, "blame", "-L", f"{lineno},{lineno}", "--porcelain", "--", path)
    if not out:
        return None
    first = out.split("\n", 1)[0].split()[0]
    if set(first) == {"0"}:
        return None
    m = re.search(r"^author-time (\d+)$", out, re.M)
    return int(m.group(1)) if m else None


def tree_files(repo):
    out = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [p for p in out.split("\0") if p]


def read_text(repo, rel):
    path = os.path.join(repo, rel)
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
        if b"\0" in raw:
            return None
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


def finding(kind, file, line, evidence, when=None, detail=""):
    return {"id": f"{kind}:{file}:{line or 0}", "kind": kind, "severity": SEVERITY[kind],
            "file": file, "line": line, "evidence": evidence, "when": when, "detail": detail}


def scan(repo, now=None):
    findings = []
    files = tree_files(repo)
    head_ts = git(repo, "log", "-1", "--format=%at").strip()
    ref_now = now if now is not None else (int(head_ts) if head_ts else 0)
    stale_now = now if now is not None else time.time()
    current_secrets = set()

    for rel in files:
        if rel.startswith(".git/") or GENERATED.search(rel):
            continue
        text = read_text(repo, rel)
        if text is None:
            continue
        lines = text.split("\n")
        fixture = bool(FIXTURE_PATH.search(rel))
        is_py = rel.endswith(".py")
        is_js = rel.endswith((".js", ".ts", ".jsx", ".tsx"))

        for i, line in enumerate(lines, 1):
            for kind, value in secret_hits(line):
                current_secrets.add(value)
                if fixture:
                    continue
                ts = blame_time(repo, rel, i)
                findings.append(finding(
                    "secret_in_tree", rel, i, masked_line(line, value),
                    iso(ts) if ts else "uncommitted", f"looks like a {kind.replace('_', ' ')}"))
            if TODO.search(line):
                ts = blame_time(repo, rel, i)
                if ts and (ref_now - ts) / 86400 >= OLD_TODO_DAYS:
                    days = int((ref_now - ts) / 86400)
                    findings.append(finding("old_todo", rel, i, line.strip(), iso(ts),
                                            f"{days} days old"))
            if is_py and BARE_EXCEPT.match(line):
                findings.append(finding("bare_except", rel, i, line.strip()))
            if (is_py and PY_DEBUG.match(line)) or (is_js and JS_DEBUG.match(line)):
                findings.append(finding("debug_leftover", rel, i, line.strip()))
            elif (is_py or is_js) and not fixture and DEBUG_PRINT.match(line):
                findings.append(finding("debug_print", rel, i, line.strip()))

        if text == "" and os.path.basename(rel) not in EMPTY_OK:
            ts = git(repo, "log", "-1", "--format=%at", "--", rel).strip()
            findings.append(finding("empty_file", rel, None, rel, iso(int(ts)) if ts else "uncommitted",
                                    "created and never filled in"))

        if is_py:
            try:
                tree = ast.parse(text, filename=rel)
                if not fixture:
                    findings += ast_findings(rel, tree, lines)
            except SyntaxError as e:
                ln = e.lineno or 1
                quoted = (lines[ln - 1].strip() if 0 < ln <= len(lines) else "")
                findings.append(finding("syntax_error", rel, ln, quoted, None, str(e.msg)))
            except ValueError:
                pass

    for rel in git(repo, "ls-files", "-z").split("\0"):
        if rel and SENSITIVE_FILE.search(rel) and not SAFE_ENV.search(rel) and not FIXTURE_PATH.search(rel):
            findings.append(finding("sensitive_file_tracked", rel, None, rel))

    findings += history_secrets(repo, current_secrets)

    for row in git(repo, "status", "--porcelain").splitlines():
        code, path = row[:2], row[3:]
        if GENERATED.search(path):
            continue
        if "M" in code or "D" in code:
            try:
                ts = int(os.path.getmtime(os.path.join(repo, path)))
            except OSError:
                continue
            days = int((stale_now - ts) / 86400)
            if days >= STALE_UNCOMMITTED_DAYS:  # a file touched an hour ago is just work in progress
                findings.append(finding("uncommitted_change", path, None, path, iso(ts),
                                        f"changed {days} days ago, never committed"))

    seen = 0
    for row in git(repo, "log", "--format=%H%x09%at%x09%ai%x09%s").splitlines():
        h, at, ai, subj = (row.split("\t") + ["", "", "", ""])[:4]
        hour = int(ai[11:13]) if len(ai) >= 13 and ai[11:13].isdigit() else 12
        if hour < 5:
            findings.append(finding("late_night_commit", h[:7], None, subj, ai[:16], f"committed at {ai[11:16]}"))
            seen += 1
            if seen == 2:
                break

    findings.sort(key=lambda f: (-f["severity"], f["file"], f["line"] or 0))
    return findings


def ast_findings(rel, tree, lines):
    """Unfinished functions and unused imports in a Python file."""
    out = []
    skip_decorators = {"abstractmethod", "overload"}

    def decorator_names(node):
        names = set()
        for d in node.decorator_list:
            target = d.func if isinstance(d, ast.Call) else d
            names.add(getattr(target, "attr", getattr(target, "id", "")))
        return names

    def is_protocol(cls):
        return any(getattr(b, "id", getattr(b, "attr", "")) in ("Protocol", "ABC") for b in cls.bases)

    def stub_body(fn):
        """Only a plain `pass` (after an optional docstring). `...` and NotImplementedError
        are how hooks, protocols and base classes are written on purpose, so they are not flagged."""
        body = list(fn.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        return bool(body) and all(isinstance(st, ast.Pass) for st in body)

    def visit(node, owner_has_bases):
        """owner_has_bases: the enclosing class inherits from something, so an empty method
        is probably an override or a hook rather than unfinished work."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, bool(child.bases) or is_protocol(child))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if (not child.name.startswith("__") and not owner_has_bases
                        and not (decorator_names(child) & skip_decorators) and stub_body(child)):
                    ln = child.lineno
                    out.append(finding("stub_function", rel, ln, lines[ln - 1].strip(), None,
                                       "a function whose whole body is pass"))
                visit(child, False)
            else:
                visit(child, owner_has_bases)

    visit(tree, False)
    source = "\n".join(lines)

    base = os.path.basename(rel)
    if base != "__init__.py" and "compat" not in base and base not in ("types.py", "_types.py"):
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported[(a.asname or a.name).split(".")[0]] = node.lineno
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for a in node.names:
                    if a.name != "*":
                        imported[a.asname or a.name] = node.lineno
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        exported = {c.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "__all__" for t in n.targets)
                    for c in ast.walk(n.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        by_line = {}
        for name, ln in imported.items():
            line = lines[ln - 1]
            elsewhere = len(re.findall(r"\b" + re.escape(name) + r"\b", source)) > 1  # strings and annotations count as use
            if name not in used and name not in exported and not elsewhere and "noqa" not in line:
                by_line.setdefault(ln, []).append(name)
        for ln, names in sorted(by_line.items()):  # one finding per import line, not one per name
            quoted = ", ".join(f"'{n}'" for n in names)
            out.append(finding("unused_import", rel, ln, lines[ln - 1].strip(), None,
                               f"{quoted} {'is' if len(names) == 1 else 'are'} imported and never used"))
    return out


def history_secrets(repo, current_secrets):
    """Secrets added in some commit that are no longer in the working tree."""
    out = git(repo, "log", "--all", "--max-count=500", "-p", "-U0", "--format=@@@%H %at")
    found, commit, ts, path, newline = [], None, None, None, 0
    seen = set()
    for raw in out.split("\n"):
        if raw.startswith("@@@"):
            commit, ts = raw[3:].split()[0], int(raw.split()[1])
        elif raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            newline = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path:
            line = raw[1:]
            if FIXTURE_PATH.search(path):
                newline += 1
                continue
            for kind, value in secret_hits(line):
                if value not in current_secrets and value not in seen:
                    seen.add(value)
                    found.append(finding(
                        "secret_in_history_removed", path, newline, masked_line(line, value),
                        iso(ts), f"added in commit {commit[:7]}, gone from the files, still in history"))
            newline += 1
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--now", help="YYYY-MM-DD to treat as today (default: the latest commit)")
    args = ap.parse_args()
    if not os.path.isdir(os.path.join(args.repo, ".git")):
        sys.exit(f"{args.repo} is not a git repository")
    now = None
    if args.now:
        now = int(datetime.strptime(args.now, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    result = scan(args.repo, now)
    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return
    for f in result:
        where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        print(f"[{f['severity']}] {f['kind']:<26} {where}  {f['evidence']}  ({f['detail']}{', ' + f['when'] if f['when'] else ''})")
    print(f"\n{len(result)} findings")


if __name__ == "__main__":
    main()
