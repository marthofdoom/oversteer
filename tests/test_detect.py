"""Discipline, surface and wet: honest unknowns, the game's own word, the
shape of a run, and the surface calibration from labelled runs."""
import random

from oversteer import drive_detect
from oversteer.drive_detect import (classify_discipline, classify_surface, classify_wet, calibrate,
                                    classify_segment)
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry_store import TRACE_CHANNELS, open_store
from tests.sim import Course, STAGE, CIRCUIT, course_samples, feed_course


def summary(**fields):
    base = {'game': 'forza-fh', 'profile': '_no_profile', 'laps': 0, 'stage_length': None, 'distance': 4000.0,
            'moving_time': 160.0, 'standing': 3.0, 'launch': 3.0, 'stops': 0, 'puddles': None}
    base.update(fields)
    return base


def trace_of(samples):
    """A 10 Hz trace like the run tracker's, from course samples."""
    rows, last, driven, previous = [], None, 0.0, None
    for t, s, throttle in samples:
        if previous is not None:
            driven += s.speed * (t - previous)
        previous = t
        if s.speed < 3.0 and not rows:
            continue
        if last is None or t - last >= 0.095:
            last = t
            row = dict.fromkeys(TRACE_CHANNELS, float('nan'))
            row.update(t=t, distance=driven, speed=s.speed, x=s.pos[0], y=s.pos[1], z=s.pos[2],
                       yaw_rate=s.yaw_rate, steer=s.steer, gear=s.gear)
            rows.append(tuple(row[c] for c in TRACE_CHANNELS))
    return rows


def test_no_position_no_shape():
    verdict = classify_discipline(summary(game='beamng'), [], TRACE_CHANNELS)
    assert verdict.value == 'unknown' and verdict.confidence is None
    assert any('does not send' in m for m in verdict.missing)


def test_the_game_says_so():
    assert classify_discipline(summary(game='eawrc'), [], TRACE_CHANNELS).confidence == 'game'
    rx = classify_discipline(summary(game='dirt', laps=4, stage_length=1100.0), [], TRACE_CHANNELS)
    assert (rx.value, rx.confidence) == ('rallycross', 'game') and '4 laps of 1.1 km' in rx.evidence[0]
    stage = classify_discipline(summary(game='dirt', stage_length=9800.0, profile='circuit'), [], TRACE_CHANNELS)
    assert stage.value == 'rally-stage' and stage.evidence[-1] == "Oversteer profile 'circuit'."


def test_the_shape_of_a_run():
    stage = course_samples(Course(STAGE), game='forza-fh', car='forza-fh/77')
    verdict = classify_discipline(summary(), trace_of(stage), TRACE_CHANNELS)
    assert (verdict.value, verdict.confidence) == ('rally-stage', 'medium'), verdict
    assert any('point to point' in e for e in verdict.evidence)
    laps = course_samples(Course(CIRCUIT), laps=3, game='forza-fh', car='forza-fh/77')
    verdict = classify_discipline(summary(distance=4700.0), trace_of(laps), TRACE_CHANNELS)
    assert (verdict.value, verdict.confidence) == ('circuit', 'high'), verdict
    rolling = course_samples(Course(CIRCUIT), laps=3, rolling=True, game='forza-fh', car='forza-fh/77')
    verdict = classify_discipline(summary(distance=4700.0, standing=0.0, launch=0.0), trace_of(rolling),
                                  TRACE_CHANNELS)
    assert verdict.value == 'time-attack'
    hill = course_samples(Course(STAGE, grade=0.06), game='forza-fh', car='forza-fh/77')
    verdict = classify_discipline(summary(), trace_of(hill), TRACE_CHANNELS)
    assert (verdict.value, verdict.confidence) == ('hillclimb', 'medium'), verdict


def test_too_short_to_tell_but_the_profile_says():
    short = course_samples(Course(STAGE), game='forza-fh', car='forza-fh/77')[:2000]
    verdict = classify_discipline(summary(distance=900.0, moving_time=40.0), trace_of(short), TRACE_CHANNELS)
    assert verdict.value == 'unknown' and 'a longer run' in verdict.missing[0]
    verdict = classify_discipline(summary(distance=900.0, moving_time=40.0, profile='My Rally'), trace_of(short),
                                  TRACE_CHANNELS)
    assert (verdict.value, verdict.confidence) == ('rally-stage', 'low')


