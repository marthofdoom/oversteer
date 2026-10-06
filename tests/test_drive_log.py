"""The drive log: writes leave the listener for their own thread, and what
the thread writes is what an inline replay writes."""
import sqlite3
import threading
import time

from oversteer import drive_log
from oversteer.drive_log import DriveLog, SENT_LENGTH_TOLERANCE
from oversteer.shift_learner import ShiftLearner
from tests.sim import drive, exits


def test_events_run_on_the_thread_in_order(tmp_path):
    log = DriveLog(str(tmp_path / 'telemetry.db'))
    seen = []
    for i in range(100):
        log.post(lambda i=i: seen.append((i, threading.current_thread().name)))
    assert log.sync()
    assert [i for i, _ in seen] == list(range(100)) and {name for _, name in seen} == {'drive-log'}
    assert log.call(lambda: 42) == 42
    log.close()


def test_a_full_queue_drops_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_log, 'QUEUE_SIZE', 4)
    log = DriveLog(str(tmp_path / 'telemetry.db'))
    gate = threading.Event()
    log.post(gate.wait)                          # holds the thread
    time.sleep(0.05)
    posted = sum(log.post(lambda: None) for _ in range(10))
    assert posted == 4 and log.dropped == 6      # the listener never waits
    gate.set()
    assert log.sync()
    log.close()


def test_threaded_learner_writes_what_an_inline_one_does(tmp_path):
    rows = []
    for name, threaded in (('inline.db', False), ('threaded.db', True)):
        learner = ShiftLearner(str(tmp_path / name), threaded=threaded)
        learner.clock = lambda now: 1e9 + now
        exits(learner)
        drive(learner, shift_at=6000, runs=2)
        learner.save()
        history = learner.history('test-car')
        assert learner.history_changed >= 1
        rows.append(([(h['started'], h['shifts'], h['methods']) for h in history],
                     learner.db.execute('SELECT model FROM cars').fetchall(),
                     learner.db.execute('SELECT session, at, gear, rpm, method FROM shifts ORDER BY id').fetchall()))
        if threaded:
            learner.publish()                     # what the thread does once a second
            assert learner.snapshot() is learner.published
        learner.close()
    assert rows[0] == rows[1]


from tests.sim import Course, STAGE, STAGE_CORNERS, CIRCUIT, course_samples, feed_course   # noqa: E402


def drive_runs(tmp_path, *drives, name='telemetry.db'):
    """Feed each list of course samples in turn and end the session: the
    learner and the session's row."""
    learner = ShiftLearner(str(tmp_path / name))
    for samples in drives:
        feed_course(learner, samples)
    learner.save()
    reader = learner._reader()
    history = reader.history('_no_profile', drives[0][0][1].car)
    return learner, reader, reader.session(history[0]['id'])


