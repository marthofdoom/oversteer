"""One-off driver style profile from marth's ACR runs (research, not shipped).

Read-only: opens a sqlite .backup copy in ~/.cache/oversteer-style with mode=ro, and the captures read-only.
Writes ~/.cache/oversteer-style/features.json.

  python3 -I research/driver-style/extract.py [db copy]
"""

import glob
import json
import math
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, REPO)

import numpy as np  # noqa: E402

from oversteer import coach_context as cc, potential, run_analysis, telemetry_store as ts  # noqa: E402
from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer.telemetry_formats import decode_sample  # noqa: E402

CACHE = os.path.expanduser('~/.cache/oversteer-style')
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CACHE, 'flatpak.db')
CAPTURES = os.path.expanduser('~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')
CH = {n: i for i, n in enumerate(ts.TRACE_CHANNELS)}

reader = ts.open_reader(DB)
db = reader.db

runs = [dict(zip(('id', 'cls', 'car', 'car_id', 'stage', 'surface', 'dist', 'result', 'started', 'ended'), r))
        for r in db.execute(
            "SELECT r.id, r.run_class, c.name, c.id, r.stage, r.surface, r.distance, r.result_time, r.started, r.ended "
            "FROM runs r JOIN sessions s ON s.id = r.session JOIN cars c ON c.id = s.car "
            "WHERE s.game = 'acr' AND r.finished = 1 ORDER BY r.id")]
clean = [r for r in runs if r['cls'] == 'clean']


def bad_ranges(run_id):
    return [(e['d0'], e['d1'] if e['d1'] is not None else e['d0']) for e in reader.events(run_id)
            if e['kind'] in ('off', 'spin', 'stall', 'hit') and e['d0'] is not None]


def near_bad(d, bad, reach=cc.INCIDENT_REACH):
    return any(lo - reach <= d <= hi + reach for lo, hi in bad)


out = {'runs': [], 'corners': [], 'sections': [], 'sectors': [], 'risk': {}, 'capture': {}}

# ---------- per clean run: trace features ----------
for r in clean:
    run = reader.run(r['id'])
    rows = run_analysis.stage_trace(reader, run)
    arr = potential.arrays(rows)
    bad = bad_ranges(r['id'])
    t, d, v = arr['t'], arr['d'], arr['speed']
    thr, brk, steer, yaw = arr['throttle'], arr['brake'], arr['steer'], arr['yaw_rate']
    dt = np.r_[np.diff(t), 0.1]
    ok = np.array([not near_bad(x, bad) for x in d]) & (v > 5.0) & np.isfinite(thr) & np.isfinite(brk)
    braking = ok & (brk > cc.BRAKE_ON)
    lfb = braking & (thr > 0.10)
    moving = ok & (v > 10.0)
    full = moving & (thr >= cc.THROTTLE_FULL)
    srate = np.abs(np.diff(steer) / np.maximum(np.diff(t), 1e-3))
    srate = srate[moving[1:] & np.isfinite(srate)]
    rec = dict(id=r['id'], cls=r['cls'], car=r['car'], stage=r['stage'], surface=r['surface'],
               km=r['dist'] / 1000.0, result=r['result'],
               brake_s=float(dt[braking].sum()), lfb_s=float(dt[lfb].sum()),
               moving_s=float(dt[moving].sum()), full_s=float(dt[full].sum()),
               steer_rate_p50=float(np.percentile(srate, 50)), steer_rate_p90=float(np.percentile(srate, 90)),
               steer_rate_p99=float(np.percentile(srate, 99)))

    # Corners (stored by the coach) with the trace around them
    corners = sorted(reader.corners(r['id']), key=lambda k: k['d'])
    for n, k in enumerate(corners):
        if k.get('off') or near_bad(k['d'], bad) or k.get('d0') is None:
            continue
        i0 = int(np.searchsorted(d, k['d0']))
        ia = int(np.searchsorted(d, k['d']))
        if ia <= i0 or ia >= len(d):
            continue
        # Trail braking: brake still on at turn-in (yaw window start), and how far into the entry it lasted
        braked = k.get('brake_d') is not None
        on = brk[i0:ia + 1] > cc.BRAKE_ON
        trail_at_turn_in = bool(braked and np.isfinite(brk[i0]) and brk[i0] > cc.BRAKE_ON)
        trail_share = float(on.mean()) if braked and on.size else 0.0         # of turn-in -> slowest point under brake
        # Scandinavian flick: in the 2 s before turn-in, yaw the other way past 0.15 rad/s with steer the other way,
        # not part of an S (no opposite corner ending within 2 s before)
        sign = k['direction']
        tt = t[i0]
        w = (t >= tt - 2.0) & (t < tt)
        prev = corners[n - 1] if n else None
        s_bend = False
        if prev is not None and prev['direction'] == -sign and prev.get('d1') is not None:
            j1 = int(np.searchsorted(d, prev['d1']))
            s_bend = j1 < len(t) and t[j1] > tt - 2.5
        flick = bool(not s_bend and w.any() and np.nanmin(yaw[w] * sign) < -0.15 and np.nanmin(steer[w] * sign) < -0.05)
        # The LFB share inside corners: overlap while slowing on entry
        out['corners'].append(dict(
            run=r['id'], car=r['car'], surface=r['surface'], d=k['d'], dir=sign, radius=k.get('radius'),
            heading=k.get('heading_change'), duration=k.get('duration'), counter=k.get('counter_steer'),
            brake_d=k.get('brake_d'), throttle_on_t=k.get('throttle_on_t'), throttle_t=k.get('throttle_t'),
            overlap_entry=k.get('overlap_entry'), relifts=k.get('relifts'), braked=braked,
            trail=trail_at_turn_in, trail_share=trail_share, flick=flick, s_bend=s_bend,
            tightness=k.get('tightness'), min_speed=k.get('min_speed')))

    # Against the stored potential (the grip layer): grip used at the apex, braking vs the layer's point
    pot = reader.potential(r['stage'], r['car_id'])
    if pot:
        for s in potential.analyse_run(pot, arr, bad):
            if s['time'] is None:
                continue
            out['sections'].append(dict(run=r['id'], car=r['car'], surface=r['surface'], time=s['time'],
                                        grip_s=s['grip_s'], grip_used=s['grip_used'], apex=s['apex_kmh'],
                                        apex_pot=s['apex_kmh_pot'], braked=s['braked'], brake_early=s['brake_early'],
                                        radius=s.get('radius_m'), cause=s['cause']))
        rec.update(pot_grip_s=pot['grip_s'], pot_car_s=pot['car_s'], pot_user_s=pot['user_s'])
    # The game's sectors
    sec = run_analysis._sector_rows(r['stage'], rows, None, reader, (run, None))
    rec['sectors'] = [s['time'] for s in sec] if sec else None
    out['runs'].append(rec)

