#!/usr/bin/env python3
"""Parse COMBO_DEMOG.csv with bharataddress + bureau-specific cleanup/enrichment."""

from __future__ import annotations

import csv
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for pkg_root in (ROOT / "_vendor", ROOT / "bharataddress"):
    if (pkg_root / "bharataddress" / "__init__.py").exists():
        sys.path.insert(0, str(pkg_root))
        break

from bharataddress import parse, pincode  # noqa: E402
from bharataddress import phonetic  # noqa: E402

# Fuzzy thresholds — tuned for bureau OCR noise, not loose guessing.
FUZZY_LOCALITY_CUTOFF = 0.82
FUZZY_PLACE_CUTOFF = 0.85

INPUT_CSV = ROOT / "COMBO_DEMOG.csv"
OUTPUT_CSV = ROOT / "COMBO_DEMOG_parsed.csv"
REPORT_TXT = ROOT / "cross_check_report.txt"
PROJECT_CSV = ROOT / "dotcom.project.csv"
PINCODE_LOCALITY_CSV = ROOT / "Pincode To Locality  Mapping.csv"
LOCATION_MASTER_CSV = ROOT.parent / "india_location_db" / "data" / "india_location_master.csv"
BLR_ZIP_PREFIX = "560"

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
}

_PROJECT_INDEX: dict[str, dict[str, str]] | None = None
_PROJECT_GLOBAL: dict[str, str] | None = None
_PROJECT_BY_BRAND: dict[str, list[tuple[str, str, str]]] | None = None
_PROJECT_CANONICAL: set[str] | None = None
_PROJECT_COLLAPSED_BY_BRAND: dict[str, list[tuple[str, str]]] | None = None
_PINCODE_LOCALITIES: dict[str, list[str]] | None = None
_LOCATION_BUILDINGS: dict[str, str] | None = None
_LOCATION_BY_BRAND: dict[str, list[str]] | None = None
_LOCATION_BUILDINGS_SORTED: list[tuple[str, str]] | None = None
_LOCATION_CANONICAL: set[str] | None = None
_BLR_LOCALITY_GAZETTEER: dict[str, str] | None = None
_BLR_LOCALITY_SORTED: list[tuple[str, str]] | None = None
_BLR_LOCALITY_CANONICAL: list[str] | None = None
_COMMON_LOCALITY_BLOCK: frozenset[str] | None = None

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
LOCATION_MATCH_COL = "location_matched"
OUTPUT_COLS = (
    "building_number",
    "building_name",
    DOTCOM_MATCH_COL,
    LOCATION_MATCH_COL,
    "landmark",
    "locality",
    "city",
    "district",
    "confidence",
)

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


def _norm_project_text(text: str) -> str:
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
    """Load dotcom.project.csv as norm_phrase -> canonical projectName maps."""
    global _PROJECT_INDEX, _PROJECT_GLOBAL, _PROJECT_BY_BRAND, _PROJECT_CANONICAL
    global _PROJECT_COLLAPSED_BY_BRAND
    if _PROJECT_INDEX is not None and _PROJECT_GLOBAL is not None:
        return _PROJECT_INDEX, _PROJECT_GLOBAL

    by_city: dict[str, dict[str, str]] = {}
    global_map: dict[str, str] = {}
    brand_raw: dict[str, list[tuple[str, str, str]]] = {}
    collapsed_by_brand: dict[str, list[tuple[str, str]]] = {}
    canonical: set[str] = set()
    if not PROJECT_CSV.exists():
        _PROJECT_INDEX, _PROJECT_GLOBAL = by_city, global_map
        _PROJECT_BY_BRAND, _PROJECT_CANONICAL = brand_raw, canonical
        _PROJECT_COLLAPSED_BY_BRAND = collapsed_by_brand
        return by_city, global_map

    with PROJECT_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("projectData.projectName") or "").strip()
            if not name:
                continue
            norm_name = _norm_project_text(name)
            if len(norm_name) < 5:
                continue
            city_key = _norm_project_city(row.get("projectData.city"))
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
    return by_city, global_map


def _dotcom_matched_flag(building_name: str | None) -> str:
    _load_project_dictionary()
    assert _PROJECT_CANONICAL is not None
    name = (building_name or "").strip()
    return "Yes" if name and name in _PROJECT_CANONICAL else "No"


