# SquareAddress

BLR bureau address parsing pipeline — offline extraction of messy credit-bureau addresses (Bangalore focus) into structured fields using [bharataddress](https://github.com/Neelagiri65/bharataddress), Square Yards project dictionary matching, and OSM location enrichment.

## Features

- Bangalore row filter (`560xxx` / `561xxx` / `562xxx` pincodes)
- Tiered `building_name` extraction (dotcom dictionary → OSM master → heuristics → area fallbacks)
- `dotcom_matched` / `location_matched` Yes/No flags for dictionary-backed names
- Pincode + state cross-check with reliability-weighted `confidence` score
- Optional Groq LLM backfill for remaining empty `building_name` rows

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Optional local dictionaries (not included — proprietary/large)

Place these files in this directory (or update paths in `Address.py`):

| File | Purpose |
|------|---------|
| `COMBO_DEMOG.csv` | Input bureau addresses |
| `dotcom.project.csv` | Square Yards project name dictionary |
| `Pincode To Locality  Mapping.csv` | Pincode → locality lists |
| `../india_location_db/data/india_location_master.csv` | Bangalore OSM buildings/localities |

## Usage

```bash
# Filter input to Bangalore only (creates backup COMBO_DEMOG_all_cities.csv once)
python3 Address.py --filter-blr

# Full parse → COMBO_DEMOG_parsed.csv + cross_check_report.txt
python3 Address.py

# Dotcom fuzzy retry on unmatched rows only
python3 Address.py --project-retry-only

# Optional LLM backfill (requires GROQ_API_KEY)
export GROQ_API_KEY="your-key"
pip install groq
python3 Address_llm_backfill.py --limit 20
```

## Output columns

`ADDRESS`, `State code`, `ZIP`, `building_number`, `building_name`, `dotcom_matched`, `location_matched`, `landmark`, `locality`, `city`, `district`, `confidence`

## Documentation

See [ADDRESS_PARSING_DOCS.md](ADDRESS_PARSING_DOCS.md) for the full BLR pipeline flow, fuzzy matching, confidence formula, and dictionary logic.

## License

Internal Square Yards tooling. Dictionary CSVs are not redistributed in this repository.
