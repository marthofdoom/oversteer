"""The Forza Horizon add-on against a slice of a real FH6 drive (tests/data/fh6-replay.json.gz: decoded fields only,
made by scripts/ffb-addon-replay.py --fixture: 15 s either side of the strongest front understeer and of the longest
jump, and a resume from a pause). The samples go through the 324-byte decoder as the engine gets them; the add-on's
gain never adds force, stays within its bounds, does not chatter, and the engine cuts at once when the game's raw
level goes stale. Nothing here touches a real wheel or /sys."""
import gzip
import json
import os
import random

import pytest

from oversteer import ffb_addon as fa
from tests.test_ffb_addon import Drive, engine_on, fh6, last_nonzero_after, never_lowered, RecordingDriver  # noqa: F401
from tests.test_ffb_addon import _clean_writes  # noqa: F401 (the autouse fixture)
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)
from oversteer.telemetry_formats import decode_sample

FIXTURE = os.path.join(os.path.dirname(__file__), 'data', 'fh6-replay.json.gz')
FLOOR = 1.0 - fa.STRENGTH * (1.0 - fa.G_MIN)


def samples():
    with gzip.open(FIXTURE, 'rt') as f:
        rows = json.load(f)['rows']
    out = []
    for r in rows:
        if len(r) == 1:
            out.append((r[0], decode_sample(bytes(324))))
        else:
            t, speed, *rest = r
            out.append((t, fh6(speed=speed, slip_angle=tuple(rest[0:4]), slip_ratio=tuple(rest[4:8]),
                               lat=rest[8], steer=rest[9])))
    return out


def replay(strength=fa.STRENGTH, raw=lambda t, s: 10000.0, passes_per_sample=1):
    """(t, sample, g_eff, lightness(sample)) per sample: feed() then gain() as the engine does."""
    addon = fa.ForzaLightness(strength)
    rows, last = [], None
    for t, s in samples():
        addon.feed(t, s, raw(t, s) if s.car is not None else None)
        for i in range(passes_per_sample):
            g = addon.gain(t + i * 0.001)
        rows.append((t, s, g, fa.lightness(s) if s.car is not None and s.speed > fa.MIN_SPEED else None))
    return rows


def test_the_fixture_has_what_it_is_for():
    rows = replay()
    assert 2000 < len(rows) < 6000 and os.path.getsize(FIXTURE) < 200 * 1024
    assert any(s.car is None for _t, s, _g, _l in rows)                    # a pause
    assert any(l is not None and l < 0.5 for _t, _s, _g, l in rows)       # the understeer


def test_the_gain_stays_within_its_bounds_and_follows_the_understeer():
    rows = replay()
    for _t, _s, g, lightness in rows:
        assert FLOOR - 1e-9 <= g <= 1.0
        assert lightness is None or fa.G_MIN <= lightness <= 1.0
    assert min(g for _t, _s, g, _l in rows) < 0.8                            # the understeer is felt
    assert all(g == 1.0 for _t, s, g, _l in rows[:1])                        # and it starts untouched


@pytest.mark.parametrize('strength', [0.0, 0.3, 1.0])
def test_strength_bounds_the_gain(strength):
    gs = [g for _t, _s, g, _l in replay(strength)]
    assert min(gs) >= 1.0 - strength * (1.0 - fa.G_MIN) - 1e-9 and max(gs) <= 1.0
    if strength == 0.0:
        assert set(gs) == {1.0}


def test_the_gain_never_moves_faster_than_the_slew():
    rows = replay()
    for (t0, _s0, g0, _l0), (t1, _s1, g1, _l1) in zip(rows, rows[1:]):
        dt = min(max(t1 - t0, 0.0), 0.1)
        assert abs(g1 - g0) <= fa.SLEW * dt + 1e-9


def test_no_chatter_beyond_a_bound():
    """Direction reversals of g (moves over 0.004) per second of the replay: the real drive's slip is busy, the
    gain's low-pass and slew keep it under what a hand can feel as a buzz (measured on this fixture: about 3/s)."""
    rows = replay()
    reversals, last = 0, 0
    for (_t0, _s0, g0, _l0), (_t1, _s1, g1, _l1) in zip(rows, rows[1:]):
        d = g1 - g0
        if abs(d) > 0.004:
            sign = 1 if d > 0 else -1
            reversals += bool(last and sign != last)
            last = sign
    assert reversals / (rows[-1][0] - rows[0][0]) < 6.0


def test_a_pause_lets_the_gain_go_back_to_1():
    """Menu and pause packets name no car: the add-on asks for no lightening, and after a pause of over 0.3 s the
    gain is back at 1 when the car returns (it recovers at the slew's rate)."""
    rows = replay()
    resumes = [i for i in range(1, len(rows)) if rows[i - 1][1].car is None and rows[i][1].car is not None]
    assert resumes
    checked = 0
    for i in resumes:
        j = i
        while j > 0 and rows[j - 1][1].car is None:
            j -= 1
        if rows[i][0] - rows[j][0] > 0.3 and rows[j - 1][2] is not None:
            assert rows[i - 1][2] >= 0.99, "g at the end of the pause"
            checked += 1
    assert checked


def test_the_force_never_adds_and_is_never_more_than_the_games():
    rng = random.Random(7)
    margins = (0.0, 200.0, 3000.0)
    for _t, _s, g, _l in replay()[::7]:
        raw = rng.choice((-1, 1)) * rng.uniform(0, 30000)
        for margin in margins:
            f = fa.ForzaLightness.force(raw, g, margin)
            assert f == 0 or (f > 0) != (raw > 0)                            # against the game's level
            assert abs(f) <= abs(raw)                                        # and never more than it
            assert abs(raw + f) >= min(abs(raw), margin) - 1                 # a next level within margin keeps its sign
    assert fa.ForzaLightness.force(10000, 1.0) == 0


def test_a_non_finite_sample_is_no_lightness():
    s = fh6(speed=25.0, slip_angle=(0.5, 0.5, 0, 0), slip_ratio=(0, 0, 0, 0))
    s.slip_angle = (float('nan'), 0.0, 0.0, 0.0)
    assert fa.lightness(s) is None
    s.slip_angle = (float('inf'), 0.0, 0.0, 0.0)
    assert fa.lightness(s) is None


def test_the_engine_cuts_at_once_when_raw_goes_stale_during_the_understeer(tmp_path):
    """The fixture's understeer through the engine with the game's force playing (ext_force against raw); then the
    uploads stop with the level held: ext_force is 0 within three upload intervals plus 20 ms."""
    engine, path = engine_on(tmp_path)

    class Replay(Drive):
        def __init__(self, engine, stream):
            super().__init__(engine)
            self.stream, self.i = stream, 0

        def sample(self, t):
            i = min(self.i, len(self.stream) - 1)
            self.i += 1
            return self.stream[i][1]

        def alpha(self, t):                                                  # the Drive's own force
            return 0.0

    stream = [(t, s) for t, s in samples()[:1500] if s.car is not None]
    drive = Replay(engine, stream)
    drive.run(8.0)
    assert engine.status.active
    drive.uploads = False
    stop = drive.t
    last = last_nonzero_after(engine, drive, 0.3)
    assert last is None or last - stop <= 3.0 / 60.0 + 0.020
    assert not engine.status.active and engine._phase == 'idle' and never_lowered(path)
    assert RecordingDriver.writes[-1][0] == 0
