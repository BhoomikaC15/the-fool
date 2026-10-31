#!/usr/bin/env python3
"""Build small git repositories with KNOWN mistakes planted in them, plus decoys.

Decoys look like mistakes but are fine (an env lookup, a placeholder, a recent TODO,
a lowercase todo, .env.example ...). A good scanner must stay quiet on them.
The answer key goes to samples/ground_truth.json.

  python make_samples.py --out samples --n 12 --seed 5

Honest limit: I wrote both the planted mistakes and the scanner, so this checks the
scanner does what I meant. It is a sanity test, not a benchmark of real repositories.
"""
import argparse
import json
import os
import random
import shutil
import stat
import sys
import string
import subprocess
from datetime import datetime, timedelta

START = datetime(2026, 9, 1, 11, 0)
HEAD = datetime(2026, 10, 20, 17, 0)
TZ = "+0530"

BASE = {
    "app.py": "def greet(name):\n    return f\"hello {name}\"\n\n\ndef total(prices):\n    return sum(prices)\n",
    "utils.py": "def clean(text):\n    return text.strip().lower()\n",
    "handlers.py": "def handle(event):\n    return {\"ok\": True, \"event\": event}\n",
    "config.py": "DEBUG = True\nPORT = 8000\n",
    "settings.py": "NAME = \"demo\"\n",
    "client.py": "CLIENT_NAME = \"demo\"\n",
    "README.md": "# demo\n\nA tiny demo project.\n",
}


def git(repo, *args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    subprocess.run(["git", "-c", "user.name=Sam", "-c", "user.email=sam@example.test",
                    "-c", "commit.gpgsign=false", "-C", repo, *args],
                   check=True, capture_output=True, env=e)


def commit(repo, msg, when):
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S") + TZ
    git(repo, "add", "-A")
    git(repo, "commit", "-m", msg, env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})


def append(repo, rel, text):
    with open(os.path.join(repo, rel), "a", encoding="utf-8") as f:
        f.write(text)


def replace(repo, rel, old, new):
    path = os.path.join(repo, rel)
    with open(path, encoding="utf-8") as f:
        s = f.read()
    with open(path, "w", encoding="utf-8") as f:
        f.write(s.replace(old, new))


def token(rng, n):
    return "".join(rng.choices(string.ascii_letters + string.digits, k=n))


def day(rng, lo_days, hi_days):
    """A working-hours time between START+lo and START+hi days."""
    d = START + timedelta(days=rng.randint(lo_days, hi_days))
    return d.replace(hour=rng.randint(10, 18), minute=rng.randint(0, 59))


# Each planter returns a list of (when, action) events and the truth entries it creates.
def p_secret_in_tree(repo, rng):
    when = day(rng, 3, 40)
    return [(when, "add API key", lambda: append(repo, "config.py", f'API_KEY = "sk_live_{token(rng, 20)}"\n'))], \
           [{"kind": "secret_in_tree", "file": "config.py"}]


def p_secret_removed(repo, rng):
    t1 = day(rng, 3, 20)
    t2 = t1 + timedelta(days=rng.randint(1, 6))
    secret = token(rng, 18)
    return [(t1, "add db password", lambda: append(repo, "settings.py", f'DB_PASSWORD = "{secret}"\n')),
            (t2, "use env var", lambda: replace(repo, "settings.py", f'DB_PASSWORD = "{secret}"', 'import os\nDB_PASSWORD = os.environ["DB_PASSWORD"]'))], \
           [{"kind": "secret_in_history_removed", "file": "settings.py"}]


def p_env_file(repo, rng):
    return [(day(rng, 3, 40), "add env", lambda: append(repo, ".env", "PORT=8000\nDEBUG=true\n"))], \
           [{"kind": "sensitive_file_tracked", "file": ".env"}]


def p_old_todo(repo, rng):
    when = HEAD - timedelta(days=rng.randint(25, 40))
    return [(when, "note", lambda: append(repo, "utils.py", "\n# TODO: handle empty input\n"))], \
           [{"kind": "old_todo", "file": "utils.py"}]


def p_syntax(repo, rng):
    return [(day(rng, 3, 40), "wip", lambda: append(repo, "broken.py", "def broken(:\n    pass\n"))], \
           [{"kind": "syntax_error", "file": "broken.py"}]


def p_debug(repo, rng):
    return [(day(rng, 3, 40), "debug", lambda: append(repo, "app.py", "\n\ndef risky(x):\n    breakpoint()\n    return x\n"))], \
           [{"kind": "debug_leftover", "file": "app.py"}]


def p_bare_except(repo, rng):
    return [(day(rng, 3, 40), "handle errors",
             lambda: append(repo, "handlers.py", "\n\ndef safe(x):\n    try:\n        return 1 / x\n    except:\n        return 0\n"))], \
           [{"kind": "bare_except", "file": "handlers.py"}]


def p_late_night(repo, rng):
    when = day(rng, 3, 40).replace(hour=3, minute=12)
    return [(when, "fix stuff", lambda: append(repo, "README.md", "\nnotes written at night\n"))], \
           [{"kind": "late_night_commit", "file": None}]


def p_stub(repo, rng):
    return [(day(rng, 3, 40), "start training", lambda: append(repo, "app.py", "\n\ndef train(model):\n    pass\n"))], \
           [{"kind": "stub_function", "file": "app.py"}]


def p_empty(repo, rng):
    def make():
        open(os.path.join(repo, "todo.py"), "w").close()
    return [(day(rng, 3, 40), "add file", make)], [{"kind": "empty_file", "file": "todo.py"}]


