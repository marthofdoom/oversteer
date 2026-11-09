#!/usr/bin/env python3
"""The rim force in Nm by |ay| band (0-1, 1-3, 3-6, 6-9, 9+ m/s2), median and p90, for each capture through today's rack.

    scripts/ffb-bands.py CAPTURE [CAPTURE ...] [--class NAME] [--peak NM] [--empty-store] [--store PATH]

Each capture goes through the rack of its game as in ffb-sim-match (two passes, aligning spring off), power class from --class
(default rally); the rim Nm is the share times the wheel's peak, samples at 8 m/s and more. The car properties start from a COPY of
the live store (--empty-store: none; --store PATH: that file). Full log: ~/.cache/ffb-bands/latest.log
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-bands/latest.log')
BANDS = ((0, 1), (1, 3), (3, 6), (6, 9), (9, 1e9))


def band_lines(name, ay, nm):
    """['band  seconds-free count  median  p90'] rows of the rim Nm by |ay| band."""
    out = ['  |ay| m/s2    samples  median Nm  p90 Nm']
    for lo, hi in BANDS:
        z = (np.abs(ay) >= lo) & (np.abs(ay) < hi) & np.isfinite(nm)
        label = '%g-%g' % (lo, hi) if hi < 1e9 else '%g+' % lo
        out.append('  %-10s %9d  %9s  %6s' % (label, z.sum(), '%.2f' % np.median(nm[z]) if z.sum() else '-',
                                              '%.2f' % np.percentile(nm[z], 90) if z.sum() else '-'))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('captures', nargs='+')
    parser.add_argument('--class', dest='cls', default='rally', choices=('rally', 'street', 'off'))
    parser.add_argument('--peak', type=float, metavar='NM')
    tools.add_store_args(parser)
    args = parser.parse_args(argv)
    import ffb_benchmark as fb
    import ffb_parity as fp
    import ffb_score as fs
    try:
        tools.apply_store_args(args)
    except ValueError as e:
        print('ffb-bands: %s' % e, file=sys.stderr)
        return 2
    out = [fs.seed_path()[1]]
    with fs._sandbox():
        for name in args.captures:
            path = tools.capture_path(name)
            game = fp.game_of(path)
            try:
                tools.rack_for(game, os.path.basename(path)[:15])
            except tools.NoRack as e:
                out.append('%s: %s' % (name, e))
                continue
            sim = tools.simulate_rows(list(fb._walk(path)[1]), game, args.cls, args.peak)
            out += tools.info_lines(os.path.basename(path)[:15], sim['info'])
            out += band_lines(name, sim['cols'].get('ay', np.zeros(0)), sim['cols'].get('nm', np.zeros(0)))
    text = '\n'.join(out)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
