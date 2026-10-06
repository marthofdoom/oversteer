"""Run the diagnosis prototype over every compared section of a store's runs on a stage and car, beside the coach's
current advice (coach._how + coach._section_action, and potential.call for the top-3 places), and check it against
the runs that came after (did the next quicker pass do what the fix said?).

    python3 research/diagnosis/validate.py STORE.db [out.json]

STORE.db is a replay of the captures or a backup copy of a live database (never the live file). Read-only.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

from oversteer import coach, coach_context as cc, potential  # noqa: E402
from oversteer.telemetry_store import open_reader  # noqa: E402
import diagnose as dg  # noqa: E402

STAGES = ('acr:wales:afon-bidno-severn', 'acr:alsace:sommet-de-munster')
CAR_LIKE = '%i20N%'
JUDGED = ('clean', 'off', 'partial')


def load(reader, run):
    trace = reader.trace(run['id'])
    if not trace:
        return None
    rows = cc.stage_rows(trace, run['course'], run['finished'] == 1, run['result_time'])
    return {'rows': rows, 'arr': potential.arrays(rows), 'corners': reader.corners(run['id']),
            'bad': [(e['d0'], e['d1'] if e['d1'] is not None else e['d0']) for e in reader.events(run['id'])
                    if e['kind'] in ('off', 'spin', 'stall', 'hit') and e['d0'] is not None]}


def grid_sections(ref, ref_data):
    """The reference's grid as section_loss times it: [(g, a, b, lo)]."""
    grid = cc.sections_of(ref_data['corners'])
    track = cc.along(ref_data['rows'])
    bounds = cc.section_bounds(grid, track[0], ref.get('course') or track[-1])
    out = []
    for j, (g, (a, b)) in enumerate(zip(grid, bounds)):
        b2 = min(b, cc.exit_end(ref_data['rows'], track, g))
        prev_apex = grid[j - 1]['apex'] if j else track[0]
        out.append((g, a, b2, max(a - dg.APPROACH, prev_apex + 5.0, track[0])))
    return grid, out


def floor_of(gsec, j):
    """Where braking may be looked for back to: the slowest point of the section before."""
    return gsec[j - 1][0]['apex'] + 5.0 if j else gsec[j][3]


def old_items(corners, ref_corners, net=True, keep_unlossed=False):
    """The coach's section items, with the net-of-the-gain-before adjustment _stage_place makes, and the text it
    would say for each (whether or not it is in the top SECTION_TIPS)."""
    report = cc.section_report(corners, ref_corners)
    out = []
    for n, it in enumerate(report):
        if it['compare'] is None or it['first'] or it['ref_off'] or it['last']:
            continue
        if not keep_unlossed and (it['loss'] is None or it['off']):
            continue
        if it['loss'] is None or not net:
            out.append(dict(it, old_text=None))
            continue
        prev = report[n - 1] if n else None
        if (it['loss'] > 0 and prev is not None and prev['loss'] is not None and not prev['off']
                and not prev['first'] and prev['loss'] <= -cc.SECTION_MIN):
            net = it['loss'] + prev['loss']
            it = dict(it, loss=net, gained_before=-prev['loss'], pattern=(
                cc.section_pattern(it['compare'], net) if net >= cc.SECTION_MIN else None))
        c = it['compare']
        text = None
        if it['loss'] >= cc.SECTION_MIN:
            how = coach._how(c, late_throttle=it['pattern'] == 'late-throttle') or [
                'were within a few km/h and metres of it']
            text = 'you {}. {}'.format(coach._say(how), coach._section_action(it['pattern'], c, it))
        elif it['loss'] <= -cc.SECTION_MIN:
            text = 'praise: you {}.'.format(coach._say(coach._how(c))) if coach._how(c) else 'praise'
        out.append(dict(it, old_text=text))
    return out


def env_of(pot):
    if pot is None:
        return None
    p = pot['profile']
    return {'bins': np.asarray(p['env_bins']), 'lat': np.asarray(p['env_lat'])}


def pot_calls(pot, data):
    """{apex_d: potential.call text} of a run's sections with time available (as the top-3 would say them)."""
    if pot is None or data['arr'] is None:
        return []
    rows = potential.analyse_run(pot, data['arr'], data['bad'])
    out = []
    for r in rows:
        if r['available'] is None or r['available'] < potential.AVAILABLE_MIN:
            continue
        terms = potential.critique(r, r.get('best_gear'), None)
        out.append((r['d0'], r['d1'], r['apex_d'], r['available'], r['cause'], potential.call(r, terms[0][1] if terms else None)))
    return out


def jsonable(x):
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items() if not k.startswith('_') and k not in ('section', 'ref', 'compare')}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.floating, np.integer, np.bool_)):
        return x.item()
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


OLD_FIX = {'overdriven': 'brake-earlier', 'over-slowing': 'brake-later', 'under-committed': 'carry-speed',
           'slower': 'carry-speed', 'late-throttle': 'throttle-sooner', 'over-rotated': 'rotate-less'}
