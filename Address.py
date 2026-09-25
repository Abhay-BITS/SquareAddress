#!/usr/bin/env python3
"""Credit-bureau address parsing pipeline (Square Yards / COMBO_DEMOG).

Flow for each address string:
  1. bureau_preprocess() — fix OCR/spacing noise before segmentation.
  2. bharataddress.parse() — baseline building_number, building_name, locality, etc.
  3. parse_address_row() — enrich building_name (dotcom > OSM > heuristics) and locality.
  4. _build_parsed_output_row() — dotcom triplet: city-scoped subLocation in address, then
     project among those rows; fallback validates building_name + city + subLocation.

Reference data (lazy-loaded on first use):
  - dotcom.project.csv          Square Yards canonical project names (city-scoped).
  - Pincode To Locality Mapping.csv
  - india_location_master.csv   OSM buildings (Bangalore rows only in loader).

Outputs (see OUTPUT_COLS): structured fields plus dotcom_matched / location_matched /
confidence. dotcom_matched is Yes only when projectName + city + subLocation align with
one row in dotcom.project.csv; matched rows overwrite building_name, city, and locality
with those canonical CSV values.

CLI (see block at bottom):
  python Address.py                         → process_csv() on COMBO_DEMOG.csv
  python Address.py --ner                   → same plus the NER stage for empty fields

Mumbai Imp dual-address parsing lives in process_mumbai_imp.py (imports helpers here).
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent
# Local bharataddress package: _vendor/ or bharataddress/ next to this script.
for pkg_root in (ROOT / "_vendor", ROOT / "bharataddress"):
    if (pkg_root / "bharataddress" / "__init__.py").exists():
        sys.path.insert(0, str(pkg_root))
        break

from bharataddress import parse, pincode  # noqa: E402  — rule-based Indian address parser
from bharataddress import phonetic  # noqa: E402  — fuzzy place strings when RapidFuzz absent

import preprocess  # noqa: E402  — bureau text clean-up (see preprocess.py)
import ner_stage  # noqa: E402  — optional model stage for fields rules leave empty

USE_NER = False  # set by --ner; fills empty building_name/locality only

# ---------------------------------------------------------------------------
# Paths, thresholds, and lazy-loaded dictionary caches (_PROJECT_*, _LOCATION_*, …)
# ---------------------------------------------------------------------------

# Fuzzy thresholds — tuned for bureau OCR noise, not loose guessing.
FUZZY_LOCALITY_CUTOFF = 0.82
FUZZY_PLACE_CUTOFF = 0.85

# Default batch paths (override via --input / --output for Excel runs).
INPUT_CSV = ROOT / "COMBO_DEMOG.csv"
OUTPUT_CSV = ROOT / "COMBO_DEMOG_parsed.csv"
REPORT_TXT = ROOT / "cross_check_report.txt"
PROJECT_CSV = ROOT / "dotcom.project.csv"
PINCODE_LOCALITY_CSV = ROOT / "Pincode To Locality  Mapping.csv"
PINCODE_LOCALITY_MERGED_CSV = ROOT / "pincode_locality_merged.csv"  # Pincode.xlsx + post offices
DOTCOM_PINCODE_MASTER_CSV = ROOT / "dotcom.project_pincode_master.csv"  # subLocation -> pincodes
LOCATION_MASTER_CSV = ROOT.parent / "india_location_db" / "data" / "india_location_master.csv"
BLR_ZIP_PREFIX = "560"  # primary Bangalore pin prefix; BLR_PIN_PREFIXES adds 561/562

# dotcom city labels -> lookup key for city-scoped project matching
PROJECT_CITY_NORM: dict[str, str] = {
    "bengaluru": "bangalore",
    "bangalore": "bangalore",
    "gurugram": "gurgaon",
    "gurgaon": "gurgaon",
    "mumbai": "mumbai",
    "bombay": "mumbai",
    "new delhi": "delhi",
    "delhi": "delhi",
    "ncr": "delhi",
    "navi mumbai": "navi mumbai",
    "greater noida": "noida",
    "noida": "noida",
    "pune": "pune",
}


class _DotcomRecord(NamedTuple):
    """One dotcom.project.csv row: project + city + subLocation kept together."""

    projectName: str
    city: str
    city_key: str
    subLocation: str
    norm_name: str
    norm_subloc: str


_PROJECT_INDEX: dict[str, dict[str, str]] | None = None
_PROJECT_GLOBAL: dict[str, str] | None = None
_PROJECT_BY_BRAND: dict[str, list[tuple[str, str, str]]] | None = None
_PROJECT_CANONICAL: set[str] | None = None
_PROJECT_COLLAPSED_BY_BRAND: dict[str, list[tuple[str, str]]] | None = None
_DOTCOM_RECORDS: list[_DotcomRecord] | None = None
_PINCODE_LOCALITIES: dict[str, list[str]] | None = None
_PIN_LOCALITY_NORMS: dict[str, set[str]] | None = None
_PIN_CITY: dict[str, str] | None = None
_SUBLOC_PINCODES: dict[tuple[str, str], set[str]] | None = None
_LOCATION_BUILDINGS: dict[str, str] | None = None
_LOCATION_BY_BRAND: dict[str, list[str]] | None = None
_LOCATION_BUILDINGS_SORTED: list[tuple[str, str]] | None = None
_LOCATION_CANONICAL: set[str] | None = None
_BLR_LOCALITY_GAZETTEER: dict[str, str] | None = None
_BLR_LOCALITY_SORTED: list[tuple[str, str]] | None = None
_BLR_LOCALITY_CANONICAL: list[str] | None = None
_COMMON_LOCALITY_BLOCK: frozenset[str] | None = None  # generic tokens to drop from locality-like names

# OSM location master: which entity_type values count as "building" vs skip (roads, hospitals, …)
LOCATION_ENTITY_TYPES = frozenset(
    {"apartment", "residential_complex", "building", "commercial_complex", "tower", "mall"}
)
LOCATION_SKIP_TYPES = frozenset(
    {
        "road", "hospital", "temple", "school", "college", "office", "hotel", "park",
        "bus_stop", "police", "bank", "atm", "pharmacy", "place_of_worship", "clinic",
    }
)
LOCATION_MIN_CONF = 0.50
LOCATION_NAME_BLOCK_RE = re.compile(
    r"\b(ROAD|MAIN ROAD|HIGHWAY|FLYOVER|JUNCTION|CIRCLE|SQUARE)\b",
    re.IGNORECASE,
)
BLR_PIN_PREFIXES = ("560", "561", "562")

FUZZY_PROJECT_CUTOFF = 0.88
FUZZY_PROJECT_BACKFILL_CUTOFF = 0.90
PROJECT_BRAND_MIN_COUNT = 15
PROJECT_BRAND_STOP: frozenset[str] = frozenset(
    {
        "THE", "AND", "NEW", "SRI", "SHRI", "URBAN", "INDIA", "CITY", "TOWN", "PARK",
        "GARDEN", "GARDENS", "HEIGHTS", "TOWER", "TOWERS", "HOUSE", "HOMES", "HOME",
        "VILLA", "VILLAS", "BLOCK", "PHASE", "SECTOR", "WEST", "EAST", "NORTH", "SOUTH",
        "MAIN", "CROSS", "ROAD", "NAGAR", "COLONY", "LAYOUT", "METRO", "CENTRAL", "ROYAL",
        "GREEN", "GOLD", "GRAND", "SUPREME", "PRIME", "LUXURY", "RESIDENCY", "APARTMENT",
        "APARTMENTS", "COMPLEX", "ENCLAVE", "WORLD", "ONE", "TWO", "THREE", "LAKE", "HILL",
        "HILLS", "VIEW", "LIFE", "LIVING", "SPACE", "CREST", "CREEK", "POINT", "PLAZA",
        "SQUARE", "COURT", "MANSION", "PALACE", "TEMPLE", "SCHOOL", "MASJID", "CHURCH",
        "BANGALORE", "BENGALURU", "MUMBAI", "KARNATAKA", "DELHI", "GURGAON", "GURUGRAM",
        "HYDERABAD", "PUNE", "CHENNAI", "NOIDA", "THANE", "WESTEND", "TOTAL", "ENVIRONMENT",
        "ASIAN", "SUN", "F", "B", "KA", "NO", "FLAT", "PLOT", "SHOP", "DOOR",
    }
)
PROJECT_OCR_BOUNDARY_RE = (
    re.compile(r"(\d)([A-Z])"),
    re.compile(r"([A-Z])(\d)"),
)
ZIP_FROM_TEXT_RE = re.compile(r"\b([1-9]\d{5})\b")

# Columns produced by parse_address_row (confidence added in _build_parsed_output_row).
PARSED_COLS = (
    "building_number",
    "building_name",
    "landmark",
    "locality",
    "city",
    "district",
    "confidence",
)
DOTCOM_MATCH_COL = "dotcom_matched"
DOTCOM_RULE_COL = "dotcom_match_rule"  # which of the three project/locality/pincode checks passed
LOCATION_MATCH_COL = "location_matched"
# Written to CSV: parsed fields + Yes/No flags (not part of parse_address_row return dict).
OUTPUT_COLS = (
    "building_number",
    "building_name",
    DOTCOM_MATCH_COL,
    DOTCOM_RULE_COL,
    LOCATION_MATCH_COL,
    "landmark",
    "locality",
    "locality_source",
    "city",
    "district",
    "confidence",
)

# ---------------------------------------------------------------------------
# Regex patterns and bureau text normalisation tables (OCR_FIXES, STATE_ABBREV)
# ---------------------------------------------------------------------------

# Bureau OCR / spacing artefacts seen in credit-bureau dumps.
OCR_FIXES: dict[str, str] = {
    "R OAD": "ROAD",
    "G ROAD": "GROAD",
    "B ANGALORE": "BANGALORE",
    "B ENGALURU": "BENGALURU",
    "O BENGALURU": "BENGALURU",
    "E BANGALORE": "EAST BANGALORE",
    "B ANGALO RE": "BANGALORE",
    "BANGALOR E": "BANGALORE",
    "BANG ALORE": "BANGALORE",
    "BANGALO RE": "BANGALORE",
    "WEST BANGALO RE": "WEST BANGALORE",
    "MADURAI NA DU": "MADURAI NADU",
    "HOSU R": "HOSUR",
    "MAHADEV APURA": "MAHADEVAPURA",
    "VETERNITY": "VETERINARY",
    "N AGAR": "NAGAR",
    "P NAGAR": "PNAGAR",
    "R PURAM": "RPURAM",
}

STATE_ABBREV: dict[str, str] = {
    "AN": "Andaman and Nicobar Islands",
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CG": "Chhattisgarh",
    "CH": "Chandigarh",
    "CT": "Chhattisgarh",
    "DD": "Daman and Diu",
    "DL": "Delhi",
    "DN": "Dadra and Nagar Haveli and Daman and Diu",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HP": "Himachal Pradesh",
    "HR": "Haryana",
    "JH": "Jharkhand",
    "JK": "Jammu and Kashmir",
    "KA": "Karnataka",
    "KL": "Kerala",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "MH": "Maharashtra",
    "ML": "Meghalaya",
    "MN": "Manipur",
    "MP": "Madhya Pradesh",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OD": "Odisha",
    "OR": "Odisha",
    "PB": "Punjab",
    "PY": "Puducherry",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TN": "Tamil Nadu",
    "TR": "Tripura",
    "TS": "Telangana",
    "UK": "Uttarakhand",
    "UL": "Uttarakhand",
    "UP": "Uttar Pradesh",
    "UT": "Uttarakhand",
    "WB": "West Bengal",
}

TRAILING_GEO_RE = re.compile(
    r"\b(?:KARNATAKA|TAMIL NADU|ANDHRA PRADESH|MAHARASHTRA|KERALA|TELANGANA|"
    r"UTTAR PRADESH|WEST BENGAL|BIHAR|GUJARAT|RAJASTHAN|MADHYA PRADESH|ODISHA|"
    r"JHARKHAND|HARYANA|DELHI|PUNJAB|INDIA|UTTARAKHAND|ASSAM|CHHATTISGARH|GOA|"
    r"HIMACHAL PRADESH|PUDUCHERRY|LADAKH)\b.*$",
    re.IGNORECASE,
)
ADDRESSEE_RE = re.compile(
    r"^(?:S/O|C/O|D/O|W/O|SON OF|DAUGHTER OF|WIFE OF|CARE OF)\s+"
    r".+?(?=\s+(?:FLAT|NO\.?\s*\d|NO\s+\d|H\.?\s*NO|HOUSE|PLOT|DOOR|SHOP|#\d|[A-Z]?\d{2,}))",
    re.IGNORECASE,
)
LANDMARK_SPLIT_RE = re.compile(
    r"\s+(?=(?:NEAR|BEHIND|OPPOSITE|OPP\.|BESIDE|NEXT TO|IN FRONT OF|ADJACENT TO)\b)",
    re.IGNORECASE,
)
BUILDING_SPLIT_RE = re.compile(
    r"\s+(?=(?:FLAT\s+NO\.?|FLAT|H\.?\s*NO\.?|HOUSE\s+NO\.?|PLOT\s+NO\.?|"
    r"DOOR\s+NO\.?|SHOP\s+NO\.?|#\d)\b)",
    re.IGNORECASE,
)
PIN_IN_TEXT_RE = re.compile(r"\b[1-8]\d{5}\b")
BUILDING_NUMBER_RES = (
    re.compile(r"\bNO\.?\s*(\d+[A-Z]?(?:[/-]\d+[A-Z]?)?)\b", re.IGNORECASE),
    re.compile(
        r"(?:FLAT|HOUSE|H|PLOT|DOOR|SHOP|APT|APARTMENT|ROOM|UNIT|OFFICE)\s*"
        r"(?:NO\.?|NUMBER|NUM)\s*[:#.\-]?\s*([A-Z]{0,3}\d+[A-Z]?(?:[/-]\d+[A-Z]?)?)",
        re.IGNORECASE,
    ),
    re.compile(r"^([A-Z]{1,3}-?\d+[A-Z]?)(?:\s|,|$)", re.IGNORECASE),
    re.compile(r"^(\d{1,4}(?:/\d+)?)(?:\s|,|$)"),
)
BUILDING_NAME_TRIM_RE = re.compile(
    r"^(.*?\b(?:APARTMENT|APARTMENTS|APTS|TOWER|TOWERS|SOCIETY|COMPLEX|ENCLAVE|"
    r"HEIGHTS|RESIDENCY|RESIDENCES|VILLA|VILLAS|PLAZA|COURT|MANSION|ARCADE|SQUARE|"
    r"ORCHIDS|SUNFLOWER|FORTUNA|ATLANTIS|MEADOWS|NIVAS|HOMES|PALACE|CHAMBERS|"
    r"HERITAGE|REGENCY|CHALET|CHALETS|CASTLE|COUNTOUR|HABITAT|SEASONS|SPRINGS|"
    r"NILAYA|NILAYAM|BHAVAN|BHAVANA|KUTEER|KUNJ|APPT|APPTS|NEST|NESTS))\b",
    re.IGNORECASE,
)
BUILDING_SUFFIX = (
    r"APARTMENT|APARTMENTS|APTS|TOWER|TOWERS|SOCIETY|COMPLEX|ENCLAVE|HEIGHTS|"
    r"RESIDENCY|RESIDENCES|VILLA|VILLAS|PLAZA|COURT|MANSION|ARCADE|SQUARE|"
    r"ORCHIDS|SUNFLOWER|FORTUNA|ATLANTIS|MEADOWS|NIVAS|HOMES|PALACE|CHAMBERS|"
    r"HERITAGE|REGENCY|CHALET|CHALETS|CASTLE|COUNTOUR|HABITAT|SEASONS|SPRINGS|"
    r"NILAYA|NILAYAM|BHAVAN|BHAVANA|KUTEER|KUNJ|APPT|APPTS|NEST|NESTS"
)
LOCALITY_STREET_TOKENS: frozenset[str] = frozenset(
    {
        "MAIN", "CROSS", "ROAD", "BLOCK", "FLOOR", "STAGE", "PHASE", "SECTOR",
        "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH", "NORTH", "SOUTH", "EAST", "WEST",
    }
)
LOCALITY_FALLBACK_SUFFIXES: tuple[str, ...] = (
    "LAYOUT", "COLONY", "GARDEN", "NAGAR", "PURAM", "PURA", "HALLI",
    "EXTENSION", "ENCLAVE", "TOWNSHIP",
)
SINGLE_AREA_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{4,20}(?:PURAM|PURA|HALLI|LAYOUT|COLONY|GARDEN|NAGAR|ENCLAVE|ESTATE|TOWNSHIP))\b",
    re.IGNORECASE,
)
TWO_WORD_LAYOUT_RE = re.compile(r"\b([A-Z]{2,10}\s+LAYOUT)\b", re.IGNORECASE)
BEFORE_FLOOR_BUILDING_RE = re.compile(
    r"\b((?:[A-Z][A-Z0-9&.-]+\s+){1,5}[A-Z][A-Z0-9&.-]+)\s+"
    r"(?:GROUND|\d+(?:ST|ND|RD|TH))\s+FLOOR\b",
    re.IGNORECASE,
)
LAYOUT_JUNK_RE = re.compile(r"^[A-Z]{1,2}$|^\d+[A-Z]$", re.IGNORECASE)
LAYOUT_UNIT_RE = re.compile(
    r"^\d+[A-Z]?$|^NO\d*$|^\d+/\d+$|^[A-Z]-?\d+$",
    re.IGNORECASE,
)
LAYOUT_PRE_RE = re.compile(r"(\S+(?:\s+\S+){0,6})\s+LAYOUT\b", re.IGNORECASE)
ORDINAL_WORD_RE = re.compile(r"^\d+(?:ST|ND|RD|TH)$", re.IGNORECASE)
SUFFIX_ONLY_WORDS: frozenset[str] = frozenset(
    {
        "VILLA", "VILLAS", "APARTMENT", "APARTMENTS", "APTS", "TOWER", "TOWERS",
        "SOCIETY", "COMPLEX", "ENCLAVE", "HEIGHTS", "RESIDENCY", "RESIDENCES",
        "PLAZA", "COURT", "MANSION", "ARCADE", "SQUARE", "MEADOWS", "NIVAS",
        "HOMES", "PALACE", "CHAMBERS", "APPT", "APPTS", "NEST", "NESTS", "LAYOUT",
    }
)
COMPOUND_BUILDING_RE = re.compile(
    r"\b([A-Z][A-Z0-9]{1,20}(?:VILLA|VILLAS|NILAYA|NIVAS|HEIGHTS|TOWERS|APPT|NESTS?))\b",
    re.IGNORECASE,
)
BUILDING_NAME_RE = re.compile(
    rf"((?:[\w&.-]+\s+){{0,5}}(?:{BUILDING_SUFFIX}))\b",
    re.IGNORECASE,
)
EXTENDED_BUILDING_RE = BUILDING_NAME_RE
AFTER_UNIT_BUILDING_RE = re.compile(
    r"(?:FLAT|FLATNO|HOUSE|HNO|NO|APT|APARTMENT|UNIT|ROOM|OFFICE|PLOT|#|B)\s*[-#.]?\s*"
    r"[A-Z0-9/-]+\s+((?:[A-Z][A-Z0-9&.-]+\s+){1,6}[A-Z][A-Z0-9&.-]+)",
    re.IGNORECASE,
)
STREET_START_RE = re.compile(
    r"\b\d+(?:ST|ND|RD|TH)\s+(?:CROSS|MAIN|BLOCK)|\b\d+(?:ST|ND|RD|TH)\s+MAIN|"
    r"\bGROUND\s+FLOOR|\b\d+(?:ST|ND|RD|TH)\s+STAGE|\b\d+(?:ST|ND|RD|TH)\s+FLOOR",
    re.IGNORECASE,
)
STREET_WORDS: frozenset[str] = frozenset(
    {"CROSS", "MAIN", "ROAD", "BLOCK", "FLOOR", "STAGE", "PHASE", "SECTOR"}
)
SINGLE_WORD_LAYOUT_RE = re.compile(r"\b([A-Z][A-Z0-9]{4,20})\s+LAYOUT\b", re.IGNORECASE)
SOCIETY_BUILDING_RE = re.compile(
    r"\b((?:[A-Z][A-Z0-9\-]+\s+){1,4}(?:NAGAR|COLONY|GARDEN|GARDENS|PURAM|PURA|HALLI|PET|PALYA))\b",
    re.IGNORECASE,
)
BLDG_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9 \-]{2,40})\s+BLDG\b", re.IGNORECASE)
RESIDENCE_NAME_RE = re.compile(
    r"\b((?:[A-Z][A-Z0-9\-]+\s+){0,4}RESIDENCE)\b",
    re.IGNORECASE,
)
BUILDING_STOP_TOKENS: frozenset[str] = frozenset(
    {
        "BANGALORE", "BENGALURU", "KARNATAKA", "INDIA", "MAIN", "CROSS", "ROAD",
        "FLOOR", "NEAR", "BEHIND", "PHASE", "SECTOR", "BLOCK", "WEST", "EAST",
        "NORTH", "SOUTH", "FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH",
        "GROUND", "BASEMENT", "PLOT", "DOOR", "SHOP", "FLAT", "NO", "UNIT",
    }
)
BUILDING_REJECT_WORDS: frozenset[str] = frozenset(
    {
        "PVT", "LTD", "LIMITED", "PRIVATE", "LLP", "INC", "CORP", "CORPORATION",
        "TECHNOLOGIES", "TECHNOLOGY", "GLOBALSOFT", "SOLUTIONS", "SERVICES",
        "IND", "INDIA", "KA", "RE", "RU", "ORE", "BAN", "BANG", "BANGAL",
        "BANGALOR", "BANGALORE", "BENGALURU", "BENGALUR", "ANGALORE", "ALORE",
        "STRE", "ET", "HALI", "ST", "PH", "ASE", "EXTN", "EXTN", "TALUK",
    }
) | frozenset(STATE_ABBREV.keys())
LAYOUT_PRE_STREET: frozenset[str] = BUILDING_STOP_TOKENS | frozenset(
    {
        "CROSS", "MAIN", "ROAD", "BLOCK", "FLR", "STAGE", "PHASE", "SECTOR", "POST",
        "FARM", "OLD", "NEW", "OPP", "BEHIND", "NEAR", "HOUSE", "FLAT", "PLOT", "DOOR",
        "SHOP", "RING", "OUTER", "INNER", "HIGHWAY", "LANE", "AVENUE", "MARG",
        "EXTENSION", "EXTN", "CIRCLE", "BUS", "STOP", "COLLEGE", "SCHOOL", "TEMPLE",
        "MASJID", "CHURCH", "HOSPITAL", "HOSITAL", "DUPLEX", "HSE", "RD", "BLDG",
        "BUILDING", "FLOOR", "GROUND", "WING", "TOWER",
    }
)
LOCALITY_LIKE_SUFFIXES: frozenset[str] = frozenset(
    {"NAGAR", "LAYOUT", "COLONY", "GARDEN", "GARDENS", "PARK", "PURAM", "PURA", "HALLI"}
)
ORDINAL_RE = re.compile(r"\b(\d+)(ST|ND|RD|TH)\b", re.IGNORECASE)
ORDINAL_TOKEN_RE = re.compile(r"\b(\d+)ORD(ST|ND|RD|TH)\b", re.IGNORECASE)
LANDMARK_RE = re.compile(
    r"(?:NEAR|BEHIND|OPPOSITE|OPP\.|BESIDE|NEXT TO|IN FRONT OF|ADJACENT TO)\s+"
    r"(.+?)(?=\s+(?:NEAR|BEHIND|FLAT|NO\.?\s*\d|SECTOR|PHASE|\d{6}\b)|$)",
    re.IGNORECASE,
)
LOCALITY_RE = re.compile(
    r"\b((?:[\w&.-]+\s+){0,3}(?:NAGAR|COLONY|LAYOUT|VIHAR|PURAM|PURI|PURA|GANJ|BAGH|"
    r"ENCLAVE|EXTENSION|TOWNSHIP|CHOWK|MOHALLA|WADI|HALLI|PALLY|PET|KUNJ|PARK|"
    r"GARDENS|ESTATE|SECTOR|PHASE|BLOCK|CROSS|MAIN ROAD|MAIN))\b",
    re.IGNORECASE,
)
PROJECT_NORM_RE = re.compile(r"[^A-Z0-9 ]+")

# ---------------------------------------------------------------------------
# Dotcom project dictionary (dotcom.project.csv)
# Match order in _match_project_dictionary: exact phrase → collapsed OCR → fuzzy.
# ---------------------------------------------------------------------------


def _norm_project_text(text: str) -> str:
    """Uppercase alphanumeric tokens for dictionary keys and comparisons."""
    return re.sub(r"\s+", " ", PROJECT_NORM_RE.sub(" ", text.upper())).strip()


def _norm_project_city(city: str | None) -> str:
    if not city:
        return ""
    key = _norm_project_text(city).lower()
    return PROJECT_CITY_NORM.get(key, key)


def _ocr_fix_project_text(text: str) -> str:
    t = text.upper()
    for pattern in PROJECT_OCR_BOUNDARY_RE:
        t = pattern.sub(r"\1 \2", t)
    return t


def _load_project_dictionary() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Load dotcom.project.csv: phrase maps plus full project/city/subLocation records."""
    global _PROJECT_INDEX, _PROJECT_GLOBAL, _PROJECT_BY_BRAND, _PROJECT_CANONICAL
    global _PROJECT_COLLAPSED_BY_BRAND
    global _DOTCOM_RECORDS
    if _PROJECT_INDEX is not None and _PROJECT_GLOBAL is not None:
        return _PROJECT_INDEX, _PROJECT_GLOBAL

    by_city: dict[str, dict[str, str]] = {}
    global_map: dict[str, str] = {}
    brand_raw: dict[str, list[tuple[str, str, str]]] = {}
    collapsed_by_brand: dict[str, list[tuple[str, str]]] = {}
    canonical: set[str] = set()
    records: list[_DotcomRecord] = []

    if not PROJECT_CSV.exists():
        _PROJECT_INDEX, _PROJECT_GLOBAL = by_city, global_map
        _PROJECT_BY_BRAND, _PROJECT_CANONICAL = brand_raw, canonical
        _PROJECT_COLLAPSED_BY_BRAND = collapsed_by_brand
        _DOTCOM_RECORDS = records
        return by_city, global_map

    with PROJECT_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("projectData.projectName") or "").strip()
            if not name:
                continue
            norm_name = _norm_project_text(name)
            if len(norm_name) < 5:
                continue
            city_display = (row.get("projectData.city") or "").strip()
            city_key = _norm_project_city(city_display)
            sub_loc = (row.get("projectData.subLocation") or "").strip()
            norm_subloc = _norm_project_text(sub_loc) if sub_loc else ""

            rec = _DotcomRecord(
                projectName=name,
                city=city_display,
                city_key=city_key,
                subLocation=sub_loc,
                norm_name=norm_name,
                norm_subloc=norm_subloc,
            )
            records.append(rec)

            canonical.add(name)
            city_bucket = by_city.setdefault(city_key, {})
            for bucket in (city_bucket, global_map):
                current = bucket.get(norm_name)
                if current is None or len(name) > len(current):
                    bucket[norm_name] = name
            brand = norm_name.split()[0]
            brand_raw.setdefault(brand, []).append((name, norm_name, city_key))
            compact = norm_name.replace(" ", "")
            if len(compact) >= 10 and brand not in PROJECT_BRAND_STOP:
                bucket = collapsed_by_brand.setdefault(brand, [])
                if name not in {n for _, n in bucket}:
                    bucket.append((compact, name))

    by_brand = {
        brand: entries
        for brand, entries in brand_raw.items()
        if brand not in PROJECT_BRAND_STOP and len(entries) >= PROJECT_BRAND_MIN_COUNT
    }
    for brand, entries in collapsed_by_brand.items():
        entries.sort(key=lambda x: len(x[0]), reverse=True)
    _PROJECT_INDEX, _PROJECT_GLOBAL = by_city, global_map
    _PROJECT_BY_BRAND, _PROJECT_CANONICAL = by_brand, canonical
    _PROJECT_COLLAPSED_BY_BRAND = collapsed_by_brand
    _DOTCOM_RECORDS = records
    return by_city, global_map


