"""The 0.16 data repair (oversteer/data_repair.py): the version gate, the PB kept through the reanalysis, the DiRT
stage re-key, the phantom runs' limits, resuming after a crash, the car names and the re-timing from captures."""
import glob
import os
import sqlite3

import pytest

from oversteer import coach_context, data_repair, telemetry_store
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry_store import DATA_VERSION, TRACE_CHANNELS, open_store
from tests.test_coach_wiring import drive_stages

T = {name: i for i, name in enumerate(TRACE_CHANNELS)}
EL_RODEO = 'dirt:2846:210'
EL_RODEO_LENGTH = 2845.6298828125


def _old(store):
    """A file as an older build left it: no data version."""
    store._do("DELETE FROM store_meta WHERE key = 'data_version'")
    store.commit()


def _rows(n, d_end, xz0, xz1, t_end=None):
    """n trace rows from (x, z) xz0 to xz1, distance 0..d_end."""
    out = []
    for i in range(n):
        f = i / (n - 1)
        row = [float('nan')] * len(TRACE_CHANNELS)
        row[T['t']] = (t_end or n / 10.0) * f
        row[T['wall']] = row[T['t']]
        row[T['distance']] = d_end * f
        row[T['speed']] = 20.0
        row[T['x']] = xz0[0] + (xz1[0] - xz0[0]) * f
        row[T['y']] = 0.0
        row[T['z']] = xz0[1] + (xz1[1] - xz0[1]) * f
        out.append(tuple(row))
    return out


class Dirt:
    """A store with DiRT runs written as the run tracker would have."""

    def __init__(self, tmp_path, game='dirt', key='dirt/9000-1300-6', name='9000 rpm, 6 gears'):
        self.store = open_store(str(tmp_path / 'telemetry.db'))
        self.game = game
        self.car = self.store.car_id('p', key, game, name)
        self.session = self.store.start_session('p', self.car, game, 1000.0)
        self.n = 0
        self.t = 1000.0

    def run(self, stage, length, finished, result, distance, xz0, xz1, klass, trace=True, session=None):
        self.n += 1
        self.t += 300.0
        store = self.store
        run = store.start_run(session or self.session, self.n, self.t, stage, self.game, length,
                              start_pos=[xz0[0], 0.0, xz0[1]])
        store.end_run(run, ended=self.t + 200.0, distance=distance, finished=finished, result_time=result,
                      run_class=klass, course=distance)
        if trace:
            store.add_trace(run, _rows(60, distance, xz0, xz1))
        store.commit()
        return run


# -- the version gate --

def test_a_new_file_is_at_the_data_version_and_is_not_repaired(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert store.data_version() == DATA_VERSION
    assert data_repair.migrate(store) is None
    assert glob.glob(str(tmp_path / '*.bak')) == []
    store.close()


def test_an_old_file_is_repaired_once_after_a_backup(tmp_path):
    d = Dirt(tmp_path)
    d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 175.0, 2860.0, (0, 0), (500, 500), 'clean')
    _old(d.store)
    assert d.store.data_version() == 0
    summary = data_repair.migrate(d.store)
    assert summary is not None and summary['queued'] == 1
    assert d.store.data_version() == DATA_VERSION
    [backup] = glob.glob(str(tmp_path / 'telemetry.db.pre-0.16-*.bak'))
    copy = sqlite3.connect(backup)
    assert copy.execute('SELECT COUNT(*) FROM store_meta WHERE key = ?', ('data_version',)).fetchone()[0] == 0
    copy.close()
    assert data_repair.migrate(d.store) is None                    # done: never again
    assert d.store.reanalysis_pending() == 1                       # the queue is not built again either
    d.store.close()


def test_the_drive_log_repairs_an_old_file_when_it_opens_it(tmp_path):
    d = Dirt(tmp_path)
    d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 175.0, 2860.0, (0, 0), (500, 500), 'clean')
    _old(d.store)
    d.store.close()
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    assert learner.log.store.data_version() == DATA_VERSION
    learner.close()


