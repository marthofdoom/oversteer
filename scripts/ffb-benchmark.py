#!/usr/bin/env python3
"""A comparison engine to tune any game's derived force against (library: scripts/ffb_benchmark.py).

    scripts/ffb-benchmark.py build [--hold-out NAME ...] [--out PATH] [--captures NAME ...]
    scripts/ffb-benchmark.py compare CAPTURE [--bench PATH] [--view played|column|replay ...] [--pool CLASS] [--json PATH]

build: replays every reference-game (ACR) drive that carries tyre forces through today's RackForce at a fixed mapping and
writes ~/.cache/ffb-benchmark/benchmark.json (cells by lateral load x body slip x speed x pedal, pooled over similar cars).
compare: scores a drive of any game against it. The target is "what ACR would do": the owner's stated reference, which
physics may overrule. Prints the report and writes the same text to ~/.cache/ffb-benchmark/latest.log. Reads captures only:
nothing touches a wheel, sysfs, the proxy, a game, the car store or the telemetry database. Run it under `nice -n 15`.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_benchmark as fb  # noqa: E402

LOG = os.path.join(fb.CACHE, 'latest.log')


def emit(text, mode='a'):
    print(text, flush=True)
    os.makedirs(fb.CACHE, exist_ok=True)
    with open(LOG, mode) as f:
        f.write(text + "\n")


def reference_captures(names=None):
    """The ACR captures in the captures folders (a decodable ACR game key), oldest first."""
    found = {}
    for folder in fb.fp.captures_folders():
        for f in sorted(os.listdir(folder)):
            if f.endswith('.ovcap.gz'):
                found[f[:-len('.ovcap.gz')]] = os.path.join(folder, f)
    out = []
    for name, path in sorted(found.items()):
        if names and name not in names:
            continue
        if fb.fp.game_of(path) == 'acr':
            out.append(path)
    return out


def main():
    if fb.MISSING:
        print(fb.MISSING, file=sys.stderr)
        return 3
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--hold-out', nargs='*', default=[], metavar='NAME', help="drives to leave out (a sanity check)")
    b.add_argument('--out', default=fb.BENCHMARK, metavar='PATH')
    b.add_argument('--captures', nargs='*', metavar='NAME', help="only these captures (default: every ACR capture)")
    c = sub.add_parser('compare')
    c.add_argument('capture')
    c.add_argument('--bench', default=fb.BENCHMARK, metavar='PATH')
    c.add_argument('--view', nargs='*', choices=['played', 'column', 'replay'], help="default: every view the drive allows")
    c.add_argument('--pool', metavar='CLASS', help="score against this pool (default: the drive's class, else all)")
    c.add_argument('--json', metavar='PATH')
    args = ap.parse_args()
    try:
        fb.configure_store()
        if args.cmd == 'build':
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            emit("build started", 'w')
            bench = fb.build(reference_captures(args.captures), args.hold_out, log=emit)
            with open(args.out, 'w') as f:
                json.dump(bench, f, indent=1, sort_keys=True, default=float)
            emit(fb.report_build(bench))
            emit("written %s" % args.out)
            return 0
        with open(args.bench) as f:
            bench = json.load(f)
        res = fb.compare(fb.fp.resolve(args.capture), bench, args.view, args.pool)
        emit(fb.report_compare(res, bench), 'w')
        if args.json:
            with open(args.json, 'w') as f:
                json.dump(res, f, indent=1, sort_keys=True, default=float)
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        print("ffb-benchmark: %s" % e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
