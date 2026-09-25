#!/usr/bin/env python3
"""Bureau address preprocessing: raw credit-bureau text -> PreparedAddress.

Each step is small, word-bounded and recorded, so a bad clean-up can be traced instead of
silently eating the address. Nothing deletes to end-of-string: the old rule that cut from
the first "INDIA"/state word onwards turned "INDIA INFOLINE LTD, 31/9 KRIMSON SQUARE, HOSUR
MAIN ROAD, BANGALORE" into an empty string.

    prepared = prepare("S/O RAJU FLAT 106 KOTE ARCADE APT, K CHANNASANDRA", "560067")
    prepared.text      -> matching text (uppercase, spaced, abbreviations expanded)
    prepared.compact   -> same without spaces, for glued-OCR matching
    prepared.parser_text -> text for bharataddress (ordinals protected)
    prepared.pincode / care_of / organisation / landmarks / dropped

Run `python preprocess.py [N]` to audit N random COMBO rows before/after.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Word-bounded expansions. Keys must be whole words; values are the canonical spelling.
ABBREVIATIONS: dict[str, str] = {
    "RD": "ROAD", "RDS": "ROAD", "MN": "MAIN", "CRS": "CROSS", "CRSS": "CROSS", "CR": "CROSS",
    "LYT": "LAYOUT", "LAYT": "LAYOUT", "NGR": "NAGAR", "EXTN": "EXTENSION", "EXT": "EXTENSION",
    "APT": "APARTMENT", "APTS": "APARTMENT", "APPT": "APARTMENT", "APPTS": "APARTMENT",
    "APRT": "APARTMENT", "APTMT": "APARTMENT", "APARTMENTS": "APARTMENT", "APPARTMENT": "APARTMENT",
    "APPARTMENTS": "APARTMENT", "BLDG": "BUILDING", "BLD": "BUILDING", "BLG": "BUILDING",
    "HSE": "HOUSE", "HS": "HOUSE", "FLR": "FLOOR", "FLRS": "FLOOR", "GRND": "GROUND",
    "OPP": "OPPOSITE", "NR": "NEAR", "BHND": "BEHIND", "PH": "PHASE", "BLK": "BLOCK",
    "SEC": "SECTOR", "STG": "STAGE", "SY": "SURVEY", "VLG": "VILLAGE", "PO": "POST",
    "HNO": "HOUSE NO", "DNO": "DOOR NO", "FLT": "FLAT", "SOC": "SOCIETY", "TWR": "TOWER",
    "RESI": "RESIDENCY", "RESD": "RESIDENCY", "COLNY": "COLONY", "GRDN": "GARDEN",
    "GRDNS": "GARDEN", "MRG": "MARG", "HWY": "HIGHWAY", "JN": "JUNCTION", "CIR": "CIRCLE",
}
# Tokens repeated by bureaus ("BANGALORE BANGALORE", "KARNATAKA KARNATAKA").
GEO_TOKENS: frozenset[str] = frozenset(
    {
        "BANGALORE", "BENGALURU", "KARNATAKA", "INDIA", "MUMBAI", "MAHARASHTRA", "PUNE",
        "HYDERABAD", "TELANGANA", "CHENNAI", "TAMILNADU", "DELHI", "GURGAON", "GURUGRAM",
        "NOIDA", "KOLKATA", "THANE", "URBAN", "RURAL", "NORTH", "SOUTH", "EAST", "WEST",
    }
)
CARE_OF_RE = re.compile(
    r"^\s*(?:S\s*/\s*O|C\s*/\s*O|D\s*/\s*O|W\s*/\s*O|SON\s+OF|DAUGHTER\s+OF|WIFE\s+OF|CARE\s+OF)"
    r"\b[\s.:-]*(?P<name>[A-Z][A-Z\s.]{2,60}?)"
    r"(?=\s*(?:,|FLAT|HOUSE|PLOT|DOOR|SHOP|APARTMENT|BUILDING|NO\b|H\s*NO|#|\d))",
    re.IGNORECASE,
)
ORGANISATION_RE = re.compile(
    r"^\s*(?P<org>[A-Z0-9&.\s-]{3,60}?\b(?:PVT|PRIVATE|LTD|LIMITED|LLP|INC|CORP|CORPORATION|"
    r"TECHNOLOGIES|TECHNOLOGY|SOFTWARE|SOLUTIONS|SERVICES|SYSTEMS|CONSULTING|NETWORK|NETWORKS)\b"
    r"(?:\s+(?:PVT|PRIVATE|LTD|LIMITED|LLP|INDIA)\b)*)\s*[,.]?\s*",
    re.IGNORECASE,
)
LANDMARK_RE = re.compile(
    r"\b(?:NEAR|NEXT\s+TO|OPPOSITE|BEHIND|BESIDE|IN\s+FRONT\s+OF|ADJACENT\s+TO|ABOVE|BELOW)\b"
    r"\s+(?P<landmark>(?:[A-Z0-9&.'-]+\s+){0,4}[A-Z0-9&.'-]+)",
    re.IGNORECASE,
)
PINCODE_RE = re.compile(r"\b([1-9]\d{5})\b")
PHONE_RE = re.compile(r"\b(?:\+?91[-\s]?)?[6-9]\d{9}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
LONG_ID_RE = re.compile(r"\b\d{8,}\b")
JUNK_RE = re.compile(r"[#*._-]{3,}")
ORDINAL_RE = re.compile(r"\b(\d+)\s*(ST|ND|RD|TH)\b", re.IGNORECASE)
UNIT_LETTER_DIGIT_RE = re.compile(r"\b([A-Z])\s+(\d{1,4}[A-Z]?)\b")
# "/" only splits when it is not inside a number like 66/3 or 12/1A.
SEPARATOR_RE = re.compile(r"[;|\n\r\t]+|(?<![A-Z0-9])/(?![A-Z0-9])|\s{2,}|,", re.IGNORECASE)


@dataclass
class PreparedAddress:
    """Cleaned address plus everything pulled out of it."""

    raw: str
    text: str = ""
    parser_text: str = ""
    pincode: str = ""
    care_of: str = ""
    organisation: str = ""
    landmarks: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def compact(self) -> str:
        return self.text.replace(" ", "")


def _record(prepared: PreparedAddress, step: str, removed: str) -> None:
    removed = " ".join(removed.split())
    if removed:
        prepared.dropped.append((step, removed))


def normalise_characters(text: str) -> str:
    """NFKC, drop control characters, unify dashes/quotes, delete ###### filler."""
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text if ch.isprintable() or ch in " \t\n")
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    return JUNK_RE.sub(" ", text)


