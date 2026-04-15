# Usage Data Analysis

## Answers

**Which sim_card_id had the highest total usage?**

sim_card 1001 at 165.0 MB. I got here by joining usage_events through profile_installation (pid → asset_id) and matching each event to the SIM that was active at the time of the event. A few events couldn't be mapped — pid 999 doesn't exist in profile_installation (probably test data), and a couple of events fell outside their SIM's active window.

Full breakdown:

| sim_card_id | Total MB |
|-------------|----------|
| 1001        | 165.0    |
| 1000        | 100.0    |
| 1002        | 94.0     |
| 1004        | 89.0     |
| 1007        | 77.0     |
| 1005        | 57.0     |
| 1003        | 40.0     |
| 1008        | 30.0     |
| 1006        | 5.0      |
| unmapped    | 39.0     |

**How many usage events resolved to 3G after cleanup?**

1 event. After normalizing the tech field (LTE/lte → 4G, 5G/5g/NR → 5G, etc.), only HSPA+ maps to 3G. That's sid 30. Worth noting: that event also has a suspicious date of 2035-01-01 which is almost certainly a typo.

One edge case: CDMA could be 3G depending on whether it's IS-95 (2G) or CDMA2000 (3G). I classified it as 2G/3G ambiguous and didn't count it. If the team considers CDMA as 3G, the answer would be 2.

**How many duplicate usage events did you identify?**

2 duplicates. I used (pid, evt_dttm) as the natural key — same profile at the same timestamp should be the same event. Both duplicates are for pid=100 on 2026-01-16 08:00, loaded from different source files:

- Row 1: sid=2, 50.0 MB, from usage_1.parquet (kept)
- Row 18: sid=2, 50.0 MB, from usage_2.parquet (duplicate)
- Row 19: sid=2, 55.0 MB, from usage_3.parquet (duplicate, also has a different MB value)

Row 19 is interesting because the MB value differs (55 vs 50). I kept the first-loaded value since it matches the majority, but this kind of conflict is worth flagging — it suggests the source files may have different levels of data corrections applied.

**What is the cost of all data used in the linked data?**

$11.68 USD. Calculated by walking the full join chain: usage_events → profile_installation → sim_card_plan_history → rate_card, then multiplying mb × rt_amt for each event.

For rate matching, I used prio_nbr to pick the most specific rate — tech-specific rates (higher priority) win over generic fallback rates. A handful of events couldn't be costed due to missing mappings or invalid cc2 values.

---

## Data Quality Problems

Here's what I found and how I dealt with it.

**usage_events**

The tech field is a mess — same technology shows up as LTE, lte, 4G, 4g, etc. I normalized everything to a canonical generation (2G/3G/4G/5G) based on standard telecom mappings.

Two events from different source files turned out to be duplicates of the same event (same pid + timestamp). Deduplicated by keeping the first occurrence.

sid 26 has a negative MB value (-5.0). Could be a reversal or correction record, but without more context I treated it as zero. sid 30 has an event dated 2035-01-01 — almost certainly a typo for 2025 or 2026. sid 27 is missing evt_dttm entirely, so it can't be placed on a timeline. sid 28 is missing cc1, and sid 29 has cc2=99999 which doesn't match anything in the rate card.

sid 12 uses pid=999 which has no profile_installation record. Looks like test or dummy data.

**profile_installation**

pid 107 has end_dttm (Jan 9) before beg_dttm (Jan 10) — that's an invalid range. pid 103 has two identical-looking rows for asset 1005 with the same beg_dttm but different crt_dttm timestamps, which is a duplicate. pid 102 has overlapping installations where two SIMs (1003 and 1004) are both "active" from Jan 18-20.

**rate_card**

There's a negative rate (-0.01) for bundle 2000 which I excluded. Also a typo in curr_cd ("US D" with a space instead of "USD"). Bundle 9999 appears in rate_card but isn't referenced anywhere in sim_card_plan_history — orphaned/test data.

Bundle 2000 has three rows for the same (cc1=310, cc2=260, tech=4G) combination with rates of 0.01, -0.01, and 0.011. Not clear which is authoritative.

**sim_card_plan_history**

asset 1007 has an entry where x_dttm (Jan 14) is before eff_dttm (Jan 15) — another inverted date range. The why_cd values aren't normalized: "activation" vs "ACT", and there's a "FIX" code that's unclear.

**Cross-table issues**

