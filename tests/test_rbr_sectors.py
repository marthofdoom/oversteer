"""Richard Burns Rally's real stage lines (data/telemetry/stages/rbr.json `sector_lines_m`: the start, split and finish
markers of the stage's .dls, built by research/rbr/stage_table.py) and the sectors a run makes of them: the same
code paths as Assetto Corsa Rally's, selected by the data."""
import pytest

from oversteer import coach_context as cc, run_analysis, stage_tables
from oversteer.telemetry_formats import decode_sample
from oversteer.telemetry_store import TRACE_CHANNELS
from tests.test_drive_log import drive_runs
from tests.test_rbr import Stage, capture_packets, runs_of


def _rbr():
    return stage_tables.tables()['rbr']


# -- the shipped table --

def test_the_shipped_stages_carry_their_dls_lines_in_order():
    entries = [e for e in _rbr().values() if e.get('sector_lines_m')]
    assert len(entries) > 400
    for e in entries:
        lines = e['sector_lines_m']
        assert len(lines) >= 3 and all(a < b for a, b in zip(lines, lines[1:])), e['key']     # start < splits < finish
        assert e['sector_lines_source'] and e['sector_lines_confidence'] in ('high', 'medium'), e['key']
        assert stage_tables.sector_lines(e) == lines


def test_monster_hc_has_the_dls_start_splits_and_finish():
    e = stage_tables.entry('rbr:309')
    assert e['sector_lines_m'] == [2.1, 633.5, 1850.9, 2503.5] and e['sector_lines_confidence'] == 'high'
    assert e['finish_m'] == 2503.5 and e['finish_confidence'] == 'high'
    placed = stage_tables.sector_bounds(e)
    assert placed['confidence'] == 'game' and placed['start_m'] == 2.1
    assert placed['bounds'] == [(2.1, 633.5), (633.5, 1850.9), (1850.9, 2503.5)]


def test_a_dls_without_a_start_marker_starts_at_zero_with_medium_confidence():
    e = stage_tables.entry('rbr:71')                                        # Rally School Stage: no start marker
    assert e['sector_lines_m'] == [0.0, 717.0, 1572.7, 2263.2] and e['sector_lines_confidence'] == 'medium'


def test_the_local_table_keeps_the_shipped_lines_but_not_a_stale_set_of_its_own():
    tables = {'rbr': {'rbr:309': {'id': 309, 'stage': 'Monster HC', 'sector_lines_m': [2.1, 633.5, 1850.9, 2503.5],
                                  'sector_lines_source': 's', 'sector_lines_confidence': 'high'}}}
    stage_tables._add_table(tables, {'game': 'rbr', 'stages': [{'id': 309, 'stage': 'Monster HC', 'length_m': 2501}]},
                            'local', replace=True)
    assert tables['rbr']['rbr:309']['sector_lines_m'] == [2.1, 633.5, 1850.9, 2503.5]
    assert tables['rbr']['rbr:309']['length_m'] == 2501
    stage_tables._add_table(tables, {'game': 'rbr', 'stages': [{'id': 309, 'sector_lines_m': [0.0, 1.0, 2.0]}]},
                            'local', replace=True)
    assert tables['rbr']['rbr:309']['sector_lines_m'] == [0.0, 1.0, 2.0]


# -- a run's sectors --

def _sectors(reader, run):
    full = reader.run(run['id'])
    rows = cc.stage_rows(reader.trace(run['id']), full['course'], True, full['result_time'])
    return run_analysis._sector_rows(run['stage'], rows, None, reader, (full, None)), full


def test_the_clock_of_marths_monster_hc_stops_at_the_dls_finish_and_a_run_begun_mid_stage_has_no_sectors(tmp_path):
    """The capture is a slice of the last 400 m (it begins 2165 m in): the driveline at the clock stop is the table's
    finish line, and the run, which did not cover the first sectors, is timed through none of them."""
    packets = capture_packets('rbr-finish.ovcap.gz')
    samples = [decode_sample(d) for _, _, d in packets]
    stopped = [s for a, s in zip(samples, samples[1:]) if s.stage_time == a.stage_time and s.stage_time > 100 and s.speed > 20]
    assert stopped[0].lap_distance == pytest.approx(stage_tables.entry('rbr:309')['finish_m'], abs=1.5)
    learner, reader, session = drive_runs(tmp_path, [(t, s, 0.8) for (t, _, _), s in zip(packets, samples)])
    [run] = session['runs']
    assert reader.run_start(run['id']) == pytest.approx(samples[0].lap_distance, abs=1.0) and reader.run_clock(run['id']) == 'game'
    assert _sectors(reader, run)[0] is None
    learner.close()