def rejoin_split_words(text: str, vocabulary: frozenset[str] | None = None) -> str:
    """Rejoin OCR-split words: BANGALOR E -> BANGALORE, R OAD -> ROAD, WHIT EFIELD -> WHITEFIELD.

    A stray 1-2 letter fragment is attached forward first ("R OAD" is ROAD, not APARTMENTR),
    then backward, and only when the merged form is a known word, so real initials
    (K R PURAM, M G ROAD) survive.
    """
    if not vocabulary:
        return text
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        merged = (word + nxt).upper()
        after = words[i + 2] if i + 2 < len(words) else ""
        fragment_forward = len(word) <= 2 and nxt and merged in vocabulary
        # "APARTMENT R OAD": R belongs to OAD, so don't swallow it backwards.
        fragment_goes_forward = len(nxt) <= 2 and after and (nxt + after).upper() in vocabulary
        fragment_back = len(nxt) <= 2 and nxt and merged in vocabulary and not fragment_goes_forward
        if merged.isalpha() and (fragment_forward or fragment_back):
            out.append(merged)
            i += 2
            continue
        out.append(word)
        i += 1
    return " ".join(out)


def split_glued_run(token: str, vocabulary: frozenset[str]) -> list[str]:
    """Split a long glued run using known words, longest match first.

    PROVIDENTSUNWORTHVENKATAPURA -> PROVIDENT SUNWORTH VENKATAPURA; a run that cannot be
    segmented is returned unchanged rather than chopped arbitrarily.
    """
    upper = token.upper()
    if len(upper) < 14 or not upper.isalpha():
        return [token]
    parts: list[str] = []
    i = 0
    while i < len(upper):
        for size in range(min(18, len(upper) - i), 4, -1):
            if upper[i : i + size] in vocabulary:
                parts.append(upper[i : i + size])
                i += size
                break
        else:
            if parts:
                parts[-1] += upper[i]
            else:
                parts.append(upper[i])
            i += 1
    return parts if len(parts) > 1 else [token]


def expand_abbreviations(text: str) -> str:
    return " ".join(ABBREVIATIONS.get(word.upper(), word) for word in text.split())


def dedupe_geo_tokens(text: str) -> str:
    """Drop immediate repeats and repeated city/state words, keeping the first."""
    out: list[str] = []
    seen_geo: set[str] = set()
    for word in text.split():
        upper = word.upper()
        if out and upper == out[-1].upper():
            continue
        if upper in GEO_TOKENS:
            if upper in seen_geo:
                continue
            seen_geo.add(upper)
        out.append(word)
    return " ".join(out)


