"""The situation bands and the A-against-B comparison shared by scripts/ffb-match.py (what was played) and
scripts/ffb-sim-match.py (what today's force code makes of the same physics), and the force-curve cells (front axle lateral
force against normalised slip) of scripts/ffb-force-curve.py. Library only."""
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


# --- ffb-force-curve: Nm at the rim against what causes it -------------------------------------------------------------
FYF = (0.0, 300.0, 600.0, 1000.0, 1500.0, 2000.0, 3000.0, 4000.0, 6000.0, 9000.0, 1e9)      # N: front axle lateral force
THETA = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.5, 3.0, 1e9)                                       # normalised slip
THETA_GRIP = 5                                   # theta bands under this index lie below theta 1
MIN_PCT_NM = 0.05                                # Nm: a B median under this gives no percent (the ratio is noise)


def curve_label(edges, i):
    return '%g-%g' % (edges[i], edges[i + 1]) if edges[i + 1] < 1e8 else '%g+' % edges[i]


def curve_cells(nm, fyf, theta, dt):
    """({(Fyf band, theta band): [Nm]}, {cell: seconds}) of per-sample arrays; samples without a finite theta or Fyf are left out.
    `dt` is the seconds each sample stands for (a number or an array)."""
    nm, fyf, theta = (np.asarray(x, float) for x in (nm, fyf, theta))
    dt = np.broadcast_to(np.asarray(dt, float), nm.shape)
    ok = np.isfinite(nm) & np.isfinite(fyf) & np.isfinite(theta)
    nm, fyf, theta, dt = nm[ok], fyf[ok], theta[ok], dt[ok]
    i, j = band(FYF, fyf), band(THETA, theta)
    cells, secs = {}, {}
    for k in set(zip(i.tolist(), j.tolist())):
        m = (i == k[0]) & (j == k[1])
        cells[k] = nm[m].tolist()
        secs[k] = float(dt[m].sum())
    return cells, secs


def curve_rows(a, b, min_s):
    """[(key, med A Nm, med B Nm, s A, s B, A-B Nm, A-B as % of B or nan, rank)] of the cells both sides have >= min_s in,
    the biggest |difference| x sqrt(shared seconds) first. `a` and `b` are (cells, secs) of curve_cells."""
    rows = []
    for r in compare(a[0], b[0], min_s, a[1], b[1]):
        key, ma, mb, sa, sb = r[0], r[1], r[5], r[4], r[8]
        pct = (ma - mb) / mb * 100.0 if mb > MIN_PCT_NM else float('nan')
        rows.append((key, ma, mb, sa, sb, ma - mb, pct, abs(ma - mb) * np.sqrt(min(sa, sb))))
    return sorted(rows, key=lambda r: -r[7])


def _weighted(rows, pick):
    sel = [r for r in rows if pick(r)]
    if not sel:
        return None
    w = np.array([min(r[3], r[4]) for r in sel])
    d = np.array([r[5] for r in sel])
    p = np.array([r[6] for r in sel])
    ok = np.isfinite(p)
    out = {'n': len(sel), 'nm_mean': float(np.average(d, weights=w)), 'nm_size': float(np.average(np.abs(d), weights=w)), 'secs': float(w.sum())}
    if ok.any():
        out.update(npct=int(ok.sum()), pct_mean=float(np.average(p[ok], weights=w[ok])), pct_size=float(np.average(np.abs(p[ok]), weights=w[ok])))
    return out


def curve_summary(rows, knee_nm):
    """The summary lines: over all cells, and over the cells with theta < 1 whose forces lie under the 1:1 knee (knee_nm).
    Time-weighted by the shared seconds; percent is of B, cells with a B median under MIN_PCT_NM give none."""
    sets = (('all cells', lambda r: True),
            ('theta < 1 and under the knee (%.2f Nm)' % knee_nm, lambda r: r[0][1] < THETA_GRIP and max(r[1], r[2]) < knee_nm))
    out = []
    for name, pick in sets:
        s = _weighted(rows, pick)
        if s is None:
            out.append('%s: no cells in common' % name)
        elif 'pct_mean' not in s:
            out.append('%s: %d cells, %.0f s; A minus B %+.3f Nm, typical %.3f Nm; no percent (B under %.2f Nm)' % (
                name, s['n'], s['secs'], s['nm_mean'], s['nm_size'], MIN_PCT_NM))
        else:
            out.append('%s: %d cells (%d with a percent), %.0f s; A minus B %+.1f %% of B, typical size %.1f %%  (%+.3f Nm, typical %.3f Nm)' % (
                name, s['n'], s['npct'], s['secs'], s['pct_mean'], s['pct_size'], s['nm_mean'], s['nm_size']))
    return out


def curve_head():
    return '%-10s %-8s | %7s %7s | %6s %6s | %8s %7s' % ('Fyf N', 'theta', 'A Nm', 'B Nm', 's A', 's B', 'A-B Nm', '% of B')


def curve_line(r):
    return '%-10s %-8s | %7.3f %7.3f | %6.1f %6.1f | %+8.3f %7s' % (
        curve_label(FYF, r[0][0]), curve_label(THETA, r[0][1]), r[1], r[2], r[3], r[4], r[5],
        ('%+.1f' % r[6]) if np.isfinite(r[6]) else '.')


