"""DiRT Rally 2.0 sectors and the run's lifecycle on marth's captures (read-only).

    python3 research/dr2/sectors.py [CAPTURE ...]

For each stage run (the stage clock, float 1, restarting), prints where float 48 (sector) changes, with the clock,
lap distance and floats 49/50 (sector 1 / 2 times) at that moment, and how the run ends (lap 36, laps done 59,
progress 3, last lap time 62). Also the vertical axis check (range of x, y, z per run). Writes nothing.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inventory import FOLDER, dr2_packets  # noqa: E402


def main(paths):
    if not paths:
        paths = sorted(glob.glob(os.path.join(os.path.expanduser(FOLDER), '*.ovcap.gz')))
    for path in paths:
        prev = None
        print('==', os.path.basename(path))
        for t, f in dr2_packets(path):
            if prev is None:
                prev = f
                print('  start t %.2f clock %.2f dist %.1f prog %.3f len %.1f sector %d' % (t, f[1], f[2], f[3], f[61], f[48]))
                continue
            events = []
            if f[48] != prev[48]:
                events.append('sector %d->%d' % (prev[48], f[48]))
            if f[36] != prev[36]:
                events.append('lap %d->%d' % (prev[36], f[36]))
            if f[59] != prev[59]:
                events.append('laps_done %d->%d' % (prev[59], f[59]))
            if f[49] != prev[49] or f[50] != prev[50]:
                events.append('s1 %.3f s2 %.3f' % (f[49], f[50]))
            if f[62] != prev[62]:
                events.append('last_lap %.3f' % f[62])
            if (f[3] >= 1.5) != (prev[3] >= 1.5):
                events.append('prog %.3f->%.3f' % (prev[3], f[3]))
            if f[1] < prev[1] - 1.0:
                events.append('CLOCK RESET from %.2f' % prev[1])
            if f[61] != prev[61]:
                events.append('len %.1f->%.1f' % (prev[61], f[61]))
            if f[63] != prev[63]:
                events.append('max_rpm %.1f->%.1f idle %.1f gears %d' % (prev[63] * 9.5493, f[63] * 9.5493, f[64] * 9.5493, f[65]))
            if (prev[1] == 0.0) != (f[1] == 0.0):
                events.append('clock %s' % ('runs' if f[1] else 'zero'))
            if events:
                print('  t %8.2f clock %7.2f total %7.2f dist %8.1f prog %.3f spd %5.1f | %s' % (
                    t, f[1], f[0], f[2], f[3], f[7], '; '.join(events)))
            prev = f


if __name__ == '__main__':
    main(sys.argv[1:])
