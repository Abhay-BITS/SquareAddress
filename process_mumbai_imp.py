#!/usr/bin/env python3
"""Combine Mumbai Imp Data TSV (.XLS) files and parse Address1 / Address2 separately."""

from __future__ import annotations

import csv
import importlib.util
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
csv.field_size_limit(min(sys.maxsize, 10_000_000))
MUMBAI_IMP_DIR = (
    ROOT.parent
    / "OneDrive_SquareYards"
    / "OneDrive_1_14-09-2026"
    / "BData"
    / "Relavant"
    / "Good"
    / "Mumbai Imp Data"
)
COMBINED_CSV = ROOT / "Mumbai_Imp_Data_combined.csv"
PARSED_CSV = ROOT / "Mumbai_Imp_Data_parsed.csv"
REPORT_TXT = ROOT / "Mumbai_Imp_Data_parsed.report.txt"

DEFAULT_STATE = "MH"
CITY_HINT = "Mumbai"

SUFFIX_FIELDS = (
    "ZIP",
    "building_number",
    "building_name",
    "dotcom_matched",
    "dotcom_match_rule",
    "location_matched",
    "landmark",
    "locality",
    "locality_source",
    "city",
    "district",
    "confidence",
)

LABELED_BUILDING_PATTERNS = (
    re.compile(r"\bBuilding\s*:\s*([^,]+)", re.IGNORECASE),
    re.compile(r"\bSociety\s*:\s*([^,]+)", re.IGNORECASE),
    re.compile(r"\bComplex\s*:\s*([^,]+)", re.IGNORECASE),
)
WEAK_BUILDING_NAME_RE = re.compile(
    r"^(wing|wng|plot|flat|floor|shop|unit|block|bldg)\b",
    re.IGNORECASE,
)


