#!/usr/bin/env python3
"""Score scan.py against the planted mistakes in samples/ground_truth.json.

  python eval_scan.py samples

A finding counts as correct if its (kind, file) matches a planted mistake
(late-night commits match on kind only). Anything else the scanner reports is a
false alarm; any planted mistake it does not report is a miss.
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from scan import scan

NOW = int(datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp())  # fixed "today" so the result is reproducible


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "samples"
    with open(os.path.join(root, "ground_truth.json"), encoding="utf-8") as f:
        answer = json.load(f)
    hit, miss, false_alarm = defaultdict(int), defaultdict(int), defaultdict(int)
    problems = []
    for name, entry in sorted(answer.items()):
        found = scan(os.path.join(root, name), NOW)
        unmatched = list(found)
        for t in entry["truth"]:
            match = next((f for f in unmatched if f["kind"] == t["kind"] and (t["file"] is None or f["file"] == t["file"])), None)
            if match:
                hit[t["kind"]] += 1
                unmatched.remove(match)
            else:
                miss[t["kind"]] += 1
                problems.append(f"MISS  {name}: {t['kind']} in {t['file']}")
        for f in unmatched:
            false_alarm[f["kind"]] += 1
            problems.append(f"FALSE {name}: {f['kind']} {f['file']}:{f['line']}  {f['evidence']}")
    kinds = sorted(set(hit) | set(miss) | set(false_alarm))
    print(f"{'kind':<28}{'planted':>8}{'found':>7}{'missed':>8}{'false alarms':>14}")
    for k in kinds:
        print(f"{k:<28}{hit[k] + miss[k]:>8}{hit[k]:>7}{miss[k]:>8}{false_alarm[k]:>14}")
    total_p = sum(hit.values()) + sum(miss.values())
    total_f = sum(hit.values())
    total_fa = sum(false_alarm.values())
    print(f"\nPlanted: {total_p}  Found: {total_f}  Missed: {sum(miss.values())}  False alarms: {total_fa}")
    if total_f + total_fa:
        print(f"Recall {total_f / total_p:.1%}   Precision {total_f / (total_f + total_fa):.1%}")
    for p in problems:
        print(p)


if __name__ == "__main__":
    main()
