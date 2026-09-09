# Bangalore Bureau Address Parsing

Pipeline for **Bangalore-only** rows from `COMBO_DEMOG.csv` (ZIP `560xxx` / `561xxx` / `562xxx`).

| Item | Path |
|------|------|
| Input | `COMBO_DEMOG.csv` (~42,248 BLR rows; full India backup in `COMBO_DEMOG_all_cities.csv`) |
| Output | `COMBO_DEMOG_parsed.csv` |
| Script | `Address.py` |
| Engine | [bharataddress](https://github.com/Neelagiri65/bharataddress) + Square Yards enrichment |
| Dotcom dictionary | `dotcom.project.csv` |
| OSM location dictionary | `../india_location_db/data/india_location_master.csv` |
| Pincode localities | `Pincode To Locality  Mapping.csv` |

---

## 1. Output columns

| Column | Description |
|--------|-------------|
| `ADDRESS` | Original bureau text (unchanged) |
| `State code` | Bureau state abbreviation |
| `ZIP` | Pincode |
| `building_number` | Flat / house / plot number |
| `building_name` | Project, society, layout, or named building |
| `dotcom_matched` | **Yes** if `building_name` is an exact canonical name from `dotcom.project.csv`; else **No** |
| `location_matched` | **Yes** if `building_name` is an exact canonical name from `india_location_master.csv` (Bangalore OSM); else **No** |
| `landmark` | Text after NEAR / BEHIND / OPP |
| `locality` | Sub-area name |
| `city` | City (usually Bangalore) |
| `district` | District |
| `confidence` | Reliability score (see §8) |

---

## 2. End-to-end flow

```
COMBO_DEMOG.csv row
        │
        ▼
┌───────────────────────────┐
│ 1. bureau_preprocess()    │  OCR fixes, S/O strip, pin append, comma insert
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│ 2. bharataddress.parse()  │  Rule parser + India Post pincode lookup
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│ 3. Regex backfill         │  building_number, landmark if still empty
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│ 4. Building name (tier 1) │  Priority order below
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│ 5. Locality enrichment    │  Pincode map → BLR gazetteer → fuzzy gazetteer
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│ 6. Building name (tier 2) │  Area / layout / society fallbacks (if still empty)
└───────────────────────────┘
        │
        ▼
┌───────────────────────────┐
│ 7. Match flags + score    │  dotcom_matched, location_matched, confidence
└───────────────────────────┘
        │
        ▼
COMBO_DEMOG_parsed.csv
```

Run:

```bash
cd Address_Extraction
python3 Address.py              # full BLR file
python3 Address.py --filter-blr   # re-filter input from backup (once)
python3 Address.py --project-retry-only   # dotcom fuzzy retry on unmatched rows
```

---

## 3. Preprocessing (`bureau_preprocess`)

Bureau strings are noisier than normal postal addresses. Before parsing we:

1. Apply **OCR spacing fixes** (`B ANGALORE` → `BANGALORE`, `R OAD` → `ROAD`, etc.)
2. Insert digit/letter boundaries (`3RD` → `3 RD` for ordinals)
3. Strip **S/O, C/O, W/O** parent-name prefixes
4. Append ZIP if missing from text
5. Insert commas between major segments

---

## 4. Building name — priority order

### Tier 1 (high trust)

| Step | Source | Logic |
|------|--------|-------|
| A | **dotcom.project.csv** | Exact n-gram phrase match (city-scoped, then global) |
| A2 | dotcom collapsed | Brand-filtered substring on space-removed text (OCR-glued names) |
| A3 | dotcom fuzzy | Brand-filtered `rapidfuzz` token_set_ratio ≥ 0.88 |
| B | **india_location_master.csv** | Exact phrase + collapsed substring + brand fuzzy (Bangalore, conf ≥ 0.5, apartment/building/complex types) |
| C | Parser cleanup | Sanitize bharataddress `building_name` if it passes validation |
| D | Compound suffix | Glued names: `JAYVILLA`, `KALPARUKSHA NEST` (before suffix-only regex) |
| E | Suffix regex | Words before APARTMENT, TOWER, VILLA, NILAYA, HEIGHTS, … |
| F | After-unit heuristic | Text after `FLAT NO 106`, `HNO 203`, etc. |
| G | Multi-word layout | e.g. `MANJUSHREE NILAYA VSR LAYOUT` |

### Tier 2 (area identity — fills gaps, lower semantic precision)

Only runs **after locality is known**, if `building_name` is still empty:

| Step | Logic |
|------|-------|
| Landmark phrase | Building suffix inside NEAR/BEHIND chunk |
| Single-word layout | `KAVERAPPA LAYOUT` (word ≥ 5 chars, not a blocked locality) |
| Society/colony regex | `LIC MODEL HOUSING COLONY`, `CMR GARDEN` |
| Parsed locality | Multi-word locality that appears verbatim in address |
| Named area gazetteer | Longest 2+ word BLR locality phrase in address text |
| Area token | Tokens ending PURAM / PURA / HALLI / LAYOUT / NAGAR / … |
| Enclave/estate | `… ENCLAVE`, `… ESTATE`, `… TOWNSHIP` |
| Before-floor | Name before `GROUND FLOOR` / `3RD FLOOR` (strict) |

### Rejection filters (`_reject_building_name`)

Applied on all cleaned names to avoid known bad extractions:

- Company suffixes: `PVT`, `LTD`, `LIMITED`, `GLOBALSOFT`, …
- OCR fragments: `KA`, `IND`, `RE`, `RU`, `ORE`, split `BANGALORE` tokens
- State abbreviations as words
- Suffix-only: lone `VILLA`, `APARTMENT`, …
- Numeric + suffix: `203 VILLA`
- Short layout prefix: `BD LAYOUT`
- All words ≤ 3 characters (`5 Ka Ind`)

---

## 5. dotcom dictionary (`dotcom.project.csv`)

~211k project names from Square Yards dotcom.

**Load:** norm phrase → canonical `projectData.projectName`, indexed by city and first-word brand.

**Exact match:** sliding n-grams (2–10 words) on OCR-fixed uppercase text against city bucket, then global bucket.

**Collapsed match:** For each brand token in address, test compact project names (length ≥ 10) as substrings of compact address. City-scoped when possible.

**Fuzzy match:** Only when brand token from address appears in dictionary (≥ 15 projects per brand). `rapidfuzz.token_set_ratio` ≥ 0.88 on 2–8 word windows. Main parse uses cutoff 0.88; empty-row backfill uses 0.90 with shared-word validation.

**`dotcom_matched`:** `Yes` only when final `building_name` equals a canonical dotcom string exactly (not fuzzy-normalized).

---

## 6. Location dictionary (`india_location_master.csv`)

Bangalore rows from OSM-derived master (~1,587 rows; ~600 building-type entities after filters).

**Entity types used:** `apartment`, `residential_complex`, `building`, `commercial_complex`, `tower`, `mall`

**Skipped:** roads, hospitals, temples, schools, offices, parks, …

**Columns used:** `name`, `society_name`, `building_name` (confidence ≥ 0.5)

**Match order:** exact n-gram → collapsed substring (compact ≥ 8) → brand-filtered fuzzy (same as dotcom)

**`location_matched`:** `Yes` only when final `building_name` equals a canonical OSM master name exactly.

---

## 7. Locality enrichment

| Step | Source | Method |
|------|--------|--------|
| 1 | bharataddress + regex | Nagar / Colony / Layout patterns |
| 2 | Pincode map CSV | Per-ZIP locality list; substring then fuzzy |
| 3 | BLR gazetteer | All 560–562 pincodes from map + OSM localities; substring / collapsed / single fuzzy |
| 4 | bharataddress localities.json | Fuzzy fallback per ZIP |

Fuzzy locality uses `phonetic.best_match` or `rapidfuzz` with cutoff **0.82**.

---

## 8. Confidence score (reliability-weighted)

Confidence is **not** just field count. It weights **pincode/state cross-check** and **dictionary-backed building names**.

| Signal | Points |
|--------|--------|
| Valid 6-digit ZIP | +0.18 |
| ZIP found in India Post DB | +0.07 |
| State code matches pincode lookup state | +0.15 |
| State code **mismatch** vs pincode | **−0.12** |
| Parsed city matches pincode city | +0.10 |
| City present, unverified | +0.04 |
| `building_name` present | +0.22 |
| `dotcom_matched = Yes` **or** `location_matched = Yes` | +0.18 |
| `building_name` present but heuristic only | +0.06 |
| `building_name` equals `locality` (duplicate) | +0.04 |
| Locality present | +0.08 |
| Building number present | +0.04 |
| Landmark present | +0.04 |

**Range:** 0.0 – 1.0 (clamped)

**Examples:**

| Row profile | Approx. score |
|-------------|---------------|
| ZIP + state OK + dotcom building + locality | **0.90** |
| ZIP + state OK + heuristic building + locality | **0.72** |
| ZIP + state mismatch + locality only | **0.41** |
| ZIP + no building, no locality | **0.25** |

High confidence now implies a **verified geography cross-check** and preferably a **dictionary building name**, not merely many filled columns.

---

## 9. Fuzzy matching summary

| Use case | Library | Scorer | Cutoff |
|----------|---------|--------|--------|
| City / state alias | phonetic | normalise + fuzzy_ratio | 0.85 |
| Locality vs gazetteer | phonetic / rapidfuzz | best_match / token_set_ratio | 0.82 |
| Dotcom project (brand-filtered) | rapidfuzz | token_set_ratio | 0.88 |
| Dotcom backfill (empty rows) | rapidfuzz | token_set_ratio | 0.90 + shared word |
| Location master building | rapidfuzz | token_set_ratio | 0.88 |

**Brand filtering:** Fuzzy runs only on candidate projects/buildings whose first word appears in the address. Prevents single-letter false positives (e.g. matching `M` as a project).

**Not used for building fill:** semantic embedding search (wrong tool for OCR typos on proper nouns).

---

## 10. Latest BLR results

From `cross_check_report.txt` (42,248 rows):

| Field | Filled | Rate |
|-------|--------|------|
| building_name | ~32,729 | ~77.5% |
| building_number | 33,055 | 78.2% |
| locality | 40,767 | 96.5% |
| city | 42,227 | ~100% |
| district | 42,188 | 99.9% |
| landmark | 7,553 | 17.9% |
| dotcom_matched Yes | 14,749 | 34.9% |
| location_matched Yes | 1,173 | 2.8% |
| confidence mean | 0.863 | 76% rows ≥ 0.8 |

**Quality notes:**

- ~7% of rows have `building_name == locality` (tier-2 area fallback)
- Tier-1 dictionary fills (~35% dotcom) are the most reliable building names
- ~9,000 rows remain empty — mostly pure street addresses (`NO 120 3RD CROSS …`) with no project name in text
- Optional: `Address_llm_backfill.py` (Groq) for remaining empty `building_name` rows

---

## 11. Example rows (from user review)

| Address snippet | Issue | Fix applied |
|-----------------|-------|-------------|
| `… GLOBALSOFT PVT LTD … HP AVENUE` | Was `PVT LTD` | Reject company suffix tokens |
| `NO 203 JAYVILLA …` | Was `203 VILLA` / `VILLA` | Compound `JAYVILLA` + reject suffix-only / numeric+suffix |
| `FLAT NO 5 … BANGALORE RE KA IND` | Was `5 Ka Ind` | Reject all-short-word and state/OCR fragments |
| `… BD LAYOUT … J P NAGAR` | Was `Bd Layout` | Reject layout prefix ≤ 3 chars |
| `22GIDDAMMALAYOUT AKASH NAGAR …` | Was `22 Bengaluru Ru` | Reject numeric-leading / OCR city fragments |

---

## 12. File reference

| File | Role |
|------|------|
| `Address.py` | Main pipeline |
| `Address_llm_backfill.py` | Optional Groq backfill for empty building_name |
| `cross_check_report.txt` | Per-run stats |
| `COMBO_DEMOG_parsed.csv` | Output |
| `dotcom.project.csv` | Square Yards project dictionary |
| `india_location_db/data/india_location_master.csv` | Bangalore OSM location dictionary |
| `Pincode To Locality  Mapping.csv` | Pincode → locality lists |

---

*Last updated: BLR pipeline, September 2026.*
