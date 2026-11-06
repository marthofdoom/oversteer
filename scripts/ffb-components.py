#!/usr/bin/env python3
"""What the played force is made of: per band (0-1, 1-6, 6-30 Hz) a joint least-squares fit of the force on its causes (ay, yaw
acceleration, brake, throttle, |steer rate| and, where the capture has them, front Fx difference, front load sum, front Mz),
an errors-in-variables check on yaw acceleration, and the settle-after-a-turn events (does the force flip sign?).

    scripts/ffb-components.py CAPTURE [CAPTURE ...]

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


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0 if args else 2
    out = []
    for name in args:
        path = tools.capture_path(name)
        got = extract(path) if os.path.exists(path) else None
        out += report(os.path.basename(path).replace('.ovcap.gz', ''), *got) if got else ['%s: no capture, or no recorded force' % name]
    text = '\n'.join(out)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
