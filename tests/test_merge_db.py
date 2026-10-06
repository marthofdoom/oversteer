"""scripts/merge-telemetry-db.py: one telemetry database's runs ported into another, ids remapped, duplicates kept
once, derived rows left to the backfill, all or nothing, and nothing added on a second run."""
import importlib.util
import json
import os
import sqlite3

import pytest

from oversteer.telemetry_store import open_store, TRACE_CHANNELS

HERE = os.path.dirname(os.path.abspath(__file__))


def script():
    spec = importlib.util.spec_from_file_location('merge_db', os.path.join(HERE, '..', 'scripts',
                                                                           'merge-telemetry-db.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge_db = script()


def trace(seed, n=40, positions=True):
    rows = []
    for i in range(n):
        row = [float('nan')] * len(TRACE_CHANNELS)
        row[0], row[1], row[2] = i * 0.1, i * 2.0 + seed, 20.0
        if positions:
            row[TRACE_CHANNELS.index('x')], row[TRACE_CHANNELS.index('z')] = float(i), float(seed)
        row[-1] = i * 0.1
        rows.append(tuple(row))
    return rows


def model(pulls, gears=3):
    return {'key': 'acr/Car', 'name': 'Car', 'pulls': pulls,
            'ratios': {str(g): [100.0 * g] * 5 for g in range(1, gears + 1)}}


def drive(store, profile, key, started, stage='acr:wales:afon-bidno-severn', seed=0, traced=True, clock=False,
          positions=True, label=None, pulls=10):
    """A session with one finished run: its trace, a lap, a segment, a change of gear, corners, events, a launch
    metric and a derived metric. Returns (session, run)."""
    car = store.save_model(profile, key, key.split('/')[0], 'Car', model(pulls))
    tune = store.add_tune(car, started, {1: 100.0, 2: 200.0}, 'first')
    session = store.start_session(profile, car, key.split('/')[0], started, track='Wales Afon Bidno', stage=stage)
    store.update_session(session, tune=tune, ended=started + 300)
    run = store.start_run(session, 1, started + 5, stage=stage, start_pos=[1.0, 2.0, 3.0])
    store.end_run(run, ended=started + 200, distance=5000.0, duration=190.0, finished=1, result_time=180.0 + seed,
                  course=5000.0, run_class='clean')
    if traced:
        store.add_trace(run, trace(seed, positions=positions))
    store.add_lap(run, 1, 180.0, 5000.0, 1)
    store.add_segment(run, 0.0, 200.0, 0.0, 10.0, {'grip': 1.0})
    store.add_shift(session, run, {'at': started + 10, 'gear': 2, 'gear_to': 3, 'direction': 'up', 'rpm': 7000.0})
    store.add_shift(session, None, {'at': started + 1, 'gear': 1, 'gear_to': 2, 'direction': 'up', 'rpm': 6000.0})
    store.add_corners(run, [{'d': 100.0, 'direction': 1}])
    store.add_events(run, [{'kind': 'off', 'd0': 50.0, 'detail': {'x': 1}}])
    store.add_metrics(session, run, [{'name': 'launch.t50', 'value': 2.0, 'count': 1},
                                     {'name': 'shift.cost', 'value': 0.3, 'count': 4}])
    store.set_run_start(run, 120.0)
    if clock:
        store.set_run_clock(run, 'game')
        store.set_run_stop(run, 5100.0)
    if label:
        store.set_label(run, note=label)
    return session, run


@pytest.fixture
def files(tmp_path):
    target, source = str(tmp_path / 'target.db'), str(tmp_path / 'source.db')
    t = open_store(target)
    t.begin()
    drive(t, 'AC Rally', 'acr/Car', 1000.0, seed=1, pulls=50)
    t.save_potential('acr:wales:afon-bidno-severn', 1, 'v', 1.0, {'sections': [], 'profile': {}, 'pb_s': 1.0})
    t.set_stage_fingerprint('acr:wales:afon-bidno-severn', 'old')
    t.coach_seen('AC Rally', 1, 'tip.a', 1.0, at=100.0)
    t.commit()
    t.db.close()
    s = open_store(source)
    s.begin()
    drive(s, 'ACR', 'acr/Car', 5000.0, seed=2, clock=True, pulls=5)
    drive(s, 'ACR', 'acr/Other', 9000.0, stage='acr:alsace:sommet-de-munster', seed=3, label='mine')
    # a run with no trace: its derived rows are all there is of it
    car = s.car_id('ACR', 'acr/Other', 'acr')
    session = s.start_session('ACR', car, 'acr', 9500.0)
    run = s.start_run(session, 1, 9501.0, stage='acr:alsace:sommet-de-munster')
    s.end_run(run, ended=9502.0, distance=0.0, finished=0, run_class='restart')
    s.add_corners(run, [{'d': 1.0}])
    s.add_metrics(session, run, [{'name': 'shift.cost', 'value': 1.0, 'count': 1}])
    s.coach_seen('ACR', 1, 'tip.a', 2.0, at=200.0)
    s.coach_seen('ACR', 1, 'tip.a', 3.0, at=300.0)
    s.coach_seen('ACR', 0, 'tip.driver', 1.0, at=50.0)
    s.set_stage_prior('acr:alsace:sommet-de-munster', 'surface', 'tarmac', 'user')
    s.commit()
    s.db.close()
    return target, source, tmp_path


def run(files, dry_run=False, **kw):
    target, source, tmp = files
    lines = []
    plan = merge_db.run_merge(target, source, {'ACR': 'AC Rally'}, dry_run=dry_run, workdir=str(tmp / 'work'),
                              backup_dir=None if dry_run else str(tmp / 'backup'), out=lines.append, **kw)
    return plan, lines


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def counts(path):
    db = sqlite3.connect(path)
    try:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {n: db.execute('SELECT COUNT(*) FROM {}'.format(n)).fetchone()[0] for n in names}
    finally:
        db.close()


def test_runs_are_ported_with_their_rows_remapped(files):
    target, source, tmp = files
    before = open(source, 'rb').read()
    plan, lines = run(files)
    assert open(source, 'rb').read() == before                      # the source is only read
    db = connect(target)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    cars = {r['key']: r['id'] for r in db.execute("SELECT key, id FROM cars WHERE profile = 'AC Rally'")}
    assert set(cars) == {'acr/Car', 'acr/Other'} and db.execute('SELECT COUNT(*) FROM cars').fetchone()[0] == 2
    runs = db.execute('SELECT r.*, s.car, s.profile FROM runs r JOIN sessions s ON s.id = r.session '
                      'ORDER BY r.started').fetchall()
    assert len(runs) == 4
    ported = runs[1]
    assert ported['car'] == cars['acr/Car'] and ported['profile'] == 'AC Rally'
    assert ported['run_class'] is None                               # the backfill works it over
    assert runs[3]['run_class'] == 'restart'                         # no trace: kept as it was
    pid = ported['id']
    assert db.execute('SELECT clock FROM run_clock WHERE run = ?', (pid,)).fetchone()[0] == 'game'
    assert db.execute('SELECT stop_m FROM run_stop WHERE run = ?', (pid,)).fetchone()[0] == 5100.0
    assert db.execute('SELECT start_m FROM run_start WHERE run = ?', (pid,)).fetchone()[0] == 120.0
    assert db.execute('SELECT COUNT(*) FROM laps WHERE run = ?', (pid,)).fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM segments WHERE run = ?', (pid,)).fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM traces WHERE run = ?', (pid,)).fetchone()[0] == 1
    # derived rows of a traced run are left to the backfill; the launch metric it cannot redo comes along
    assert db.execute('SELECT COUNT(*) FROM corners WHERE run = ?', (pid,)).fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM events WHERE run = ?', (pid,)).fetchone()[0] == 0
    assert [r[0] for r in db.execute('SELECT name FROM metrics WHERE run = ?', (pid,))] == ['launch.t50']
    assert db.execute('SELECT session FROM metrics WHERE run = ?', (pid,)).fetchone()[0] == ported['session']
    traceless = runs[3]['id']
    assert db.execute('SELECT COUNT(*) FROM corners WHERE run = ?', (traceless,)).fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM metrics WHERE run = ?', (traceless,)).fetchone()[0] == 1
    # changes of gear: with their run, and the session's own
    shifts = db.execute('SELECT session, run FROM shifts WHERE session = ?', (ported['session'],)).fetchall()
    assert sorted((r['run'] or 0) for r in shifts) == [0, pid]
    assert db.execute('SELECT note FROM labels WHERE run = ?', (runs[2]['id'],)).fetchone()[0] == 'mine'
    # a tune per session, on the right car
    assert db.execute('SELECT t.car FROM sessions s JOIN tunes t ON t.id = s.tune WHERE s.id = ?',
                      (ported['session'],)).fetchone()[0] == cars['acr/Car']
    # the stale derived and state rows of a stage that got runs are gone
    assert db.execute('SELECT COUNT(*) FROM stage_potential').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM stage_lines').fetchone()[0] == 0
    stage = db.execute("SELECT * FROM stages WHERE key = 'acr:alsace:sommet-de-munster'").fetchone()
    assert (stage['surface_prior'], stage['surface_prior_source']) == ('tarmac', 'user')
    assert stage['runs'] == 2
    assert db.execute("SELECT runs FROM stages WHERE key = 'acr:wales:afon-bidno-severn'").fetchone()[0] == 2
    assert os.listdir(str(tmp / 'backup'))
    assert any(line.startswith('merged into') for line in lines)


def test_the_learnt_model_with_more_data_is_kept(files):
    target, _, _ = files
    run(files)
    db = connect(target)
    car = json.loads(db.execute("SELECT model FROM cars WHERE key = 'acr/Car'").fetchone()[0])
    assert car['pulls'] == 50                                       # the target's: it learnt more


def test_the_source_model_is_taken_when_it_has_more(files):
    target, source, _ = files
    s = open_store(source)
    s.save_model('ACR', 'acr/Car', 'acr', 'Car', model(500, gears=5))
    s.db.close()
    run(files)
    car = json.loads(connect(target).execute("SELECT model FROM cars WHERE key = 'acr/Car'").fetchone()[0])
    assert car['pulls'] == 500 and car['key'] == 'acr/Car'


def test_coach_state_is_merged_not_summed(files):
    target, _, _ = files
    run(files)
    db = connect(target)
    row = db.execute("SELECT * FROM coach_state WHERE tip = 'tip.a'").fetchone()
    assert (row['profile'], row['first_shown'], row['last_shown'], row['times'], row['value']) == \
        ('AC Rally', 100.0, 300.0, 2, 3.0)
    assert db.execute("SELECT car FROM coach_state WHERE tip = 'tip.driver'").fetchone()[0] == 0
    run(files)
    assert connect(target).execute("SELECT times FROM coach_state WHERE tip = 'tip.a'").fetchone()[0] == 2


def test_a_second_run_adds_nothing(files):
    target, _, tmp = files
    run(files)
    after = counts(target)
    plan, lines = run(files)
    assert plan.changes() == 0 and counts(target) == after
    assert len(plan.duplicates) == 3
    assert any(line.startswith('nothing to merge') for line in lines)
    assert len(os.listdir(str(tmp / 'backup'))) == 1                # no second backup: nothing was written


def test_a_dry_run_writes_nothing(files):
    target, _, tmp = files
    before = open(target, 'rb').read()
    plan, lines = run(files, dry_run=True)
    assert open(target, 'rb').read() == before and not os.path.exists(str(tmp / 'backup'))
    assert plan.counts['runs'] == {'insert': 3} and any('dry run' in line for line in lines)
    assert plan.counts['corners'] == {'skip': 2, 'insert': 1}       # the traceless run's own


def test_an_error_leaves_the_target_as_it_was(files, monkeypatch):
    target, _, tmp = files
    before = open(target, 'rb').read()

    def broken(tdb, sdb, profile_map=None, plan=None):
        tdb.execute("INSERT INTO stages (key, game) VALUES ('x', 'acr')")
        raise merge_db.MergeError('boom')
    monkeypatch.setattr(merge_db, 'merge', broken)
    with pytest.raises(merge_db.MergeError):
        run(files)
    assert open(target, 'rb').read() == before and not os.path.exists(str(tmp / 'backup'))


def test_a_target_written_meanwhile_is_not_overwritten(files, monkeypatch):
    target, _, tmp = files
    real = merge_db.merge

    def meanwhile(tdb, sdb, profile_map=None, plan=None):
        other = sqlite3.connect(target)
        other.execute("UPDATE cars SET updated = 1e12")
        other.commit()
        other.close()
        return real(tdb, sdb, profile_map, plan)
    monkeypatch.setattr(merge_db, 'merge', meanwhile)
    with pytest.raises(merge_db.MergeError, match='written while merging'):
        run(files)
    assert counts(target)['runs'] == 1


def test_the_same_drive_in_both_is_kept_once_and_the_richer_copy_wins(tmp_path):
    target, source = str(tmp_path / 'target.db'), str(tmp_path / 'source.db')
    t = open_store(target)
    t.begin()
    _, plain = drive(t, 'AC Rally', 'acr/Car', 1000.0, seed=1, label='kept')
    _, same = drive(t, 'AC Rally', 'acr/Car', 3000.0, seed=4)
    t.commit()
    t.db.close()
    s = open_store(source)
    s.begin()
    drive(s, 'ACR', 'acr/Car', 1001.0, seed=1, clock=True)          # the same drive on the game's clock: richer
    drive(s, 'ACR', 'acr/Car', 3000.5, seed=4)                      # the same drive, no more data
    s.commit()
    s.db.close()
    files = (target, source, tmp_path)
    plan, _ = run(files)
    assert len(plan.duplicates) == 2 and plan.counts['runs'] == {'replace': 1, 'insert': 1, 'skip': 1}
    db = connect(target)
    runs = db.execute('SELECT id, started FROM runs ORDER BY started').fetchall()
    assert len(runs) == 2 and runs[1]['id'] == same
    replaced = runs[0]['id']
    assert replaced != plain and db.execute('SELECT clock FROM run_clock WHERE run = ?', (replaced,)).fetchone()
    assert db.execute('SELECT note FROM labels WHERE run = ?', (replaced,)).fetchone()[0] == 'kept'
    assert db.execute('SELECT COUNT(*) FROM shifts WHERE run = ?', (plain,)).fetchone()[0] == 0
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    plan, _ = run(files)
    assert plan.changes() == 0


def test_an_identical_trace_is_a_duplicate(tmp_path):
    target, source = str(tmp_path / 'target.db'), str(tmp_path / 'source.db')
    for path, profile, started in ((target, 'AC Rally', 1000.0), (source, 'ACR', 50000.0)):
        store = open_store(path)
        store.begin()
        drive(store, profile, 'acr/Car', started, seed=7)
        store.commit()
        store.db.close()
    plan, _ = run((target, source, tmp_path), dry_run=True)
    assert plan.counts['runs'] == {'skip': 1} and 'identical trace' in plan.duplicates[0]


def test_unknown_tables_are_refused(files):
    target, source, _ = files
    db = sqlite3.connect(source)
    db.execute('CREATE TABLE something_new (x)')
    db.commit()
    db.close()
    with pytest.raises(merge_db.MergeError, match='something_new'):
        run(files, dry_run=True)


def test_every_table_of_the_schema_has_a_decision(tmp_path):
    store = open_store(str(tmp_path / 'fresh.db'))
    names = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names == merge_db.KNOWN


def test_a_real_merge_needs_a_backup_dir(files):
    target, source, tmp = files
    with pytest.raises(merge_db.MergeError, match='backup'):
        merge_db.run_merge(target, source, dry_run=False, workdir=str(tmp / 'w'), out=lambda line: None)


def test_main_maps_profiles_and_reports(files, capsys):
    target, source, tmp = files
    assert merge_db.main([target, source, '--map-profile', 'ACR=AC Rally', '--dry-run',
                          '--workdir', str(tmp / 'w')]) == 0
    out = capsys.readouterr().out
    assert "'ACR' -> 'AC Rally'" in out and 'duplicates found: 0' in out


def test_without_a_profile_map_the_cars_come_in_under_their_own_profile(files):
    target, source, tmp = files
    plan = merge_db.run_merge(target, source, dry_run=True, workdir=str(tmp / 'w'), out=lambda line: None)
    assert plan.counts['cars'] == {'insert': 2} and any("'ACR'" in n for n in plan.notes)


def test_backfill_works_the_ported_runs_over_before_the_target_is_written(files):
    target, source, tmp = files
    lines = []
    merge_db.run_merge(target, source, {'ACR': 'AC Rally'}, dry_run=False, workdir=str(tmp / 'work'),
                       backup_dir=str(tmp / 'backup'), out=lines.append, backfill_profile='AC Rally')
    assert any(line.startswith('backfill') for line in lines)
    db = connect(target)
    assert db.execute('SELECT COUNT(*) FROM runs r JOIN traces t ON t.run = r.id WHERE r.run_class IS NULL'
                      ).fetchone()[0] == 0
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
