#!/usr/bin/env python3
"""Score the repo Q&A of The Fool.

Two numbers per question set:
  retrieval  - did the top-3 snippets contain the file the answer lives in? (no model, instant)
  answered   - did the model's answer cite a REAL file:line, and did the citations include
               the expected file? (needs Ollama; slow on a laptop)

  python eval_qa.py samples --no-model                  # retrieval only
  python eval_qa.py samples --model gemma3:4b           # full run, about 35 s per question
  python eval_qa.py path/to/repo --questions my_questions.json

my_questions.json: [{"q": "Where is the config loaded?", "file": "config.py"}, ...]

Honest limit: the built-in questions were written by me for the sample repos, so they are
easy. Write 10 questions of your own for a real repo for a fairer number.
"""
import argparse
import json
import os
import random
from types import SimpleNamespace

import fool_server as fs

SAMPLE_QUESTIONS = [
    ("Where is the greet function defined?", "app.py"),
    ("What does the handle function return?", "handlers.py"),
    ("Where is the PORT setting?", "config.py"),
    ("What does the clean function do to text?", "utils.py"),
    ("Where is NAME set for the project?", "settings.py"),
    ("What is the README about?", "README.md"),
    ("Where is CLIENT_NAME defined?", "client.py"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a repo, or a folder of sample repos (repo01, repo02, ...)")
    ap.add_argument("--questions", help="JSON file of {q, file} for a single real repo")
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--model", default="gemma3:4b")
    ap.add_argument("--llm-url", default="http://localhost:11434")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--limit", type=int, default=0, help="questions per repo")
    ap.add_argument("--expand", action="store_true", help="let the model guess code names before searching")
    ap.add_argument("--basic-search", action="store_true", help="plain word matching only, to compare against")
    args = ap.parse_args()

    if args.questions:
        jobs = [(args.path, [(x["q"], x["file"]) for x in json.load(open(args.questions, encoding="utf-8"))])]
    else:
        jobs = [(os.path.join(args.path, d), SAMPLE_QUESTIONS) for d in sorted(os.listdir(args.path))
                if os.path.isdir(os.path.join(args.path, d, ".git"))]
    n = hit = cited = valid_any = verified_dont_know = 0
    for repo, questions in jobs:
        index = fs.RepoIndex(repo, smart=not args.basic_search)
        run_args = SimpleNamespace(model=args.model, llm_url=args.llm_url, timeout=args.timeout, expand=args.expand)
        for q, want in questions[: args.limit or None]:
            n += 1
            extra = fs.expand_query(run_args, q) if (args.expand and not args.no_model) else ()
            got = {c["file"] for c in index.search(q, extra=extra)}
            ok_ret = want in got
            hit += ok_ret
            line = f"{os.path.basename(repo)}: {q[:48]:<48} retrieval={'yes' if ok_ret else 'NO '}"
            if not args.no_model:
                res = fs.answer_from_repo(run_args, index, q)
                files = {r["file"] for r in res["receipts"]}
                valid_any += bool(res["receipts"])
                cited += want in files
                verified_dont_know += (not res["receipts"]) and res["verified"]
                line += f"  receipts={len(res['receipts'])} expected_file_cited={'yes' if want in files else 'no'}"
            print(line)
    print(f"\nQuestions: {n}")
    print(f"Retrieval (expected file in the top-3 snippets): {hit}/{n} = {hit / n:.0%}")
    if not args.no_model:
        print(f"Answers with at least one real file:line receipt: {valid_any}/{n} = {valid_any / n:.0%}")
        print(f"Answers citing the expected file: {cited}/{n} = {cited / n:.0%}")
        print(f"Honest 'I don't know' answers (no receipts, nothing invented): {verified_dont_know}")


if __name__ == "__main__":
    main()
