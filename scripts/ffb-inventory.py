#!/usr/bin/env python3
"""Force inventory: every force the played wheel could carry, its origin, whether it is in each game's data and in each game's
force, and how strong it is (rms Nm at the hands on the 279 mm rim, Nm per unit of its cause). Uses ffb-components' extract and
joint fit unchanged (speed over 12 m/s, 60 Hz grid, force = recorded share x 3.08 Nm).

    scripts/ffb-inventory.py CAPTURE [CAPTURE ...] [--doc PATH]

"in force" = the joint fit gives the cause a coefficient whose 1-6 Hz contribution is over 0.02 Nm. A force whose channel the game
does not send is "missing in data"; one that is sent but contributes less is "in data, not in force": both are findings.
Writes docs/ffb-force-inventory.md (or --doc) and ~/.cache/ffb-inventory/latest.log. Reads only the captures.
"""
import importlib.util
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-inventory/latest.log')
DOC = os.path.join(HERE, '..', 'docs', 'ffb-force-inventory.md')
PRESENT_NM = 0.02
MID, LOW, HIGH = 1, 0, 2                      # indexes into tools.BANDS

# (label, cause in the fit or None, origin, unit of the cause)
FORCES = (
    ('lateral (Fy x caster + pneumatic)', 'ay', 'tyre side force at the front axle times caster and pneumatic trail; seen as lateral acceleration', 'Nm per m/s2'),
    ('yaw acceleration', 'yawacc', 'inertia of the front tyres and rack as the body rotates; seen as d(yaw rate)/dt', 'Nm per rad/s2'),
    ('front load (weight transfer, bumps)', 'load_sum', 'vertical load on the front tyres: scales every tyre force, bumps and dives move it', 'Nm per N'),
    ('longitudinal front Fx (brake and drive pull, scrub)', 'fx_diff', 'left-right difference of the front tyres\' longitudinal force acting on the scrub radius', 'Nm per N'),
    ('front Mz (self-aligning torque)', 'mz_front', 'tyre aligning moment of the front axle, as the game gives it', 'Nm per Nm'),
    ('steer rate (damping)', 'steerrate', 'friction and damping of the rack and tyres against the wheel\'s motion', 'Nm per unit/s'),
    ('throttle (as a cause)', 'throttle', 'drive torque through the driven axle; on the front only for a front or four wheel drive', 'Nm per unit'),
    ('brake (as a cause)', 'brake', 'braking torque and the dive it causes', 'Nm per unit'),
    ('texture / vibration 6-30 Hz', None, 'road surface, kerbs, tyre and engine vibration; whatever the game puts above 6 Hz', 'Nm rms'),
)