def _location_matched_flag(building_name: str | None) -> str:
    _load_location_buildings()
    assert _LOCATION_CANONICAL is not None
    name = (building_name or "").strip()
    return "Yes" if name and name in _LOCATION_CANONICAL else "No"


def _project_lookup_maps(city: str | None) -> list[dict[str, str]]:
    by_city, global_map = _load_project_dictionary()
    city_key = _norm_project_city(city)
    maps: list[dict[str, str]] = []
    if city_key and city_key in by_city:
        maps.append(by_city[city_key])
    maps.append(global_map)
    return maps


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
                if norm_name not in city_bucket and norm_name not in _global:
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
    if out:
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
    """Return (canonical projectName, match_kind) where kind is exact|fuzzy."""
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


def _load_pincode_localities() -> dict[str, list[str]]:
    global _PINCODE_LOCALITIES
    if _PINCODE_LOCALITIES is not None:
        return _PINCODE_LOCALITIES
    by_pin: dict[str, list[str]] = {}
    if not PINCODE_LOCALITY_CSV.exists():
        _PINCODE_LOCALITIES = by_pin
        return by_pin
    with PINCODE_LOCALITY_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pin = (row.get("pincode") or "").strip()
            loc = (row.get("Location ") or row.get("Location") or "").strip()
            if not pin or not loc or len(loc) < 3:
                continue
            bucket = by_pin.setdefault(pin, [])
            if loc not in bucket:
                bucket.append(loc)
    _PINCODE_LOCALITIES = by_pin
    return by_pin


def _load_location_buildings() -> dict[str, str]:
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


def _match_blr_locality_gazetteer(text: str) -> str | None:
    """Fallback: match any known Bangalore locality appearing in address text."""
    gazetteer = _load_blr_locality_gazetteer()
    if not gazetteer:
        return None

    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    collapsed = norm_text.replace(" ", "")
    _load_blr_locality_gazetteer()
    sorted_locs = _BLR_LOCALITY_SORTED or []
    for norm_loc, canonical in sorted_locs:
        if len(norm_loc) < 5:
            continue
        if norm_loc in norm_text:
            return _title_locality(canonical)
        compact = norm_loc.replace(" ", "")
        if len(compact) >= 5 and compact in collapsed:
            return _title_locality(canonical)

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


def _match_pincode_locality(text: str, zip_code: str, seed: str | None = None) -> str | None:
    """Pick best locality for a pincode from Pincode To Locality Mapping.csv."""
    locs = _load_pincode_localities().get(zip_code.strip(), [])
    if not locs:
        return None

    norm_text = _norm_project_text(_ocr_fix_project_text(text))
    for loc in sorted(locs, key=len, reverse=True):
        norm_loc = _norm_project_text(loc)
        if len(norm_loc) >= 4 and norm_loc in norm_text:
            return _title_locality(loc)

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


def is_bangalore_row(row: dict) -> bool:
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


def _project_phrases(text: str, *, min_words: int = 2, max_words: int = 10) -> list[str]:
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


def bureau_preprocess(address: str, zip_code: str = "") -> str:
    """Normalise bureau-style free-text before bharataddress segmentation."""
    text = " ".join(address.split())
    for bad, good in OCR_FIXES.items():
        text = re.sub(re.escape(bad), good, text, flags=re.IGNORECASE)
    text = re.sub(r"\bNO(\d)", r"NO \1", text, flags=re.IGNORECASE)
    # Stop bharataddress expanding RD/ST inside 3RD / 5TH / 1ST.
    text = ORDINAL_RE.sub(lambda m: f"{m.group(1)}ORD{m.group(2).upper()}", text)
    text = ADDRESSEE_RE.sub("", text).strip()
    text = BUILDING_SPLIT_RE.sub(", ", text)
    text = LANDMARK_SPLIT_RE.sub(", ", text)
    text = TRAILING_GEO_RE.sub("", text).strip(" ,.-")
    text = PIN_IN_TEXT_RE.sub("", text).strip(" ,.-")
    zip_code = zip_code.strip()
    if zip_code and zip_code not in text:
        text = f"{text} {zip_code}"
    return text


def _deordinal(text: str | None) -> str | None:
    if not text:
        return text
    return ORDINAL_TOKEN_RE.sub(lambda m: f"{m.group(1)}{m.group(2).lower()}", text)


