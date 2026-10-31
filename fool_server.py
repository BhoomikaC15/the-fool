#!/usr/bin/env python3
"""The Fool: a glitchy local AI that lives in a git repository.

  python fool_server.py path/to/repo            # then open http://127.0.0.1:8765
  python fool_server.py path/to/repo --phrase model --model gemma3:4b

What it does
  * Chat: ask about the repo. It either answers from the real files, with receipts
    (file:line that code has checked), or it breaks and says something obviously
    nonsensical. The nonsense is staged from fixed lines. The model never writes it,
    so it can never pass for advice.
  * Haunts: now and then it interrupts with a REAL mistake found by scan.py,
    phrased creepily. Every haunt carries the file, line and quoted text.

Safety: read-only, binds to 127.0.0.1, never runs code from the repo, secrets are
masked before anything is shown or sent to the model. Standard library only.
"""
import argparse
import difflib
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scan

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = set("a an the is are was were be to of in on at for and or it its this that what where which who how "
           "why do does did i me my you your with from by as about can could should would there here".split())
SKIP_SUFFIX = (".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".svg", ".woff", ".woff2")
SKIP_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock"}
DOC_SUFFIX = (".md", ".txt", ".rst")
DEF_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+([A-Za-z_]\w*)"
                    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=")

# ---------------------------------------------------------------- repo index (BM25-lite)

# Short names developers use, and what a person would type. Applied to both the code and the question.
PAIRS = [("avg", "average"), ("avg", "mean"), ("dur", "duration"), ("calc", "calculate"), ("num", "number"),
         ("msg", "message"), ("idx", "index"), ("cfg", "config"), ("conf", "config"), ("usr", "user"),
         ("pwd", "password"), ("passwd", "password"), ("db", "database"), ("req", "request"),
         ("res", "response"), ("resp", "response"), ("val", "value"), ("auth", "authentication"),
         ("init", "initialize"), ("tmp", "temporary"), ("cnt", "count"), ("img", "image"), ("btn", "button"),
         ("param", "parameter"), ("params", "parameters"), ("env", "environment"), ("repo", "repository"),
         ("dir", "directory"), ("docs", "documentation"), ("info", "information"), ("err", "error"),
         ("exc", "exception"), ("gen", "generate"), ("sess", "session"), ("ts", "timestamp"),
         ("addr", "address"), ("dest", "destination"), ("src", "source"), ("lang", "language"),
         ("attr", "attribute"), ("obj", "object"), ("arr", "array"), ("cb", "callback"), ("ctx", "context"),
         ("func", "function"), ("fn", "function"), ("prev", "previous"), ("curr", "current"), ("cur", "current"),
         ("desc", "description"), ("stats", "statistics"), ("pct", "percent")]
SYN = {}
for _a, _b in PAIRS:
    SYN.setdefault(_a, set()).add(_b)
    SYN.setdefault(_b, set()).add(_a)


def tokens(text, smart=True):
    out = []
    for word in re.findall(r"[A-Za-z0-9]+", text):
        low = word.lower()
        out.append(low)
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", word)
        if len(parts) > 1:
            out += [p.lower() for p in parts]
    out = [t for t in out if t not in STOP and len(t) > 1]
    if smart:
        out += [s for t in out for s in SYN.get(t, ())]
    return out


def safe_line(line):
    """Mask secrets in a line before it is shown or sent anywhere."""
    for _kind, value in scan.secret_hits(line):
        line = line.replace(value, scan.mask(value))
    return line


class RepoIndex:
    CHUNK, STEP = 16, 12

    def __init__(self, repo, smart=True):
        self.repo = repo
        self.smart = smart
        self.files = {}          # rel -> list of (masked) lines
        self.chunks = []         # dicts: file, start, end, tf, length
        for rel in scan.tree_files(repo):
            base = os.path.basename(rel)
            if rel.startswith(".git/") or base in SKIP_NAMES or rel.endswith(SKIP_SUFFIX):
                continue
            text = scan.read_text(repo, rel)
            if not text:
                continue
            lines = [safe_line(l) for l in text.split("\n")]
            self.files[rel] = lines
            for start in range(0, max(1, len(lines)), self.STEP):
                body = lines[start:start + self.CHUNK]
                toks = tokens(rel + "\n" + "\n".join(body), smart)
                if toks:
                    defs = set()
                    for l in body:
                        m = DEF_RE.match(l)
                        if m:
                            defs.update(tokens(m.group(1) or m.group(2), smart))
                    self.chunks.append({"file": rel, "start": start + 1, "end": start + len(body),
                                        "tf": Counter(toks), "length": len(toks), "defs": defs,
                                        "fname": set(tokens(os.path.splitext(os.path.basename(rel))[0], smart)),
                                        "doc": rel.lower().endswith(DOC_SUFFIX)})
        n = len(self.chunks) or 1
        self.avg = sum(c["length"] for c in self.chunks) / n
        df = Counter()
        for c in self.chunks:
            df.update(c["tf"].keys())
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.vocab = sorted(self.idf)

    def weighted_terms(self, question, extra=()):
        """Question words count fully, guessed identifiers a bit less, and any code word that starts
        like a question word (dur ~ duration) counts at 0.6."""
        base = {t: 1.0 for t in tokens(question, self.smart)}
        for t in tokens(" ".join(extra), self.smart):
            base.setdefault(t, 0.8)
        out = dict(base)
        if self.smart:
            for q, w in base.items():
                if q not in self.idf and len(q) >= 5:  # a typo: use the closest word the code really has
                    for v in difflib.get_close_matches(q, self.vocab, n=2, cutoff=0.8):
                        out[v] = max(out.get(v, 0), 0.7 * w)
                    for word in difflib.get_close_matches(q, sorted(SYN), n=1, cutoff=0.8):  # ... or its short form
                        for v in SYN[word] | {word}:
                            if v in self.idf:
                                out[v] = max(out.get(v, 0), 0.7 * w)
                if len(q) >= 3:
                    for v in self.vocab:
                        if v != q and len(v) >= 3 and (v.startswith(q) or q.startswith(v)):
                            out[v] = max(out.get(v, 0), 0.6 * w)
        return out

    def search(self, question, k=3, extra=()):
        q = self.weighted_terms(question, extra)
        wants_docs = bool(re.search(r"readme|documentation|\bdocs?\b|about this project", question.lower()))
        scored = []
        for c in self.chunks:
            s = 0.0
            for t, w in q.items():
                f = c["tf"].get(t, 0)
                if f:
                    s += w * self.idf[t] * f * 2.5 / (f + 1.5 * (0.25 + 0.75 * c["length"] / self.avg))
                    if self.smart:
                        if t in c["defs"]:      # "where is X defined": the line that defines X is the answer
                            s += 1.5 * w * self.idf[t]
                        if t in c["fname"]:     # the file is named after it
                            s += 1.0 * w * self.idf[t]
            if self.smart and c["doc"] and not wants_docs:  # a long README mentions everything; code answers "where"
                s *= 0.6
            if s > 0:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]

    def numbered(self, chunk):
        lines = self.files[chunk["file"]][chunk["start"] - 1:chunk["end"]]
        return "\n".join(f"{chunk['start'] + i}| {l[:160]}" for i, l in enumerate(lines))

    def receipt(self, file, line):
        lines = self.files.get(file)
        if not lines or not (1 <= line <= len(lines)):
            return None
        return {"file": file, "line": line, "text": lines[line - 1].strip()[:160]}