def _load_address_module():
    spec = importlib.util.spec_from_file_location("addr", ROOT / "Address.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_imp_file(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()[:8]
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = path.read_text(encoding="utf-16-le", errors="replace")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    rows = list(csv.reader(text.splitlines(), delimiter="\t"))
    if not rows:
        return []
    header = [h.lstrip("\ufeff").strip() for h in rows[0]]
    out: list[dict[str, str]] = []
    for line in rows[1:]:
        if not any(cell.strip() for cell in line):
            continue
        cells = line + [""] * (len(header) - len(line))
        raw_row = {header[i]: cells[i].strip() for i in range(len(header))}
        out.append(
            {
                "source_file": path.name,
                "Name": raw_row.get("Name", ""),
                "Address1": raw_row.get("Address1", ""),
                "Address2": raw_row.get("Address2", ""),
                "Telephone": raw_row.get("Telephone", ""),
                "Mobile": raw_row.get("Mobile", ""),
            }
        )
    return out


def _extract_labeled_building(text: str) -> str:
    for pattern in LABELED_BUILDING_PATTERNS:
        match = pattern.search(text or "")
        if match:
            name = match.group(1).strip(" ,")
            if len(name) >= 2:
                return name
    return ""


def _labeled_building_aligned(addr_mod, labeled: str, current: str) -> bool:
    if not labeled or not current:
        return False
    norm_l = addr_mod._norm_project_text(labeled)
    norm_c = addr_mod._norm_project_text(current)
    return bool(norm_l and norm_c and (norm_l == norm_c or norm_l in norm_c or norm_c in norm_l))


def _is_weak_building_name(name: str) -> bool:
    text = (name or "").strip()
    if not text or len(text) <= 2:
        return True
    return bool(WEAK_BUILDING_NAME_RE.match(text))


def _refresh_parsed_flags(addr_mod, parsed: dict[str, str]) -> None:
    zip_code = (parsed.get("ZIP") or "").strip()
    state_code = DEFAULT_STATE
    lookup = addr_mod.pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
    if lookup and lookup.get("state"):
        for code, name in addr_mod.STATE_ABBREV.items():
            if addr_mod._states_match(name, lookup.get("state")):
                state_code = code
                break
    # Same project+city+subLocation gate as the COMBO pipeline (was a name-only check).
    addr_mod._apply_dotcom_triplet(parsed, address=parsed.get("ADDRESS") or "")
    parsed["location_matched"] = addr_mod._location_matched_flag(parsed.get("building_name"))
    parsed["confidence"] = str(
        addr_mod._score_confidence(
            pin=zip_code or None,
            state_code=state_code,
            city=parsed.get("city"),
            district=parsed.get("district"),
            locality=parsed.get("locality"),
            building_number=parsed.get("building_number"),
            building_name=parsed.get("building_name"),
            landmark=parsed.get("landmark"),
            lookup=lookup,
            dotcom_matched=parsed["dotcom_matched"],
            dotcom_match_rule=parsed.get("dotcom_match_rule", ""),
            location_matched=parsed["location_matched"],
        )
    )


def _apply_labeled_building_override(
    addr_mod,
    parsed: dict[str, str],
    address: str,
    stats: Counter,
    *,
    slot: str,
) -> dict[str, str]:
    labeled = _extract_labeled_building(address)
    if not labeled:
        return parsed

    stats[f"slot{slot}_labeled_in_text"] += 1
    current = (parsed.get("building_name") or "").strip()
    if _labeled_building_aligned(addr_mod, labeled, current):
        stats[f"slot{slot}_labeled_already_ok"] += 1
        return parsed

    canonical = addr_mod._canonical_dotcom_lookup(labeled, CITY_HINT)
    if not canonical:
        hit, _kind = addr_mod._match_project_dictionary(address, CITY_HINT)
        if hit:
            canonical = hit

    if _is_weak_building_name(current):
        stats[f"slot{slot}_labeled_replaced_weak"] += 1
    else:
        stats[f"slot{slot}_labeled_replaced_other"] += 1

    parsed["building_name"] = canonical or labeled.strip()
    _refresh_parsed_flags(addr_mod, parsed)
    stats[f"slot{slot}_labeled_override_applied"] += 1
    return parsed


def _prefix_slot(parsed: dict[str, str], slot: str) -> dict[str, str]:
    return {f"{key}_{slot}": parsed.get(key, "") for key in SUFFIX_FIELDS}


def _parse_slot(addr_mod, address: str, stats: Counter, *, slot: str) -> dict[str, str]:
    zip_code = addr_mod._extract_zip_from_text(address)
    state_code = DEFAULT_STATE
    if zip_code.isdigit() and len(zip_code) == 6:
        lookup = addr_mod.pincode.lookup(zip_code)
        if lookup and lookup.get("state"):
            for code, name in addr_mod.STATE_ABBREV.items():
                if addr_mod._states_match(name, lookup.get("state")):
                    state_code = code
                    break
    parsed = addr_mod._build_parsed_output_row(
        address=address,
        state_code=state_code,
        zip_code=zip_code,
        stats=Counter(),
        city_hint=CITY_HINT,
    )
    return _apply_labeled_building_override(addr_mod, parsed, address, stats, slot=slot)


def _slot_stats(stats: Counter, slot: str, parsed: dict[str, str]) -> None:
    stats[f"slot{slot}_rows"] += 1
    if (parsed.get("ADDRESS") or "").strip():
        stats[f"slot{slot}_address_present"] += 1
    if (parsed.get("building_name") or "").strip():
        stats[f"slot{slot}_building_name_filled"] += 1
    if parsed.get("dotcom_matched") == "Yes":
        stats[f"slot{slot}_dotcom_yes"] += 1
    if parsed.get("location_matched") == "Yes":
        stats[f"slot{slot}_location_yes"] += 1


def combine_folder(src_dir: Path) -> tuple[list[dict[str, str]], list[tuple[str, int]]]:
    files = sorted(
        p
        for p in src_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".xls", ".xlsx", ".csv"}
    )
    combined: list[dict[str, str]] = []
    file_counts: list[tuple[str, int]] = []
    for path in files:
        chunk = _read_imp_file(path)
        file_counts.append((path.name, len(chunk)))
        combined.extend(chunk)
    return combined, file_counts