def test_a_stage_is_one_run(tmp_path):
    course = Course(STAGE)
    learner, reader, session = drive_runs(tmp_path, course_samples(course))
    [run] = session['runs']
    assert abs(run['distance'] - course.length) < 20 and run['finished'] == 1
    assert run['stage'] == 'dirt:4241:100'                  # DiRT names no stage: its length and start do
    assert 150 < run['duration'] < 170 and run['moving_time'] <= run['duration']
    segments = reader.segments(run['id'])
    assert len(segments) == int(course.length // 200)
    assert all(s['features']['n'] > 100 and s['features']['speed_mean'] > 5 for s in segments)
    trace = reader.trace(run['id'])
    assert abs(len(trace) - run['duration'] * 10) < 20
    corners = reader.corners(run['id'])
    assert [c['direction'] for c in corners] == STAGE_CORNERS
    assert all(c['min_speed'] < c['entry_speed'] and c['counter_steer'] == 0.0 for c in corners)
    assert session['distance'] == run['distance'] and reader.stage('dirt:4241:100')['runs'] == 1


def test_a_restart_is_a_new_run_of_the_same_stage(tmp_path):
    course = Course(STAGE)
    first = course_samples(course)[:4000]                    # given up half way
    again = course_samples(Course(STAGE, start=(100.0, 20.0, 105.1)), t=first[-1][0] + 5)
    learner, reader, session = drive_runs(tmp_path, first, again)
    runs = session['runs']
    assert [r['finished'] for r in runs] == [0, 1]
    assert runs[0]['stage'] == runs[1]['stage']             # start z 104.9 and 105.1: one stage


def test_counter_steer_in_a_slide(tmp_path):
    course = Course(STAGE)
    learner, reader, session = drive_runs(tmp_path, course_samples(course, slide=True))
    corners = reader.corners(session['runs'][0]['id'])
    left = [c['counter_steer'] for c in corners if c['direction'] == 1]
    right = [c['counter_steer'] for c in corners if c['direction'] == -1]
    assert all(c > 0.3 for c in left) and all(c == 0.0 for c in right)


def test_ea_wrc_packets_start_and_end_a_run(tmp_path):
    course = Course(STAGE)
    samples = course_samples(course, game='eawrc', car='eawrc/17', packets=True)
    t = samples[-1][0]
    # after the end the game goes on sending the car standing in the service area
    after = [(t + i / 60, s, 0.0) for i, (_, s, _) in enumerate(course_samples(Course([('straight', 200)]),
                                                                           game='eawrc', car='eawrc/17')[:300])]
    for _, s, _ in after:
        s.packet = 'update'
    learner, reader, session = drive_runs(tmp_path, samples, after)
    [run] = session['runs']
    assert run['finished'] == 1 and run['stage'] == 'eawrc:4:12'


def test_a_circuit_has_laps(tmp_path):
    course = Course(CIRCUIT)
    learner, reader, session = drive_runs(tmp_path, course_samples(course, laps=3, rolling=True))
    [run] = session['runs']
    laps = reader.db.execute('SELECT n, time, distance FROM laps WHERE run = ?', (run['id'],)).fetchall()
    assert [n for n, _, _ in laps] == [1, 2]
    assert all(abs(d - course.length) < 30 for _, _, d in laps)


def test_a_moment_in_the_menus_is_no_run(tmp_path):
    course = Course(STAGE)
    learner, reader, session = drive_runs(tmp_path, course_samples(course)[:400], name='a.db')
    assert session is None or session['runs'] == []


def test_segment_features():
    """Rough gravel shakes the suspension fast and each side on its own;
    spin shows at full throttle in the higher gears; a segment votes on
    the surface only once it reached the grip limit."""
    import math
    import random
    from oversteer.drive_log import segment_features, pushed, SEGMENT_CHANNELS
    rng = random.Random(1)

    def rows(shake, same_sides, lateral):
        out = []
        for i in range(600):
            t = i / 60
            left = shake * rng.gauss(0, 1)
            right = left if same_sides else shake * rng.gauss(0, 1)
            corner = lateral * max(0.0, math.sin(t))              # into a corner and out
            row = dict(t=t, speed=20.0, a_long=0.5, a_lat=corner, yaw_rate=None, throttle=1.0, gear=3,
                       slip=0.12 if i % 4 == 0 else 0.02, susp_fl=None, susp_fr=None, susp_vel_fl=left,
                       susp_vel_fr=right, slip_angle=None, puddle=None, rumble=None, steer=0.0)
            out.append(tuple(row[c] for c in SEGMENT_CHANNELS))
        return out
    gravel = segment_features(rows(0.2, False, 8.0))
    tarmac = segment_features(rows(0.02, True, 8.0))
    assert gravel['rough'] > 5 * tarmac['rough']
    assert gravel['lr_corr'] < 0.3 < 0.9 < tarmac['lr_corr']
    assert abs(gravel['spin'] - 0.25) < 0.01
    assert pushed(gravel) == 1 and gravel['mu_p95'] > 0.7
    cruising = segment_features(rows(0.02, True, 0.3))
    assert pushed(cruising) == 0                                   # never near the limit: no vote


def test_a_pause_is_not_driving(tmp_path):
    """EA SPORTS WRC's pause packets suspend the run: its duration leaves
    the pause out and it stays one run."""
    course = Course(STAGE)
    samples = course_samples(course, game='eawrc', car='eawrc/17', packets=True)
    half = len(samples) // 2
    t_pause = samples[half][0]
    paused = []
    for i in range(1800):                                           # 30 s paused
        s = samples[half][1]
        from oversteer.telemetry import Sample
        p = Sample(s.rpm, s.max_rpm, gear=s.gear, speed=0.0, car=s.car, game='eawrc')
        p.pos, p.packet, p.stage_time, p.distance = s.pos, 'pause', s.stage_time, s.distance
        paused.append((t_pause + i / 60, p, 0.0))
    rest = [(t + 30, s, th) for t, s, th in samples[half + 1:]]
    learner, reader, session = drive_runs(tmp_path, samples[:half + 1], paused, rest)
    [run] = session['runs']
    assert run['finished'] == 1 and 150 < run['duration'] < 170


def test_the_listener_stays_within_its_budget(tmp_path):
    """Two minutes of EA SPORTS WRC through the live path with the drive-log
    thread: a loose check (the bench, tests/bench_telemetry.py, measures)."""
    from tests.bench_telemetry import measure
    from tests.sim import stage_packets
    mean, p99, worst, dropped = measure(stage_packets(2.0))
    assert mean < 2.0 and dropped == 0


def test_a_replay_writes_the_same_database_every_time(tmp_path):
    from oversteer.telemetry_capture import CaptureWriter, read_capture, replay
    from tests.sim import stage_packets
    path = str(tmp_path / 'stages.ovcap.gz')
    with CaptureWriter(path, port=5310) as writer:
        for t, data in stage_packets(4.0):
            writer.write(t, ('127.0.0.1', 5555), data)
    dumps = []
    for name in ('one.db', 'two.db'):
        learner = ShiftLearner(str(tmp_path / name))
        meta, records = read_capture(path)
        replay(records, learner, started=meta['started'])
        tables = ('cars', 'tunes', 'stages', 'sessions', 'runs', 'segments', 'corners', 'laps', 'shifts', 'traces')
        dumps.append({t: learner.db.execute('SELECT * FROM ' + t).fetchall() for t in tables})
        learner.close()
    assert dumps[0] == dumps[1]
    assert len(dumps[0]['runs']) >= 2 and len(dumps[0]['segments']) > 30 and dumps[0]['shifts']


def test_forza_motorsport_laps_through_the_live_path(tmp_path):
    """Three laps sent as Forza Motorsport's packets, through the decoder
    and the listener as a game would send them: one circuit run with its
    laps, the gearing and the tyre radius learnt."""
    from oversteer.telemetry_capture import replay
    from tests.sim import RATIOS, forza_packets
    course = Course(CIRCUIT)
    samples = course_samples(course, laps=3, game='forza-fm', car='forza-fm/777')
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'), threaded=False)
    replay([(t, ('127.0.0.1', 0), data) for t, data in forza_packets(samples)], learner, started=1.7e9)
    reader = learner._reader()
    [car] = reader.db.execute('SELECT id, key, game FROM cars').fetchall()
    assert car[1:] == ('forza-fm/777', 'forza-fm')
    [session] = reader.sessions(car[0])
    assert (session['discipline'], session['discipline_conf'], session['stage']) == ('circuit', 'game', 'fm:512')
    assert abs(session['distance'] - course.length * 3) < 60
    [run] = reader.session(session['id'])['runs']
    laps = reader.db.execute('SELECT n, distance FROM laps WHERE run = ?', (run['id'],)).fetchall()
    assert [n for n, _ in laps] == [1, 2] and all(abs(d - course.length) < 30 for _, d in laps)
    corners = reader.db.execute('SELECT direction FROM corners WHERE run = ?', (run['id'],)).fetchall()
    assert len(corners) >= 8 and {d for d, in corners} == {1}          # every corner of this circuit is a left
    learnt = {row['gear']: row['ratio'] for row in learner.snapshot()['gears']}
    assert all(abs(learnt[g] - RATIOS[g]) / RATIOS[g] < 0.01 for g in learnt) and len(learnt) >= 3
    [tune] = reader.db.execute('SELECT tyre_radius FROM tunes').fetchall()
    assert tune[0] is not None and abs(tune[0] - 0.33) < 0.005
    learner.close()


def test_a_rolled_back_batch_leaves_no_stale_row_ids(tmp_path, monkeypatch):
    """A batch that cannot be committed (a full disk) is rolled back and
    SQLite hands its row ids out again: the session written in it starts
    again, and nothing lands in a row that is not its own."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    store = learner.log.store
    real_commit = store.commit
    monkeypatch.setattr(store, 'commit', lambda: None)       # one batch, as on the thread
    samples = course_samples(Course(STAGE))
    feed_course(learner, samples[:1500])
    first = learner.session
    assert first in learner._session_rows and learner.runs.run_rows

    def full():
        raise sqlite3.OperationalError('database or disk is full')
    monkeypatch.setattr(store, 'commit', full)
    learner.log._commit()
    assert learner._session_rows == {} and learner.runs.run_rows == {}
    assert store.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
    monkeypatch.setattr(store, 'commit', real_commit)
    feed_course(learner, samples[1500:])
    assert learner.session != first
    learner.save()
    [(session, runs)] = store.db.execute('SELECT s.id, COUNT(r.id) FROM sessions s JOIN runs r ON r.session = s.id '
                                         'GROUP BY s.id').fetchall()
    assert runs == 1 and learner._session_rows == {}


def test_a_failing_call_does_not_keep_the_caller_waiting(tmp_path):
    log = DriveLog(str(tmp_path / 'telemetry.db'))

    def broken():
        raise ValueError('broken')
    started = time.monotonic()
    assert log.call(broken, timeout=2.0) is None
    assert time.monotonic() - started < 1.0
    log.close()


def test_forza_above_100_m_s_is_one_run(tmp_path):
    """At 60 packets a second the jump between two positions implies the
    car's own speed: 396 km/h is no teleport."""
    samples = course_samples(Course([('straight', 4000)]), game='forza-fh', car='forza-fh/1', top=110.0)
    x, last = 0.0, samples[0][0]
    for t, sample, _ in samples:                                  # positions that move at the car's speed
        x += sample.speed * (t - last)
        last = t
        sample.pos = (x, 20.0, 0.0)
    assert max(s.speed for _, s, _ in samples) > 105
    learner, reader, session = drive_runs(tmp_path, samples)
    [run] = session['runs']
    assert run['distance'] > 3900 and learner.runs._runs == 1


def test_a_long_pause_keeps_no_rows(tmp_path):
    """DiRT repeats its last packet while paused, a parked car sends on at
    packet rate: neither piles up rows for the segment being driven."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    samples = course_samples(Course(STAGE))
    t = feed_course(learner, samples[:3000])
    runs = learner.runs
    rows, trace = len(runs._rows), len(runs._trace)
    _, last, _ = samples[2999]
    from oversteer.telemetry import Sample
    for i in range(20 * 60 * 60 // 10):                          # 2 minutes of a frozen DiRT pause
        p = Sample(last.rpm, last.max_rpm, gear=last.gear, speed=0.0, car=last.car, game=last.game)
        p.pos, p.stage_time, p.lap_distance = last.pos, last.stage_time, last.lap_distance
        t += 1 / 60
        learner.feed(t, p, 7500.0, 0.0, 0.0)
    assert (len(runs._rows), len(runs._trace)) == (rows, trace)
    stage_time = last.stage_time
    for i in range(10000):                                        # parked with the clock running
        p = Sample(900.0, 7500.0, gear=1, speed=0.0, car=last.car, game=last.game)
        stage_time += 1 / 60
        p.pos, p.stage_time, p.lap_distance = last.pos, stage_time, last.lap_distance
        t += 1 / 60
        learner.feed(t, p, 7500.0, 0.0, 0.0)
    assert len(runs._rows) <= drive_log.SEGMENT_ROWS


def wrcg_samples(course, finish, overrun_clock=True):
    """WRC Generations as it seems to be: no stage length, no progress; the
    stage clock stops at `finish` m while the car rolls on."""
    samples = course_samples(course, game='wrcg', car='wrcg/7500-800-5')
    stopped = None
    for _, s, _ in samples:
        s.stage_length = s.progress = None
        if s.lap_distance is not None and s.lap_distance >= finish and overrun_clock:
            stopped = s.stage_time if stopped is None else stopped
            s.stage_time = stopped
    return samples


def test_wrc_generations_stage_by_the_distance_to_the_finish(tmp_path, monkeypatch):
    from oversteer import stage_tables
    from tests.test_store import _tables
    length = Course(STAGE).length
    tables = _tables('wrcg', {'location': 'Rally Sweden', 'stage': 'Vargasen', 'length_m': length + 20.0,
                              'surface': 'snow'},
                     {'location': 'Rally Sweden', 'stage': 'Hof-Finnskog', 'length_m': length * 1.5, 'surface': 'snow'})
    monkeypatch.setattr(stage_tables, '_tables', tables)
    # 300 m past the line to stop control: beyond 1 %, the stopped clock marks the finish
    course = Course(STAGE + [('straight', 300)])
    learner, reader, session = drive_runs(tmp_path, wrcg_samples(course, length))
    [run] = session['runs']
    assert run['stage'] == 'wrcg:rally-sweden:vargasen'
    assert (run['surface'], run['surface_conf']) == ('snow', 'game')
    assert 'Vargasen (Rally Sweden) is snow' in run['surface_evidence'][0]
    stage = reader.stage(run['stage'])
    assert (stage['name'], stage['location'], stage['runs']) == ('Vargasen', 'Rally Sweden', 1)


def test_wrc_generations_two_stages_of_one_length(tmp_path, monkeypatch):
    """A stage and its reverse, one length, neither driven before: which
    stage stays open (a start cell key), but where is known."""
    from oversteer import stage_tables
    from tests.test_store import _tables
    length = Course(STAGE).length
    tables = _tables('wrcg', {'location': 'Rally Sweden', 'stage': 'Lesjofors', 'length_m': length, 'surface': 'snow'},
                     {'location': 'Rally Sweden', 'stage': 'Lesjofors Reverse', 'length_m': length,
                      'surface': 'snow', 'reverse_of': 'Lesjofors'})
    monkeypatch.setattr(stage_tables, '_tables', tables)
    learner, reader, session = drive_runs(tmp_path, wrcg_samples(Course(STAGE), length, overrun_clock=False))
    [run] = session['runs']
    assert run['stage'].startswith('cell:wrcg:')
    assert reader.stage(run['stage'])['location'] == 'Rally Sweden'
    assert (run['surface'], run['surface_conf']) == ('snow', 'game')


def test_a_stage_given_up_is_not_matched_by_distance(tmp_path, monkeypatch):
    """Where the game sends progress, a run that stops short of the finish
    is not the stage whose length it happens to have driven."""
    from oversteer import stage_tables
    from tests.test_store import _tables
    monkeypatch.setattr(stage_tables, '_tables', _tables('wrcg', {'location': 'Wales', 'stage': 'Short',
                                                                  'length_m': 2000.0, 'surface': 'gravel'}))
    samples = [x for x in course_samples(Course(STAGE), game='wrcg', car='wrcg/7500-800-5')
               if x[1].lap_distance is None or x[1].lap_distance < 2000.0]
    for _, s, _ in samples:
        s.stage_length = None
    learner, reader, session = drive_runs(tmp_path, samples)
    [run] = session['runs']
    assert run['finished'] != 1 and run['stage'].startswith('cell:wrcg:')


def test_two_stages_of_one_length_on_one_surface(tmp_path, monkeypatch):
    """A location with more than one surface: the stages the distance
    leaves open still share one."""
    from oversteer import stage_tables
    from tests.test_store import _tables
    length = Course(STAGE).length
    tables = _tables('wrcg', {'location': 'Rally de Portugal', 'stage': 'Felgueiras', 'length_m': length,
                              'surface': 'gravel'},
                     {'location': 'Rally de Portugal', 'stage': 'Felgueiras reverse', 'length_m': length,
                      'surface': 'gravel', 'reverse_of': 'Felgueiras'},
                     {'location': 'Rally de Portugal', 'stage': 'Lousada', 'length_m': 3580.0, 'surface': 'mixed',
                      'surface_parts': {'tarmac': 0.62, 'gravel': 0.38}})
    monkeypatch.setattr(stage_tables, '_tables', tables)
    learner, reader, session = drive_runs(tmp_path, wrcg_samples(Course(STAGE), length, overrun_clock=False))
    [run] = session['runs']
    assert run['stage'].startswith('cell:wrcg:') and (run['surface'], run['surface_conf']) == ('gravel', 'game')
    assert 'Felgueiras (Rally de Portugal) or Felgueiras reverse (Rally de Portugal): all gravel' in \
        run['surface_evidence'][0]


def test_a_burst_of_packets_or_a_reset_is_no_teleport(tmp_path):
    # Packets arriving together (gap ~0.5 ms) with the car's usual half
    # metre between them implied 1000 m/s and split a WRCG stage in four;
    # a reset after a crash moves the car a few metres
    from oversteer.drive_log import RunTracker
    tracker = RunTracker.__new__(RunTracker)
    tracker._session, tracker._last_pos, tracker._last_speed = 1, (0.0, 0.0, 0.0), 25.0
    tracker._last_t, tracker._last_stage_time, tracker._last_lap = 10.0, 50.0, None
    tracker._last_lap_distance = tracker._last_track = None
    from oversteer.telemetry import Sample
    burst = Sample(5000.0, speed=25.0)
    burst.pos, burst.stage_time = (0.5, 0.0, 0.0), 50.0
    assert tracker._boundary(10.0005, burst, 1) is None
    reset = Sample(0.0, speed=0.0)
    reset.pos, reset.stage_time = (8.0, 0.0, 6.0), 50.2
    assert tracker._boundary(10.02, reset, 1) is None
    restart = Sample(0.0, speed=0.0)
    restart.pos, restart.stage_time = (2000.0, 0.0, 0.0), 50.2
    assert tracker._boundary(10.02, restart, 1) == 'teleport'


def test_a_sent_length_tells_a_stage_from_its_reverse(tmp_path, monkeypatch):
    from oversteer import stage_tables
    from oversteer.telemetry_store import open_store
    from tests.test_store import _tables
    monkeypatch.setattr(stage_tables, '_tables', _tables(
        'wrcg', {'location': 'Rally México', 'stage': 'Media Luna', 'length_m': 3979.648829, 'surface': 'gravel'},
        {'location': 'Rally México', 'stage': 'Media Luna reverse', 'length_m': 3932.115078, 'surface': 'gravel'},
        {'location': 'Rally Sweden', 'stage': 'Sävar', 'length_m': 7885.974407, 'surface': 'snow'},
        {'location': 'Rally Sweden', 'stage': 'Sävar reverse', 'length_m': 7885.979652, 'surface': 'snow'},
        {'location': 'Rally México', 'stage': 'Autódromo de León', 'length_m': 2581.312418, 'surface': 'tarmac',
         'alt_codes': [{'code': 'VersusRaceTrack', 'length_m': 2581.109047}]}))
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert store.match_distance('wrcg', 3979.6488285064697, within=SENT_LENGTH_TOLERANCE)[0] == \
        'wrcg:rally-mexico:media-luna'                                               # the float the game sent
    assert store.match_distance('wrcg', 3960.0, within=SENT_LENGTH_TOLERANCE) == (None, [])
    # 5 mm apart: the float names one; an unknown start does not undo it
    assert store.match_distance('wrcg', 7885.9796524, (1.0, 0.0, 1.0), within=SENT_LENGTH_TOLERANCE)[0] == \
        'wrcg:rally-sweden:savar-reverse'
    assert store.match_distance('wrcg', 7885.977, within=SENT_LENGTH_TOLERANCE)[0] is None     # between them
    assert store.match_distance('wrcg', 2581.1090469, within=SENT_LENGTH_TOLERANCE)[0] == \
        'wrcg:rally-mexico:autodromo-de-leon'                                        # its versus layout
    assert [e['stage'] for e in store.match_distance('wrcg', 3960.0)[1]] == ['Media Luna', 'Media Luna reverse']


def test_the_shipped_wrc_generations_table_names_marths_mexico_stages(tmp_path):
    """The lengths WRC Generations sent on the first recorded run
    (20260925-145356.ovcap.gz), against the shipped table."""
    from oversteer import stage_tables
    from oversteer.telemetry_store import open_store
    stage_tables.set_tables(None)
    store = open_store(str(tmp_path / 'telemetry.db'))
    key = store.match_distance('wrcg', 3979.6488285064697, (0.0, 0.0, 0.0), within=SENT_LENGTH_TOLERANCE)[0]
    assert key == 'wrcg:rally-mexico:media-luna'
    assert stage_tables.entry(key)['code'] == 'SS1_RaceTrack' and not stage_tables.entry(key)['reverse_of']
    key = store.match_distance('wrcg', 2581.312417984009, within=SENT_LENGTH_TOLERANCE)[0]
    assert stage_tables.entry(key)['stage'] == 'Autódromo de León'
    # every stage its own length: no two within 5 mm
    lengths = sorted(e['length_m'] for e in stage_tables.tables()['wrcg'].values())
    assert all(b - a > 0.005 for a, b in zip(lengths, lengths[1:]))


def acr_drive(t0, start, until, track='Wales Afon Bidno', speed=22.0, car='acr/Skoda Fabia RS Rally2'):
    """An Assetto Corsa Rally drive as the bridge sends it: no position or
    stage clock, the distance along the stage's spline from the start line."""
    from oversteer.telemetry import Sample
    samples, t, d = [], t0, start
    for _ in range(20):                                        # standing at the line
        s = Sample(1500.0, 7500.0, gear=1, speed=0.0, car=car, game='acr', throttle=0.0)
        s.track, s.lap_distance, s.stage_length = track, d, 5599.8
        samples.append((t, s, 0.0))
        t += 0.1
    while d < until:
        s = Sample(6000.0, 7500.0, gear=3, speed=speed, car=car, game='acr', throttle=0.8)
        s.track, s.lap_distance, s.stage_length = track, d, 5599.8
        samples.append((t, s, 0.8))
        t += 0.1
        d += speed * 0.1
    return samples


def test_acr_finish_restart_and_shared_names(tmp_path):
    # Afon Bidno - Severn: start 238 m, last pace note 5510 m. An attempt
    # given up at 1500 m, the stage restarted (the distance jumps back to
    # the line), then a run past the last note: two runs, the second finished
    first = acr_drive(0.0, 238.0, 1500.0)
    second = acr_drive(first[-1][0] + 0.1, 238.0, 5530.0)
    learner, reader, session = drive_runs(tmp_path, first + second)
    runs = [r for r in session['runs'] if r['distance'] > 300]
    assert [r['finished'] for r in runs] == [0, 1]
    assert all(r['stage'] == 'acr:wales:afon-bidno-severn' for r in runs)
    assert 225 < runs[1]['result_time'] < 235              # (5287 - 238) / 22 m/s from moving off: the flying finish, not the stop control


def test_a_run_stores_where_along_the_spline_it_began(tmp_path):
    standing = acr_drive(0.0, 238.0, 1500.0)
    learner, reader, session = drive_runs(tmp_path, standing)
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert reader.run_start(run['id']) == 238.0
    mid = acr_drive(0.0, 3000.0, 3600.0, track='Wales Afon Bidno')        # a run that began mid-stage, rolling
    learner, reader, session = drive_runs(tmp_path, mid, name='mid.db')
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert reader.run_start(run['id']) == mid[0][1].lap_distance


def test_an_acr_run_that_began_mid_stage_is_not_finished_at_the_flying_finish(tmp_path):
    """A restart after a silence (or the second half of a run split by one) starts anywhere on the stage:
    crossing the finish from there is not the stage's time."""
    mid = acr_drive(0.0, 3000.0, 5530.0)
    learner, reader, session = drive_runs(tmp_path, mid)
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert run['finished'] != 1 and run['result_time'] is None
    # a run that began at the line still is
    learner, reader, session = drive_runs(tmp_path, acr_drive(0.0, 238.0, 5530.0), name='line.db')
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert run['finished'] == 1


def test_an_acr_stage_is_not_guessed_from_the_distance_driven(tmp_path):
    """ACR names its stage (the track name): a track the table does not know stays unnamed, however far the
    run drove against some other stage's published length."""
    drive = acr_drive(0.0, 0.0, 5600.0, track='Nowhere Unknown')
    learner, reader, session = drive_runs(tmp_path, drive)
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert run['stage'] is None


def test_acr_stages_of_one_name_told_apart_by_the_start():
    from oversteer import stage_tables
    # "Alsace For_t" is Foret de Munster (first note 3811.6) or de Saverne
    # (132.3): a run starting at 3773 m is Munster, whatever the spline says
    assert stage_tables.acr_stage('Alsace For_t', start=3773.0, length=10927.0)['stage'] == 'Forêt de Munster'
    assert stage_tables.acr_stage('Alsace For_t', start=100.0, length=10927.0)['stage'] == 'Forêt de Saverne'
    assert stage_tables.acr_stage('Wales Afon Bidno', start=238.0)['stage'] == 'Afon Bidno - Severn'


def test_acc_laps_are_not_restarts(tmp_path):
    # AC and ACC: the lap distance wraps at the line with numberOfLaps 0 in
    # practice; the ACR restart rule must not cut such a session per lap
    laps = []
    t = 0.0
    for lap in range(3):
        drive = acr_drive(t, 0.0, 3000.0, track='Monza', car='acc/ferrari_296_gt3')
        for _, s, _throttle in drive:
            s.game = 'acc'
        laps += drive[20:] if lap else drive
        t = laps[-1][0] + 0.1
    learner, reader, session = drive_runs(tmp_path, laps)
    assert len([r for r in session['runs'] if r['distance'] > 300]) == 1


def test_a_run_knows_its_surface_from_the_stage(tmp_path):
    """The learner's best points are per surface: an Assetto Corsa Rally
    run on Greece Elatia (85 % gravel) is on gravel from the stage table,
    a DiRT run on a stage its earlier runs taught nothing about on none."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    surfaces = []
    for t, sample, throttle in acr_drive(0.0, 100.0, 600.0, track='Greece Elatia'):
        learner.feed(t, sample, 7500.0, throttle, 0.0)
        surfaces.append(learner.surface)
    assert surfaces[0] is None and surfaces[-1] == 'gravel'
    assert learner.car.last_surface in (None, 'gravel')
    learner.save()
    other = ShiftLearner(str(tmp_path / 'other.db'))
    feed_course(other, course_samples(Course(STAGE)))
    assert other.surface is None


def acr_after_finish(t0, d, track='Wales Afon Bidno', seconds=60.0, speed=0.0, car='acr/Skoda Fabia RS Rally2'):
    """What ACR keeps sending after the finish: the results screen, the car
    at `speed` on the spot (lap distance stays where it was)."""
    from oversteer.telemetry import Sample
    samples, t = [], t0
    while t < t0 + seconds:
        s = Sample(1500.0, 7500.0, gear=1, speed=speed, car=car, game='acr', throttle=0.0)
        s.track, s.lap_distance, s.stage_length = track, d, 5599.8
        samples.append((t, s, 0.0))
        t += 0.1
        d += speed * 0.1
    return samples


def test_acr_run_ends_when_the_car_rests_after_the_finish(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive = acr_drive(0.0, 238.0, 5530.0)
    feed_course(learner, drive)
    assert learner.runs.run is not None                    # not over yet: nothing past the line
    before = learner.history_changed
    after = acr_after_finish(drive[-1][0] + 0.1, 5530.0, seconds=5.0)
    feed_course(learner, after[:10])
    assert learner.runs.run is not None                    # a second of standing is not enough
    feed_course(learner, after[10:])
    learner.log.sync()
    assert learner.runs.run is None                        # over seconds after the line, not at the next stage
    assert learner.history_changed > before
    reader = learner._reader()
    session = reader.session(reader.history('_no_profile', drive[0][1].car)[0]['id'])
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert run['finished'] == 1 and 225 < run['result_time'] < 235
    assert run['distance'] < 5400                          # the standing seconds are not in it


def test_acr_finish_is_the_flying_finish_else_the_last_pace_note(tmp_path):
    from oversteer import stage_tables
    bidno = stage_tables.acr_stage('Wales Afon Bidno', start=238.0)
    assert bidno['pacenote_last_m'] - 300 < bidno['finish_m'] < bidno['pacenote_last_m'] - 150
    assert bidno['finish_runs'] >= 2 and bidno['finish_spread_m'] < 45
    elatia = stage_tables.acr_stage('Greece Elatia')
    assert 'finish_m' not in elatia                        # no run to derive it from: the last note
    drive = acr_drive(0.0, 100.0, 6730.0, track='Greece Elatia')
    learner, reader, session = drive_runs(tmp_path, drive)
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    assert run['finished'] == 1 and 295 < run['result_time'] < 308     # (6710 - 100) / 22 m/s


def test_acr_post_finish_packets_do_not_start_a_run_and_a_restart_does(tmp_path):
    first = acr_drive(0.0, 238.0, 5530.0)
    t = first[-1][0] + 0.1
    after = acr_after_finish(t, 5530.0, seconds=20.0, speed=5.0)       # rolling out, then on past the grace
    t = after[-1][0] + 0.1
    after += acr_after_finish(t, after[-1][1].lap_distance, seconds=30.0, speed=22.0)
    second = acr_drive(after[-1][0] + 0.1, 238.0, 1500.0)              # the stage restarted
    learner, reader, session = drive_runs(tmp_path, first + after + second)
    runs = [r for r in session['runs'] if r['distance'] > 100]
    assert [r['finished'] for r in runs] == [1, None]       # the restarted run is still open at the session's end
    assert 225 < runs[0]['result_time'] < 235
    assert 4950 < runs[0]['distance'] < 5150               # driven to the line, not the roll-out
    assert runs[0]['ended'] < runs[1]['ended']


def test_a_circuit_lap_in_acc_does_not_end_the_run(tmp_path):
    """ACC's progress is the position around the lap: near 1 at the end of
    every lap, not a finish."""
    from oversteer.telemetry import Sample
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    samples, t = [], 0.0
    for lap in range(3):
        for i in range(400):
            s = Sample(6000.0, 7500.0, gear=3, speed=40.0, car='acc/test', game='acc', throttle=0.8)
            s.track, s.stage_length = 'monza', 1600.0
            s.progress = i / 399.0
            s.lap_distance = s.progress * 1600.0
            samples.append((t, s, 0.8))
            t += 0.1
    feed_course(learner, samples[:420])                  # past the end of the first lap
    run = learner.runs.run
    assert run is not None
    feed_course(learner, samples[420:])
    assert learner.runs.run == run                       # 80 s on, the later laps are the same run


def test_a_circuit_lap_is_not_a_finish_and_its_wrapping_distance_is_not_the_runs(tmp_path):
    """AC and ACC send the position around the lap: progress near 1 and the lap distance back to 0 at the line,
    every lap. The first lap's end is not the finish of the run, and the trace's distance goes on."""
    from oversteer.telemetry import Sample
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    samples, t = [], 0.0
    for lap in range(3):
        for i in range(400):
            s = Sample(6000.0, 7500.0, gear=3, speed=40.0, car='acc/test', game='acc', throttle=0.8)
            s.track, s.stage_length = 'monza', 1600.0
            s.progress = i / 399.0
            s.lap_distance = s.progress * 1600.0
            samples.append((t, s, 0.8))
            t += 0.1
    feed_course(learner, samples[:420])                  # past the end of the first lap
    assert learner.runs.run is not None and learner.runs._finished is None
    feed_course(learner, samples[420:])
    learner.save()
    reader = learner._reader()
    history = reader.history('_no_profile', 'acc/test')
    [run] = [r for r in reader.session(history[0]['id'])['runs'] if r['distance'] > 300]
    assert run['finished'] != 1
    distance = [row[1] for row in reader.trace(run['id'])]
    assert all(b >= a for a, b in zip(distance, distance[1:])) and distance[-1] > 4000.0