def _resolve_dotcom_city_key(
    city: str | None,
    zip_code: str,
    zip_lookup: dict | None,
) -> str:
    """Dotcom city key from parsed city, pin lookup, or Bangalore ZIP prefixes.

    Only keys that exist in dotcom.project.csv count, so taluk names the parser reports
    as city (Bommanahalli, Mahadevapura) fall through to the pincode, then to the city
    recorded for that pincode in Pincode.xlsx, and last to the Bangalore pin prefixes.
    """
    _load_triplet_index()
    assert _DOTCOM_CITY_DISPLAY is not None
    for candidate in (city, zip_lookup.get("city") if zip_lookup else None):
        city_key = _norm_project_city(candidate)
        if city_key in _DOTCOM_CITY_DISPLAY:
            return city_key
    _load_triplet_index()
    assert _DOTCOM_CITY_DISPLAY is not None
    from_pin = _pin_city_key(zip_code)
    if from_pin in _DOTCOM_CITY_DISPLAY:
        return from_pin
    z = (zip_code or "").strip()
    if z.isdigit() and len(z) == 6 and z[:3] in BLR_PIN_PREFIXES:
        return "bangalore"
    return ""


_TRIPLET_ABBREV_RES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{short}\b"), full)
    for short, full in (
        ("RD", "ROAD"),
        ("NGR", "NAGAR"),
        ("LYT", "LAYOUT"),
        ("EXTN", "EXTENSION"),
        ("STG", "STAGE"),
        ("PH", "PHASE"),
    )
)
_SUBLOC_TAIL_RE = re.compile(r"\s+(?:PHASE|STAGE|SECTOR|BLOCK)\s+(?:[IVX]+|\d+)$")
DOTCOM_GENERIC_WORDS: frozenset[str] = PROJECT_BRAND_STOP | SUFFIX_ONLY_WORDS | frozenset(
    {
        "SREE", "SAI", "BDA", "FLATS", "BUILDING", "NILAYA", "NILAYAM", "NIVAS", "RESIDENCE",
        "APPARTMENT", "APPARTMENTS", "ENCLAVE", "MEADOWS", "PARADISE", "PRIDE", "AVENUE",
    }
)
FUZZY_TRIPLET_PROJECT_CUTOFF = 92
FUZZY_TRIPLET_STRONG_CUTOFF = 93  # fuzzy name may skip the locality leg above this
PARTIAL_NAME_MIN_LEN = 12  # a builder-less fragment must still be this distinctive
GLUED_PROJECT_PREFIX = 6  # index key length for names glued into surrounding text
GLUED_PROJECT_MIN_LEN = 12  # only long names may match without a word boundary
FUZZY_TRIPLET_SUBLOC_CUTOFF = 92
PIN_SUBLOC_MIN_COUNT = 3
PIN_SUBLOC_MIN_SHARE = 0.03

# Confidence weights: how much each kind of evidence is worth (see _score_confidence).
DOTCOM_RULE_WEIGHT = {
    "project_locality_pincode": 0.20,
    "project_locality": 0.16,
    "project_pincode": 0.12,
    "project_unique_nearby": 0.10,
}
LOCALITY_SOURCE_WEIGHT = {
    "dotcom_match": 0.10,
    "dotcom_subloc": 0.09,
    "pincode_map": 0.08,
    "gazetteer": 0.08,
    "pincode_fuzzy": 0.06,
    "ner": 0.05,
    "parser": 0.04,
}

# How a dotcom row was confirmed; strongest first when several records qualify.
OPTION_LOCALITY_PINCODE = "project_locality_pincode"
OPTION_LOCALITY = "project_locality"
OPTION_PINCODE = "project_pincode"
OPTION_UNIQUE_NEARBY = "project_unique_nearby"
OPTION_RANK = {
    OPTION_LOCALITY_PINCODE: 4,
    OPTION_LOCALITY: 3,
    OPTION_PINCODE: 2,
    OPTION_UNIQUE_NEARBY: 1,
}
# Calibrated on matches confirmed without distance (subLocation written in the address),
# whose address-pincode to subLocation distance runs: P50 0.0, P80 1.5, P85 3.2, P90 4.7,
# P95 23.2 km. The radius is that P90: it covers 90% of genuine match geometry, while 7km
# would add only 1.5pp and admit 451 more unconfirmed rows. Past P90 the tail (P95 = 23km)
# is pincode-centroid noise, not real spread.
UNIQUE_NEARBY_MAX_KM = 4.7