def main() -> None:
    src_dir = MUMBAI_IMP_DIR
    if len(sys.argv) > 1:
        src_dir = Path(sys.argv[1]).resolve()

    addr = _load_address_module()
    rows, file_counts = combine_folder(src_dir)
    if not rows:
        raise SystemExit(f"No rows loaded from {src_dir}")

    base_fields = ["source_file", "Name", "Address1", "Address2", "Telephone", "Mobile"]
    out_fields = base_fields + [f"{f}_{s}" for s in ("1", "2") for f in SUFFIX_FIELDS]

    # raw combined (no parse)
    with COMBINED_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base_fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in base_fields})

    stats: Counter = Counter()
    stats["combined_rows"] = len(rows)
    either = 0
    t0 = time.perf_counter()

    with PARSED_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            out = {k: row.get(k, "") for k in base_fields}
            for slot, col in (("1", "Address1"), ("2", "Address2")):
                parsed = _parse_slot(addr, (row.get(col) or "").strip(), stats, slot=slot)
                out.update(_prefix_slot(parsed, slot))
                _slot_stats(stats, slot, parsed)
            if out.get("dotcom_matched_1") == "Yes" or out.get("dotcom_matched_2") == "Yes":
                either += 1
            writer.writerow(out)
            if i % 5000 == 0:
                print(f"... parsed {i}/{len(rows)}", file=sys.stderr)

    elapsed = time.perf_counter() - t0
    n = stats["combined_rows"]

    lines = [
        "Mumbai Imp Data dual-address parse report",
        "=" * 44,
        f"Source folder: {src_dir}",
        f"Source files:  {len(file_counts)}",
        "Rows per source file:",
    ]
    for name, count in file_counts:
        lines.append(f"  {name}: {count:,}")
    lines += [
        "",
        f"Combined rows: {n:,}",
        f"Combined CSV:  {COMBINED_CSV.name}",
        f"Parsed CSV:    {PARSED_CSV.name}",
        f"Elapsed: {elapsed:.1f}s ({n / elapsed:.1f} rows/s)" if elapsed else "",
        "",
        "Address1 slot",
        "-" * 44,
        f"  Rows parsed:           {stats.get('slot1_rows', 0):,}",
        f"  Non-empty address:     {stats.get('slot1_address_present', 0):,}",
        f"  building_name filled:  {stats.get('slot1_building_name_filled', 0):,} "
        f"({100 * stats.get('slot1_building_name_filled', 0) / max(stats.get('slot1_address_present', 1), 1):.1f}% of non-empty Address1)",
        f"  Building: label in text: {stats.get('slot1_labeled_in_text', 0):,}",
        f"  Label override applied:  {stats.get('slot1_labeled_override_applied', 0):,} "
        f"(weak: {stats.get('slot1_labeled_replaced_weak', 0):,}, other: {stats.get('slot1_labeled_replaced_other', 0):,})",
        f"  dotcom_matched Yes:    {stats.get('slot1_dotcom_yes', 0):,} "
        f"({100 * stats.get('slot1_dotcom_yes', 0) / n:.1f}%)",
        f"  location_matched Yes:  {stats.get('slot1_location_yes', 0):,}",
        "",
        "Address2 slot",
        "-" * 44,
        f"  Rows parsed:           {stats.get('slot2_rows', 0):,}",
        f"  Non-empty address:     {stats.get('slot2_address_present', 0):,}",
        f"  building_name filled:  {stats.get('slot2_building_name_filled', 0):,} "
        f"({100 * stats.get('slot2_building_name_filled', 0) / max(stats.get('slot2_address_present', 1), 1):.1f}% of non-empty Address2)",
        f"  Building: label in text: {stats.get('slot2_labeled_in_text', 0):,}",
        f"  Label override applied:  {stats.get('slot2_labeled_override_applied', 0):,}",
        f"  dotcom_matched Yes:    {stats.get('slot2_dotcom_yes', 0):,} "
        f"({100 * stats.get('slot2_dotcom_yes', 0) / n:.1f}%)",
        f"  location_matched Yes:  {stats.get('slot2_location_yes', 0):,}",
        "",
        "Either Address1 or Address2 dotcom Yes:",
        f"  {either:,} ({100 * either / n:.1f}%)",
    ]
    REPORT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Combined {n:,} rows -> {COMBINED_CSV.name}")
    print(f"Parsed -> {PARSED_CSV.name}")
    print(f"Report -> {REPORT_TXT.name}")


if __name__ == "__main__":
    main()
