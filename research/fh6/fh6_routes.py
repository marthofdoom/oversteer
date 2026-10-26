#!/usr/bin/env python3
"""Read Forza Horizon 6 AI route files (media/OpenWorld/Brio/AITracks/Route*.owt)
read-only and summarise each route: point count, length, start, end, closed
loop or not, bounding box. Optionally match against a catalogue of named
courses (LapScope's track_catalog.json: start [x, z], span [dx, dz]).

.owt layout (worked out 2026-10-26, FH6 build of 2026-10-05):
  0   char[4]  'FTWO'
  4   u32      0x200 (version)
  8   u32      hash
  12  u32      payload size (file size - 32)
  32  u32      1, u32 count (sometimes), ...
  96  records of 56 bytes:
        f32 x, y, z         world metres, y up (the Data Out PositionX/Y/Z frame?)
        f32 wx, wy, wz      half-width / lateral vector (~5 m)
        f32 ux, uy, uz      up normal
        20 bytes            flags / ids (not decoded)
Records run until the coordinates stop being finite or jump > 100 m.

Usage: fh6_routes.py <AITracks dir> [catalog.json] > routes.csv
"""
import csv
import json
import math
import os
import struct
import sys

STRIDE = 56
FIRST = 96


def read_route(path):
    d = open(path, 'rb').read()
    if d[:4] != b'FTWO':
        return None
    pts = []
    off = FIRST
    while off + STRIDE <= len(d):
        x, y, z = struct.unpack_from('<3f', d, off)
        if not all(math.isfinite(v) and abs(v) < 50000 for v in (x, y, z)):
            break
        if pts and math.dist(pts[-1], (x, y, z)) > 100:
            break
        pts.append((x, y, z))
        off += STRIDE
    return pts


def summary(pts):
    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    gap = math.dist(pts[0], pts[-1])
    xs = [p[0] for p in pts]
    zs = [p[2] for p in pts]
    return {
        'points': len(pts), 'length_m': round(length, 1),
        'start_x': round(pts[0][0], 1), 'start_y': round(pts[0][1], 1), 'start_z': round(pts[0][2], 1),
        'end_x': round(pts[-1][0], 1), 'end_z': round(pts[-1][2], 1),
        'closed': gap < 60, 'span_x': round(max(xs) - min(xs), 1), 'span_z': round(max(zs) - min(zs), 1),
    }


def main():
    folder = sys.argv[1]
    catalog = []
    if len(sys.argv) > 2:
        catalog = json.load(open(sys.argv[2]))['routes']
    rows = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith('.owt'):
            continue
        pts = read_route(os.path.join(folder, name))
        if not pts or len(pts) < 2:
            continue
        row = {'route': name[:-4]}
        row.update(summary(pts))
        # Best named course: start distance plus a bounding-box term (the
        # catalogue's start is the grid / launch point, not the route's first node)
        best = None
        for c in catalog:
            dist = math.hypot(c['start'][0] - row['start_x'], c['start'][1] - row['start_z'])
            sx, sz = c['span']
            span_err = max(abs(sx - row['span_x']) / max(sx, 50), abs(sz - row['span_z']) / max(sz, 50))
            score = dist + 2000 * span_err
            if best is None or score < best[0]:
                best = (score, c, dist, span_err)
        if best:
            score, c, dist, span_err = best
            ok = dist < 400 and span_err < 0.05
            row.update(match=c['name'] if ok else '', match_kind=c['kind'] if ok else '',
                       start_m=round(dist, 1), span_err=round(span_err, 3),
                       nearest=c['name'])
        rows.append(row)
    fields = list(rows[0].keys()) if rows else []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    w = csv.DictWriter(sys.stdout, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)


if __name__ == '__main__':
    main()