_TRIPLET_NAME_INDEX: dict[str, dict[str, list[_DotcomRecord]]] | None = None
_TRIPLET_BRAND_INDEX: dict[str, dict[str, list[str]]] | None = None
_TRIPLET_SUBLOCS_SORTED: dict[str, list[str]] | None = None
_TRIPLET_SUBLOC_DISPLAY: dict[str, dict[str, str]] | None = None
_TRIPLET_COMPACT_BY_PREFIX: dict[str, dict[str, list[tuple[str, str]]]] | None = None
_TRIPLET_PARTIAL: dict[str, dict[str, str]] | None = None
_DOTCOM_CITY_DISPLAY: dict[str, str] | None = None


_GLUED_TOKEN_RE = re.compile(
    r"(BENGALURU|BANGALORE|BENGALUR|BANGALOR|KARNATAKA|LAYOUT|CROSS|STAGE|PHASE|FLOOR)"
)


def _split_glued_tokens(norm_text: str) -> str:
    """Space out bureau glue words: LAYOUTKAGGADASAPURABENGALUR -> LAYOUT KAGGADASAPURA BENGALUR."""
    return re.sub(r"\s+", " ", _GLUED_TOKEN_RE.sub(r" \1 ", norm_text)).strip()


def _triplet_norm(text: str) -> str:
    t = _norm_project_text(text or "")
    for pattern, full in _TRIPLET_ABBREV_RES:
        t = pattern.sub(full, t)
    return t


def _triplet_locality_text(text: str) -> str:
    return _split_glued_tokens(_triplet_norm(text))


def _load_triplet_index() -> None:
    """city_key -> norm projectName -> records, brand -> norm names, sorted subLocations."""
    global _TRIPLET_NAME_INDEX, _TRIPLET_BRAND_INDEX, _TRIPLET_SUBLOCS_SORTED, _TRIPLET_SUBLOC_DISPLAY
    global _TRIPLET_COMPACT_BY_PREFIX, _TRIPLET_PARTIAL
    global _DOTCOM_CITY_DISPLAY
    if _TRIPLET_NAME_INDEX is not None:
        return
    _load_project_dictionary()
    assert _DOTCOM_RECORDS is not None
    names: dict[str, dict[str, list[_DotcomRecord]]] = {}
    brands: dict[str, dict[str, list[str]]] = {}
    sublocs: dict[str, set[str]] = {}
    spellings: dict[str, dict[str, Counter]] = {}
    for rec in _DOTCOM_RECORDS:
        if not rec.city_key or not rec.norm_subloc:
            continue
        spellings.setdefault(rec.city_key, {}).setdefault(
            _triplet_norm(rec.subLocation), Counter()
        )[rec.subLocation] += 1
        norm_name = _triplet_norm(rec.projectName)
        city_names = names.setdefault(rec.city_key, {})
        bucket = city_names.setdefault(norm_name, [])
        if any(r.norm_subloc == rec.norm_subloc for r in bucket):
            continue
        if not bucket:
            brand = norm_name.split()[0]
            brands.setdefault(rec.city_key, {}).setdefault(brand, []).append(norm_name)
        bucket.append(rec)
        sub = _triplet_norm(rec.subLocation)
        if len(sub) >= 4:
            sublocs.setdefault(rec.city_key, set()).add(sub)
    _TRIPLET_NAME_INDEX = names
    _TRIPLET_BRAND_INDEX = brands
    _TRIPLET_SUBLOCS_SORTED = {
        city: sorted(subs, key=len, reverse=True) for city, subs in sublocs.items()
    }
    _TRIPLET_SUBLOC_DISPLAY = {
        city: {norm: cnt.most_common(1)[0][0] for norm, cnt in by_norm.items()}
        for city, by_norm in spellings.items()
    }
    by_prefix: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for city, names_by_norm in names.items():
        bucket = by_prefix.setdefault(city, {})
        for norm_name in names_by_norm:
            compact = norm_name.replace(" ", "")
            if len(compact) >= GLUED_PROJECT_MIN_LEN:
                bucket.setdefault(compact[:GLUED_PROJECT_PREFIX], []).append((compact, norm_name))
    _TRIPLET_COMPACT_BY_PREFIX = by_prefix

    # Bureau text often drops the builder ("Maithri Shilpitha Sunflower" written as
    # "Shilpitha Sunflower"), so index the inner word runs of long names. A run kept only
    # when it points at one project in that city, so it can never be ambiguous.
    partial: dict[str, dict[str, str | None]] = {}
    for city, names_by_norm in names.items():
        bucket = partial.setdefault(city, {})
        city_sublocs = sublocs.get(city, set())
        subloc_compacts = {sub.replace(" ", "") for sub in city_sublocs}
        locality_words = {w for sub in city_sublocs for w in sub.split()} | subloc_compacts
        for norm_name in names_by_norm:
            parts = norm_name.split()
            if len(parts) < 3:
                continue
            for start in range(len(parts) - 1):
                for end in range(start + 2, len(parts) + 1):
                    if (start, end) == (0, len(parts)):
                        continue
                    run = " ".join(parts[start:end])
                    if len(run) < PARTIAL_NAME_MIN_LEN or run in names_by_norm:
                        continue
                    if _is_generic_project_name(run) or run.replace(" ", "") in subloc_compacts:
                        continue
                    # The fragment must carry the project's own identity: at least two words
                    # that are neither locality names nor generic building words. Without this
                    # "Elegant Exotica Yelahanka New Town" is reachable by its locality half and
                    # "GLR Neela Apartment" by "NEELA APARTMENT".
                    distinctive = [
                        w for w in run.split()
                        if w not in locality_words and w not in DOTCOM_GENERIC_WORDS and len(w) >= 4
                    ]
                    if len(distinctive) < 2:
                        continue
                    bucket[run] = None if run in bucket and bucket[run] != norm_name else norm_name
    _TRIPLET_PARTIAL = {
        city: {run: name for run, name in runs.items() if name} for city, runs in partial.items()
    }

    city_names: dict[str, Counter] = {}
    for rec in _DOTCOM_RECORDS:
        if rec.city_key:
            city_names.setdefault(rec.city_key, Counter())[rec.city] += 1
    _DOTCOM_CITY_DISPLAY = {key: cnt.most_common(1)[0][0] for key, cnt in city_names.items()}


def _dotcom_subloc_spelling(locality: str, city_key: str) -> str | None:
    """Canonical dotcom spelling for a near-identical locality (Mahadevapura -> Mahadevpura)."""
    _load_triplet_index()
    assert _TRIPLET_SUBLOC_DISPLAY is not None
    by_norm = _TRIPLET_SUBLOC_DISPLAY.get(city_key)
    if not by_norm:
        return None
    norm = _triplet_norm(locality)
    if norm in by_norm:
        return by_norm[norm]
    compact = norm.replace(" ", "")
    if len(compact) < 8 or not _HAS_RAPIDFUZZ or _rf_process is None:
        return None
    by_compact = {n.replace(" ", ""): d for n, d in by_norm.items()}
    hit = _rf_process.extractOne(
        compact, list(by_compact), scorer=_rf_fuzz.ratio, score_cutoff=FUZZY_TRIPLET_SUBLOC_CUTOFF
    )
    return by_compact[hit[0]] if hit else None


def _dotcom_locality_from_text(address: str, city_key: str, zip_code: str) -> str | None:
    """Dotcom subLocation (canonical spelling) written in the address for this city.

    Several hits: prefer one the pincode is known to cover, then the longest; hits that
    are part of a longer hit (ELECTRONIC CITY inside ELECTRONIC CITY PHASE I) are dropped.
    """
    if not city_key:
        return None
    _load_triplet_index()
    assert _TRIPLET_SUBLOCS_SORTED is not None and _TRIPLET_SUBLOC_DISPLAY is not None
    norm_text = _triplet_locality_text(address)
    hits = [s for s in _TRIPLET_SUBLOCS_SORTED.get(city_key, ()) if _locality_in_text(s, norm_text)]
    if not hits:
        return None
    hits = [h for h in hits if not any(h != o and f" {h} " in f" {o} " for o in hits)]
    pin_locs = _pin_locality_norms(zip_code)
    best = max(hits, key=lambda h: (_SUBLOC_TAIL_RE.sub("", h) in pin_locs, len(h)))
    return _TRIPLET_SUBLOC_DISPLAY[city_key].get(best)


def _find_subloc_in_text(norm_text: str, city_key: str) -> str | None:
    """Longest dotcom subLocation of city_key literally present in (normalised) text."""
    _load_triplet_index()
    assert _TRIPLET_SUBLOCS_SORTED is not None
    for sub in _TRIPLET_SUBLOCS_SORTED.get(city_key, ()):
        if _locality_in_text(sub, norm_text):
            return sub
    return None


def _load_subloc_pincodes() -> dict[tuple[str, str], set[str]]:
    """(city_key, subLocation base) -> pincodes, from the geocoded dotcom pincode master.

    A subLocation usually spans several pincodes; the Square Yards sheet lists one, so this
    widens the pincode leg. Rows the master itself marks low/very_low are ignored.
    """
    global _SUBLOC_PINCODES
    if _SUBLOC_PINCODES is not None:
        return _SUBLOC_PINCODES
    by_sub: dict[tuple[str, str], set[str]] = {}
    if DOTCOM_PINCODE_MASTER_CSV.exists():
        with DOTCOM_PINCODE_MASTER_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("pincode_confidence") not in ("high", "medium"):
                    continue
                pins = [p for p in (row.get("all_pincodes") or "").split(";") if p]
                sub = row.get("projectData.subLocation") or ""
                if not pins or not sub:
                    continue
                key = (_norm_project_city(row.get("projectData.city")), _SUBLOC_TAIL_RE.sub("", _triplet_norm(sub)))
                by_sub.setdefault(key, set()).update(pins)
    _SUBLOC_PINCODES = by_sub
    return by_sub


_PHASE_RE = re.compile(r"\b(?:PHASE|STAGE|BLOCK|TOWER)\s+([IVX]+|\d+)\b")
_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6"}


_PIN_COORDS: dict[str, tuple[float, float]] | None = None
LANDMARK_WORDS: frozenset[str] = frozenset(
    {"NEAR", "NEARBY", "OPPOSITE", "OPP", "BEHIND", "BESIDE", "ADJACENT", "FACING", "FRONT"}
)


def _pin_coords() -> dict[str, tuple[float, float]]:
    """pincode -> (lat, lon) from the India Post data shipped with bharataddress."""
    global _PIN_COORDS
    if _PIN_COORDS is None:
        _PIN_COORDS = {}
        data = Path(pincode.__file__).with_name("data") / "pincodes.json"
        if data.exists():
            with data.open(encoding="utf-8") as f:
                for pin, row in json.load(f).items():
                    if row.get("latitude") and row.get("longitude"):
                        _PIN_COORDS[pin] = (row["latitude"], row["longitude"])
    return _PIN_COORDS


def _km_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _subloc_distance_km(rec: _DotcomRecord, zip_code: str) -> float | None:
    """Distance from the address pincode to the nearest pincode of the row's subLocation."""
    coords = _pin_coords()
    here = coords.get((zip_code or "").strip())
    if not here:
        return None
    key = (rec.city_key, _SUBLOC_TAIL_RE.sub("", _triplet_norm(rec.subLocation)))
    distances = [
        _km_between(here, coords[p]) for p in _load_subloc_pincodes().get(key, ()) if p in coords
    ]
    return min(distances) if distances else None


def _named_as_landmark(name: str, norm_text: str) -> bool:
    """True when every mention of the project sits right after NEAR/OPPOSITE/BEHIND.

    Works for OCR variants too: it anchors on the first word of the name rather than the
    whole span, because a collapsed or fuzzy match is not literally in the text.
    """
    first = (name or "").split()[0] if name else ""
    if not first:
        return False
    words = norm_text.split()
    positions = [i for i, w in enumerate(words) if w == first]
    if not positions:
        return False
    for i in positions:
        if not any(w in LANDMARK_WORDS for w in words[max(0, i - 3) : i]):
            return False
    return True


def _phase_numbers(text: str) -> set[str]:
    """Phase/stage numbers written in a name or address, roman numerals folded to digits."""
    return {
        _ROMAN.get(m.group(1), m.group(1)) for m in _PHASE_RE.finditer(_triplet_norm(text))
    }


def _is_generic_project_name(norm_name: str) -> bool:
    """Short / suffix-only names (SLV NIVAS, BDA FLATS, SAPTHAGIRI) need text evidence."""
    words = norm_name.split()
    if len(words) < 2:
        return True
    distinctive = [w for w in words if w not in DOTCOM_GENERIC_WORDS and not w.isdigit()]
    return sum(len(w) for w in distinctive) < 5


def _project_candidates_in_text(norm_text: str, city_key: str) -> dict[str, tuple[str, str]]:
    """norm projectName -> (match kind, the text span that matched) for this city."""
    _load_triplet_index()
    assert _TRIPLET_NAME_INDEX is not None and _TRIPLET_BRAND_INDEX is not None
    city_names = _TRIPLET_NAME_INDEX.get(city_key)
    if not city_names:
        return {}
    city_brands = _TRIPLET_BRAND_INDEX.get(city_key, {})
    words = norm_text.split()
    found: dict[str, tuple[str, str]] = {}  # norm name -> (kind, matched span)

    for size in range(min(8, len(words)), 0, -1):
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i : i + size])
            if phrase in city_names and (size >= 2 or len(phrase) >= 8):
                found.setdefault(phrase, ("exact", phrase))

    compact_text = norm_text.replace(" ", "")
    brand_positions = [
        (i, w) for i, w in enumerate(words) if w in city_brands and w not in PROJECT_BRAND_STOP
    ]
    for i, brand in brand_positions:
        for norm_name in city_brands[brand]:
            if norm_name in found:
                continue
            compact = norm_name.replace(" ", "")
            if len(compact) >= 10 and compact in compact_text:
                found[norm_name] = ("collapsed", norm_name)

        if not _HAS_RAPIDFUZZ or _rf_process is None or _rf_fuzz is None:
            continue
        pool = [n for n in city_brands[brand] if n not in found and len(n.split()) >= 2]
        if not pool:
            continue
        for size in range(2, min(6, len(words) - i) + 1):
            phrase = " ".join(words[i : i + size])
            hit = _rf_process.extractOne(
                phrase, pool, scorer=_rf_fuzz.ratio, score_cutoff=FUZZY_TRIPLET_PROJECT_CUTOFF
            )
            if hit and abs(len(hit[0].split()) - size) <= 1:
                # A close match whose digits agree may stand on the pincode alone;
                # RENAISSANCE PARK 3 vs RENAISSANCE PARK I must not.
                digits_agree = re.findall(r"\d+", phrase) == re.findall(r"\d+", hit[0])
                strong = hit[1] >= FUZZY_TRIPLET_STRONG_CUTOFF and digits_agree
                found.setdefault(hit[0], ("fuzzy_strong" if strong else "fuzzy", phrase))
    assert _TRIPLET_PARTIAL is not None
    partials = _TRIPLET_PARTIAL.get(city_key, {})
    if partials:
        for size in range(min(6, len(words)), 1, -1):
            for i in range(len(words) - size + 1):
                run = " ".join(words[i : i + size])
                full = partials.get(run)
                if full and full not in found:
                    found[full] = ("partial", run)

    # Fully glued bureau text (PROVIDENTSUNWORTH5J802VENKATAPURA) has no word to key on,
    # so scan the compact text against long project names by prefix. Runs last: a name the
    # word-based passes already found keeps their (stronger) kind.
    assert _TRIPLET_COMPACT_BY_PREFIX is not None
    prefixes = _TRIPLET_COMPACT_BY_PREFIX.get(city_key, {})
    for i in range(len(compact_text) - GLUED_PROJECT_MIN_LEN + 1):
        for compact, norm_name in prefixes.get(compact_text[i : i + GLUED_PROJECT_PREFIX], ()):
            if norm_name not in found and compact_text.startswith(compact, i):
                found[norm_name] = ("glued", norm_name)
    return found