def _reject_building_name(name: str) -> bool:
    """Drop company suffixes, OCR fragments, and unit+suffix noise (e.g. 203 VILLA)."""
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
    """Second-pass building extraction after locality is known."""
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
    location_matched: str = "No",
    locality_fuzzy_score: float = 0.0,
) -> float:
    """Reliability-weighted score: pincode/state cross-check + building dictionary match."""
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
    if city and lookup_city and _places_match(city, lookup_city):
        score += 0.10
    elif city:
        score += 0.04

    bn = (building_name or "").strip()
    if bn:
        score += 0.22
        if dotcom_matched == "Yes" or location_matched == "Yes":
            score += 0.18
        elif locality and bn.lower() == locality.strip().lower():
            score += 0.04
        else:
            score += 0.06

    if locality:
        score += 0.08
    if building_number:
        score += 0.04
    if landmark:
        score += 0.04

    return round(max(0.0, min(score, 1.0)), 3)


def parse_address_row(address: str, zip_code: str, stats: Counter | None = None) -> dict:
    """Parse one bureau row with cleanup + regex backfill for sparse fields."""
    prepared = bureau_preprocess(address, zip_code)
    result = parse(prepared)
    lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None

    out = {
        "building_number": result.building_number,
        "building_name": result.building_name,
        "landmark": result.landmark,
        "locality": result.locality,
        "city": result.city,
        "district": result.district,
    }

    if not out["building_number"]:
        out["building_number"] = _regex_building_number(prepared) or _regex_building_number(address)
    if not out["landmark"]:
        out["landmark"] = _regex_landmark(prepared) or _regex_landmark(address)
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

    if not out["locality"]:
        out["locality"] = _regex_locality(prepared, out["city"]) or _regex_locality(address, out["city"])

    locality_fuzzy_score = 0.0
    if zip_code.isdigit() and len(zip_code) == 6:
        had_locality = bool(out["locality"])
        pin_loc = _match_pincode_locality(prepared, zip_code, seed=out.get("locality"))
        if not pin_loc:
            pin_loc = _match_pincode_locality(address, zip_code, seed=out.get("locality"))
        if pin_loc:
            if not had_locality and stats is not None:
                stats["pincode_locality_filled"] += 1
            elif had_locality and pin_loc.lower() != (out["locality"] or "").lower() and stats is not None:
                stats["pincode_locality_refined"] += 1
            out["locality"] = pin_loc
        else:
            blr_loc = _match_blr_locality_gazetteer(prepared) or _match_blr_locality_gazetteer(
                address
            )
            if blr_loc:
                if not had_locality and stats is not None:
                    stats["blr_locality_filled"] += 1
                elif had_locality and blr_loc.lower() != (out["locality"] or "").lower() and stats is not None:
                    stats["blr_locality_refined"] += 1
                out["locality"] = blr_loc
            else:
                fuzzy_loc, locality_fuzzy_score = _fuzzy_locality(
                    prepared, zip_code, seed=out.get("locality")
                )
                if fuzzy_loc:
                    if not had_locality and stats is not None:
                        stats["fuzzy_locality_filled"] += 1
                    elif had_locality and fuzzy_loc.lower() != (out["locality"] or "").lower() and stats is not None:
                        stats["fuzzy_locality_refined"] += 1
                    out["locality"] = fuzzy_loc
    elif not out["locality"]:
        blr_loc = _match_blr_locality_gazetteer(prepared) or _match_blr_locality_gazetteer(address)
        if blr_loc:
            if stats is not None:
                stats["blr_locality_filled"] += 1
            out["locality"] = blr_loc

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

    for key in ("building_number", "building_name", "landmark", "locality", "city", "district"):
        out[key] = _deordinal(out[key])
    return out


