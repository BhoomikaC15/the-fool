# The Fool

*My entry for Hacktoberfest Open-Source AI Challenge Week 4: Ghost in the Machine.*

The Fool is an AI that lives in your git repo. It looks broken and unfinished on purpose: it stutters, it loads its brain to 12% and stays there, and when you ask it something it sometimes says something obviously stupid. But it has read every file in your project, and every now and then, out of nowhere, it interrupts to point out a mistake you made. A real one, with the file, the line and the date.

It runs entirely on your machine with a small open model (Gemma through Ollama). It is read-only: it never changes your repo and never runs your code.

## How it works

Two things are kept strictly apart:

1. **Code finds the mistakes.** `scan.py` reads the working tree and the git history and reports: a secret in a file, a secret you deleted but git still remembers, a tracked `.env` or key file, a syntax error, a leftover debugger or `print("here")`, a bare `except:`, an old TODO, an empty file, a function that is just `pass`, an unused import, files changed and never committed for days, and commits made after midnight. Each finding carries the exact file, line, time and quoted text. Secrets are masked to four characters.
2. **The model only talks.** In the chat, Gemma answers from the most relevant snippets of your repo, and must cite file and line. Code checks every citation against the real files and drops any that do not exist. Haunts are worded by templates by default, or by the model with `--phrase model`; a model-worded line is used only if it still contains the file name and every number from the finding.

The glitchy "stupid" answers never come from the model. They are fixed lines (with a mangled echo of your question) and the UI labels them "glitch, not an answer", so they cannot pass for advice.

## Run it

You need Python 3.9 or newer and [Ollama](https://ollama.com/). No `pip install`.

```
ollama pull gemma3:4b
python fool_server.py path\to\your\repo
```

Open http://127.0.0.1:8765. Options: `--phrase model` (the model words the haunts), `--gibberish 0.3` (chance that an answer is a staged glitch), `--model`, `--port`. The "summon" button makes it haunt you right now. "Speak aloud" uses your browser's own voice, locally.

Just the scanner:

```
python scan.py path\to\your\repo
```

## Does it work? What I measured

- **Scanner on planted mistakes.** `make_samples.py` builds 12 git repos with known mistakes and look-alike decoys (an env-variable lookup, a placeholder key, a recent TODO, `.env.example`, abstract methods). `eval_scan.py` found 67 of 67 with no false alarms. **This is a sanity test:** I wrote the mistakes and the scanner, so it only shows the scanner does what I meant.
- **Scanner on real code.** I ran it on two real open-source repos (click, requests) and on my own unfinished project. The first run flagged deliberate test certificates as secrets, so I excluded test and fixture folders. Later I removed noisy stub and unused-import cases (`...` bodies, `NotImplementedError`, subclass hooks, re-export modules). **Those rules were tuned after I saw real output**, so treat them as heuristics, not a benchmark. On my own unfinished project it found 15 things (5 files changed and never committed, one for 138 days; 2 commits after midnight; 8 unused imports). All of them were real.
- **Repo Q&A.** `eval_qa.py` checks whether the top-3 snippets contain the file the answer lives in (retrieval), and whether the model's citations are real and include that file. On my built-in sample questions retrieval is 83 of 84; those are easy and I wrote them. The fairer test is 20 questions I wrote for my own unfinished project (ANCHOR), in my own words, with typos and loose wording ("average duration" for `avg_dur`):

  | Search | Retrieval | Answer cites the right file | Answer has a real file:line receipt |
  | --- | --- | --- | --- |
  | Plain word matching (`--basic-search`) | 13/20 (65%) | 12/20 (60%) | 19/20 |
  | Default (synonyms, prefix and typo matching, definition boost) | 14/20 (70%) | 13/20 (65%) | 19/20 |
  | Default + `--expand` (Gemma guesses code names first) | 16/20 (80%) | 15/20 (75%) | 19/20 |

  Read this carefully: it is 20 questions, so each question is 5 points. **I added the default search rules after seeing the plain search fail on these same questions, so 70% is not a clean held-out result**, and the gain from them was one question. `--expand` helped most, at the cost of one extra model call per question. It still misses 4 of 20 (for example "which file is the timer test written in", and a question about a persona baseline). Some answers had zero receipts, and those are shown as unverified, not as answers.
- **Haunt wording.** With `--phrase model`, 8 of 10 Gemma-worded lines kept their facts and passed the check; the rest fell back to templates. `/api/info` reports this.

## Limits

- Most checks are Python-specific. Language-independent: secrets, tracked key files, TODOs, empty files, uncommitted changes, late commits.
- Gemma 3 4B on my laptop (8 GB RAM, no GPU) takes about [30] seconds per chat answer.
- Citations prove the file and line exist, not that the answer is right.
- "Uncommitted for N days" uses file modification time, which can be wrong after a copy or a checkout.
- It reads only the repo you point it at. Do not point it at someone else's.

## Files

| File | What it does |
| --- | --- |
| `fool_server.py` | The local app: repo index, chat, haunts |
| `index.html` | The glitchy interface |
| `scan.py` | Finds the real mistakes |
| `make_samples.py`, `eval_scan.py` | Planted-mistake repos and the scanner score |
| `eval_qa.py` | Q&A retrieval and citation score |

## Notes

Built during the challenge window as a new project. No code from earlier projects was reused. 