def test_no_backup_no_repair(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    _old(d.store)

    def fail(db, dest):
        raise OSError('disk full')
    monkeypatch.setattr(data_repair, '_backup', fail)
    assert data_repair.migrate(d.store) is None
    assert d.store.data_version() == 0                             # tried again on the next start
    d.store.close()


# -- the PB through the reanalysis --

def test_the_pb_stays_the_pb_and_keeps_its_trace_while_the_runs_are_worked_over(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0, 26.0, 30.0))
    store = learner.log.store
    rows = store.db.execute('SELECT r.id, r.stage, s.car, r.result_time, r.run_class FROM runs r '
                            'JOIN sessions s ON s.id = r.session ORDER BY r.id').fetchall()
    stage, car = rows[0][1], rows[0][2]
    pb = coach_context.stage_pb_id(store, stage, car)
    assert pb is not None
    classes = [r[4] for r in rows]
    _old(store)
    learner._backfilled = False
    assert data_repair.migrate(store) is not None
    queued = store.runs_to_reanalyse(10)
    assert set(queued) == {r[0] for r in rows}
    # the reference runs first, oldest first (the first finish is one)
    assert queued == data_repair.queue_order(store) and queued[0] == rows[0][0]
    # nothing is cleared while queued: the PB is still the PB, every class as it was
    assert store.runs_to_backfill(10) == []
    assert coach_context.stage_pb_id(store, stage, car) == pb
    assert [r[0] for r in store.db.execute('SELECT run_class FROM runs ORDER BY id')] == classes
    # no trace is pruned while runs are queued, however small the cap
    assert store.prune_traces(cap=1) == 0 and len(store.trace_runs([r[0] for r in rows])) == 3
    from oversteer.drive_log import backfill_step
    assert backfill_step(learner, 1) == 1                          # one run rewritten, the rest still queued
    assert store.reanalysis_pending() == 2
    assert coach_context.stage_pb_id(store, stage, car) == pb
    assert store.prune_traces(cap=1) == 0
    learner.backfill()
    assert store.reanalysis_pending() == 0
    assert coach_context.stage_pb_id(store, stage, car) == pb
    assert all(r[0] in coach_context.PB_CLASSES for r in store.db.execute('SELECT run_class FROM runs'))
    assert store.corners(pb)                                       # rewritten
    # the launch is not in the trace: its metrics and event stay as the run wrote them
    assert {m['name'] for m in store.run_metrics(pb)} >= {'launch.t50', 'launch.g'}
    assert store.events(pb, 'launch')
    # the queue done, the traces are pruned again, and the PB's (and the PB history's) are kept
    store.prune_traces(cap=1)
    assert store.trace(pb) is not None and store.trace(rows[0][0]) is not None
    learner.close()


def test_the_queue_puts_the_reference_runs_first_oldest_first(tmp_path):
    d = Dirt(tmp_path)
    a = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 180.0, 2860.0, (0, 0), (500, 500), 'learning')
    b = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 185.0, 2860.0, (0, 0), (500, 500), 'clean')     # slower: no reference
    c = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 175.0, 2860.0, (0, 0), (500, 500), 'off')       # the PB, with an off
    e = d.run(EL_RODEO, EL_RODEO_LENGTH, 0, None, 900.0, (0, 0), (500, 500), 'restart')
    assert data_repair.queue_order(d.store) == [a, c, b, e]
    d.store.close()


# -- the DiRT stage re-key --

def test_dirt_stages_are_matched_again_and_merged_with_the_pb_the_fastest_finish(tmp_path):
    d = Dirt(tmp_path)
    store = d.store
    slow = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 180.0, 2860.0, (900, 210), (-250, 1260), 'learning')
    fast = d.run('dirt:2846:190', EL_RODEO_LENGTH, 1, 175.0, 2860.0, (900, 190), (-250, 1260), 'learning')
    other = d.run('dirt:2846:1260', EL_RODEO_LENGTH, 0, None, 150.0, (0, 0), (100, 0), 'restart', trace=False)
    store.upsert_stage('dirt:8160:4680', 'dirt', 8159.82)          # an orphan: no run on it
    store.upsert_stage('dirt:9999:0', 'dirt', 9999.0)              # an unknown stage with a run stays
    unknown = d.run('dirt:9999:0', 9999.0, 1, 500.0, 9999.0, (0, 0), (10, 10), 'clean')
    for key in (EL_RODEO, 'dirt:2846:190'):
        store._do("INSERT INTO stage_potential (stage, car, data_version, sections, profile) VALUES (?, ?, 'x', "
                  "'[]', '{}')", (key, d.car))
    store._do("INSERT INTO coach_state (profile, car, tip) VALUES ('p', ?, 'corner.best:dirt:2846:190')", (d.car,))
    store._do("INSERT INTO coach_state (profile, car, tip) VALUES ('p', ?, 'corner.best:dirt:2846:1900')", (d.car,))
    store.commit()
    assert coach_context.stage_pb_id(store, EL_RODEO, d.car) == slow
    _old(store)
    summary = data_repair.migrate(store)
    assert summary['stages_moved'] == {'dirt:2846:190': EL_RODEO, 'dirt:2846:1260': EL_RODEO}
    assert set(summary['stages_deleted']) == {'dirt:2846:190', 'dirt:2846:1260', 'dirt:8160:4680'}
    assert {r[0] for r in store.db.execute('SELECT stage FROM runs')} == {EL_RODEO, 'dirt:9999:0'}
    assert store.run(other)['stage'] == EL_RODEO and store.run(unknown)['stage'] == 'dirt:9999:0'
    assert coach_context.stage_pb_id(store, EL_RODEO, d.car) == fast            # the fastest finish under the new key
    assert store.stage(EL_RODEO)['runs'] == 3 and store.stage(EL_RODEO)['name'] == 'El Rodeo'
    assert store.stage('dirt:2846:190') is None and store.stage('dirt:8160:4680') is None
    assert store.db.execute('SELECT COUNT(*) FROM stage_potential').fetchone()[0] == 0
    assert [r[0] for r in store.db.execute('SELECT tip FROM coach_state')] == ['corner.best:dirt:2846:1900']
    # once more changes nothing
    assert data_repair.rekey_dirt_stages(store) == ({}, [])
    store.close()


