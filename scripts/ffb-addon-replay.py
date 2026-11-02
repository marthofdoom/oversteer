#!/usr/bin/env python3
"""Replay a Forza Horizon capture through the Countersteer FFB add-on (oversteer/ffb_addon.py), as numbers.

    scripts/ffb-addon-replay.py FILE.ovcap.gz [--wheel FILE.wheel.gz] [--force constant|aligning] [--pass-hz N]
                                [--strength 0..1] [--a A] [--k K] [--sweep]
                                [--fixture OUT.json.gz]

Each telemetry sample goes through ForzaLightness.feed() and gain() (the engine's code path) on the capture's
clock; --pass-hz N adds engine passes between samples (0: one per sample). The game's force was not recorded,
so a stand-in is fed as game_ffb_raw: `constant` (a fixed level) or `aligning` (k x steering x a speed factor,
from the sidecar's steering, or the game's if there is none). It only matters for the add-on's readiness
check; the gain g follows the slip alone. --a/--k change the lightness() constants for what-if runs.

Prints: share of driving time with g under 0.95/0.8/0.6 and at what speeds, the distribution of the combined
slip s, chatter (g reversals per second, largest step after the slew), g dropping with no front understeer
(slip ratio driving s on the front tyres; oversteer), pauses and jumps, and where the front tyres' grip
saturates (lateral acceleration against front slip angle). Nothing here touches a wheel or the running app.
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer import ffb_addon as fa  # noqa: E402
from oversteer.telemetry_capture import read_capture, read_wheel  # noqa: E402
from oversteer.telemetry_formats import decode_sample  # noqa: E402

G = 9.81
SPEED_BINS = (5, 15, 30, 45, 60, 1e9)    # m/s


def load(path):
    items = []
    for t, _addr, data in read_capture(path)[1]:
        sample = decode_sample(data)
        if sample is not None and sample.game == 'forza-fh':
            items.append((t, sample))
    return items


def load_steering(path):
    """([t], [steering -1..1 positive left]) of a sidecar's ABS_X."""
    meta, records = read_wheel(path)
    lo, hi, _ = meta.get('axes', {}).get('0', [0, 65535, 0])
    mid = 0.5 * (lo + hi)
    ts, vs = [], []
    for t, kind, code, value in records:
        if kind == 3 and code == 0:
            ts.append(t)
            vs.append(-(value - mid) / (0.5 * (hi - lo)))
    return ts, vs


def interp(ts, vs, t, i):
    """vs at t, `i` the running index (ts rising)."""
    while i < len(ts) - 2 and ts[i + 1] <= t:
        i += 1
    if t <= ts[0] or t >= ts[-1]:
        return vs[0 if t <= ts[0] else -1], i
    f = (t - ts[i]) / max(ts[i + 1] - ts[i], 1e-9)
    return vs[i] + f * (vs[i + 1] - vs[i]), i


def pct(values, ps):
    values = sorted(values)
    if not values:
        return [None] * len(ps)
    return [values[min(int(p * len(values)), len(values) - 1)] for p in ps]


def replay(items, steering, force, pass_hz, strength, a, k):
    """One row per sample: dict of t, raw g, s, g_eff and what the analysis needs."""
    addon = fa.ForzaLightness(strength)
    rows = []
    i = 0
    prev_t = None
    flip = None
    for t, s in items:
        steer = s.steer
        if steering[0]:
            w, i = interp(steering[0], steering[1], t, i)
            if flip is None:
                flip = 1.0
            steer = w
        speed = s.speed or 0.0
        if force == 'constant':
            raw = 10000.0
        else:
            raw = 20000.0 * (steer or 0.0) * min(speed / 15.0, 1.0) / (1.0 + speed / 40.0)
        if s.car is None:
            sm = s
        else:
            sm = s
            sm.steer = steer
        # extra engine passes between samples
        if pass_hz and prev_t is not None:
            n = int((t - prev_t) * pass_hz)
            for j in range(1, n):
                addon.gain(prev_t + j / pass_hz)
        addon.feed(t, sm, raw if s.car is not None else None)
        g_eff = addon.gain(t)
        prev_t = t
        row = {'t': t, 'run': s.car is not None, 'speed': speed, 'eff': g_eff, 'ready': addon.ready,
               'refused': addon.refused}
        gl = fa.lightness(s, a, k) if s.car is not None and speed > fa.MIN_SPEED else None
        row['g'] = gl
        if gl is not None:
            ang, rat = s.slip_angle, s.slip_ratio
            ta = 0.5 * (abs(ang[0]) + abs(ang[1])) / a
            tr = 0.5 * (abs(rat[0]) + abs(rat[1])) / k
            row.update(s=0.5 * (math.hypot(ang[0] / a, rat[0] / k) + math.hypot(ang[1] / a, rat[1] / k)),
                       fa=0.5 * (abs(ang[0]) + abs(ang[1])), fr=0.5 * (abs(rat[0]) + abs(rat[1])),
                       ra=0.5 * (abs(ang[2]) + abs(ang[3])), rr=0.5 * (abs(rat[2]) + abs(rat[3])),
                       ta=ta, tr=tr, throttle=s.throttle or 0.0, drive=s.drivetrain, car=s.car,
                       ay=(s.accel[1] / G) if s.accel else 0.0, steer=steer,
                       air=s.susp_norm is not None and max(s.susp_norm) < 0.03)
        rows.append(row)
    return rows


