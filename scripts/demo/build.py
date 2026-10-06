#!/usr/bin/env python3
"""Build the real-runs demo site (index.html + data.js) from copies of Oversteer's telemetry databases.

    build.py            build from the existing copies (flat.db, dev.db in the work folder)
    build.py --refresh  first copy the live databases (sqlite backup API, read-only on the source), work the copies
                        over with the current repo code (backfill: classes, corners, sections), then build

Never opens a live database for writing. Only the copies are migrated or backfilled.
The work folder is $OVERSTEER_DEMO_WORK (default ~/.cache/oversteer-demo): the database copies and site/ go there,
never into the repository. The page itself is template.html (copied to site/index.html); the numbers are site/data.js.
"""
import argparse
import bisect
import json
import math
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get('OVERSTEER_REPO', os.path.dirname(os.path.dirname(HERE)))
WORK = os.path.expanduser(os.environ.get('OVERSTEER_DEMO_WORK', '~/.cache/oversteer-demo'))
sys.path.insert(0, REPO)

from oversteer import coach, coach_context as cc, stage_tables, telemetry_store as ts  # noqa: E402

SOURCES = [  # (copy, live database, profile the coach reads)
    ('flat.db', os.path.expanduser('~/.var/app/io.github.berarma.Oversteer/data/oversteer/telemetry.db'), 'AC Rally'),
    ('dev.db', os.path.expanduser('~/.local/share/oversteer/telemetry.db'), 'ACR'),
]
STAGES_SHOWN = 4
FIRST_STAGE = 'acr:alsace:sommet-de-munster'      # the stage the page opens on
STEP = 5                      # m between samples
DATE = re.compile(r' on \d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?: \d{4})?')   # 'on 3 Oct' in a tip
DATE_THAN = re.compile(r'than \d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?: \d{4})?')   # 'than 5 Oct (169.8 s)'
CH = {n: i for i, n in enumerate(ts.TRACE_CHANNELS)}
G = 9.81


def refresh():
    from oversteer.shift_learner import ShiftLearner
    os.makedirs(WORK, exist_ok=True)
    for copy, live, profile in SOURCES:
        if not os.path.exists(live):
            continue
        src = sqlite3.connect('file:{}?mode=ro'.format(live), uri=True)
        dst = sqlite3.connect(os.path.join(WORK, copy))
        src.backup(dst)
        src.close()
        dst.close()
        learner = ShiftLearner(os.path.join(WORK, copy), profile=profile)       # migrates the copy
        # corners from before the section windows (no complex) are worked over again with the current code
        learner.backfill()
        for _ in range(2):          # (a run first worked over gets its windows on the second pass: observed)
            fix = sqlite3.connect(os.path.join(WORK, copy))
            fix.execute('UPDATE runs SET run_class = NULL WHERE id IN (SELECT run FROM corners WHERE complex IS NULL '
                        'AND run IN (SELECT run FROM traces))')
            fix.commit()
            fix.close()
            learner.backfill()
        learner.close()


def fin(x):
    return x is not None and not math.isnan(x)


def clean(x, default=0.0):
    return default if x is None or not fin(x) else x


def pick(rows, track, d, ch, nearest=False):
    """The channel at distance d, interpolated between the rows around it (NaN gaps give the neighbour)."""
    i = bisect.bisect_left(track, d)
    if i >= len(rows):
        i = len(rows) - 1
    if i == 0 or track[i] == track[i - 1]:
        return rows[i][ch]
    a, b = rows[i - 1][ch], rows[i][ch]
    if not fin(a) or not fin(b):
        return b if fin(b) else a
    f = (d - track[i - 1]) / (track[i] - track[i - 1])
    if nearest:
        return b if f >= 0.5 else a
    return a + (b - a) * f


def sample(rows, n):
    track = cc.along(rows)
    t0 = rows[0][CH['t']]
    out = {k: [] for k in ('t', 'v', 'thr', 'brk', 'steer', 'gear', 'rpm', 'alat', 'along')}
    for i in range(n + 1):
        d = i * STEP
        out['t'].append(int(round((pick(rows, track, d, CH['t']) - t0) * 100)))
        out['v'].append(int(round(clean(pick(rows, track, d, CH['speed'])) * 3.6)))
        out['thr'].append(int(round(100 * min(1, max(0, clean(pick(rows, track, d, CH['throttle'])))))))
        out['brk'].append(int(round(100 * min(1, max(0, clean(pick(rows, track, d, CH['brake'])))))))
        out['steer'].append(int(round(100 * min(1, max(-1, clean(pick(rows, track, d, CH['steer'])))))))
        out['gear'].append(int(round(clean(pick(rows, track, d, CH['gear'], True)))))
        out['rpm'].append(int(round(clean(pick(rows, track, d, CH['rpm'])) / 10)))
        out['alat'].append(int(round(100 * max(-1.8, min(1.8, clean(pick(rows, track, d, CH['a_lat'])) / G)))))
        out['along'].append(int(round(100 * max(-1.8, min(1.8, clean(pick(rows, track, d, CH['a_long'])) / G)))))
    return out