def _subloc_evidence(
    rec: _DotcomRecord,
    norm_name: str,
    norm_text: str,
    locality: str | None,
    zip_code: str,
) -> tuple[str, bool]:
    """(locality evidence, pincode agrees) for rec.subLocation.

    Locality evidence is "text" when the subLocation is written in the address, "locality"
    when it equals the parsed locality, "" otherwise. The project name is blanked out first
    so "PRESTIGE JAYANAGAR" cannot prove its own subLocation. The pincode flag says the
    subLocation is registered for this pincode in pincode_locality_merged.csv or in the
    dotcom pincode master.
    """
    sub = _triplet_norm(rec.subLocation)
    if len(sub) < 3:
        return "", False
    forms = {sub, _SUBLOC_TAIL_RE.sub("", sub)}
    pin_ok = bool(forms & _pin_locality_norms(zip_code)) or zip_code in _load_subloc_pincodes().get(
        (rec.city_key, _SUBLOC_TAIL_RE.sub("", sub)), ()
    )

    text_wo_name = _split_glued_tokens(f" {norm_text} ".replace(f" {norm_name} ", " | "))
    if any(len(form) >= 3 and _locality_in_text(form, text_wo_name) for form in forms):
        return "text", pin_ok
    sub_compact = sub.replace(" ", "")
    if _HAS_RAPIDFUZZ and _rf_fuzz is not None and len(sub_compact) >= 8:
        words = text_wo_name.split()
        n = len(sub.split())
        for size in {max(1, n - 1), n, n + 1}:
            for i in range(len(words) - size + 1):
                window = "".join(words[i : i + size])
                if _rf_fuzz.ratio(sub_compact, window) >= FUZZY_TRIPLET_SUBLOC_CUTOFF:
                    return "text", pin_ok

    if locality:
        loc = _triplet_norm(locality)
        if loc and (loc in forms or loc.replace(" ", "") == sub_compact):
            return "locality", pin_ok
    return "", pin_ok


def _match_dotcom_triplet(
    address: str,
    zip_code: str,
    *,
    city: str | None,
    locality: str | None,
    building_name: str | None,
) -> tuple[_DotcomRecord | None, str, str]:
    """Return (record, match rule, reject_reason) for one address.

    city (parsed or pincode) scopes the project list. A project name found in the address
    is accepted only with subLocation support, and the rule records which support was found:
    project+locality+pincode, project+locality, or project+pincode. Generic or fuzzy project
    names always need the locality; the pincode alone is not enough for them.
    """
    zip_code = (zip_code or "").strip()
    lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
    city_key = _resolve_dotcom_city_key(city, zip_code, lookup)
    if not city_key:
        return None, "", "no_city"
    _load_triplet_index()
    assert _TRIPLET_NAME_INDEX is not None
    city_names = _TRIPLET_NAME_INDEX.get(city_key, {})

    norm_text = _triplet_norm(address)
    candidates = _project_candidates_in_text(norm_text, city_key)
    bn = _triplet_norm(building_name or "")
    if bn in city_names:
        candidates.setdefault(bn, ("building_name", bn))
    if not candidates:
        return None, "", "no_project"

    address_phases = _phase_numbers(address)
    best: tuple[int, int, _DotcomRecord, str] | None = None
    seen_rules: set[str] = set()
    for norm_name, (kind, span) in candidates.items():
        name_phases = _phase_numbers(norm_name)
        if name_phases and address_phases and not (name_phases & address_phases):
            continue  # NANDI GARDENS PHASE 1 must not match Nandi Gardens Phase II
        if _named_as_landmark(span, norm_text):
            continue  # "NEAR ELEMENTS MALL" is a landmark, not the address itself
        generic = _is_generic_project_name(norm_name)
        needs_locality = kind in ("fuzzy", "building_name", "glued") or generic
        # A builder-less fragment must be tied to the place by locality or pincode.
        unique_nearby_ok = kind in ("exact", "collapsed") and not generic

        for rec in city_names.get(norm_name, ()):
            loc_evidence, pin_ok = _subloc_evidence(rec, norm_name, norm_text, locality, zip_code)
            if loc_evidence and pin_ok:
                rule = OPTION_LOCALITY_PINCODE
            elif loc_evidence:
                rule = OPTION_LOCALITY
            elif pin_ok:
                rule = OPTION_PINCODE
            elif unique_nearby_ok and len(city_names.get(norm_name, ())) == 1:
                distance = _subloc_distance_km(rec, zip_code)
                if distance is None or distance > UNIQUE_NEARBY_MAX_KM:
                    continue
                rule = OPTION_UNIQUE_NEARBY
            else:
                continue
            if needs_locality and not loc_evidence:
                seen_rules.add("needs_locality")
                continue
            key = (OPTION_RANK[rule], len(norm_name))
            if best is None or key > best[:2]:
                best = (key[0], key[1], rec, rule)
    if best:
        return best[2], best[3], ""
    return None, "", "generic_pincode_only" if seen_rules else "subloc_mismatch"


def _apply_dotcom_triplet(
    row_out: dict[str, str],
    *,
    address: str = "",
    stats: Counter | None = None,
) -> None:
    """Set dotcom_matched/basis; on Yes overwrite building_name, city, locality from one CSV row."""
    rec, rule, reason = _match_dotcom_triplet(
        address or row_out.get("ADDRESS") or "",
        row_out.get("ZIP") or "",
        city=row_out.get("city"),
        locality=row_out.get("locality"),
        building_name=row_out.get("building_name"),
    )
    if rec:
        row_out["building_name"] = rec.projectName
        row_out["city"] = rec.city
        row_out["locality"] = rec.subLocation
        row_out["locality_source"] = "dotcom_match"
        row_out[DOTCOM_MATCH_COL] = "Yes"
        row_out[DOTCOM_RULE_COL] = rule
        if stats is not None:
            stats["dotcom_triplet_matched"] += 1
            stats[f"dotcom_rule_{rule}"] += 1
        return
    row_out[DOTCOM_MATCH_COL] = "No"
    row_out[DOTCOM_RULE_COL] = ""
    if stats is not None:
        stats[f"dotcom_reject_{reason}"] += 1


def _canonical_dotcom_lookup(building_name: str, city: str | None) -> str | None:
    """Canonical dotcom projectName for a building name, scoped to a city."""
    _load_project_dictionary()
    assert _PROJECT_CANONICAL is not None
    norm = _norm_project_text(building_name)
    if len(norm) < 5:
        return None
    for bucket in _project_lookup_maps(city):
        hit = bucket.get(norm)
        if hit and hit in _PROJECT_CANONICAL:
            return hit
    return None


def _dotcom_matched_flag(building_name: str | None) -> str:
    """Legacy name-only check; prefer _apply_dotcom_triplet on full row."""
    _load_project_dictionary()
    assert _PROJECT_CANONICAL is not None
    name = (building_name or "").strip()
    return "Yes" if name and name in _PROJECT_CANONICAL else "No"


def _location_matched_flag(building_name: str | None) -> str:
    """Yes if building_name is an exact member of OSM location master canonical set."""
    _load_location_buildings()
    assert _LOCATION_CANONICAL is not None
    name = (building_name or "").strip()
    return "Yes" if name and name in _LOCATION_CANONICAL else "No"


def _project_lookup_maps(city: str | None) -> list[dict[str, str]]:
    """City-scoped phrase map only when city is known; else global fallback."""
    by_city, global_map = _load_project_dictionary()
    city_key = _norm_project_city(city)
    if city_key and city_key in by_city:
        return [by_city[city_key]]
    return [global_map]


def _match_project_exact(text: str, city: str | None) -> str | None:
    """Exact dotcom phrase match (with OCR boundary spacing)."""
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    if len(norm_text) < 5:
        return None
    for bucket in _project_lookup_maps(city):
        for phrase in _project_phrases(norm_text):
            hit = bucket.get(phrase)
            if hit:
                return hit
    return None


def _match_project_collapsed(text: str, city: str | None) -> str | None:
    """Match glued OCR project names via collapsed substring (brand-filtered)."""
    if _PROJECT_COLLAPSED_BY_BRAND is None:
        _load_project_dictionary()
    assert _PROJECT_COLLAPSED_BY_BRAND is not None

    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    collapsed_addr = norm_text.replace(" ", "")
    if len(collapsed_addr) < 10:
        return None

    city_key = _norm_project_city(city)
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for brand in norm_text.split():
        if brand not in _PROJECT_COLLAPSED_BY_BRAND:
            continue
        for compact, name in _PROJECT_COLLAPSED_BY_BRAND[brand]:
            if name in seen:
                continue
            seen.add(name)
            candidates.append((compact, name))

    for compact, name in sorted(candidates, key=lambda x: len(x[0]), reverse=True):
        if compact in collapsed_addr:
            if city_key:
                by_city, _global = _load_project_dictionary()
                norm_name = _norm_project_text(name)
                city_bucket = by_city.get(city_key, {})
                if norm_name not in city_bucket:
                    continue
            return name
    return None


def _project_brand_candidates(city: str | None, brands: list[str]) -> list[str]:
    """City-scoped project names for brand tokens seen in the address."""
    if _PROJECT_BY_BRAND is None:
        _load_project_dictionary()
    assert _PROJECT_BY_BRAND is not None
    city_key = _norm_project_city(city)
    out: list[str] = []
    seen: set[str] = set()
    for brand in brands:
        for name, _norm_name, ck in _PROJECT_BY_BRAND.get(brand, []):
            if city_key and ck and ck != city_key:
                continue
            if name not in seen:
                seen.add(name)
                out.append(name)
    if out or city_key:
        return out
    for brand in brands:
        for name, _norm_name, _ck in _PROJECT_BY_BRAND.get(brand, []):
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


try:
    from rapidfuzz import fuzz as _rf_fuzz
    from rapidfuzz import process as _rf_process

    _HAS_RAPIDFUZZ = True
except ImportError:
    _rf_fuzz = None
    _rf_process = None
    _HAS_RAPIDFUZZ = False


def _best_project_fuzzy(phrase: str, candidates: list[str], cutoff: float) -> tuple[str, float] | None:
    if not phrase or not candidates:
        return None
    if _HAS_RAPIDFUZZ and _rf_process is not None and _rf_fuzz is not None:
        hit = _rf_process.extractOne(
            phrase,
            candidates,
            scorer=_rf_fuzz.token_set_ratio,
            score_cutoff=int(cutoff * 100),
        )
        if hit:
            return hit[0], hit[1] / 100.0
        return None
    return phonetic.best_match(phrase, candidates, cutoff=cutoff)


def _match_project_fuzzy(text: str, city: str | None) -> str | None:
    """Brand-filtered fuzzy match for OCR typos; only call when exact match failed."""
    if _PROJECT_BY_BRAND is None:
        _load_project_dictionary()
    assert _PROJECT_BY_BRAND is not None

    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    words = norm_text.split()
    if len(words) < 2:
        return None

    brands = [w for w in words if w in _PROJECT_BY_BRAND]
    if not brands:
        return None

    candidates = _project_brand_candidates(city, brands)
    if not candidates:
        return None

    best_name: str | None = None
    best_score = FUZZY_PROJECT_CUTOFF
    brand_positions = [i for i, w in enumerate(words) if w in brands]
    for start in brand_positions:
        for end in range(start + 2, min(start + 9, len(words) + 1)):
            phrase = " ".join(words[start:end])
            if len(phrase) < 6:
                continue
            hit = _best_project_fuzzy(phrase, candidates, FUZZY_PROJECT_CUTOFF)
            if hit and hit[1] >= best_score:
                best_name, best_score = hit[0], hit[1]
    return best_name


def _match_project_dictionary(
    text: str,
    city: str | None,
    *,
    allow_fuzzy: bool = True,
) -> tuple[str | None, str | None]:
    """Return (canonical projectName, match_kind) where kind is exact|fuzzy.

    Tries exact n-gram lookup, then space-collapsed OCR variants, then brand-scoped fuzzy.
    """
    hit = _match_project_exact(text, city)
    if hit:
        return hit, "exact"
    hit = _match_project_collapsed(text, city)
    if hit:
        return hit, "exact"
    if allow_fuzzy:
        fuzzy_hit = _match_project_fuzzy(text, city)
        if fuzzy_hit:
            return fuzzy_hit, "fuzzy"
    return None, None


# ---------------------------------------------------------------------------
# Pincode locality map, OSM location master (Bangalore), BLR locality gazetteer
# ---------------------------------------------------------------------------


def _load_pincode_localities() -> dict[str, list[str]]:
    """pincode -> locality names (pincode_locality_merged.csv, else the post-office CSV)."""
    global _PINCODE_LOCALITIES
    if _PINCODE_LOCALITIES is not None:
        return _PINCODE_LOCALITIES
    by_pin: dict[str, list[str]] = {}
    merged = PINCODE_LOCALITY_MERGED_CSV.exists()
    path = PINCODE_LOCALITY_MERGED_CSV if merged else PINCODE_LOCALITY_CSV
    if not path.exists():
        _PINCODE_LOCALITIES = by_pin
        return by_pin
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pin = (row.get("pincode") or "").strip()
            raw = row.get("locality") if merged else (row.get("Location ") or row.get("Location"))
            loc = _clean_locality_name(raw)
            if not pin or not loc:
                continue
            bucket = by_pin.setdefault(pin, [])
            if loc not in bucket:
                bucket.append(loc)
    _PINCODE_LOCALITIES = by_pin
    return by_pin


def _pin_city_key(zip_code: str) -> str:
    """Dotcom city key for a pincode, from the Square Yards rows of the merged file."""
    global _PIN_CITY
    if _PIN_CITY is None:
        _PIN_CITY = {}
        if PINCODE_LOCALITY_MERGED_CSV.exists():
            counts: dict[str, Counter] = {}
            with PINCODE_LOCALITY_MERGED_CSV.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("source") == "india_post":
                        continue  # district names, not cities
                    key = _norm_project_city(row.get("city"))
                    if key:
                        counts.setdefault(row["pincode"], Counter())[key] += 1
            _PIN_CITY = {pin: c.most_common(1)[0][0] for pin, c in counts.items()}
    return _PIN_CITY.get((zip_code or "").strip(), "")


def _pin_locality_norms(zip_code: str) -> set[str]:
    """Normalised locality names registered for a pincode (with phase/stage suffix stripped)."""
    global _PIN_LOCALITY_NORMS
    if _PIN_LOCALITY_NORMS is None:
        _PIN_LOCALITY_NORMS = {}
        for pin, locs in _load_pincode_localities().items():
            forms: set[str] = set()
            for loc in locs:
                norm = _triplet_norm(loc)
                forms.update({norm, _SUBLOC_TAIL_RE.sub("", norm)})
            _PIN_LOCALITY_NORMS[pin] = {f for f in forms if len(f) >= 3}
    return _PIN_LOCALITY_NORMS.get((zip_code or "").strip(), set())


def _load_location_buildings() -> dict[str, str]:
    """Load Bangalore OSM names from india_location_master.csv (norm -> display name)."""
    global _LOCATION_BUILDINGS, _LOCATION_BY_BRAND, _LOCATION_BUILDINGS_SORTED, _LOCATION_CANONICAL
    if _LOCATION_BUILDINGS is not None and _LOCATION_BY_BRAND is not None:
        return _LOCATION_BUILDINGS

    buildings: dict[str, str] = {}
    by_brand: dict[str, list[str]] = {}
    canonical: set[str] = set()
    if not LOCATION_MASTER_CSV.exists():
        _LOCATION_BUILDINGS = buildings
        _LOCATION_BY_BRAND = by_brand
        _LOCATION_BUILDINGS_SORTED = []
        _LOCATION_CANONICAL = canonical
        return buildings

    with LOCATION_MASTER_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("city") or "").strip().lower() != "bangalore":
                continue
            entity_type = row.get("entity_type") or ""
            if entity_type in LOCATION_SKIP_TYPES:
                continue
            if entity_type not in LOCATION_ENTITY_TYPES:
                continue
            try:
                conf = float(row.get("confidence") or 0)
            except ValueError:
                conf = 0.0
            if conf < LOCATION_MIN_CONF:
                continue
            for col in ("name", "society_name", "building_name"):
                name = (row.get(col) or "").strip()
                if not name or len(name) < 5:
                    continue
                if LOCATION_NAME_BLOCK_RE.search(name):
                    continue
                norm_name = _norm_project_text(name)
                if len(norm_name) < 5:
                    continue
                canonical.add(name)
                current = buildings.get(norm_name)
                if current is None or len(name) > len(current):
                    buildings[norm_name] = name
                    brand = norm_name.split()[0]
                    if brand not in PROJECT_BRAND_STOP and len(brand) >= 4:
                        bucket = by_brand.setdefault(brand, [])
                        if name not in bucket:
                            bucket.append(name)

    _LOCATION_BUILDINGS = buildings
    _LOCATION_BY_BRAND = by_brand
    _LOCATION_BUILDINGS_SORTED = sorted(buildings.items(), key=lambda x: len(x[0]), reverse=True)
    _LOCATION_CANONICAL = canonical
    return buildings