NEW_FIX = {'OVERSHOT': 'brake-earlier', 'OVER-SLOWED': 'brake-less', 'EARLY-BRAKE': 'brake-later',
           'LATE-THROTTLE': 'throttle-sooner', 'SLIDE': 'straighter', 'COASTING': 'no-coast', 'GEAR': 'gear',
           'EARLY-APEX': 'later-apex', 'AT-LIMIT': 'line', 'EXIT-BRAKE': 'no-exit-brake'}


def followed(fix, m):
    """Whether the change m (measures of the quicker later pass against the diagnosed one) did what `fix` said."""
    if m is None:
        return None
    o = m['onset_dd']
    if fix == 'brake-earlier':
        return (o is not None and o <= -dg.BRAKE_SAME) or m['turnin_dv'] <= -dg.SPEED_SAME
    if fix == 'brake-later':
        return o is not None and o >= dg.BRAKE_SAME
    if fix == 'brake-less':                       # the same or later braking point, more speed carried
        return (o is None or o > -dg.BRAKE_SAME) and m['min_dv'] >= dg.SPEED_SAME
    if fix == 'carry-speed':
        return m['min_dv'] >= dg.SPEED_SAME
    if fix == 'throttle-sooner':
        return m['pickup_dd'] is not None and m['pickup_dd'] <= -dg.THROTTLE_LATER
    if fix == 'straighter':
        return m['counter_d'] is not None and m['counter_d'] <= -dg.COUNTER_MORE / 2
    if fix == 'no-coast':
        return m['coast_ds'] <= -dg.COAST_LONGER / 2
    if fix == 'later-apex':
        return m['min_dd'] >= dg.APEX_SHIFT / 2
    if fix == 'no-exit-brake':
        return m['exit_brake_x'] - m['exit_brake_r'] <= -dg.SHED_MORE
    if fix == 'gear':
        return m['gear_exit_x'] != m['gear_exit_r']
    return None


def old_text(pattern, c, loss, entry, exit_):
    """The coach's words for a losing section (coach._how + coach._section_action), or None under SECTION_MIN."""
    if loss < cc.SECTION_MIN:
        return None
    how = coach._how(c, late_throttle=pattern == 'late-throttle') or ['were within a few km/h and metres of it']
    return 'you {}. {}'.format(coach._say(how), coach._section_action(pattern, c, {'loss': loss, 'entry': entry,
                                                                                    'exit': exit_}))


