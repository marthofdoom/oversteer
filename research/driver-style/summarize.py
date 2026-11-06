"""The profile table from ~/.cache/oversteer-style/features.json (extract.py)."""

import collections
import json
import os
import statistics as st

import numpy as np

F = json.load(open(os.path.expanduser('~/.cache/oversteer-style/features.json')))
R, K, S = F['runs'], F['corners'], F['sections']
P = {}

print('== runs (clean, finished) ==')
for r in R:
    print(r['id'], r['car'], r['stage'], r['surface'], round(r['km'], 2), r['result'], r.get('sectors'))
print('cars', collections.Counter(r['car'] for r in R), 'surfaces', collections.Counter(r['surface'] for r in R))
km = sum(r['km'] for r in R)
print('km clean', round(km, 1))

# Braking
P['lfb_share'] = sum(r['lfb_s'] for r in R) / sum(r['brake_s'] for r in R)
by_surface = collections.defaultdict(lambda: [0, 0])
for r in R:
    by_surface[r['surface']][0] += r['lfb_s']
    by_surface[r['surface']][1] += r['brake_s']
P['lfb_by_surface'] = {k: a / b for k, (a, b) in by_surface.items()}
braked = [k for k in K if k['braked']]
P['corners'] = len(K)
P['braked_corners'] = len(braked)
P['overlap_entry_share'] = np.mean([(k['overlap_entry'] or 0) > 0.2 for k in braked])
P['trail_at_turn_in'] = np.mean([k['trail'] for k in braked])
P['trail_share_median'] = float(np.median([k['trail_share'] for k in braked]))
sb = [s for s in S if s['braked'] and s['brake_early'] is not None]
P['brake_early_median_m'] = float(np.median([s['brake_early'] for s in sb]))
P['early_brake_share'] = np.mean([s['brake_early'] > 10.0 for s in sb])
P['over_slowed_share'] = np.mean([s['brake_early'] > 10.0 and s['apex'] is not None and s['apex_pot'] is not None
                                  and s['apex'] < s['apex_pot'] - 3.0 for s in sb])
P['late_brake_share'] = np.mean([s['brake_early'] < -10.0 for s in sb])
P['sections_braked'] = len(sb)

# Rotation
P['handbrake'] = 'not sent by ACR (trace channel all NaN)'
w = [(k['counter'], k['duration']) for k in K if k['counter'] is not None and k['duration']]
P['counter_steer_time_share'] = sum(c * d for c, d in w) / sum(d for c, d in w)
P['counter_corner_share'] = np.mean([c > 0.15 for c, d in w])
nf = [k for k in K if not k['s_bend']]
P['flick_share'] = np.mean([k['flick'] for k in nf])
P['flick_share_tight'] = np.mean([k['flick'] for k in nf if k['radius'] and k['radius'] < 45])
C = F['capture']
cc = {k: v for k, v in C.items() if v['cls'] == 'clean'}
ca = C
print('captured clean runs', sorted(cc), 'all captured classes', collections.Counter(v['cls'] for v in C.values()))


def pool(src, key):
    vals = [(v[key], v['rows']) for v in src.values() if v.get(key) is not None]
    return sum(a * n for a, n in vals) / sum(n for a, n in vals) if vals else None


for name, src in (('clean', cc), ('allfinished', ca)):
    P['beta_turn_p50_' + name] = pool(src, 'beta_turn_p50')
    P['beta_turn_p90_' + name] = pool(src, 'beta_turn_p90')
    P['beta_p995_' + name] = pool(src, 'beta_turn_max')
    P['slide_share_' + name] = pool(src, 'slide_share')
    P['counter_in_slide_' + name] = pool(src, 'counter_in_slide')
    P['srate60_p50_' + name] = pool(src, 'srate_p50')
    P['srate60_p90_' + name] = pool(src, 'srate_p90')
    P['captured_runs_' + name] = len(src)
for surf in ('gravel', 'tarmac'):
    sub = {k: v for k, v in ca.items() if v['surface'] == surf}
    P['beta_turn_p50_' + surf] = pool(sub, 'beta_turn_p50')
    P['beta_turn_p90_' + surf] = pool(sub, 'beta_turn_p90')

