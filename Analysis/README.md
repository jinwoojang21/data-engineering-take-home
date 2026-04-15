# Data Engineering Take-Home

## Quick start

```bash
pip install pandas pyarrow
python3 analysis.py
```

That runs the full analysis and prints all four answers to the console.

## What's in here

| File | What it is |
|------|-----------|
| `ANALYSIS.md` | Answers to all questions, data quality findings, database redesign with full DDL, assumptions, and open questions |
| `analysis.py` | Python script that reproduces every number in the analysis. Reads the parquet files, cleans the data, and outputs results |
| `daily_usage_chart.png` | Line chart of total usage (MB) per day across January 2026 |
| `redesigned_erd.png` | Proposed new database schema — color-coded by table type with a plain-English relationship summary |
| `ERD.png` | Original ERD (provided) |
| `data/` | Source parquet files (provided) |

## Requirements

- Python 3.10+
- `pandas` and `pyarrow` (`pip install pandas pyarrow`)

## Notes

- The script prints supporting work (duplicates found, SIM card breakdown, daily totals) followed by a summary block at the end with the four answers:

```
ANSWERS
==================================================
1. Highest usage sim_card_id: 1001 (165.0 MB)
2. 3G events after cleanup: 1
3. Duplicate events: 2
4. Total cost: $11.68
```

- Takes about 2 seconds to run. All output goes to stdout.
- If you want to poke around the raw data without running the script: `python3 -c "import pandas as pd; print(pd.read_parquet('data/usage_events.parquet').to_string())"`
