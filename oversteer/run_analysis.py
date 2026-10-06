"""Post-run analysis for the Telemetry > Run view, shared by the web page (telemetry_web) and the GTK tab
(gtk_run_view): a run and its comparison (the PB, the previous run, or any run) resampled to one distance grid,
the coach's sections with each one's time and loss against the comparison, and the coach's advice placed on
those sections. docs/telemetry-ui-design.md, sections 7.3 and 9.3 and the owner additions.

The traces are cut with coach_context.stage_rows (the run's course, its finish) and the sections are the
reference run's grid (coach_context.grid_of), exactly as coach.splits() reads them, so a section's time here is
the split time there and the delta trace agrees with the debrief. Nothing here writes: results are cached per
run (a trace is immutable; the cache key holds what a backfill may change, the run's course, time and class).
"""

import bisect
import math
import threading
import time
from collections import OrderedDict

import numpy as np

from . import coach, coach_context as cc, coach_diagnosis, potential, stage_tables
from .telemetry_formats import plan_xy

SLACK = 5.0                      # m a run may stop short of the grid's end, or start past its start, and still be drawn there
STEP = 2.0                       # m between the grid's points
CHANNELS = ('t', 'speed', 'throttle', 'brake', 'steer', 'gear', 'rpm', 'a_long', 'a_lat')
CACHE_SIZE = 24
ADVICE_TTL = 30.0                # s the coach's tips are kept (they are read from the whole history)
RUN_CLASSES = ('clean', 'learning', 'off', 'partial')     # what the run list holds: a restart is no run to study
_G = cc.G

_cache = OrderedDict()
_lock = threading.Lock()


def _remember(key, build):
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    value = build()
    with _lock:
        _cache[key] = value
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return value


def clear_cache():
    with _lock:
        _cache.clear()


def _signature(run):
    return (run['id'], run['course'], run['result_time'], run['run_class'], run['finished'])


def _ns(reader):
    """What tells one database from another in the cache: run ids are only unique within one."""
    return getattr(reader, 'namespace', None) or id(reader)


# -- the runs --

def _numbers(reader, session, cache):
    """{run id: n} of a session's runs (the number the game session counted them by)."""
    if session not in cache:
        cache[session] = {r['id']: r['n'] for r in reader.runs(session)}
    return cache[session]


def _label(row, n):
    day = time.strftime('%-d %b', time.localtime(row['started'])) if row.get('started') else ''
    return 'Run {} · {}'.format(n, day) if day else 'Run {}'.format(n)


def recent_runs(reader, car_id, limit=12, namespace=None):
    """The car's recent runs on the stage of its latest run, newest first: id, n, started, time, finished,
    run_class, wet, stage, stage_name, pb (the quickest finished clean run), trace (it has one). []
    without runs."""
    stage = None
    for session in reader.sessions(car_id, 6):
        found = [r for r in reader.runs(session['id']) if r['stage']]
        if found:
            stage = max(found, key=lambda r: (r['started'] or 0, r['id']))['stage']
            break
    if stage is None:
        return []
    rows = reader.stage_runs(stage, car=car_id, limit=60)
    classes = {r['id']: r['run_class'] for r in rows}
    ranked = reader.stage_runs(stage, car=car_id, limit=60, ranked=True)           # the PB among the runs that rank
    finished = [r for r in ranked if r['finished'] == 1 and r['result_time'] and r['run_class'] == 'clean']
    pb = min(finished, key=lambda r: r['result_time'])['id'] if finished else None
    shown = [r for r in rows if r['run_class'] in RUN_CLASSES][:limit]
    have = reader.trace_runs(r['id'] for r in shown)
    numbers, out = {}, []
    stage_row = reader.stage(stage) or {}
    for r in shown:
        out.append({'id': r['id'], 'n': _numbers(reader, r['session'], numbers).get(r['id']), 'started': r['started'],
                    'time': r['result_time'] if r['finished'] == 1 else None, 'finished': r['finished'],
                    'run_class': classes[r['id']], 'wet': r['wet'], 'stage': stage,
                    'stage_name': coach._stage_name(stage, stage_row), 'pb': r['id'] == pb,
                    'trace': r['id'] in have})
    return out


