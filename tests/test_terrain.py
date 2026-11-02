"""Elevation, gradient, crests and drift angle (oversteer/terrain.py) on synthetic traces: a known slope, a crest, a
height that jumps, a constant-drift circle between two straights, and the games it is not available for."""
import math

import pytest

from oversteer import terrain
from oversteer.telemetry_store import TRACE_CHANNELS

N = len(TRACE_CHANNELS)
CHI = {n: i for i, n in enumerate(TRACE_CHANNELS)}
HZ = 10.0


def row(**kw):
    r = [float('nan')] * N
    for k, v in kw.items():
        r[CHI[k]] = v
    return tuple(r)


def climb(profile, game='acr', speed=20.0, length=1000.0, jump_at=None):
    """Rows driving straight east at `speed` m/s up a road whose height is profile(d); the height axis is the
    game's (ACR y, RBR z)."""
    rows = []
    n = int(length / speed * HZ)
    for i in range(n):
        d = i * speed / HZ
        h = profile(d)
        x = 100.0 + d
        if jump_at is not None and d >= jump_at:
            x += 500.0
        pos = {'x': x, 'y': h, 'z': 10.0} if game in ('acr', 'dirt') else {'x': x, 'y': 10.0, 'z': h}
        rows.append(row(t=i / HZ, distance=d, speed=speed, yaw_rate=0.0, **pos))
    return rows


def test_a_constant_slope():
    e = terrain.elevation(climb(lambda d: 0.05 * d), 'acr')
    assert e['range'] == pytest.approx(50.0, abs=1.5)
    assert e['net'] == pytest.approx(50.0, abs=1.5)
    assert e['max_grade'] == pytest.approx(5.0, abs=0.3)
    assert all(s['grade'] == pytest.approx(5.0, abs=0.3) for s in e['sections'])
    assert e['crests'] == []


def test_a_downhill_is_negative_and_rbr_height_is_z():
    e = terrain.elevation(climb(lambda d: -0.08 * d, game='rbr'), 'rbr')
    assert e['net'] == pytest.approx(-80.0, abs=2.5)
    assert e['sections'][3]['grade'] == pytest.approx(-8.0, abs=0.4)


def test_a_crest_is_found_where_the_road_falls_away():
    # rising 6 %, over a crest at 500 m (rounded over 20 m), falling 4 %
    def profile(d):
        if d < 490:
            return 0.06 * d
        if d > 510:
            return 0.06 * 490 + 0.06 * 10 - 0.04 * (d - 510) + 0.0
        return 0.06 * 490 + 0.06 * (d - 490) - 0.5 * 0.1 * (d - 490) ** 2 / 20.0 * 0.2
    e = terrain.elevation(climb(profile), 'acr')
    assert len(e['crests']) == 1
    c = e['crests'][0]
    assert c['d'] == pytest.approx(500.0, abs=25.0)
    assert c['before'] > 4.0 and c['after'] < 0.0 and c['drop'] >= terrain.CREST_BREAK


def test_a_steady_bend_of_the_road_is_not_a_crest():
    assert terrain.elevation(climb(lambda d: 0.05 * d - 0.00002 * d * d), 'acr')['crests'] == []


def test_a_position_jump_is_rejected_and_the_profile_joins():
    e = terrain.elevation(climb(lambda d: 0.05 * d, jump_at=500.0), 'acr')
    assert e['net'] == pytest.approx(50.0, abs=4.0)         # no 500 m step in the height, no 500 m of road


def test_games_without_checked_axes_or_without_positions_give_none():
    assert terrain.elevation(climb(lambda d: 0.05 * d), 'beamng') is None
    assert terrain.elevation([row(t=0.0, distance=0.0, speed=10.0)] * 50, 'acr') is None
    assert terrain.elevation([], 'acr') is None


def circle(beta_deg, direction=1, v=15.0, radius=50.0, game='acr'):
    """A straight, a constant-radius corner on which the car is angled `beta_deg` into the corner (past its path),
    a straight; 10 Hz, ACR's axes (north = -z). The car turns in and out over a second at each end."""
    beta = math.radians(beta_deg)
    rows, corner = [], []
    x = y = 0.0
    psi = 0.0
    t = 0.0
    k = direction * v / radius
    phases = [(6.0, 0.0), (10.0, k), (6.0, 0.0)]
    s = 0.0
    for dur, rate in phases:
        for _ in range(int(dur * HZ)):
            psi += rate / HZ
            x += v * math.cos(psi) / HZ
            y += v * math.sin(psi) / HZ
            s += v / HZ
            t += 1.0 / HZ
            rows.append([t, s, psi, x, y])
        if rate:
            corner = (s - v * dur, s)
    out = []
    for i, (t, s, psi, x, y) in enumerate(rows):
        # the offset eases in and out over 10 rows
        j = i - int(6.0 * HZ)
        ramp = min(1.0, max(0.0, j / 10.0)) * min(1.0, max(0.0, (int(10.0 * HZ) - j) / 10.0))
        head_now = psi + (beta * direction) * ramp
        out.append((t, s, head_now, x, y))
    res = []
    for i, (t, s, head_now, x, y) in enumerate(out):
        yaw = (head_now - out[i - 1][2]) * HZ if i else 0.0
        res.append(row(t=t, distance=s, speed=v, yaw_rate=yaw, x=x, y=0.0, z=-y))
    # the corner without the second the car takes to turn in and to straighten
    return res, {'d': sum(corner) / 2, 'd0': corner[0] + 2 * v, 'd1': corner[1] - 2 * v, 'direction': direction}


@pytest.mark.parametrize('direction', [1, -1])
def test_a_constant_drift_circle_reads_its_angle(direction):
    rows, corner = circle(12.0, direction)
    out = terrain.drift(rows, 'acr', [corner])
    assert len(out) == 1
    assert out[0]['mean'] == pytest.approx(12.0, abs=1.0)       # positive: angled into the corner, both sides
    assert out[0]['peak'] == pytest.approx(12.0, abs=2.0)


def test_a_car_pointing_outside_reads_negative():
    rows, corner = circle(-8.0, 1)
    assert terrain.drift(rows, 'acr', [corner])[0]['mean'] == pytest.approx(-8.0, abs=1.5)


def test_no_drift_on_a_clean_circle():
    rows, corner = circle(0.0, 1)
    out = terrain.drift(rows, 'acr', [corner])[0]
    assert abs(out['mean']) < 1.0 and abs(out['peak']) < 1.5


def test_drift_is_not_available_without_a_yaw_rate_of_its_own():
    rows, corner = circle(12.0, 1)
    assert terrain.drift(rows, 'wrcg', [corner]) is None
    assert terrain.drift(rows, 'dirt', [corner]) is None
    assert terrain.drift(rows, 'acr', []) is None
