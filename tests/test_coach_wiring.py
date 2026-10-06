"""The context layer wired into the drive log, the model and the detectors
(docs/coach-techniques.md, build step 1): what a run writes, the backfill of
runs from before, the limiter figure, the drivetrain, the ACR discipline."""
from oversteer import coach, coach_context, drive_detect, stage_tables, telemetry_store
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


def test_the_start_gives_a_car_the_shipped_drivetrain_over_a_learnt_one(tmp_path):
    from oversteer.drive_log import repair_shipped
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    fabia = CarModel(FABIA)
    fabia.drivetrain = 'fwd'
    store.save_model('p', FABIA, 'acr', 'Fabia', fabia.to_dict(), None, 'fwd')
    other = CarModel('forza/9')
    other.drivetrain = 'rwd'
    store.save_model('p', 'forza/9', 'forza-fh', 'Other', other.to_dict(), None, 'rwd')
    assert repair_shipped(store) == 1
    assert store.car('p', FABIA)['drivetrain'] == 'awd' and store.car('p', FABIA)['model']['drivetrain'] == 'awd'
    assert store.car('p', 'forza/9')['drivetrain'] == 'rwd'            # no shipped value: the learnt one stays
    assert repair_shipped(store) == 0
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


# -- ACR runs timed to the old finish line --

def _timed_run(store, stage, result=230.0, course=5278.0, finished=1, end_speed=0.0):
    """A finished run with a trace of 10 m/s steps every second, 1 m/s^2 of pace: t = d / 10."""
    car = store.car_id('p', FABIA, 'acr', 'Fabia')
    session = store.start_session('p', car, 'acr', 1.0, stage=stage)
    run = store.start_run(session, 1, 1.0, stage)
    store.update_run(run, finished=finished, result_time=result, course=course, run_class='clean', ended=2.0)
    rows = [tuple(i / 10.0 if name == 't' else float(i) if name == 'distance' else
                  end_speed if name == 'speed' else 0.0 for name in TRACE_CHANNELS)
            for i in range(0, int(course) + 1)]
    store.add_trace(run, rows)
    return run


def test_an_acr_run_timed_to_the_old_line_is_timed_again_once_and_queued(tmp_path):
    from oversteer.drive_log import repair_shipped, retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    entry = stage_tables.entry(stage)
    gap = entry['pacenote_last_m'] - entry['finish_m']
    run = _timed_run(store, stage)
    other = _timed_run(store, 'acr:nowhere:none')
    repair_shipped(store)
    row = store.run(run)
    # the trace takes 0.1 s a metre
    assert abs(row['result_time'] - (230.0 - gap / 10.0)) < 0.01 and abs(row['course'] - (entry['finish_m'] - stage_tables.start_line(entry))) < 1e-6
    assert row['run_class'] is None and run in store.runs_to_backfill(10)
    assert store.run(other)['result_time'] == 230.0 and store.run(other)['run_class'] == 'clean'
    store.update_run(run, run_class='clean')
    assert retime_finishes(store) == 0                   # once
    assert store.run(run)['result_time'] == row['result_time'] and store.run(run)['run_class'] == 'clean'
    learner.close()


def test_a_run_that_crept_past_the_stop_control_is_timed_from_the_line_not_from_where_it_stopped(tmp_path):
    """The course is where the car finally stopped, seconds after the stop control: the shift is anchored on the
    old line's driven distance (its road position less where the run began), not on the course."""
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    entry = stage_tables.entry(stage)
    start = stage_tables.start_line(entry)
    d_old, d_new = entry['pacenote_last_m'] - start, entry['finish_m'] - start
    run = _timed_run(store, stage, result=230.0, course=5278.0)
    store.update_run(run, course=5600.0)
    rows = [tuple(i / 10.0 if name == 't' else float(i) if name == 'distance' else 0.0 for name in TRACE_CHANNELS)
            for i in range(0, 5273)]
    rows += [tuple(527.2 + (i - 5272) if name == 't' else float(i) if name == 'distance' else 0.0
                   for name in TRACE_CHANNELS) for i in range(5273, 5601)]
    store.db.execute('DELETE FROM traces WHERE run = ?', (run,))
    store.add_trace(run, rows)
    assert retime_finishes(store) == 1
    row = store.run(run)
    assert abs(row['result_time'] - (230.0 - (d_old - d_new) / 10.0)) < 0.1
    assert abs(row['course'] - d_new) < 1e-6
    learner.close()


