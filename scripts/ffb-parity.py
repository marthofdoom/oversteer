#!/usr/bin/env python3
"""Compare one recorded FFB drive against a reference drive (library: scripts/ffb_parity.py).

    scripts/ffb-parity.py [CAPTURE] [--ref CAPTURE] [--json PATH] [--peak NM]

CAPTURE: a name in the captures folder (with or without .ovcap.gz) or a full path; the default is the newest capture
recorded for FFB tuning, and --ref defaults to the newest one of a different game. Prints the report and writes the same
text to ~/.cache/ffb-parity/latest.log; --json writes every measure. Reads captures only: nothing touches a wheel, sysfs,
the proxy, a game or the telemetry database.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_parity as fp  # noqa: E402

LOG = os.path.join(os.path.expanduser('~'), '.cache', 'ffb-parity', 'latest.log')


def main():
    if fp.MISSING:
        print(fp.MISSING, file=sys.stderr)
        return 3
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('capture', nargs='?', help="the drive (default: the newest FFB capture)")
    ap.add_argument('--ref', metavar='CAPTURE', help="the reference drive (default: the newest of a different game)")
    ap.add_argument('--json', metavar='PATH', help="write every measure to PATH")
    ap.add_argument('--peak', type=float, metavar='NM', help="the wheel's peak torque if the header lacks it (default 3.08)")
    args = ap.parse_args()
    try:
        if args.capture:
            drive, ref = fp.resolve(args.capture), None
            if not args.ref:
                game = fp.game_of(drive)
                for _name, path in reversed(fp.list_tuning()):
                    if path != drive and fp.has_force(path) and fp.game_of(path) != game:
                        ref = path
                        break
        else:
            drive, ref = fp.default_pair()
        if args.ref:
            ref = fp.resolve(args.ref)
        text, data = fp.run(drive, ref, args.peak)
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print("ffb-parity: %s" % e, file=sys.stderr)
        return 2
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + "\n")
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(data, f, indent=1, sort_keys=True, default=float)
    return 0


if __name__ == '__main__':
    sys.exit(main())
