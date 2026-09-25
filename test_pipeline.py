#!/usr/bin/env python3
"""Regression tests: every bug found while building this pipeline, as a fast check.

    python test_pipeline.py

Each case is a real address from COMBO_DEMOG.csv (or a minimal variant) that once produced
a wrong answer. Run this before and after changing matching or preprocessing; it takes a few
seconds, where a full COMBO run takes twenty minutes.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import Address as A  # noqa: E402
import preprocess  # noqa: E402

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


def match(address: str, zip_code: str, **kw):
    rec, rule, reason = A._match_dotcom_triplet(
        address, zip_code, city=kw.get("city"), locality=kw.get("locality"),
        building_name=kw.get("building_name"),
    )
    return (rec.projectName if rec else None), (rec.subLocation if rec else None), (rule or reason)


def test_preprocessing() -> None:
    vocab = preprocess.build_vocabulary()

    def text(raw: str, pin: str = "560001") -> str:
        return preprocess.prepare(raw, pin, vocabulary=vocab).text

    # Cutting from the first INDIA/state word used to delete the whole address.
    check("org prefix kept",
          text("INDIA INFOLINE LTD  31/9 KRIMSON SQUARE 2ND FLOOR  HOSU R MAIN ROAD  BANGALORE", "560068"),
          "31/9 KRIMSON SQUARE 2ND FLOOR, HOSUR MAIN ROAD, BANGALORE")
    # A stray fragment belongs to the word after it, not before ("APARTMENTR OAD").
    check("fragment joins forward", text("ADITYA APARTMENT R OAD BEHIND"), "ADITYA APARTMENT ROAD BEHIND")
    check("initials survive", text("NO 5 K R PURAM M G ROAD"), "NO 5 K R PURAM M G ROAD")
    check("city repeat dropped", text("AMODA APARTMENT BANGALORE BANGALORE"), "AMODA APARTMENT BANGALORE")
    check("hash junk dropped", text("BISMILLA NAGAR###### BANGALORE"), "BISMILLA NAGAR, BANGALORE")
    check("house number kept whole", text("NO 66/3 BAGMANE TECH PARK"), "NO 66/3 BAGMANE TECH PARK")
    prepared = preprocess.prepare("S/O LATE SATHI RAJU FLAT NO 106 KOTE ARCADE APARTMENT", "560067", vocabulary=vocab)
    check("care_of captured", prepared.care_of, "LATE SATHI RAJU")
    check("care_of removed from text", "SATHI" in prepared.text, False)


def test_locality() -> None:
    # ALURU must not match inside BENGALURU (3,313 rows once said "Aluru").
    # "Aluru" was returned for 3,313 rows, matched inside the word BENGALURU.
    check("no locality inside city name",
          A._match_blr_locality_gazetteer("#4/4 8TH CROSS HOYSALA NAGAR SUNKADAKATTE BENGALURU") == "Aluru",
          False)
    check("bare city gives no locality", A._match_blr_locality_gazetteer("12 MAIN ROAD BENGALURU"), None)
    check("no locality inside OCR-split city", A._match_blr_locality_gazetteer("12 MAIN RD BENG ALURU"), None)
    check("OCR-split locality still matches",
          A._match_blr_locality_gazetteer("FLAT 2 WHIT EFIELD BANGALORE"), "Whitefield")
    check("no mid-word locality", A._locality_in_text("ALAHALLI", "LAKE VIEW ROAD AVALAHALLI"), False)
    check("post office suffix stripped", A._clean_locality_name("Bommanahalli S.O (Bengaluru)"), "Bommanahalli")
    check("city-only locality dropped", A._clean_locality_name("Bengaluru G."), None)
    check("street fragment is not a locality", A._precise_parser_locality("1st MAIN"), None)
    check("area tail kept", A._precise_parser_locality("NO 654 B BLOCK SUBASH NAGAR"), "Subash Nagar")


def test_building_name() -> None:
    check("street words cut from building",
          A._clean_building_name("THANGAM 2ND CROSS ADITYA APARTMENT"), "ADITYA APARTMENT")
    check("ordinal hack never leaks", "ORD" in (A.bureau_preprocess("3RD CROSS", "560001") or ""), True)
    check("ordinal hack absent from output", A._deordinal("3ORDRD CROSS"), "3rd CROSS")


def test_matching() -> None:
    # City scoping: the same project name in another city must not match.
    check("cross-city blocked",
          match("FLAT 1 PRESTIGE SHANTINIKETAN WHITEFIELD", "400001")[0], None)
    check("bangalore match", match("FLAT 1 PRESTIGE SHANTINIKETAN WHITEFIELD", "560066")[0],
          "Prestige Shantiniketan")
    # Landmarks are not residences.
    check("landmark not matched",
          match("H NO 189 3RD CROSS THANISANDRA RD NEAR ELEMENTS MALL", "560045")[0], None)
    check("real residence wins over landmark",
          match("E-120 SLS SPRINGS HARALUR VILLAGE BEHIND SOBHA DAFFODILS HSR LAYOUT", "560102")[0],
          "SLS Springs")
    # Builder name omitted by the bureau.
    check("partial name matched",
          match("A 1005 SPRING BLOCK SHILPITHA SUNFLOWER SOCIETY", "560066")[0],
          "Maithri Shilpitha Sunflower")
    # A locality fragment of a project name must never match on its own.
    check("locality fragment not a project",
          match("VD 802 PURVA VENEZIA YELAHANKA NEW TOWN BANGALORE", "560064")[0], "Purva Venezia")
    # Phases.
    check("phase respected", match("FLAT 101 CONFIDENT ATRIA PHASE IV SARJAPUR", "562125",
                                   city="Bangalore", locality="Sarjapur")[0], "Confident Atria Phase IV")
    check("wrong phase rejected", match("FLAT 10 NANDI GARDENS PHASE 1 J P NAGAR", "560062")[0], None)
    # Unique name confirmed by distance, and the far-away namesakes that must stay unmatched.
    # Unique-name matching is capped at P90 of genuine match distances (5km). 79 Orchids
    # sits at 6.2km from its catalogue subLocation, so it is deliberately left unmatched.
    check("unique name beyond P90 rejected",
          match("T301 79 ORCHIDS 5TH CROSS ROYAL ENCLAVE", "560064")[0], None)
    check("unique name within P90 matched",
          match("NO S101 FIRST FLOOR RENUKA RESIDENCY PANATHUR VILLAGE", "560087")[0],
          "Renuka Residency")
    check("far namesake rejected", match("NO 1C MARUTHI MANSION 1ST CROSS SARASWATHIPURAM", "560096")[0], None)
    check("generic name needs locality", match("FP#35 SAI NIVAS PATEL LAYOUT CHEEMASANDRA", "560049")[0], None)
    # Not in the catalogue at all.
    check("absent project stays unmatched",
          match("S/O LATE SATHI RAJU FLAT NO 106 KOTE ARCADE APARTMENT K CHANNASANDRA", "560067")[0], None)


def test_other_cities() -> None:
    for address, pin, project in (
        ("FLAT 1204 LODHA BELLEZZA MANJEERA ROAD KUKATPALLY HYDERABAD", "500072", "Lodha Bellezza Sky Villas"),
        ("A-703 RUNWAL GREENS MULUND WEST MUMBAI", "400078", "Runwal Greens"),
        ("TOWER 4 UNITECH THE CLOSE NORTH SECTOR 50 GURGAON", "122018", "Unitech The Close North"),
    ):
        check(f"city works: {pin}", match(address, pin)[0], project)
    check("pincode gives the city", A._resolve_dotcom_city_key(None, "400078", None), "mumbai")


def main() -> int:
    for test in (test_preprocessing, test_locality, test_building_name, test_matching, test_other_cities):
        test()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}")
        for failure in FAILURES:
            print("  -", failure)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
