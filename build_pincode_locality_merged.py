#!/usr/bin/env python3
"""Merge the two pincode->locality sources into pincode_locality_merged.csv.

  Pincode.xlsx                     Square Yards city / location / subLocation / pincode
  Pincode To Locality  Mapping.csv India Post office names (S.O / B.O / H.O suffixes stripped)

One row per (pincode, locality). A locality present in both keeps the Square Yards
spelling and is marked source "both", so nothing is duplicated. Names that are only a
city ("Bengaluru G.") or shorter than 3 letters are dropped.

Usage:  python build_pincode_locality_merged.py
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import Address as A  # noqa: E402

XLSX = ROOT / "Pincode.xlsx"
POST_CSV = ROOT / "Pincode To Locality  Mapping.csv"
OUT_CSV = ROOT / "pincode_locality_merged.csv"


def main() -> None:
    # (pincode, normalised locality) -> row; Square Yards wins on spelling.
    merged: dict[tuple[str, str], dict[str, str]] = {}
    stats: Counter = Counter()

    ws = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)["Pincode"]
    for city, location, sublocation, pincode in (
        (r[1], r[3], r[5], r[6]) for r in ws.iter_rows(min_row=2, values_only=True)
    ):
        pin = str(pincode or "").strip()
        if not (pin.isdigit() and len(pin) == 6):
            stats["skipped_bad_pincode"] += 1
            continue
        for name, kind in ((sublocation, "subLocation"), (location, "location")):
            name = A._clean_locality_name(str(name or "").strip())
            if not name:
                continue
            key = (pin, A._triplet_norm(name))
            if key in merged:
                continue
            merged[key] = {
                "pincode": pin,
                "locality": name,
                "city": str(city or "").strip(),
                "level": kind,
                "source": "squareyards",
            }
            stats[f"squareyards_{kind}"] += 1

    for row in csv.DictReader(POST_CSV.open(encoding="utf-8")):
        pin = (row.get("pincode") or "").strip()
        name = A._clean_locality_name(row.get("Location ") or row.get("Location") or "")
        if not (pin.isdigit() and len(pin) == 6) or not name:
            stats["skipped_post_office"] += 1
            continue
        key = (pin, A._triplet_norm(name))
        existing = merged.get(key)
        if existing:
            if existing["source"] == "squareyards":
                existing["source"] = "both"
                stats["both"] += 1
                stats["squareyards_" + existing["level"]] -= 1
            continue
        merged[key] = {
            "pincode": pin,
            "locality": name,
            "city": (row.get("district") or "").strip().title(),
            "level": "post_office",
            "source": "india_post",
        }
        stats["india_post"] += 1

    rows = sorted(merged.values(), key=lambda r: (r["pincode"], r["locality"]))
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["pincode", "locality", "city", "level", "source"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"{len(rows)} rows, {len({r['pincode'] for r in rows})} pincodes -> {OUT_CSV.name}")
    for key, value in sorted(stats.items()):
        print(f"  {key:26s} {value}")


if __name__ == "__main__":
    main()