def dead_reckon(rows, n):
    """[xs, ys] per STEP m from speed and yaw rate (left turns positive), or None without a yaw channel."""
    if not any(fin(r[CH['yaw_rate']]) for r in rows):
        return None
    track = cc.along(rows)
    h = x = y = 0.0
    cum = [(track[0], 0.0, 0.0)]
    for i in range(1, len(rows)):
        dt = max(0.0, min(0.5, rows[i][CH['t']] - rows[i - 1][CH['t']]))
        h += clean(rows[i][CH['yaw_rate']]) * dt
        v = clean(rows[i][CH['speed']])
        x += v * math.cos(h) * dt
        y += v * math.sin(h) * dt
        cum.append((track[i], x, y))
    ds = [c[0] for c in cum]
    pts = []
    for k in range(n + 1):
        i = min(max(bisect.bisect_left(ds, k * STEP), 0), len(cum) - 1)
        pts.append((round(cum[i][1], 1), round(cum[i][2], 1)))
    return [[p[0] for p in pts], [p[1] for p in pts]]


def positions(rows, n):
    """(plan [xs, ys], heights) per STEP m from the game's own world positions (x, -z: x east, z south in the game's
    left-handed frame, so the plan is not mirrored), or None where the trace has no positions."""
    track = cc.along(rows)
    pts = []
    for i in range(n + 1):
        d = i * STEP
        pts.append(tuple(pick(rows, track, d, CH[c]) for c in ('x', 'z', 'y')))
    if sum(1 for p in pts if all(fin(v) for v in p)) < 0.95 * len(pts):
        return None
    xs, zs = [p[0] for p in pts if fin(p[0])], [p[1] for p in pts if fin(p[1])]
    if max(xs) - min(xs) < 100 and max(zs) - min(zs) < 100:
        return None
    x0, z0 = pts[0][0], pts[0][1]
    fill = lambda v, a: a if not fin(v) else v
    plan = [[round(fill(p[0], x0) - x0, 1) for p in pts], [round(-(fill(p[1], z0) - z0), 1) for p in pts]]
    return plan, [round(fill(p[2], pts[0][2]), 1) for p in pts]


def short_name(name):
    """'the 4 right at 3.0 km' -> '4 right' (the km is shown beside it)."""
    return name.replace('the ', '', 1).split(' at ')[0]


class Group:
    """One car's runs on one stage, read from one database copy."""

    def __init__(self, reader, profile, stage, car):
        self.reader, self.profile, self.stage, self.car = reader, profile, stage, car
        runs = reader.stage_runs(stage, limit=200, car=car)
        self.all = sorted(runs, key=lambda r: r['started'])
        self.done = [r for r in self.all if r['finished'] == 1 and r['result_time']]
        # A finished run the coach classed 'partial' only because its stage's finish is not learnt yet (the road length
        # is the pace notes' span) is a real run of the stage: read as clean for the page (first run: learning).
        for k, r in enumerate(self.done):
            if r['run_class'] == 'partial':
                r['run_class'] = 'learning' if k == 0 else 'clean'

    def course(self):
        return max((r['course'] or 0 for r in self.done), default=0)


def rows_of(reader, r):
    trace, corners = reader.trace(r['id']), reader.corners(r['id'])
    if not trace:
        return None
    return {'trace': cc.stage_rows(trace, r['course'], r['finished'] == 1, r['result_time']), 'corners': corners,
            'course': r['course'], 'run': r['id']}


def section_time(rows, a, b):
    track = cc.along(rows['trace'])
    x, y = cc.elapsed_at(rows['trace'], a, track), cc.elapsed_at(rows['trace'], b, track)
    return None if x is None or y is None else y - x