def _load(file):
    spec = importlib.util.spec_from_file_location(file.replace('-', '_').replace('.py', ''), os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def analyse(game, g, missing):
    """{'game', 'n', 'rms' (force rms), 'rows': {cause: dict}, 'texture': rms 6-30 Hz, 'settle': dict}. Uses the same
    joint fit as ffb-components.report, per band, over the same samples."""
    comp = _load('ffb-components.py')
    ok = g['ok']
    names = ['ay', 'yawacc', 'brake', 'throttle', 'steerrate'] + [n for n in ('fx_diff', 'load_sum', 'mz_front') if n in g]
    bands = []
    for _bn, short, long in tools.BANDS:
        y = tools.band_of(g['F'], short, long)[ok]
        causes = {n: tools.band_of(g[n], short, long)[ok] for n in names}
        bands.append((np.std(y), tools.joint_fit(y, causes)[0]))
    rows = {}
    for n in ('ay', 'yawacc', 'brake', 'throttle', 'steerrate', 'fx_diff', 'load_sum', 'mz_front'):
        if n not in g:
            rows[n] = {'data': False}
            continue
        low, mid = bands[LOW][1][n], bands[MID][1][n]
        rows[n] = {'data': True, 'coef_low': low[0], 'rms_low': low[1], 'coef': mid[0], 'rms': mid[1],
                   'present': mid[1] > PRESENT_NM}
    events = comp.settle_events(g, held=0.20)
    f = events['force']
    settle = {'n': events['n']}
    if events['n']:
        d = lambda h: float(np.median(np.array(f[h]) - np.array(f[0.0])))        # noqa: E731
        settle.update(d3=d(0.3), d5=d(0.5), opposite=float(np.mean(np.array(f[0.3]) < 0)))
    return {'game': game, 'n': int(ok.sum()), 'rms': float(np.std(g['F'][ok])), 'rows': rows, 'texture': float(bands[HIGH][0]),
            'settle': settle, 'strict_settles': comp.settle_events(g)['n']}


def status(row):
    if not row['data']:
        return 'MISSING IN DATA'
    return 'in force' if row['present'] else 'IN DATA, NOT IN FORCE'


def cell(res, key):
    """One table cell: status, rms Nm (0-1 / 1-6 Hz) and the coefficient."""
    if key is None:
        t = res['texture']
        return ('in force' if t > PRESENT_NM else 'IN DATA, NOT IN FORCE') + ', rms %.3f Nm' % t
    r = res['rows'][key]
    if not r['data']:
        return 'MISSING IN DATA'
    return '%s; rms %.3f / %.3f Nm (0-1 / 1-6 Hz); %+.4g / %+.4g per unit' % (status(r), r['rms_low'], r['rms'], r['coef_low'], r['coef'])


def table(results):
    """results: [(name, analyse() dict)] -> Markdown lines."""
    head = '| Force | Origin | Unit | ' + ' | '.join('%s (%s)' % (n, r['game']) for n, r in results) + ' |'
    lines = [head, '|' + '---|' * (3 + len(results))]
    for label, key, origin, unit in FORCES:
        lines.append('| %s | %s | %s | %s |' % (label, origin, unit, ' | '.join(cell(r, key) for _n, r in results)))
    cells = []
    for _n, r in results:
        s = r['settle']
        cells.append('%d held settles at <=20 %%/s (%d at <=2 %%/s)%s' % (s['n'], r['strict_settles'], (
            '; force %+.2f Nm by +0.3 s, %+.2f Nm by +0.5 s, opposite pull in %.0f %%' % (s['d3'], s['d5'], 100 * s['opposite'])
            if s['n'] else '')))
    lines.append('| held-wheel settle (opposite pull as the car settles) | ffb-components --settle: wheel held, yaw rate peaks, rear unloads | Nm vs the peak | '
                 + ' | '.join(cells) + ' |')
    return lines


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print(__doc__)
        return 0 if args else 2
    doc = DOC
    if '--doc' in args:
        i = args.index('--doc')
        doc = args[i + 1]
        del args[i:i + 2]
    comp = _load('ffb-components.py')
    results = []
    for name in args:
        path = tools.capture_path(name)
        got = comp.extract(path) if os.path.exists(path) else None
        if got:
            results.append((os.path.basename(path).replace('.ovcap.gz', ''), analyse(*got)))
        else:
            print('%s: no capture, or no recorded force' % name)
    if not results:
        return 1
    body = table(results)
    text = '\n'.join(['Force inventory (rms Nm at the hands, 279 mm rim; speed over 12 m/s; "in force" = 1-6 Hz contribution over %.2f Nm)' %
                      PRESENT_NM] + ['%s: %d samples, force rms %.2f Nm' % (n, r['n'], r['rms']) for n, r in results] + [''] + body)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    with open(doc, 'w') as f:
        f.write('# Force inventory\n\nGenerated by `scripts/ffb-inventory.py ' + ' '.join(args) + '`. Rms is Nm at the hands on the 279 mm rim '
                '(recorded share x 3.08), speed over 12 m/s. "in force" means the joint fit of ffb-components gives the cause a coefficient whose '
                '1-6 Hz contribution is over %.2f Nm. MISSING IN DATA: the game sends no such channel. IN DATA, NOT IN FORCE: sent, but the played '
                'force does not follow it.\n\n' % PRESENT_NM + '\n'.join(body) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