def _load_blr_locality_gazetteer() -> dict[str, str]:
    """All Bangalore locality names: pincode map (560-562) + india_location_master."""
    global _BLR_LOCALITY_GAZETTEER, _BLR_LOCALITY_SORTED, _BLR_LOCALITY_CANONICAL
    if _BLR_LOCALITY_GAZETTEER is not None:
        return _BLR_LOCALITY_GAZETTEER

    gazetteer: dict[str, str] = {}
    for pin, locs in _load_pincode_localities().items():
        if not pin.startswith(BLR_PIN_PREFIXES):
            continue
        for loc in locs:
            norm_loc = _norm_project_text(loc)
            if len(norm_loc) < 4:
                continue
            current = gazetteer.get(norm_loc)
            if current is None or len(loc) > len(current):
                gazetteer[norm_loc] = loc

    if LOCATION_MASTER_CSV.exists():
        with LOCATION_MASTER_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("city") or "").strip().lower() != "bangalore":
                    continue
                for col in ("locality", "sub_locality", "neighbourhood"):
                    loc = (row.get(col) or "").strip()
                    if not loc or len(loc) < 4:
                        continue
                    norm_loc = _norm_project_text(loc)
                    if len(norm_loc) < 4:
                        continue
                    current = gazetteer.get(norm_loc)
                    if current is None or len(loc) > len(current):
                        gazetteer[norm_loc] = loc

    _BLR_LOCALITY_GAZETTEER = gazetteer
    _BLR_LOCALITY_SORTED = sorted(gazetteer.items(), key=lambda x: len(x[0]), reverse=True)
    _BLR_LOCALITY_CANONICAL = list(dict.fromkeys(gazetteer.values()))
    return gazetteer


def _match_location_building_fuzzy(text: str) -> str | None:
    if _LOCATION_BY_BRAND is None:
        _load_location_buildings()
    assert _LOCATION_BY_BRAND is not None

    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    words = norm_text.split()
    if len(words) < 2:
        return None

    brands = [
        w
        for w in words
        if w in _LOCATION_BY_BRAND and w not in PROJECT_BRAND_STOP and len(w) >= 4
    ]
    if not brands:
        return None

    seen: set[str] = set()
    candidates: list[str] = []
    for brand in brands:
        for name in _LOCATION_BY_BRAND.get(brand, []):
            if name not in seen:
                seen.add(name)
                candidates.append(name)
    if not candidates:
        return None

    best_name: str | None = None
    best_score = FUZZY_PROJECT_CUTOFF
    brand_positions = [i for i, w in enumerate(words) if w in brands]
    for start in brand_positions:
        for end in range(start + 2, min(start + 8, len(words) + 1)):
            phrase = " ".join(words[start:end])
            if len(phrase) < 6:
                continue
            hit = _best_project_fuzzy(phrase, candidates, FUZZY_PROJECT_CUTOFF)
            if hit and hit[1] >= best_score:
                best_name, best_score = hit[0], hit[1]
    return best_name


def _match_location_building(text: str) -> str | None:
    """Match Bangalore building/society names from india_location_master.csv."""
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    if len(norm_text) < 5:
        return None
    buildings = _load_location_buildings()
    for phrase in _project_phrases(norm_text):
        hit = buildings.get(phrase)
        if hit:
            return hit
    collapsed = norm_text.replace(" ", "")
    sorted_buildings = _LOCATION_BUILDINGS_SORTED or sorted(
        buildings.items(), key=lambda x: len(x[0]), reverse=True
    )
    for norm_name, canonical in sorted_buildings:
        compact = norm_name.replace(" ", "")
        if len(compact) >= 8 and compact in collapsed:
            return canonical
    return _match_location_building_fuzzy(text)


_CITY_STATE_COMPACT_RE = re.compile(r"BENGALURU|BANGALORE|BANGLORE|BENGALORE|KARNATAKA|INDIA")
GEO_STOP: frozenset[str] = frozenset(
    {"BANGALORE", "BENGALURU", "KARNATAKA", "INDIA", "URBAN", "RURAL", "DISTRICT", "TALUK", "STATE"}
)
_CITY_WORDS_RE = re.compile(
    r"\b(?:BANGALORE|BENGALURU|BANGLORE|KARNATAKA|INDIA)(?:\s+(?:NORTH|SOUTH|EAST|WEST|URBAN|RURAL))?\b",
    re.IGNORECASE,
)

_POST_OFFICE_SUFFIX_RE = re.compile(r"\s+[SHBG]\.?\s*O\.?\s*(?:\(.*\))?\s*$|\s*\([^)]*\)\s*$", re.IGNORECASE)


def _clean_locality_name(name: str | None) -> str | None:
    """Drop post-office suffixes (Bommanahalli S.O (Bengaluru)) and city-only names (Bengaluru G.)."""
    name = _POST_OFFICE_SUFFIX_RE.sub("", (name or "").strip()).strip(" ,.-")
    if len(re.sub(r"\bCITY\b", " ", _CITY_WORDS_RE.sub(" ", name), flags=re.IGNORECASE).strip(" ,.-")) < 3:
        return None
    return name


def _locality_in_text(norm_loc: str, norm_text: str) -> bool:
    """Whole-word locality match; OCR-split spacing (WHIT EFIELD) allowed only for long names,
    still anchored at word edges so ALAHALLI never matches inside AVALAHALLI.

    City/state words are removed first so short localities (ALURU) never match inside
    BENGALURU or its OCR splits (BENG ALURU).
    """
    compact_text = _CITY_STATE_COMPACT_RE.sub("|", norm_text.replace(" ", ""))
    compact_loc = norm_loc.replace(" ", "")
    if compact_loc not in compact_text:
        return False
    if re.search(rf"(?<![A-Z0-9]){re.escape(norm_loc)}(?![A-Z0-9])", norm_text):
        return True
    if len(compact_loc) < 8:
        return False
    spaced = " ?".join(re.escape(ch) for ch in compact_loc)
    return re.search(rf"(?<![A-Z0-9]){spaced}(?![A-Z0-9])", norm_text) is not None


def _match_blr_locality_gazetteer(text: str, *, fuzzy: bool = True) -> str | None:
    """Fallback: match any known Bangalore locality appearing in address text."""
    gazetteer = _load_blr_locality_gazetteer()
    if not gazetteer:
        return None

    norm_text = _split_glued_tokens(_norm_project_text(_ocr_fix_project_text(text)))
    sorted_locs = _BLR_LOCALITY_SORTED or []
    for norm_loc, canonical in sorted_locs:
        if len(norm_loc) < 5:
            continue
        if _locality_in_text(norm_loc, norm_text):
            return _title_locality(canonical)
    if not fuzzy:
        return None

    locs = _BLR_LOCALITY_CANONICAL or list(dict.fromkeys(gazetteer.values()))
    if _HAS_RAPIDFUZZ and _rf_process is not None and _rf_fuzz is not None:
        hit = _rf_process.extractOne(
            norm_text,
            locs,
            scorer=_rf_fuzz.token_set_ratio,
            score_cutoff=int(FUZZY_LOCALITY_CUTOFF * 100),
        )
        if hit:
            return _title_locality(hit[0])
    return None


def _match_pincode_locality(
    text: str, zip_code: str, seed: str | None = None, *, fuzzy: bool = True
) -> str | None:
    """Pick best locality for a pincode from Pincode To Locality Mapping.csv."""
    locs = _load_pincode_localities().get(zip_code.strip(), [])
    if not locs:
        return None

    norm_text = _split_glued_tokens(_norm_project_text(_ocr_fix_project_text(text)))
    for loc in sorted(locs, key=len, reverse=True):
        norm_loc = _norm_project_text(loc)
        if len(norm_loc) >= 4 and _locality_in_text(norm_loc, norm_text):
            return _title_locality(loc)
    if not fuzzy:
        return None

    best_name: str | None = None
    best_score = FUZZY_LOCALITY_CUTOFF
    queries: list[str] = []
    if seed:
        queries.append(seed)
    queries.extend(_locality_phrases(text))
    seen_q: set[str] = set()
    for query in queries:
        q = query.strip()
        if len(q) < 4 or q in seen_q:
            continue
        seen_q.add(q)
        hit = phonetic.best_match(q, locs, cutoff=FUZZY_LOCALITY_CUTOFF)
        if hit and hit[1] >= best_score:
            best_name, best_score = hit[0], hit[1]

    if best_name:
        return _title_locality(best_name)

    if _HAS_RAPIDFUZZ and _rf_process is not None and _rf_fuzz is not None:
        hit = _rf_process.extractOne(
            norm_text,
            locs,
            scorer=_rf_fuzz.token_set_ratio,
            score_cutoff=int(FUZZY_LOCALITY_CUTOFF * 100),
        )
        if hit:
            return _title_locality(hit[0])
    return None


# ---------------------------------------------------------------------------
# COMBO filter helper (--filter-blr)
# ---------------------------------------------------------------------------


def is_bangalore_row(row: dict) -> bool:
    """True if ZIP is 560/561/562 or India Post lookup city normalises to bangalore."""
    zip_code = (row.get("ZIP") or "").strip()
    if zip_code.startswith(BLR_ZIP_PREFIX) and len(zip_code) == 6:
        return True
    if zip_code.isdigit() and len(zip_code) == 6:
        lookup = pincode.lookup(zip_code)
        if lookup and phonetic.normalise(lookup.get("city")) == "bangalore":
            return True
    return False


def filter_bangalore_csv(*, backup: bool = True) -> int:
    """Keep only Bangalore rows in COMBO_DEMOG.csv."""
    with INPUT_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or ["ADDRESS", "State code", "ZIP"]
        rows = list(reader)
    blr_rows = [r for r in rows if is_bangalore_row(r)]
    if backup and INPUT_CSV.exists():
        backup_path = INPUT_CSV.with_name("COMBO_DEMOG_all_cities.csv")
        if not backup_path.exists():
            with backup_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(rows)
    with INPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(blr_rows)
    return len(blr_rows)


# ---------------------------------------------------------------------------
# Shared helpers: phrase windows, state/city fuzzy equality, locality phrases
# ---------------------------------------------------------------------------


def _project_phrases(text: str, *, min_words: int = 2, max_words: int = 10) -> list[str]:
    """Sliding word n-grams from address text for exact/fuzzy project matching."""
    words = text.split()
    if not words:
        return []
    phrases: list[str] = []
    seen: set[str] = set()
    upper = min(max_words, len(words))
    for size in range(upper, min_words - 1, -1):
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i : i + size])
            if len(phrase) >= 5 and phrase not in seen:
                seen.add(phrase)
                phrases.append(phrase)
    return phrases


def _places_match(a: str | None, b: str | None) -> bool:
    """Phonetic alias match, then fuzzy_ratio for OCR typos (Bangalroe/Bangalore)."""
    if not a or not b:
        return False
    if phonetic.normalise(a) == phonetic.normalise(b):
        return True
    if a.lower() in b.lower() or b.lower() in a.lower():
        return True
    return phonetic.fuzzy_ratio(a, b) >= FUZZY_PLACE_CUTOFF


def _norm_state(name: str | None) -> str:
    if not name:
        return ""
    return phonetic.normalise(name.strip())


def _states_match(a: str | None, b: str | None) -> bool:
    return _places_match(a, b)


def _locality_phrases(text: str, max_phrases: int = 8) -> list[str]:
    """Small phrase set for gazetteer lookup — avoid O(n²) sliding windows on 56k rows."""
    phrases: list[str] = []
    seen: set[str] = set()
    for seg in re.split(r"[,;]", text):
        seg = seg.strip(" ,.-")
        if len(seg) >= 5 and seg not in seen:
            seen.add(seg)
            phrases.append(seg)
    words = text.split()
    if len(words) >= 3:
        for size in (4, 3):
            if len(words) >= size:
                for chunk in (" ".join(words[:size]), " ".join(words[-size:])):
                    if len(chunk) >= 6 and chunk not in seen:
                        seen.add(chunk)
                        phrases.append(chunk)
    return phrases[:max_phrases]


def _title_locality(name: str) -> str:
    return " ".join(w.capitalize() if w.islower() else w for w in name.split())


def _fuzzy_locality(
    text: str,
    zip_code: str,
    seed: str | None = None,
) -> tuple[str | None, float]:
    """Match address text to pincode.known_localities via phonetic.best_match()."""
    known = pincode.known_localities(zip_code)
    if not known:
        return None, 0.0

    best_name: str | None = None
    best_score = FUZZY_LOCALITY_CUTOFF
    seen: set[str] = set()
    queries: list[str] = []
    if seed:
        queries.append(seed)
    queries.extend(_locality_phrases(text))

    for query in queries:
        q = query.strip()
        if len(q) < 4 or q in seen:
            continue
        seen.add(q)
        hit = phonetic.best_match(q, known, cutoff=FUZZY_LOCALITY_CUTOFF)
        if hit and hit[1] >= best_score:
            best_name, best_score = hit[0], hit[1]

    if not best_name:
        return None, 0.0
    return _title_locality(best_name), best_score


# ---------------------------------------------------------------------------
# Bureau preprocess, regex extractors, and building-name heuristics
# Used when dotcom/OSM do not supply building_name (see parse_address_row).
# ---------------------------------------------------------------------------


_PREPROCESS_VOCABULARY: frozenset[str] | None = None


def _preprocess_vocabulary() -> frozenset[str]:
    global _PREPROCESS_VOCABULARY
    if _PREPROCESS_VOCABULARY is None:
        _PREPROCESS_VOCABULARY = preprocess.build_vocabulary()
    return _PREPROCESS_VOCABULARY


def prepare_address(address: str, zip_code: str = "") -> preprocess.PreparedAddress:
    """Clean one bureau address (see preprocess.py) with the dictionary-backed vocabulary."""
    return preprocess.prepare(address, zip_code, vocabulary=_preprocess_vocabulary())


def bureau_preprocess(address: str, zip_code: str = "") -> str:
    """Text for bharataddress.parse(): cleaned address with ordinals protected."""
    return prepare_address(address, zip_code).parser_text


def _deordinal(text: str | None) -> str | None:
    if not text:
        return text
    return ORDINAL_TOKEN_RE.sub(lambda m: f"{m.group(1)}{m.group(2).lower()}", text)


def _reject_building_name(name: str) -> bool:
    """True if name looks like noise (company suffix, unit+suffix, all short tokens, …)."""
    words = _norm_project_text(name).split()
    if not words:
        return True
    if any(w in BUILDING_REJECT_WORDS for w in words):
        return True
    if all(len(w) <= 3 for w in words):
        return True
    if len(words) == 2 and words[0].isdigit():
        return True
    if words[-1].upper() == "LAYOUT" and len(words[0]) <= 3:
        return True
    if words[-2:] == ["PVT", "LTD"] or words[-1] in {"PVT", "LTD", "LIMITED", "LLP"}:
        return True
    if len(words) == 1 and words[0] in SUFFIX_ONLY_WORDS:
        return True
    return False


def _compound_building_name(text: str) -> str | None:
    """Glued OCR names like JAYVILLA before suffix-only regex matches VILLA."""
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    best: str | None = None
    for match in COMPOUND_BUILDING_RE.finditer(norm_text):
        token = match.group(1)
        if token in SUFFIX_ONLY_WORDS or len(token) < 6:
            continue
        if _reject_building_name(token):
            continue
        if not best or len(token) > len(best):
            best = token
    return _title_building_name(best) if best else None