# -- the phantom runs --

def _finished_and_after(d, gap=1.0, distance=115.0, finished=None, klass='restart', after_stage=None, trace=True,
                        length=EL_RODEO_LENGTH):
    end = (-250.0, 1260.0)
    run = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 175.0, 2860.0, (900, 210), end, 'learning')
    phantom = d.run(after_stage or 'dirt:2846:1260', length, finished, None, distance,
                    (end[0] + gap, end[1]), (end[0] + gap + distance, end[1]), klass, trace=trace)
    return run, phantom


@pytest.mark.parametrize('case, deleted', [
    ({}, True),
    ({'finished': 0}, True),
    ({'gap': 100.0}, False),                         # began elsewhere
    ({'distance': 250.0}, False),                    # a real run's length
    ({'finished': 1}, False),                        # a finish is never deleted
    ({'klass': 'off'}, False),                       # a class that can be a PB
    ({'klass': 'learning'}, False),
    ({'trace': False}, False),                       # nothing proves where it began
    ({'after_stage': 'dirt:4381:-2370', 'length': 4380.74}, False),    # another stage's length
])
def test_only_a_provable_dirt_phantom_is_deleted(tmp_path, case, deleted):
    d = Dirt(tmp_path)
    run, phantom = _finished_and_after(d, **case)
    _old(d.store)
    summary = data_repair.migrate(d.store)
    assert summary['phantoms_deleted'] == ([phantom] if deleted else [])
    assert (d.store.run(phantom) is None) == deleted
    assert d.store.run(run) is not None and d.store.trace(run) is not None
    d.store.close()


def test_a_labelled_or_acr_phantom_is_kept(tmp_path):
    d = Dirt(tmp_path)
    run, phantom = _finished_and_after(d)
    d.store.set_label(phantom, note='mine')
    d.store.commit()
    assert data_repair.phantom_runs(d.store) == []
    d.store.close()
    (tmp_path / 'acr').mkdir()
    acr = Dirt(tmp_path / 'acr', game='acr', key='acr/x', name='x')
    _finished_and_after(acr)
    assert data_repair.phantom_runs(acr.store) == []
    acr.store.close()


# -- a crash part way, and resuming --

def test_a_crash_in_the_repair_changes_nothing_and_it_runs_on_the_next_start(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    run, phantom = _finished_and_after(d)
    _old(d.store)

    def crash(store):
        raise RuntimeError('power cut')
    with monkeypatch.context() as m:
        m.setattr(data_repair, 'rename_dirt_cars', crash)
        assert data_repair.migrate(d.store) is None
    assert d.store.data_version() == 0 and d.store.run(phantom) is not None
    assert d.store.reanalysis_pending() == 0
    assert len(glob.glob(str(tmp_path / '*.pre-0.16-*.bak'))) == 1
    d.store.close()
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert data_repair.migrate(store)['phantoms_deleted'] == [phantom]
    assert len(glob.glob(str(tmp_path / '*.pre-0.16-*.bak'))) == 1        # the first backup is the one kept
    store.close()


def test_the_reanalysis_resumes_after_a_restart(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0, 26.0, 30.0))
    store = learner.log.store
    _old(store)
    learner.close()
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))           # the drive log repairs it as it opens
    store = learner.log.store
    assert store.data_version() == DATA_VERSION and store.reanalysis_pending() == 3
    from oversteer.drive_log import backfill_step
    backfill_step(learner, 1)
    learner.close()                                                   # quit part way
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    store = learner.log.store
    assert store.reanalysis_pending() == 2                            # not queued again: the rest
    learner.backfill()
    assert store.reanalysis_pending() == 0
    learner.close()