def _before(reader, run):
    """The stage's earlier runs of this car, newest first (as the coach's reference is picked from)."""
    return [r for r in reader.stage_runs(run['stage'], exclude=run['id'], limit=60, car=run['car'], ranked=True)
            if r['started'] <= run['started']]


def pb_run(reader, run, before=None):
    """The coach's reference for `run`: the quickest finished clean or learning run before it, or None."""
    before = _before(reader, run) if before is None else before
    return cc.reference_run([r for r in before if r['run_class'] in ('clean', 'learning')], wet=run['wet'])


def previous_run(reader, run, before=None):
    """The run before `run` on the stage that finished (not a partial one: its time holds the slow-down to the
    stop control), or None."""
    before = _before(reader, run) if before is None else before
    return next((r for r in before if r['finished'] == 1 and r['result_time'] and r['run_class'] != 'partial'), None)


def candidates(reader, run):
    """{'pb': the PB run row or None, 'prev': the previous run's row or None}, rows as stage_runs gives them."""
    before = _before(reader, run)
    return {'pb': pb_run(reader, run, before), 'prev': previous_run(reader, run, before)}


def resolve(reader, run, vs):
    """(kind, row) of the comparison `vs` names: 'pb', 'prev' or a run id; (kind, None) where there is none."""
    if vs in (None, '', 'pb'):
        return 'pb', candidates(reader, run)['pb']
    if vs == 'prev':
        return 'prev', candidates(reader, run)['prev']
    try:
        other = reader.run(int(vs))
    except (TypeError, ValueError):
        return 'pb', candidates(reader, run)['pb']
    if other is None or other['stage'] != run['stage'] or other['id'] == run['id']:
        return 'run', None
    return 'run', other


def head(reader, run_id, tips=None, namespace=None):
    """One run's header: id, n, started, time, finished, run_class, stage, stage_name, car, car_name, wet,
    compare (the PB and the previous run as {id, run, n, time, label, delta}: delta is this run's time less
    theirs), `delta_pb`, `limiter` (rpm, from the car's model), `game`, and `advice` (the coach's tips about this run, as Tip.to_dict() gives them, when
    `tips` -- all of the car's tips -- is given). None for a run that does not exist."""
    run = reader.run(run_id)
    if run is None:
        return None
    numbers = {}
    car = reader.car_by_id(run['car']) if run['car'] is not None else None
    stage_row = reader.stage(run['stage']) if run['stage'] else None
    mine = run['result_time'] if run['finished'] == 1 else None
    found = candidates(reader, run) if run['stage'] else {'pb': None, 'prev': None}
    compare = []
    for kind in ('pb', 'prev'):
        other = found[kind]
        entry = {'id': kind, 'run': None, 'n': None, 'time': None, 'label': None, 'delta': None}
        if other is not None:
            n = _numbers(reader, other['session'], numbers).get(other['id'])
            entry.update(run=other['id'], n=n, time=other['result_time'], label=_label(other, n),
                         delta=None if mine is None else mine - other['result_time'])
        compare.append(entry)
    advice = [t for t in (tips or []) if (t.get('place') or {}).get('run') == run_id]
    return {'id': run['id'], 'n': _numbers(reader, run['session'], numbers).get(run['id']), 'started': run['started'],
            'time': mine, 'finished': run['finished'], 'run_class': run['run_class'], 'stage': run['stage'],
            'stage_name': coach._stage_name(run['stage'], stage_row) if run['stage'] else None,
            'car': run['car'], 'car_name': (car or {}).get('name') or (car or {}).get('key'), 'wet': run['wet'],
            'limiter': ((car or {}).get('model') or {}).get('limiter'), 'game': (car or {}).get('game'),
            'compare': compare, 'delta_pb': compare[0]['delta'], 'advice': advice,
            'potential': stage_potential(reader, run)}


def stage_potential(reader, run):
    """The stage's potential for the run's car as the views show it: {user, grip, car (s, in that order), runs,
    built, sections (potential.stage()'s: per section its distances, apex, grade, the three layers' seconds and the
    PB's columns)}, or None where there is none."""
    if not run.get('stage') or run.get('car') is None:
        return None
    pot = potential.stored(reader, run['stage'], run['car'])
    if pot is None:
        return None
    return dict(potential.layers(pot, _sum_of_best(reader, run['stage'], run['car'], pot)), runs=pot['runs'],
                built=pot['built'], sections=pot['sections'])


