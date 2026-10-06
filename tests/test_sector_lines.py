"""The game's real stage lines (start, splits, finish: data/telemetry/stages/acr.json `sector_lines_m`, read from the
game's level data by scripts/acr-sectors.py) and what the app makes of them: the stage tables' start, finish and
sectors, and split times by the distance crossing each line."""
import pytest

from oversteer import coach_context as cc, run_analysis, stage_tables
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry_store import TRACE_CHANNELS
from tests.test_coach_wiring import _timed_run

BIDNO = 'acr:wales:afon-bidno-severn'
CUTS = {'WelesS3HafrenNorthCut1Forward': 450.1,           # Hafren Forest
        'GreeceS3ElatiaCut2Reverse': 358.0,               # Zeli Reverse
        'GreeceS4LoutrakiCut2Reverse': 363.1}             # Aghii Theodori Reverse


def _variants():
    return {e['game_variant_id']: e for e in stage_tables.tables()['acr'].values()}


# -- the shipped table --

def test_every_stage_has_its_lines_in_order_and_one_more_than_its_sectors():
    entries = _variants()
    assert len(entries) == 46
    for vid, e in entries.items():
        lines = e['sector_lines_m']
        assert e['sector_lines_source'] and e['sector_lines_confidence'] in ('high', 'medium'), vid
        if e.get('sector_lines_circuit'):
            assert vid.startswith('LivignoTestTrack01') and e['sector_lines_confidence'] == 'medium'
            assert len(lines) == len(e['sectors_km']), vid          # the lap line, then the splits: no finish
            assert stage_tables.sector_lines(e) is None and stage_tables.sector_bounds(e) is None
            continue
        assert e['sector_lines_confidence'] == 'high', vid
        assert len(lines) == len(e['sectors_km']) + 1, vid
        assert all(a < b for a, b in zip(lines, lines[1:])), vid     # start < splits < finish
        assert stage_tables.sector_lines(e) == lines
        if e.get('pacenote_last_m'):
            assert lines[-1] < e['pacenote_last_m'], vid             # the finish is before the stop control


def test_the_three_cuts_start_where_the_game_puts_their_line_not_before_their_first_note():
    entries = _variants()
    for vid, line in CUTS.items():
        e = entries[vid]
        assert stage_tables.start_line(e) == line
        assert line - (e['pacenote_first_m'] - stage_tables.ROAD_BEFORE_NOTE) > 50      # was placed 51-274 m early
        assert stage_tables.sector_bounds(e)['start_m'] == line
        # a run that begins at the line is a run from the line (RunTracker's test)
        assert line - 3.0 <= line + stage_tables.START_LINE_PAST


def test_a_stage_without_lines_keeps_the_notes_start():
    assert stage_tables.start_line({'pacenote_first_m': 200.0}) == 200.0 - stage_tables.ROAD_BEFORE_NOTE
    assert stage_tables.start_line({'pacenote_first_m': 200.0, 'start_m': 190.0}) == 190.0
    assert stage_tables.start_line({'pacenote_first_m': 200.0, 'start_m': 190.0,
                                    'sector_lines_m': [180.0, 900.0, 1500.0]}) == 180.0


def test_afon_bidno_finishes_at_the_line_and_a_run_timed_at_the_old_estimate_is_moved(tmp_path):
    from oversteer.drive_log import retime_finishes
    assert stage_tables.tables()['acr'][BIDNO]['finish_m'] == 5287.4                 # the shipped estimate stays
    assert stage_tables.entry(BIDNO)['finish_m'] == 5294.1
    assert stage_tables.acr_stage('Wales Afon Bidno', start=239.0)['finish_m'] == 5294.1
    # the road a run drives is from where its runs begin (the measured start), not from the real line a few metres on
    assert stage_tables.road_length(stage_tables.entry(BIDNO)) == pytest.approx(5294.1 - 238.2)
    assert stage_tables.road_length({'start_m': 100.0, 'finish_m': 1100.0, 'sector_lines_m': [110.0, 600.0, 1100.0]}) == 1000.0
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    run = _timed_run(store, BIDNO, result=200.0, course=5100.0, end_speed=40.0)
    store.set_run_finish(run, 5287.4)                                                # timed at the old line
    assert retime_finishes(store) == 1
    row = store.run(run)
    origin = stage_tables.run_origin(stage_tables.entry(BIDNO))                      # 238.2, where its runs were measured
    assert row['course'] == pytest.approx(5294.1 - origin)
    assert row['result_time'] == pytest.approx(200.0 + (5294.1 - 5287.4) / 10.0, abs=0.01)    # 0.1 s a metre
    assert row['run_class'] is None and retime_finishes(store) == 0                  # once
    learner.close()


