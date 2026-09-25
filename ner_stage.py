#!/usr/bin/env python3
"""Optional NER stage: fill building_name / locality the rules could not find.

Model: shiprocket-ai/open-tinybert-indian-address-ner (Apache-2.0, 66M params, ~40 rows/s
on CPU). Measured on 2,000 COMBO rows it fills building_name for 14% and locality for 5.5%
of rows that rules left empty, and changes no dotcom decision either way — so it is a
field-completeness stage, not a matching stage. It never overwrites a rules-based value and
never relaxes the dotcom triplet gate.

Enable with `python Address.py --ner` (first run downloads ~270MB, then works offline).
"""

from __future__ import annotations

import re

MODEL_ID = "shiprocket-ai/open-tinybert-indian-address-ner"
MIN_SCORE = 0.60
_PIPELINE = None
_SUBWORD_RE = re.compile(r"##")
_ORG_RE = re.compile(r"\b(PVT|LTD|LIMITED|LLP|INC|CORP|TECHNOLOGIES|SOLUTIONS|SERVICES)\b", re.I)


def available() -> bool:
    try:
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def _pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        from transformers import pipeline

        # device=-1: the batched MPS path returns empty predictions on this machine.
        _PIPELINE = pipeline(
            "token-classification",
            model=MODEL_ID,
            aggregation_strategy="simple",
            device=-1,
        )
    return _PIPELINE


def _clean(span: str) -> str:
    """Model spans carry subword marks, leading unit numbers and company words."""
    text = _SUBWORD_RE.sub("", span or "")
    text = _ORG_RE.sub(" ", text)
    text = re.sub(r"^[\W\d]+", "", text).strip(" ,.-")
    text = " ".join(text.split())
    return text.title() if len(text) >= 4 else ""


def extract(address: str) -> dict[str, str]:
    """{'building_name': ..., 'locality': ...} from one raw address; missing keys omitted."""
    if not (address or "").strip():
        return {}
    spans: dict[str, list[str]] = {}
    for entity in _pipeline()(address):
        if float(entity["score"]) >= MIN_SCORE:
            spans.setdefault(entity["entity_group"], []).append(entity["word"])

    out: dict[str, str] = {}
    building = _clean(max(spans.get("building_name", [""]), key=len))
    locality = _clean(max(spans.get("locality", []) + spans.get("sub_locality", [""]), key=len))
    if building:
        out["building_name"] = building
    if locality:
        out["locality"] = locality
    return out
