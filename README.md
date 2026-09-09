# SquareAddress

**Address Parsing Engine** for messy credit-bureau and free-text Indian addresses.

Offline, rule-based extraction into structured fields using [bharataddress](https://github.com/Neelagiri65/bharataddress), optional project and OSM location dictionaries, and pincode-locality enrichment.

**Repository:** https://github.com/Abhay-BITS/SquareAddress

## Features

- Bureau-specific preprocessing (OCR fixes, S/O strip, comma insertion)
- Tiered `building_name` extraction (project dictionary → OSM master → heuristics → area fallbacks)
- `dotcom_matched` / `location_matched` Yes/No flags for exact dictionary hits
- Pincode + state cross-check with reliability-weighted `confidence` score
- Optional Groq LLM backfill for remaining empty `building_name` rows

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Optional local dictionaries (not included in repo)

Place these files in the project directory (or update paths in `Address.py`):

| File | Purpose |
|------|---------|
| Input CSV (e.g. `COMBO_DEMOG.csv`) | Bureau addresses with `ADDRESS`, `State code`, `ZIP` |
| `dotcom.project.csv` | Project name dictionary |
| `Pincode To Locality  Mapping.csv` | Pincode → locality lists |
| `india_location_master.csv` | OSM-derived buildings and localities (filter by city in code) |

## Usage

Point `INPUT_CSV` in `Address.py` at your file, then:

```bash
python3 Address.py                        # full parse → output CSV + cross_check_report.txt
python3 Address.py --project-retry-only   # dotcom fuzzy retry on unmatched rows only

# Optional LLM backfill (requires GROQ_API_KEY)
export GROQ_API_KEY="your-key"
pip install groq
python3 Address_llm_backfill.py --limit 20
```

## Output columns

`ADDRESS`, `State code`, `ZIP`, `building_number`, `building_name`, `dotcom_matched`, `location_matched`, `landmark`, `locality`, `city`, `district`, `confidence`

## Documentation

Full pipeline reference: [ADDRESS_PARSING_DOCS.md](ADDRESS_PARSING_DOCS.md)

## License

See [LICENSE](LICENSE).