def _clean_building_name(name: str) -> str | None:
    name = re.sub(
        r"^(?:NO\.?\s*)?\d+[A-Z]?(?:[/-]\d+[A-Z]?)?\s+",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = re.sub(r"^[A-Z]{1,3}-?\d+[A-Z]?\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^\d+\s+", "", name).strip(" ,.-")
    trim = BUILDING_NAME_TRIM_RE.match(name)
    if trim:
        name = trim.group(1).strip(" ,.-")
    name = _deordinal(name) or name
    # "THANGAM 2ND CROSS ADITYA APARTMENT" is Aditya Apartment: keep what follows the
    # last street/ordinal token, since a building name never starts before one.
    words = name.split()
    cut = max(
        (i for i, w in enumerate(words)
         if _norm_project_text(w) in STREET_WORDS or ORDINAL_WORD_RE.match(_norm_project_text(w))),
        default=-1,
    )
    if cut >= 0 and len(words) - cut - 1 >= 1:
        tail = " ".join(words[cut + 1 :]).strip(" ,.-")
        if len(tail) >= 4:
            name = tail
    if len(name) < 4:
        return None
    if _reject_building_name(name):
        return None
    return name


def _trim_landmark(text: str | None, city: str | None, district: str | None) -> str | None:
    if not text:
        return None
    stops = [city, district, "INDIA", "KARNATAKA", "TAMIL NADU", "ANDHRA PRADESH"]
    upper = text.upper()
    cut = len(text)
    for stop in stops:
        if not stop:
            continue
        idx = upper.find(stop.upper())
        if idx > 5:
            cut = min(cut, idx)
    trimmed = text[:cut].strip(" ,.-")
    return trimmed if len(trimmed) >= 3 else text


def _regex_building_number(text: str) -> str | None:
    for pattern in BUILDING_NUMBER_RES:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1).strip("-/ ")
        if re.fullmatch(r"\d{6}", value):
            continue
        if value.isdigit() and len(value) > 5:
            continue
        return value
    return None


def _regex_landmark(text: str) -> str | None:
    parts: list[str] = []
    for match in LANDMARK_RE.finditer(text):
        part = match.group(1).strip(" ,.-")
        if len(part) >= 3:
            parts.append(part)
    return "; ".join(dict.fromkeys(parts)) or None


def _regex_building_name(text: str) -> str | None:
    match = BUILDING_NAME_RE.search(text)
    if not match:
        return None
    return _clean_building_name(match.group(1).strip(" ,.-"))


def _title_building_name(name: str) -> str:
    return " ".join(
        w.capitalize() if w.isupper() or w.islower() else w for w in name.split()
    )


def _valid_heuristic_building(name: str) -> bool:
    words = _norm_project_text(name).split()
    if len(words) < 2 or len(name.strip()) < 6:
        return False
    if any(w in BUILDING_STOP_TOKENS for w in words):
        return False
    if words[-1] in LOCALITY_LIKE_SUFFIXES and not re.search(
        BUILDING_SUFFIX, name, re.IGNORECASE
    ):
        return False
    if re.search(BUILDING_SUFFIX, name, re.IGNORECASE):
        return True
    return len(words) <= 5


def _sanitize_parser_building(name: str | None, text: str) -> str | None:
    if not name:
        return None
    cleaned = _clean_building_name(name)
    if cleaned and _valid_heuristic_building(cleaned):
        return _title_building_name(cleaned)
    norm_name = _norm_project_text(name)
    for stop in (" BANGALORE", " BENGALURU", " KARNATAKA", " INDIA"):
        idx = norm_name.find(stop.strip())
        if idx > 5:
            norm_name = norm_name[:idx].strip(" ,.-")
    match = BUILDING_NAME_RE.search(norm_name) or AFTER_UNIT_BUILDING_RE.search(norm_name)
    if match:
        candidate = _clean_building_name(match.group(1).strip(" ,.-"))
        if candidate and _valid_heuristic_building(candidate):
            return _title_building_name(candidate)
    if _valid_heuristic_building(norm_name) and len(norm_name.split()) <= 6:
        return _title_building_name(norm_name.title())
    return None


def _heuristic_building_name(text: str) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    match = EXTENDED_BUILDING_RE.search(norm_text)
    if match:
        candidate = _clean_building_name(match.group(1).strip(" ,.-"))
        if candidate and _valid_heuristic_building(candidate):
            return _title_building_name(candidate)
    match = AFTER_UNIT_BUILDING_RE.search(norm_text)
    if match:
        candidate = _clean_building_name(match.group(1).strip(" ,.-"))
        if candidate and _valid_heuristic_building(candidate):
            return _title_building_name(candidate)
    return None


def _layout_building_name(text: str) -> str | None:
    """Named layouts (e.g. MANJUSHREE NILAYA VSR LAYOUT) common in Bangalore bureau text."""
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    best: str | None = None
    for match in LAYOUT_PRE_RE.finditer(norm_text):
        tail = match.group(1).split()[-6:]
        name_words: list[str] = []
        for word in reversed(tail):
            if (
                word in LAYOUT_PRE_STREET
                or ORDINAL_WORD_RE.match(word)
                or LAYOUT_UNIT_RE.match(word)
                or LAYOUT_JUNK_RE.match(word)
            ):
                break
            name_words.insert(0, word)
        if len(name_words) < 2 or len(name_words) > 5:
            continue
        if any(w in LAYOUT_PRE_STREET for w in name_words):
            continue
        if sum(1 for w in name_words if len(w) <= 2) > 1:
            continue
        if not any(len(w) >= 4 for w in name_words):
            continue
        candidate = _clean_building_name(" ".join(name_words) + " Layout")
        if candidate:
            best = _title_building_name(candidate)
    return best


def _common_locality_block() -> frozenset[str]:
    global _COMMON_LOCALITY_BLOCK
    if _COMMON_LOCALITY_BLOCK is not None:
        return _COMMON_LOCALITY_BLOCK
    block = {
        _norm_project_text(name)
        for name in (
            "Jayanagar", "Vijayanagar", "Basavanagudi", "Banashankari", "Koramangala",
            "Indiranagar", "Whitefield", "Marathahalli", "Hebbal", "Yelahanka", "Rajajinagar",
            "Malleshwaram", "Mahadevapura", "Domlur", "Ulsoor", "Electronic City", "Bellandur",
            "Sarjapur", "Horamavu", "Kaggadasapura", "Banaswadi", "Peenya", "Nagarbhavi",
            "Subramanyapura", "Uttarahalli", "Kengeri", "Dasarahalli", "Btm Layout", "Hsr Layout",
        )
    }
    gaz = _load_blr_locality_gazetteer()
    for norm in gaz:
        if len(norm.split()) <= 2 and len(norm) >= 4:
            block.add(norm)
    _COMMON_LOCALITY_BLOCK = frozenset(block)
    return _COMMON_LOCALITY_BLOCK


def _is_locality_like_name(name: str, locality: str | None) -> bool:
    norm = _norm_project_text(name)
    if locality and norm == _norm_project_text(locality):
        return True
    if norm in _common_locality_block():
        return True
    words = norm.split()
    if len(words) <= 2 and norm in _load_blr_locality_gazetteer():
        return True
    return False


def _after_unit_building_name(text: str) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    best: str | None = None
    for match in AFTER_UNIT_BUILDING_RE.finditer(norm_text):
        candidate = _clean_building_name(match.group(1).strip(" ,.-"))
        if not candidate:
            continue
        words = candidate.split()
        if words[0] in LAYOUT_PRE_STREET or ORDINAL_WORD_RE.match(words[0]):
            continue
        if any(w in STREET_WORDS for w in words):
            continue
        if re.search(BUILDING_SUFFIX, candidate, re.IGNORECASE) or (
            2 <= len(words) <= 5 and not any(w in LAYOUT_PRE_STREET for w in words)
        ):
            if len(candidate) >= 5:
                best = _title_building_name(candidate)
    return best


def _landmark_building_name(text: str) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    for match in LANDMARK_RE.finditer(norm_text):
        chunk = match.group(1).strip(" ,.-")
        for stop in (" BANGALORE", " BENGALURU", " KARNATAKA", " INDIA", " NEAR", " BEHIND"):
            idx = chunk.find(stop.strip())
            if idx > 5:
                chunk = chunk[:idx].strip(" ,.-")
        suffix_match = re.search(
            rf"((?:[\w&.-]+\s+){{0,5}}(?:{BUILDING_SUFFIX}))\b",
            chunk,
            re.IGNORECASE,
        )
        if not suffix_match:
            continue
        candidate = _clean_building_name(suffix_match.group(1).strip(" ,.-"))
        if candidate and len(candidate.split()) >= 2:
            return _title_building_name(candidate)
    return None


def _single_word_layout_name(text: str, locality: str | None) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    best: str | None = None
    for match in SINGLE_WORD_LAYOUT_RE.finditer(norm_text):
        word = match.group(1)
        if word in LAYOUT_PRE_STREET:
            continue
        candidate = f"{word} Layout"
        if _is_locality_like_name(candidate, locality):
            continue
        if len(word) >= 5:
            best = _title_building_name(candidate)
    return best


def _society_building_name(text: str, locality: str | None) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    best: str | None = None
    best_len = 0
    for match in SOCIETY_BUILDING_RE.finditer(norm_text):
        candidate = match.group(1).strip(" ,.-")
        if _is_locality_like_name(candidate, locality):
            continue
        words = candidate.split()
        if len(words) < 2:
            continue
        if any(w in BUILDING_STOP_TOKENS for w in words):
            continue
        if sum(1 for w in words if len(w) <= 2) > 1:
            continue
        if len(words) < 3 and "COLONY" not in candidate and "HOUSING" not in candidate:
            if not any(len(w) >= 6 for w in words):
                continue
        if len(candidate) > best_len:
            best_len = len(candidate)
            best = _title_building_name(candidate)
    return best


def _bldg_token_building_name(text: str) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    match = BLDG_NAME_RE.search(norm_text)
    if not match:
        return None
    candidate = _clean_building_name(match.group(1).strip(" ,.-"))
    if not candidate or len(candidate) < 4 or len(candidate.split()) > 5:
        return None
    if "BUILD" in candidate.upper():
        return _title_building_name(candidate)
    return _title_building_name(f"{candidate} Bldg")


def _residence_building_name(text: str) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    match = RESIDENCE_NAME_RE.search(norm_text)
    if not match:
        return None
    candidate = _clean_building_name(match.group(1).strip(" ,.-"))
    if candidate and len(candidate.split()) >= 2:
        return _title_building_name(candidate)
    return None


def _match_project_fuzzy_backfill(text: str, city: str | None) -> str | None:
    """Strict brand-filtered fuzzy match for rows still missing building_name."""
    if _PROJECT_BY_BRAND is None:
        _load_project_dictionary()
    assert _PROJECT_BY_BRAND is not None

    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    words = norm_text.split()
    if len(words) < 2:
        return None

    brands = [w for w in words if w in _PROJECT_BY_BRAND]
    if not brands:
        return None

    candidates = _project_brand_candidates(city, brands)
    if not candidates:
        return None

    best_name: str | None = None
    best_score = FUZZY_PROJECT_BACKFILL_CUTOFF
    brand_positions = [i for i, w in enumerate(words) if w in brands]
    for start in brand_positions:
        for end in range(start + 2, min(start + 9, len(words) + 1)):
            phrase = " ".join(words[start:end])
            if len(phrase) < 8 or len(phrase.split()) < 2:
                continue
            hit = _best_project_fuzzy(phrase, candidates, FUZZY_PROJECT_BACKFILL_CUTOFF)
            if not hit or hit[1] < best_score:
                continue
            phrase_words = {w for w in phrase.split() if len(w) >= 4}
            cand_words = {w for w in _norm_project_text(hit[0]).split() if len(w) >= 4}
            if not phrase_words.intersection(cand_words):
                continue
            best_name, best_score = hit[0], hit[1]
    return best_name


def _backfill_building_name(
    text: str,
    *,
    locality: str | None,
    city: str | None,
    stats: Counter | None = None,
) -> str | None:
    """Last-chance building_name before _area_building_fallback (strict fuzzy dotcom inside)."""
    for stat_key, fn in (
        ("landmark_building_filled", lambda: _landmark_building_name(text)),
        ("single_layout_filled", lambda: _single_word_layout_name(text, locality)),
        ("bldg_token_filled", lambda: _bldg_token_building_name(text)),
        ("residence_building_filled", lambda: _residence_building_name(text)),
    ):
        hit = fn()
        if hit:
            if stats is not None:
                stats[stat_key] += 1
            return hit

    fuzzy_hit = _match_project_fuzzy_backfill(text, city)
    if fuzzy_hit:
        if stats is not None:
            stats["project_backfill_fuzzy_filled"] += 1
        return fuzzy_hit
    return None


def _locality_building_fallback(locality: str | None, text: str) -> str | None:
    """Use parsed locality as building when it clearly appears in address text."""
    if not locality:
        return None
    norm_loc = _norm_project_text(locality)
    words = norm_loc.split()
    if any(w in LOCALITY_STREET_TOKENS for w in words):
        return None
    if any(ORDINAL_WORD_RE.match(w) for w in words):
        return None
    if re.search(r"\d{3,}", norm_loc):
        return None
    if norm_loc in _common_locality_block() and len(words) <= 2:
        return None
    has_suffix = any(norm_loc.endswith(suffix) for suffix in LOCALITY_FALLBACK_SUFFIXES)
    if len(words) < 2 and not has_suffix:
        return None
    if len(words) == 1 and has_suffix and len(norm_loc) >= 6:
        if norm_loc not in _load_blr_locality_gazetteer():
            return None
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    compact_loc = norm_loc.replace(" ", "")
    compact_text = norm_text.replace(" ", "")
    if norm_loc not in norm_text and compact_loc not in compact_text:
        return None
    return _title_locality(locality)


def _named_area_building_name(text: str, locality: str | None) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    compact_text = norm_text.replace(" ", "")
    loc_norm = _norm_project_text(locality) if locality else ""
    block = _common_locality_block()
    best: str | None = None
    best_score = 0
    for norm_loc, canonical in _BLR_LOCALITY_SORTED or []:
        if len(norm_loc.split()) < 2 or norm_loc in block or norm_loc == loc_norm:
            continue
        if any(w in LOCALITY_STREET_TOKENS for w in norm_loc.split()):
            continue
        score = 0
        if norm_loc in norm_text:
            score = len(norm_loc)
        else:
            compact = norm_loc.replace(" ", "")
            if len(compact) >= 10 and compact in compact_text:
                score = len(compact)
        if score > best_score:
            best_score = score
            best = _title_locality(canonical)
    return best


def _single_area_building_name(text: str) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    block = _common_locality_block()
    best: str | None = None
    best_len = 0
    for match in SINGLE_AREA_RE.finditer(norm_text):
        token = match.group(1)
        if _norm_project_text(token) in block:
            continue
        if len(token) > best_len:
            best_len = len(token)
            best = _title_building_name(token)
    for match in TWO_WORD_LAYOUT_RE.finditer(norm_text):
        token = match.group(1)
        if _norm_project_text(token) in block:
            continue
        if len(token) > best_len:
            best_len = len(token)
            best = _title_building_name(token)
    return best


def _before_floor_building_name(text: str, locality: str | None) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    for match in BEFORE_FLOOR_BUILDING_RE.finditer(norm_text):
        candidate = _clean_building_name(match.group(1).strip(" ,.-"))
        if not candidate:
            continue
        words = candidate.split()
        if words[0] in LAYOUT_PRE_STREET or ORDINAL_WORD_RE.match(words[0]):
            continue
        if any(w in STREET_WORDS for w in words):
            continue
        if _is_locality_like_name(candidate, locality):
            continue
        if re.search(BUILDING_SUFFIX, candidate, re.IGNORECASE):
            return _title_building_name(candidate)
        if len(words) >= 2 and any(len(w) >= 5 for w in words):
            if not all(len(w) <= 6 for w in words[:2]):
                return _title_building_name(candidate)
    return None


def _enclave_building_name(text: str, locality: str | None) -> str | None:
    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    match = re.search(
        r"\b((?:[A-Z][A-Z0-9\-]+\s+){0,3}(?:ENCLAVE|ESTATE|TOWNSHIP))\b",
        norm_text,
    )
    if not match:
        return None
    candidate = _clean_building_name(match.group(1).strip(" ,.-"))
    if not candidate or len(candidate.split()) < 2:
        return None
    if _is_locality_like_name(candidate, locality):
        return None
    return _title_building_name(candidate)


