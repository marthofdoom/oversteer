#!/usr/bin/env python3
"""Today's force against what causes it: the rim force in Nm by the front axle's lateral force Fyf and the normalised slip theta.

    scripts/ffb-force-curve.py A [A2 ...] --vs B [B2 ...] [--class-a NAME] [--class-b NAME] [--peak NM] [--set NAME=VALUE ...]

Names are relative to the captures folder. Each capture goes through the rack of its game as in ffb-sim-match (two passes, the
aligning spring off); the force is the share times the wheel's peak. Samples at 8 m/s and more are binned by Fyf (ACR: the
tyres' |Fy_FL + Fy_FR|; Forza and RBR: the value the rack derives itself) and by theta (ffb_tools.THETA_TABLE: one place for the
per-game definition). Cells with at least --min-s seconds on both sides are compared; the biggest |difference| x sqrt(seconds)
come first. Also: Newton's Fyf against the tyres' for ACR (with the one best mass), and the effective trail (Nm per unit of
Fyf) by theta. --set ffb_forza.TP_S_HALF=0.4 (or RbrRack.DERIVED_SLIP_PEAK=0.2) sets a module or class constant for the
replay only. Runs inside ffb_score._sandbox(): the car properties start from a COPY of the live store (--empty-store: none; --store PATH: that file); no user file is written. Full log: ~/.cache/ffb-force-curve/latest.log
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_situations as sit  # noqa: E402
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-force-curve/latest.log')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('a', nargs='+')
    parser.add_argument('--vs', nargs='+', required=True)
    parser.add_argument('--class-a', choices=('rally', 'street', 'off'), help="power class for the A captures (default: the game's profile)")
    parser.add_argument('--class-b', choices=('rally', 'street', 'off'), help="power class for the B captures (default: the game's profile)")
    parser.add_argument('--peak', type=float, metavar='NM', help="the wheel's peak torque (default 3.08)")
    parser.add_argument('--set', action='append', default=[], metavar='NAME=VALUE', help='override a module or class constant for the replay, e.g. ffb_forza.TP_S_HALF=0.4')
    parser.add_argument('--min-s', type=float, default=1.0)
    parser.add_argument('--top', type=int, default=15, help='cells printed to the screen; the log has all')
    tools.add_store_args(parser)
    args = parser.parse_args(argv)
    try:
        tools.apply_store_args(args)
        r = tools.force_curve(args.a, args.vs, args.class_a, args.class_b, args.peak, args.min_s, args.set)
    except tools.NoRack as e:
        print('ffb-force-curve: %s' % e, file=sys.stderr)
        return 2
    except (FileNotFoundError, OSError, ValueError) as e:
        print('ffb-force-curve: %s' % e, file=sys.stderr)
        return 2
    rows = r['rows']
    by_cell = sorted(rows, key=lambda x: x[0])
    top = ['top %d cells by |A-B| x sqrt(shared s) of %d' % (min(args.top, len(rows)), len(rows)), sit.curve_head()] + [sit.curve_line(x) for x in rows[:args.top]]
    head = r['head'] + ['1:1 knee %.2f Nm' % r['knee_nm']]
    screen = head + [''] + r['newton'] + ([''] if r['newton'] else []) + r['summary'] + [''] + r['trail'] + [''] + top
    full = screen + ['', 'all cells by (Fyf, theta)', sit.curve_head()] + [sit.curve_line(x) for x in by_cell]
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write('\n'.join(full) + '\n')
    print('\n'.join(screen))
    print('all cells: ' + LOG)
    return 0


if __name__ == '__main__':
    sys.exit(main())
