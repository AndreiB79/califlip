# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Single-address analysis (opens HTML report in browser)
python3 califlip.py "123 Main St, Riverside, CA 92501"
python3 califlip.py "https://www.zillow.com/homedetails/..."

# Bulk screener UI
streamlit run califlip_app.py

# API connectivity diagnostics
python3 diagnose.py

# Install core dependencies
pip3 install -r requirements.txt                        # requests only
pip3 install streamlit pandas                           # needed for califlip_app.py
```

For Zillow data in bulk mode, a `.env` file is needed:
```
RAPIDAPI_KEY=your_key_here
RAPIDAPI_HOST=zillow-property-data-api1.p.rapidapi.com
```

Reports are saved to `~/califlip_reports/`.

## Architecture

Three-layer structure where each layer imports the one below:

```
califlip_app.py   (Streamlit UI — bulk screener)
    └── califlip_api.py   (bulk listing fetch + two-stage scoring)
            └── califlip.py   (core: address→APN→data→HTML report)
```

**`califlip.py`** is the only file with no internal imports. It does the full single-address pipeline:
1. Extract address from text or URL (Zillow/Redfin/Realtor.com)
2. Parse address into structured components (`parse_us_address`)
3. Direct APN lookup in Riverside County Assessor via `TABLE_GENERAL` (primary path)
4. Fallback: geocode via Nominatim → US Census Geocoder → geometry query in `LAYER_PARCELS`
5. Fetch property characteristics, tax history, ownership records from ArcGIS layers
6. Find neighbor parcels → assessed values → comps
7. FEMA flood zone check
8. Compute `tech_score` (0–20 pts) in Python
9. Render self-contained HTML with embedded JS that calculates the financial score (0–70 pts) live in the browser after the user types listing price

**Scoring split**: Python computes the tech score (year built, sqft, bed/bath config). The financial score (70% rule, discount-to-median, flood risk) is computed entirely in JS in the browser because listing price isn't available from county data — the user enters it manually.

**`califlip_api.py`** implements two-stage bulk screening:
- Stage 1 (Quick): Zillow data only → quick score 0-100 for all 200-300 listings
- Stage 2 (Deep): county data + full Flip Score for top-N candidates only

**Key ArcGIS layers** (all under `ASSESSOR_BASE`):
- Layer 40: parcel geometries (APN lookup by coordinates)
- Table 70: general data (owner, address — `TABLE_GENERAL`)
- Table 80: property characteristics (year, sqft, beds, baths — `TABLE_PROPERTY_CHAR`)
- Table 90: recorded book / transfer history (`TABLE_RECORDED_BOOK`)
- Table 100: tax year history / assessed values (`TABLE_TAXYEAR`)

## Critical constraints

**STREET_NUMBER is not indexed** in the Assessor API — putting it in a WHERE clause returns an error. The workaround in `find_parcel_by_address`: query by `STREET_NAME + CITY`, get up to 500 results, filter by number in Python.

**Zillow scraping is blocked** by PerimeterX. `fetch_zillow_listing` immediately returns `None`. The legacy scraping code is left as scaffold for a future RapidAPI upgrade.

**Prop 13 ARV multipliers** (`ARV_MULT_PESSIMISTIC/REALISTIC/OPTIMISTIC` = 1.2/1.5/1.8): California assessed values lag market prices because Prop 13 freezes reassessment. These multipliers are applied to assessed comps to estimate market ARV. If Zillow Zestimate is available, it overrides these.

**Scope is Riverside County only**. The Assessor ArcGIS endpoint is county-specific. Out-of-county addresses will fail at the parcel-lookup step. `califlip_api.py` has partial support for other counties via Zillow-only mode.