def test_a_run_that_fails_to_be_worked_over_keeps_its_analysis(tmp_path, monkeypatch):
    learner, reader = drive_stages(tmp_path, (30.0, 26.0))
    store = learner.log.store
    before = store.db.execute('SELECT run, COUNT(*) FROM corners GROUP BY run ORDER BY run').fetchall()
    _old(store)
    data_repair.migrate(store)
    from oversteer import drive_log

    def boom(*args):
        raise RuntimeError('bad trace')
    monkeypatch.setattr(drive_log, '_backfill_run', boom)
    drive_log.backfill_step(learner, 5)
    assert store.reanalysis_pending() == 0                            # out of the queue: traces may be pruned again
    assert store.db.execute('SELECT run, COUNT(*) FROM corners GROUP BY run ORDER BY run').fetchall() == before
    assert store.runs_to_backfill(10) == []
    learner.close()


# -- the car names --

def test_dirt_cars_named_by_the_decoder_get_their_shipped_names(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    a = store.car_id('p', 'dirt/9000-1300-6', 'dirt', '9000 rpm, 6 gears')
    b = store.car_id('p', 'dirt/9000-1200-6', 'dirt', '9000 rpm, 6 gears')
    store.rename_car('p', 'dirt/9000-1200-6', 'My Golf')
    c = store.car_id('p', 'wrcg/7900-800-6', 'wrcg', '7900 rpm, 6 gears')
    assert data_repair.rename_dirt_cars(store) == {a: ('9000 rpm, 6 gears', 'Seat Ibiza Kit Car')}
    names = dict(store.db.execute('SELECT id, name FROM cars').fetchall())
    assert names == {a: 'Seat Ibiza Kit Car', b: 'My Golf', c: '7900 rpm, 6 gears'}
    assert data_repair.rename_dirt_cars(store) == {}
    store.close()


# -- re-timing from the captures --

def _fake_captures(monkeypatch, tmp_path, replayed):
    capture = str(tmp_path / 'captures' / 'x.ovcap.gz')
    monkeypatch.setattr(data_repair, 'capture_files', lambda folders: [(capture, 1000.0, 1e6, 1000)])
    calls = []

    def replay(path, scratch, learnt=None):
        calls.append(path)
        return replayed()
    monkeypatch.setattr(data_repair, '_replay_runs', replay)
    return calls


def _replay(run_row, result, course, clock=None, stop=None, trace=None):
    return {'started': run_row['started'], 'finished': 1, 'result_time': result, 'course': course, 'distance': course,
            'game': 'dirt', 'run_clock': (clock,) if clock else None, 'run_stop': (stop,) if stop else None,
            'run_start': None, 'run_finish': None, 'trace': trace}


def test_the_retime_takes_the_replays_time_after_a_backup_and_queues_the_run(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    run = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 174.176, 2837.0, (0, 0), (500, 500), 'learning')
    same = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 180.0, 2837.0, (0, 0), (500, 500), 'learning')
    lost = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 190.0, 2837.0, (0, 0), (500, 500), 'learning')
    rows = {r: d.store.run(r) for r in (run, same, lost)}
    d.store.close()
    calls = _fake_captures(monkeypatch, tmp_path, lambda: [
        _replay(rows[run], 175.261, 2865.9, 'game', 2845.6), _replay(rows[same], 180.01, 2837.0)])
    path = str(tmp_path / 'telemetry.db')
    dry = data_repair.retime_from_captures(path, dry_run=True, out=lambda *a: None)
    assert [r[0] for r in dry['retimed']] == [run] and dry['backup'] is None
    assert glob.glob(str(tmp_path / '*.pre-retime-*.bak')) == []
    report = data_repair.retime_from_captures(path, out=lambda *a: None)
    assert [(r[0], r[2]) for r in report['retimed']] == [(run, 175.261)]       # 0.01 s is the same time
    assert [(r[0], r[3]) for r in report['unmatched']] == [(lost, 'the replay started 0 runs then')]
    assert report['backup'] and os.path.exists(report['backup']) and len(calls) == 2   # one replay a call
    store = open_store(path)
    assert store.run(run)['result_time'] == 175.261 and store.run(run)['course'] == 2865.9
    assert store.run(same)['result_time'] == 180.0
    assert store.run_clock(run) == 'game'
    assert store.runs_to_reanalyse(10) == [run]
    assert store.trace(run)[-1][T['distance']] == pytest.approx(2837.0)    # the stored trace reaches the finish: kept
    store.close()
    # once more: nothing to do
    assert data_repair.retime_from_captures(path, out=lambda *a: None)['retimed'] == []