def _sum_of_best(reader, stage, car, pot):
    """The splits' sum of best of the stage and car (the user layer, the same number as the coach's and the splits
    sheet's), cached with the stored potential's version (the runs it was built from)."""
    key = (_ns(reader), 'sob', stage, car, pot['version'])
    return _remember(key, lambda: potential.stage_sob(reader, stage, car))


def car_tips(reader, profile, car_id, namespace=None):
    """The coach's tips for the car as dicts (cached ADVICE_TTL s; nothing is marked as seen)."""
    rows = reader.sessions(car_id, 1)
    newest = rows[0]['id'] if rows else None
    key = (namespace or _ns(reader), 'tips', profile, car_id, newest, int(time.time() / ADVICE_TTL))
    return _remember(key, lambda: [t.to_dict() for t in coach.Coach(reader).tips(profile, car_id)])


# -- the traces on a distance grid --

def _fin(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def stage_trace(reader, run):
    """The run's rows that are the stage (coach_context.stage_rows), or None without a trace."""
    trace = reader.trace(run['id'])
    if not trace:
        return None
    rows = cc.stage_rows(trace, run['course'], run['finished'] == 1, run['result_time'])
    return rows or None


def grid_points(length, step=STEP):
    n = int(math.floor(length / step))
    points = [i * step for i in range(n + 1)]
    if length - points[-1] > 1e-6:
        points.append(length)
    return points


def resample(rows, grid, channels=CHANNELS):
    """{channel: [value at each grid distance]} for the stage rows: linear between rows, None where the run
    was not there (before its first row, past its last) or the channel was not sent; the gear is the nearer
    row's. `t` is seconds from the run's first row; speed is km/h, the pedals and steer as the trace has
    them, rpm, a_long and a_lat in g."""
    track = cc.along(rows)
    base = rows[0][cc.CH['t']]
    index = {name: cc.CH[name] for name in channels if name != 't'}
    out = {name: [None] * len(grid) for name in channels}
    for k, g in enumerate(grid):
        i = bisect.bisect_left(track, g)
        if i >= len(rows):
            if g > track[-1] + SLACK:
                continue
            i0 = i1 = len(rows) - 1
            w = 0.0
        elif i == 0:
            if g < track[0] - SLACK:
                continue
            i0 = i1 = 0
            w = 0.0
        else:
            a, b = track[i - 1], track[i]
            i0, i1 = i - 1, i
            w = 1.0 if a == float('-inf') else (g - a) / (b - a)
        r0, r1 = rows[i0], rows[i1]
        for name in channels:
            if name == 't':
                v0, v1 = r0[cc.CH['t']] - base, r1[cc.CH['t']] - base
            else:
                v0, v1 = r0[index[name]], r1[index[name]]
            if name == 'gear':
                v = v0 if w < 0.5 else v1
                out[name][k] = int(round(v)) if _fin(v) else None
                continue
            if not (_fin(v0) and _fin(v1)):
                continue
            v = v0 + (v1 - v0) * w
            if name == 'speed':
                v *= 3.6
            elif name in ('a_long', 'a_lat'):
                v /= _G
            out[name][k] = v
    return out


_ROUND = {'t': 3, 'speed': 1, 'throttle': 2, 'brake': 2, 'steer': 2, 'gear': 0, 'rpm': 0, 'a_long': 2, 'a_lat': 2}


def _rounded(columns):
    out = {}
    for name, values in columns.items():
        dp = _ROUND.get(name, 1)
        out[name] = [None if v is None else (int(round(v)) if dp == 0 else round(v, dp)) for v in values]
    return out


def _short(section):
    """'L-R', '3L', 'H R': the section's corners by side and tightness."""
    parts = []
    for k in section['corners']:
        side = 'L' if (k.get('direction') or 0) > 0 else 'R'
        call = k.get('tightness')
        parts.append(('H ' if call == 'hairpin' else '{}'.format(call or '')) + side)
    return '-'.join(parts)


def _positions(rows, grid, game=None):
    """(x, z) lists on the grid, or (None, None) where the run has no position. They are the plan's axes whatever
    the game's world (telemetry_formats.plan_xy): ACR's z is turned over, WRC Generations' height is z."""
    if not any(_fin(r[cc.CH['x']]) and _fin(r[cc.CH['z']]) for r in rows):
        return None, None
    track = cc.along(rows)
    xs, zs = [], []
    for g in grid:
        i = bisect.bisect_left(track, g)
        if i >= len(rows) and g > track[-1] + SLACK or (i == 0 and g < track[0] - SLACK):
            xs.append(None)
            zs.append(None)
            continue
        i = min(i, len(rows) - 1)
        i0 = max(0, i - 1)
        a, b = track[i0], track[i]
        w = 0.0 if i == i0 or b == a or a == float('-inf') else (g - a) / (b - a)
        pts = []
        for name in ('x', 'y', 'z'):
            v0, v1 = rows[i0][cc.CH[name]], rows[i][cc.CH[name]]
            pts.append(v0 + (v1 - v0) * w if _fin(v0) and _fin(v1) else None)
        pts = plan_xy(game, pts) if None not in pts else (None, None)
        xs.append(None if pts[0] is None or pts[1] is None else round(pts[0], 1))
        zs.append(None if pts[0] is None or pts[1] is None else round(pts[1], 1))
    return xs, zs


def _sector_rows(stage, mine, other, reader=None, runs=(None, None)):
    """The game's sectors timed on both runs, [{name, d0, d1, time, ref_time, delta}], or None. Each run (`runs`:
    this one's and the other's rows from the store) is placed from where it began and, on the game's own clock,
    timed by it where its distance crosses each line (coach_context.sector_times)."""
    entry = stage_tables.entry(stage) if stage else None
    placed = stage_tables.sector_bounds(entry) if stage else None
    if placed is None:
        return None
    bounds = [(a - placed['start_m'], b - placed['start_m']) for a, b in placed['bounds']]
    found = []
    for rows, run in zip((mine, other), runs):
        if not rows:
            found.append({})
            continue
        origin = reader.run_start(run['id']) if reader is not None and run else None
        if origin is None:
            origin = stage_tables.run_origin(entry)
        found.append(cc.sector_times(rows, bounds, 0.0 if origin is None else origin - placed['start_m'],
                                     reader is not None and run is not None and reader.run_clock(run['id']) == 'game'))
    out = []
    for i, (a, b) in enumerate(bounds):
        times = [f.get(i) for f in found]
        out.append({'name': 'S{}'.format(i + 1), 'd0': a, 'd1': b, 'time': times[0], 'ref_time': times[1],
                    'delta': None if None in times else times[0] - times[1], 'confidence': placed['confidence']})
    return out if any(r['time'] is not None for r in out) else None


def _brake_deltas(reader, run, rows, other, other_rows, other_corners, report, pot):
    """{section index of `run` (its stored sections): metres its braking began earlier than the comparison's
    (negative: later)} on the road, as the coach's diagnosis measures it (coach._diagnose): this run against the
    comparison over the comparison's own spans, the first corner of the section that either braked for. Left out
    where either run did not brake there or a trace is too short."""
    if not report or not other or not other_rows:
        return {}
    mine, theirs = potential.arrays(rows), potential.arrays(other_rows)
    if mine is None or theirs is None:
        return {}
    spans = {g['id']: (g, a, b, lo, floor) for g, a, b, lo, floor in cc.spans(
        {'trace': other_rows, 'corners': other_corners, 'course': other.get('course')})}
    prof = pot['profile'] if pot else None
    env = {'bins': np.asarray(prof['env_bins'], dtype=float), 'lat': np.asarray(prof['env_lat'], dtype=float)} \
        if prof and prof.get('env_bins') and prof.get('env_lat') else None
    car = reader.car_by_id(run['car']) if run['car'] is not None else None
    out = {}
    for i, item in enumerate(report):
        span = spans.get((item['ref'] or {}).get('id'))
        if span is None:
            continue
        g, a, b, lo, floor = span
        parts = coach_diagnosis.measures_section(mine, theirs, {
            'a': a, 'b': b, 'lo': lo, 'floor': floor, 'corners': g['corners'], 'game': (car or {}).get('game')}, env)
        found = [m['onset_dd'] for _, m in parts if m['onset_dd'] is not None]
        if found:
            out[i] = -found[0]
    return out


def _section_rows(reader, run, rows, other, other_rows, other_corners, grid_run, pot=None):
    """The coach's sections for the run: against `other` where there is one (its times, loss, min, exit and
    brake against it), on the grid of `grid_run` (the PB run's grid, the splits' sections) where there is one,
    else the run's own sections. With the stage's stored potential `pot` (oversteer/potential.py) each section also
    has `grip_s` (the seconds the grip layer takes through it), `car_s` and `avail` (the run's time less the grip
    layer's: the time available), None where the run has no time there or an off touched it."""
    corners = reader.corners(run['id'])
    track = cc.along(rows)
    mine = cc.sections_of(corners)
    if grid_run is not None:
        ref_rows = grid_run['rows']
        grid = cc.sections_of(grid_run['corners'])
        bounds = cc.section_bounds(grid, cc.along(ref_rows)[0] if ref_rows else 0.0,
                                   grid_run['course'] or cc.along(ref_rows)[-1])
    else:
        grid = mine
        bounds = cc.section_bounds(grid, track[0], track[-1]) if grid else []
    if not grid:
        return []
    matches = cc.match_sections(mine, grid) if mine else {}
    report = cc.section_report(corners, other_corners) if other_corners and mine else None
    braked = _brake_deltas(reader, run, rows, other, other_rows, other_corners, report, pot)
    by_grid = {}
    for i, j in matches.items():
        by_grid[j] = (mine[i], report[i] if report else None, braked.get(i))
    other_track = cc.along(other_rows) if other_rows else None
    offs = set()
    for i, s in enumerate(mine):
        if s['off'] and i in matches:
            offs.add(matches[i])
    out = []
    last = len(grid) - 1
    for j, (a, b) in enumerate(bounds):
        s = grid[j]
        ta, tb = cc.elapsed_at(rows, a, track), cc.elapsed_at(rows, b, track)
        time_ = tb - ta if ta is not None and tb is not None else None
        ref_time = None
        if other_rows:
            oa, ob = cc.elapsed_at(other_rows, a, other_track), cc.elapsed_at(other_rows, b, other_track)
            ref_time = ob - oa if oa is not None and ob is not None else None
        mine_section, item, dbrake = by_grid.get(j, (None, None, None))
        row = {'i': j, 'name': cc.section_name(s), 'short': _short(s), 'd0': a, 'd1': b, 'apex': s['apex'],
               'time': time_, 'ref_time': ref_time,
               'loss': None if time_ is None or ref_time is None else time_ - ref_time,
               'first': j == 0, 'last': j == last,
               'off': bool(s['off']) or j in offs or bool(item and item['ref_off'])}
        if pot is not None:
            grip_s, car_s = potential.span_times(pot, a, b)
            row['grip_s'], row['car_s'] = grip_s, car_s
            row['avail'] = None if time_ is None or row['off'] else time_ - grip_s
        if mine_section is not None:
            key = cc.key_corner(mine_section)
            ends = mine_section['corners'][-1]
            row['min'] = None if key.get('min_speed') is None else key['min_speed'] * 3.6
            row['exit'] = None if ends.get('exit_speed') is None else ends['exit_speed'] * 3.6
        if item is not None and item['compare'] is not None:
            c = item['compare']
            row['dmin'] = None if c['speed'] is None else c['speed'] * 3.6
            row['dexit'] = None if c['exit'] is None else c['exit'] * 3.6
            row['dbrake'] = dbrake
            row['pattern'] = item['pattern']
        out.append(row)
    return out


def analysis(reader, run_id, vs='pb', step=STEP, namespace=None):
    """The Run view's data for `run_id` against `vs` ('pb', 'prev' or a run id):
    {run, vs, ref ({id, n, time, label} or None), step, length, limiter (rpm), time, ref_time, channels, this, cmp, x, z, sections,
    sectors}: `this` and `cmp` (None without a comparison) map each of CHANNELS to its values on the grid (points `step` m apart from 0), `x`
    and `z` are this run's position (None where it has none), `sections` the coach's sections (_section_rows) and
    `sectors` the game's, and `potential` (the stage's three layers, user / grip / car seconds, or None). None for an
    unknown run or one without a trace. Cached per run and comparison."""
    run = reader.run(run_id)
    if run is None or not run['stage']:
        return None
    kind, other = resolve(reader, run, vs)
    ns = namespace or _ns(reader)
    pot = potential.stored(reader, run['stage'], run['car'])
    key = (ns, 'trace', _signature(run), kind, _signature(other) if other else None, float(step),
           pot['version'] if pot else None)

    def build():
        rows = stage_trace(reader, run)
        if rows is None:
            return None
        track = cc.along(rows)
        length = track[-1]
        grid = grid_points(length, step)
        other_rows = stage_trace(reader, other) if other else None
        other_corners = reader.corners(other['id']) if other and other_rows else []
        pb = other if kind == 'pb' else pb_run(reader, run)
        pb_rows = (other_rows if kind == 'pb' else stage_trace(reader, pb)) if pb else None
        grid_run = {'rows': pb_rows, 'corners': reader.corners(pb['id']), 'course': pb['course']} \
            if pb is not None and pb_rows else None
        this = _rounded(resample(rows, grid))
        ref = _rounded(resample(other_rows, grid)) if other_rows else None
        xs, zs = _positions(rows, grid, run['stage'].partition(':')[0])
        numbers = {}
        info = None
        if other_rows:
            n = _numbers(reader, other['session'], numbers).get(other['id'])
            info = {'id': other['id'], 'n': n, 'time': other['result_time'], 'label': _label(other, n)}
        car = reader.car_by_id(run['car']) if run['car'] is not None else None
        return {'run': run['id'], 'vs': kind, 'ref': info, 'step': step, 'length': length,
                'limiter': ((car or {}).get('model') or {}).get('limiter'),
                'time': run['result_time'] if run['finished'] == 1 else None,
                'ref_time': other['result_time'] if other_rows else None, 'channels': list(CHANNELS),
                'this': this, 'cmp': ref, 'x': xs, 'z': zs,
                'potential': potential.layers(pot, _sum_of_best(reader, run['stage'], run['car'], pot)) if pot else None,
                'sections': _section_rows(reader, run, rows, other, other_rows, other_corners, grid_run, pot),
                'sectors': _sector_rows(run['stage'], rows, other_rows, reader, (run, other))}
    return _remember(key, build)


# -- the advice on the sections --

def advice_by_section(advice, sections):
    """{section index: [tip]}: each tip (Tip.to_dict() with a `place`) goes to the section its place falls in
    (`d`, or each of `ds` for a tip about several sections, between the section's bounds; the last section
    takes whatever is past the end). Tips with no place are left out."""
    out = {}
    if not sections:
        return out
    for tip in advice or []:
        place = tip.get('place')
        if not place:
            continue
        for d in place.get('ds') or [place.get('d')]:
            if d is None:
                continue
            for s in sections:
                if s['d0'] <= d < s['d1'] or (s['last'] and d >= s['d0']):
                    if tip not in out.setdefault(s['i'], []):
                        out[s['i']].append(tip)
                    break
    return out


def advice_line(tip):
    """(cost text, kind, place text, sentence) of a tip in the debrief's style: '+0.8', 'tip', '0.51 km'."""
    place = tip.get('place') or {}
    cost = tip.get('cost') or 0.0
    kind = tip.get('kind') or 'tip'
    sign = '' if not cost else ('−' if kind == 'praise' else '+') + '{:.1f}'.format(abs(cost))
    km = '{:.2f} km'.format(place['d'] / 1000.0) if place.get('d') is not None else ''
    return sign, kind, km, tip.get('text') or ''


def biggest_loss(sections):
    """The index of the section that lost the most (the first and last sections, which hold the launch and the finish, and any beside an off left out), or None."""
    best = None
    for s in sections or []:
        if s['loss'] is None or s['first'] or s['last'] or s['off']:
            continue
        if best is None or s['loss'] > best['loss']:
            best = s
    return None if best is None or best['loss'] <= 0 else best['i']


def loss_rows(sections, limit=None):
    """The 'where the time went' rows, biggest loss first: the sections with a time against the comparison."""
    rows = [s for s in sections or [] if s['loss'] is not None]
    rows.sort(key=lambda s: -s['loss'])
    return rows[:limit] if limit else rows
