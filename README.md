# SquareAddress

Address parsing and catalogue matching for messy Indian addresses, the kind that come out of credit bureau dumps, CRM exports and scanned forms.

The engine does two things. It splits free text into structured fields, and it decides whether an address belongs to a known project in a catalogue you supply, without accepting a match on the project name alone.

Everything runs offline. No data is shipped with this repository.

**Repository:** https://github.com/Abhay-BITS/SquareAddress

---

## The problem this solves

A real bureau record looks like this:

```
NO7 THANGAM 2ND CROSS ADITYA APARTMENT R OAD  BEHIND DR DO PHASE 2
QUARTERS  MAHADEV APURA VILLAGE  B ANGALORE NORTH
```

The building, the street, the landmark, the locality and the city are all mixed together, a word is split in half, and the city appears twice. Matching a catalogue name against text like this is easy to get wrong in two directions at once: missing real matches because the text is damaged, and inventing matches because project names repeat across a city.

The engine treats a catalogue row as one identity made of three parts, **project name plus city plus locality**, and confirms all three before saying yes.

---

## What you get per address

| Column | Meaning |
| --- | --- |
| `building_number` | Flat, house or plot number |
| `building_name` | Building or project. Overwritten with the catalogue name on a confirmed match |
| `dotcom_matched` | `Yes` or `No` |
| `dotcom_match_rule` | Which of four tests confirmed it |
| `location_matched` | Whether the building also appears in an OSM derived building list |
| `landmark` | The near, opposite or behind phrase |
| `locality` | Area. Overwritten with the catalogue locality on a confirmed match |
| `locality_source` | Which source produced the locality |
| `city`, `district` | Resolved city and district |
| `confidence` | 0 to 1, weighted by how much the evidence agrees |

The original address, state code and pincode are carried through unchanged, so any row can be traced back.

---

## How matching works

### Step 1, clean the text

`preprocess.py` turns raw text into a `PreparedAddress` through small, ordered, word bounded steps. Every step is recorded, so a bad clean up can be traced instead of silently eating the address.

- Unicode normalisation, control characters removed, filler such as `######` dropped
- Phone numbers, emails and long digit runs removed
- Pincode, care of name, company name and landmarks pulled out as fields
- OCR damage repaired with a vocabulary: `BANGALOR E` becomes `BANGALORE`, `R OAD` becomes `ROAD`, and a glued run such as `PROVIDENTSUNWORTHVENKATAPURA` is split back into words. A fragment is only joined when the result is a known word, so real initials such as `K R PURAM` survive
- Abbreviations expanded on whole words only: `RD` to `ROAD`, `APT` to `APARTMENT`, and about fifty more
- Repeated city and state words collapsed

Nothing is ever cut from the middle of the address to the end. An earlier version cut from the first occurrence of a state or country word, which turned `INDIA INFOLINE LTD, 31/9 KRIMSON SQUARE, HOSUR MAIN ROAD` into an empty string.

### Step 2, first pass parse