# -- precedence of the finish line --

def test_the_finish_line_is_the_real_one_then_the_learnt_then_the_shipped_then_the_last_note():
    base = {'location': 'X', 'stage': 'Y', 'track': 'X Y', 'pacenote_first_m': 100.0, 'pacenote_last_m': 5000.0}
    shipped = dict(base, finish_m=4800.0, finish_confidence='medium')
    real = dict(shipped, sector_lines_m=[90.0, 1500.0, 4700.0], sector_lines_confidence='high')
    stage_tables.set_tables({'acr': {'acr:x:y': dict(real, key='acr:x:y')}})
    try:
        stage_tables.set_learnt({'acr:x:y': {'finish_m': 4750.0, 'finish_runs': 3, 'finish_spread_m': 1.0}})
        assert stage_tables.entry('acr:x:y')['finish_m'] == 4700.0                   # the game's line beats a learnt one
        assert stage_tables.entry('acr:x:y')['finish_confidence'] == 'high'
        assert stage_tables.acr_stage('X Y')['finish_m'] == 4700.0
        stage_tables.set_tables({'acr': {'acr:x:y': dict(shipped, key='acr:x:y')}})
        assert stage_tables.entry('acr:x:y')['finish_m'] == 4750.0                   # then the learnt one
        stage_tables.set_learnt({})
        assert stage_tables.entry('acr:x:y')['finish_m'] == 4800.0                   # then the shipped one
        stage_tables.set_tables({'acr': {'acr:x:y': dict(base, key='acr:x:y')}})
        assert stage_tables.entry('acr:x:y').get('finish_m') is None
        assert stage_tables.road_length(stage_tables.entry('acr:x:y')) == 5000.0 - (100.0 - stage_tables.ROAD_BEFORE_NOTE)
    finally:
        stage_tables.set_tables(None)


def test_sector_bounds_come_from_the_real_lines_else_the_scaled_estimate():
    placed = stage_tables.sector_bounds({'sectors_km': [1.0, 2.0], 'sector_lines_m': [100.0, 1200.0, 3100.0]})
    assert placed['confidence'] == 'game' and placed['start_m'] == 100.0
    assert placed['bounds'] == [(100.0, 1200.0), (1200.0, 3100.0)]
    est = stage_tables.sector_bounds({'sectors_km': [1.0, 2.0], 'length_m': 3000, 'pacenote_first_m': 100.0,
                                      'pacenote_last_m': 3500.0})
    assert est['confidence'] == 'low'


def test_the_start_of_two_stages_of_one_name_is_told_by_the_line():
    """Every stage is found by its track name and where its run began: 2-5 m short of its start line."""
    entries = _variants()
    for vid, e in entries.items():
        if e.get('sector_lines_circuit'):
            continue
        for short in (2.0, 4.7):
            found = stage_tables.acr_stage(stage_tables.bridge_track(e['track']), start=e['sector_lines_m'][0] - short)
            assert found is not None and found['game_variant_id'] == vid, (vid, short)


# -- split times by the distance crossing the lines --

LINES = [100.0, 1100.0, 2100.0, 3100.0]


def _trace(origin, first_t, speed=20.0, dt=0.1, clock_at_go=0.0):
    """A run standing from the clock's start (`clock_at_go`, 0) to `first_t`, then at `speed`: rows every `dt` s from
    `first_t`; distance driven from `origin` (the trace's distance 0), lap distance = origin + distance."""
    rows = []
    t = first_t
    while origin + speed * (t - first_t) <= LINES[-1] + 5.0:
        rows.append(tuple(t if n == 't' else speed * (t - first_t) if n == 'distance' else
                          speed if n == 'speed' else 0.0 for n in TRACE_CHANNELS))
        t += dt
    return rows


