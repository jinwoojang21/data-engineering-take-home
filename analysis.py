import sys, json
sys.path.insert(0, '/home/claude')
from parquet_reader_v2 import read_parquet
from datetime import datetime, timezone

def ns_to_dt(ns):
    if ns is None: return None
    try: return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    except: return None

# Load all data
tables = {}
for fn in ['usage_events', 'profile_installation', 'rate_card', 'sim_card_plan_history']:
    fp = f'/home/claude/data-engineering-take-home-main/data/{fn}.parquet'
    cols, nrows, meta = read_parquet(fp)
    rows = []
    col_names = list(cols.keys())
    for i in range(nrows):
        row = {}
        for c in col_names:
            v = cols[c][i] if i < len(cols[c]) else None
            row[c] = v
        rows.append(row)
    tables[fn] = rows

ue = tables['usage_events']
pi_tbl = tables['profile_installation']
rc = tables['rate_card']
scph = tables['sim_card_plan_history']

print("="*80)
print("DATA QUALITY ANALYSIS")
print("="*80)

# 1. DUPLICATES
print("\n--- DUPLICATE DETECTION ---")
seen = {}
duplicates = []
for i, r in enumerate(ue):
    key = (r['pid'], r['evt_dttm'])
    if key in seen:
        duplicates.append((i, seen[key], key))
    else:
        seen[key] = i

print("Duplicate events found by (pid, evt_dttm):", len(duplicates))
for dup in duplicates:
    idx, orig, key = dup
    dt = ns_to_dt(key[1]) if key[1] else None
    orig_r = ue[orig]
    dup_r = ue[idx]
    print(f"  Row {idx} duplicates row {orig}: pid={key[0]}, date={dt}")
    print(f"    Original: sid={orig_r['sid']}, mb={orig_r['mb']}, src={orig_r['src_nm']}")
    print(f"    Duplicate: sid={dup_r['sid']}, mb={dup_r['mb']}, src={dup_r['src_nm']}")

# 2. DATA QUALITY ISSUES
print("\n--- DATA QUALITY ISSUES ---")
for i, r in enumerate(ue):
    issues = []
    if r['mb'] is not None and r['mb'] < 0:
        issues.append(f"Negative MB: {r['mb']}")
    if r['evt_dttm'] is not None:
        dt = ns_to_dt(r['evt_dttm'])
        if dt and dt.year > 2026:
            issues.append(f"Future date: {dt}")
    if r['evt_dttm'] is None:
        issues.append("Missing evt_dttm")
    if r['cc1'] is None:
        issues.append("Missing cc1")
    if r['cc2'] is not None and r['cc2'] > 1000:
        issues.append(f"Suspicious cc2: {r['cc2']}")
    if r['tech'] is None:
        issues.append("Missing tech")
    if issues:
        print(f"  Row {i} (sid={r['sid']}): {'; '.join(issues)}")

# TECH NORMALIZATION
print("\n--- TECH NORMALIZATION ---")
tech_counts = {}
for r in ue:
    t = r['tech']
    tech_counts[str(t)] = tech_counts.get(str(t), 0) + 1
print("Raw tech distribution:", json.dumps(tech_counts, indent=2))

def normalize_tech(t):
    if t is None: return None
    t_upper = t.upper()
    if t_upper in ('LTE', '4G'): return '4G'
    if t_upper in ('5G', 'NR'): return '5G'
    if t_upper == 'HSPA+': return '3G'
    if t_upper == 'GSM': return '2G'
    if t_upper == 'CDMA': return '2G/3G'
    return t_upper

tech_norm_counts = {}
for r in ue:
    n = normalize_tech(r['tech'])
    tech_norm_counts[str(n)] = tech_norm_counts.get(str(n), 0) + 1
print("Normalized tech distribution:", json.dumps(tech_norm_counts, indent=2))

# 3. DEDUPLICATED DATA
clean_ue = []
seen_keys = set()
for r in ue:
    key = (r['pid'], r['evt_dttm'])
    if key in seen_keys:
        continue
    seen_keys.add(key)
    clean_ue.append(r)

print(f"\nAfter dedup: {len(clean_ue)} events (removed {len(ue) - len(clean_ue)})")

# Count 3G after cleanup
count_3g = sum(1 for r in clean_ue if normalize_tech(r['tech']) == '3G')
print(f"3G events after cleanup: {count_3g}")

# 4. SIM CARD MAPPING
def get_sim_card(pid, evt_ns):
    if pid is None or evt_ns is None: return None
    matches = []
    for p in pi_tbl:
        if p['pid'] != pid: continue
        beg = p['beg_dttm']
        end = p['end_dttm']
        if beg is not None and evt_ns < beg: continue
        if end is not None and evt_ns > end: continue
        matches.append(p)
    if len(matches) >= 1:
        return max(matches, key=lambda x: x['beg_dttm'] or 0)['asset_id']
    return None

