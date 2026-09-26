"""The telemetry database: the upgrade from the first schema, the writer's
rules (adoption, forget, stages) and a reader alongside the writer."""
import json
import math
import sqlite3

from oversteer import telemetry_store
from oversteer.telemetry_store import open_store, open_reader, SCHEMA_V1, TRACE_CHANNELS


def v1_database(path):
    """A file as Oversteer 0.13 left it: DiRT cars keyed in rad/s x 10."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_V1)
    f = math.pi / 3                                              # what the old decoder multiplied by
    cars = {
        'codemasters-7854-838-6': {'key': 'codemasters-7854-838-6', 'name': '7854 rpm, 6 gears',
                                   'limiter': 7215.0, 'top_seen': 7100.0, 'ratios': {'2': [330.0 * f] * 30},
                                   'upshifts': {'2': [6800.0 * f]}, 'power': {str(int(6000 * f // 100)): [100.0] * 5}},
        'forza-777': {'key': 'forza-777', 'name': 'My Forza car', 'limiter': 8000.0, 'ratios': {'3': [250.0] * 30}},
        'outgauge-beam': {'key': 'outgauge-beam', 'name': 'beam', 'limiter': 6000.0},
        'eawrc-17': {'key': 'eawrc-17', 'name': 'EA WRC car 17', 'limiter': 7000.0},
    }
    for key, model in cars.items():
        db.execute('INSERT INTO cars (profile, key, name, model, updated) VALUES (?, ?, ?, ?, 5)',
                   ('rally', key, model['name'], json.dumps(model)))
    dirt = db.execute("INSERT INTO sessions (profile, car, track, started, ended, limiter_time) "
                      "VALUES ('rally', 'codemasters-7854-838-6', NULL, 10, 20, 3.5)").lastrowid
    db.execute('INSERT INTO shifts (session, at, gear, rpm, best, throttle, method) VALUES (?, 11, 2, ?, ?, 1.0, ?)',
               (dirt, 6800.0 * f, 7000.0 * f, 'sequential'))
    db.execute('INSERT INTO shifts (session, at, gear, rpm, best, throttle, method) VALUES (?, 12, 3, 6000, NULL, 0.5, NULL)',
               (dirt,))
    forza = db.execute("INSERT INTO sessions (profile, car, track, started) VALUES ('rally', 'forza-777', 'x', 30)").lastrowid
    db.execute('INSERT INTO shifts (session, at, gear, rpm) VALUES (?, 31, 1, 7000)', (forza,))
    orphan = db.execute("INSERT INTO sessions (profile, car, started) VALUES ('rally', 'gone', 40)").lastrowid
    db.execute('INSERT INTO shifts (session, at, gear, rpm) VALUES (?, 41, 1, 7000)', (orphan,))
    db.commit()
    db.close()


def test_upgrade_from_the_first_schema(tmp_path):
    path = str(tmp_path / 'telemetry.db')
    v1_database(path)
    store = open_store(path)
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == telemetry_store.VERSION
    assert (tmp_path / 'telemetry.db.v0.bak').exists() and (tmp_path / 'telemetry.db.v1.bak').exists()
    cars = {k: n for k, n in store.cars('rally')}
    assert cars == {'codemasters/7500-800-6': '7500 rpm, 6 gears', 'forza/777': 'My Forza car',
                    'beamng/unknown': 'beam', 'eawrc/17': 'EA WRC car 17'}
    dirt = store.car('rally', 'codemasters/7500-800-6')
    assert dirt['game'] == 'codemasters' and dirt['model']['key'] == 'codemasters/7500-800-6'
    assert abs(dirt['model']['limiter'] - 7215.0 * 3 / math.pi) < 0.01        # the launch figure, rescaled
    assert abs(dirt['model']['ratios']['2'][0] - 330.0) < 0.01
    assert list(dirt['model']['power']) in (['59'], ['60'])
    history = store.history('rally', 'codemasters/7500-800-6')
    assert len(history) == 1 and history[0]['limiter_time'] == 3.5
    assert abs(history[0]['error'] + 200.0) < 0.01 and history[0]['methods'] == ['sequential']
    shifts = store.shifts(history[0]['id'])
    assert [(s['gear'], s['gear_to'], s['direction'], s['flat_out']) for s in shifts] == [
        (2, 3, 'up', 1), (3, 4, 'up', 0)]
    # The session of a car that was forgotten has nothing to be read against
    assert store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 2
    assert store.db.execute('SELECT COUNT(*) FROM shifts').fetchone()[0] == 3
    store.db.close()
    again = open_store(path)                                     # nothing more to do
    assert len(again.cars('rally')) == 4


def test_a_new_game_key_adopts_the_old_car(tmp_path):
    path = str(tmp_path / 'telemetry.db')
    v1_database(path)
    store = open_store(path)
    reader = open_reader(path)
    assert reader.car('rally', 'forza-fh/777')['key'] == 'forza/777'       # found before it is renamed
    old = store.car('rally', 'forza/777')['id']
    assert store.car_id('rally', 'forza-fh/777', 'forza-fh') == old
    car = reader.car('rally', 'forza-fh/777')
    assert car['key'] == 'forza-fh/777' and car['game'] == 'forza-fh'
    assert len(reader.history('rally', 'forza-fh/777')) == 1              # its sessions came along
    assert store.car_id('rally', 'dirt/7500-800-6', 'dirt') == store.car('rally', 'dirt/7500-800-6')['id']
    assert store.car_id('rally', 'wrcg/7500-800-6', 'wrcg') != old        # nothing left to adopt: a new car


def test_forget_keeps_sessions_runs_and_labels(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.save_model('p', 'dirt/1', 'dirt', 'car', {'key': 'dirt/1', 'name': 'car', 'limiter': 7000.0})
    store.add_tune(car, 1.0, {2: 330.0}, 'first')
    session = store.start_session('p', car, 'dirt', 1.0)
    run = store.start_run(session, 1, 1.0)
    store.set_label(run, surface='gravel')
    store.forget_model('p', 'dirt/1')
    assert store.car('p', 'dirt/1')['model'] == {'key': 'dirt/1', 'name': 'car'}
    assert store.tunes(car) == [] and len(store.sessions(car)) == 1
    assert store.labels_for('dirt') == [(run, {'discipline': None, 'surface': 'gravel', 'wet': None,
                                               'shifter': None, 'note': None})]


def test_a_run_on_a_new_stage(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'eawrc/17', 'eawrc')
    session = store.start_session('p', car, 'eawrc', 1.0, stage='eawrc:4:12')     # the stage row comes first
    run = store.start_run(session, 1, 1.0, stage='eawrc:4:12', stage_length=10200.0, start_pos=[1, 2, 3])
    store.end_run(run, ended=2.0, distance=10150.0, discipline='rally-stage', discipline_conf='game',
                  discipline_evidence=['EA SPORTS WRC sends stage telemetry'])
    stage = store.stage('eawrc:4:12')
    assert stage['game'] == 'eawrc' and stage['length'] == 10200.0 and stage['runs'] == 1
    runs = store.runs(session)
    assert runs[0]['start_pos'] == [1, 2, 3] and runs[0]['discipline_evidence'] == [
        'EA SPORTS WRC sends stage telemetry']


def test_stage_keys_match_within_tolerance(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    key = store.match_stage('dirt', 9843.4, 104.9)
    assert key == 'dirt:9843:100'
    store.upsert_stage(key, 'dirt', 9843.4)
    assert store.match_stage('dirt', 9844.2, 105.1) == key                  # the same stage over a boundary
    assert store.match_stage('dirt', 9843.4, 160.0) != key                  # same length, another start
    assert store.match_stage('dirt', 9900.0, 105.0) != key
    cell = store.match_cell('forza-fh', (10, -4, 2), 5230.0)
    assert cell == 'cell:forza-fh:10:-4:2:5200'
    store.upsert_stage(cell, 'forza-fh')
    assert store.match_cell('forza-fh', (11, -4, 3), 5300.0) == cell        # a neighbouring cell and heading
    assert store.match_cell('forza-fh', (10, -4, 2), 8000.0) != cell        # another route from the same start


def test_reader_alongside_the_writer(tmp_path):
    path = str(tmp_path / 'telemetry.db')
    store = open_store(path)
    reader = open_reader(path)
    store.begin()
    store.save_model('p', 'lfs/XRG', 'lfs', 'XRG', {'key': 'lfs/XRG'})
    assert reader.cars('p') == []                           # not committed yet: the reader is not blocked
    store.commit()
    assert reader.cars('p') == [('lfs/XRG', 'XRG')]


def test_traces_and_their_cap(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'lfs/XRG', 'lfs')
    session = store.start_session('p', car, 'lfs', 1.0)
    rows = [tuple(float(i + c) for c in range(len(TRACE_CHANNELS))) for i in range(100)]
    rows[5] = (None,) + rows[5][1:]
    runs = [store.start_run(session, n, float(n)) for n in (1, 2, 3)]
    for run in runs:
        store.add_trace(run, rows)
    back = store.trace(runs[0])
    assert len(back) == 100 and back[7] == rows[7] and math.isnan(back[5][0])
    size = store.db.execute('SELECT LENGTH(data) FROM traces WHERE run = ?', (runs[0],)).fetchone()[0]
    assert store.prune_traces(cap=size * 2) == 1                            # the oldest goes
    assert store.trace(runs[0]) is None and store.trace(runs[2]) is not None
    assert len(store.runs(session)) == 3                                    # its run stays


def test_session_shifter(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'lfs/XRG', 'lfs')
    session = store.start_session('p', car, 'lfs', 1.0)
    assert store.session_shifter(session) is None
    for i, method in enumerate(['sequential'] * 8 + ['paddles'] * 2):
        store.add_shift(session, None, {'at': i, 'gear': 2, 'gear_to': 3, 'rpm': 6000.0, 'method': method})
    assert store.session_shifter(session) == 'sequential'
    store.add_shift(session, None, {'at': 20, 'gear': 2, 'gear_to': 3, 'rpm': 6000.0, 'method': 'h-pattern'})
    assert store.session_shifter(session) == 'mixed'
    empty = store.start_session('p', car, 'lfs', 2.0)
    assert store.drop_session_if_empty(empty) and not store.drop_session_if_empty(session)


def test_calibration_versions(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert store.calibration('dirt', 'surface') is None
    assert store.save_calibration('dirt', 'surface', {'classes': []}, 6, 120, 0.7, False) == 1
    assert store.save_calibration('dirt', 'surface', {'classes': ['gravel']}, 8, 160, 0.93, True) == 2
    calibration = store.calibration('dirt', 'surface')
    assert calibration['deployed'] and calibration['version'] == 2 and calibration['model'] == {'classes': ['gravel']}


def test_a_session_is_labelled_run_by_run(tmp_path):
    from oversteer.telemetry_store import open_store
    store = open_store(str(tmp_path / 't.db'))
    car = store.car_id('rally', 'eawrc/17', 'eawrc')
    store.begin()
    session = store.start_session('rally', car, 'eawrc', 1.0)
    for n in (1, 2):
        store.start_run(session, n, float(n), 'eawrc:4:12')
    assert store.label_session(session, surface='gravel', shifter='h-pattern', at=5.0) == 2
    store.commit()
    assert [label['surface'] for _, label in store.labels_for('eawrc')] == ['gravel', 'gravel']


def test_two_old_keys_that_become_one(tmp_path):
    """'acpmf' and 'acpmf-unknown' are both acpmf/unknown now: the car that
    learnt more is kept and the other's sessions and shifts go to it."""
    path = str(tmp_path / 'telemetry.db')
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_V1)
    db.execute('INSERT INTO cars VALUES (?, ?, ?, ?, ?)', ('p', 'acpmf', None, json.dumps({'key': 'acpmf'}), 1.0))
    db.execute('INSERT INTO cars VALUES (?, ?, ?, ?, ?)',
               ('p', 'acpmf-unknown', None, json.dumps({'key': 'acpmf-unknown', 'ratios': {'1': [1.0] * 30}}), 1.0))
    for i, key in enumerate(('acpmf', 'acpmf-unknown', 'acpmf-unknown')):
        db.execute('INSERT INTO sessions (id, profile, car, started) VALUES (?, ?, ?, ?)', (i + 1, 'p', key, float(i)))
        db.execute('INSERT INTO shifts (session, at, gear, rpm) VALUES (?, ?, ?, ?)', (i + 1, float(i), 2, 6000.0))
    db.execute('PRAGMA user_version = 1')
    db.commit()
    db.close()
    store = open_store(path)
    [(car, model)] = store.db.execute('SELECT id, model FROM cars').fetchall()
    assert json.loads(model)['ratios'] == {'1': [1.0] * 30}
    assert store.db.execute('SELECT COUNT(*) FROM sessions WHERE car = ?', (car,)).fetchone()[0] == 3
    assert store.db.execute('SELECT COUNT(*) FROM shifts').fetchone()[0] == 3


