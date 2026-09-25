#!/usr/bin/env python3
"""Build dotcom_match_review.csv: a stratified sample for hand-labelling.

Fill the `verdict` column with correct / wrong / unsure (and `notes` if useful), then run
  python make_review_sample.py --score
to get precision per match rule. Until this is labelled, every precision claim is a guess.
"""
from __future__ import annotations

import csv
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARSED = ROOT / "COMBO_DEMOG_parsed.csv"
REVIEW = ROOT / "dotcom_match_review.csv"
PER_RULE = 50


def build() -> None:
    rows = [r for r in csv.DictReader(PARSED.open(encoding="utf-8")) if r["dotcom_matched"] == "Yes"]
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_rule[row["dotcom_match_rule"]].append(row)
    random.seed(20260923)
    sample: list[dict] = []
    for rule, pool in sorted(by_rule.items()):
        sample.extend(random.sample(pool, min(PER_RULE, len(pool))))
    fields = ["verdict", "notes", "ADDRESS", "ZIP", "building_name", "locality", "city",
              "dotcom_match_rule", "confidence"]
    with REVIEW.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in sample:
            writer.writerow({"verdict": "", "notes": "", **row})
    print(f"{len(sample)} rows -> {REVIEW.name}")
    print("Label each row: correct / wrong / unsure. Ask: is this address really that project?")
    for rule, pool in sorted(by_rule.items()):
        print(f"  {rule:26s} population {len(pool):6d}  sampled {min(PER_RULE, len(pool))}")


def score() -> None:
    rows = list(csv.DictReader(REVIEW.open(encoding="utf-8")))
    labelled = [r for r in rows if (r["verdict"] or "").strip().lower() in ("correct", "wrong", "unsure")]
    if not labelled:
        raise SystemExit("No verdicts filled in yet.")
    per_rule: dict[str, Counter] = defaultdict(Counter)
    for row in labelled:
        per_rule[row["dotcom_match_rule"]][row["verdict"].strip().lower()] += 1
    print(f"labelled {len(labelled)} of {len(rows)}")
    total = Counter()
    for rule, counts in sorted(per_rule.items()):
        decided = counts["correct"] + counts["wrong"]
        total.update(counts)
        rate = f"{100 * counts['correct'] / decided:.0f}%" if decided else "n/a"
        print(f"  {rule:26s} correct {counts['correct']:3d}  wrong {counts['wrong']:3d}"
              f"  unsure {counts['unsure']:3d}  precision {rate}")
    decided = total["correct"] + total["wrong"]
    if decided:
        print(f"  {'OVERALL':26s} precision {100 * total['correct'] / decided:.1f}% on {decided} decided rows")


if __name__ == "__main__":
    score() if "--score" in sys.argv else build()
