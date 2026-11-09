#!/usr/bin/env python3
"""What the played force is made of: per band (0-1, 1-6, 6-30 Hz) a joint least-squares fit of the force on its causes (ay, yaw
acceleration, brake, throttle, |steer rate| and, where the capture has them, front Fx difference, front load sum, front Mz),
an errors-in-variables check on yaw acceleration, and the settle-after-a-turn events (does the force flip sign?).

    scripts/ffb-components.py [--settle] CAPTURE [CAPTURE ...]

--settle: only the held-wheel settle (stacked on the yaw rate's peak: is there an opposite pull as the car settles?).

CAPTURE: a name in the captures folder or a full path. Speed over 12 m/s, a 60 Hz grid, force in Nm at the hands (recorded
share x 3.08). Reads only. Full log: ~/.cache/ffb-components/latest.log
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-components/latest.log')
MIN_SPEED = 12.0
SETTLE_TIMES = (-0.2, 0.0, 0.1, 0.2, 0.3, 0.5, 0.8)      # s from the yaw rate's peak
HELD_RATE = 0.02                                         # |steer rate| limit, full lock (steer = +-1) per second
HELD_LOOSER = (0.05, 0.10, 0.20, 0.30)                              # shown too: how the answer moves when the wheel is held less strictly


def _pair(v, f):
    try:
        return f(v) if v is not None and len(v) >= 2 else math.nan
    except TypeError:
        return math.nan


def extract(path):
    """(game, grid dict of arrays incl. 'F' and 'ok', missing names) for a capture; None when it has no usable force."""
    import ffb_benchmark as fb
    try:
        import ffb_parity as fp
        game = fp.game_of(path)
    except Exception:
        game = None
    _raw, played, _h = fb._levels(path)
    if played is None:
        return None
    rows = []
    _meta, gen = fb._walk(path)
    with fb.quiet():
        for t, s in gen:
            if (s.speed or 0) > MIN_SPEED and s.accel and s.yaw_rate is not None:
                tyre = lambda a, f: _pair(getattr(s, a, None), f)           # noqa: E731
                rows.append((t, s.accel[1], s.yaw_rate, s.brake or 0.0, s.throttle or 0.0, s.steer or 0.0, s.speed,
                             tyre('tyre_fx', lambda v: v[1] - v[0]), tyre('tyre_load', lambda v: v[0] + v[1]),
                             tyre('tyre_mz', lambda v: v[0] + v[1])))
    if len(rows) < 200:
        return None
    a = np.array(rows, float)
    a = a[np.r_[True, np.diff(a[:, 0]) > 0]]
    t = a[:, 0]
    grid = np.arange(t[0], t[-1], 1 / tools.GRID_HZ)
    g = {k: np.interp(grid, t, a[:, i]) for i, k in enumerate(('ay', 'yaw', 'brake', 'throttle', 'steer', 'speed'), 1)}
    g['t'] = grid
    g['F'] = np.interp(grid, played[:, 0], played[:, 1]) / fb.FULL * tools.DEFAULT_PEAK
    near = np.searchsorted(t, grid).clip(0, len(t) - 1)
    g['ok'] = (np.abs(t[near] - grid) < 0.05) & (g['speed'] > MIN_SPEED) & (grid >= played[0, 0]) & (grid <= played[-1, 0])
    g['yawacc'] = np.gradient(tools.moving_mean(g['yaw'], 5)) * tools.GRID_HZ
    g['steerrate'] = np.abs(np.gradient(g['steer']) * tools.GRID_HZ)
    missing = []
    for name, col in (('fx_diff', 7), ('load_sum', 8), ('mz_front', 9)):
        v = a[:, col]
        if np.isfinite(v).mean() > 0.5:
            g[name] = np.interp(grid, t, np.nan_to_num(v))
        else:
            missing.append(name)
    return game, g, missing


def report(name, game, g, missing):
    ok = g['ok']
    names = ['ay', 'yawacc', 'brake', 'throttle', 'steerrate'] + [n for n in ('fx_diff', 'load_sum', 'mz_front') if n in g]
    lines = ['%s  game %s  %d samples at >%.0f m/s, force rms %.2f Nm  missing: %s' % (
        name, game, ok.sum(), MIN_SPEED, np.std(g['F'][ok]), ', '.join(missing) or 'none')]
    smooth = tools.moving_mean(g['yawacc'], 6)
    for bn, short, long in tools.BANDS:
        y = tools.band_of(g['F'], short, long)[ok]
        causes = {n: tools.band_of(g[n], short, long)[ok] for n in names}
        fit, full = tools.joint_fit(y, causes)
        lines.append('band %s  force rms %.3f Nm  r2 %.2f' % (bn, np.std(y), full))
        for n in names:
            c, rms, dr2, inrms = fit[n]
            lines.append('  %-9s %+9.4f Nm/unit  rms %.3f Nm  dr2 %.3f  input rms %.3g' % (n, c, rms, dr2, inrms))
        alt = dict(causes)
        alt['yawacc'] = tools.band_of(smooth, short, long)[ok]
        c2 = tools.joint_fit(y, alt)[0]['yawacc'][0]
        c1 = fit['yawacc'][0]
        if abs(c2 - c1) > 0.3 * abs(c1):
            lines.append('  yawacc with 100 ms smoothing: %+.4f vs %+.4f Nm/unit: noisy input' % (c2, c1))
    for n in ('fx_diff', 'load_sum', 'mz_front'):
        if n in missing:
            lines.append('  %s: missing' % n)
    cnt, ch, fl = tools.yaw_events(g['F'], g['yaw'], g['ay'])
    hs = list(ch)
    lines.append('yaw settle events %d; median change (Nm, + = same sense as the turn force) at %s s:' % (
        cnt, '/'.join('%.1f' % h for h in hs)))
    lines.append('  ' + '  '.join('%+.3f' % ch[h] for h in hs) + '   sign flips (force then opposite to the turn): ' +
                 '  '.join('%.0f%%' % (100 * fl[h]) for h in hs))
    return lines


def settle_events(g, min_yaw=0.3, min_force=0.2, held=HELD_RATE, times=SETTLE_TIMES, hz=tools.GRID_HZ):
    """Held-wheel settles: at each yaw-rate peak (over min_yaw, the largest within 0.5 s) where |steer rate| stays under
    `held` from 0.3 s before to 0.8 s after, |ay| is lower 0.8 s after than at the peak and never rises above it, and the
    force before the peak (-0.3..-0.1 s) is over min_force. Returns {'n', 'force', 'yawacc', 'ay'}: per time offset the list
    of values, force signed relative to the turn's force, yaw acceleration relative to the yaw sign, |ay|."""
    yaw, F, ay, ok = (np.asarray(g[k], float) for k in ('yaw', 'F', 'ay', 'ok'))
    ys, Fs, ays = tools.moving_mean(yaw, 3), tools.moving_mean(F, 7), np.abs(tools.moving_mean(ay, 3))
    acc = tools.moving_mean(g['yawacc'], 3)
    # steer is quantised (FH6: 1/127 steps at ~30 Hz), so its per-sample rate never rests: rate = change of the 0.2 s mean over 0.27 s
    sm = tools.moving_mean(g['steer'], 12)
    rate = np.abs(np.roll(sm, -8) - np.roll(sm, 8)) * hz / 16.0
    pre, post, w, n = int(round(0.3 * hz)), int(round(0.8 * hz)), int(round(0.5 * hz)), len(F)
    out = {'n': 0, 'force': {h: [] for h in times}, 'yawacc': {h: [] for h in times}, 'ay': {h: [] for h in times}}
    i = max(pre, w)
    while i < n - post - 1:
        s = 1.0 if ys[i] > 0 else -1.0
        if s * ys[i] > min_yaw and s * ys[i] >= np.max(s * ys[i - w:i + w + 1]) and ok[i - pre:i + post + 1].all():
            f0 = float(np.median(Fs[i - pre:i - int(round(0.1 * hz)) + 1]))
            if (abs(f0) >= min_force and rate[i - pre:i + post + 1].max() < held and
                    ays[i + post] < ays[i] and ays[i:i + post + 1].max() <= 1.05 * ays[i]):
                sf = 1.0 if f0 > 0 else -1.0
                out['n'] += 1
                for h in times:
                    k = i + int(round(h * hz))
                    out['force'][h].append(sf * Fs[k])
                    out['yawacc'][h].append(s * acc[k])
                    out['ay'][h].append(ays[k])
                i += post
                continue
        i += 1
    return out


