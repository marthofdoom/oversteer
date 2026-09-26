#!/usr/bin/env python3
"""Show what arrives on the telemetry port and how Oversteer decodes it.

    scripts/telemetry-capture.py [port] [--write FILE.ovcap.gz] [--note TEXT]

Quit Oversteer (or switch its rev lights off) first: only one listener can
own the port. Prints one line per distinct (source, size) and a decoded
sample every second. With --write, every packet is also kept in a capture
file that scripts/telemetry-replay.py plays back.
"""
import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer.telemetry_formats import decode_sample  # noqa: E402
from oversteer.telemetry_capture import CaptureWriter  # noqa: E402

parser = argparse.ArgumentParser(description="Show (and optionally record) the telemetry arriving on a UDP port.")
parser.add_argument('port', nargs='?', type=int, default=5310)
parser.add_argument('--write', metavar='FILE', help="also record every packet to this capture file (.ovcap.gz)")
parser.add_argument('--note', default='', help="a note stored in the capture (the game, what was driven)")
args = parser.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', args.port))
sock.settimeout(1.0)
writer = CaptureWriter(args.write, port=args.port, note=args.note) if args.write else None
print("listening on UDP %d (Ctrl-C to stop)%s" % (args.port, ", recording to " + args.write if writer else ""))
seen = set()
last_print = 0.0
count = 0
try:
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        if writer is not None:
            writer.write(time.monotonic(), addr, data)
        count += 1
        key = (addr[0], len(data))
        if key not in seen:
            seen.add(key)
            print("new source: %s, %d-byte packets, first bytes %s" % (addr[0], len(data), data[:8].hex()))
        now = time.time()
        if now - last_print >= 1.0:
            last_print = now
            sample = decode_sample(data)
            if sample is None:
                print("  #%d %d bytes -> not decoded" % (count, len(data)))
            else:
                print("  #%d %d bytes -> %s %s: %.0f rpm (max %s), gear %s, %s m/s" % (
                    count, len(data), sample.game, sample.car, sample.rpm,
                    '%.0f' % sample.max_rpm if sample.max_rpm else '?', sample.gear,
                    '%.1f' % sample.speed if sample.speed is not None else '?'))
except KeyboardInterrupt:
    pass
finally:
    if writer is not None:
        writer.close()
        print("%d packets written to %s" % (writer.packets, args.write))