def _area_building_fallback(
    text: str,
    *,
    locality: str | None,
    stats: Counter | None = None,
) -> str | None:
    """Last-resort named-area extraction from address text (not canonical dotcom)."""
    for stat_key, fn in (
        ("society_building_filled", lambda: _society_building_name(text, locality)),
        ("locality_building_filled", lambda: _locality_building_fallback(locality, text)),
        ("named_area_filled", lambda: _named_area_building_name(text, locality)),
        ("single_area_filled", lambda: _single_area_building_name(text)),
        ("enclave_building_filled", lambda: _enclave_building_name(text, locality)),
        ("before_floor_filled", lambda: _before_floor_building_name(text, locality)),
    ):
        hit = fn()
        if hit:
            if stats is not None:
                stats[stat_key] += 1
            return hit
    return None


_AREA_SUFFIXES = (
    "NAGAR", "NAGARA", "LAYOUT", "COLONY", "GARDEN", "GARDENS", "PURA", "PURAM", "HALLI",
    "HALLY", "PALYA", "PALYAM", "PET", "PETE", "EXTENSION", "ENCLAVE", "TOWN", "VILLAGE",
    "SANDRA", "KERE", "GUDI", "WADI", "AGRAHARA",
)


def _precise_parser_locality(locality: str | None, building_name: str | None = None) -> str | None:
    """Trailing area phrase of parser output (NO 654 B BLOCK SUBASH NAGAR -> SUBASH NAGAR).

    Kept only when it ends in an area suffix (NAGAR, LAYOUT, PALYA, ...); street
    fragments like 1ST MAIN or 2 3 OBALAPPA CROSS give None.
    """
    words = _CITY_WORDS_RE.sub(" ", _deordinal(locality or "") or "").upper()
    words = _norm_project_text(words).split()
    while words and words[-1].isdigit():
        words.pop()
    tail: list[str] = []
    for w in reversed(words):
        if w in LOCALITY_STREET_TOKENS or w in STREET_WORDS or any(ch.isdigit() for ch in w):
            break
        tail.insert(0, w)
    ends = [i for i, w in enumerate(tail) if w.endswith(_AREA_SUFFIXES)]
    if not ends or ends[-1] != len(tail) - 1:
        return None
    tail = tail[: ends[0] + 1][-4:]  # first area phrase: GARUDACHAR PALYA, not + SHETTY LAYOUT
    bn_words = set(_norm_project_text(building_name or "").split())
    while len(tail) > 1 and tail[0] in bn_words:
        tail = tail[1:]
    if not any(len(w) >= 4 for w in tail):
        return None
    return _title_locality(" ".join(tail).title())


def _fuzzy_window_locality(text: str, candidates: list[str]) -> str | None:
    """Best 1-4 word window of text vs a pin's own localities (MALLESHWARA M -> Malleswaram)."""
    if not candidates or not _HAS_RAPIDFUZZ or _rf_process is None:
        return None
    by_compact = {}
    for c in candidates:
        compact = _triplet_norm(c).replace(" ", "")
        if len(compact) >= 6:
            by_compact.setdefault(compact, c)
    if not by_compact:
        return None
    words = _triplet_locality_text(text).split()
    best: tuple[float, int, str] | None = None
    for size in range(1, 5):
        for i in range(len(words) - size + 1):
            window = "".join(words[i : i + size])
            if len(window) < 6:
                continue
            hit = _rf_process.extractOne(
                window, list(by_compact), scorer=_rf_fuzz.ratio, score_cutoff=88
            )
            if hit and (best is None or (hit[1], len(hit[0])) > best[:2]):
                best = (hit[1], len(hit[0]), by_compact[hit[0]])
    return _title_locality(best[2]) if best else None


def _pin_locality_candidates(zip_code: str, city_key: str) -> list[str]:
    """Localities known for a pin: merged pincode->locality file plus India Post."""
    out = list(_load_pincode_localities().get(zip_code, []))
    out.extend(pincode.known_localities(zip_code) or [])
    return out


def _regex_locality(text: str, city: str | None) -> str | None:
    best: str | None = None
    city_low = (city or "").lower()
    for match in LOCALITY_RE.finditer(text):
        candidate = match.group(1).strip(" ,.-")
        if city_low and city_low in candidate.lower() and len(candidate.split()) <= 2:
            continue
        if len(candidate) >= 4:
            best = candidate
    return best


# ---------------------------------------------------------------------------
# Confidence score (0.0–1.0): pincode DB, state/city cross-check, field fill, dotcom/OSM
# ---------------------------------------------------------------------------


def _score_confidence(
    *,
    pin: str | None,
    state_code: str | None,
    city: str | None,
    district: str | None,
    locality: str | None,
    building_number: str | None,
    building_name: str | None,
    landmark: str | None,
    lookup: dict | None,
    dotcom_matched: str = "No",
    dotcom_match_rule: str = "",
    location_matched: str = "No",
    locality_source: str = "",
    locality_fuzzy_score: float = 0.0,
) -> float:
    """Reliability-weighted score: pincode/state cross-check, match rule, locality source."""
    _ = district, locality_fuzzy_score
    score = 0.0

    if pin and pin.isdigit() and len(pin) == 6:
        score += 0.18
        if lookup:
            score += 0.07

    expected_state = STATE_ABBREV.get((state_code or "").strip().upper())
    lookup_state = lookup.get("state") if lookup else None
    if expected_state and lookup_state:
        if _states_match(expected_state, lookup_state):
            score += 0.15
        else:
            score -= 0.12
    elif lookup_state:
        score += 0.05

    lookup_city = lookup.get("city") if lookup else None
    if city and lookup_city and (
        _places_match(city, lookup_city)
        or _resolve_dotcom_city_key(lookup_city, pin or "", lookup) == _norm_project_city(city)
    ):
        score += 0.10
    elif city:
        score += 0.04

    bn = (building_name or "").strip()
    if bn:
        score += 0.22
        if dotcom_matched == "Yes":
            score += DOTCOM_RULE_WEIGHT.get(dotcom_match_rule, 0.12)
        elif location_matched == "Yes":
            score += 0.12
        elif locality and bn.lower() == locality.strip().lower():
            score += 0.04
        else:
            score += 0.06

    if locality:
        score += LOCALITY_SOURCE_WEIGHT.get(locality_source, 0.05)
    if building_number:
        score += 0.04
    if landmark:
        score += 0.04

    return round(max(0.0, min(score, 1.0)), 3)


def _validated_ner_building(name: str | None, locality: str | None) -> str | None:
    """Keep a model building span only if it passes the same checks as a rules-based one."""
    cleaned = _clean_building_name(_deordinal(name or "") or "")
    if not cleaned or not _valid_heuristic_building(cleaned):
        return None
    words = _norm_project_text(cleaned).split()
    if any(w in BUILDING_STOP_TOKENS or w in STREET_WORDS for w in words):
        return None
    if _is_locality_like_name(cleaned, locality):
        return None
    return _title_building_name(cleaned)


def _validated_ner_locality(name: str | None, city_key: str) -> str | None:
    """Keep a model locality span only if it is a locality we already know (never a city/state)."""
    cleaned = _clean_locality_name(name)
    if not cleaned:
        return None
    norm = _triplet_norm(cleaned)
    if not norm or any(w in STREET_WORDS or w in GEO_STOP for w in norm.split()):
        return None
    known = norm in _load_blr_locality_gazetteer() or norm in {
        _triplet_norm(loc) for locs in (_load_pincode_localities().values()) for loc in locs
    }
    if not known:
        _load_triplet_index()
        assert _TRIPLET_SUBLOC_DISPLAY is not None
        known = norm in _TRIPLET_SUBLOC_DISPLAY.get(city_key, {})
    return _title_locality(cleaned) if known else None


def parse_address_row(address: str, zip_code: str, stats: Counter | None = None) -> dict:
    """Parse one address into structured fields (no dotcom_matched column here).

    Steps:
      1. bureau_preprocess + bharataddress.parse → base out{...}.
      2. Regex backfill for building_number and landmark.
      3. building_name priority:
           dotcom (_match_project_dictionary) → OSM (_match_location_building)
           → sanitize parser output → compound/regex/heuristic/layout/after-unit
           → _backfill_building_name (strict fuzzy dotcom)
           → _area_building_fallback (named areas, enclave, locality-as-name).
      4. India Post lookup fills city/district; locality from pin map, BLR gazetteer, fuzzy.
      5. _deordinal on string fields.

    Optional stats Counter tracks which enrichment path filled each field (for reports).
    """
    cleaned = prepare_address(address, zip_code)
    prepared = cleaned.text
    result = parse(cleaned.parser_text)
    lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None

    out = {
        "building_number": result.building_number,
        "building_name": result.building_name,
        "landmark": result.landmark,
        "locality": result.locality,
        "city": result.city,
        "district": result.district,
    }
    if out["locality"]:
        out["locality"] = _CITY_WORDS_RE.sub(" ", out["locality"]).strip(" ,.-") or None
    city_key = _resolve_dotcom_city_key(out["city"], zip_code, lookup)
    if city_key:
        out["city"] = _DOTCOM_CITY_DISPLAY[city_key]

    if not out["building_number"]:
        out["building_number"] = _regex_building_number(prepared) or _regex_building_number(address)
    if not out["landmark"]:
        out["landmark"] = _regex_landmark(prepared) or _regex_landmark(address)
    if not out["landmark"] and cleaned.landmarks:
        out["landmark"] = "; ".join(cleaned.landmarks)
    out["landmark"] = _trim_landmark(out["landmark"], out.get("city"), out.get("district"))

    project_city = out.get("city") or (lookup.get("city") if lookup else None)
    project_hit, match_kind = _match_project_dictionary(prepared, project_city)
    if not project_hit:
        project_hit, match_kind = _match_project_dictionary(address, project_city)

    location_hit = None
    if not project_hit and not (out.get("building_name") or "").strip():
        location_hit = _match_location_building(prepared) or _match_location_building(address)

    if project_hit:
        if match_kind == "fuzzy" and stats is not None:
            stats["project_dict_fuzzy_filled"] += 1
        elif out["building_name"] and out["building_name"] != project_hit and stats is not None:
            stats["project_dict_overrode"] += 1
        elif not out["building_name"] and stats is not None:
            stats["project_dict_filled"] += 1
        out["building_name"] = project_hit
    elif location_hit:
        if out["building_name"] and out["building_name"] != location_hit and stats is not None:
            stats["location_db_overrode"] += 1
        elif not out["building_name"] and stats is not None:
            stats["location_db_filled"] += 1
        out["building_name"] = location_hit
    else:
        parser_bn = _sanitize_parser_building(out.get("building_name"), prepared)
        if parser_bn:
            if not out["building_name"] and stats is not None:
                stats["parser_building_filled"] += 1
            out["building_name"] = parser_bn
        elif not out["building_name"]:
            out["building_name"] = (
                _compound_building_name(prepared)
                or _compound_building_name(address)
                or _regex_building_name(prepared)
                or _regex_building_name(address)
            )
        if not out["building_name"]:
            heuristic = _heuristic_building_name(prepared) or _heuristic_building_name(address)
            if not out["building_name"] and heuristic:
                out["building_name"] = heuristic
                if stats is not None:
                    stats["heuristic_building_filled"] += 1
            if not out["building_name"]:
                after_unit = _after_unit_building_name(prepared) or _after_unit_building_name(address)
                if after_unit:
                    out["building_name"] = after_unit
                    if stats is not None:
                        stats["after_unit_filled"] += 1
        if not out["building_name"]:
            layout_hit = _layout_building_name(prepared) or _layout_building_name(address)
            if layout_hit:
                out["building_name"] = layout_hit
                if stats is not None:
                    stats["layout_building_filled"] += 1
        elif out["building_name"] and not project_hit and not location_hit:
            cleaned = _clean_building_name(out["building_name"])
            if cleaned:
                out["building_name"] = cleaned

    if lookup:
        out["city"] = out["city"] or lookup.get("city")
        out["district"] = out["district"] or lookup.get("district")

    parser_locality = out["locality"] or _regex_locality(prepared, out["city"]) or _regex_locality(
        address, out["city"]
    )
    has_pin = zip_code.isdigit() and len(zip_code) == 6
    # Most precise first: dotcom subLocation written in text > exact pincode/gazetteer name
    # in text > fuzzy matches > raw parser output.
    locality_steps = (
        ("dotcom_subloc", lambda: _dotcom_locality_from_text(address, city_key, zip_code)),
        ("pincode_map", lambda: has_pin and (
            _match_pincode_locality(prepared, zip_code, fuzzy=False)
            or _match_pincode_locality(address, zip_code, fuzzy=False)
        )),
        ("gazetteer", lambda: _match_blr_locality_gazetteer(prepared, fuzzy=False)
            or _match_blr_locality_gazetteer(address, fuzzy=False)),
        ("pincode_fuzzy", lambda: has_pin and (
            _fuzzy_window_locality(address, _pin_locality_candidates(zip_code, city_key))
            or _match_pincode_locality(prepared, zip_code, seed=parser_locality)
            or _fuzzy_locality(prepared, zip_code, seed=parser_locality)[0]
        )),
        ("gazetteer_fuzzy", lambda: _match_blr_locality_gazetteer(prepared)),
        ("parser", lambda: _precise_parser_locality(parser_locality, out.get("building_name"))),
    )
    out["locality"], out["locality_source"] = None, ""
    for source, step in locality_steps:
        loc = _clean_locality_name(step() or None)
        if loc:
            if source != "dotcom_subloc":
                loc = _dotcom_subloc_spelling(loc, city_key) or loc
            out["locality"], out["locality_source"] = loc, source
            break

    if not out["building_name"]:
        backfill = _backfill_building_name(
            prepared,
            locality=out.get("locality"),
            city=project_city,
            stats=stats,
        )
        if not backfill:
            backfill = _backfill_building_name(
                address,
                locality=out.get("locality"),
                city=project_city,
                stats=stats,
            )
        if backfill:
            out["building_name"] = backfill

    if not out["building_name"]:
        area_hit = _area_building_fallback(
            prepared,
            locality=out.get("locality"),
            stats=stats,
        )
        if not area_hit:
            area_hit = _area_building_fallback(
                address,
                locality=out.get("locality"),
                stats=stats,
            )
        if area_hit:
            out["building_name"] = area_hit

    if USE_NER and (not out["building_name"] or not out["locality"]):
        spans = ner_stage.extract(address)
        if not out["building_name"]:
            candidate = _validated_ner_building(spans.get("building_name"), out.get("locality"))
            if candidate:
                out["building_name"] = candidate
                if stats is not None:
                    stats["ner_building_name_filled"] += 1
        if not out["locality"]:
            candidate = _validated_ner_locality(spans.get("locality"), city_key)
            if candidate:
                out["locality"], out["locality_source"] = candidate, "ner"
                if stats is not None:
                    stats["ner_locality_filled"] += 1

    for key in ("building_number", "building_name", "landmark", "locality", "city", "district"):
        out[key] = _deordinal(out[key])
    return out


# ---------------------------------------------------------------------------
# Batch I/O: single-row wrapper, COMBO CSV, Excel export, cross_check_report.txt
# ---------------------------------------------------------------------------


def _extract_zip_from_text(text: str) -> str:
    """First 6-digit Indian pincode token in text (ZIP_FROM_TEXT_RE)."""
    match = ZIP_FROM_TEXT_RE.search(text or "")
    return match.group(1) if match else ""


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _build_parsed_output_row(
    *,
    address: str,
    state_code: str,
    zip_code: str,
    stats: Counter,
    city_hint: str = "",
    locality_hint: str = "",
) -> dict[str, str]:
    """Run parse_address_row; add ADDRESS/State/ZIP, match flags, and confidence."""
    if not zip_code and address:
        zip_code = _extract_zip_from_text(address)
    if not zip_code and address:
        try:
            zip_code = parse(bureau_preprocess(address, "")).pincode or ""
        except Exception:
            zip_code = ""

    parsed = parse_address_row(address, zip_code, stats=stats)
    if city_hint and not (parsed.get("city") or "").strip():
        parsed["city"] = city_hint
    if locality_hint and not (parsed.get("locality") or "").strip():
        parsed["locality"] = locality_hint
        parsed["locality_source"] = "sheet_hint"

    row_out: dict[str, str] = {
        "ADDRESS": address,
        "State code": state_code,
        "ZIP": zip_code,
        **{k: ("" if v is None else v) for k, v in parsed.items()},
    }
    _apply_dotcom_triplet(row_out, address=address, stats=stats)
    row_out[LOCATION_MATCH_COL] = _location_matched_flag(row_out.get("building_name"))

    lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
    row_out["confidence"] = str(
        _score_confidence(
            pin=zip_code or None,
            state_code=state_code,
            city=row_out.get("city"),
            district=row_out.get("district"),
            locality=row_out.get("locality"),
            building_number=row_out.get("building_number"),
            building_name=row_out.get("building_name"),
            landmark=row_out.get("landmark"),
            lookup=lookup,
            dotcom_matched=row_out[DOTCOM_MATCH_COL],
            dotcom_match_rule=row_out.get(DOTCOM_RULE_COL, ""),
            location_matched=row_out[LOCATION_MATCH_COL],
            locality_source=row_out.get("locality_source", ""),
        )
    )
    return row_out


