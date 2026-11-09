#!/usr/bin/env python3
"""Each game's own grip and collapse from its recordings: per rolling 60 s stretch (speed over 8 m/s) the upper envelope
(p80 in slip bins) of |ay|/g against the front slip: initial slope C, top mu, and peak slip (the first bin reaching 97 % of
the top). Stretches whose top bin holds under 1 s or whose envelope still rises over 3 % into the last bin are 'limit not
reached' and left out of the peak statistics. Per mu band (<.7, .7-1, 1-1.3, >1.3): stretches, median C, mu, peak slip.

    scripts/ffb-tyre-fit.py CAPTURE [CAPTURE ...] [--empty-store] [--store PATH]

Slip: ACR mean |tyre_slip_angle| of the fronts (rad); RBR |RbrRack._slip| (kinematic, rad); Forza mean |slip_angle| and,
separately, mean |combined_slip| of the fronts (normalised). ACR is validated against the tyres' own sum|Fy| / sum Fz and
its Mz / (mu Fz) and |Mz| / |Fy| are shown against slip / peak. RbrRack.DERIVED_SLIP_PEAK and Forza's theta scale are only
printed for comparison. Reads only (the RBR slip runs on a COPY of the live car-property store; --empty-store: none; --store PATH: that file). Full log: ~/.cache/ffb-tyre-fit/latest.log
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-tyre-fit/latest.log')
MIN_SPEED = 8.0
LAST_CAR = [None]                      # the car of the capture rows() last read
STRETCH_S = 60.0
MIN_STRETCH_S = 15.0
TBINS = [0, .2, .4, .6, .8, 1, 1.5, 1e9]
TNAMES = ['0-.2', '.2-.4', '.4-.6', '.6-.8', '.8-1', '1-1.5', '1.5+']


def _mean_abs(v, n=2):
    if v is None or len(v) < n:
        return np.nan
    try:
        return float(np.mean([abs(float(a)) for a in v[:n]]))
    except (TypeError, ValueError):
        return np.nan


def _all4(v):
    return v is not None and len(v) >= 4 and all(a is not None and np.isfinite(a) for a in v[:4])


def rows(path, game):
    """{'t','ay', slip series..., (ACR) 'mu_true', 'mz', 'fy', 'fz'} arrays over the samples above MIN_SPEED."""
    import ffb_benchmark as fb
    _meta, gen = fb._walk(path)
    rack = None
    if game == 'rbr':
        from oversteer import ffb_engine as fe
        rack = fe.RbrRack()
    keys = ('t', 'ay', 'slip', 'slip2', 'mu_true', 'mz', 'fy', 'fz')
    out = {k: [] for k in keys}
    for t, s in gen:
        LAST_CAR[0] = s.car or LAST_CAR[0]
        if (s.speed or 0) < MIN_SPEED or not s.accel or len(s.accel) < 2 or s.accel[1] is None:
            continue
        slip2 = np.nan
        mu_true = np.nan
        mz = fy = fz = (np.nan, np.nan)
        if game == 'acr':
            slip = _mean_abs(s.tyre_slip_angle)
            if _all4(s.tyre_fy) and _all4(s.tyre_load) and sum(s.tyre_load[:4]) > 0:
                mu_true = abs(sum(s.tyre_fy[:4])) / sum(s.tyre_load[:4])
            if s.tyre_mz is not None and _all4(s.tyre_fy) and _all4(s.tyre_load) and len(s.tyre_mz) >= 2:
                mz, fy, fz = tuple(abs(a) for a in s.tyre_mz[:2]), tuple(abs(a) for a in s.tyre_fy[:2]), tuple(s.tyre_load[:2])
        elif game == 'rbr':
            rack.set_car(s.car)
            a = rack._slip(s)
            slip = abs(a) if a is not None and np.isfinite(a) else np.nan
        else:
            slip, slip2 = _mean_abs(s.slip_angle), _mean_abs(s.combined_slip)
        for k, v in zip(keys, (t, abs(s.accel[1]) / tools.G, slip, slip2, mu_true, mz, fy, fz)):
            out[k].append(v)
    return {k: np.array(v, float) for k, v in out.items()}


def stretches(r):
    sg = ((r['t'] - r['t'][0]) // STRETCH_S).astype(int)
    for g in np.unique(sg):
        yield np.nonzero(sg == g)[0]


def fit_all(r, dt, key, bins):
    fits = []
    for ix in stretches(r):
        if len(ix) * dt < MIN_STRETCH_S:
            continue
        f = tools.envelope_fit(r[key][ix], r['ay'][ix], dt, bins)
        if f:
            f['ix'] = ix
            fits.append(f)
    return fits


def band_table(label, fits):
    lines = ['  %s: %d stretches, %d limit not reached' % (label, len(fits), sum(not f['reached'] for f in fits)),
             '    band      stretches  median C   mu     peak slip']
    for b, nm in enumerate(tools.MU_BAND_NAMES):
        sel = [f for f in fits if tools.mu_band(f['mu']) == b]
        pk = [f['peak'] for f in sel if f['reached']]
        if sel:
            lines.append('    %-8s %6d %10.2f %7.2f %9s' % (nm, len(sel), np.median([f['C'] for f in sel]),
                         np.median([f['mu'] for f in sel]), ('%.3f (%d)' % (np.median(pk), len(pk))) if pk else '-'))
    pk = [f['peak'] for f in fits if f['reached']]
    lines.append('    median peak slip over all reached stretches: %s' % (('%.3f' % np.median(pk)) if pk else 'none'))
    return lines


def acr_extra(r, fits, dt, bins):
    lines = ['  validation: mu from |ay|/g over mu from sum|Fy|/sum Fz (same stretches), median ratio per band']
    for b, nm in enumerate(tools.MU_BAND_NAMES):
        ratios = []
        for f in fits:
            if tools.mu_band(f['mu']) != b:
                continue
            g = tools.envelope_fit(r['slip'][f['ix']], r['mu_true'][f['ix']], dt, bins)
            if g and g['mu'] > 0:
                ratios.append(f['mu'] / g['mu'])
        if ratios:
            lines.append('    %-8s %d stretches, ratio %.2f' % (nm, len(ratios), np.median(ratios)))
    cols = [[] for _ in TNAMES]
    for f in fits:
        if not f['reached']:
            continue
        ix = f['ix']
        mz = np.concatenate([_w(r, 'mz', ix, w) for w in (0, 1)])
        fy = np.concatenate([_w(r, 'fy', ix, w) for w in (0, 1)])
        fz = np.concatenate([_w(r, 'fz', ix, w) for w in (0, 1)])
        x = np.concatenate([r['slip'][ix]] * 2) / f['peak']
        ok = np.isfinite(mz) & np.isfinite(fy) & (fz > 800) & (fy > 200)
        for i in range(len(TNAMES)):
            m = ok & (x >= TBINS[i]) & (x < TBINS[i + 1])
            if m.any():
                cols[i].append(np.c_[mz[m] / (f['mu'] * fz[m]), mz[m] / fy[m]])
    lines.append('  torque shape (fronts, reached stretches): slip/peak   n   Mz/(mu Fz)   |Mz|/|Fy|')
    med = []
    for i, nm in enumerate(TNAMES):
        if cols[i]:
            z = np.vstack(cols[i])
            med.append(np.median(z[:, 0]))
            lines.append('    %-6s %8d %10.4f %10.4f' % (nm, len(z), med[-1], np.median(z[:, 1])))
        else:
            med.append(-1)
    if max(med) > 0:
        lines.append('  Mz/(mu Fz) peaks at slip/peak %s' % TNAMES[int(np.argmax(med))])
    return lines


def _w(r, key, ix, w):
    return r[key][ix][:, w]


def report(name, game, r):
    n = len(r['t'])
    if n < 100:
        return ['%s: game %s, too few samples over %.0f m/s (%d)' % (name, game, MIN_SPEED, n)]
    dt = tools.sample_spacing(r['t'])
    lines = ['%s  game %s  %d samples, spacing %.1f ms' % (name, game, n, dt * 1e3)]
    kind = 'rad' if game in ('acr', 'rbr') else 'norm'
    bins = tools.TYRE_BINS[kind]
    variants = [('slip', {'acr': 'mean |tyre_slip_angle| fronts (rad)', 'rbr': '|RbrRack._slip| (rad)',
                          'forza-fh': 'mean |slip_angle| fronts (normalised)'}.get(game, game))]
    if game == 'forza-fh':
        variants.append(('slip2', 'mean |combined_slip| fronts (normalised)'))
    for key, label in variants:
        fits = fit_all(r, dt, key, bins)
        lines += band_table(label, fits) if fits else ['  %s: no usable stretch' % label]
        if game == 'acr' and fits:
            lines += acr_extra(r, fits, dt, bins)
        pk = [f['peak'] for f in fits if f['reached']]
        if pk and game == 'rbr':
            lines.append('  compare RbrRack.DERIVED_SLIP_PEAK 0.15: median peak %.3f' % np.median(pk))
        if pk and game == 'forza-fh':
            lines.append('  compare Forza theta scale 0.7: median peak %.3f (%s)' % (np.median(pk), key))
    return lines


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0 if args else 2
    import ffb_parity as fp
    import ffb_score
    flags = [a for a in args if a.startswith('--')]
    store = None
    if '--store' in args:
        i = args.index('--store')
        store = args[i + 1] if i + 1 < len(args) else None
        flags = [a for a in flags if a != '--store']
        args = args[:i] + args[i + 2:]
    args = [a for a in args if a != '--empty-store']
    try:
        tools.apply_store_args(type('A', (), {'empty_store': '--empty-store' in flags, 'store': store})())
    except ValueError as e:
        print('ffb-tyre-fit: %s' % e, file=sys.stderr)
        return 2
    out = [ffb_score.seed_path()[1]]
    with ffb_score._sandbox():
        for name in args:
            path = tools.capture_path(name)
            if not os.path.exists(path):
                out.append('%s: no capture' % name)
                continue
            game = fp.game_of(path)
            base = os.path.basename(path).replace('.ovcap.gz', '')
            if game not in ('acr', 'rbr', 'forza-fh'):
                out.append('%s: game %s not handled' % (base, game))
                continue
            r = rows(path, game)
            out += ['%s: car %s; %s' % (base, LAST_CAR[0], tools.props_line(LAST_CAR[0]))]
            out += report(base, game, r)
    text = '\n'.join(out)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
