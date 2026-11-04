#!/usr/bin/env python3
"""Find the low-pass corners that bring RBR's Newton front lateral force to ACR's noise level (read-only).

    scripts/ffb-noise-match.py [--rbr CAPTURE] [--acr CAPTURE] [--max-delay MS] [--json PATH]

Newton's Fy_f = m (b/L) (a_y + a rdot) (ffb_forza.front_lateral): RBR's lateral acceleration and, above all, its yaw
acceleration (the 3-sample central difference of the yaw rate over the game's clock, RbrRack._track_yaw) are noisier than
ACR's. Per term (a_y, and a rdot in m/s^2, mass and b/L divided out so two cars compare) the script takes the band RMS at
3-15 Hz and 15-30 Hz while driving (speed over 10 m/s) in the ACR drive as it comes (the target), and in the RBR drive
through ffb_forza.LowPass (the engine's own filter, first or second order, exact for irregular samples) at each corner, and
reports for each term and order the corner with the smallest excess of the two bands over the target (a band at or under the
target by TOL costs nothing: a low-pass cannot raise it; among equal ones the highest corner, the least delay) within the delay
budget (the filter's group delay at low frequency: 1/w for first order, sqrt 2 / w for second). The unfiltered RBR / ACR ratio
and the same ratio relative to each drive's own 0.5-3 Hz band (its cornering intensity) are shown too. Defaults: the 2026-11-04 Fabia Rally2
drives, RBR 20261104-143327 and ACR 20261104-143823. Reads captures only: nothing touches a wheel, sysfs, the proxy, a game
or the telemetry database. The full text goes to ~/.cache/ffb-noise-match/latest.log."""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))
import ffb_parity as fp  # noqa: E402

LOG = os.path.join(os.path.expanduser('~'), '.cache', 'ffb-noise-match', 'latest.log')
FS = 720.0                                       # Hz: the uniform grid the bands are measured on (RBR's own game clock)
BANDS = ((3.0, 15.0), (15.0, 30.0))
CONTEXT = ((0.5, 3.0), (30.0, 60.0))             # shown, not matched
CORNERS = [round(4.0 * (150.0 / 4.0) ** (i / 39.0), 2) for i in range(40)]
TOL = 1.10                                       # a band within 10 % over the target is on it
MIN_SPEED = 10.0
LINK_GAP = 0.05                                  # s: samples further apart are not one stretch
DEFAULT_RBR, DEFAULT_ACR = '20261104-143327', '20261104-143823'


def series(path):
    """(time, a_y, rdot, valid, link, a, name) of a capture: the engine's rdot (RbrRack._track_yaw: central difference of
    three yaw-rate samples over the game's clock, arrival time where the game has none), both clipped as front_lateral clips."""
    import collections
    from oversteer import ffb_forza, telemetry_capture as tc, telemetry_formats as tf
    _meta, recs = tc.read_capture(path)
    yaw = collections.deque(maxlen=3)
    t, ay, rd, ok = [], [], [], []
    drivetrain = None
    for arrival, _a, data in recs:
        try:
            s = tf.decode_sample(data)
        except Exception:
            continue
        if s is None or s.accel is None or len(s.accel) < 2:
            continue
        drivetrain = getattr(s, 'drivetrain', None) or drivetrain
        ts = s.game_time if ffb_forza.finite(s.game_time) else arrival
        if not ffb_forza.finite(s.yaw_rate):
            yaw.clear()
            continue
        yaw.append((ts, s.yaw_rate))
        r = 0.0
        if len(yaw) == 3 and 0.0 < yaw[2][0] - yaw[0][0] <= 0.2:
            r = (yaw[2][1] - yaw[0][1]) / (yaw[2][0] - yaw[0][0])
        t.append(ts)
        ay.append(min(max(s.accel[1], -ffb_forza.AY_MAX), ffb_forza.AY_MAX) if ffb_forza.finite(s.accel[1]) else 0.0)
        rd.append(min(max(r, -ffb_forza.RDOT_MAX), ffb_forza.RDOT_MAX))
        ok.append(ffb_forza.finite(s.speed) and s.speed > MIN_SPEED)
    import numpy as np
    t, ay, rd, ok = np.array(t), np.array(ay), np.array(rd), np.array(ok)
    order = np.argsort(t, kind='stable')
    t, ay, rd, ok = t[order], ay[order], rd[order], ok[order]
    link = np.concatenate([[False], (np.diff(t) > 0) & (np.diff(t) <= LINK_GAP) & ok[1:] & ok[:-1]])
    a = ffb_forza.WHEELBASE * (1.0 - ffb_forza.front_share(drivetrain))
    return t, ay, rd, link, a


def filtered(t, x, hz, order):
    from oversteer import ffb_forza
    import numpy as np
    if hz is None:
        return x
    f = ffb_forza.LowPass(hz, order)
    return np.array([f.step(ti, xi) for ti, xi in zip(t, x)])


