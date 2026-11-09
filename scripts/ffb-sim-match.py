#!/usr/bin/env python3
"""Today's force code over recorded physics, in ffb-match's situations: what the rack makes of the same drive, not what was played.

    scripts/ffb-sim-match.py A [A2 ...] --vs B [B2 ...] [--class-a NAME] [--class-b NAME] [--peak NM] [--min-s 1.0]

Names are relative to the captures folder. Each capture is fed to the rack of its game (ACR: RackForce, RBR: RbrRack, Forza:
ForzaRack; another game exits with a message saying which rack is missing) twice, the first pass letting the rack learn, with
the aligning spring off so the force is the whole share. The power class is the one in the game's profile (else rally, Forza
street); --class-a / --class-b override it for that side. Time in a situation is the number of samples times the capture's real
sample spacing. Runs inside ffb_score._sandbox(): the car properties start from a COPY of the live store (--empty-store: none; --store PATH: that file); no user file is written. Full log: ~/.cache/ffb-sim-match/latest.log
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_situations as sit  # noqa: E402
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-sim-match/latest.log')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('a', nargs='+')
    parser.add_argument('--vs', nargs='+', required=True)
    parser.add_argument('--class-a', choices=('rally', 'street', 'off'), help="power class for the A captures (default: the game's profile)")
    parser.add_argument('--class-b', choices=('rally', 'street', 'off'), help="power class for the B captures (default: the game's profile)")
    parser.add_argument('--peak', type=float, metavar='NM', help="the wheel's peak torque (default 3.08)")
    parser.add_argument('--min-s', type=float, default=1.0)
    parser.add_argument('--top', type=int, default=12, help='rows printed to the screen; the log has all')
    tools.add_store_args(parser)
    args = parser.parse_args(argv)
    try:
        tools.apply_store_args(args)
        lines, rows, summary = tools.sim_match(args.a, args.vs, args.class_a, args.class_b, args.peak, args.min_s)
    except tools.NoRack as e:
        print('ffb-sim-match: %s' % e, file=sys.stderr)
        return 2
    except (FileNotFoundError, OSError, ValueError) as e:
        print('ffb-sim-match: %s' % e, file=sys.stderr)
        return 2
    head, body = sit.table(rows)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write('\n'.join(lines + [summary, head] + body) + '\n')
    print('\n'.join(lines + [summary, head] + body[:args.top]))
    print('all rows: ' + LOG)
    return 0


if __name__ == '__main__':
    sys.exit(main())
