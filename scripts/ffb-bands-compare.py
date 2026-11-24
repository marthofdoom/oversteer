#!/usr/bin/env python3
"""The same captures through two versions of the force code, by the front axle's grip in use: how far a change to the trail law moves the played force.

    scripts/ffb-bands-compare.py CAPTURE [CAPTURE ...] [--old REF] [--new REF]

OLD is a git ref (default master) checked out into a temporary detached worktree; NEW is this tree (default) or another ref. Each
side's label is read from git (the commit, and "+uncommitted" for a dirty tree), never typed, so the two cannot be swapped. Per capture and
band of the grip in use u (u < .5, .5-.8, .8-.9, .9-1, and past the peak, x = slip / peak slip > 1; speed over 8 m/s) the median rim
torque, pneumatic+caster trail (kingpin / Fy_f) and played Nm of today's rack for the capture's game (RBR, FH6), and the change.
Log: ~/.cache/ffb-bands-compare/latest.log. Reads only (the car properties start from a copy of the live store, as ffb-stages)."""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOG = os.path.expanduser('~/.cache/ffb-bands-compare/latest.log')
BANDS = (('u<.5', lambda u, x: u < .5), ('.5-.8', lambda u, x: (u >= .5) & (u < .8)), ('.8-.9', lambda u, x: (u >= .8) & (u < .9)),
         ('.9-1', lambda u, x: (u >= .9) & (u < 1) & (x <= 1)), ('past peak', lambda u, x: x > 1))
KEYS = ('rim', 'trail_mm', 'played')


def band_rows(cols):
    """[(band, samples, {key: median})] of the stage columns (`u`, `x`, `fyf`, `speed` and KEYS) by grip-use band."""
    out = []
    ok = (np.asarray(cols['fyf']) > 0) & np.isfinite(cols['u']) & (np.asarray(cols['speed']) >= 8)
    for label, f in BANDS:
        with np.errstate(invalid='ignore'):
            m = ok & f(np.asarray(cols['u']), np.asarray(cols['x']))
        out.append((label, int(m.sum()), {k: (float(np.nanmedian(np.asarray(cols[k])[m])) if m.any() else None) for k in KEYS}))
    return out


def label(root):
    """The git commit of the tree at `root`, with +uncommitted when its files differ from it."""
    def git(*a):
        return subprocess.run(('git', '-C', root) + a, capture_output=True, text=True, check=True).stdout.strip()
    return git('rev-parse', '--short', 'HEAD') + ('+uncommitted' if git('status', '--porcelain', '--untracked-files=no') else '')


def measure(root, names):
    """{capture: [(band, n, medians)]} of today's rack of the code under `root` (this runs inside a subprocess on that root)."""
    sys.path.insert(0, os.path.join(root, 'scripts'))
    sys.path.insert(0, root)
    import ffb_benchmark as fb
    import ffb_parity as fp
    import ffb_score as fs
    import ffb_tools as tools
    from oversteer import ffb_forza

    def probe(rack, s):
        nan = float('nan')
        if hasattr(rack, '_grip_r'):                       # RBR
            a = tools._slip_rbr(s, rack)
            mu = rack._mu_work() if hasattr(rack, '_mu_work') else rack.grip_mu          # (the grip u is read over: older code reads grip_mu)
            return {'u': rack._grip_r / mu if mu else nan, 'x': a / rack.slip_peak if a is not None else nan, 'mu': rack.grip_mu}
        mu = rack._limit_grip.mu()                         # Forza
        a = tools._slip_forza(s, rack)
        return {'u': nan, 'x': a / ffb_forza.FORZA_SLIP_PEAK if a is not None else nan, 'mu': mu if mu else nan}
    out = {}
    with fs._sandbox():
        for name in names:
            path = tools.capture_path(name)
            game = fp.game_of(path)
            c = tools.simulate_stages(list(fb._walk(path)[1]), game, tools.profile_power_class(game), probe=(('u', 'x', 'mu'), probe))['cols']
            if game.startswith('forza'):
                c['u'] = c['fyf'] / (c['mass'] * c['bl'] * ffb_forza.G) / c['mu']
            out[name] = band_rows(c)
    return out


def _run(root, names):
    p = subprocess.run([sys.executable, os.path.join(HERE, 'ffb-bands-compare.py'), '--measure', root] + list(names), capture_output=True,
                       text=True, cwd=root, env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
    if p.returncode:
        sys.exit('measuring %s failed:\n%s' % (root, p.stderr[-2000:]))
    return json.loads(p.stdout.splitlines()[-1])


def _tree(ref, tmp, side):
    """(root, label): this tree for ref None, else a detached worktree of `ref` in `tmp` (one directory per side, so both sides may name the
    same ref); the worktree is removed again when anything after adding it fails."""
    if ref is None:
        return REPO, label(REPO)
    root = os.path.join(tmp, side)
    subprocess.run(['git', '-C', REPO, 'worktree', 'add', '--detach', '-q', root, ref], check=True)
    try:
        return root, label(root)
    except BaseException:
        _drop(root)
        raise


def _drop(root):
    subprocess.run(['git', '-C', REPO, 'worktree', 'remove', '--force', root], capture_output=True)


def table(names, old, new, lab_old, lab_new):
    """Lines: per capture, per band, the median of each key under OLD and NEW and NEW / OLD."""
    lines = ['OLD = %s   NEW = %s' % (lab_old, lab_new)]
    for name in names:
        lines += ['', name, '  band       |   n(old)  n(new) | rim Nm old -> new (ratio) | trail mm old -> new | played Nm old -> new']
        for (b, n0, a), (_b, n1, c) in zip(old[name], new[name]):
            def f(k, ratio=False):
                x, y = a[k], c[k]
                if x is None or y is None:
                    return '-'
                return '%.2f -> %.2f%s' % (x, y, ' (%.2f)' % (y / x) if ratio and x else '')
            lines.append('  %-10s | %7d %7d | %s | %s | %s' % (b, n0, n1, f('rim', True), f('trail_mm'), f('played')))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('captures', nargs='+')
    ap.add_argument('--old', default='master', metavar='REF', help='git ref of the old code (default master)')
    ap.add_argument('--new', default=None, metavar='REF', help='git ref of the new code (default: this tree)')
    ap.add_argument('--measure', metavar='ROOT', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.measure:
        print(json.dumps(measure(args.measure, args.captures)))
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        trees = []
        try:
            for side, ref in (('old', args.old), ('new', args.new)):
                trees.append(_tree(ref, tmp, side))
            (r0, l0), (r1, l1) = trees
            old, new = _run(r0, args.captures), _run(r1, args.captures)
        finally:
            for root, _l in trees:
                if root != REPO:
                    _drop(root)
    lines = table(args.captures, old, new, '%s (%s)' % (l0, args.old), '%s (%s)' % (l1, args.new or 'this tree'))
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