def main(db, out_path=None, mode='coach'):
    """mode 'coach': each judged run against the reference the coach picked when it ended (the earlier best), with
    the coach's own stored loss; 'pb': every run with a trace (restarts and partials too, on the sections they
    covered without an off) against the car's quickest clean run on the stage, with the loss measured here."""
    reader = open_reader(db)
    results = []
    for stage in STAGES:
        cars = reader._rows("SELECT DISTINCT s.car FROM runs r JOIN sessions s ON s.id = r.session JOIN cars c "
                            "ON c.id = s.car WHERE r.stage = ? AND c.name LIKE ?", (stage, CAR_LIKE))
        for (car,) in cars:
            pot = potential.stored(reader, stage, car)
            env = env_of(pot)
            runs = sorted(reader.stage_runs(stage, limit=200, car=car, ranked=True), key=lambda r: r['started'])
            cache = {}

            def get(r):
                if r['id'] not in cache:
                    cache[r['id']] = load(reader, r)
                return cache[r['id']]
            clean = [r for r in runs if r['run_class'] == 'clean' and r['finished'] == 1 and r['result_time']]
            pb = min(clean, key=lambda r: r['result_time']) if clean else None
            pairs = []
            for n, run in enumerate(runs):
                before = [r for r in runs[:n] if r['started'] <= run['started']]
                if mode == 'coach':
                    if run['run_class'] not in JUDGED:
                        continue
                    ref = cc.reference_run([r for r in before if r['run_class'] in ('clean', 'learning')],
                                           wet=run['wet'])
                    pairs += [(run, ref)] if ref is not None else []
                elif mode == 'pb':
                    pairs += [(run, pb)] if pb is not None and run['id'] != pb['id'] else []
                elif run['run_class'] in JUDGED and run['finished'] == 1:          # 'pairs'
                    pairs += [(run, r) for r in before if r['run_class'] in ('clean', 'learning') and r['finished'] == 1
                              and r['result_time'] and r['result_time'] < run['result_time']]
            for run, ref in pairs:
                X, R = get(run), get(ref)
                if X is None or R is None or not X['corners'] or not R['corners'] or X['arr'] is None:
                    continue
                grid, gsec = grid_sections(ref, R)
                by_id = {g['id']: k for k, (g, *_r) in enumerate(gsec)}
                calls = pot_calls(pot, X)
                track = cc.along(X['rows'])
                for it in old_items(X['corners'], R['corners'], net=mode == 'coach', keep_unlossed=mode != 'coach'):
                    j = by_id.get(it['ref']['id'])
                    if j is None:
                        continue
                    g, a, b, lo = gsec[j]
                    if mode != 'coach' and (track[-1] < b or any(lo_ - 100 < b and hi_ + 100 > a for lo_, hi_ in X['bad'])):
                        continue
                    parts = dg.measures_section(X['arr'], R['arr'], {'a': a, 'b': b, 'lo': lo, 'floor': floor_of(gsec, j),
                                                                     'corners': g['corners']}, env)
                    if not parts:
                        continue
                    m = dg.measures(X['arr'], R['arr'], {'a': a, 'b': b, 'lo': lo, 'key': cc.key_corner(g)}, env)
                    if m is None:
                        continue
                    if mode != 'coach':
                        loss, entry, exit_ = m['loss'], m['t_approach'] + m['t_brake'], m['t_exit']
                        pattern = cc.section_pattern(it['compare'], loss)
                        text = old_text(pattern, it['compare'], loss, entry, exit_)
                    else:
                        loss, entry, exit_, pattern, text = it['loss'], it['entry'], it['exit'], it['pattern'], it['old_text']
                    d = dg.diagnose_section(parts, loss)
                    d['loss'] = loss
                    m = d['m']
                    where = it['name'] if len(parts) == 1 else '{} ({} at {:.2f} km)'.format(
                        it['name'], cc.corner_name(d['corner']).split(' at ')[0], d['corner']['d'] / 1000.0)
                    pc = [c for c in calls if a <= c[2] <= b]
                    results.append({'mode': mode, 'stage': stage, 'car': car, 'run': run['id'],
                                    'run_class': run['run_class'], 'run_time': run['result_time'],
                                    'ref_run': ref['id'], 'ref_time': ref['result_time'], 'grid': j,
                                    'name': it['name'], 'apex': g['apex'], 'a': a, 'b': b,
                                    'loss': loss, 'loss_entry': entry, 'loss_exit': exit_,
                                    'old_pattern': pattern, 'old_text': text,
                                    'old_brake': it['compare']['brake'], 'old_speed': it['compare']['speed'],
                                    'old_exit': it['compare']['exit'], 'old_entry': it['compare']['entry'],
                                    'pot_calls': [c[5] for c in pc], 'pot_causes': [c[4] for c in pc],
                                    'diag': d['code'], 'confidence': d['confidence'], 'evidence': d['evidence'],
                                    'facts': d['facts'], 'fix': d['fix'],
                                    'parts': d['parts'], 'part_loss': d['part_loss'], 'parts_d': round(d['corner']['d']),
                                    'new_text': dg.sentence(where, d, 'your best run'), 'm': m})
            # follow-through: for each diagnosed loss, the next later run quicker through the same grid section
            # (on the diagnosed run's reference's grid) by LOSS_MIN or more, measured against the diagnosed run
            for r in [x for x in results if x['stage'] == stage and x['car'] == car and x['loss'] >= dg.LOSS_MIN]:
                ref_run = next(x for x in runs if x['id'] == r['ref_run'])
                grid, gsec = grid_sections(ref_run, get(ref_run))
                g, a, b, lo = gsec[r['grid']]
                arun = next(x for x in runs if x['id'] == r['run'])
                A = get(arun)
                track = cc.along(A['rows'])
                e0, e1 = cc.elapsed_at(A['rows'], a, track), cc.elapsed_at(A['rows'], b, track)
                if e0 is None or e1 is None:
                    continue
                ta = e1 - e0
                for x in [x for x in runs if x['started'] > arun['started']]:
                    B = get(x)
                    if B is None or B['arr'] is None:
                        continue
                    if any(lo_ - 100 < b and hi_ + 100 > a for lo_, hi_ in B['bad']):
                        continue
                    tb = cc.along(B['rows'])
                    if tb[-1] < b:
                        continue
                    e0, e1 = cc.elapsed_at(B['rows'], a, tb), cc.elapsed_at(B['rows'], b, tb)
                    if e0 is None or e1 is None:
                        continue
                    if (e1 - e0) <= ta - dg.LOSS_MIN:
                        corner = next((k for k in g['corners'] if round(k['d']) == r['parts_d']), cc.key_corner(g))
                        parts2 = dg.measures_section(B['arr'], A['arr'], {'a': a, 'b': b, 'lo': lo, 'floor': floor_of(gsec, r['grid']),
                                                                          'corners': g['corners']}, env)
                        m2 = next((mm for kk, mm in parts2 if kk is corner), None)
                        r['next'] = {'run': x['id'], 'gain': ta - (e1 - e0)}
                        r['followed_new'] = followed(NEW_FIX.get(r['diag']), m2)
                        r['followed_old'] = followed(OLD_FIX.get(r['old_pattern']), m2)
                        r['next_m'] = m2
                        break
    if out_path:
        with open(out_path, 'w') as fh:
            json.dump(jsonable(results), fh, indent=1)
    return results


if __name__ == '__main__':
    from collections import Counter
    db = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    for mode in ('coach', 'pb', 'pairs'):
        res = main(db, out and out.replace('.json', '-{}.json'.format(mode)), mode)
        print(mode, len(res), 'compared sections')
        print(Counter((r['stage'].split(':')[-1], r['diag']) for r in res))
