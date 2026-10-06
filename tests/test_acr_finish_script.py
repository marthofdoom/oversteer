"""scripts/acr-finish.py: a run with fewer captures does not erase the finish lines an earlier one found."""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def script():
    spec = importlib.util.spec_from_file_location('acr_finish', os.path.join(HERE, '..', 'scripts', 'acr-finish.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table():
    return {'stages': [
        {'location': 'Wales', 'stage': 'Afon Bidno - Severn', 'track': 'Wales Afon Bidno', 'pacenote_last_m': 5510.0,
         'finish_m': 5287.4, 'finish_runs': 10, 'finish_spread_m': 35.0, 'finish_confidence': 'medium'},
        {'location': 'Greece', 'stage': 'Elatia', 'track': 'Greece Elatia', 'pacenote_last_m': 6710.3},
        {'location': 'Wales', 'stage': 'Elatia', 'track': 'Wales Elatia', 'pacenote_last_m': 3000.0}]}


def test_stages_without_new_evidence_keep_their_finish(capsys):
    acr_finish = script()
    t = table()
    # two runs of the Greek Elatia only; the same stage name in Wales has none
    acr_finish.apply_finish(t, {'acr:greece:elatia': [-200.0, -210.0]})
    bidno, greece, wales = t['stages']
    assert bidno['finish_m'] == 5287.4 and bidno['finish_runs'] == 10             # no evidence: kept
    assert greece['finish_m'] == round(6710.3 - 205.0, 1) and 'finish_m' not in wales   # keyed by stage, not name


def test_reset_recomputes_everything_from_the_captures(capsys):
    acr_finish = script()
    t = table()
    acr_finish.apply_finish(t, {'acr:greece:elatia': [-200.0, -210.0]}, reset=True)
    assert 'finish_m' not in t['stages'][0] and 'finish_runs' not in t['stages'][0]
    assert t['stages'][1]['finish_m'] == round(6710.3 - 205.0, 1)


def test_a_clock_stop_gives_the_finish_exactly_and_beats_the_brake_onset(capsys):
    acr_finish = script()
    t = table()
    # brake onsets say -200 and -210 (a driver braking early); the game's own clock stopped at -150 in both
    acr_finish.apply_finish(t, {'acr:greece:elatia': [-200.0, -210.0]}, exact={'acr:greece:elatia': [-150.0, -151.0]})
    greece = t['stages'][1]
    assert greece['finish_m'] == round(6710.3 - 150.5, 1) and greece['finish_runs'] == 2
    assert greece['finish_confidence'] == 'high' and 'clock' in greece['finish_source']
    # a single clock run is enough (exact), where the brake onset needs two
    t = table()
    acr_finish.apply_finish(t, {}, exact={'acr:greece:elatia': [-150.0]})
    assert t['stages'][1]['finish_m'] == round(6710.3 - 150.0, 1) and t['stages'][1]['finish_confidence'] == 'medium'


def test_the_clock_stop_of_a_v4_run():
    acr_finish = script()
    last = 6000.0
    # (track, lap distance, speed, brake, stage length, clock, time): the clock moves to d = 5850 and stands after it
    run = [('Greece Elatia', 5000.0 + 10.0 * i, 30.0, 0.0, 6800.0, 100.0 + 0.33 * i, 0.3 * i) for i in range(86)]
    clock = run[-1][5]
    run += [('Greece Elatia', 5850.0 + 10.0 * i, 30.0, 0.0, 6800.0, clock, 0.3 * (85 + i)) for i in range(1, 15)]
    assert acr_finish.clock_stop(run, last) == 5850.0
    # a clock that stands from the start (a v3 capture, none) or far from the line is no finish
    assert acr_finish.clock_stop([r[:5] + (None, r[6]) for r in run], last) is None
    assert acr_finish.clock_stop(run, 9000.0) is None