def test_a_run_is_moved_when_the_finish_line_is_refined_since_it_was_timed(tmp_path):
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    flying = stage_tables.entry(stage)['finish_m']
    run = _timed_run(store, stage, result=200.0, course=5200.0, end_speed=40.0)
    store.set_run_finish(run, flying + 50.0)               # timed at a line 50 m on from the table's now
    assert retime_finishes(store) == 1
    row = store.run(run)
    assert abs(row['result_time'] - 195.0) < 0.01 and abs(row['course'] - (flying - stage_tables.start_line(stage_tables.entry(stage)))) < 1e-6   # 0.1 s a metre
    assert row['run_class'] is None and retime_finishes(store) == 0       # once, and queued for the backfill
    far = _timed_run(store, stage, result=200.0, course=5000.0, end_speed=40.0)
    store.set_run_finish(far, flying - 100.0)              # the line is on from the trace's end: left as it was
    assert retime_finishes(store) == 0 and store.run(far)['result_time'] == 200.0
    learner.close()


def test_a_run_on_the_games_clock_is_never_re_timed(tmp_path):
    """The game stopped its clock at its own line: where the tables put theirs does not move the time."""
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    flying = stage_tables.entry(stage)['finish_m']
    moved = _timed_run(store, stage, result=200.0, course=5000.0, end_speed=40.0)
    store.set_run_finish(moved, flying + 50.0)
    store.set_run_clock(moved, 'game')
    untimed = _timed_run(store, stage)                      # no run_finish row: it would be re-timed to the flying finish
    store.set_run_clock(untimed, 'game')
    assert retime_finishes(store) == 0
    assert store.run(moved)['result_time'] == 200.0 and store.run(untimed)['result_time'] == 230.0
    assert store.finished_untimed('acr') == [] and store.finished_timed('acr') == []
    learner.close()


# -- finish lines learnt from the game's own clock --

PETIT_BALLON = 'acr:alsace:col-du-petit-ballon'          # no flying finish in the shipped table: the stop control at 5984.6


def _clock_run(store, stage, stop_m, result=200.0, course=5500.0):
    run = _timed_run(store, stage, result=result, course=course, end_speed=40.0)
    store.set_run_clock(run, 'game')
    store.set_run_stop(run, stop_m)
    return run


def test_a_finish_is_learnt_from_where_the_games_clock_stopped(tmp_path):
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    assert 'finish_m' not in stage_tables.entry(PETIT_BALLON)
    for stop in (5739.0, 5741.0, 5740.0, 5900.0):          # one far from the others: an outlier
        _clock_run(store, PETIT_BALLON, stop)
    learnt = store.learn_finishes()
    found = learnt[PETIT_BALLON]
    assert abs(found['finish_m'] - 5740.0) < 1e-6 and found['finish_runs'] == 3 and found['finish_spread_m'] <= 2.0
    entry = stage_tables.entry(PETIT_BALLON)
    assert abs(entry['finish_m'] - 5740.0) < 1e-6 and entry['finish_runs'] == 3
    assert stage_tables.road_length(entry) == 5740.0 - stage_tables.start_line(entry)
    learner.close()
    again = ShiftLearner(str(tmp_path / 't.db'))                  # learnt in the file: known on the next start
    stage_tables.set_learnt({})
    again.log.store.learn_finishes()
    assert abs(stage_tables.entry(PETIT_BALLON)['finish_m'] - 5740.0) < 1e-6
    again.close()


def test_a_learnt_finish_beats_the_shipped_one_and_the_shipped_one_the_last_note(tmp_path):
    afon = 'acr:wales:afon-bidno-severn'
    shipped = stage_tables.entry(afon)['finish_m']
    assert stage_tables.acr_stage('Wales Afon Bidno', start=238.0)['finish_m'] == shipped
    stage_tables.set_learnt({afon: {'finish_m': shipped + 12.0, 'finish_runs': 2, 'finish_spread_m': 1.0}})
    assert stage_tables.entry(afon)['finish_m'] == shipped + 12.0
    assert stage_tables.acr_stage('Wales Afon Bidno', start=238.0)['finish_m'] == shipped + 12.0
    assert stage_tables.entry(PETIT_BALLON).get('finish_m') is None
    assert stage_tables.entry(PETIT_BALLON)['pacenote_last_m'] == 5984.6
    stage_tables.set_learnt({})
    assert stage_tables.entry(afon)['finish_m'] == shipped