def prepare(
    address: str,
    zip_code: str = "",
    *,
    vocabulary: frozenset[str] | None = None,
) -> PreparedAddress:
    """Clean one bureau address; see module docstring for the fields produced."""
    prepared = PreparedAddress(raw=address or "")
    text = normalise_characters(address)

    for name, pattern in (("email", EMAIL_RE), ("phone", PHONE_RE)):
        for hit in pattern.findall(text):
            _record(prepared, name, hit if isinstance(hit, str) else hit[0])
        text = pattern.sub(" ", text)

    pins = PINCODE_RE.findall(text)
    prepared.pincode = (zip_code or "").strip() or (pins[0] if pins else "")
    for pin in pins:
        text = re.sub(rf"\b{pin}\b", " ", text)
    text = LONG_ID_RE.sub(" ", text)

    text = SEPARATOR_RE.sub(", ", text)
    text = re.sub(r"(?:\s*,\s*)+", ", ", text).strip(" ,.-")

    care_of = CARE_OF_RE.search(text)
    if care_of:
        prepared.care_of = " ".join(care_of.group("name").split())
        text = text[: care_of.start()] + text[care_of.end() :]
        _record(prepared, "care_of", care_of.group(0))
        text = text.strip(" ,.-")

    organisation = ORGANISATION_RE.match(text)
    if organisation:
        prepared.organisation = " ".join(organisation.group("org").split())
        text = text[organisation.end() :].strip(" ,.-")
        _record(prepared, "organisation", organisation.group("org"))

    for hit in LANDMARK_RE.finditer(text):
        landmark = " ".join(hit.group("landmark").split())
        if landmark and landmark not in prepared.landmarks:
            prepared.landmarks.append(landmark)

    text = text.upper()
    text = rejoin_split_words(text, vocabulary)
    if vocabulary:
        text = " ".join(
            part
            for token in text.split()
            for part in split_glued_run(token, vocabulary)
        )
    text = expand_abbreviations(text)
    text = UNIT_LETTER_DIGIT_RE.sub(r"\1\2", text)
    text = dedupe_geo_tokens(text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")

    prepared.text = text
    prepared.segments = [seg.strip(" ,.-") for seg in text.split(",") if seg.strip(" ,.-")]
    # bharataddress reads RD/ST inside 3RD/1ST as road tokens, so protect ordinals there only.
    parser_text = ORDINAL_RE.sub(lambda m: f"{m.group(1)}ORD{m.group(2).upper()}", text)
    if prepared.pincode:
        parser_text = f"{parser_text} {prepared.pincode}"
    prepared.parser_text = parser_text
    return prepared


def _audit(count: int) -> None:
    import csv
    import random
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import Address as A

    vocab = build_vocabulary()
    rows = list(csv.DictReader(A.INPUT_CSV.open(encoding="utf-8")))
    random.seed(0)
    for row in random.sample(rows, count):
        out = prepare(row["ADDRESS"], row["ZIP"], vocabulary=vocab)
        print("RAW :", row["ADDRESS"].strip())
        print("TEXT:", out.text)
        for label, value in (
            ("pin", out.pincode), ("care_of", out.care_of), ("org", out.organisation),
            ("landmarks", "; ".join(out.landmarks)),
        ):
            if value:
                print(f"  {label}: {value}")
        print()


def build_vocabulary() -> frozenset[str]:
    """Known words for de-splitting: dotcom names/subLocations, localities, common address words."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import Address as A

    words: set[str] = {
        "ROAD", "MAIN", "POST", "GATE", "HALL", "PARK", "LANE",
        "BANGALORE", "BENGALURU", "KARNATAKA", "INDIA", "APARTMENT", "LAYOUT", "NAGAR",
        "CROSS", "MAIN", "ROAD", "PHASE", "STAGE", "BLOCK", "FLOOR", "VILLAGE", "COLONY",
        "GARDEN", "TOWER", "RESIDENCY", "ENCLAVE", "HOUSE", "BUILDING", "SOCIETY", "EXTENSION",
    }
    A._load_triplet_index()
    for name_index in (A._TRIPLET_NAME_INDEX or {}).values():
        for norm_name in name_index:
            words.update(w for w in norm_name.split() if len(w) >= 5)
    for subs in (A._TRIPLET_SUBLOCS_SORTED or {}).values():
        for sub in subs:
            words.update(w for w in sub.split() if len(w) >= 5)
            words.add(sub.replace(" ", ""))
    for locs in A._load_pincode_localities().values():
        for loc in locs:
            norm = A._triplet_norm(loc)
            words.update(w for w in norm.split() if len(w) >= 5)
            words.add(norm.replace(" ", ""))
    return frozenset(w for w in words if w.isalpha() and len(w) >= 4)


if __name__ == "__main__":
    import sys

    _audit(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
