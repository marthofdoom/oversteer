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


# -- the coach over time --

from oversteer.coach import Coach, DAY, seen          # noqa: E402
from oversteer.telemetry_store import open_store      # noqa: E402

T0 = 1.7e9


class History:
    """A telemetry database filled with sessions of chosen metrics, one run
    of 10 km each, a day apart."""

    def __init__(self, path, profile='rally'):
        self.store = open_store(str(path))
        self.profile = profile
        self.car = self.store.car_id(profile, 'eawrc/17', 'eawrc', 'Test car', {})
        self.t = T0

    def session(self, metrics, discipline='rally-stage', surface='tarmac', stage='eawrc:4:12', distance=10000.0,
                days=1.0):
        store = self.store
        self.t += days * DAY
        store.begin()
        session = store.start_session(self.profile, self.car, 'eawrc', self.t, stage=stage)
        run = store.start_run(session, 1, self.t, stage)
        store.end_run(run, ended=self.t + 600, distance=distance, finished=1, result_time=600.0)
        store.add_metrics(session, run, [dict(m, discipline=discipline, surface=surface) for m in metrics])
        store.commit()
        return session

    def coach(self, days=0.0):
        return Coach(self.store, now=self.t + days * DAY)

    def tips(self, days=0.0, **kwargs):
        return self.coach(days).tips(self.profile, self.car, **kwargs)

    def show(self, tips, days=0.0):
        """What the GTK tab does after showing them."""
        self.store.begin()
        seen(self.store, self.profile, self.car, tips, at=self.t + days * DAY)
        self.store.commit()


def error(gear, value, count=8, method='h-pattern'):
    return {'name': 'shift.error', 'value': value, 'count': count, 'gear': gear, 'method': method}


def test_a_late_change_every_session_is_the_focus(tmp_path):
    h = History(tmp_path / 't.db')
    for _ in range(6):
        h.session([error(2, 450.0), error(3, 20.0), {'name': 'limiter.per_km', 'value': 0.1, 'count': 10}])
    tips = h.tips()
    assert [t.kind for t in tips] == ['focus']
    assert tips[0].id == 'shift.late:2:h-pattern:rally-stage:tarmac'
    assert tips[0].text == '2→3 with the H-pattern: you change up about 450 rpm late; change a little sooner.'
    assert tips[0].evidence == ['8 changes up flat out in 1 session (rally stage, tarmac).',
                                'More than 200 rpm late in 6 of your last 6 sessions.']


def test_changing_up_early_depends_on_the_surface(tmp_path):
    """Short-shifting can be right on a loose surface: silent on gravel,
    a note once when the surface is unknown, a tip on tarmac or when the
    wheels were seen not to spin."""
    for surface, slip, expected in (('gravel', None, []), ('unknown', None, ['note']), ('tarmac', None, ['tip']),
                                    ('unknown', 0.02, ['tip'])):
        h = History(tmp_path / '{}-{}.db'.format(surface, slip))
        metrics = [error(2, -500.0)]
        if slip is not None:
            metrics.append({'name': 'shift.slip', 'value': slip, 'count': 8, 'gear': 2, 'method': 'h-pattern'})
        h.session(metrics, surface=surface)
        tips = h.tips()
        assert [t.kind for t in tips] == expected, surface
        if expected == ['note']:
            assert tips[0].id == 'gate.surface' and 'wait until the surface is known' in tips[0].text
            h.show(tips)
            assert h.tips() == []                                   # said once


def test_progress_is_praised_with_its_numbers(tmp_path):
    h = History(tmp_path / 't.db')
    for _ in range(3):
        h.session([error(2, -500.0)])
    for _ in range(3):
        h.session([error(2, -60.0)])
    [praise] = [t for t in h.tips() if t.kind == 'praise']
    assert praise.text == ('Better: 2→3 with the H-pattern is 60 rpm from the best over your last 3 sessions; '
                           '3 days ago you were 500 early.')


def test_growth_needs_twenty_events_a_side(tmp_path):
    h = History(tmp_path / 't.db')
    h.session([error(2, -500.0, count=10)])
    for _ in range(3):
        h.session([error(2, -60.0)])
    assert not [t for t in h.tips() if t.kind == 'praise']


