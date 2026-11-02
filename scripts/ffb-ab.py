#!/usr/bin/env python3
"""Objective A/B numbers for Countersteer FFB: the game's force against ours, from a capture.

    scripts/ffb-ab.py FILE.ovcap.gz [FILE2 ...] [--json] [--sections N] [--texture-level 0..1] [--all]
    scripts/ffb-ab.py --blind [--runs 2] [--seed N]
    scripts/ffb-ab.py --reveal KEYFILE [FILE1 FILE2 ...]

The capture should be a "Record for FFB tuning" one (full rate, header 'ffb_tuning'), from the
version 5 bridge (ACR). Its finalFF stands in for the game's force; ours is the engine's RackForce
and Texture driven by the same samples on the capture's clock (oversteer/ffb_metrics.py defines every
number). Nothing here touches a wheel or the running app.

--blind prints which state of the Countersteer FFB switch to drive each run with, in a random order,
and writes the key (with the order) to ~/.cache/ffb-ab/ for --reveal, which prints it back and, given
the captures in the order driven, analyses each under its state. Bookkeeping only.
"""
import argparse
import datetime
import json
import os
import random
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer import ffb_metrics  # noqa: E402
from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer.telemetry_formats import decode_sample  # noqa: E402

KEY_DIR = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'), 'ffb-ab')


def load(path):
    """(header, [(t, Sample)]) of a capture; packets that are not telemetry we know are skipped."""
    meta, records = read_capture(path)
    items = []
    for t, _addr, data in records:
        sample = decode_sample(data)
        if sample is not None:
            items.append((t, sample))
    return meta, items


def run_one(path, args):
    meta, items = load(path)
    result = ffb_metrics.analyse(items, sections=args.sections, texture_level=args.texture_level,
                                 only_ready=not args.all)
    result['file'] = path
    result['ffb_tuning'] = bool(meta.get('ffb_tuning'))
    return result


def blind(args):
    rnd = random.Random(args.seed)
    states = (['off', 'on'] * ((args.runs + 1) // 2))[:args.runs]
    rnd.shuffle(states)
    os.makedirs(KEY_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    path = os.path.join(KEY_DIR, 'ab-%s-%s.json' % (stamp, uuid.uuid4().hex[:6]))
    key = {'created': stamp, 'runs': [{'run': i + 1, 'switch': s} for i, s in enumerate(states)]}
    with open(path, 'w') as f:
        json.dump(key, f, indent=1)
    for entry in key['runs']:
        print("run %d: Countersteer FFB switch %s" % (entry['run'], entry['switch'].upper()))
    print("key: %s" % path)
    print("Not blind for the driver: you set the switch, so you know. The order only keeps you from always testing one state first.")
    print("Record each run with \"Record for FFB tuning\" on; then: scripts/ffb-ab.py --reveal %s RUN1 RUN2 ..." % path)
    return 0


def reveal(args):
    with open(args.reveal) as f:
        key = json.load(f)
    runs = key['runs']
    for entry in runs:
        print("run %d: switch %s" % (entry['run'], entry['switch']))
    if args.files and len(args.files) != len(runs):
        print("%d captures given for %d runs" % (len(args.files), len(runs)), file=sys.stderr)
        return 2
    if args.files:
        return analyse(args, [(p, 'run %d, switch %s' % (e['run'], e['switch'])) for p, e in zip(args.files, runs)])
    return 0


def analyse(args, files):
    results = []
    for path, label in files:
        result = run_one(path, args)
        result['label'] = label
        results.append(result)
        if not args.json:
            note = '' if result['ffb_tuning'] else ' (not a full-rate capture)'
            print(ffb_metrics.table(result, '%s [%s]%s' % (label, path, note)))
            print()
    if args.json:
        json.dump(results if len(results) != 1 else results[0], sys.stdout, indent=1, default=float)
        print()
    return 1 if any('error' in r for r in results) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('files', nargs='*', help="captures (.ovcap.gz)")
    ap.add_argument('--json', action='store_true', help="JSON instead of the table")
    ap.add_argument('--sections', type=int, default=0, metavar='N',
                    help="where the game gives no sectors, split each stage into N equal sections by distance")
    ap.add_argument('--texture-level', type=float, default=None, metavar='L',
                    help="Texture master level 0..1 (default: the engine's; 0 = steering force alone)")
    ap.add_argument('--all', action='store_true',
                    help="judge every moving sample, not only where the engine would be playing")
    ap.add_argument('--blind', action='store_true', help="a random switch order for A/B runs, and its key file")
    ap.add_argument('--runs', type=int, default=2, help="with --blind: runs (default 2)")
    ap.add_argument('--seed', type=int, default=None, help="with --blind: random seed")
    ap.add_argument('--reveal', metavar='KEYFILE', help="print a --blind key, and analyse captures under it")
    args = ap.parse_args()
    if args.blind:
        return blind(args)
    if args.reveal:
        return reveal(args)
    if not args.files:
        ap.error("a capture, or --blind, or --reveal")
    return analyse(args, [(p, os.path.basename(p)) for p in args.files])


if __name__ == '__main__':
    sys.exit(main())
