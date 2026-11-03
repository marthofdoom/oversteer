#!/usr/bin/env python3
"""Score one recorded drive against the current Countersteer FFB code, offline (library: scripts/ffb_score.py).

    scripts/ffb-score.py DRIVE [--json OUT.json] [--hz N] [--wheel FILE] [--friction-comp PCT] [--derate X]
                         [--align-on|--align-off] [--mapping classic|knee] [--column-damping PCT] [--power-class rally|street|off] [--limit N] [--events N] [--label NAME]
    scripts/ffb-score.py --compare A.json B.json

DRIVE: ~/.cache/ffb-slip/dump.npz (the default) or a "Record for FFB tuning" capture (.ovcap.gz). Prints the compact
report (physics, wheel fit, dynamics, safety); --json writes every metric. --compare shows the deltas between two
iterations' JSON. Nothing here touches a wheel, sysfs or the running app.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_score as fs  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('drive', nargs='?', default=fs.DUMP, help="the dump (.npz) or a capture (.ovcap.gz)")
    ap.add_argument('--json', metavar='OUT', help="write every metric to OUT")
    ap.add_argument('--compare', nargs=2, metavar=('A', 'B'), help="deltas between two result files")
    ap.add_argument('--hz', type=float, default=50.0, help="engine rate fed from the dump's snapshots (default 50)")
    ap.add_argument('--wheel', default=fs.WHEEL, help="the calibrated wheel's fit result (default: the G29's)")
    ap.add_argument('--friction-comp', type=int, default=0, metavar='PCT', help="the app's friction compensation")
    ap.add_argument('--derate', type=float, default=1.0, help="the driver's thermal derate (default 1)")
    ap.add_argument('--align-on', dest='align', action='store_true', default=None, help="engine's aligning split on")
    ap.add_argument('--align-off', dest='align', action='store_false', help="engine's aligning split off")
    ap.add_argument('--mapping', choices=('classic', 'knee'), default=None, help="the steering mapping (default: the engine's, classic)")
    ap.add_argument('--column-damping', type=float, default=None, metavar='PCT', help="the knee mapping's column damping, %% of the real column's (default: the engine's, 60)")
    ap.add_argument('--power-class', choices=('rally', 'street', 'off'), default=None, help="the assist class (default: the engine's, rally)")
    ap.add_argument('--limit', type=int, default=None, help="only the first N rows (quick looks)")
    ap.add_argument('--events', type=int, default=24, help="slides to simulate (default 24)")
    ap.add_argument('--label', default='', help="a name for the report's first line")
    args = ap.parse_args()
    if args.compare:
        a, b = (json.load(open(p)) for p in args.compare)
        print(fs.compare(a, b, os.path.basename(args.compare[0]), os.path.basename(args.compare[1])))
        return 0
    if not os.path.exists(args.drive):
        print("no such drive: %s" % args.drive, file=sys.stderr)
        return 2
    drive = fs.load_drive(args.drive, args.limit)
    result = fs.score(drive, hz=args.hz, wheel_path=args.wheel, friction_comp=args.friction_comp, derate=args.derate,
                      align_on=args.align, events=args.events, mapping=args.mapping,
                      column_damping=args.column_damping, power_class=args.power_class)
    print(fs.report(result, args.label))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(result, f, indent=1, sort_keys=True)
        print("wrote %s" % args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