def test_the_backup_has_what_is_still_in_the_log(tmp_path):
    """The last writes before a crash may be in the write-ahead log only: a
    copy of the file would miss them, the backup must not."""
    path = str(tmp_path / 'telemetry.db')
    v1_database(path)
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode = WAL')
    writer.execute('PRAGMA wal_autocheckpoint = 0')
    writer.execute("INSERT INTO cars (profile, key, name, model, updated) VALUES ('rally', 'forza-9', 'late', '{}', 6)")
    writer.commit()                                              # in the log, not in the file; left open
    open_store(path).db.close()
    backup = sqlite3.connect(str(tmp_path / 'telemetry.db.v1.bak'))
    assert backup.execute("SELECT name FROM cars WHERE key = 'forza-9'").fetchone() == ('late',)
    writer.close()


def test_a_reader_on_an_awkward_path(tmp_path):
    folder = tmp_path / 'odd?#%name'
    folder.mkdir()
    path = str(folder / 'telemetry.db')
    open_store(path).db.close()
    assert open_reader(path).db.execute('SELECT COUNT(*) FROM cars').fetchone() == (0,)


def _tables(game, *entries):
    """Shipped stage tables for a test: {game: {key: entry}}."""
    from oversteer import stage_tables
    table = {}
    for entry in entries:
        entry = dict(entry, key=stage_tables.stage_key(game, entry))
        table[entry['key']] = entry
    return {game: table}


