#!/usr/bin/env python3
"""Build dotcom.project_pincode_master.csv: every dotcom row gets a pincode.

Per (city, subLocation) pair, first hit wins:
  office_exact   subLocation == India Post office name inside the city's pin region
  combo_learned  pins whose COMBO_DEMOG addresses write this subLocation (Bangalore only)
  office_fuzzy   strict fuzzy (ratio >= 90) office name in the city's own district
  geocode_postcode / geocode_nearest_po
                 OpenStreetMap Nominatim search "subLocation, city, India", retried as
                 "subLocation, state, India" (1 req/s, cached); city-level hits rejected;
                 postcode from the result, else nearest post office to the point
  city_default   post office nearest the city centre (low confidence)
UAE cities (no postal codes) stay blank with source "no_postal_code".

Usage:  python build_dotcom_pincode_master.py [--offline]
        --offline: geocode only city centres; per-subLocation lookups come from cache only
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import Address as A  # noqa: E402

DOTCOM_CSV = ROOT / "dotcom.project.csv"
OUT_CSV = ROOT / "dotcom.project_pincode_master.csv"
PINCODES_JSON = ROOT / "bharataddress" / "bharataddress" / "data" / "pincodes.json"
CACHE_JSON = ROOT / "nominatim_cache.json"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "squareyards-dotcom-pincode-master/1.0 (one-off locality lookup)"
CITY_RADIUS_KM = 40.0
NO_POSTAL_CITIES = {"dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "umm al quwain", "fujairah"}
CONFIDENCE = {
    "office_exact": "high",
    "combo_learned": "high",
    "office_fuzzy": "medium",
    "geocode_postcode": "medium",
    "geocode_nearest_po": "low",
    "city_default": "very_low",
    "no_postal_code": "",
}
# Nominatim hits this coarse only name the whole city/district, not the subLocation.
AREA_TOO_BROAD = {"city", "state", "state_district", "county", "country", "region", "province"}
_SUFFIX_RE = re.compile(r"\s+(?:S\.?O|B\.?O|H\.?O|G\.?P\.?O)\b.*$|\s*\([^)]*\)", re.IGNORECASE)


def norm(text: str) -> str:
    return A._triplet_norm(_SUFFIX_RE.sub("", text or ""))


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


class Geocoder:
    def __init__(self, offline: bool) -> None:
        self.offline = offline
        self.cache: dict[str, list] = json.loads(CACHE_JSON.read_text()) if CACHE_JSON.exists() else {}
        self.last = 0.0
        self.calls = 0

    def search(self, query: str, *, allow_online: bool = True) -> list:
        if query in self.cache or self.offline and not allow_online:
            return self.cache.get(query, [])
        wait = 1.1 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        url = NOMINATIM + "?" + urllib.parse.urlencode(
            {"q": query, "format": "jsonv2", "addressdetails": 1, "limit": 3, "countrycodes": "in"}
        )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except Exception as exc:  # network hiccup: don't cache, retry next run
            print(f"  geocode failed {query!r}: {exc}", file=sys.stderr)
            self.last = time.time()
            return []
        self.last = time.time()
        self.cache[query] = result
        self.calls += 1
        if self.calls % 20 == 0:
            self.save()
        return result

    def save(self) -> None:
        CACHE_JSON.write_text(json.dumps(self.cache))


def main() -> None:
    offline = "--offline" in sys.argv
    geo = Geocoder(offline)
    pins: dict[str, dict] = json.loads(PINCODES_JSON.read_text())
    pin_ll = {p: (v["latitude"], v["longitude"]) for p, v in pins.items() if v.get("latitude")}

    offices: dict[str, set[str]] = {}
    for p, v in pins.items():
        for office in v.get("offices", []):
            offices.setdefault(norm(office), set()).add(p)
    for p, locs in A._load_pincode_localities().items():
        for loc in locs:
            offices.setdefault(norm(loc), set()).add(p)

    rows = list(csv.DictReader(DOTCOM_CSV.open(encoding="utf-8")))
    pairs = sorted({(r["projectData.city"].strip(), r["projectData.subLocation"].strip()) for r in rows})

    # COMBO-learned pins per Bangalore subLocation base, most-written pin first.
    combo_counts: dict[str, Counter] = {}
    pin_totals: Counter = Counter()
    for r in csv.DictReader((ROOT / "COMBO_DEMOG.csv").open(encoding="utf-8")):
        zip_code = (r["ZIP"] or "").strip()
        sub = A._find_subloc_in_text(A._triplet_locality_text(r["ADDRESS"]), "bangalore")
        if sub and zip_code in pins:
            combo_counts.setdefault(A._SUBLOC_TAIL_RE.sub("", sub), Counter())[zip_code] += 1
            pin_totals[zip_code] += 1
    combo_pins = {
        base: [p for p, n in cnt.most_common() if n >= A.PIN_SUBLOC_MIN_COUNT and n / pin_totals[p] >= A.PIN_SUBLOC_MIN_SHARE]
        for base, cnt in combo_counts.items()
    }

    # City region = pins within CITY_RADIUS_KM of the geocoded centre, plus same-district pins.
    city_center: dict[str, tuple[float, float]] = {}
    city_pins: dict[str, set[str]] = {}
    city_core_district: dict[str, str] = {}
    city_state: dict[str, str] = {}
    for city in sorted({c for c, _ in pairs}):
        if city.lower() in NO_POSTAL_CITIES:
            continue
        hits = geo.search(f"{city}, India", allow_online=True)
        if not hits:
            continue
        center = (float(hits[0]["lat"]), float(hits[0]["lon"]))
        city_state[city] = (hits[0].get("address") or {}).get("state", "")
        near = {p for p, ll in pin_ll.items() if km(center, ll) <= CITY_RADIUS_KM}
        districts = {pins[p]["district"] for p in near}
        city_center[city] = center
        city_pins[city] = near | {p for p, v in pins.items() if v["district"] in districts}
        core = min(((km(center, pin_ll[p]), p) for p in near), default=None)
        if core:
            city_core_district[city] = pins[core[1]]["district"]
    print(f"cities with a pin region: {len(city_pins)}", file=sys.stderr)

    def nearest_pin(point: tuple[float, float], region: set[str]) -> str | None:
        best = min(((km(point, pin_ll[p]), p) for p in region if p in pin_ll), default=None)
        return best[1] if best else None

    city_office_cache: dict[str, tuple[dict[str, set[str]], dict[str, str]]] = {}

    def region_office_index(city: str, region: set[str]) -> tuple[dict[str, set[str]], dict[str, str]]:
        if city not in city_office_cache:
            by_office = {o: ps & region for o, ps in offices.items() if ps & region}
            city_office_cache[city] = (by_office, {o.replace(" ", ""): o for o in by_office})
        return city_office_cache[city]

    def in_core_first(city: str, found: set[str]) -> list[str]:
        """Pins in the city's own district first; drop other districts if any core pin exists."""
        core = city_core_district.get(city)
        own = sorted(p for p in found if pins[p]["district"] == core)
        return own or sorted(found)

    resolved: dict[tuple[str, str], tuple[list[str], str]] = {}
    for idx, (city, sub) in enumerate(pairs):
        if city.lower() in NO_POSTAL_CITIES:
            resolved[(city, sub)] = ([], "no_postal_code")
            continue
        region = city_pins.get(city, set())
        forms = [f for f in dict.fromkeys([norm(sub), A._SUBLOC_TAIL_RE.sub("", norm(sub)),
                                           re.sub(r"\s+(?:MAIN\s+)?ROAD$", "", norm(sub))]) if len(f) >= 3]

        found: list[str] = []
        for form in forms:
            found = in_core_first(city, offices.get(form, set()) & region)
            if found:
                break
        if found:
            resolved[(city, sub)] = (found, "office_exact")
            continue

        base = A._SUBLOC_TAIL_RE.sub("", A._triplet_norm(sub))
        learned = combo_pins.get(base, []) if A._norm_project_city(city) == "bangalore" else []
        if learned:
            resolved[(city, sub)] = (learned, "combo_learned")
            continue

        region_offices, compacts = region_office_index(city, region)
        if forms and len(forms[0].replace(" ", "")) >= 6 and compacts:
            hit = process.extractOne(forms[0].replace(" ", ""), list(compacts), scorer=fuzz.ratio, score_cutoff=90)
            if hit:
                fuzzy_pins = region_offices[compacts[hit[0]]]
                core = city_core_district.get(city)
                if core and any(pins[p]["district"] == core for p in fuzzy_pins):
                    resolved[(city, sub)] = (in_core_first(city, fuzzy_pins), "office_fuzzy")
                    continue

        center = city_center.get(city)
        queries = [f"{sub}, {city}, India"]
        if city_state.get(city):
            queries.append(f"{sub}, {city_state[city]}, India")
        results = []
        for query in queries if sub and center else []:
            results = [
                res for res in geo.search(query, allow_online=False)
                if res.get("addresstype") not in AREA_TOO_BROAD
                and norm(res.get("name", "")) != norm(city)
            ]
            if results:
                break
        if results:
            for result in results:
                point = (float(result["lat"]), float(result["lon"]))
                if km(point, center) > CITY_RADIUS_KM + 15:
                    continue
                postcode = re.sub(r"\s", "", (result.get("address") or {}).get("postcode", ""))
                if postcode in pins:
                    resolved[(city, sub)] = ([postcode], "geocode_postcode")
                    break
                near = nearest_pin(point, region or set(pin_ll))
                if near:
                    resolved[(city, sub)] = ([near], "geocode_nearest_po")
                    break
            if (city, sub) in resolved:
                continue

        near = nearest_pin(center, region) if center else None
        resolved[(city, sub)] = ([near] if near else [], "city_default" if near else "unresolved")
        if (idx + 1) % 200 == 0:
            print(f"... {idx + 1}/{len(pairs)} pairs, {geo.calls} geocoder calls", file=sys.stderr)
    geo.save()

    fields = list(rows[0].keys()) + ["pincode", "all_pincodes", "pincode_source", "pincode_confidence"]
    stats: Counter = Counter()
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            found, source = resolved[(r["projectData.city"].strip(), r["projectData.subLocation"].strip())]
            r["pincode"] = found[0] if found else ""
            r["all_pincodes"] = ";".join(found)
            r["pincode_source"] = source
            r["pincode_confidence"] = CONFIDENCE.get(source, "")
            writer.writerow(r)
            stats[source] += 1
    pair_stats = Counter(src for _, src in resolved.values())
    print(f"rows: {len(rows)}  pairs: {len(pairs)}")
    for source in CONFIDENCE:
        print(f"  {source:20s} rows {stats.get(source, 0):7d}   pairs {pair_stats.get(source, 0):5d}")
    if stats.get("unresolved"):
        print(f"  {'unresolved':20s} rows {stats['unresolved']:7d}   pairs {pair_stats['unresolved']:5d}")
    print(f"Wrote {OUT_CSV}")


if __name__ == "__main__":
    main()
