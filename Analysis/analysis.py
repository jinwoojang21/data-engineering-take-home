"""
Usage Data Analysis
Requirements: pip install pandas pyarrow
"""
import pandas as pd
from datetime import datetime

# --- Load data ---
ue = pd.read_parquet('data/usage_events.parquet')
pi = pd.read_parquet('data/profile_installation.parquet')
rc = pd.read_parquet('data/rate_card.parquet')
scph = pd.read_parquet('data/sim_card_plan_history.parquet')

# Convert nanosecond timestamps to datetime where needed
for col in ['evt_dttm', 'ld_dttm']:
    ue[col] = pd.to_datetime(ue[col], unit='ns', utc=True, errors='coerce')
for col in ['beg_dttm', 'end_dttm', 'crt_dttm']:
    pi[col] = pd.to_datetime(pi[col], unit='ns', utc=True, errors='coerce')
for col in ['beg_dttm', 'end_dttm']:
    rc[col] = pd.to_datetime(rc[col], unit='ns', utc=True, errors='coerce')
for col in ['eff_dttm', 'x_dttm', 'upd_dttm']:
    scph[col] = pd.to_datetime(scph[col], unit='ns', utc=True, errors='coerce')

print("Loaded tables:")
print(f"  usage_events: {len(ue)} rows")
print(f"  profile_installation: {len(pi)} rows")
print(f"  rate_card: {len(rc)} rows")
print(f"  sim_card_plan_history: {len(scph)} rows")


# --- Tech normalization ---
def normalize_tech(t):
    if pd.isna(t):
        return None
    t = t.upper()
    if t in ('LTE', '4G'): return '4G'
    if t in ('5G', 'NR'): return '5G'
    if t == 'HSPA+': return '3G'
    if t == 'GSM': return '2G'
    if t == 'CDMA': return '2G/3G'
    return t

ue['tech_norm'] = ue['tech'].apply(normalize_tech)


# --- Duplicate detection ---
dupes = ue.duplicated(subset=['pid', 'evt_dttm'], keep='first')
num_dupes = dupes.sum()
print(f"\nDuplicates found (by pid + evt_dttm): {num_dupes}")
print(ue[dupes][['sid', 'pid', 'evt_dttm', 'mb', 'src_nm']].to_string(index=False))

clean = ue[~dupes].copy()
print(f"After dedup: {len(clean)} events")


# --- 3G count ---
count_3g = (clean['tech_norm'] == '3G').sum()
print(f"\n3G events after cleanup: {count_3g}")


# --- SIM card mapping ---
def get_sim_card(pid, evt_time):
    if pd.isna(pid) or pd.isna(evt_time):
        return None
    matches = pi[(pi['pid'] == pid)]
    matches = matches[matches['beg_dttm'].isna() | (matches['beg_dttm'] <= evt_time)]
    matches = matches[matches['end_dttm'].isna() | (matches['end_dttm'] >= evt_time)]
    if len(matches) == 0:
        return None
    return matches.sort_values('beg_dttm', ascending=False).iloc[0]['asset_id']

clean['sim_card_id'] = clean.apply(lambda r: get_sim_card(r['pid'], r['evt_dttm']), axis=1)

sim_usage = clean.copy()
sim_usage['mb_clean'] = sim_usage['mb'].clip(lower=0)
totals = sim_usage.groupby('sim_card_id')['mb_clean'].sum().sort_values(ascending=False)
print("\nUsage by sim_card_id:")
print(totals.to_string())
highest = totals.index[0]
print(f"\nHighest: sim_card_id {int(highest)} ({totals.iloc[0]:.1f} MB)")


# --- Cost calculation ---
def get_bundle(asset_id, evt_time):
    if pd.isna(asset_id) or pd.isna(evt_time):
        return None
    matches = scph[scph['asset_id'] == asset_id]
    matches = matches[matches['eff_dttm'].isna() | (matches['eff_dttm'] <= evt_time)]
    matches = matches[matches['x_dttm'].isna() | (matches['x_dttm'] >= evt_time)]
    if len(matches) == 0:
        return None
    return matches.sort_values('eff_dttm', ascending=False).iloc[0]['bundle_id']

def get_rate(bundle_id, cc1, cc2, tech_norm, evt_time):
    if pd.isna(bundle_id):
        return None
    candidates = rc[rc['bundle_id'] == bundle_id].copy()
    candidates = candidates[candidates['rt_amt'].notna() & (candidates['rt_amt'] > 0)]
    if cc1 is not None and not pd.isna(cc1):
        candidates = candidates[candidates['cc1'].isna() | (candidates['cc1'] == int(cc1))]
    if cc2 is not None and not pd.isna(cc2):
        candidates = candidates[candidates['cc2'].isna() | (candidates['cc2'] == cc2)]
    if evt_time is not None:
        candidates = candidates[candidates['beg_dttm'].isna() | (candidates['beg_dttm'] <= evt_time)]
        candidates = candidates[candidates['end_dttm'].isna() | (candidates['end_dttm'] >= evt_time)]
    if len(candidates) == 0:
        return None
    # prefer tech-specific match
    if tech_norm:
        tech_match = candidates[candidates['tech_cd'].str.upper() == tech_norm]
        if len(tech_match) > 0:
            return tech_match.sort_values('prio_nbr', ascending=False).iloc[0]['rt_amt']
    # fallback to generic
    generic = candidates[candidates['tech_cd'].isna()]
    if len(generic) > 0:
        return generic.sort_values('prio_nbr').iloc[0]['rt_amt']
    return candidates.iloc[0]['rt_amt']

total_cost = 0
for _, r in clean.iterrows():
    mb = max(r['mb'], 0) if not pd.isna(r['mb']) else 0
    if mb == 0:
        continue
    sim = r['sim_card_id']
    bundle = get_bundle(sim, r['evt_dttm']) if not pd.isna(sim) else None
    rate = get_rate(bundle, r['cc1'], r['cc2'], r['tech_norm'], r['evt_dttm']) if bundle else None
    if rate:
        total_cost += mb * rate

print(f"\nTotal cost: ${total_cost:.2f}")


# --- Daily usage for chart ---
daily = clean.copy()
daily['date'] = daily['evt_dttm'].dt.date
daily['mb_clean'] = daily['mb'].clip(lower=0)
daily_totals = daily.groupby('date')['mb_clean'].sum().sort_index()
print("\nDaily usage:")
print(daily_totals.to_string())


# --- Summary ---
print("\n" + "=" * 50)
print("ANSWERS")
print("=" * 50)
print(f"1. Highest usage sim_card_id: {int(highest)} ({totals.iloc[0]:.1f} MB)")
print(f"2. 3G events after cleanup: {count_3g}")
print(f"3. Duplicate events: {num_dupes}")
print(f"4. Total cost: ${total_cost:.2f}")