def share(rows, key, limit):
    n = sum(1 for r in rows if r['g'] is not None)
    return sum(1 for r in rows if r['g'] is not None and r[key] < limit) / n if n else 0.0


def analyse(rows, a, k):
    drv = [r for r in rows if r['g'] is not None]
    out = {'packets': len(rows), 'driving_packets': len(drv)}
    if len(rows) < 2:
        return out
    dt = sorted(b['t'] - r['t'] for r, b in zip(rows, rows[1:]) if 0 < b['t'] - r['t'] < 0.5)
    step = dt[len(dt) // 2] if dt else 1 / 60
    out['driving_s'] = round(len(drv) * step, 1)
    out['share_raw_g'] = {str(lim): round(share(drv, 'g', lim), 4) for lim in (0.95, 0.8, 0.6)}
    out['share_g_eff'] = {str(lim): round(sum(1 for r in drv if r['eff'] < lim) / max(len(drv), 1), 4)
                          for lim in (0.95, 0.8, 0.6)}
    bins = []
    for lo, hi in zip(SPEED_BINS, SPEED_BINS[1:]):
        b = [r for r in drv if lo <= r['speed'] < hi]
        bins.append({'speed_m_s': '%g-%s' % (lo, '+' if hi > 1e8 else '%g' % hi), 'share_of_driving': round(len(b) / max(len(drv), 1), 3),
                     'g<0.8': round(sum(1 for r in b if r['g'] < 0.8) / max(len(b), 1), 4),
                     'g<0.6': round(sum(1 for r in b if r['g'] < 0.6) / max(len(b), 1), 4)})
    out['by_speed'] = bins
    out['s_percentiles'] = dict(zip(('p10', 'p50', 'p90', 'p99', 'p99.9'), [round(v, 3) for v in
                                pct([r['s'] for r in drv], (0.1, 0.5, 0.9, 0.99, 0.999))]))
    # chatter: reversals of g_eff (moves above 0.004) per second of driving, largest step per sample
    rev = 0
    last = 0
    biggest = 0.0
    fastest = 0.0
    for r, b in zip(rows, rows[1:]):
        d = b['eff'] - r['eff']
        biggest = max(biggest, abs(d))
        if 0.0 < b['t'] - r['t'] <= 0.1:       # (a longer gap is capped at 0.1 s by gain())
            fastest = max(fastest, abs(d) / (b['t'] - r['t']))
        if abs(d) > 0.004:
            sgn = 1 if d > 0 else -1
            if last and sgn != last:
                rev += 1
            last = sgn
    out['chatter'] = {'reversals_per_s': round(rev / max(out['driving_s'], 1), 2), 'largest_step_per_sample': round(biggest, 4),
                      'fastest_g_per_s_(slew limit %g)' % fa.SLEW: round(fastest, 2), 'sample_step_s': round(step, 4)}
    raw_rev = 0
    last = 0
    for r, b in zip(drv, drv[1:]):
        d = b['g'] - r['g']
        if abs(d) > 0.02:
            sgn = 1 if d > 0 else -1
            raw_rev += last and sgn != last
            last = sgn
    out['chatter']['raw_g_reversals_per_s_(moves>0.02)'] = round(raw_rev / max(out['driving_s'], 1), 2)
    # g<0.8 split by what drives s: the angle or the slip ratio of the front tyres, rear slip, throttle
    low = [r for r in drv if r['g'] < 0.8]
    if low:
        ratio_led = [r for r in low if r['tr'] > r['ta']]
        angle_low = [r for r in low if r['fa'] < 0.5 * a]
        over = [r for r in low if r['ra'] > 1.5 * max(r['fa'], 0.05) and r['ra'] > 0.5 * a]
        power = [r for r in low if r['throttle'] > 0.6]
        out['low_g_causes'] = {
            'events_g<0.8_samples': len(low),
            'ratio_term_larger_than_angle_term': round(len(ratio_led) / len(low), 3),
            'front_angle_below_A/2 (not understeering)': round(len(angle_low) / len(low), 3),
            'rear_angle_over_front_(oversteer-like)': round(len(over) / len(low), 3),
            'throttle>0.6': round(len(power) / len(low), 3)}
        for d in ('fwd', 'rwd', 'awd'):
            n = sum(1 for r in drv if r['drive'] == d)
            if n:
                m = [r for r in low if r['drive'] == d]
                rl = [r for r in m if r['tr'] > r['ta']]
                out.setdefault('by_drivetrain', {})[d] = {
                    'driving_share': round(n / len(drv), 3), 'g<0.8': round(len(m) / n, 4),
                    'of_those_ratio_led': round(len(rl) / max(len(m), 1), 3)}
    out['front_slip_ratio_under_power'] = {}
    for d in ('fwd', 'rwd', 'awd'):
        p = [r['fr'] for r in drv if r['drive'] == d and r['throttle'] > 0.8 and r['fa'] < 0.2 * a]
        if len(p) > 50:
            p50, p90, p99 = pct(p, (0.5, 0.9, 0.99))
            out['front_slip_ratio_under_power'][d] = {'n': len(p), 'p50': round(p50, 3), 'p90': round(p90, 3),
                                                      'p99': round(p99, 3), 'K_for_p99_at_s=1_would_be': round(p99, 3)}
    # pauses/menus and the air
    pauses, resume_g = 0, []
    for r, b in zip(rows, rows[1:]):
        if not r['run'] and b['run']:
            pauses += 1
            resume_g.append(b['eff'])
    out['pauses'] = {'menu_packets': sum(1 for r in rows if not r['run']), 'resumes': pauses,
                     'g_eff_at_resume_min': round(min(resume_g), 3) if resume_g else None}
    air = [r for r in drv if r['air']]
    out['air'] = {'samples': len(air), 'g_eff_min_in_air': round(min((r['eff'] for r in air), default=1.0), 3),
                  'g_below_0.8_in_air': round(sum(1 for r in air if r['g'] < 0.8) / max(len(air), 1), 3)}
    # grip saturation: |lateral g| against front slip angle (no slip ratio, steady-ish, off the air)
    sat = []
    for lo in [x * 0.1 for x in range(0, 20)]:
        hi = lo + 0.1
        b = [abs(r['ay']) for r in drv if lo <= r['fa'] < hi and r['fr'] < 0.05 and r['speed'] > 10 and not r['air']]
        if len(b) >= 30:
            p50, p90 = pct(b, (0.5, 0.9))
            sat.append({'front_angle': '%.1f-%.1f' % (lo, hi), 'n': len(b), 'lat_g_p50': round(p50, 2),
                        'lat_g_p90': round(p90, 2)})
    out['lat_g_vs_front_angle'] = sat
    return out


def fixture(items, path, half=15.0):
    """A small regression fixture (tests/test_ffb_addon_replay.py): decoded fields only, `half` s either side of
    the strongest front understeer (front slip over A, the rear well under it, no front slip ratio, cornering) and of the
    longest jump, and of a resume from a pause: [t, speed, 4 slip angles, 4 slip ratios, lateral accel, steer] per
    packet, [t] for a menu packet; windows one after the other with a 2 s gap."""
    picks = []
    best, best_i = 0.0, None
    air_runs, cur = [], []
    for i, (t, s) in enumerate(items):
        if s.car is None:
            continue
        ang, rat = s.slip_angle, s.slip_ratio
        fa_ = 0.5 * (abs(ang[0]) + abs(ang[1]))
        ra = 0.5 * (abs(ang[2]) + abs(ang[3]))
        fr = 0.5 * (abs(rat[0]) + abs(rat[1]))
        if s.speed > 15 and fr < 0.1 and ra < 0.5 * fa_ and s.accel[1] / G > 1.0 and 0.8 < fa_ < 1.5 and fa_ > best:
            best, best_i = fa_, i
        if s.susp_norm is not None and max(s.susp_norm) < 0.03:
            if cur and i - cur[-1] > 3:
                air_runs.append(cur)
                cur = []
            cur.append(i)
    if cur:
        air_runs.append(cur)
    if best_i is not None:
        picks.append(best_i)
    if air_runs:
        picks.append(max(air_runs, key=len)[0])
    for i in range(1, len(items)):
        if items[i - 1][1].car is None and items[i][1].car is not None and items[i][1].speed > 15:
            picks.append(i)
            break
    out, base = [], 0.0
    for i in picks:
        t0 = items[i][0]
        lo = hi = i
        half_w = half if i != picks[-1] else 5.0
        while lo > 0 and items[lo - 1][0] > t0 - half_w:
            lo -= 1
        while hi < len(items) - 1 and items[hi + 1][0] < t0 + half_w:
            hi += 1
        first = items[lo][0]
        for t, s in items[lo:hi + 1]:
            at = round(base + t - first, 4)
            if s.car is None:
                out.append([at])
            else:
                out.append([at, round(s.speed, 2)] + [round(v, 4) for v in s.slip_angle] +
                           [round(v, 4) for v in s.slip_ratio] + [round(s.accel[1], 3), round(s.steer, 3)])
        base += items[hi][0] - first + 2.0
    import gzip
    with gzip.open(path, 'wt') as f:
        json.dump({'note': 'decoded Forza Horizon 324-byte fields, windows around the strongest understeer, '
                           'the longest jump and a resume from a pause (scripts/ffb-addon-replay.py --fixture): '
                           't speed slip_angle[4] slip_ratio[4] lateral_accel steer',
                   'rows': out}, f, separators=(',', ':'))
    return len(out), picks


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('capture')
    p.add_argument('--wheel', help="sidecar (default: CAPTURE.wheel.gz if there)")
    p.add_argument('--force', choices=('constant', 'aligning'), default='aligning')
    p.add_argument('--pass-hz', type=float, default=0.0)
    p.add_argument('--strength', type=float, default=fa.STRENGTH)
    p.add_argument('--a', type=float, default=fa.A)
    p.add_argument('--k', type=float, default=fa.K)
    p.add_argument('--fixture', help="write a small regression fixture from the capture and stop")
    p.add_argument('--sweep', action='store_true', help="a table of A and K choices instead of the full report")
    args = p.parse_args()
    wheel = args.wheel or (args.capture + '.wheel.gz' if os.path.exists(args.capture + '.wheel.gz') else None)
    steering = load_steering(wheel) if wheel else ([], [])
    items = load(args.capture)
    # what-if constants reach lightness() through its defaults
    fa.lightness.__defaults__ = (args.a, args.k, fa.Q)
    if args.fixture:
        print('%d rows, windows at packets %s' % fixture(items, args.fixture))
        return
    if args.sweep:
        print('A     K     g<0.95  g<0.8  g<0.6(raw)  reversals/s')
        for a in (0.5, 0.6, 0.7, 0.8, 1.0):
            for k in (0.35, 0.5, 0.7, 1.0, 1.5):
                fa.lightness.__defaults__ = (a, k, fa.Q)
                res = analyse(replay(items, steering, args.force, args.pass_hz, args.strength, a, k), a, k)
                sh = res['share_raw_g']
                print('%.2f  %.2f  %.3f   %.3f  %.3f       %.2f' % (a, k, sh['0.95'], sh['0.8'], sh['0.6'],
                                                                   res['chatter']['reversals_per_s']))
        return
    rows = replay(items, steering, args.force, args.pass_hz, args.strength, args.a, args.k)
    result = analyse(rows, args.a, args.k)
    result.update(file=args.capture, force=args.force, A=args.a, K=args.k, strength=args.strength)
    if rows:
        result['ready_at_end'] = rows[-1]['ready']
        result['ready_share'] = round(sum(1 for r in rows if r['ready']) / len(rows), 3)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