def test_the_retime_stores_the_replays_trace_where_the_stored_one_stops_short(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    run = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 160.0, 2400.0, (0, 0), (500, 500), 'off')
    row = d.store.run(run)
    d.store.close()
    full = telemetry_store.pack_trace(_rows(80, 2866.0, (0, 0), (600, 600)))
    _fake_captures(monkeypatch, tmp_path, lambda: [_replay(row, 175.261, 2865.9, trace=(2, full))])
    data_repair.retime_from_captures(str(tmp_path / 'telemetry.db'), out=lambda *a: None)
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert store.trace(run)[-1][T['distance']] == pytest.approx(2866.0)
    store.close()


def test_the_retime_refuses_an_open_database(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 174.0, 2837.0, (0, 0), (500, 500), 'learning')
    _fake_captures(monkeypatch, tmp_path, lambda: [])
    with pytest.raises(data_repair.Locked):
        data_repair.retime_from_captures(str(tmp_path / 'telemetry.db'), out=lambda *a: None)
    out = []
    assert data_repair.run_retime(str(tmp_path / 'telemetry.db'), out=out.append) == 2
    assert 'close Oversteer' in out[0]
    d.store.close()
    assert data_repair.run_retime(str(tmp_path / 'telemetry.db'), out=out.append) == 0


# -- the review's should-fixes --

def _acr_runs(tmp_path, specs):
    """ACR runs of one car on one stage, oldest first: (result_time, run_class). Returns (store, ids, trace size)."""
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'acr/a', 'acr')
    session = store.start_session('p', car, 'acr', 1.0)
    rows = [tuple(float(i + c) for c in range(len(TRACE_CHANNELS))) for i in range(100)]
    ids = []
    for n, (result, klass) in enumerate(specs, 1):
        run = store.start_run(session, n, float(n), 'acr:x:y', 'acr', 5000.0)
        store.end_run(run, ended=300.0, distance=5000.0, finished=1, result_time=result, course=4900.0,
                      run_class=klass, wet='dry')
        store.add_trace(run, rows)
        ids.append(run)
    store.commit()
    size = store.db.execute('SELECT LENGTH(data) FROM traces WHERE run = ?', (ids[0],)).fetchone()[0]
    return store, ids, size


def test_a_pb_whose_class_an_older_build_cleared_keeps_its_trace_past_the_cap(tmp_path):
    store, ids, size = _acr_runs(tmp_path, [(210.0, 'clean'), (220.0, 'clean'), (200.0, None), (230.0, 'clean')])
    assert store.prune_traces(cap=size * 2) > 0
    assert store.trace(ids[2]) is not None                         # the PB, waiting for the backfill
    assert store.trace(ids[0]) is not None                         # the PB history
    store.close()


def test_a_class_less_run_with_a_bogus_quick_time_does_not_hide_the_real_pb(tmp_path):
    # an older build's run, its class cleared, quicker than anything since (a bad time); then the ranked runs
    store, ids, size = _acr_runs(tmp_path, [(150.0, None), (210.0, 'clean'), (220.0, 'clean'), (200.0, 'off'),
                                            (230.0, 'clean')])
    car = store.run(ids[1])['car']
    assert coach_context.stage_pb_id(store, 'acr:x:y', car) == ids[3]
    assert store.prune_traces(cap=size * 2) > 0
    assert store.trace(ids[3]) is not None                         # the PB, slower than the class-less run
    assert store.trace(ids[1]) is not None                         # the PB history of the ranked runs
    assert store.trace(ids[0]) is not None                         # and the class-less one, still protected
    assert store.trace(ids[2]) is None and store.trace(ids[4]) is None
    store.close()