def test_shipped_stages_are_seeded_and_matched_first(tmp_path, monkeypatch):
    from oversteer import stage_tables
    tables = _tables('dirt', {'location': 'Wales', 'stage': 'Fferm Wynt', 'length_m': 9843.62, 'start_z': 99.2,
                              'surface': 'gravel'},
                     {'location': 'Monte Carlo', 'stage': 'Pra d´Alart', 'length_m': 6000.0, 'start_z': 10.0,
                      'surface': 'mixed', 'surface_parts': ['tarmac', 'snow']})
    monkeypatch.setattr(stage_tables, '_tables', tables)
    store = open_store(str(tmp_path / 'telemetry.db'))
    store.upsert_stage('dirt:9843:110', 'dirt', 9843.4)                     # a measured key from before the table
    store.seed_stages(tables)
    wales = store.stage('dirt:9844:100')
    assert (wales['name'], wales['location'], wales['length']) == ('Fferm Wynt', 'Wales', 9843.62)
    assert wales['surface_prior'] is None                                   # one surface: the game's word, no prior
    monte = store.stage('dirt:6000:10')
    assert (monte['surface_prior'], monte['surface_prior_source']) == ('mixed:tarmac,snow', 'table')
    assert store.match_stage('dirt', 9843.4, 104.9) == 'dirt:9844:100'      # the table before the stored key
    assert store.match_stage('dirt', 9843.4, 160.0) == 'dirt:9843:160'      # another start: not that stage