def test_runs_timed_to_the_stop_control_move_to_the_learnt_finish(tmp_path):
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    old = _timed_run(store, PETIT_BALLON, result=230.0, course=5800.0, end_speed=0.0)   # ran to the stop control
    assert retime_finishes(store) == 0 and store.run(old)['result_time'] == 230.0       # no finish known
    _clock_run(store, PETIT_BALLON, 5740.0)
    store.learn_finishes()
    note = stage_tables.entry(PETIT_BALLON)['pacenote_last_m']
    start = stage_tables.start_line(stage_tables.entry(PETIT_BALLON))
    assert retime_finishes(store) == 1
    row = store.run(old)
    assert abs(row['result_time'] - (230.0 - ((note - start) - (5740.0 - start)) / 10.0)) < 0.5   # 0.1 s a metre
    assert abs(row['course'] - (5740.0 - start)) < 1e-6
    learner.close()


def test_a_trace_that_ends_a_row_short_of_the_old_line_is_still_re_timed(tmp_path):
    """A 10 Hz trace stops a row before the run's finish (ACR's old line: 3 m at 30 m/s): the time at the old line is
    taken a little past the last row, not 'out of range'."""
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    start = stage_tables.start_line(stage_tables.entry(PETIT_BALLON))
    d_old = stage_tables.entry(PETIT_BALLON)['pacenote_last_m'] - start
    run = _timed_run(store, PETIT_BALLON, result=230.0, course=d_old, end_speed=10.0)
    rows = [r for r in store.trace(run) if r[1] <= d_old - 3.0]                 # the last row 3 m short of the line
    store.db.execute('DELETE FROM traces WHERE run = ?', (run,))
    store.add_trace(run, rows)
    stage_tables.set_learnt({PETIT_BALLON: {'finish_m': 5740.0, 'finish_runs': 2, 'finish_spread_m': 1.0}})
    assert retime_finishes(store) == 1
    row = store.run(run)
    assert abs(row['result_time'] - (230.0 - (d_old - (5740.0 - start)) / 10.0)) < 0.35 and row['run_class'] is None
    assert store.finish_unknown(run) is False
    learner.close()


# -- the start offset: a game-clock run starts timing at the game's clock, an older one at 3 m/s --

def test_once_a_stage_and_car_have_a_clock_run_only_clock_runs_rank(tmp_path):
    """A run on the game's clock starts its time 0.3-0.9 s before the run's own clock does (the go against the
    car reaching 3 m/s), so the two are never compared: from the first clock run on the PB, the coach's reference and
    the live reference are among the clock runs of that stage and car. The run list still shows every run."""
    from oversteer import run_analysis
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    old = _timed_run(store, stage, result=200.0, course=5000.0, end_speed=40.0)         # quicker, but the older clock
    store.set_run_finish(old, stage_tables.entry(stage)['finish_m'])
    assert {r['id'] for r in store.stage_runs(stage, ranked=True)} == {old}              # no clock run yet: all rank
    new = _timed_run(store, stage, result=200.8, course=5000.0, end_speed=40.0)
    store.set_run_clock(new, 'game')
    third = _timed_run(store, stage, result=205.0, course=5000.0, end_speed=40.0)
    store.set_run_clock(third, 'game')
    car = store.run(new)['car']
    ranked = store.stage_runs(stage, car=car, ranked=True)
    assert {r['id'] for r in ranked} == {new, third} and all(r['clock'] == 'game' for r in ranked)
    assert {r['id'] for r in store.stage_runs(stage, car=car)} == {old, new, third}        # unranked: every run
    assert run_analysis.pb_run(store, store.run(third))['id'] == new                      # not the old, quicker run
    shown = run_analysis.recent_runs(store, car)
    assert {r['id'] for r in shown} == {old, new, third} and [r['id'] for r in shown if r['pb']] == [new]
    learner.close()


