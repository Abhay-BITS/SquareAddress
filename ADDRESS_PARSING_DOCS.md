# SquareAddress — Address Parsing Engine

| Item | Description |
|------|-------------|
| **Script** | `Address.py` |
| **Engine** | [bharataddress](https://github.com/Neelagiri65/bharataddress) (offline, rule-based) |
| **Optional dictionaries** | `dotcom.project.csv`, `india_location_master.csv`, `Pincode To Locality  Mapping.csv` |
| **Example input** | Bureau CSV with `ADDRESS`, `State code`, `ZIP` (e.g. `COMBO_DEMOG.csv`) |
| **Output** | Parsed CSV (e.g. `COMBO_DEMOG_parsed.csv`) + `cross_check_report.txt` |

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
| `location_matched` | **Yes** if `building_name` is an exact canonical name from `india_location_master.csv`; else **No** |
| `landmark` | Text after NEAR / BEHIND / OPP |
| `locality` | Sub-area / neighbourhood name |
| `city` | City |
| `district` | District |
| `confidence` | Reliability score (see §8) |

---

## 2. End-to-end flow

Each address goes through **7 steps, in order**. Whatever comes out of step 7 is saved in the output file.

**Step 1 — Clean up the raw text**  
Fix common typos and spacing (e.g. broken city names), remove `S/O …` name lines, add the pincode if missing, and insert commas so the rest of the system can read the address more easily.

**Step 2 — Basic automatic parsing**  
A standard India address parser reads the cleaned text and pulls out basics like city, district, and pincode using postal rules.

**Step 3 — Fill gaps with simple patterns**  
If flat/house number or landmark is still missing, look for obvious patterns (e.g. `FLAT NO 106`, `NEAR …`).

**Step 4 — Find the building/project name (tier 1)**  
Match against the project list, then the OSM location master, then smart text rules. Take the best match found.

**Step 5 — Find the locality**  
Use the pincode locality map, a city gazetteer (pincode map + OSM localities), and fuzzy matching for slight misspellings.

**Step 6 — Building name (tier 2, if still empty)**  
Softer guesses from the address: layout names, society/colony names, or area-like words.

**Step 7 — Quality tags and confidence score**  
Set `dotcom_matched` / `location_matched` (Yes only on exact dictionary hits), then compute `confidence`.

```
Input CSV row
      │
      ▼
1. bureau_preprocess()
      ▼
2. bharataddress.parse()
      ▼
3. Regex backfill (building_number, landmark)
      ▼
4. Building name — tier 1
      ▼
5. Locality enrichment
      ▼
6. Building name — tier 2
      ▼
7. Match flags + confidence
      ▼
Output CSV
```

### Running the pipeline

Change the input file path in `Address.py`, then:

```bash
python3 Address.py                        # full file
python3 Address.py --project-retry-only   # dotcom fuzzy retry on unmatched rows
```

---

## 3. Preprocessing (`bureau_preprocess`)

Bureau strings are noisier than normal postal addresses. Before parsing:

**Fix broken spelling from OCR**  
Patch common scan errors, e.g. `B ANGALORE` → `BANGALORE`, `R OAD` → `ROAD`.

**Fix ordinal numbers**  
Split ordinals safely (`3RD` → `3 RD`) so "3rd cross" parses correctly.

**Remove parent or care-of lines**  
Strip lines starting with `S/O`, `C/O`, or `W/O` (person names, not address parts).

**Add pincode if missing from text**  
Append the 6-digit ZIP from the CSV when absent from the address body.

**Add commas between major parts**  
Break run-on bureau text into segments (near landmarks, flat numbers, etc.).

---

## 4. Building name — priority order

### Tier 1 (high trust)

| Step | Where it comes from | What we do |
|------|---------------------|------------|
| **A** | `dotcom.project.csv` | Exact project name in address. City-scoped first, then global. |
| **B** | `india_location_master.csv` | Known apartments, towers, societies. Exact → collapsed → brand fuzzy. |
| **C** | Basic parser output | Use parser `building_name` if it passes validation. |
| **D** | Pattern in address text | Words before APARTMENT, TOWER, VILLA, NILAYA, HEIGHTS, etc. |
| **E** | Pattern after flat/house number | Text after `FLAT NO 106`, `HNO 203`, etc. |

### Tier 2 (area identity — lower precision)

Only runs **after locality is known**, if `building_name` is still empty:

| Step | What we look for | Plain English |
|------|------------------|---------------|
| Landmark phrase | Text after NEAR or BEHIND | Use landmark chunk as stand-in name. |
| Building suffix in landmark | Apartment/Tower/Villa inside NEAR/BEHIND chunk | Pull name before building-type word. |
| Single-word layout | One word + LAYOUT (5+ chars, not blocked) | e.g. `KAVERAPPA LAYOUT`. |
| Society/colony regex | Colony / housing society patterns | e.g. `LIC MODEL HOUSING COLONY`, `CMR GARDEN`. |
| Parsed locality | Locality already found, verbatim in address | Reuse locality as building name. |
| Named area gazetteer | Long 2+ word locality phrase in address | Longest known phrase match. |
| Area token | Neighbourhood-style endings | Puram, Pura, Halli, Layout, Nagar, etc. |
| Enclave/estate | Estate-style names | Enclave, Estate, Township. |
| Before-floor | Text before floor mentions | Name before `GROUND FLOOR`, `3RD FLOOR` (strict). |

### Rejection filters (`_reject_building_name`)

Applied on all cleaned names:

- Company suffixes: PVT, LTD, LIMITED, GLOBALSOFT, etc.
- Suffix-only: lone VILLA, APARTMENT, etc.
- Numeric + suffix: `203 VILLA`
- Short layout prefix: `BD LAYOUT`
- All words ≤ 3 characters (`5 Ka Ind`)

---

## 5. dotcom dictionary (`dotcom.project.csv`)

~211k project names (Square Yards dotcom). Indexed by city and brand (first word).

**Match order:**

1. **Exact** — scan 2–10 word chunks against city projects, then all projects.
2. **Collapsed** — compact project names (10+ chars) inside compact address text; city first.
3. **Fuzzy** — only when a brand in the address exists in the dictionary (15+ projects per brand). `rapidfuzz.token_set_ratio` ≥ 0.88 on 2–8 word windows. Empty-row backfill uses 0.90 with shared-word validation.

**`dotcom_matched = Yes`** only when `building_name` is the **exact** official dotcom name. Fuzzy/collapsed can still fill the name; they do not get the Yes flag.

---

## 6. Location dictionary (`india_location_master.csv`)

OSM-derived master, filtered by city and entity type in code.

**Entity types used:** apartment, residential_complex, building, commercial_complex, tower, mall  
**Skipped:** roads, hospitals, temples, schools, offices, parks, etc.  
**Columns used:** `name`, `society_name`, `building_name` (confidence ≥ 0.5)

**Match order:** exact phrase → collapsed substring (compact ≥ 8 chars) → brand-filtered fuzzy (same idea as dotcom).

**`location_matched = Yes`** only when `building_name` exactly matches a canonical OSM master name.

---

## 7. Locality enrichment

Four steps, stopping when something good is found:

| Step | Source | Method |
|------|--------|--------|
| 1 | bharataddress + regex | Nagar / Colony / Layout patterns |
| 2 | Pincode map CSV | Per-ZIP locality list; substring then fuzzy |
| 3 | City gazetteer | Pincode map localities + OSM localities; substring / collapsed / fuzzy |
| 4 | bharataddress `localities.json` | Fuzzy fallback per ZIP |

Fuzzy locality uses `phonetic.best_match` or `rapidfuzz` with cutoff **0.82**.

---

## 8. Confidence score (reliability-weighted)

Confidence weights **pincode/state cross-check** and **dictionary-backed building names**, not just field count.

| Signal | Points |
|--------|--------|
| Valid 6-digit ZIP | +0.18 |
| ZIP found in India Post DB | +0.07 |
| State code matches pincode lookup state | +0.15 |
| State code mismatch vs pincode | −0.12 |
| Parsed city matches pincode city | +0.10 |
| City present, unverified | +0.04 |
| `building_name` present | +0.22 |
| `dotcom_matched = Yes` or `location_matched = Yes` | +0.18 |
| `building_name` heuristic only | +0.06 |
| `building_name` equals `locality` | +0.04 |
| Locality present | +0.08 |
| Building number present | +0.04 |
| Landmark present | +0.04 |

**Range:** 0.0 – 1.0 (clamped)

### Example benchmark (sample bureau run, ~42k rows)

| Stat | Value |
|------|------:|
| Median | 0.900 |
| Mean | 0.863 |
| Min / Max | 0.22 / 1.00 |
| P25 / P75 | 0.84 / 1.00 |
| P10 / P90 | 0.62 / 1.00 |

| Band | Rows | Share |
|------|-----:|------:|
| ≥ 0.8 | 32,147 | 76.1% |
| 0.6 – 0.8 | 7,800 | 18.5% |
| < 0.6 | 2,301 | 5.4% |

---

## 9. Example evaluation results

Sample run on a bureau dataset (~42,248 rows):

| Field | Filled | Rate |
|-------|-------:|-----:|
| building_name | ~32,729 | ~77.5% |
| building_number | 33,055 | 78.2% |
| locality | 40,767 | 96.5% |
| city | 42,227 | ~100% |
| district | 42,188 | 99.9% |
| landmark | 7,553 | 17.9% |
| dotcom_matched Yes | 14,749 | 34.9% |
| location_matched Yes | 1,173 | 2.8% |
| Heuristic / fallback fill | ~16,807 | ~40% |
| confidence mean | 0.863 | 76% rows ≥ 0.8 |

**Quality notes**

- Tier-1 dictionary fills (~35% dotcom) are the most reliable building names.
- ~9,000 rows remain empty — mostly pure street addresses with no project name in text.

### Dipstick analysis (500 random rows)

| Field fill | Sample | Population |
|------------|-------:|-----------:|
| building_number | 78.0% | 78.2% |
| building_name | 79.8% | 77.5% |
| locality | 96.4% | 96.5% |
| landmark | 19.2% | 17.9% |

| Confidence | Sample | Population |
|------------|-------:|-----------:|
| Median | 0.900 | 0.900 |
| Mean | 0.869 | 0.863 |
| ≥ 0.8 | 78.0% | 76.1% |

**Where `building_name` comes from (exclusive buckets)**

| Source | Share |
|--------|------:|
| Dotcom verified | 33.6% |
| Both dotcom + location | 1.8% |
| Location verified only | 2.2% |
| Heuristic / unverified | 42.2% |
| Empty | 20.2% |

**Quality signals (500-row sample)**

| Issue | Sample |
|-------|-------:|
| Filled but no dictionary flag | 42.2% |
| building_name = locality | 7.0% |
| confidence < 0.6 | 5.6% |

---

## 10. Example rows (edge cases)

| Address snippet | Issue | Fix applied |
|-----------------|-------|-------------|
| `… GLOBALSOFT PVT LTD … HP AVENUE` | Was `PVT LTD` | Reject company suffix tokens |
| `NO 203 JAYVILLA …` | Was `203 VILLA` / `VILLA` | Compound `JAYVILLA` + reject suffix-only / numeric+suffix |
| `FLAT NO 5 … BANGALORE RE KA IND` | Was `5 Ka Ind` | Reject all-short-word and state/OCR fragments |
| `… BD LAYOUT … J P NAGAR` | Was `Bd Layout` | Reject layout prefix ≤ 3 chars |
| `22GIDDAMMALAYOUT AKASH NAGAR …` | Was `22 Bengaluru Ru` | Reject numeric-leading / OCR city fragments |

---

## 11. Limitations

- `dotcom.project.csv` coverage is limited; many real societies are not listed.
- ~20% empty `building_name`: garbled or street-only addresses, or name trapped in `locality`.
- Low confidence (< 0.6): usually missing `building_name` and weak locality.
- Heuristic tier-2 fills boost fill rate but are not dictionary-verified (`dotcom_matched` / `location_matched` stay No).

---

## 12. File reference

| File | Role |
|------|------|
| `Address.py` | Main pipeline |
| `cross_check_report.txt` | Per-run stats (generated locally) |
| `requirements.txt` | Python dependencies |
| `dotcom.project.csv` | Project dictionary (local, not in repo) |
| `india_location_master.csv` | OSM location dictionary (local, not in repo) |
| `Pincode To Locality  Mapping.csv` | Pincode → locality lists (local, not in repo) |

---

*SquareAddress — September 2026.*