def test_a_synthetic_stage_crossing_the_lines_gives_sectors_that_add_up(tmp_path):
    # Rally School Stage (71): lines 0, 717.0, 1572.7, 2263.2 along the driveline; the packets' driveline is 1.5 + the
    # metres driven, the clock stops at the finish line
    stage = Stage(length=2261.7).countdown().drive(2261.7)
    result = stage.clock
    stage.finish()
    learner, reader, runs = runs_of(tmp_path, stage)
    [run] = runs
    assert reader.run_start(run['id']) == pytest.approx(1.5, abs=1.0)        # the driveline where the run began
    found, full = _sectors(reader, run)
    assert [r['name'] for r in found] == ['S1', 'S2', 'S3']
    assert sum(r['time'] for r in found) == pytest.approx(result, abs=0.05)
    speed = 25.0
    assert found[0]['time'] == pytest.approx((717.0 - 1.5) / speed, abs=0.1)
    assert found[1]['time'] == pytest.approx((1572.7 - 717.0) / speed, abs=0.1)
    learner.close()


# -- judged by the clock, not the distance --

LINES = [2695.0, 3295.0, 3895.0, 4495.0]         # a stage whose start marker is far from the driveline's 0 at the go


def _rows(first_t, origin=0.0, speed=20.0, dt=0.1, end=4500.0):
    rows, t = [], first_t
    while origin + speed * (t - first_t) <= end:
        rows.append(tuple(t if n == 't' else speed * (t - first_t) if n == 'distance' else
                          speed if n == 'speed' else 0.0 for n in TRACE_CHANNELS))
        t += dt
    return rows


def test_a_rbr_run_that_began_at_the_go_is_timed_from_zero_wherever_the_start_marker_is():
    bounds = [(a - LINES[0], b - LINES[0]) for a, b in zip(LINES, LINES[1:])]
    rows = _rows(0.3)
    assert cc.began_at_go(rows) and not cc.began_at_go(_rows(40.0))
    usable, go = cc.sector_go('rbr:900', rows, None)
    assert usable and go
    times = cc.sector_times(rows, bounds, shift=0.0 - LINES[0], game_clock=True, from_go=go)
    assert times[0] == pytest.approx(0.3 + 3295.0 / 20.0, abs=0.1)           # S1 from the clock's 0, 2695 m from the marker
    # the distance rule would have started S1 at the marker's crossing, 135 s in
    assert cc.sector_times(rows, bounds, shift=0.0 - LINES[0], game_clock=True)[0] == pytest.approx(30.0, abs=0.1)


def test_a_rbr_run_with_no_recorded_start_that_began_mid_stage_has_no_sectors():
    rows = _rows(70.0)
    assert cc.sector_go('rbr:900', rows, None) == (False, False)
    assert cc.sector_go('rbr:900', rows, 500.0) == (True, False)             # a recorded start: placed, but not through S1
    bounds = [(0.0, 600.0), (600.0, 1200.0)]
    assert 0 not in cc.sector_times(rows, bounds, shift=0.0, game_clock=True, from_go=False)
    assert cc.sector_go('acr:x:y', rows, None) == (True, None)               # ACR as before


def test_the_result_ends_the_last_sector_only_when_it_is_the_clock_stopping_at_the_line():
    bounds = [(0.0, 500.0), (500.0, 1000.0)]
    rows = _rows(0.0, end=1010.0)
    line = 1000.0 / 20.0
    assert cc.sector_times(rows, bounds, game_clock=True, result_time=line + 0.2)[1] == pytest.approx(line + 0.2 - 25.0, abs=0.01)
    assert cc.sector_times(rows, bounds, game_clock=True, result_time=line + 3.0)[1] == pytest.approx(25.0, abs=0.01)


def test_the_local_table_drops_the_shipped_lines_of_a_stage_of_another_length():
    shipped = {'rbr:9': {'id': 9, 'sector_lines_m': [0.0, 700.0, 1500.0, 2300.0], 'sector_lines_source': 's',
                         'sector_lines_confidence': 'high'}}
    for length, kept in ((2200, True), (2500, True), (5000, False)):
        tables = {'rbr': {k: dict(v) for k, v in shipped.items()}}
        stage_tables._add_table(tables, {'game': 'rbr', 'stages': [{'id': 9, 'length_m': length}]}, 'local', replace=True)
        assert ('sector_lines_m' in tables['rbr']['rbr:9']) is kept, length
