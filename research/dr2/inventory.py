"""Inventory every float DiRT Rally 2.0 sends (extradata 3, 264 bytes) on marth's captures (read-only).

    python3 research/dr2/inventory.py [CAPTURE ...]

With no arguments, every capture in the Flatpak's captures folder holding 264-byte packets. Prints, per capture, the
min / max / distinct count of each of the 66 floats, then per-run summaries (runs split where float 1, the stage
clock, falls back). Writes nothing.
"""
import collections
import glob
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from oversteer.telemetry_capture import read_capture  # noqa: E402

FOLDER = '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures'
NAMES = ['time', 'lap_time', 'lap_distance', 'distance/progress', 'x', 'y', 'z', 'speed', 'vx', 'vy', 'vz',
         'roll_x', 'roll_y', 'roll_z', 'pitch_x', 'pitch_y', 'pitch_z', 'susp_rl', 'susp_rr', 'susp_fl', 'susp_fr',
         'svel_rl', 'svel_rr', 'svel_fl', 'svel_fr', 'wspd_rl', 'wspd_rr', 'wspd_fl', 'wspd_fr', 'throttle', 'steer',
         'brake', 'clutch', 'gear', 'g_lat', 'g_lon', 'lap', 'rpm', 'sli_pro', 'car_pos', 'kers', 'kers_max', 'drs',
         'tc', 'abs', 'fuel', 'fuel_cap', 'in_pits', 'sector', 'sector1_time', 'sector2_time', 'btemp_rl',
         'btemp_rr', 'btemp_fl', 'btemp_fr', 'tpress_rl', 'tpress_rr', 'tpress_fl', 'tpress_fr', 'laps_done',
         'total_laps', 'track_length', 'last_lap_time', 'max_rpm', 'idle_rpm', 'max_gears']


def dr2_packets(path):
    _meta, records = read_capture(path)
    for t, _addr, data in records:
        if len(data) == 264:
            yield t, struct.unpack('<66f', data)


def main(paths):
    if not paths:
        paths = sorted(glob.glob(os.path.join(os.path.expanduser(FOLDER), '*.ovcap.gz')))
    for path in paths:
        rows = list(dr2_packets(path))
        if not rows:
            continue
        print('==', os.path.basename(path), len(rows), 'packets, %.0f s' % (rows[-1][0] - rows[0][0]))
        for i, name in enumerate(NAMES):
            vals = [r[1][i] for r in rows if math.isfinite(r[1][i])]
            distinct = len(set(round(v, 4) for v in vals))
            print('  %2d %-18s min %12.4f max %12.4f distinct %6d' % (i, name, min(vals), max(vals), distinct))
        # runs: the stage clock restarting
        runs, start = [], 0
        for k in range(1, len(rows)):
            if rows[k][1][1] < rows[k - 1][1][1] - 1.0:
                runs.append(rows[start:k])
                start = k
        runs.append(rows[start:])
        for run in runs:
            f = [r[1] for r in run]
            last = f[-1]
            secs = collections.Counter(int(x[48]) for x in f)
            print('  run: %d pkts clock %.1f..%.1f lapdist %.0f..%.0f prog %.3f..%.3f len %.1f startz %.1f '
                  'lap %d..%d laps_done %d..%d total_laps %d sector %s s1 %.2f s2 %.2f last_lap %.2f car %.0f/%.0f/%.0f'
                  % (len(f), f[0][1], last[1], min(x[2] for x in f), max(x[2] for x in f),
                     min(x[3] for x in f), max(x[3] for x in f), last[61], f[0][6], f[0][36], last[36],
                     f[0][59], last[59], last[60], dict(secs), max(x[49] for x in f), max(x[50] for x in f),
                     max(x[62] for x in f), last[63] * 30 / math.pi, last[64] * 30 / math.pi, last[65]))


if __name__ == '__main__':
    main(sys.argv[1:])