def test_a_game_clock_run_is_timed_where_its_distance_crosses_each_line():
    origin = 97.0                                    # the car stands 3 m short of the line when the clock starts
    rows = _trace(origin, first_t=0.5)               # the first row is 0.5 s into the stage clock, still at rest
    bounds = [(a - LINES[0], b - LINES[0]) for a, b in zip(LINES, LINES[1:])]
    times = cc.sector_times(rows, bounds, shift=origin - LINES[0], game_clock=True)
    # 20 m/s from t = 0.5: lap distance 1100 is reached 1003 m on, 50.15 s later
    assert times[0] == pytest.approx(0.5 + 1003 / 20.0)               # S1 from the clock's start (0), the standing included
    assert times[1] == pytest.approx(50.0) and times[2] == pytest.approx(50.0)
    assert sum(times.values()) == pytest.approx(0.5 + 3003 / 20.0)    # the clock at the finish line
    # the same run on the run's own clock (no game clock): the first sector starts at the clock's start too, so the
    # sectors add up to the run's time
    own = cc.sector_times(rows, bounds, shift=origin - LINES[0], game_clock=False)
    assert own[0] == pytest.approx(0.5 + 1003 / 20.0) and own[1] == pytest.approx(50.0)
    assert sum(own.values()) == pytest.approx(sum(times.values()))
    # a run that began 400 m on (a restart) did not cover the first sector: not timed through it
    late = cc.sector_times(_trace(497.0, 0.5), bounds, shift=497.0 - LINES[0], game_clock=True)
    assert 0 not in late and late[1] == pytest.approx(50.0)


def test_the_sectors_add_up_to_the_result_where_the_trace_stops_a_row_short_of_the_line():
    origin = 100.0
    rows = _trace(origin, first_t=0.0)
    rows = [r for r in rows if r[cc.CH['distance']] <= 3000.0 - 3.0]            # the last row 3 m short of the finish
    bounds = [(a - LINES[0], b - LINES[0]) for a, b in zip(LINES, LINES[1:])]
    result = 3000.0 / 20.0                                                      # the clock at the finish: 150 s
    exact = cc.sector_times(rows, bounds, shift=0.0, result_time=result)
    assert sum(exact.values()) == pytest.approx(result)
    near = cc.sector_times(rows, bounds, shift=0.0)                             # no result: the last row's speed
    assert sum(near.values()) == pytest.approx(result, abs=0.01)
    # a run that stops more than END_SLACK short did not time its last sector
    assert 2 not in cc.sector_times([r for r in rows if r[cc.CH['distance']] <= 2900.0], bounds, shift=0.0, result_time=result)


class _Reader:
    def __init__(self, starts, clocks):
        self.starts, self.clocks = starts, clocks

    def run_start(self, run):
        return self.starts.get(run)

    def run_clock(self, run):
        return self.clocks.get(run)


def test_the_run_views_sectors_are_exact_on_the_game_clock_and_say_so():
    lines = stage_tables.sector_lines(stage_tables.entry(BIDNO))
    assert lines == [242.9, 1985.1, 3655.2, 5294.1]
    origin, speed = 239.6, 25.0
    rows = []
    t = 0.6
    while origin + speed * (t - 0.6) <= lines[-1] + 3.0:
        rows.append(tuple(t if n == 't' else speed * (t - 0.6) if n == 'distance' else 0.0 for n in TRACE_CHANNELS))
        t += 0.02
    reader = _Reader({1: origin}, {1: 'game'})
    found = run_analysis._sector_rows(BIDNO, rows, None, reader, ({'id': 1}, None))
    assert [r['name'] for r in found] == ['S1', 'S2', 'S3'] and all(r['confidence'] == 'game' for r in found)
    assert found[0]['time'] == pytest.approx(0.6 + (1985.1 - origin) / speed, abs=0.03)
    assert found[1]['time'] == pytest.approx((3655.2 - 1985.1) / speed, abs=0.03)
    assert sum(r['time'] for r in found) == pytest.approx(0.6 + (5294.1 - origin) / speed, abs=0.03)
    assert all(r['delta'] is None and r['ref_time'] is None for r in found)