def sector_data(g, runs, rsets, L):
    """The game's own sectors of the stage as the splits of the page, or None where it has none good enough
    (coach.SECTOR_SPLITS) or a sector no run timed: ([{'name', 'start', 'end', 'conf'}], {run id: [seconds or None]},
    [best seconds]). The times are coach._sectors's: on the game's clock where the run has it, from where each
    distance crosses the line; the best is over the clean and learning runs of the stage."""
    entry = stage_tables.entry(g.stage)
    placed = stage_tables.sector_bounds(entry)
    if placed is None or placed['confidence'] not in coach.SECTOR_SPLITS:
        return None
    bounds = [(a - placed['start_m'], b - placed['start_m']) for a, b in placed['bounds']]

    def times(r, trace):
        origin = g.reader.run_start(r['id'])
        if origin is None:
            origin = stage_tables.run_origin(entry)
        shift = 0.0 if origin is None else origin - placed['start_m']
        got = cc.sector_times(trace, bounds, shift, g.reader.run_clock(r['id']) == 'game', r['result_time'])
        return [got.get(i) for i in range(len(bounds))]

    per, best = {}, [None] * len(bounds)
    # (a stage driven only with offs, as the demo's are, has its best over those: they are the demo's runs)
    pool = {r['id'] for r in g.done if r['run_class'] in ('clean', 'learning')} or {r['id'] for r in g.done}
    for r in g.done:
        rs = rsets.get(r['id']) or rows_of(g.reader, r)
        if rs is None:
            continue
        per[r['id']] = times(r, rs['trace'])
        if r['id'] in pool:
            for i, t in enumerate(per[r['id']]):
                if t is not None and (best[i] is None or t < best[i]):
                    best[i] = t
    if any(b is None for b in best):
        return None
    sectors = [{'name': 'S{}'.format(i + 1), 'start': round(min(a, L), 1), 'end': round(min(b, L), 1),
                'conf': placed['confidence']} for i, (a, b) in enumerate(bounds)]
    return sectors, {i: [None if t is None else round(t, 2) for t in v] for i, v in per.items()}, \
        [round(t, 2) for t in best]