sim_usage = {}
for r in clean_ue:
    sim = get_sim_card(r['pid'], r['evt_dttm'])
    mb = max(r['mb'], 0) if r['mb'] is not None else 0
    label = sim if sim is not None else 'UNMAPPED'
    sim_usage[label] = sim_usage.get(label, 0) + mb

print("\n--- USAGE BY SIM_CARD_ID ---")
for sim, mb in sorted(sim_usage.items(), key=lambda x: -x[1]):
    print(f"  sim_card {sim}: {mb:.1f} MB")

mapped_sims = {k: v for k, v in sim_usage.items() if k != 'UNMAPPED'}
highest_sim = max(mapped_sims, key=lambda k: mapped_sims[k])

# 5. COST CALCULATION
def get_bundle(asset_id, evt_ns):
    if asset_id is None or evt_ns is None: return None
    matches = []
    for s in scph:
        if s['asset_id'] != asset_id: continue
        eff = s['eff_dttm']
        x = s['x_dttm']
        if eff is not None and evt_ns < eff: continue
        if x is not None and evt_ns > x: continue
        matches.append(s)
    if matches:
        return max(matches, key=lambda x: x['eff_dttm'] or 0)['bundle_id']
    return None

def get_rate(bundle_id, cc1_val, cc2_val, tech, evt_ns):
    if bundle_id is None: return None
    tech_norm = normalize_tech(tech)
    
    candidates = []
    for r in rc:
        if r['bundle_id'] != bundle_id: continue
        # Skip negative rates
        if r['rt_amt'] is not None and r['rt_amt'] < 0: continue
        # Check cc1 match
        cc1_int = int(cc1_val) if cc1_val is not None else None
        if r['cc1'] is not None and cc1_int is not None and r['cc1'] != cc1_int: continue
        # Check cc2 match
        if r['cc2'] is not None and cc2_val is not None and r['cc2'] != cc2_val: continue
        # Check date range
        beg = r['beg_dttm']
        end = r['end_dttm']
        if beg is not None and evt_ns is not None and evt_ns < beg: continue
        if end is not None and evt_ns is not None and evt_ns > end: continue
        if r['rt_amt'] is None: continue
        candidates.append(r)
    
    if not candidates: return None
    
    # Prefer tech-specific match with highest priority
    if tech_norm:
        tech_matches = [c for c in candidates if c.get('tech_cd') is not None and c['tech_cd'].upper() == tech_norm]
        if tech_matches:
            return max(tech_matches, key=lambda x: x.get('prio_nbr') or 0)['rt_amt']
    
    # Fallback to generic (no tech_cd)
    fallback = [c for c in candidates if c.get('tech_cd') is None]
    if fallback:
        return min(fallback, key=lambda x: x.get('prio_nbr') or 0)['rt_amt']
    
    return candidates[0]['rt_amt']

total_cost = 0
print("\n--- COST BREAKDOWN ---")
print(f"{'sid':>4} {'mb':>7} {'sim':>6} {'bundle':>7} {'rate':>8} {'cost':>10} {'tech':>6}")
print("-" * 55)
for r in clean_ue:
    mb = max(r['mb'], 0) if r['mb'] is not None else 0
    if mb == 0:
        continue
    sim = get_sim_card(r['pid'], r['evt_dttm'])
    bundle = get_bundle(sim, r['evt_dttm']) if sim else None
    rate = get_rate(bundle, r['cc1'], r['cc2'], r['tech'], r['evt_dttm']) if bundle else None
    cost = mb * rate if rate else None
    cost_str = f"${cost:.4f}" if cost else "N/A"
    rate_str = f"{rate:.4f}" if rate else "N/A"
    print(f"{r['sid']:>4} {mb:>7.1f} {str(sim):>6} {str(bundle):>7} {rate_str:>8} {cost_str:>10} {str(r['tech']):>6}")
    if cost is not None:
        total_cost += cost

# 6. DAILY USAGE
daily = {}
for r in clean_ue:
    dt = ns_to_dt(r['evt_dttm'])
    if dt is None: continue
    day = dt.strftime('%Y-%m-%d')
    mb = max(r['mb'], 0) if r['mb'] is not None else 0
    daily[day] = daily.get(day, 0) + mb

print("\n--- DAILY USAGE ---")
for day in sorted(daily):
    print(f"  {day}: {daily[day]:.1f} MB")

with open('/home/claude/daily_usage.json', 'w') as f:
    json.dump([{'date': d, 'mb': daily[d]} for d in sorted(daily)], f)

print("\n" + "="*80)
print("SUMMARY ANSWERS")
print("="*80)
print(f"1. Highest total usage sim_card_id: {highest_sim} ({mapped_sims[highest_sim]:.1f} MB)")
print(f"2. 3G events after cleanup: {count_3g}")
print(f"3. Duplicate usage events identified: {len(duplicates)}")
print(f"4. Total cost of all data: ${total_cost:.4f}")