def test_a_run_with_no_trace_to_re_time_is_left_and_not_tried_again(tmp_path):
    from oversteer.drive_log import repair_shipped, retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    run = _timed_run(store, 'acr:wales:afon-bidno-severn', course=100.0)      # shorter than the gap
    assert retime_finishes(store) == 0
    # kept as it was, but its stop-control time is not ranked beside flying-finish times
    assert store.run(run)['result_time'] == 230.0 and store.run(run)['run_class'] == 'partial'
    assert store.finish_unknown(run)
    assert 'clean' not in {r['run_class'] for r in store.stage_runs('acr:wales:afon-bidno-severn') if r['id'] == run}
    assert store.finished_untimed('acr') == []
    learner.close()


def test_a_run_that_ends_at_speed_was_timed_at_the_flying_finish_already(tmp_path):
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    run = _timed_run(store, 'acr:wales:afon-bidno-severn', result=196.0, course=5056.0, end_speed=40.0)
    assert retime_finishes(store) == 0                   # a build without the marker timed it at the line
    assert store.run(run)['result_time'] == 196.0 and store.run(run)['run_class'] == 'clean'
    assert store.finished_untimed('acr') == []
    learner.close()


# -- DBs recorded between the game-clock commit and the run_stop commit: a clock run with no recorded stop --

def test_game_clock_runs_with_no_recorded_stop_teach_the_finish_and_are_re_classed(tmp_path):
    from oversteer.drive_log import repair_shipped
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    entry = stage_tables.entry(PETIT_BALLON)
    start = stage_tables.start_line(entry)
    stage_tables.set_learnt({})
    runs = []
    for stop in (5739.0, 5741.0, 5740.0):
        run = _timed_run(store, PETIT_BALLON, result=200.0, course=stop - start, end_speed=40.0)
        store.set_run_clock(run, 'game')
        store.set_run_start(run, start)
        runs.append(run)
    far = _timed_run(store, PETIT_BALLON, result=200.0, course=entry['pacenote_last_m'] + 500.0 - start, end_speed=40.0)
    store.set_run_clock(far, 'game')
    store.set_run_start(far, start)
    repair_shipped(store)
    stops = dict(store.db.execute('SELECT run, stop_m FROM run_stop').fetchall())
    assert set(stops) == set(runs) and abs(stops[runs[2]] - 5740.0) < 1e-6      # the gate leaves the far one out
    assert abs(stage_tables.entry(PETIT_BALLON)['finish_m'] - 5740.0) < 1e-6
    assert all(store.run(r)['run_class'] is None for r in runs + [far])          # queued for the backfill
    learner.close()
    stage_tables.set_learnt({})


# -- a v2 database: the course is NULL after the migration --

def test_a_run_with_no_course_is_re_timed_from_the_traces_last_distance(tmp_path):
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    entry = stage_tables.entry(stage)
    gap = entry['pacenote_last_m'] - entry['finish_m']
    run = _timed_run(store, stage)
    store.update_run(run, course=None, run_class=None)
    assert retime_finishes(store) == 1
    row = store.run(run)
    assert abs(row['result_time'] - (230.0 - gap / 10.0)) < 0.01 and row['run_class'] is None
    assert not store.finish_unknown(run)
    learner.close()


def test_a_run_that_cannot_be_re_timed_keeps_a_class_that_is_still_to_be_worked_out(tmp_path):
    from oversteer.drive_log import retime_finishes
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    run = _timed_run(store, 'acr:wales:afon-bidno-severn', course=100.0)
    store.update_run(run, run_class=None)
    assert retime_finishes(store) == 0
    assert store.finish_unknown(run) and store.run(run)['run_class'] is None        # _work_over says 'partial'
    learner.close()


def test_runs_a_v2_database_marked_unknown_for_want_of_a_course_are_re_timed(tmp_path):
    from oversteer.drive_log import repair_shipped
    learner = ShiftLearner(str(tmp_path / 't.db'))
    store = learner.log.store
    stage = 'acr:wales:afon-bidno-severn'
    run = _timed_run(store, stage)
    store.update_run(run, course=None, run_class='partial')
    store.set_run_finish(run, None)                                  # what the old retime_finishes wrote
    other = _timed_run(store, stage, course=100.0)                   # a real failure: course known
    store.set_run_finish(other, None)
    store.update_run(other, run_class='partial')
    repair_shipped(store)
    assert store.run(run)['result_time'] < 230.0 and store.run(run)['run_class'] is None
    assert store.finish_unknown(other) and store.run(other)['run_class'] == 'partial'
    learner.close()