def test_surface_from_the_route_table(monkeypatch):
    monkeypatch.setattr(drive_detect, 'EAWRC_LOCATIONS', {4: 'Rally Sweden', 1: 'Rally Monte-Carlo'})
    game = summary(game='eawrc')
    sweden = classify_surface(game, [], location=drive_detect.location_name('eawrc', 'eawrc:4:12'))
    assert (sweden.value, sweden.confidence) == ('snow', 'game')
    monte = classify_surface(game, [], location=drive_detect.location_name('eawrc', 'eawrc:1:3'))
    assert monte.value == 'unknown' and monte.prior == 'mixed:tarmac,snow'       # a prior, not an answer
    nothing = classify_surface(summary(game='dirt'), [])
    assert nothing.value == 'unknown' and 'not calibrated for DiRT Rally' in nothing.missing[0]


def test_wet_only_where_the_game_says():
    assert classify_wet(summary(puddles=0.05)).value == 'wet'
    assert classify_wet(summary(puddles=0.0)).value == 'unknown'
    assert 'does not send' in classify_wet(summary(game='dirt')).missing[0]


SURFACE_LOOK = {'gravel': (0.55, 0.20, 0.030, 0.2), 'tarmac': (0.95, 0.02, 0.005, 0.9),
                'snow': (0.35, 0.35, 0.015, 0.5)}


def segments(surface, n, rng):
    mu, spin, rough, corr = SURFACE_LOOK[surface]
    return [{'mu_p95': rng.gauss(mu, 0.05), 'spin': rng.gauss(spin, 0.03), 'rough': rng.gauss(rough, 0.003),
             'lr_corr': rng.gauss(corr, 0.08), 'post_peak': None, 'rumble': None} for _ in range(n)]


def test_calibration_round_trip():
    rng = random.Random(3)
    labelled = [(i, s, segments(s, 12, rng)) for i, s in enumerate(['gravel', 'tarmac'] * 2)]
    model, holdout, deployed, reason = calibrate(labelled)
    assert model is None and not deployed and '2 gravel, 2 tarmac' in reason
    labelled = [(i, s, segments(s, 12, rng)) for i, s in enumerate(['gravel', 'tarmac', 'snow'] * 3)]
    model, holdout, deployed, reason = calibrate(labelled)
    assert deployed and holdout >= 0.9 and model['features'] == ['mu_p95', 'spin', 'rough', 'lr_corr']
    assert classify_segment(model, segments('gravel', 1, rng)[0])[0] == 'gravel'
    alien = {'mu_p95': 2.5, 'spin': 0.9, 'rough': 0.2, 'lr_corr': -0.9}
    assert classify_segment(model, alien)[0] == 'unknown'                    # like nothing calibrated
    run = [{'pushed': 1, 'features': f} for f in segments('tarmac', 12, rng)]
    verdict = classify_surface(summary(game='dirt'), run, {'deployed': True, 'model': model, 'holdout': holdout})
    assert (verdict.value, verdict.confidence) == ('tarmac', 'high'), verdict
    cruising = [{'pushed': 0, 'features': f} for f in segments('tarmac', 12, rng)]
    verdict = classify_surface(summary(game='dirt'), cruising, {'deployed': True, 'model': model})
    assert verdict.value == 'unknown' and 'never pushed enough' in verdict.evidence[-1]


def test_calibrate_from_the_store(tmp_path):
    rng = random.Random(4)
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'dirt/1', 'dirt')
    session = store.start_session('p', car, 'dirt', 1.0)
    for i, surface in enumerate(['gravel', 'tarmac'] * 3):
        run = store.start_run(session, i + 1, float(i))
        for n, features in enumerate(segments(surface, 10, rng)):
            store.add_segment(run, n * 200.0, n * 200.0 + 200.0, 0, 1, features, pushed=1)
        store.set_label(run, surface=surface)
    version, holdout, deployed, _ = drive_detect.calibrate_game(store, 'dirt')
    assert version == 1 and deployed and store.calibration('dirt', 'surface')['deployed']


def test_runs_are_judged_and_stages_learn_priors(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'), profile='rally')
    t = 0.0
    for _ in range(3):
        t = feed_course(learner, course_samples(Course(STAGE), game='forza-fh', car='forza-fh/77', t=t + 200.0))
    learner.save()
    reader = learner._reader()
    sessions = reader.history('rally', 'forza-fh/77')
    runs = [reader.runs(s['id'])[0] for s in sessions]
    assert {(r['discipline'], r['discipline_conf']) for r in runs} == {('rally-stage', 'medium')}
    assert "Oversteer profile 'rally'." in runs[0]['discipline_evidence']
    assert runs[0]['surface'] == 'unknown' and runs[0]['wet'] == 'unknown'
    assert len({r['stage'] for r in runs}) == 1 and runs[0]['stage'].startswith('cell:forza-fh:')
    stage = reader.stage(runs[0]['stage'])
    assert (stage['discipline_prior'], stage['discipline_prior_source'], stage['runs']) == ('rally-stage', 'learnt', 3)
    assert reader.session(sessions[0]['id'])['discipline'] == 'rally-stage'