def test_tips_go_quiet_and_come_back(tmp_path):
    """Shown twice without change, a tip becomes a quiet "still:" line; it
    comes back when it gets 20 % worse or after two weeks. At most three
    tips at a time, the costliest first."""
    h = History(tmp_path / 't.db')
    rates = [{'name': 'hpattern.missed', 'value': 10.0, 'count': 40, 'method': 'h-pattern'},
             {'name': 'hpattern.skip', 'value': 8.0, 'count': 40, 'method': 'h-pattern'},
             {'name': 'downshift.over_rev', 'value': 20.0, 'count': 40, 'method': 'h-pattern'},
             {'name': 'limiter.per_km', 'value': 2.0, 'count': 10}]
    h.session(rates)
    tips = h.tips()
    assert [t.kind for t in tips] == ['tip', 'tip', 'tip']
    assert [t.id for t in tips] == ['limiter', 'hpattern.missed:h-pattern', 'hpattern.skip:h-pattern']
    h.show(tips)
    h.show(h.tips())
    tips = h.tips()
    assert [t.id for t in tips] == ['downshift.over_rev:h-pattern', 'limiter', 'hpattern.missed:h-pattern',
                                    'hpattern.skip:h-pattern']
    assert [t.kind for t in tips] == ['tip', 'still', 'still', 'still']
    assert tips[1].text.startswith('Still: 2.0 s per km on the limiter')
    h.session([dict(rates[3], value=2.5)])                          # worse: back as a tip
    assert [t.id for t in h.tips() if t.kind == 'tip'][0] == 'limiter'
    assert 'limiter' in [t.id for t in h.tips(days=15) if t.kind == 'tip']
    assert len([t for t in h.tips(show_all=True) if t.kind == 'tip']) == 4


def test_the_way_of_changing_is_compared_with_itself(tmp_path):
    h = History(tmp_path / 't.db')
    h.session([error(2, -400.0, count=12, method='h-pattern'), error(2, 0.0, count=12, method='paddles')])
    [tip] = [t for t in h.tips() if t.id.startswith('shift.method')]
    assert tip.text == 'With the H-pattern you change up 400 rpm earlier than with the paddles.'


def test_both_pedals_only_matter_on_tarmac_when_the_corners_are_slower(tmp_path):
    for surface, loss, expected in (('gravel', 2.0, False), ('tarmac', 0.0, False), ('tarmac', 2.0, True)):
        h = History(tmp_path / '{}-{}.db'.format(surface, loss))
        h.session([{'name': 'pedal.overlap', 'value': 0.12, 'count': 10},
                   {'name': 'corner.loss', 'value': loss, 'count': 1}], surface=surface)
        found = [t for t in h.tips() if t.id.startswith('pedal.overlap')]
        assert bool(found) == expected, (surface, loss)


def test_stage_tips_compare_with_the_same_stage(tmp_path):
    h = History(tmp_path / 't.db')
    for value in (2.0, 2.2, 1.9):
        h.session([{'name': 'pedal.coast', 'value': value, 'count': 10}])
    h.session([{'name': 'pedal.coast', 'value': 4.0, 'count': 10}], stage='eawrc:5:3')   # another stage
    assert not [t for t in h.tips() if t.id.startswith('pedal.coast')]
    h.session([{'name': 'pedal.coast', 'value': 3.5, 'count': 10}])
    [tip] = [t for t in h.tips() if t.id.startswith('pedal.coast')]
    assert tip.text == ('On stage eawrc:4:12 you coasted 3.5 s per km, 1.6 more than your best run there: stay on '
                        'one pedal or the other.')


def test_the_coach_uses_the_cars_model(tmp_path):
    """With the car's learnt model, a tip names the rpm and what the next
    gear gives there."""
    from tests.sim import drive, exits, analytic_shift
    learner = ShiftLearner(str(tmp_path / 't.db'), profile='rally')
    exits(learner)
    drive(learner, shift_at=6000, runs=3)
    learner.close()
    h = History(tmp_path / 't.db')
    h.car = h.store.car_id('rally', 'test-car', 'unknown')
    h.session([error(2, -800.0)])
    [tip] = h.tips()
    best = learner.car.best_shift(2)[0]
    assert abs(best - analytic_shift(2)) < 150
    assert tip.text.startswith('2→3 with the H-pattern: you change up at {:.0f} rpm, 800 early. Gear 3 gives '.format(
        best - 800))
    assert tip.text.endswith('% less drive there. Hold it to about {:.0f}.'.format(best))