def build_stage(g):
    reader = g.reader
    good = [r for r in g.done if r['run_class'] in ('clean', 'learning')]
    ranked = sorted(good or g.done, key=lambda r: r['result_time'])
    feat = ranked[0]
    nxt = ranked[1] if len(ranked) > 1 else None
    before = [r for r in g.done if r['started'] < feat['started']]
    prev = before[-1] if before else None
    first = g.done[0] if g.done[0]['id'] != feat['id'] else None
    if prev is not None and nxt is not None and prev['id'] == nxt['id']:
        prev = None             # the run before is the next best: one chip
        if first is not None and first['id'] != nxt['id']:
            prev, prev_label = first, 'first run'
    prev_label = locals().get('prev_label', 'previous')
    order = {r['id']: i + 1 for i, r in enumerate(g.done)}
    frows = rows_of(reader, feat)
    grid, bounds = cc.grid_of(frows)
    if not grid:
        return None
    needed = [feat] + [r for r in (nxt, prev) if r]
    rowsets = {r['id']: rows_of(reader, r) for r in needed}
    others = []          # for the best of each section, as coach.splits() loads them
    for r in g.done:
        if r['id'] != feat['id'] and r['run_class'] in ('clean', 'learning', 'off', 'partial'):
            rr = rowsets.get(r['id']) or rows_of(reader, r)
            if rr and rr['corners']:
                others.append(rr)
    st = cc.stitched(frows, others)
    nmin = min(int(cc.along(rowsets[r['id']]['trace'])[-1] // STEP) for r in needed)
    L = nmin * STEP
    sections = []
    for s, (a, b) in zip(grid, bounds):
        sections.append({'start': round(min(a, L), 1), 'end': round(min(b, L), 1), 'apex': round(s['apex'], 1),
                         'name': short_name(cc.section_name(s))})
    best = [None] * len(grid)
    if st:
        for j, v in st['best'].items():
            best[j] = round(v, 2)
    runs, rsets = {}, {}
    for r in needed:
        rs = rowsets[r['id']]
        ok = cc.grid_times(rs['trace'], rs['corners'], grid, bounds)
        d = {'n': order[r['id']], 'time': round(r['result_time'], 2), 'cls': r['run_class'],
             'sec': [None if x is None else round(x, 2) for x in (section_time(rs, a, b) for a, b in bounds)],
             'ok': [1 if j in ok else 0 for j in range(len(grid))]}
        d.update(sample(rs['trace'], nmin))
        runs[str(r['id'])], rsets[r['id']] = d, rs

    found = sector_data(g, runs, rsets, L)
    if found:
        for rid, v in found[1].items():
            if str(rid) in runs:
                runs[str(rid)]['sc'] = v

    def corner_cmp(r, ref_r):
        """Per grid section [dmin km/h, dexit km/h, dbrake m (+ earlier)] of r against ref_r; None where not compared."""
        a, b = rsets[r['id']], rsets[ref_r['id']]
        sa, sb = cc.sections_of(a['corners']), cc.sections_of(b['corners'])
        ia = {j: i for i, j in cc.match_sections(sa, grid).items()}
        ib = {j: i for i, j in cc.match_sections(sb, grid).items()}
        okA, okB = runs[str(r['id'])]['ok'], runs[str(ref_r['id'])]['ok']
        out = []
        for j in range(len(grid)):
            if not (okA[j] and okB[j] and j in ia and j in ib):
                out.append(None)
                continue
            c = cc.compare_section(sa[ia[j]], sb[ib[j]])
            out.append([None if c[k] is None else round(c[k] * m, 1) for k, m in (('speed', 3.6), ('exit', 3.6), ('brake', 1))])
        return out

    sa = cc.sections_of(rsets[feat['id']]['corners'])
    ia = {j: i for i, j in cc.match_sections(sa, grid).items()}
    ab = [[round(cc.key_corner(sa[ia[j]])['min_speed'] * 3.6), round(sa[ia[j]]['corners'][-1]['exit_speed'] * 3.6)]
          if j in ia else None for j in range(len(grid))]
    cmps = []
    if nxt:
        cmps.append({'id': str(nxt['id']), 'label': 'next best', 'cm': corner_cmp(feat, nxt)})
    if prev:
        cmps.append({'id': str(prev['id']), 'label': prev_label, 'cm': corner_cmp(feat, prev)})
    real = positions(rsets[feat['id']]['trace'], nmin)
    if real:
        xy, elev = real
    else:
        xy, elev = dead_reckon(rsets[(nxt or feat)['id']]['trace'], nmin), None
    cname = reader.car_row(g.car)[1].split('/', 1)[1]
    tips = []
    try:
        for t in coach.Coach(reader).tips(g.profile, g.car):
            tips.append({'kind': t.kind, 'text': DATE_THAN.sub('than your previous best', DATE.sub('', t.text))})
    except Exception as e:  # a car the coach cannot read still plays
        print('tips failed for', cname, e, file=sys.stderr)
    return {'name': (reader.stage(g.stage) or {}).get('name') or g.stage, 'length': L, 'car': cname,
            'feat': str(feat['id']), 'cmps': cmps, 'sections': sections, 'best': best, 'ab': ab,
            'sectors': found[0] if found else None, 'sbest': found[2] if found else None,
            'multi': len(ranked) > 1, 'runs': runs, 'xy': xy, 'pos': bool(real), 'elev': elev, 'tips': tips}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true')
    args = ap.parse_args()
    if args.refresh:
        refresh()
    cars = {c['name']: c for c in json.load(open(os.path.join(REPO, 'data/telemetry/cars/acr.json')))['cars']
            if isinstance(c, dict)}
    groups = []
    for copy, _live, profile in SOURCES:
        path = os.path.join(WORK, copy)
        if not os.path.exists(path):
            continue
        reader = ts.open_reader(path)
        for stage, car in reader._rows(
                "SELECT DISTINCT r.stage, s.car FROM runs r JOIN sessions s ON r.session = s.id "
                "WHERE s.game = 'acr' AND r.stage IS NOT NULL"):
            g = Group(reader, profile, stage, car)
            if g.done:
                groups.append(g)
    by_stage = {}        # per stage the car with the most finished runs (then the quickest)
    for g in groups:
        key = (len(g.done), -min(r['result_time'] for r in g.done))
        if g.stage not in by_stage or key > by_stage[g.stage][0]:
            by_stage[g.stage] = (key, g)
    chosen = sorted((v[1] for v in by_stage.values()),      # the first stage, then those with a comparison, longest first
                    key=lambda g: (g.stage != FIRST_STAGE, len(g.done) < 2, -g.course()))[:STAGES_SHOWN]
    stages = []
    for g in chosen:
        s = build_stage(g)
        if s is None:
            continue
        info = cars.get(s['car'], {})
        s['limiter'] = info.get('limiter_rpm', 7500)
        s['lights'] = info.get('shift_lights_rpm', {'prepare': 6700, 'shift': 7000, 'late': 7250})
        s['gears'] = len(info['gear_sets'][0]['gears']) if info.get('gear_sets') else 5
        stages.append(s)
    names = [s['name'] for s in stages]      # a tip about another shown stage is left out of this one
    for s in stages:
        s['tips'] = [t for t in s['tips'] if not any(n in t['text'] and n != s['name'] for n in names)]
    out = os.path.join(WORK, 'site')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'data.js'), 'w') as f:
        f.write('const DATA = ' + json.dumps({'stages': stages}, separators=(',', ':'), ensure_ascii=False) + ';\n')
    with open(os.path.join(HERE, 'template.html')) as f, open(os.path.join(out, 'index.html'), 'w') as o:
        o.write(f.read())
    size = sum(os.path.getsize(os.path.join(out, n)) for n in os.listdir(out))
    print('site/ {} bytes'.format(size))
    for s in stages:
        print(' ', s['name'], s['car'], s['length'], 'm', 'runs', {k: (v['n'], v['time'], v['cls']) for k, v in s['runs'].items()},
              'feat', s['feat'], 'tips', len(s['tips']), 'sections', len(s['sections']), 'sectors', len(s['sectors'] or ()))


if __name__ == '__main__':
    main()
