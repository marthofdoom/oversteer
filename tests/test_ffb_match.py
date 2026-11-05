import importlib.util
import os

SPEC = importlib.util.spec_from_file_location('ffb_match', os.path.join(os.path.dirname(__file__), '..', 'scripts', 'ffb-match.py'))
match = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(match)


def test_only_situations_both_sides_have_enough_of_are_compared_biggest_gap_first():
    enough = int(1.0 / match.STEP)
    a = {(1, 0, 0, 0): [0.5] * enough, (2, 0, 0, 0): [0.3] * enough, (3, 0, 0, 0): [0.9] * 5, (4, 0, 0, 0): [0.2] * enough}
    b = {(1, 0, 0, 0): [0.4] * enough, (2, 0, 0, 0): [0.6] * enough, (3, 0, 0, 0): [0.1] * enough}
    rows = match.compare(a, b, 1.0)
    assert [r[0] for r in rows] == [(2, 0, 0, 0), (1, 0, 0, 0)]
    assert abs(rows[0][1] - 0.3) < 1e-9 and abs(rows[0][5] - 0.6) < 1e-9