# --- ffb-stages: each stage of the force chain by |ay| and speed ---------------------------------------------------------
STAGE_AY = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0, 15.0, 99.0)     # m/s2: |lateral acceleration|
STAGE_SPEED = (8.0, 20.0, 30.0, 199.0)                                                       # m/s
STAGE_SPLIT = 5                                  # STAGE_AY bands under this index are |ay| < 5 m/s2
# (key, name in the output) in pipeline order; ay and speed first, they are what the cells are made of
STAGES = (('ay', 'ay m/s2'), ('speed', 'speed m/s'), ('slip', 'slip rad'), ('fyf', 'Fyf N'), ('mass', 'mass kg'), ('bl', 'b/L'),
          ('kingpin', 'kingpin Nm'), ('caster', ' caster Nm'), ('pneu', ' pneu/Mz Nm'), ('trail_mm', 'trail mm'),
          ('ratio', 'ratio'), ('column', 'column Nm'), ('driver', 'driver Nm'), ('gain', 'boost gain'), ('rho', 'rho'),
          ('rim', 'rim Nm'), ('share', 'share'), ('played', 'Nm played'))


def stage_cells(cols, dt):
    """({(ay band, speed band): {stage: median}}, {cell: seconds}) of per-sample arrays in `cols` (keys of STAGES; NaN where a
    game has no such stage) and `dt`, the seconds each sample stands for (a number or an array). Real time, not sample counts."""
    ay, speed = np.asarray(cols['ay'], float), np.asarray(cols['speed'], float)
    dt = np.broadcast_to(np.asarray(dt, float), ay.shape)
    ok = np.isfinite(ay) & np.isfinite(speed)
    i, j = band(STAGE_AY, ay), band(STAGE_SPEED, speed)
    meds, secs = {}, {}
    for k in set(zip(i[ok].tolist(), j[ok].tolist())):
        m = ok & (i == k[0]) & (j == k[1])
        row = {}
        for key, _name in STAGES:
            v = np.asarray(cols[key], float)[m]
            v = v[np.isfinite(v)]
            row[key] = float(np.median(v)) if len(v) else float('nan')
        meds[k] = row
        secs[k] = float(dt[m].sum())
    return meds, secs


def stage_pool(sides):
    """Join per-capture (cols, dt) into one cols dict and one dt array."""
    cols = {key: [] for key, _name in STAGES}
    dts = []
    for c, dt in sides:
        n = len(c['ay'])
        for key in cols:
            cols[key].append(np.asarray(c[key], float) if key in c else np.full(n, np.nan))
        dts.append(np.full(n, dt))
    return {k: np.concatenate(v) if v else np.zeros(0) for k, v in cols.items()}, (np.concatenate(dts) if dts else np.zeros(0))


def stage_ratios(a, b, min_s, pick=None):
    """{stage: (n cells, mean A, mean B, A/B, seconds)}: over the cells both sides have >= min_s in (and `pick(cell)`), the
    cells' medians averaged with the shared seconds as weights, A/B the ratio of those two means (cells where either side has no
    value at a stage are left out of that stage). `a`, `b` are (medians, seconds) of stage_cells. None for a stage with no cell."""
    out = {}
    cells = [k for k in set(a[0]) & set(b[0]) if a[1][k] >= min_s - 1e-9 and b[1][k] >= min_s - 1e-9 and (pick is None or pick(k))]
    for key, _name in STAGES:
        sel = [k for k in cells if np.isfinite(a[0][k][key]) and np.isfinite(b[0][k][key])]
        if not sel:
            out[key] = None
            continue
        w = np.array([min(a[1][k], b[1][k]) for k in sel])
        ma = float(np.average([a[0][k][key] for k in sel], weights=w))
        mb = float(np.average([b[0][k][key] for k in sel], weights=w))
        out[key] = (len(sel), ma, mb, ma / mb if mb else float('nan'), float(w.sum()))
    return out


def stage_summary(a, b, min_s):
    """Lines: per stage in pipeline order, mean A, mean B and A/B over all cells, the cells with |ay| under 5 and those with 5 or more."""
    sets = (('all', None), ('|ay| < 5', lambda k: k[0] < STAGE_SPLIT), ('|ay| >= 5', lambda k: k[0] >= STAGE_SPLIT))
    res = [stage_ratios(a, b, min_s, p) for _n, p in sets]
    secs = ['%s: %d cells, %.0f s' % (n, max([r[k][0] for k in r if r[k]] or [0]), max([r[k][4] for k in r if r[k]] or [0.0])) for (n, _p), r in zip(sets, res)]
    lines = ['stage ratios A/B, cell medians averaged with the shared seconds (cells with >= %g s on both sides): ' % min_s + '; '.join(secs),
             '%-12s | %s' % ('stage', ' | '.join('%-26s' % n for n, _p in sets)),
             '%-12s | %s' % ('', ' | '.join('%8s %8s %7s' % ('A', 'B', 'A/B') for _ in sets))]
    for key, name in STAGES:
        parts = []
        for r in res:
            v = r[key]
            parts.append('%8.4g %8.4g %7.3f' % v[1:4] if v else '%8s %8s %7s' % ('.', '.', '.'))
        lines.append('%-12s | %s' % (name, ' | '.join(parts)))
    return lines


def stage_cell_lines(a, b, min_s):
    """The per-cell table for the log: for each cell both sides have, A median, B median and A/B at every stage."""
    keys = sorted(k for k in set(a[0]) & set(b[0]) if a[1][k] >= min_s - 1e-9 and b[1][k] >= min_s - 1e-9)
    lines = []
    for k in keys:
        lines.append('|ay| %s m/s2, speed %s m/s: A %.1f s, B %.1f s' % (label(STAGE_AY, k[0]), label(STAGE_SPEED, k[1]), a[1][k], b[1][k]))
        for key, name in STAGES[2:]:
            x, y = a[0][k][key], b[0][k][key]
            lines.append('    %-12s A %10.4g  B %10.4g  A/B %s' % (name, x, y, ('%.3f' % (x / y)) if np.isfinite(x) and np.isfinite(y) and y else '.'))
    return lines