def p_unused_import(repo, rng):
    return [(day(rng, 3, 40), "import", lambda: append(repo, "utils.py", "\nimport json\n"))], \
           [{"kind": "unused_import", "file": "utils.py"}]


def p_debug_print(repo, rng):
    return [(day(rng, 3, 40), "debug", lambda: append(repo, "handlers.py", "\n\ndef debugme(x):\n    print(\"here\")\n    return x\n"))], \
           [{"kind": "debug_print", "file": "handlers.py"}]


PLANTED = {
    "stub_function": p_stub,
    "empty_file": p_empty,
    "unused_import": p_unused_import,
    "debug_print": p_debug_print,
    "secret_in_tree": p_secret_in_tree,
    "secret_in_history_removed": p_secret_removed,
    "sensitive_file_tracked": p_env_file,
    "old_todo": p_old_todo,
    "syntax_error": p_syntax,
    "debug_leftover": p_debug,
    "bare_except": p_bare_except,
    "late_night_commit": p_late_night,
}


# Decoys: look suspicious, are fine. The scanner must NOT report any of them.
def _empty_init(repo, rng):
    os.makedirs(os.path.join(repo, "pkg"), exist_ok=True)
    open(os.path.join(repo, "pkg", "__init__.py"), "w").close()


DECOYS = {
    "empty_init": _empty_init,
    "abstract_stub": lambda repo, rng: append(repo, "client.py", "\nfrom abc import ABC, abstractmethod\n\n\nclass Base(ABC):\n    @abstractmethod\n    def run(self):\n        pass\n"),
    "used_import": lambda repo, rng: append(repo, "client.py", "\nimport json\n\nDATA = json.dumps({})\n"),
    "class_pass": lambda repo, rng: append(repo, "client.py", "\n\nclass AppError(Exception):\n    pass\n"),
    "real_print": lambda repo, rng: append(repo, "client.py", "\nprint(\"starting server\")\n"),
    "env_lookup": lambda repo, rng: append(repo, "client.py", 'import os\nAPI_KEY = os.environ["API_KEY"]\n'),
    "placeholder": lambda repo, rng: append(repo, "client.py", 'token = "your_token_goes_here_please"\n'),
    "recent_todo": lambda repo, rng: append(repo, "client.py", "# TODO: add retries\n"),
    "lower_todo": lambda repo, rng: append(repo, "client.py", "# todo: maybe later\n"),
    "env_example": lambda repo, rng: append(repo, ".env.example", "PORT=8000\n"),
    "except_exception": lambda repo, rng: append(repo, "client.py", "\ntry:\n    pass\nexcept Exception:\n    pass\n"),
    "comment_debug": lambda repo, rng: append(repo, "client.py", "# never leave breakpoint() in production\n"),
}


def build(repo, rng):
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in BASE.items():
        with open(os.path.join(repo, rel), "w", encoding="utf-8") as f:
            f.write(text)
    commit(repo, "initial commit", START)

    events, truth = [], []
    for kind in rng.sample(list(PLANTED), k=rng.randint(4, 6)):
        ev, tr = PLANTED[kind](repo, rng)
        events += ev
        truth += tr
    decoys = rng.sample(list(DECOYS), k=rng.randint(4, len(DECOYS)))
    for name in decoys:
        # a recent TODO must really be recent; everything else sits anywhere in the timeline
        when = HEAD - timedelta(days=2) if name == "recent_todo" else day(rng, 3, 40)
        if name == "lower_todo":
            when = HEAD - timedelta(days=30)
        events.append((when, f"decoy {name}", (lambda n=name: DECOYS[n](repo, rng))))
    events.sort(key=lambda e: e[0])
    for when, msg, action in events:
        action()
        commit(repo, msg, when)
    append(repo, "README.md", "\nrelease notes\n")
    commit(repo, "release", HEAD)
    if rng.random() < 0.5:
        append(repo, "README.md", "\nrewriting the intro\n")  # never committed
        stamp = datetime(2026, 10, 21, 12, 0).timestamp()
        os.utime(os.path.join(repo, "README.md"), (stamp, stamp))
        truth.append({"kind": "uncommitted_change", "file": "README.md"})
    elif rng.random() < 0.6:  # decoy: touched yesterday, still work in progress
        append(repo, "config.py", "# tweaking\n")
        stamp = datetime(2026, 10, 27, 12, 0).timestamp()
        os.utime(os.path.join(repo, "config.py"), (stamp, stamp))
    return truth, decoys


def _force_delete(func, path, _exc):
    """Windows keeps git object files read-only; clear the flag and retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args()
    if os.path.exists(args.out):
        if sys.version_info >= (3, 12):
            shutil.rmtree(args.out, onexc=_force_delete)
        else:
            shutil.rmtree(args.out, onerror=_force_delete)
    os.makedirs(args.out)
    answer = {}
    for i in range(1, args.n + 1):
        name = f"repo{i:02d}"
        truth, decoys = build(os.path.join(args.out, name), random.Random(args.seed * 1000 + i))
        answer[name] = {"truth": truth, "decoys": decoys}
    with open(os.path.join(args.out, "ground_truth.json"), "w", encoding="utf-8") as f:
        json.dump(answer, f, indent=1)
    planted = sum(len(v["truth"]) for v in answer.values())
    print(f"Built {args.n} repos in {args.out}/ with {planted} planted mistakes. Answer key: ground_truth.json")


if __name__ == "__main__":
    main()
