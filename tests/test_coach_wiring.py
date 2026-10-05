"""The context layer wired into the drive log, the model and the detectors
(docs/coach-techniques.md, build step 1): what a run writes, the backfill of
runs from before, the limiter figure, the drivetrain, the ACR discipline."""
from oversteer import coach, coach_context, drive_detect, telemetry_store
from oversteer.shift_learner import CarModel, ShiftLearner
from oversteer.telemetry import Sample
from oversteer.telemetry_store import TRACE_CHANNELS
from tests.sim import Course, STAGE, course_samples, feed_course
from tests.test_shift_learner import FABIA


# -- the limiter figure --

def test_a_launch_within_two_percent_of_the_games_limiter_does_not_replace_it():
    car = CarModel(FABIA)                                  # the game's files say 7500
    assert car.known_limiter() == 7500.0
    assert car.set_limiter(7577.0, 'launch') is False      # the launch overshoots the cut
    assert car.known_limiter() == 7500.0 and car.limiter_source is None
    assert car.set_limiter(7000.0, 'launch') is True       # a real difference is the car as it is now
    assert car.known_limiter() == 7000.0
    # a figure stored by an older version: the launch's 7577 is the overshoot too
    old = CarModel(FABIA)
    old.limiter, old.limiter_source = 7577.0, 'launch'
    assert old.known_limiter() == 7500.0
    old.limiter = 7000.0
    assert old.known_limiter() == 7000.0


def test_a_launch_beats_a_games_limiter_it_is_far_from():
    """WRC Generations over-reports its maximum: the launch figure, 7 % under, stands."""
    car = CarModel('wrcg/7900-800-6')
    car.set_limiter(7900.0, 'game')
    assert car.set_limiter(7940.0, 'launch') is False      # within 2 %: the game's
    assert car.set_limiter(7460.0, 'launch') is True
    assert car.known_limiter() == 7460.0


# -- the drivetrain --

def test_the_shipped_drivetrain_beats_the_vote(tmp_path):
    learner = ShiftLearner(str(tmp_path / 't.db'))
    sample = Sample(4000.0, 7500.0, gear=3, speed=20.0, car=FABIA, game='acr')
    sample.drivetrain = 'fwd'                              # what a vote or a bridge says
    learner.feed(1.0, sample, 7500.0, 0.3, 0.0)
    assert learner.car.drivetrain == 'awd'                 # the Fabia is all-wheel drive in the game's files
    other = Sample(4000.0, 7500.0, gear=3, speed=20.0, car='forza/9', game='forza-fh')
    other.drivetrain = 'rwd'
    learner.feed(2.0, other, 7500.0, 0.3, 0.0)
    assert learner.car.drivetrain == 'rwd'                 # no shipped data: the game's word
    learner.close()


def test_the_coach_context_names_the_cars_drivetrain_lights_and_top_gear():
    car = CarModel(FABIA)
    context = coach.car_context(car, 'gravel')
    assert context['drivetrain'] == 'awd' and context['lights'] == {'prepare': 6250.0, 'shift': 6900.0, 'late': 7100.0,
                                                                    'over_limit': 7300.0}
    assert context['shipped_top'] == 5 and context['turbo'] is True and context['limiter'] == 7500.0
    assert context['ratio'](3) > context['ratio'](4)       # rpm per m/s from the game's gearing and the tyre
    assert coach.car_context(None) == {}


# -- discipline from the stage table --