def process_csv(limit: int | None = None) -> dict:
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

            parsed = parse_address_row(address, zip_code, stats=stats)
            row_out = {
                "ADDRESS": address,
                "State code": state_code,
                "ZIP": zip_code,
                **{k: ("" if v is None else v) for k, v in parsed.items()},
            }
            row_out[DOTCOM_MATCH_COL] = _dotcom_matched_flag(row_out.get("building_name"))
            row_out[LOCATION_MATCH_COL] = _location_matched_flag(row_out.get("building_name"))

            lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
            row_out["confidence"] = _score_confidence(
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
                location_matched=row_out[LOCATION_MATCH_COL],
            )
            writer.writerow(row_out)

            stats["total"] += 1
            if row_out.get(DOTCOM_MATCH_COL) == "Yes":
                stats["dotcom_matched_yes"] += 1
            if row_out.get(LOCATION_MATCH_COL) == "Yes":
                stats["location_matched_yes"] += 1
            if not zip_code:
                stats["missing_zip"] += 1
            if not state_code:
                stats["missing_state_code"] += 1

            if lookup:
                stats["pincode_in_db"] += 1
            else:
                stats["pincode_not_in_db"] += 1

            expected_state = STATE_ABBREV.get(state_code)
            if expected_state and lookup and _states_match(expected_state, lookup.get("state")):
                stats["state_code_matches_pincode_lookup"] += 1
            elif expected_state and lookup:
                stats["state_code_mismatch_pincode_lookup"] += 1
                if len(mismatches) < 20:
                    mismatches.append(
                        f"line {i}: state_code={state_code} ({expected_state}) "
                        f"vs lookup={lookup.get('state')} ZIP={zip_code}"
                    )

            if zip_code:
                stats["zip_matches_parsed_pincode"] += 1

            if lookup and parsed.get("city") and _places_match(parsed.get("city"), lookup.get("city")):
                stats["parsed_city_matches_pincode_lookup"] += 1
                if phonetic.fuzzy_ratio(parsed.get("city"), lookup.get("city")) >= FUZZY_PLACE_CUTOFF:
                    stats["parsed_city_fuzzy_match"] += 1

            for col in PARSED_COLS:
                if row_out.get(col) not in (None, ""):
                    stats[f"filled_{col}"] += 1

            conf = float(row_out.get("confidence") or 0)
            if conf >= 0.8:
                stats["confidence_ge_0_8"] += 1
            elif conf >= 0.6:
                stats["confidence_ge_0_6"] += 1

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


def _write_report(summary: dict) -> None:
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
        f"  Locality filled via fuzzy gazetteer:  {s.get('fuzzy_locality_filled', 0)}",
        f"  Locality refined via fuzzy match:    {s.get('fuzzy_locality_refined', 0)}",
        f"  City matched with fuzzy_ratio:         {s.get('parsed_city_fuzzy_match', 0)}",
        "  address_similarity: not used (requires two addresses; dedup only)",
        "",
        "Project dictionary (dotcom.project.csv)",
        "-" * 40,
        f"  building_name filled from dictionary:  {s.get('project_dict_filled', 0)}",
        f"  building_name replaced by dictionary:  {s.get('project_dict_overrode', 0)}",
        f"  building_name fuzzy (unmatched only):  {s.get('project_dict_fuzzy_filled', 0)}",
        f"  dotcom_matched Yes (canonical):        {s.get('dotcom_matched_yes', 0)}",
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
        "",
        "Pincode locality mapping",
        "-" * 40,
        f"  Locality filled via pincode map:       {s.get('pincode_locality_filled', 0)}",
        f"  Locality refined via pincode map:      {s.get('pincode_locality_refined', 0)}",
        f"  Locality filled via BLR gazetteer:     {s.get('blr_locality_filled', 0)}",
        f"  Locality refined via BLR gazetteer:    {s.get('blr_locality_refined', 0)}",
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