def settle_lines(name, game, g):
    lines = []
    for held in (HELD_RATE,) + HELD_LOOSER:
        lines += _settle_block(name, game, settle_events(g, held=held), held)
    return lines


def _settle_block(name, game, ev, held):
    lines = ['%s  game %s  held-wheel settles %d (|steer rate| < %.0f%% of full lock/s from -0.3 to +0.8 s, |ay| falling, over %.0f m/s)' % (
        name, game, ev['n'], 100 * held, MIN_SPEED)]
    if not ev['n']:
        return lines + ['  none']
    hs = SETTLE_TIMES
    lines.append('  time s            ' + ''.join('%9.1f' % h for h in hs))
    q = lambda v: '%+5.2f(%+.2f..%+.2f)' % (np.median(v), *np.percentile(v, (25, 75)))      # noqa: E731
    lines.append('  force Nm (median, 25-75 %), + = the turn\'s sense')
    lines.append('    ' + '  '.join(q(ev['force'][h]) for h in hs))
    lines.append('  yaw acceleration rad/s2, + = the turn\'s sense: ' + '  '.join('%+.2f' % np.median(ev['yawacc'][h]) for h in hs))
    lines.append('  |ay| m/s2: ' + '  '.join('%.2f' % np.median(ev['ay'][h]) for h in hs))
    late = np.array(ev['force'][0.3]) < 0
    lines.append('  opposite pull at +0.3 s (force sign against the turn): %.0f%% of settles; median change 0 -> +0.3 s %+.2f Nm, 0 -> +0.5 s %+.2f Nm' % (
        100 * late.mean(), np.median(np.array(ev['force'][0.3]) - np.array(ev['force'][0.0])),
        np.median(np.array(ev['force'][0.5]) - np.array(ev['force'][0.0]))))
    return lines


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0 if args else 2
    settle = '--settle' in args
    args = [a for a in args if a != '--settle']
    out = []
    for name in args:
        path = tools.capture_path(name)
        got = extract(path) if os.path.exists(path) else None
        if settle and got:
            out += settle_lines(os.path.basename(path).replace('.ovcap.gz', ''), got[0], got[1])
            continue
        out += report(os.path.basename(path).replace('.ovcap.gz', ''), *got) if got else ['%s: no capture, or no recorded force' % name]
    text = '\n'.join(out)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
