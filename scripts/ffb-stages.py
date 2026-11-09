#!/usr/bin/env python3
"""Each stage of today's force chain, by |ay| and speed, A against B: where two routes to the same force diverge and re-converge.

    scripts/ffb-stages.py A [A2 ...] --vs B [B2 ...] [--class-a NAME] [--class-b NAME] [--peak NM] [--set NAME=VALUE ...]
                          [--mass-a KG] [--mass-b KG] [--ratio-a R] [--ratio-b R]

Names are relative to the captures folder. Each capture goes through the rack of its game as in ffb-sim-match (two passes, the
aligning spring off), and per sample the values the rack's own code has at each stage are recorded: |ay| and speed; the front slip
(rad); Fyf, the front axle's lateral force the code uses, with the mass and b/L that went in; the kingpin torque before the ratio
(ACR: front Mz clamped to the trail band plus the caster arm and the scrub couple; RBR and Forza: trail x Fyf), split into its
caster part and the pneumatic (or Mz) part, and the effective trail kingpin / Fyf; the ratio and the column torque; the driver's
torque after driver_from_column (the boost) and the boost's gain; rho and the rim torque; the share after rim_share and the Nm
played. Samples at 8 m/s and more are binned by |ay| (0, 1, ... 10, 12, 15 m/s2) and speed (8-20, 20-30, 30+ m/s), weighted by
real time; for the cells with --min-s seconds on both sides the median of every stage, A, B and A/B, and over all cells (shared
seconds as weights) the ratio A/B at each stage in pipeline order, also for |ay| under 5 and at 5 or more.
--mass-a/b and --ratio-a/b replace the mass and the steering ratio the rack uses (RBR: the car table's mass_kg and DERIVED_RATIO;
Forza: its mass and ratio; ACR: ratio only, its force takes no mass); the value in force is printed in the header. For ACR the true
mass is estimated per drive: least squares of the four tyres' summed Fy on ay, |ay| over 3, low yaw acceleration, and its front
share. Runs inside ffb_score._sandbox(): the car properties start from a COPY of the live store (--empty-store: none; --store PATH: that file); no user file is written. Full log: ~/.cache/ffb-stages/latest.log
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-stages/latest.log')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('a', nargs='+')
    parser.add_argument('--vs', nargs='+', required=True)
    parser.add_argument('--class-a', choices=('rally', 'street', 'off'), help="power class for the A captures (default: the game's profile)")
    parser.add_argument('--class-b', choices=('rally', 'street', 'off'), help="power class for the B captures (default: the game's profile)")
    parser.add_argument('--peak', type=float, metavar='NM', help="the wheel's peak torque (default 3.08)")
    parser.add_argument('--set', action='append', default=[], metavar='NAME=VALUE', help='override a module or class constant for the replay, e.g. ffb_forza.TP_S_HALF=0.4')
    parser.add_argument('--mass-a', type=float, metavar='KG', help='the mass the A rack uses (not for ACR)')
    parser.add_argument('--mass-b', type=float, metavar='KG', help='the mass the B rack uses (not for ACR)')
    parser.add_argument('--ratio-a', type=float, metavar='R', help='the steering ratio the A rack uses')
    parser.add_argument('--ratio-b', type=float, metavar='R', help='the steering ratio the B rack uses')
    parser.add_argument('--min-s', type=float, default=1.0)
    tools.add_store_args(parser)
    args = parser.parse_args(argv)
    try:
        tools.apply_store_args(args)
        r = tools.stages(args.a, args.vs, args.class_a, args.class_b, args.peak, args.min_s, args.set,
                         (args.mass_a, args.mass_b), (args.ratio_a, args.ratio_b))
    except tools.NoRack as e:
        print('ffb-stages: %s' % e, file=sys.stderr)
        return 2
    except (FileNotFoundError, OSError, ValueError) as e:
        print('ffb-stages: %s' % e, file=sys.stderr)
        return 2
    screen = r['head'] + [''] + r['mass'] + ([''] if r['mass'] else []) + r['summary']
    full = screen + ['', 'all cells (A median, B median, A/B per stage)'] + r['cells']
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write('\n'.join(full) + '\n')
    print('\n'.join(screen))
    print('all cells: ' + LOG)
    return 0


if __name__ == '__main__':
    sys.exit(main())
