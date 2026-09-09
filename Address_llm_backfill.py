#!/usr/bin/env python3
"""LLM backfill for empty building_name (and optional landmark) via Groq API.

Separate from Address.py. Reads parsed CSV, calls Groq only where fields are empty.

Setup:
    export GROQ_API_KEY="your-key-here"
    pip install groq

Usage:
    python3 Address_llm_backfill.py --limit 20          # smoke test
    python3 Address_llm_backfill.py                     # all empty building_name rows
    python3 Address_llm_backfill.py --fields building_name,landmark

Never commit API keys. Rotate any key that was pasted into chat or logs.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INPUT_CSV = ROOT / "COMBO_DEMOG_parsed.csv"
OUTPUT_CSV = ROOT / "COMBO_DEMOG_llm_backfill.csv"
CHECKPOINT_CSV = ROOT / "COMBO_DEMOG_llm_checkpoint.csv"

DEFAULT_MODEL = "allam-2-7b"  # cheapest model on Groq; good for high-volume backfill
DEFAULT_RPM = 30
DEFAULT_SLEEP = 60.0 / DEFAULT_RPM

SYSTEM_PROMPT = """You extract structured fields from messy Indian postal addresses.
Return ONLY valid JSON with keys: building_name, landmark.
Use null when a field is not clearly present. Do not guess.
building_name = named society, apartment complex, tower, villa project, or office building.
landmark = place after NEAR, BEHIND, OPP, OPPOSITE only.
Ignore person names (S/O, C/O), city, state, pincode, and street numbers alone."""


def _needs_backfill(row: dict, fields: list[str]) -> bool:
    return any(not (row.get(f) or "").strip() for f in fields)


def _build_user_prompt(row: dict) -> str:
    parts = [
        f"ADDRESS: {row.get('ADDRESS', '').strip()}",
        f"ZIP: {row.get('ZIP', '').strip()}",
        f"city: {row.get('city', '').strip()}",
        f"district: {row.get('district', '').strip()}",
        f"locality: {row.get('locality', '').strip()}",
        f"building_number: {row.get('building_number', '').strip()}",
    ]
    return "\n".join(p for p in parts if p.split(": ", 1)[-1])


def _parse_llm_json(content: str) -> dict:
    text = (content or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    return data


def _call_groq(client, model: str, user_prompt: str) -> tuple[dict, dict]:
    raw = client.chat.completions.with_raw_response.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        max_tokens=80,
    )
    headers = {k.lower(): v for k, v in raw.headers.items()}
    content = raw.parse().choices[0].message.content or "{}"
    return _parse_llm_json(content), headers


class DailyLimitReached(Exception):
    """Groq daily request/token quota exhausted."""


def _check_headers(headers: dict, stats: dict) -> None:
    for key in ("x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens"):
        if key in headers:
            stats[key] = headers[key]
    remaining = headers.get("x-ratelimit-remaining-requests")
    if remaining is not None and int(remaining) <= 0:
        raise DailyLimitReached("daily request limit reached")


def _load_checkpoint() -> dict[int, dict]:
    if not CHECKPOINT_CSV.exists():
        return {}
    out: dict[int, dict] = {}
    with CHECKPOINT_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[int(row["line"])] = row
    return out


def _save_checkpoint(rows: dict[int, dict]) -> None:
    if not rows:
        return
    fieldnames = ["line", "building_name_llm", "landmark_llm", "llm_model"]
    with CHECKPOINT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for line in sorted(rows):
            w.writerow(rows[line])


def _write_merged_output(all_rows: list[dict], checkpoint: dict[int, dict]) -> None:
    out_fields = list(all_rows[0].keys()) + [
        "building_name_llm",
        "landmark_llm",
        "building_name_final",
        "landmark_final",
        "llm_model",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
        w.writeheader()
        for i, row in enumerate(all_rows, start=2):
            extra = checkpoint.get(i, {})
            merged = dict(row)
            merged["building_name_llm"] = extra.get("building_name_llm", "")
            merged["landmark_llm"] = extra.get("landmark_llm", "")
            merged["llm_model"] = extra.get("llm_model", "")
            merged["building_name_final"] = (row.get("building_name") or "").strip() or merged["building_name_llm"]
            merged["landmark_final"] = (row.get("landmark") or "").strip() or merged["landmark_llm"]
            w.writerow(merged)


def run(
    *,
    limit: int | None,
    fields: list[str],
    model: str,
    dry_run: bool,
    resume: bool,
    exhaust: bool,
    rpm: float,
) -> None:
    try:
        from groq import Groq
    except ImportError as exc:
        raise SystemExit("Install groq: pip install groq") from exc

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key and not dry_run:
        raise SystemExit(
            "Set GROQ_API_KEY in your environment.\n"
            "Example: export GROQ_API_KEY='gsk_...'"
        )

    if not INPUT_CSV.exists():
        raise SystemExit(f"Missing {INPUT_CSV}. Run Address.py first.")

    with INPUT_CSV.open(newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))
    all_rows = all_rows[:56760]

    targets: list[tuple[int, dict]] = []
    for i, row in enumerate(all_rows, start=2):
        if _needs_backfill(row, fields):
            targets.append((i, row))
    if limit:
        targets = targets[:limit]

    if limit:
        targets = targets[:limit]
    elif exhaust:
        limit = None  # process until quota stops us

    checkpoint = _load_checkpoint() if resume else {}
    client = None if dry_run else Groq(api_key=api_key)
    sleep_s = 60.0 / rpm if rpm > 0 else 0

    stats: dict = {"called": 0, "filled_bn": 0, "filled_lm": 0, "errors": 0, "skipped": 0}
    t0 = time.perf_counter()
    stopped = ""

    for idx, (line_no, row) in enumerate(targets):
        if line_no in checkpoint:
            stats["skipped"] += 1
            continue

        user_prompt = _build_user_prompt(row)
        result = {"building_name_llm": "", "landmark_llm": "", "llm_model": model}

        if dry_run:
            print(f"[dry-run] line {line_no}: would call Groq")
        else:
            try:
                for attempt in range(6):
                    try:
                        data, headers = _call_groq(client, model, user_prompt)
                        _check_headers(headers, stats)
                        bn = data.get("building_name")
                        lm = data.get("landmark")
                        if isinstance(bn, str) and bn.strip() and bn.strip().lower() != "null":
                            result["building_name_llm"] = bn.strip()
                            stats["filled_bn"] += 1
                        if isinstance(lm, str) and lm.strip() and lm.strip().lower() != "null":
                            result["landmark_llm"] = lm.strip()
                            stats["filled_lm"] += 1
                        stats["called"] += 1
                        break
                    except DailyLimitReached:
                        raise
                    except Exception as exc:
                        msg = str(exc).lower()
                        if "429" in msg or "rate" in msg:
                            wait = min(60, 2 ** attempt)
                            print(f"rate limit line {line_no}, sleep {wait}s", file=sys.stderr)
                            time.sleep(wait)
                            continue
                        stats["errors"] += 1
                        print(f"error line {line_no}: {exc}", file=sys.stderr)
                        break
            except DailyLimitReached as exc:
                stopped = str(exc)
                checkpoint[line_no] = {"line": line_no, **result}
                _save_checkpoint(checkpoint)
                break

            checkpoint[line_no] = {"line": line_no, **result}
            if stats["called"] % 50 == 0:
                _save_checkpoint(checkpoint)
                _write_merged_output(all_rows, checkpoint)
                rem = stats.get("x-ratelimit-remaining-requests", "?")
                print(f"... {stats['called']} calls, remaining quota ~{rem}", file=sys.stderr)
            if sleep_s:
                time.sleep(sleep_s)

        if dry_run and idx >= 4:
            break

    if not dry_run:
        _save_checkpoint(checkpoint)
        _write_merged_output(all_rows, checkpoint)

    elapsed = time.perf_counter() - t0
    print(f"Model: {model}")
    print(f"Targets needing LLM: {len(targets)}")
    print(f"API calls: {stats['called']}  skipped(resume): {stats['skipped']}  errors: {stats['errors']}")
    print(f"LLM filled building_name: {stats['filled_bn']}  landmark: {stats['filled_lm']}")
    if stats.get("x-ratelimit-remaining-requests"):
        print(f"Remaining Groq requests (header): {stats['x-ratelimit-remaining-requests']}")
    if stopped:
        print(f"Stopped: {stopped}")
    print(f"Elapsed: {elapsed:.1f}s")
    if not dry_run:
        print(f"Output: {OUTPUT_CSV}")
        print(f"Checkpoint: {CHECKPOINT_CSV}")


def main() -> None:
    p = argparse.ArgumentParser(description="Groq LLM backfill for empty address fields")
    p.add_argument("--limit", type=int, default=None, help="Max rows to send to Groq")
    p.add_argument("--fields", default="building_name", help="Comma list: building_name,landmark")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Groq model id")
    p.add_argument("--dry-run", action="store_true", help="No API calls")
    p.add_argument("--no-resume", action="store_true", help="Ignore checkpoint file")
    p.add_argument("--exhaust", action="store_true", help="Run until Groq daily quota is exhausted")
    p.add_argument("--rpm", type=float, default=DEFAULT_RPM, help="Max requests per minute pacing")
    args = p.parse_args()
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    run(
        limit=args.limit,
        fields=fields,
        model=args.model,
        dry_run=args.dry_run,
        resume=not args.no_resume,
        exhaust=args.exhaust,
        rpm=args.rpm,
    )


if __name__ == "__main__":
    main()