def test_an_acr_stage_in_the_table_is_a_rally_stage_and_livigno_a_circuit():
    summary = {'game': 'acr', 'stage': 'acr:wales:afon-bidno-severn'}
    verdict = drive_detect.classify_discipline(summary, [], TRACE_CHANNELS)
    assert verdict.value == 'rally-stage' and verdict.confidence == 'game'
    verdict = drive_detect.classify_discipline(dict(summary, stage='acr:livigno-circuit:main-circuit'), [],
                                               TRACE_CHANNELS)
    assert verdict.value == 'circuit' and verdict.confidence == 'game'
    assert drive_detect.table_discipline('acr:livigno-circuit:main-circuit-reverse') == 'circuit'
    assert drive_detect.table_discipline('dirt:4241:100') is None and drive_detect.table_discipline(None) is None
    assert drive_detect.table_discipline('acr:nowhere:none') is None
    # an unmatched ACR run is judged by its shape as before
    assert drive_detect.classify_discipline({'game': 'acr', 'stage': None}, [], TRACE_CHANNELS).confidence != 'game'


def test_the_start_repairs_runs_whose_discipline_the_stage_table_knows(tmp_path):
    from oversteer.drive_log import backfill_step
    learner, reader = drive_stages(tmp_path, (30.0, 30.0))
    store = learner.log.store
    store.upsert_stage('acr:livigno-circuit:main-circuit', 'acr')
    store.upsert_stage('acr:wales:afon-bidno-severn', 'acr')
    runs = [r[0] for r in reader.db.execute('SELECT id FROM runs ORDER BY id')]
    for run, stage, found, conf in ((runs[0], 'acr:livigno-circuit:main-circuit', 'rally-stage', 'low'),
                                    (runs[1], 'acr:wales:afon-bidno-severn', 'unknown', None)):
        store.update_run(run, stage=stage, discipline=found, discipline_conf=conf)
    store.set_stage_prior('acr:livigno-circuit:main-circuit', 'discipline', 'rally-stage', 'learnt')
    store.commit()
    backfill_step(learner, 1)
    rows = reader.db.execute('SELECT discipline, discipline_conf FROM runs ORDER BY id').fetchall()
    assert rows == [('circuit', 'game'), ('rally-stage', 'game')]
    assert store.stage('acr:livigno-circuit:main-circuit')['discipline_prior'] == 'circuit'
    session = reader.db.execute('SELECT discipline FROM sessions').fetchall()
    assert session and session[0][0] in ('circuit', 'rally-stage')
    learner.close()


# -- what a run writes --

def drive_stages(tmp_path, tops, clutch=None):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    learner.clock = lambda now: 1e9 + now
    t = 0.0
    for top in tops:
        samples = course_samples(Course(STAGE), game='eawrc', car='eawrc/17', packets=True, top=top, t=t + 60)
        if clutch is not None:
            for _, sample, _ in samples:
                if sample.speed < 1.0:
                    sample.clutch = clutch                 # the pedal while standing
        t = feed_course(learner, samples)
    learner.save()
    return learner, learner._reader()


def test_the_launch_is_the_games_unless_the_clutch_was_pressed(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0,))
    [run] = [r[0] for r in reader.db.execute('SELECT id FROM runs')]
    [launch] = reader.events(run, 'launch')
    assert launch['class'] == 'game' and launch['gear'] == 1                    # the clutch never rose
    names = {m['name'] for m in reader.run_metrics(run)}
    assert 'launch.t50' in names and 'launch.g' in names
    assert 'launch.bog' not in names and 'launch.stall' not in names            # the game's launch neither bogs nor stalls
    learner.close()
    other = tmp_path / 'pressed'
    other.mkdir()
    learner, reader = drive_stages(other, (30.0,), clutch=0.9)
    [run] = [r[0] for r in reader.db.execute('SELECT id FROM runs')]
    assert reader.events(run, 'launch')[0]['class'] == 'driver'
    assert 'launch.bog' in {m['name'] for m in reader.run_metrics(run)}
    learner.close()


