#!/usr/bin/env python3
"""Play a telemetry capture back.

    scripts/telemetry-replay.py FILE.ovcap.gz [--db PATH] [--profile NAME]
    scripts/telemetry-replay.py FILE.ovcap.gz --send HOST:PORT [--realtime]
    scripts/telemetry-replay.py FILE.ovcap.gz --cut A:B --write OUT.ovcap.gz

By default the capture goes through the same path as live telemetry
(Telemetry.handle, on the capture's own clock, with no LEDs) into a shift
learner, and what it learnt is printed. The database is a temporary one
unless --db names one (never point it at your real telemetry.db while
Oversteer runs). --send re-sends the packets over UDP to a running
Oversteer instead (for trying the Telemetry tab without the game), as fast
as possible or, with --realtime, at the pace they were recorded. --cut
keeps the seconds A to B of the capture in a new file (a test fixture).
"""
import argparse
import os
import socket
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer.telemetry_capture import CaptureWriter, read_capture, replay  # noqa: E402
from oversteer.shift_learner import ShiftLearner  # noqa: E402


def learn(records, db, profile):
    learner = ShiftLearner(db, profile=profile)
    telemetry = replay(records, learner)
    print("%d packets of unknown sizes: %s" % (len(telemetry._unknown_sizes), sorted(telemetry._unknown_sizes))
          if telemetry._unknown_sizes else "every packet decoded")
    for key, name in learner.known_cars():
        snapshot = learner.load_snapshot(key)
        print("\n%s (%s): limiter %.0f rpm, %d rev bands of power (%s)" % (
            name, key, snapshot['limiter'] or 0, snapshot['power_bands'], snapshot['power_source']))
        methods = learner.method_shifts(key)
        for row in snapshot['gears']:
            best = 'top gear' if row['last'] else ('%.0f' % row['best'] if row['best'] else 'learning')
            mine = '%.0f (%d)' % (row['average_shift'], row['shifts']) if row['average_shift'] else '-'
            per_method = ', '.join('%s %.0f (%d)' % (m, v[0], v[1]) for m, v in sorted(methods.get(row['gear'], {}).items()))
            print("  gear %d: %.1f rpm per km/h, best %s, you %s%s" % (
                row['gear'], row['ratio'] / 3.6, best, mine, '; ' + per_method if per_method else ''))
        for tip in snapshot['advice']:
            print("  - " + tip)
        for session in learner.history(key):
            print("  session: %d shifts, mean error %s, methods %s" % (
                session['shifts'], '%.0f' % session['error'] if session['error'] is not None else '-',
                ', '.join(session['methods']) or '-'))


def send(records, target, realtime):
    host, port = target.rsplit(':', 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    started, count = time.monotonic(), 0
    for t, addr, data in records:
        if realtime:
            wait = started + t - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        sock.sendto(data, (host, int(port)))
        count += 1
    print("%d packets sent to %s" % (count, target))


def cut(records, meta, span, out):
    first, last = (float(x) for x in span.split(':'))
    with CaptureWriter(out, port=meta.get('port'), note=meta.get('note', ''), version=meta.get('oversteer')) as writer:
        for t, addr, data in records:
            if first <= t <= last:
                writer.write(t, addr, data)
    print("%d packets written to %s" % (writer.packets, out))


def main():
    parser = argparse.ArgumentParser(description="Play an Oversteer telemetry capture back.")
    parser.add_argument('capture')
    parser.add_argument('--db', help="shift learner database (default: a temporary one)")
    parser.add_argument('--profile', default='_no_profile')
    parser.add_argument('--send', metavar='HOST:PORT', help="re-send the packets over UDP instead")
    parser.add_argument('--realtime', action='store_true', help="with --send: at the recorded pace")
    parser.add_argument('--cut', metavar='A:B', help="keep seconds A to B in a new capture (needs --write)")
    parser.add_argument('--write', metavar='FILE', help="with --cut: the new capture")
    args = parser.parse_args()
    meta, records = read_capture(args.capture)
    print("capture of %s, port %s%s" % (time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('started', 0))),
                                        meta.get('port'), ': ' + meta['note'] if meta.get('note') else ''))
    if args.cut:
        if not args.write:
            parser.error("--cut needs --write")
        cut(records, meta, args.cut, args.write)
    elif args.send:
        send(records, args.send, args.realtime)
    elif args.db:
        learn(records, args.db, args.profile)
    else:
        with tempfile.TemporaryDirectory() as folder:
            learn(records, os.path.join(folder, 'telemetry.db'), args.profile)


main()