def _accumulate_parse_stats(stats: Counter, row_out: dict[str, str], zip_code: str, state_code: str) -> None:
    """Increment cross_check_report counters for one output row."""
    stats["total"] += 1
    if row_out.get(DOTCOM_MATCH_COL) == "Yes":
        stats["dotcom_matched_yes"] += 1
    if row_out.get(LOCATION_MATCH_COL) == "Yes":
        stats["location_matched_yes"] += 1
    if not zip_code:
        stats["missing_zip"] += 1
    if not state_code:
        stats["missing_state_code"] += 1
    lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
    if lookup:
        stats["pincode_in_db"] += 1
    else:
        stats["pincode_not_in_db"] += 1
    expected_state = STATE_ABBREV.get(state_code.strip().upper())
    if expected_state and lookup and _states_match(expected_state, lookup.get("state")):
        stats["state_code_matches_pincode_lookup"] += 1
    elif expected_state and lookup:
        stats["state_code_mismatch_pincode_lookup"] += 1
    stats[f"locality_source_{row_out.get('locality_source') or ''}"] += 1
    for col in PARSED_COLS:
        if row_out.get(col) not in (None, ""):
            stats[f"filled_{col}"] += 1
    conf = float(row_out.get("confidence") or 0)
    if conf >= 0.8:
        stats["confidence_ge_0_8"] += 1
    elif conf >= 0.6:
        stats["confidence_ge_0_6"] += 1


def process_csv(limit: int | None = None) -> dict:
    """Read INPUT_CSV (ADDRESS, State code, ZIP), write OUTPUT_CSV + REPORT_TXT.

    A limited run writes COMBO_DEMOG_parsed_limitN.csv instead, so a quick test can never
    replace the full output (running `python Address.py 1` once did exactly that).
    """
    global OUTPUT_CSV, REPORT_TXT
    if limit:
        OUTPUT_CSV = OUTPUT_CSV.with_name(f"{OUTPUT_CSV.stem}_limit{limit}.csv")
        REPORT_TXT = REPORT_TXT.with_name(f"{REPORT_TXT.stem}_limit{limit}.txt")
    stats: Counter = Counter()
    mismatches: list[str] = []

    with INPUT_CSV.open(newline="", encoding="utf-8") as fin:
        reader = csv.DictReader(fin)
        rows = list(reader)
    if limit:
        rows = rows[:limit]

    out_fields = ["ADDRESS", "State code", "ZIP", *OUTPUT_COLS]  # ADDRESS = original bureau text
    t0 = time.perf_counter()

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=out_fields)
        writer.writeheader()

        for i, row in enumerate(rows, start=2):
            address = (row.get("ADDRESS") or "").strip()
            state_code = (row.get("State code") or "").strip().upper()
            zip_code = (row.get("ZIP") or "").strip()

            row_out = _build_parsed_output_row(
                address=address,
                state_code=state_code,
                zip_code=zip_code,
                stats=stats,
            )
            writer.writerow(row_out)

            _accumulate_parse_stats(stats, row_out, zip_code, state_code)

            lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
            parsed_city = row_out.get("city")
            if lookup and parsed_city and (
                _places_match(parsed_city, lookup.get("city"))
                or _resolve_dotcom_city_key(lookup.get("city"), zip_code, lookup)
                == _norm_project_city(parsed_city)
            ):
                stats["parsed_city_matches_pincode_lookup"] += 1
                if phonetic.fuzzy_ratio(parsed_city, lookup.get("city")) >= FUZZY_PLACE_CUTOFF:
                    stats["parsed_city_fuzzy_match"] += 1
            if zip_code:
                stats["zip_matches_parsed_pincode"] += 1

            if len(mismatches) < 20:
                expected_state = STATE_ABBREV.get(state_code)
                if expected_state and lookup and not _states_match(expected_state, lookup.get("state")):
                    mismatches.append(
                        f"line {i}: state_code={state_code} ({expected_state}) "
                        f"vs lookup={lookup.get('state')} ZIP={zip_code}"
                    )

    elapsed = time.perf_counter() - t0
    summary = {
        "rows": stats["total"],
        "elapsed_sec": round(elapsed, 2),
        "rows_per_sec": round(stats["total"] / elapsed, 1) if elapsed else 0,
        "stats": dict(stats),
        "sample_mismatches": mismatches,
    }
    _write_report(summary)
    return summary


def process_excel_file(
    input_path: Path,
    output_path: Path,
    *,
    sheet: str | int = 0,
    address_column: str = "Address",
    city_column: str = "Location",
    locality_column: str = "City",
    default_state_code: str = "MH",
    report_path: Path | None = None,
    limit: int | None = None,
) -> dict:
    """Parse an Excel sheet: keeps original columns + parsed OUTPUT_COLS.

    Default columns: Address, Location (city hint), City (locality hint).
    State code defaults to default_state_code when not in sheet.
    """
    import openpyxl

    stats: Counter = Counter()
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path or output_path.with_suffix(".report.txt")

    wb = openpyxl.load_workbook(input_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[sheet]] if isinstance(sheet, int) else wb[sheet]

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    headers = [_cell_str(h) for h in header_row]
    col_index = {name.strip().lower(): idx for idx, name in enumerate(headers) if name.strip()}

    def col(name: str) -> int | None:
        return col_index.get(name.strip().lower())

    addr_idx = col(address_column)
    if addr_idx is None:
        wb.close()
        raise SystemExit(f"Missing address column {address_column!r} in {input_path.name}")

    city_idx = col(city_column)
    locality_idx = col(locality_column)

    out_fields = headers + ["ADDRESS", "State code", "ZIP", *OUTPUT_COLS]
    t0 = time.perf_counter()

    with output_path.open("w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()

        for i, row_vals in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if limit and i - 2 >= limit:
                break
            cells = list(row_vals) + [""] * max(0, len(headers) - len(row_vals))
            source = {headers[j]: _cell_str(cells[j]) for j in range(len(headers))}

            address = _cell_str(cells[addr_idx]) if addr_idx < len(cells) else ""
            city_hint = _cell_str(cells[city_idx]) if city_idx is not None and city_idx < len(cells) else ""
            locality_hint = _cell_str(cells[locality_idx]) if locality_idx is not None and locality_idx < len(cells) else ""

            zip_code = _extract_zip_from_text(address)
            state_code = default_state_code.strip().upper()
            if zip_code.isdigit() and len(zip_code) == 6:
                lookup = pincode.lookup(zip_code)
                if lookup and lookup.get("state"):
                    for code, name in STATE_ABBREV.items():
                        if _states_match(name, lookup.get("state")):
                            state_code = code
                            break

            parsed_out = _build_parsed_output_row(
                address=address,
                state_code=state_code,
                zip_code=zip_code,
                stats=stats,
                city_hint=city_hint,
                locality_hint=locality_hint,
            )
            zip_code = parsed_out["ZIP"]
            writer.writerow({**source, **parsed_out})
            _accumulate_parse_stats(stats, parsed_out, zip_code, state_code)

            if i % 5000 == 0:
                print(f"... parsed {i - 1} rows", file=sys.stderr)

    wb.close()
    elapsed = time.perf_counter() - t0
    summary = {
        "rows": stats["total"],
        "elapsed_sec": round(elapsed, 2),
        "rows_per_sec": round(stats["total"] / elapsed, 1) if elapsed else 0,
        "stats": dict(stats),
        "sample_mismatches": [],
    }
    lines = [
        "bharataddress parse report (excel input)",
        "=" * 40,
        f"Input:  {input_path.name}",
        f"Output: {output_path.name}",
        f"Rows processed: {stats['total']}",
        f"Elapsed: {summary['elapsed_sec']}s ({summary['rows_per_sec']} rows/s)",
        "",
        f"  dotcom_matched Yes: {stats.get('dotcom_matched_yes', 0)}",
        f"  location_matched Yes: {stats.get('location_matched_yes', 0)}",
        f"  filled building_name: {stats.get('filled_building_name', 0)}",
        f"  filled locality: {stats.get('filled_locality', 0)}",
        f"  ZIP present in India Post DB: {stats.get('pincode_in_db', 0)}",
        f"  confidence >= 0.8: {stats.get('confidence_ge_0_8', 0)}",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def _write_report(summary: dict) -> None:
    """Write human-readable stats to REPORT_TXT from process_csv() summary dict."""
    s = summary["stats"]
    total = s["total"]
    lines = [
        "bharataddress cross-check report (bureau-enriched pipeline)",
        "=" * 40,
        f"Input:  {INPUT_CSV.name}",
        f"Output: {OUTPUT_CSV.name}",
        f"Rows processed: {total}",
        f"Elapsed: {summary['elapsed_sec']}s ({summary['rows_per_sec']} rows/s)",
        "",
        "Pincode / state cross-check (CSV vs India Post lookup)",
        "-" * 40,
        f"  ZIP present in India Post DB:     {s.get('pincode_in_db', 0)}",
        f"  ZIP not in DB / invalid:          {s.get('pincode_not_in_db', 0)}",
        f"  State code matches pincode state: {s.get('state_code_matches_pincode_lookup', 0)}",
        f"  State code mismatch:              {s.get('state_code_mismatch_pincode_lookup', 0)}",
        f"  Parsed city matches pincode lookup: {s.get('parsed_city_matches_pincode_lookup', 0)}",
        "",
        "Fuzzy matching (phonetic.best_match / fuzzy_ratio)",
        "-" * 40,
        f"  City matched with fuzzy_ratio:         {s.get('parsed_city_fuzzy_match', 0)}",
        "  address_similarity: not used (requires two addresses; dedup only)",
        "",
        "Project dictionary (dotcom.project.csv)",
        "-" * 40,
        f"  building_name filled from dictionary:  {s.get('project_dict_filled', 0)}",
        f"  building_name replaced by dictionary:  {s.get('project_dict_overrode', 0)}",
        f"  building_name fuzzy (unmatched only):  {s.get('project_dict_fuzzy_filled', 0)}",
        f"  dotcom_matched Yes (project+city+subLocation): {s.get('dotcom_matched_yes', 0)}",
        f"    option 1 project + locality + pincode:       {s.get('dotcom_rule_project_locality_pincode', 0)}",
        f"    option 3 project + locality (pincode differs):{s.get('dotcom_rule_project_locality', 0)}",
        f"    option 2 project + pincode (locality absent): {s.get('dotcom_rule_project_pincode', 0)}",
        f"    option 4 unique name within {UNIQUE_NEARBY_MAX_KM}km of subLoc:  "
        f"{s.get('dotcom_rule_project_unique_nearby', 0)}",
        f"  No: project found, subLocation not confirmed:  {s.get('dotcom_reject_subloc_mismatch', 0)}",
        f"  No: generic/fuzzy name, pincode only (needs locality): {s.get('dotcom_reject_generic_pincode_only', 0)}",
        f"  No: no dotcom project of that city in address: {s.get('dotcom_reject_no_project', 0)}",
        f"  No: city unresolved:                           {s.get('dotcom_reject_no_city', 0)}",
        f"  building_name from location master:    {s.get('location_db_filled', 0)}",
        f"  location_matched Yes (canonical OSM):  {s.get('location_matched_yes', 0)}",
        f"  building_name from parser cleanup:     {s.get('parser_building_filled', 0)}",
        f"  building_name from heuristics:         {s.get('heuristic_building_filled', 0)}",
        f"  building_name from layout names:       {s.get('layout_building_filled', 0)}",
        f"  building_name after unit token:        {s.get('after_unit_filled', 0)}",
        f"  building_name from landmark phrase:    {s.get('landmark_building_filled', 0)}",
        f"  building_name from single-word layout: {s.get('single_layout_filled', 0)}",
        f"  building_name from society/colony:     {s.get('society_building_filled', 0)}",
        f"  building_name from parsed locality:    {s.get('locality_building_filled', 0)}",
        f"  building_name from named area text:    {s.get('named_area_filled', 0)}",
        f"  building_name from area token:         {s.get('single_area_filled', 0)}",
        f"  building_name from enclave/estate:     {s.get('enclave_building_filled', 0)}",
        f"  building_name before floor token:      {s.get('before_floor_filled', 0)}",
        f"  building_name from BLDG token:         {s.get('bldg_token_filled', 0)}",
        f"  building_name from residence token:      {s.get('residence_building_filled', 0)}",
        f"  building_name fuzzy backfill:          {s.get('project_backfill_fuzzy_filled', 0)}",
        f"  building_name from NER model (--ner):   {s.get('ner_building_name_filled', 0)}",
        "",
        "Locality source (final column locality_source; most precise first)",
        "-" * 40,
        f"  dotcom_match   (dotcom row matched, subLocation): {s.get('locality_source_dotcom_match', 0)}",
        f"  dotcom_subloc  (dotcom subLocation in text):     {s.get('locality_source_dotcom_subloc', 0)}",
        f"  pincode_map    (pin's locality name in text):    {s.get('locality_source_pincode_map', 0)}",
        f"  gazetteer      (BLR locality name in text):      {s.get('locality_source_gazetteer', 0)}",
        f"  pincode_fuzzy  (fuzzy vs pin's localities):      {s.get('locality_source_pincode_fuzzy', 0)}",
        f"  gazetteer_fuzzy (fuzzy vs BLR localities):       {s.get('locality_source_gazetteer_fuzzy', 0)}",
        f"  parser         (bharataddress / regex):          {s.get('locality_source_parser', 0)}",
        f"  ner            (TinyBERT model, --ner only):     {s.get('locality_source_ner', 0)}",
        f"  empty:                                           {s.get('locality_source_', 0)}",
        "",
        "Field fill rates (bharataddress + bureau cleanup/backfill)",
        "-" * 40,
    ]
    for col in PARSED_COLS:
        filled = s.get(f"filled_{col}", 0)
        pct = 100 * filled / total if total else 0
        lines.append(f"  {col:18s} {filled:6d}  ({pct:.1f}%)")

    lines.extend(
        [
            "",
            "Confidence distribution",
            "-" * 40,
            "  Scoring: pincode/state cross-check + building dictionary match weight",
            f"  >= 0.8: {s.get('confidence_ge_0_8', 0)}",
            f"  >= 0.6 (and < 0.8): {s.get('confidence_ge_0_6', 0)}",
            f"  < 0.6: {total - s.get('confidence_ge_0_8', 0) - s.get('confidence_ge_0_6', 0)}",
            "",
            "Sample state-code mismatches",
            "-" * 40,
        ]
    )
    for m in summary.get("sample_mismatches", []):
        lines.append(f"  {m}")

    REPORT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Positional int = --limit; flags: --filter-blr, --ner, --input=/--output=
    limit = None
    filter_blr = "--filter-blr" in sys.argv
    USE_NER = "--ner" in sys.argv
    if USE_NER and not ner_stage.available():
        raise SystemExit("--ner needs transformers: pip install transformers torch")
    input_path: Path | None = None
    output_path: Path | None = None
    for arg in sys.argv[1:]:
        if arg.isdigit():
            limit = int(arg)
        elif arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
        elif arg.startswith("--input="):
            input_path = Path(arg.split("=", 1)[1])
        elif arg.startswith("--output="):
            output_path = Path(arg.split("=", 1)[1])

    if input_path:
        if not output_path:
            output_path = input_path.with_name(f"{input_path.stem}_parsed.csv")
        result = process_excel_file(input_path, output_path, limit=limit)
        print(f"Wrote {output_path} ({result['rows']} rows)")
        print(f"Report: {output_path.with_suffix('.report.txt')}")
    elif filter_blr:
        kept = filter_bangalore_csv()
        print(f"Filtered {INPUT_CSV} to {kept} Bangalore rows (backup: COMBO_DEMOG_all_cities.csv)")
    else:
        result = process_csv(limit=limit)
        print(f"Wrote {OUTPUT_CSV} ({result['rows']} rows)")
        print(f"Report: {REPORT_TXT}")