def test_a_run_stores_its_class_course_corners_events_and_shifts(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0, 30.0))
    rows = reader.db.execute('SELECT id, run_class, course, finished FROM runs ORDER BY id').fetchall()
    assert [r[1] for r in rows] == ['learning', 'clean'] and all(r[3] == 1 for r in rows)
    run = rows[1][0]
    corners = reader.corners(run)
    assert corners and [k['complex'] for k in corners] == sorted(k['complex'] for k in corners)
    assert all(k['d0'] <= k['d'] <= k['d1'] for k in corners) and all(k['tightness'] for k in corners)
    assert all(k['off'] == 0 for k in corners)
    lead = [k for k in corners if k['section_t'] is not None]
    assert lead and len(lead) == len({k['complex'] for k in corners})
    assert all(k['loss_entry'] is not None for k in lead)                       # against the first run
    shifts = reader.db.execute('SELECT gear, flags, d FROM shifts WHERE run = ? ORDER BY at', (run,)).fetchall()
    assert shifts and all(s[2] is not None for s in shifts)                      # where each change was
    assert 'launch' in (shifts[0][1] or '')                                      # the first change up of the launch
    assert not any('launch' in (s[1] or '') for s in shifts[1:])
    # the held limiter of the session is kept for the live line
    assert learner.session_held_time >= 0.0
    learner.close()


# -- the backfill --

def make_old(learner, reader):
    """The state version 2 left: no class, no phases, no events, the old
    metrics; the launch metrics written live."""
    db = learner.log.store.db
    db.execute('UPDATE runs SET run_class = NULL, course = NULL')
    db.execute('UPDATE corners SET d0 = NULL, d1 = NULL, complex = NULL, tightness = NULL, radius = NULL, '
               'brake_d = NULL, section_t = NULL, loss_entry = NULL, loss_exit = NULL, off = NULL')
    db.execute('DELETE FROM events')
    db.execute("DELETE FROM metrics WHERE name NOT LIKE 'launch.%'")
    db.execute("INSERT INTO metrics (session, run, name, value, count) SELECT session, id, 'limiter.per_km', 1.0, 3 "
               'FROM runs')
    db.commit()


def test_the_backfill_works_old_runs_over_a_few_at_a_time(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0, 30.0, 26.0))
    before = {r[0]: r[1] for r in reader.db.execute("SELECT run, value FROM metrics WHERE name = 'launch.t50'")}
    make_old(learner, reader)
    store = learner.log.store
    assert len(store.runs_to_backfill(10)) == 3 and learner.BACKFILL_BATCH == 3
    from oversteer.drive_log import backfill_step
    assert backfill_step(learner, 2) == 2                      # a few runs per batch
    assert len(store.runs_to_backfill(10)) == 1
    assert learner.backfill() == 1 and learner.backfill() == 0
    assert store.runs_to_backfill(10) == []
    rows = reader.db.execute('SELECT id, run_class, course, discipline FROM runs ORDER BY id').fetchall()
    assert [r[1] for r in rows] == ['learning', 'clean', 'clean'] and all(r[2] for r in rows)
    ids = [r[0] for r in rows]
    names = {m['name'] for m in reader.run_metrics(ids[2])}
    assert 'limiter.per_km' not in names and 'limiter.held' in names and 'corner.loss' in names
    assert 'launch.t50' in names                              # the launch is not in the trace: kept as it was
    after = {r[0]: r[1] for r in reader.db.execute("SELECT run, value FROM metrics WHERE name = 'launch.t50'")}
    assert after == before
    corners = reader.corners(ids[2])
    assert corners and all(k['d0'] is not None for k in corners) and any(k['loss_entry'] is not None for k in corners)
    learner.close()