The column names in the ERD don't match the actual data (e.g., ERD says "profile" and "sim" but the data has "pid" and "asset_id"). The ERD also types cc1 and cc2 as strings, but they're numeric in the data. There's no sim_cards dimension table — asset_ids only exist implicitly through references in other tables.

---

## Database Redesign

See `redesigned_erd.html` for the visual diagram. Here's the thinking behind it.

The main problems with the current schema are: no referential integrity (nothing stops an event from referencing a nonexistent pid), no constraints to catch bad data at write time (negative MB, inverted dates, etc.), inconsistent column naming, and missing dimension tables (there's no actual sim_cards table).

The redesign adds:

- Explicit dimension tables for `sim_cards`, `profiles`, and `bundles` — right now these only exist as IDs scattered across other tables with no metadata.
- A `network_technologies` lookup table that maps aliases (LTE, lte, 4G, 4g) to canonical codes (4G). This kills the normalization problem at the source.
- Foreign keys everywhere. usage_events.profile_id references profiles, profile_installations.sim_card_id references sim_cards, etc.
- CHECK constraints: `usage_mb >= 0` prevents negative values, `effective_to > effective_from` prevents inverted date ranges, `rate_per_mb > 0` blocks negative rates.
- A UNIQUE constraint on `(profile_id, event_timestamp, country_code, network_code)` in usage_events so duplicates get rejected at the database level.
- A staging table (`usage_events_staging`) where raw data lands first, gets validated, and only then moves to production. It preserves the original tech value alongside the normalized one for audit purposes.

Full DDL:

```sql
CREATE TABLE sim_cards (
    sim_card_id     BIGINT PRIMARY KEY,
    imsi            VARCHAR(20),
    iccid           VARCHAR(22),
    status          VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'deactivated')),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE profiles (
    profile_id      BIGINT PRIMARY KEY,
    profile_type    VARCHAR(50),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bundles (
    bundle_id       BIGINT PRIMARY KEY,
    bundle_name     VARCHAR(100),
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE network_technologies (
    tech_code       VARCHAR(10) PRIMARY KEY,       -- '2G','3G','4G','5G'
    tech_name       VARCHAR(50) NOT NULL,
    aliases         TEXT                            -- JSON: ["LTE","lte","4G","4g"]
);

CREATE TABLE profile_installations (
    installation_id BIGSERIAL PRIMARY KEY,
    profile_id      BIGINT NOT NULL REFERENCES profiles(profile_id),
    sim_card_id     BIGINT NOT NULL REFERENCES sim_cards(sim_card_id),
    effective_from  TIMESTAMP NOT NULL,
    effective_to    TIMESTAMP,
    source          VARCHAR(20) NOT NULL
        CHECK (source IN ('portal', 'api', 'system', 'migration')),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_date_range CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT uq_profile_sim_eff UNIQUE (profile_id, sim_card_id, effective_from)
);
CREATE INDEX idx_pi_profile_dates ON profile_installations(profile_id, effective_from, effective_to);
CREATE INDEX idx_pi_sim ON profile_installations(sim_card_id);

CREATE TABLE sim_card_plans (
    plan_id         BIGSERIAL PRIMARY KEY,
    sim_card_id     BIGINT NOT NULL REFERENCES sim_cards(sim_card_id),
    bundle_id       BIGINT NOT NULL REFERENCES bundles(bundle_id),
    effective_from  TIMESTAMP NOT NULL,
    effective_to    TIMESTAMP,
    change_reason   VARCHAR(30) NOT NULL
        CHECK (change_reason IN ('activation','upgrade','downgrade','swap','cancellation','profile_move','correction')),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_plan_dates CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE INDEX idx_scp_sim_dates ON sim_card_plans(sim_card_id, effective_from, effective_to);
CREATE INDEX idx_scp_bundle ON sim_card_plans(bundle_id);

CREATE TABLE rate_cards (
    rate_id         BIGSERIAL PRIMARY KEY,
    bundle_id       BIGINT NOT NULL REFERENCES bundles(bundle_id),
    country_code    VARCHAR(5) NOT NULL,
    network_code    VARCHAR(5) NOT NULL,
    tech_code       VARCHAR(10) REFERENCES network_technologies(tech_code),
    effective_from  DATE NOT NULL,
    effective_to    DATE,
    rate_per_mb     NUMERIC(10,6) NOT NULL,
    currency        CHAR(3) NOT NULL DEFAULT 'USD'
        CHECK (currency ~ '^[A-Z]{3}$'),
    priority        INTEGER NOT NULL DEFAULT 10,
    CONSTRAINT chk_positive_rate CHECK (rate_per_mb > 0),
    CONSTRAINT chk_rate_dates CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE INDEX idx_rc_lookup ON rate_cards(bundle_id, country_code, network_code, effective_from);

CREATE TABLE usage_events (
    event_id        BIGSERIAL PRIMARY KEY,
    profile_id      BIGINT NOT NULL REFERENCES profiles(profile_id),
    event_timestamp TIMESTAMP NOT NULL,
    usage_mb        NUMERIC(12,4) NOT NULL CHECK (usage_mb >= 0),
    country_code    VARCHAR(5) NOT NULL,
    network_code    VARCHAR(5) NOT NULL,
    tech_code       VARCHAR(10) NOT NULL REFERENCES network_technologies(tech_code),
    apn             VARCHAR(100),
    source_file     VARCHAR(200),
    loaded_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_event UNIQUE (profile_id, event_timestamp, country_code, network_code)
);
CREATE INDEX idx_ue_timestamp ON usage_events(event_timestamp);
CREATE INDEX idx_ue_daily ON usage_events(DATE(event_timestamp));

CREATE TABLE usage_events_staging (
    staging_id      BIGSERIAL PRIMARY KEY,
    raw_sid         BIGINT,
    profile_id      BIGINT,
    event_timestamp TIMESTAMP,
    usage_mb        NUMERIC(12,4),
    country_code    VARCHAR(5),
    network_code    VARCHAR(5),
    tech_raw        VARCHAR(20),
    tech_code       VARCHAR(10),
    apn             VARCHAR(100),
    source_file     VARCHAR(200),
    loaded_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_duplicate    BOOLEAN DEFAULT FALSE,
    quality_flags   TEXT,
    processed_at    TIMESTAMP
);
```

**Risks and tradeoffs**

The biggest risk is migration complexity. Any existing queries, reports, or ETL jobs that touch the old tables will break. I'd handle this by running the new schema alongside the old one with dual-write for a transition period, plus views that map old column names to new ones so nothing breaks immediately.

The foreign key checks add overhead on inserts, but at these data volumes it's negligible. For high-throughput ingestion, the staging table pattern already solves this — data lands in staging (no FKs), gets validated, then bulk-inserted into production.

Over-normalization is a tradeoff too. Analytical queries that need to go from usage event → cost now require 4 joins. A materialized view or denormalized reporting table would help for dashboards.

Using NULL for effective_to to mean "currently active" is standard SCD2 pattern but means you need COALESCE in queries. The alternative is a far-future sentinel date (9999-12-31), but I think NULL is more honest about what it represents.

---

## Assumptions

1. I used (pid, evt_dttm) as the natural key for dedup. If the same profile can legitimately generate two distinct events at the exact same timestamp, this would be wrong — but that seems unlikely.

2. When duplicates had conflicting MB values (50 vs 55), I kept the first-loaded version. Could also average them or flag for manual review.

3. CDMA classified as 2G/3G ambiguous, not counted toward 3G total.

4. The negative MB event is treated as bad data, not a legitimate reversal. If reversals are a real business concept, the cost model needs work.

5. For rate matching, higher prio_nbr + tech-specific match wins. Generic (NULL tech) rate is the fallback.

6. cc1/cc2 are MCC/MNC (standard telecom identifiers). 310 = US, 234 = UK.

**Questions I'd want to ask**

- What does `sid` actually represent? Globally unique event ID, or per-source-file sequence? The duplicates sharing sid=2 suggest the latter.
- Are negative MB values intentional (reversals/corrections) or data errors?
- Is pid 999 test data? Should we filter it in production queries?
- What about bundle 9999 in rate_card — same question.
- How should overlapping profile installations be handled? pid 102 has two SIMs active simultaneously Jan 18-20.
- What's the `why_cd` taxonomy? "activation" vs "ACT" — same thing? What does "FIX" mean?
- The event at 2035-01-01 — typo for what date?
- Which rate_card row is authoritative when bundle 2000 has three entries for the same (310, 260, 4G)?

---

## How to reproduce

Requirements: Python 3.10+ (no external packages needed — the parquet reader is pure Python)

```bash
python3 analysis.py
```

If you have pandas and pyarrow installed, you can also read the data directly:

```python
import pandas as pd
for f in ['usage_events','profile_installation','rate_card','sim_card_plan_history']:
    print(pd.read_parquet(f'data/{f}.parquet').to_string())
```

Open `daily_usage_chart.html` in a browser for the interactive chart. Open `redesigned_erd.html` for the new schema diagram.
