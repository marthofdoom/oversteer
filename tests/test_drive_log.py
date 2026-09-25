"""The drive log: writes leave the listener for their own thread, and what
the thread writes is what an inline replay writes."""
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