# ---------- risk: every finished ACR run ----------
risk = {}
for r in runs:
    ev = reader.events(r['id'])
    g = risk.setdefault(r['cls'], {'runs': 0, 'km': 0.0, 'off': 0, 'spin': 0, 'hit': 0})
    g['runs'] += 1
    g['km'] += r['dist'] / 1000.0
    for e in ev:
        if e['kind'] in ('off', 'spin', 'hit'):
            g[e['kind']] += 1
unfinished = db.execute("SELECT r.run_class, count(*), sum(r.distance) FROM runs r JOIN sessions s ON s.id = r.session "
                        "WHERE s.game = 'acr' AND (r.finished IS NULL OR r.finished = 0) GROUP BY 1").fetchall()
out['risk'] = {'finished': risk, 'unfinished': [list(x) for x in unfinished]}

# ---------- captures: slide angle (local velocity), steering at 60 Hz ----------
windows = [(r['started'], r['ended'], r) for r in runs]
beta_all, beta_corner, counter_rows, rows60, srate60 = [], [], 0, 0, []
per_run = {}
for path in sorted(glob.glob(os.path.join(CAPTURES, '*.ovcap.gz'))):
    try:
        meta, recs = read_capture(path)
    except ValueError:
        continue
    t0 = meta.get('started')
    if t0 is None:
        continue
    span = [w for w in windows if w[0] < t0 + 3 * 3600 and w[1] > t0]
    if not span:
        continue
    last = None
    for t_rel, _a, data in recs:
        wall = t0 + t_rel
        hit = next((w for w in span if w[0] <= wall <= w[1]), None)
        if hit is None:
            last = None
            continue
        s = decode_sample(data)
        if s is None or s.game != 'acr' or s.vel is None or s.speed is None:
            continue
        r = hit[2]
        pr = per_run.setdefault(r['id'], {'cls': r['cls'], 'car': r['car'], 'surface': r['surface'], 'n': 0,
                                          'beta': [], 'beta_turn': [], 'counter': 0, 'slide': 0, 'srate': []})
        fwd, left = s.vel[0], s.vel[1]
        if fwd > 8.0:
            beta = math.degrees(math.atan2(left, fwd))
            pr['n'] += 1
            pr['beta'].append(beta)
            if s.yaw_rate is not None and abs(s.yaw_rate) > 0.2:
                pr['beta_turn'].append(beta)
            if abs(beta) > 5.0 and s.steer is not None:
                pr['slide'] += 1
                if s.steer * beta > 0 and abs(s.steer) > 0.02:
                    pr['counter'] += 1
            if last is not None and s.steer is not None and last[1] is not None and 0 < wall - last[0] < 0.05:
                pr['srate'].append(abs(s.steer - last[1]) / (wall - last[0]))
        last = (wall, s.steer)

cap = {}
for rid, pr in per_run.items():
    b = np.abs(np.asarray(pr['beta']))
    bt = np.abs(np.asarray(pr['beta_turn']))
    sr = np.asarray(pr['srate'])
    cap[rid] = dict(cls=pr['cls'], car=pr['car'], surface=pr['surface'], rows=pr['n'],
                    beta_p50=float(np.median(b)) if b.size else None,
                    beta_turn_p50=float(np.median(bt)) if bt.size else None,
                    beta_turn_p90=float(np.percentile(bt, 90)) if bt.size else None,
                    beta_turn_max=float(np.percentile(bt, 99.5)) if bt.size else None,
                    slide_share=pr['slide'] / pr['n'] if pr['n'] else None,
                    counter_in_slide=pr['counter'] / pr['slide'] if pr['slide'] else None,
                    srate_p50=float(np.median(sr)) if sr.size else None,
                    srate_p90=float(np.percentile(sr, 90)) if sr.size else None)
out['capture'] = cap

with open(os.path.join(CACHE, 'features.json'), 'w') as f:
    json.dump(out, f, indent=1, default=float)
print('runs', len(out['runs']), 'corners', len(out['corners']), 'sections', len(out['sections']),
      'captured runs', len(cap))
