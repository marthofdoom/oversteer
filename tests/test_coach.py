"""Coaching: the metrics each run measures, and what the coach makes of
their history (docs/telemetry-coaching.md, section 9)."""

from oversteer import coach
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry_store import TRACE_CHANNELS
from tests.sim import Course, STAGE, course_samples, feed_course

NAN = float('nan')
C = {name: i for i, name in enumerate(TRACE_CHANNELS)}


def shift(gear, rpm, best=7000.0, method='h-pattern', direction='up', to=None, flags=None, neutral=0.1,
          band=None, engage=None, slip=None, flat_out=1):
    return {'gear': gear, 'gear_to': to if to is not None else gear + (1 if direction == 'up' else -1),
            'direction': direction, 'rpm': rpm, 'best': best, 'best_low': band[0] if band else None,
            'best_high': band[1] if band else None, 'throttle': 1.0, 'method': method, 'neutral_time': neutral,
            'engage_rpm': engage, 'flat_out': flat_out, 'slip': slip, 'flags': flags}


def by_name(metrics, name, **match):
    return [m for m in metrics if m['name'] == name and all(m.get(k) == v for k, v in match.items())]


def test_shift_metrics():
    shifts = [shift(2, 6500), shift(2, 6600, band=(6900, 7100)), shift(2, 7050, band=(6900, 7100)),
              shift(3, 7000, method='paddles', flags='double-tap'), shift(3, 7100, method='paddles'),
              shift(2, 6800, method='h-pattern', flags='missed', neutral=0.8),
              shift(3, 7000, direction='down', engage=7400, flags='over-rev'),
              shift(4, 5000, direction='down', engage=5000),
              shift(2, 5000, flat_out=0)]                       # a short shift at part throttle: not coached
    metrics = coach.shift_metrics(shifts)
    [error] = by_name(metrics, 'shift.error', gear=2, method='h-pattern')
    assert error['count'] == 4 and error['value'] == (6500 + 6600 + 7050 + 6800) / 4 - 7000
    [in_band] = by_name(metrics, 'shift.in_band', gear=2, method='h-pattern')
    assert in_band['value'] == 0.5                              # 6800 and 7050: within the band +- 100 or 200
    [tap] = by_name(metrics, 'seq.double_tap', method='paddles')
    assert tap['value'] == 50.0 and tap['count'] == 2
    [missed] = by_name(metrics, 'hpattern.missed')
    assert missed['count'] == 5 and missed['value'] == 20.0
    [over] = by_name(metrics, 'downshift.over_rev', method='h-pattern')
    assert over['value'] == 50.0 and over['count'] == 2


def trace(rows):
    """Trace rows from dicts of channels (NaN where not given)."""
    return [tuple(float(r.get(name, NAN)) for name in TRACE_CHANNELS) for r in rows]


def test_trace_metrics():
    rows = []
    t = d = 0.0
    for i in range(1200):                                       # two minutes at 10 Hz, 2.4 km
        v = min(20.0, 3.0 + i * 0.5)
        on_limiter = 300 <= i < 330                             # 3 s on the limiter in 3rd
        in_top = 600 <= i < 650                                 # 5 s in 5th (top), 2 of them on the limiter
        rows.append({'t': t, 'distance': d, 'speed': v, 'rpm': 7450.0 if on_limiter or 630 <= i < 650 else 6000.0,
                     'gear': 5.0 if in_top else 3.0, 'throttle': 1.0 if on_limiter or in_top else 0.3,
                     'brake': 0.5 if 900 <= i < 910 else 0.0, 'handbrake': 1.0 if i in (100, 101, 400) else 0.0,
                     'slip_drive': 0.2 if i < 5 else 0.0})
        t += 0.1
        d += v * 0.1
    summary = {'distance': d, 'launch': 1.2, 'launch_rpm': 5500.0, 'release': 0.4, 'gears': 5}
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0})
    [per_km] = by_name(metrics, 'limiter.per_km')
    assert abs(per_km['value'] - 3.0 / (d / 1000)) < 0.1 and per_km['count'] == round(d / 1000)
    assert abs(by_name(metrics, 'limiter.top')[0]['value'] - 2.0) < 0.15
    assert abs(by_name(metrics, 'top.share')[0]['value'] - 50 / 1200) < 0.01
    assert by_name(metrics, 'top.peak')[0]['value'] == 7450.0 / 7500.0
    t50 = by_name(metrics, 'launch.t50')[0]['value']
    assert abs(t50 - (0.4 + 2.2)) < 0.11                          # 13.9 m/s at the 22nd row
    assert by_name(metrics, 'launch.slip')[0]['value'] == 0.2
    assert by_name(metrics, 'launch.bog')[0]['value'] == 0.0 and by_name(metrics, 'launch.stall')[0]['value'] == 0.0
    assert abs(by_name(metrics, 'handbrake.per_km')[0]['value'] - 2 / (d / 1000)) < 0.01
    assert abs(by_name(metrics, 'pedal.overlap')[0]['value'] - 10 / 1200) < 1e-6     # braking at 30 % throttle
    # No brake sent: no pedal metrics rather than a wrong zero
    for r in rows:
        r.pop('brake')
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0})
    assert not by_name(metrics, 'pedal.overlap') and not by_name(metrics, 'pedal.coast')


def test_balance_from_steering_against_grip():
    """steer = a x curvature + K x lateral g: K > 0 is understeer."""
    for k in (0.05, -0.03):
        rows = []
        for i in range(400):
            v = 10.0 + (i % 40)                                   # speeds 10..49 m/s
            curvature = 0.004 + 0.0001 * (i % 7)
            yaw, lateral = v * curvature, v * v * curvature
            rows.append({'t': i * 0.1, 'speed': v, 'yaw_rate': yaw, 'a_lat': lateral,
                         'steer': 8.0 * curvature + k * lateral / 9.80665})
        found, n = coach.balance_gradient(trace(rows), C)
        assert abs(found - k) < 1e-6 and n > 200                   # the slowest rows turn too gently to count


def drive_stages(tmp_path, tops, game='eawrc', car='eawrc/17'):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    learner.clock = lambda now: 1e9 + now
    t = 0.0
    for top in tops:
        t = feed_course(learner, course_samples(Course(STAGE), game=game, car=car, packets=True, top=top, t=t + 60))
    learner.save()
    return learner, learner._reader()


def test_runs_write_their_metrics(tmp_path):
    """Each run's metrics carry its discipline and surface; consistency
    needs three finished runs of the stage, corner time a best run to
    compare with."""
    learner, reader = drive_stages(tmp_path, (30.0, 30.0, 26.0))
    runs = reader.db.execute('SELECT id FROM runs ORDER BY id').fetchall()
    assert len(runs) == 3
    names = [{m['name'] for m in reader.run_metrics(r)} for (r,) in runs]
    assert 'consistency.split_sd' not in names[1] and 'consistency.split_sd' in names[2]
    assert 'corner.loss' not in names[0] and 'corner.loss' in names[1]
    rows = reader.metrics('_no_profile')
    assert all(m['discipline'] == 'rally-stage' and m['surface'] == 'unknown' for m in rows)
    [loss] = [m for m in rows if m['name'] == 'corner.loss' and m['run'] == runs[2][0]]
    assert loss['value'] > 0.5                                  # the slower run lost time in its corners
    [sd] = [m for m in rows if m['name'] == 'consistency.split_sd']
    assert sd['value'] > 0.1
    series = reader.metric_series('_no_profile', 'corner.loss', discipline='rally-stage')
    assert [m['run'] for m in series] == [runs[2][0], runs[1][0]]
    learner.close()
