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