def test_a_run_the_backfill_cannot_read_keeps_what_it_had_and_is_tried_again_next_start(tmp_path, monkeypatch):
    learner, reader = drive_stages(tmp_path, (30.0,))
    make_old(learner, reader)
    store = learner.log.store
    count = 'SELECT (SELECT COUNT(*) FROM corners), (SELECT COUNT(*) FROM metrics)'
    had = reader.db.execute(count).fetchone()
    assert had[0] and had[1]

    def broken(*args, **kwargs):
        raise ValueError('a trace from another age')
    monkeypatch.setattr(coach_context, 'analyse', broken)
    assert learner.backfill() == 1 and learner.backfill() == 0         # not tried over and over
    [(klass,)] = reader.db.execute('SELECT run_class FROM runs').fetchall()
    assert klass is None and len(store.runs_to_backfill()) == 1         # as it was, and left to retry
    assert reader.db.execute(count).fetchone() == had                    # the rewrite was rolled back
    assert reader.db.execute("SELECT COUNT(*) FROM metrics WHERE name = 'limiter.per_km'").fetchone()[0] == 1
    # the next start (a learner without the failure) works it over
    monkeypatch.undo()
    del learner._backfill_failed
    assert learner.backfill() == 1 and not store.runs_to_backfill()
    learner.close()


def test_the_backfill_waits_for_a_quiet_moment_on_the_drive_log_thread(tmp_path):
    learner, reader = drive_stages(tmp_path, (30.0, 30.0))
    make_old(learner, reader)
    learner.close()
    threaded = ShiftLearner(str(tmp_path / 'telemetry.db'), threaded=True)
    try:
        threaded.runs.run = 7                                  # a run is on: no batch is queued
        threaded._backfill_tick()
        assert threaded.log.sync() and not threaded._backfilled
        assert len(threaded.log.store.runs_to_backfill(10)) == 2
        threaded.runs.run = None
        threaded._last_feed = __import__('time').monotonic()   # a packet a moment ago
        threaded._backfill_tick()
        assert len(threaded.log.store.runs_to_backfill(10)) == 2
        threaded._last_feed = None
        threaded._backfill_tick()                              # now: one batch
        assert threaded.log.sync() and threaded.log.store.runs_to_backfill(10) == []
        threaded._backfill_tick()
        threaded.log.sync()
        threaded._backfill_tick()
        assert threaded._backfilled                            # and it stops asking once nothing is left
    finally:
        threaded.close()


def test_an_old_database_is_upgraded_and_backfilled_when_the_app_opens_it(tmp_path):
    """Version 2 on disk; the learner opens it, and the runs get their class."""
    learner, reader = drive_stages(tmp_path, (30.0,))
    path = learner.log.path
    learner.close()
    import sqlite3
    db = sqlite3.connect(path)
    db.execute('UPDATE runs SET run_class = NULL')
    db.execute('PRAGMA user_version = 3')
    db.commit()
    db.close()
    again = ShiftLearner(path)
    assert again.log.store.db.execute('PRAGMA user_version').fetchone()[0] == telemetry_store.VERSION
    assert again.backfill() == 1
    assert again.log.store.db.execute('SELECT run_class FROM runs').fetchone()[0] == 'learning'
    again.close()


def test_a_shifts_band_is_stored_with_its_flags(tmp_path):
    store = telemetry_store.open_store(str(tmp_path / 't.db'))
    car = store.car_id('p', 'acr/a', 'acr')
    session = store.start_session('p', car, 'acr', 1.0)
    run = store.start_run(session, 1, 1.0)
    shift_id = store.add_shift(session, run, {'at': 2.0, 'gear': 2, 'gear_to': 3, 'direction': 'up', 'rpm': 7480.0,
                                              'best': 7500.0, 'best_low': 7000.0, 'best_high': 7500.0,
                                              'flat_out': 1, 'method': 'sequential'})
    store.update_shift(shift_id, 'cut', 612.5)                             # no band: the live one stays
    [row] = store.run_shifts(run)
    assert (row['flags'], row['d'], row['best_low'], row['best_high'], row['best']) == ('cut', 612.5, 7000.0, 7500.0,
                                                                                         7500.0)
    store.update_shift(shift_id, 'cut', 612.5, (6900.0, 7100.0, 7450.0))   # the band the lights give
    [row] = store.run_shifts(run)
    assert (row['best_low'], row['best_high'], row['best']) == (6900.0, 7100.0, 7450.0)
    store.close()
