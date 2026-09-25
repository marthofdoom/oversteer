"""The drive log: writes leave the listener for their own thread, and what
the thread writes is what an inline replay writes."""
import sqlite3
import threading
import time

from oversteer import drive_log
from oversteer.drive_log import DriveLog
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
        'wrcg', {'location': 'Rally México', 'stage': 'Media Luna', 'length_m': 4010.0, 'surface': 'gravel'},
        {'location': 'Rally México', 'stage': 'Media Luna reverse', 'length_m': 3980.0, 'surface': 'gravel'}))
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert store.match_distance('wrcg', 3979.6)[0] is None                         # 1 %: both fit
    assert store.match_distance('wrcg', 3979.6, within=15.0)[0] == 'wrcg:rally-mexico:media-luna-reverse'