def test_a_run_to_the_finish_matches_a_published_length(tmp_path, monkeypatch):
    from oversteer import stage_tables
    tables = _tables('wrcg', {'location': 'Rally Sweden', 'stage': 'Vargasen', 'length_m': 14520.0, 'surface': 'snow'},
                     {'location': 'Rally Sweden', 'stage': 'Lesjofors', 'length_m': 9450.0, 'surface': 'snow'},
                     {'location': 'Rally Sweden', 'stage': 'Lesjofors Reverse', 'length_m': 9450.0,
                      'surface': 'snow', 'reverse_of': 'Lesjofors'},
                     {'location': 'Rally Italia Sardegna', 'stage': 'Monte Lerno', 'length_m': 9500.0,
                      'surface': 'gravel'})
    monkeypatch.setattr(stage_tables, '_tables', tables)
    store = open_store(str(tmp_path / 'telemetry.db'))
    store.seed_stages(tables)
    assert store.match_distance('wrcg', 14400.0, (0, 0, 0))[0] == 'wrcg:rally-sweden:vargasen'   # within 1 %
    assert store.match_distance('wrcg', 14300.0, (0, 0, 0)) == (None, [])                        # not within 1 %
    key, candidates = store.match_distance('wrcg', 9460.0, (500.0, 0.0, 800.0))
    assert key is None and [c['stage'] for c in candidates] == ['Lesjofors', 'Lesjofors Reverse', 'Monte Lerno']
    # Once a run of each is known, where a run starts tells them apart
    car = store.car_id('p', 'wrcg/7000-800-6', 'wrcg')
    session = store.start_session('p', car, 'wrcg', 1.0)
    store.start_run(session, 1, 1.0, 'wrcg:rally-sweden:lesjofors', start_pos=[500.0, 3.0, 800.0])
    store.start_run(session, 2, 2.0, 'wrcg:rally-sweden:lesjofors-reverse', start_pos=[-2000.0, 3.0, 4000.0])
    store.start_run(session, 3, 3.0, 'wrcg:rally-italia-sardegna:monte-lerno', start_pos=[90.0, 1.0, 60.0])
    assert store.match_distance('wrcg', 9460.0, (520.0, 0.0, 790.0))[0] == 'wrcg:rally-sweden:lesjofors'
    assert store.match_distance('wrcg', 9460.0, (-1990.0, 0.0, 4010.0))[0] == 'wrcg:rally-sweden:lesjofors-reverse'
    assert store.match_distance('wrcg', 9460.0, (7000.0, 0.0, 7000.0))[0] is None     # none started there


