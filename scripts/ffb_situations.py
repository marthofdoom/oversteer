"""The situation bands and the A-against-B comparison shared by scripts/ffb-match.py (what was played) and
scripts/ffb-sim-match.py (what today's force code makes of the same physics). Library only."""
import numpy as np

AY = (0.0, 2.0, 4.0, 6.0, 9.0, 13.0, 99.0)
SLIP = (0.0, 0.05, 0.1, 0.2, 0.4, 9.0)
SPEED = (8.0, 15.0, 25.0, 35.0, 199.0)
STEP = 0.01                                      # s: the grid the force is read on


def band(edges, x):
    return np.clip(np.digitize(x, edges) - 1, 0, len(edges) - 2)


def label(edges, i):
    return '%g-%g' % (edges[i], edges[i + 1]) if edges[i + 1] < 90 else '%g+' % edges[i]


PEDALS = ('coast', 'power', 'brake')


def compare(a, b, min_s, secs_a=None, secs_b=None):
    """[(key, median a, p10-p90 a, s a, median b, p10-p90 b, s b)] of the situations both sides have, biggest difference first.
    Time is len * STEP unless secs_a / secs_b ({key: seconds}) say what the samples really span."""
    rows = []
    need = min_s / STEP
    for key in set(a) & set(b):
        sa = secs_a[key] if secs_a else len(a[key]) * STEP
        sb = secs_b[key] if secs_b else len(b[key]) * STEP
        if (secs_a or len(a[key]) >= need) and (secs_b or len(b[key]) >= need) and sa >= min_s - 1e-9 and sb >= min_s - 1e-9:
            x, y = np.array(a[key]), np.array(b[key])
            rows.append((key, np.median(x), np.percentile(x, 10), np.percentile(x, 90), sa,
                         np.median(y), np.percentile(y, 10), np.percentile(y, 90), sb))
    return sorted(rows, key=lambda row: -abs(row[1] - row[5]))



def summary(rows):
    """The one line about the situations in common, and the weighted numbers it holds: (line, mean, typical size)."""
    if not rows:
        return 'no situations in common', None, None
    delta = np.array([r[1] - r[5] for r in rows])
    weight = np.array([min(r[4], r[8]) for r in rows])
    mean, size = float(np.average(delta, weights=weight)), float(np.average(np.abs(delta), weights=weight))
    return ('%d situations in common; A minus B, weighted by shared time: %+.3f; typical size %.3f (share of full force)'
            % (len(rows), mean, size)), mean, size


def table(rows):
    """(header, [one line per row]) in ffb-match's format."""
    head = '%-7s %-9s %-7s %-6s | %-20s %6s | %-20s %6s | %6s' % ('ay', 'slip', 'speed', 'pedal', 'A median (p10-p90)', 's', 'B median (p10-p90)', 's', 'A-B')
    body = ['%-7s %-9s %-7s %-6s | %.3f (%.3f-%.3f) %6.1f | %.3f (%.3f-%.3f) %6.1f | %+.3f' % (
        label(AY, r[0][0]), label(SLIP, r[0][1]), label(SPEED, r[0][2]), PEDALS[r[0][3]], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[1] - r[5]) for r in rows]
    return head, body