# Throttle
ton = [k['throttle_on_t'] for k in K if k['throttle_on_t'] is not None]
P['throttle_on_after_apex_median_s'] = float(np.median(ton))
P['throttle_on_before_or_at_apex_share'] = np.mean([x <= 0.0 for x in ton])
tf = [k['throttle_t'] for k in K if k['throttle_t'] is not None]
P['full_after_apex_median_s'] = float(np.median(tf))
ramps = [(0.95 - 0.2) / (k['throttle_t'] - k['throttle_on_t']) for k in K
         if k['throttle_t'] is not None and k['throttle_on_t'] is not None and k['throttle_t'] - k['throttle_on_t'] > 0.05]
P['ramp_median_per_s'] = float(np.median(ramps))
P['relifts_per_corner'] = np.mean([k['relifts'] or 0 for k in K])
P['full_throttle_time_share'] = sum(r['full_s'] for r in R) / sum(r['moving_s'] for r in R)

# Smoothness (10 Hz trace)
P['steer_rate_p50_10hz'] = st.median(r['steer_rate_p50'] for r in R)
P['steer_rate_p90_10hz'] = st.median(r['steer_rate_p90'] for r in R)

# Commitment
gu = [s['grip_used'] for s in S if s['grip_used'] is not None and s['radius'] and s['radius'] < 120]
P['grip_used_median'] = float(np.median(gu))
P['grip_used_p90'] = float(np.percentile(gu, 90))
P['grip_used_ge_092_share'] = np.mean([g >= 0.92 for g in gu])
ratios = [(r['pot_grip_s'] / r['result'], r['result'] - r['pot_grip_s'], r['stage'], r['car'])
          for r in R if r.get('pot_grip_s') and r['result']]
P['grip_layer_pace'] = float(np.median([a for a, *_ in ratios]))
P['gap_to_grip_per_km'] = float(np.median([g / r['km'] for (a, g, *_), r in zip(ratios, [r for r in R if r.get('pot_grip_s') and r['result']])]))
apex = [s['apex'] / s['apex_pot'] for s in S if s['apex'] and s['apex_pot']]
P['apex_speed_vs_grip_layer'] = float(np.median(apex))

# Consistency: repeated stage+car among clean runs
groups = collections.defaultdict(list)
for r in R:
    groups[(r['stage'], r['car'])].append(r)
cv, spreads = [], []
for key, rs in groups.items():
    if len(rs) < 2:
        continue
    res = [r['result'] for r in rs]
    spreads.append((key, len(rs), max(res) - min(res), st.pstdev(res) / st.mean(res)))
    secs = [r['sectors'] for r in rs if r['sectors'] and all(x is not None for x in r['sectors'])]
    if len(secs) >= 2:
        for j in range(len(secs[0])):
            col = [s[j] for s in secs]
            cv.append((key, j + 1, st.pstdev(col), st.pstdev(col) / st.mean(col)))
P['stage_spread'] = spreads
P['sector_sd'] = cv
P['sector_cv_median'] = float(np.median([c[3] for c in cv])) if cv else None
P['sector_sd_median_s'] = float(np.median([c[2] for c in cv])) if cv else None

# Risk
fin = F['risk']['finished']
tot = {k: sum(g[k] for g in fin.values()) for k in ('runs', 'km', 'off', 'spin', 'hit')}
P['risk_finished'] = fin
P['risk_total'] = tot
P['offs_per_10km'] = 10 * tot['off'] / tot['km']
P['incidents_per_10km'] = 10 * (tot['off'] + tot['spin'] + tot['hit']) / tot['km']
P['risk_unfinished'] = F['risk']['unfinished']
P['clean_share_of_finished'] = fin['clean']['runs'] / tot['runs']

for k, v in P.items():
    print(k, '=', v if not isinstance(v, float) else round(v, 4))
json.dump(P, open(os.path.expanduser('~/.cache/oversteer-style/profile.json'), 'w'), indent=1, default=float)