[bharataddress](https://github.com/Neelagiri65/bharataddress) produces the first draft of house number, building, locality, city and district. Ordinals are protected only in the string handed to the parser, never in the text used for matching.

### Step 3, resolve the city

Parsers often return a taluk or sub district name instead of the city. The city is resolved from the pincode against your catalogue's own city list, because every later step searches within that city only. This alone prevents a project of the same name in another city from matching.

### Step 4, find the locality

Six sources are tried in order of reliability, and the field is left blank rather than guessed:

| `locality_source` | What it means |
| --- | --- |
| `dotcom_match` | Taken from the matched catalogue row |
| `dotcom_subloc` | A catalogue locality name found in the address text |
| `pincode_map` | A locality registered for that pincode, found in the address |
| `gazetteer` | A known area name for that city, found in the address |
| `pincode_fuzzy` | Close spelling match, compared only against that pincode's own localities |
| `parser` | The trailing area phrase from the parser, kept only when it ends like an area name |
| `ner` | Suggested by the optional model, then validated against the lists above |

Matching is whole word, with OCR spacing allowed. This matters more than it sounds: a substring match once assigned the locality `Aluru` to thousands of rows because those letters sit inside `BENGALURU`.

### Step 5, match the catalogue

Project names are searched **within the resolved city only**, through five paths:

| Path | Handles |
| --- | --- |
| exact | The name as written |
| collapsed | OCR spacing damage inside the name |
| glued | Text with no spaces at all |
| partial | The builder name omitted, for example `SHILPITHA SUNFLOWER` for `Maithri Shilpitha Sunflower`. A fragment is indexed only when it points at exactly one project and is not itself a locality name |
| fuzzy | Spelling damage, with a stricter threshold required when the name must stand without a written locality |

A name found this way is not yet a match. The locality has to be supported too, by one of four rules, and the rule is recorded per row:

| `dotcom_match_rule` | Evidence |
| --- | --- |
| `project_locality_pincode` | Name, catalogue locality and a pincode covering that locality |
| `project_locality` | Name and catalogue locality, pincode points elsewhere |
| `project_pincode` | Name plus a pincode covering the catalogue locality, locality not written |
| `project_unique_nearby` | Name unique in the city, address pincode within a calibrated radius of the locality |

Three guards apply on top:

- **Common names cannot pass on a pincode alone.** Names built only from ordinary words, such as `BDA LAYOUT` or `SAI NIVAS`, require the locality in the text. A pincode covers thousands of buildings, so without this rule every namesake matches.
- **A landmark is not a home.** A project mentioned only after near, opposite, behind, beside or facing is a direction marker. The check anchors on the first word of the name, so it works for damaged text too.
- **Phase numbers must agree.** `PHASE 1` never matches `Phase II`. Roman and digit forms are treated as equal.

On a confirmed match the building name, city and locality are replaced with the catalogue values, so downstream joins use one vocabulary.

### Step 6, optional model stage

With `--ner`, [shiprocket-ai/open-tinybert-indian-address-ner](https://huggingface.co/shiprocket-ai/open-tinybert-indian-address-ner) (Apache 2.0, 66M parameters) reads rows where a field is still empty and proposes a building name or locality. Suggestions pass the same validation as everything else, and a locality is accepted only if it already exists in the locality dictionaries. **The model cannot create a catalogue match**, by design, so it cannot introduce false positives into `dotcom_matched`.

On a laptop CPU it runs at roughly 40 rows per second. Measured on a 2,000 row sample it filled a building name for 14% and a locality for 5.5% of rows the rules left empty, and changed no match decision either way.

### Step 7, confidence

Points are added for a valid pincode, agreement between state code and pincode, a confirmed building name weighted by which rule matched, a locality weighted by its source, and the presence of a house number or landmark. A contradiction between state code and pincode subtracts. The total is capped at 1.

Confidence measures **agreement of evidence, not probability of correctness**. For filtering, `dotcom_match_rule` and `locality_source` are more informative, because they state the evidence instead of compressing it.

---

## Calibrating the distance rule

The `project_unique_nearby` radius is derived from data, not chosen by feel, and you should re calibrate it for your own catalogue.

Take matches confirmed **without** using distance, meaning the catalogue locality is written in the address so the project is not in doubt. Measure the distance from the address pincode to the nearest pincode of that locality. That distribution describes what "same project" looks like in your data.

In one bureau dataset the distribution was: P50 0.0 km, P80 1.5 km, P85 3.2 km, P90 4.7 km, P95 23.2 km. The P90 value became the limit. The jump at P95 is pincode centroid noise, since a pincode is represented by a single post office point rather than by the shape of the area, so widening the radius buys very little genuine coverage while admitting many unconfirmed rows.

Set the result in `UNIQUE_NEARBY_MAX_KM` in `Address.py`.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# only for the optional model stage
pip install torch transformers
```

Python 3.11 or newer.

---

## Reference data you supply

No data ships with this repository. Place these files beside `Address.py`. The engine degrades gracefully: whatever is missing is simply skipped.

| File | Required columns | Used for |
| --- | --- | --- |
| `dotcom.project.csv` | `projectData.projectName`, `projectData.city`, `projectData.subLocation` | The catalogue. Without it, matching is disabled and the engine still parses fields |
| `Pincode.xlsx` | `CityName`, `LocationName`, `SubLocationName`, `PinCode` | Your own pincode to locality table. The best single input if you have it |
| `Pincode To Locality  Mapping.csv` | `pincode`, `Location`, `district`, `statename` | A post office list, for example the India Post directory |
| `pincode_locality_merged.csv` | built by a script, see below | The merged locality reference actually read at run time |
| `dotcom.project_pincode_master.csv` | built by a script, see below | A pincode per catalogue row, since catalogues rarely carry one |
| `../india_location_db/data/india_location_master.csv` | OSM derived building and locality names | Optional secondary building check |

Build the two derived files:

```bash
python build_pincode_locality_merged.py      # merges your sheet with the post office list
python build_dotcom_pincode_master.py        # gives every catalogue row a pincode
```

`build_dotcom_pincode_master.py` resolves a pincode per city and locality pair in this order: an exact post office name in the city's own district, then a learned mapping from your own address corpus, then a close post office name, then an OpenStreetMap Nominatim lookup, and finally the city centre as a labelled placeholder. Every row records which method was used and a reliability label, and only high and medium rows are trusted during matching. The Nominatim step respects one request per second and caches results, so a rerun is cheap. Review its output before relying on it.

---

## Running

```bash
python Address.py                 # full run, rules only
python Address.py --ner           # full run including the model stage
python Address.py 500             # 500 row test, writes its own file, never touches the real output
python Address.py --input=sheet.xlsx --output=parsed.csv   # Excel input
python Address.py --filter-blr    # keep only Bangalore pincodes in the input file
python test_pipeline.py           # regression tests, about 13 seconds
```

Output is written next to the input, together with a run report giving match counts by rule, locality sources, fill rates and the confidence spread.

**Run one job at a time.** Two full runs writing the same output file will corrupt it. A limited run writes to its own file precisely so a quick test cannot overwrite a real one.

---

## Repository layout

| File | Role |
| --- | --- |
| `Address.py` | The pipeline: dictionaries, matching rules, scoring, CLI |
| `preprocess.py` | Text cleaning, exposed as `prepare()` returning a `PreparedAddress` |
| `ner_stage.py` | The optional model stage, isolated so it can be removed |
| `test_pipeline.py` | 27 regression checks, one per bug found in development |
| `build_pincode_locality_merged.py` | Builds the merged pincode and locality reference |
| `build_dotcom_pincode_master.py` | Builds a pincode for every catalogue row |
| `make_review_sample.py` | Draws a stratified sample for manual labelling and scores it |
| `process_mumbai_imp.py` | A separate flow for files carrying two addresses per record |

---

## Measuring accuracy

Coverage is not accuracy. The engine reports how many rows it matched, not how many it matched correctly, and no automatic check can tell you the difference.

```bash
python make_review_sample.py          # writes 50 rows per match rule for labelling
# fill the verdict column with correct / wrong / unsure
python make_review_sample.py --score  # precision per rule, and overall
```

Do this before trusting the weaker two rules, `project_pincode` and `project_unique_nearby`, since both accept a match without the locality being written in the address.

---

## Tuning

Constants at the top of `Address.py`, all documented in place:

| Constant | Controls |
| --- | --- |
| `UNIQUE_NEARBY_MAX_KM` | Radius for the unique name rule. Calibrate as described above |
| `FUZZY_TRIPLET_PROJECT_CUTOFF` | Fuzzy threshold for project names |
| `FUZZY_TRIPLET_STRONG_CUTOFF` | Higher threshold a fuzzy name must clear to stand without a written locality |
| `PARTIAL_NAME_MIN_LEN` | Minimum length of a builder less name fragment |
| `GLUED_PROJECT_MIN_LEN` | Minimum length for matching a name inside unspaced text |
| `DOTCOM_RULE_WEIGHT`, `LOCALITY_SOURCE_WEIGHT` | Confidence weights per rule and per locality source |

Run `test_pipeline.py` after changing any of these. It catches the failure modes that are easy to reintroduce: localities matching inside city names, addresses truncated by cleaning, landmarks treated as homes, wrong phases, and cross city leakage.

---

## Known limitations

- **Coverage is bounded by the catalogue.** Addresses that name no listed project cannot match. In bureau data this is usually the majority, because independent houses and small layouts are not in project catalogues.
- **Pincodes are points, not shapes.** Distance is an approximation. Real project coordinates, if you have them, are strictly better.
- **Neighbouring localities are hard.** Catalogues use micro market names while people write village or road names, so a match can be right while the two locality names differ.
- **Commercial listings match too.** Office addresses at a listed tech park will be matched. Filter them if your dataset is meant to be residential.
- **Accuracy is unmeasured until you label a sample.** See above.

---

## Licence

MIT, see `LICENSE`. The bharataddress parser and the NER model carry their own licences.