def bands_of(t, x, link, bands=BANDS):
    """Band RMS of x resampled onto the uniform grid, over the linked stretches of at least a second."""
    import numpy as np
    grid = np.arange(t[0], t[-1], 1.0 / FS)
    xi = np.interp(grid, t, x)
    j = np.clip(np.searchsorted(t, grid), 1, len(t) - 1)
    mask = link[j]
    rms, secs = fp.band_rms(xi, mask, FS, bands)
    return rms, secs


def delay_ms(hz, order):
    w = 2.0 * math.pi * hz
    return 1000.0 * (1.0 if order == 1 else math.sqrt(2.0)) / w


def err(have, want):
    """The RMS of the log excess of each band over its target (beyond TOL); 0 when none is over."""
    return math.sqrt(sum(max(math.log(h / w) - math.log(TOL), 0.0) ** 2 for h, w in zip(have, want)) / len(want))


def search(t, x, link, target, max_ms):
    """{order: [(corner, delay ms, bands, error)]} for each corner, and the best within the delay budget and overall."""
    res = {}
    for order in (1, 2):
        rows = []
        for hz in CORNERS:
            b, _ = bands_of(t, filtered(t, x, hz, order), link)
            rows.append((hz, delay_ms(hz, order), b, err(b, target)))
        res[order] = rows
    return res


def main():
    if fp.MISSING:
        print(fp.MISSING, file=sys.stderr)
        return 3
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--rbr', default=DEFAULT_RBR)
    ap.add_argument('--acr', default=DEFAULT_ACR)
    ap.add_argument('--max-delay', type=float, default=15.0, metavar='MS')
    ap.add_argument('--json', metavar='PATH')
    args = ap.parse_args()
    out = []

    def say(s=''):
        print(s)
        out.append(s)
    try:
        rbr, acr = fp.resolve(args.rbr), fp.resolve(args.acr)
    except FileNotFoundError as e:
        print("ffb-noise-match: %s" % e, file=sys.stderr)
        return 2
    tr, ayr, rdr, lr, a_r = series(rbr)
    ta, aya, rda, la, a_a = series(acr)
    say("RBR %s: %d samples; ACR %s: %d samples; a = %.2f m (RBR), %.2f m (ACR); grid %g Hz" %
        (os.path.basename(rbr)[:15], len(tr), os.path.basename(acr)[:15], len(ta), a_r, a_a, FS))
    terms = {'a_y': (ayr, aya, 1.0, 1.0), 'a rdot': (rdr, rda, a_r, a_a)}
    chosen = {}
    allb = BANDS + CONTEXT
    for name, (xr, xa, kr, ka) in terms.items():
        xr, xa = xr * kr, xa * ka
        br, sr = bands_of(tr, xr, lr, allb)
        ba, sa = bands_of(ta, xa, la, allb)
        say("\n%s (m/s^2 RMS while driving; RBR %.0f s, ACR %.0f s)" % (name, sr, sa))
        for (lo, hi), v, w in zip(allb, br, ba):
            say("  %4g-%-4g Hz  RBR %.3f   ACR %.3f   RBR/ACR %.2f   relative to the 0.5-3 Hz band %.2f%s" % (
                lo, hi, v, w, v / w, (v / br[len(BANDS) + 0]) / (w / ba[len(BANDS) + 0]), "" if (lo, hi) in BANDS else "   (not matched)"))
        target = ba[:len(BANDS)]
        res = search(tr, xr, lr, target, args.max_delay)
        chosen[name] = {}
        for order, rows in res.items():
            within = [r for r in rows if r[1] <= args.max_delay]
            best = min(within, key=lambda r: (round(r[3], 6), -r[0])) if within else None
            free = min(rows, key=lambda r: (round(r[3], 6), -r[0]))
            for tag, r in (('within %g ms' % args.max_delay, best), ('unconstrained', free)):
                if r is None:
                    say("  order %d %-14s none" % (order, tag))
                    continue
                say("  order %d %-14s corner %6.2f Hz  delay %5.1f ms  bands %s  RBR/ACR %s  error %.3f" %
                    (order, tag, r[0], r[1], " ".join("%.3f" % v for v in r[2]),
                     " ".join("%.2f" % (v / w) for v, w in zip(r[2], target)), r[3]))
            chosen[name][order] = {'within': best, 'free': free}
    say("\nA corner at the top of the range (150 Hz) is no filter at all. Unfiltered RBR / ACR on the two bands is the gap; a ratio of 1.00 is a match. Delay is the group delay at low frequency.")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write("\n".join(out) + "\n")
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(chosen, f, indent=1, default=float)
    return 0


if __name__ == '__main__':
    sys.exit(main())