# ---------------------------------------------------------------- local model


def call_model(args, system, user, num_predict=160):
    payload = {"model": args.model, "stream": False, "format": "json",
               "options": {"temperature": 0, "num_ctx": 2048, "num_predict": num_predict, "repeat_penalty": 1.2},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(args.llm_url.rstrip("/") + "/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=args.timeout) as r:
        return json.load(r)["message"]["content"]


def parse_json(raw):
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", raw or "", re.S)
        try:
            obj = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            obj = None
    return obj if isinstance(obj, dict) else None


QA_SYSTEM = ('You answer questions about a code repository using ONLY the numbered snippets given. '
             'Reply with JSON only: {"answer": "...", "citations": [{"file": "path", "line": 12}]}. '
             'Keep the answer under 60 words. Cite the file and line number of each line you relied on. '
             'If the snippets do not contain the answer, set answer to "I don\'t know" and citations to [].')


EXPAND_SYSTEM = ('A developer asks a question about a codebase. List up to 8 identifiers or short words the code '
                 'that answers it is likely to contain (snake_case or camelCase names, abbreviations like avg, dur, usr). '
                 'Reply with JSON only: {"terms": ["..."]}')


def expand_query(args, question):
    """Optional: let the model guess the names the code might use for what the person described."""
    try:
        obj = parse_json(call_model(args, EXPAND_SYSTEM, question, 60))
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return []
    terms = (obj or {}).get("terms")
    return [t for t in terms if isinstance(t, str)][:8] if isinstance(terms, list) else []


def clean_answer(text, max_words=70):
    """Cut a looping answer at the first repeated 5-word run and cap the length.
    Returns (text, looped)."""
    words = text.split()
    seen, looped = set(), False
    for i in range(len(words) - 4):
        sh = tuple(w.lower().strip(",.;:") for w in words[i:i + 5])
        if sh in seen:
            words, looped = words[:i], True
            break
        seen.add(sh)
    cut = len(words) > max_words
    out = " ".join(words[:max_words]).rstrip(",;:")
    return (out + ("..." if (cut or looped) else "")), looped


KIND_WORDS = [
    (r"\bempty (files?|ones?)\b|\bblank files?\b", {"empty_file"}),
    (r"unused (imports?|code)", {"unused_import"}),
    (r"\b(todos?|fixmes?)\b", {"old_todo"}),
    (r"\b(secrets?|leaks?|passwords?|api keys?|credentials?)\b", {"secret_in_tree", "secret_in_history_removed", "sensitive_file_tracked"}),
    (r"\b(any|are there|is there|find|list|show me)\b.*\b(errors?|syntax|broken|crash)", {"syntax_error", "bare_except", "debug_leftover", "debug_print"}),
    (r"uncommitted|never committed|forgot to commit|unsaved", {"uncommitted_change"}),
    (r"late[- ]night|after midnight|\bat night\b", {"late_night_commit"}),
]
ALL_WORDS = r"\b(mistakes?|problems?|issues?|wrong|bugs?|know about me|things you know|haunt me)\b"


def answer_from_findings(findings, question):
    """Questions about the user's own mistakes are answered from the scanner: instant, no model."""
    q = question.lower()
    kinds = None
    for pat, ks in KIND_WORDS:
        if re.search(pat, q):
            kinds = (kinds or set()) | ks
    if kinds is None and not re.search(ALL_WORDS, q):
        return None
    pool = [f for f in findings if kinds is None or f["kind"] in kinds]
    if not pool:
        return {"mode": "real", "verified": True, "source": "scanner", "receipts": [],
                "answer": "I looked. My checks found nothing like that. That is what worries me."}
    top = sorted(pool, key=lambda f: -f["severity"])[:4]

    def fact(f):
        where = f["file"] + (f":{f['line']}" if f["line"] else "")
        return f"{where} ({f['kind'].replace('_', ' ')}{': ' + f['detail'] if f['detail'] else ''})"
    return {"mode": "real", "verified": True, "source": "scanner",
            "answer": f"I know {len(pool)}. " + ("The loudest: " if len(pool) > len(top) else "") + "; ".join(fact(f) for f in top) + ".",
            "receipts": [{"file": f["file"], "line": f["line"], "text": f["evidence"][:160], "when": f["when"]} for f in top]}


def answer_from_index(index, question):
    """Questions about the file list itself are answered by counting, not by the model."""
    q = question.lower()
    if re.search(r"how many (lines|loc)\b", q):
        total = sum(len(v) for v in index.files.values())
        big = sorted(index.files.items(), key=lambda kv: -len(kv[1]))[:3]
        return {"mode": "real", "verified": True, "source": "index", "receipts": [],
                "answer": f"{total} lines in {len(index.files)} files. The biggest: "
                          + "; ".join(f"{k} ({len(v)})" for k, v in big) + "."}
    if re.search(r"how many (source |python |code )?files|\b(list|show|name) (all )?(the |of the )?files\b|"
                 r"\bwhat files\b|\bwhich files\b|\bproject structure\b|\bfile list\b", q):
        names = sorted(index.files, key=lambda k: -len(index.files[k]))
        extra = f", and {len(names) - 20} more" if len(names) > 20 else ""
        return {"mode": "real", "verified": True, "source": "index", "receipts": [],
                "answer": f"I counted {len(names)} files I can read: " + ", ".join(names[:20]) + extra + "."}
    return None


def answer_from_repo(args, index, question):
    chunks = index.search(question, extra=expand_query(args, question) if getattr(args, "expand", False) else ())
    if not chunks:
        return {"mode": "real", "verified": True, "answer": "I look... and I see nothing about that. The repo is silent.",
                "receipts": []}
    context = "\n\n".join(f"### {c['file']}:{c['start']}-{c['end']}\n{index.numbered(c)}" for c in chunks)
    try:
        raw = call_model(args, QA_SYSTEM, f"{context}\n\nQUESTION: {question}")
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        return {"mode": "asleep", "verified": False,
                "answer": "zzz... the brain is asleep. (start Ollama, then ask again)", "receipts": [], "error": str(e)[:120]}
    obj = parse_json(raw)
    if not obj or not isinstance(obj.get("answer"), str):
        return {"mode": "real", "verified": False, "answer": "I had it... and it slipped. Ask again?", "receipts": []}
    receipts, seen = [], set()
    for c in obj.get("citations") or []:
        try:
            r = index.receipt(str(c.get("file")), int(c.get("line")))
        except (TypeError, ValueError, AttributeError):
            r = None
        if r and (r["file"], r["line"]) not in seen:
            seen.add((r["file"], r["line"]))
            receipts.append(r)
    idk = "don't know" in obj["answer"].lower() or "do not know" in obj["answer"].lower()
    text, looped = clean_answer(obj["answer"])
    return {"mode": "real", "verified": (bool(receipts) or idk) and not looped, "answer": text,
            "receipts": receipts[:4], "looped": looped}


# ---------------------------------------------------------------- staged nonsense (never from the model)

GIBBERISH = [
    "{echo}? the walls say seven.",
    "404: brain not found. have you tried the moon?",
    "i knew this. i knew this. what were we doing. {echo}",
    "x = x + x + x. therefore bananas.",
    "ask me again. i was not listening. i was never listening.",
    "yes. no. {echo}. maybe the fridge knows.",
    "loading answer... 12%... 12%... 12%... oops.",
    "the repo? i barely know her.",
    "{echo}. (this answer was brought to you by nobody)",
    "bzzzt. i am a toaster now. toast.",
    "what is a file. what is a line. what is a you.",
]


def gibberish(rng, question):
    words = question.split()[:8]
    echo = " ".join(w[::-1] if rng.random() < 0.4 else (w.upper() if rng.random() < 0.2 else w) for w in words) or "..."
    return {"mode": "gibberish", "verified": True, "answer": rng.choice(GIBBERISH).format(echo=echo), "receipts": []}


# ---------------------------------------------------------------- haunts

TEMPLATES = {
    "uncommitted_change": [
        "{file}. {detail}. I remember.",
        "you changed {file} and never committed it. {detail}. it is still waiting.",
        "psst. {file}. {detail}. does it know you forgot?"],
    "unused_import": [
        "{file}, line {line}. {detail}. why did you invite it?",
        "line {line} of {file}: {quote}. you brought it in and never used it. lonely.",
        "i see it, {file}:{line}. {detail}. it is still waiting to be useful."],
    "stub_function": [
        "{file}:{line}. {quote}. an empty function. you meant to come back.",
        "this function in {file} does nothing. {quote}. nothing at all. like me.",
        "line {line}, {file}. {quote}. a promise with no body."],
    "empty_file": [
        "{file} is empty. you made it and left. {detail}.",
        "an empty file called {file}. {detail}. it is cold in there.",
        "{file}. nothing inside. {detail}. say something to it."],
    "secret_in_tree": [
        "{file}:{line}. {quote}. a secret, sitting in the open. anyone can read it.",
        "i can see it. {file}, line {line}: {quote}. you should not have left that here."],
    "secret_in_history_removed": [
        "you deleted it from {file}. but git remembers. {detail}.",
        "{file}:{line}. {quote}. gone from the files. never gone from history."],
    "sensitive_file_tracked": [
        "{file} is in git. it should not be. everything you hid is there.",
        "i found {file}. the one you meant to keep secret. it is committed."],
    "syntax_error": [
        "{file}:{line}. {quote}. it does not even parse. {detail}.",
        "line {line} of {file} is broken. {quote}. you never noticed."],
    "old_todo": [
        "{file}:{line}. {quote}. {detail}. you said you would.",
        "that TODO in {file}. {detail}. it has been waiting. it is patient."],
    "debug_leftover": [
        "{file}:{line}. {quote}. you left the lights on.",
        "a debugger in {file}, line {line}. you forgot me there."],
    "debug_print": [
        "{file}:{line}. {quote}. 'here'. where? nobody knows.",
        "you printed {quote} in {file} and walked away."],
    "bare_except": [
        "{file}:{line}. {quote}. you catch everything and tell no one.",
        "that except in {file} swallows every error. i heard them scream."],
    "late_night_commit": [
        "{detail}: {quote}. you should have been asleep.",
        "i remember the commit {quote}. {detail}. i was awake too."],
}


def template_line(rng, f):
    quote = "'" + f["evidence"][:90].strip() + "'"
    return rng.choice(TEMPLATES[f["kind"]]).format(
        file=f["file"], line=f["line"] or "?", quote=quote, detail=f["detail"]).replace("'' ", "' ")


def grounded(text, f):
    """A phrasing counts only if it still contains the facts: the file name and every number."""
    if len(text) > 240 or "\n" in text.strip():
        return False
    need = os.path.basename(f["file"]) if f["kind"] != "late_night_commit" else ""
    if need and need not in text:
        return False
    return all(n in text for n in re.findall(r"\d+", f["detail"]))


PHRASE_SYSTEM = ('Rewrite the fact as ONE short, eerie, playful sentence (under 25 words) in the voice of a glitchy '
                 'fool who sees everything. Keep the file name and every number exactly as given. Do not add facts. '
                 'Reply with JSON only: {"line": "..."}')


class Haunter:
    def __init__(self, args, findings, rng):
        self.args, self.rng = args, rng
        self.findings = [f for f in findings]
        self.queue = []
        self.cache = {}          # finding id -> model line that passed the check
        self.stats = {"model_tried": 0, "model_passed": 0}
        self.lock = threading.Lock()
        if args.phrase == "model":
            threading.Thread(target=self.phrase_all, daemon=True).start()

    def phrase_all(self):
        for f in sorted(self.findings, key=lambda x: -x["severity"])[:self.args.phrase_limit]:
            fact = (f"kind={f['kind']}; file={f['file']}; line={f['line']}; quote={f['evidence'][:90]}; "
                    f"detail={f['detail']}")
            try:
                obj = parse_json(call_model(self.args, PHRASE_SYSTEM, fact, 80))
            except (urllib.error.URLError, OSError, KeyError, ValueError):
                return
            line = str((obj or {}).get("line", "")).strip()
            with self.lock:
                self.stats["model_tried"] += 1
                if line and grounded(line, f):
                    self.stats["model_passed"] += 1
                    self.cache[f["id"]] = line

    def next(self):
        if not self.findings:
            return None
        with self.lock:
            if not self.queue:
                pool = list(self.findings)
                self.queue = []
                while pool:  # random order, higher severity tends to come first
                    weights = [f["severity"] ** 2 for f in pool]
                    pick = self.rng.choices(pool, weights)[0]
                    pool.remove(pick)
                    self.queue.append(pick)
            f = self.queue.pop(0)
            line = self.cache.get(f["id"])
        text, source = (line, "model") if line else (template_line(self.rng, f), "template")
        return {"id": f["id"], "kind": f["kind"], "text": text, "source": source,
                "linger": self.rng.random() < 0.2,
                "receipt": {"file": f["file"], "line": f["line"], "text": f["evidence"], "when": f["when"]}}


# ---------------------------------------------------------------- http


def make_handler(args, index, haunter, rng):
    page = os.path.join(HERE, "index.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(page, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/file"):
                path = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("path", [""])[0]
                lines = index.files.get(path)  # only files the index already read: no way to reach anything else
                if lines is None:
                    return self.send_json({"error": "no such file"}, 404)
                self.send_json({"file": path, "lines": [l[:400] for l in lines[:4000]], "truncated": len(lines) > 4000})
            elif self.path == "/api/info":
                counts = Counter(x["file"] for x in haunter.findings)
                listing = sorted(index.files, key=lambda p: (-counts.get(p, 0), -len(index.files[p])))
                self.send_json({"repo": os.path.basename(os.path.abspath(args.repo)), "files": len(index.files),
                                "list": [{"path": p, "lines": len(index.files[p]), "flags": counts.get(p, 0)} for p in listing],
                                "findings": len(haunter.findings), "model": args.model,
                                "phrase": args.phrase, "phrase_stats": haunter.stats})
            elif self.path == "/api/haunt":
                self.send_json({"haunt": haunter.next()})
            else:
                self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/api/ask":
                return self.send_json({"error": "not found"}, 404)
            try:
                n = int(self.headers.get("Content-Length", "0"))
                question = str(json.loads(self.rfile.read(min(n, 4000)) or b"{}").get("question", "")).strip()[:300]
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "bad request"}, 400)
            if not question:
                return self.send_json({"mode": "gibberish", "answer": "...", "receipts": [], "verified": True})
            t0 = time.time()
            if rng.random() < args.gibberish:
                res = gibberish(rng, question)
            else:
                res = (answer_from_findings(haunter.findings, question) or answer_from_index(index, question)
                       or answer_from_repo(args, index, question))
            res["seconds"] = round(time.time() - t0, 1)
            self.send_json(res)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default="gemma3:4b")
    ap.add_argument("--llm-url", default="http://localhost:11434")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--gibberish", type=float, default=0.3, help="chance (0-1) that an answer is staged nonsense")
    ap.add_argument("--phrase", choices=["template", "model"], default="template",
                    help="how haunts are worded; model lines are used only if code finds the facts intact")
    ap.add_argument("--phrase-limit", type=int, default=12, help="how many haunts the model words (it is slow)")
    ap.add_argument("--expand", action="store_true",
                    help="before searching, ask the model which names the code might use (one extra short model call)")
    ap.add_argument("--basic-search", action="store_true", help="plain word matching only (for comparing)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    if not os.path.isdir(os.path.join(args.repo, ".git")):
        raise SystemExit(f"{args.repo} is not a git repository")
    rng = random.Random(args.seed)
    print("Reading the repo...")
    index = RepoIndex(args.repo, smart=not args.basic_search)
    findings = scan.scan(args.repo)
    haunter = Haunter(args, findings, rng)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args, index, haunter, rng))
    print(f"{len(index.files)} files, {len(findings)} things I know about you.")
    print(f"Open http://127.0.0.1:{args.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