def test_assetto_corsa_rally_by_its_track_name(tmp_path, monkeypatch):
    from oversteer import stage_tables
    tables = _tables('acr', {'location': 'Alsace', 'stage': 'Forêt de Munster', 'track': 'Alsace Forêt',
                             'length_m': 6800.0, 'surface': 'tarmac'},
                     {'location': 'Alsace', 'stage': 'Forêt de Saverne', 'track': 'Alsace Forêt', 'length_m': 9100.0,
                      'surface': 'tarmac'},
                     {'location': 'Monte Carlo', 'stage': 'St. Geniez - Sisteron',
                      'track': 'Monte Carlo St. Geniez - Sistero', 'length_m': 13100.0, 'surface': 'tarmac'})
    monkeypatch.setattr(stage_tables, '_tables', tables)
    store = open_store(str(tmp_path / 'telemetry.db'))
    # as the bridge sends them: ASCII, cut to 31 characters
    assert store.match_track('Monte Carlo St. Geniez - Sister') == 'acr:monte-carlo:st-geniez-sisteron'
    assert store.match_track('Alsace For_t', 7300.0) == 'acr:alsace:foret-de-munster'    # the nearer length
    # the shipped table: a cut stage's spline may be its whole route's
    from oversteer.stage_tables import load
    monkeypatch.setattr(stage_tables, '_tables', load())
    assert store.match_track('Alsace For_t', 10900.0) == 'acr:alsace:foret-de-munster'   # Munster's spline
    assert store.match_track('Alsace For_t', 6900.0) == 'acr:alsace:foret-de-munster'    # its own
    assert store.match_track('Alsace For_t', 9450.0) == 'acr:alsace:foret-de-saverne'
    assert store.match_track('Wales Cwmbiga', 12077.95) == 'acr:wales:cwmbiga-afon-biga'
    monkeypatch.setattr(stage_tables, '_tables', tables)
    assert store.match_track('Alsace For_t') is None                                   # one name, two stages
    assert store.match_track('Imola') is None


def test_the_shipped_tables():
    """Every table loads, every stage has a key of its own, a location, a
    name and a surface the detector knows."""
    from oversteer import stage_tables
    tables = stage_tables.load()
    assert {'dirt', 'wrcg', 'acr'} <= set(tables)
    for game, entries in tables.items():
        for key, entry in entries.items():
            assert entry['location'] and entry['stage'] and entry['confidence'] in ('high', 'medium', 'low')
            assert entry['surface'] in stage_tables.SINGLE + ('mixed',)
            surface, prior = stage_tables.surface_of(entry)
            assert (surface is None) != (prior is None), key
    assert stage_tables.surface_of({'surface': 'mixed', 'surface_parts': {'gravel': 0.98, 'tarmac': 0.02}}) == (
        'gravel', None)                                                        # 2 % tarmac: a gravel stage
    assert stage_tables.surface_of({'surface': 'mixed', 'surface_parts': {'tarmac': 0.62, 'gravel': 0.38}}) == (
        None, 'mixed:tarmac,gravel')