def retry_unmatched_projects(limit: int | None = None) -> dict:
    """Re-run project matching (OCR exact + brand fuzzy) only on non-dotcom rows."""
    _load_project_dictionary()
    assert _PROJECT_CANONICAL is not None

    with INPUT_CSV.open(newline="", encoding="utf-8") as f:
        input_rows = list(csv.DictReader(f))
    if limit:
        input_rows = input_rows[:limit]

    if not OUTPUT_CSV.exists():
        raise SystemExit(f"Missing {OUTPUT_CSV}. Run Address.py first.")

    with OUTPUT_CSV.open(newline="", encoding="utf-8") as f:
        parsed_rows = list(csv.DictReader(f))
    if len(parsed_rows) != len(input_rows):
        parsed_rows = parsed_rows[: len(input_rows)]

    stats: Counter = Counter()
    stats["total"] = len(input_rows)
    t0 = time.perf_counter()

    for i, (src, parsed) in enumerate(zip(input_rows, parsed_rows)):
        address = (src.get("ADDRESS") or "").strip()
        zip_code = (src.get("ZIP") or "").strip()
        prepared = bureau_preprocess(address, zip_code)
        lookup = pincode.lookup(zip_code) if zip_code.isdigit() and len(zip_code) == 6 else None
        project_city = (parsed.get("city") or "").strip() or (
            lookup.get("city") if lookup else None
        )

        current_bn = (parsed.get("building_name") or "").strip()
        if current_bn in _PROJECT_CANONICAL:
            stats["already_dotcom"] += 1
            continue

        exact_hit = _match_project_exact(prepared, project_city) or _match_project_exact(
            address, project_city
        )
        if exact_hit:
            if not current_bn:
                stats["project_dict_filled"] += 1
            elif current_bn != exact_hit:
                stats["project_dict_overrode"] += 1
            parsed["building_name"] = exact_hit
            stats["ocr_exact_recovered"] += 1
            continue

        stats["sent_to_fuzzy"] += 1
        fuzzy_hit = _match_project_fuzzy(prepared, project_city) or _match_project_fuzzy(
            address, project_city
        )
        if fuzzy_hit:
            parsed["building_name"] = fuzzy_hit
            stats["project_dict_fuzzy_filled"] += 1
            if current_bn and current_bn != fuzzy_hit:
                stats["project_dict_overrode"] += 1

        if (i + 1) % 5000 == 0:
            print(f"... fuzzy pass {i + 1}/{len(input_rows)}", file=sys.stderr)

    for parsed in parsed_rows:
        parsed[DOTCOM_MATCH_COL] = _dotcom_matched_flag(parsed.get("building_name"))
        parsed[LOCATION_MATCH_COL] = _location_matched_flag(parsed.get("building_name"))

    out_fields = ["ADDRESS", "State code", "ZIP", *OUTPUT_COLS]  # ADDRESS = original bureau text
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(parsed_rows)

    elapsed = time.perf_counter() - t0
    dotcom_rows = stats["already_dotcom"] + stats.get("ocr_exact_recovered", 0) + stats.get(
        "project_dict_fuzzy_filled", 0
    )
    summary = {
        "rows": stats["total"],
        "elapsed_sec": round(elapsed, 2),
        "stats": dict(stats),
        "dotcom_rows_after_retry": dotcom_rows,
    }

    lines = [
        "Project dictionary retry (unmatched addresses only)",
        "=" * 40,
        f"Input addresses: {INPUT_CSV.name}",
        f"Updated output:  {OUTPUT_CSV.name}",
        f"Rows: {stats['total']}",
        f"Elapsed: {summary['elapsed_sec']}s",
        "",
        f"Already had dotcom name:     {stats.get('already_dotcom', 0)}",
        f"OCR exact recovered:         {stats.get('ocr_exact_recovered', 0)}",
        f"Sent to fuzzy pass:          {stats.get('sent_to_fuzzy', 0)}",
        f"Fuzzy recovered:             {stats.get('project_dict_fuzzy_filled', 0)}",
        "",
        f"Dotcom-matched rows (est.):  {dotcom_rows} ({100 * dotcom_rows / stats['total']:.1f}%)",
    ]
    retry_report = ROOT / "project_retry_report.txt"
    retry_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nReport: {retry_report}")
    return summary


if __name__ == "__main__":
    limit = None
    retry_only = "--project-retry-only" in sys.argv
    filter_blr = "--filter-blr" in sys.argv
    for arg in sys.argv[1:]:
        if arg.isdigit():
            limit = int(arg)
        elif arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])

    if filter_blr:
        kept = filter_bangalore_csv()
        print(f"Filtered {INPUT_CSV} to {kept} Bangalore rows (backup: COMBO_DEMOG_all_cities.csv)")
    elif retry_only:
        result = retry_unmatched_projects(limit=limit)
        print(f"Updated {OUTPUT_CSV} ({result['rows']} rows)")
    else:
        result = process_csv(limit=limit)
        print(f"Wrote {OUTPUT_CSV} ({result['rows']} rows)")
        print(f"Report: {REPORT_TXT}")