def test_the_old_requeue_paths_keep_the_class_while_the_run_waits(tmp_path):
    store, ids, size = _acr_runs(tmp_path, [(210.0, 'clean'), (200.0, 'off')])
    store.queue_stage_runs('acr:x:y')
    assert [store.run(r)['run_class'] for r in ids] == ['clean', 'off']
    assert set(store.runs_to_reanalyse(10)) == set(ids)
    assert coach_context.stage_pb_id(store, 'acr:x:y', store.run(ids[0])['car']) == ids[1]
    assert store.prune_traces(cap=1) == 0                          # nothing pruned while they wait
    store.close()


def test_every_repair_makes_its_own_backup(tmp_path):
    d = Dirt(tmp_path)
    d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 175.0, 2860.0, (0, 0), (500, 500), 'clean')
    _old(d.store)
    assert data_repair.migrate(d.store)['backup']
    d.store._do("INSERT OR REPLACE INTO store_meta (key, value) VALUES ('data_version', '0')")   # as a merge lowers it
    d.store.commit()
    import time as _time
    _time.sleep(1.1)                                               # another second in the name
    summary = data_repair.migrate(d.store)
    assert summary is not None
    assert len(glob.glob(str(tmp_path / 'telemetry.db.pre-0.16-*.bak'))) == 2
    assert d.store.meta(data_repair.BACKUP_META) is None
    d.store.close()


def test_the_potentials_are_built_again_when_the_queue_empties_by_deletion(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0, 26.0))
    store = learner.log.store
    _old(store)
    data_repair.migrate(store)
    store._do("INSERT INTO stage_potential (stage, car, data_version, sections, profile) VALUES ('x', "
              "(SELECT MIN(id) FROM cars), 'v', '[]', '{}')")
    for run in store.runs_to_reanalyse(10):
        store.drop_run(run)                                        # gone before they were worked
    store.commit()
    from oversteer.drive_log import backfill_step
    backfill_step(learner, 3)
    assert store.db.execute("SELECT COUNT(*) FROM stage_potential WHERE stage = 'x'").fetchone()[0] == 0
    assert not store.meta(telemetry_store.REBUILD_META)
    learner.close()


def test_the_retime_marks_every_matched_run_on_the_games_clock_and_skips_another_line(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    same = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 180.0, 2837.0, (0, 0), (500, 500), 'learning')
    other = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 190.0, 2837.0, (0, 0), (500, 500), 'learning')
    d.store.set_run_finish(other, 2800.0)
    d.store.commit()
    rows = {r: d.store.run(r) for r in (same, other)}
    d.store.close()
    seen = []

    def replayed():
        return [_replay(rows[same], 180.01, 2837.0, 'game', 2845.6),
                dict(_replay(rows[other], 185.0, 2837.0), run_finish=(2845.6,))]
    _fake_captures(monkeypatch, tmp_path, replayed)

    def spy(path, scratch, learnt=None):
        seen.append(learnt)
        return replayed()
    monkeypatch.setattr(data_repair, '_replay_runs', spy)
    report = data_repair.retime_from_captures(str(tmp_path / 'telemetry.db'), out=lambda *a: None)
    assert report['retimed'] == [] and report['clocked'] == [same]
    assert [(r[0], r[3][:42]) for r in report['unmatched']] == [(other, 'the replay timed it at another finish line')]
    assert seen == [{}]                                            # the live file's (here no) learnt lines go in
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert store.run_clock(same) == 'game' and store.run(same)['result_time'] == 180.0
    assert store.run(other)['result_time'] == 190.0
    assert store.db.execute('SELECT finish_m FROM run_finish WHERE run = ?', (other,)).fetchone()[0] == 2800.0
    assert store.runs_to_reanalyse(10) == [same]
    store.close()


def test_a_dry_run_only_reads_and_works_while_the_app_has_the_file(tmp_path, monkeypatch):
    d = Dirt(tmp_path)
    run = d.run(EL_RODEO, EL_RODEO_LENGTH, 1, 174.0, 2837.0, (0, 0), (500, 500), 'learning')
    row = d.store.run(run)
    d.store.db.execute('DROP TABLE reanalyse')                     # as a file this build never opened
    d.store.commit()
    _fake_captures(monkeypatch, tmp_path, lambda: [_replay(row, 175.0, 2860.0, 'game')])
    out = []
    assert data_repair.run_retime(str(tmp_path / 'telemetry.db'), dry_run=True, out=out.append) == 0  # still open
    assert any('would be re-timed' in line for line in out) and any('about 1 minute' in line for line in out)
    assert not d.store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'reanalyse'").fetchone()
    assert d.store.run(run)['result_time'] == 174.0 and glob.glob(str(tmp_path / '*.bak')) == []
    d.store.close()
