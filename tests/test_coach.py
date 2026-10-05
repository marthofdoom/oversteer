"""Coaching: the metrics each run measures, and what the coach makes of
their history (docs/telemetry-coaching.md, section 9)."""

from oversteer import coach
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry_store import TRACE_CHANNELS
from tests.sim import Course, STAGE, course_samples, feed_course

NAN = float('nan')
C = {name: i for i, name in enumerate(TRACE_CHANNELS)}


def shift(gear, rpm, klass='on', method='h-pattern', direction='up', to=None, flags=None, neutral=0.1,
          engage=None, slip=None, flat_out=1, cost=None):
    return {'gear': gear, 'gear_to': to if to is not None else gear + (1 if direction == 'up' else -1),
            'direction': direction, 'rpm': rpm, 'class': klass, 'cost': cost, 'throttle': 1.0, 'method': method,
            'neutral_time': neutral, 'engage_rpm': engage, 'flat_out': flat_out, 'slip': slip, 'flags': flags}


def by_name(metrics, name, **match):
    return [m for m in metrics if m['name'] == name and all(m.get(k) == v for k, v in match.items())]


def test_shift_metrics():
    shifts = [shift(2, 6500, 'early', cost=0.05), shift(2, 6900, 'on'), shift(2, 7050, 'on'),
              shift(2, 7450, 'cut', cost=0.2),
              shift(3, 7000, method='paddles', flags='double-tap'), shift(3, 7100, method='paddles'),
              shift(2, 6800, 'on', method='h-pattern', flags='missed', neutral=0.8),
              shift(2, 6700, 'launch'),                            # the launch's change: not judged here
              shift(3, 7000, klass=None, direction='down', engage=7400, flags='over-rev'),
              shift(4, 5000, klass=None, direction='down', engage=5000),
              shift(2, 5000, klass=None, flat_out=0)]               # a short shift at part throttle: not coached
    metrics = coach.shift_metrics(shifts)
    [in_band] = by_name(metrics, 'shift.in_band', gear=2, method='h-pattern')
    assert in_band['count'] == 5 and in_band['value'] == 3 / 5          # on the band; the launch's change is not counted
    [early] = by_name(metrics, 'shift.early_share', gear=2, method='h-pattern')
    [cut] = by_name(metrics, 'shift.cut_share', gear=2, method='h-pattern')
    assert early['value'] == 1 / 5 and cut['value'] == 1 / 5
    [cost] = by_name(metrics, 'shift.cost', gear=2, method='h-pattern')
    assert abs(cost['value'] - 0.25) < 1e-9 and cost['count'] == 1       # seconds a stage, one run
    assert not by_name(metrics, 'shift.error')
    [paddles] = by_name(metrics, 'shift.in_band', gear=3, method='paddles')
    assert paddles['value'] == 1.0 and paddles['count'] == 2
    [tap] = by_name(metrics, 'seq.double_tap', method='paddles')
    assert tap['value'] == 50.0 and tap['count'] == 2
    [missed] = by_name(metrics, 'hpattern.missed')
    assert missed['count'] == 7 and abs(missed['value'] - 100 / 7) < 1e-9
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
        on_limiter = 300 <= i < 330                             # 3 s on the limiter in 3rd, with a straight ahead
        in_top = 600 <= i < 650                                 # 5 s in 5th (top), 2 of them on the limiter
        rows.append({'t': t, 'distance': d, 'speed': v, 'rpm': 7450.0 if on_limiter or 630 <= i < 650 else 6000.0,
                     'gear': 5.0 if in_top else 3.0, 'throttle': 1.0 if on_limiter or in_top else 0.3,
                     'brake': 0.5 if 900 <= i < 910 else 0.0, 'handbrake': 1.0 if i in (100, 101, 400) else 0.0,
                     'slip_drive': 0.2 if i < 5 else 0.0, 'a_long': 4.0})
        t += 0.1
        d += v * 0.1
    summary = {'distance': d, 'launch': 1.2, 'launch_rpm': 5500.0, 'release': 0.4, 'gears': 5}
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0})
    [held] = by_name(metrics, 'limiter.held')
    assert abs(held['value'] - 3.0 / (d / 1000)) < 0.1 and held['count'] == round(d / 1000)
    assert not by_name(metrics, 'limiter.per_km')
    assert abs(by_name(metrics, 'limiter.top')[0]['value'] - 2.0) < 0.15
    assert abs(by_name(metrics, 'top.share')[0]['value'] - 50 / 1200) < 0.01
    assert by_name(metrics, 'top.peak')[0]['value'] == 7450.0 / 7500.0
    t50 = by_name(metrics, 'launch.t50')[0]['value']
    assert abs(t50 - (0.4 + 2.2)) < 0.11                          # 13.9 m/s at the 22nd row
    assert by_name(metrics, 'launch.slip')[0]['value'] == 0.2
    assert abs(by_name(metrics, 'launch.g')[0]['value'] - 4.0 / 9.80665) < 1e-6
    assert by_name(metrics, 'launch.bog')[0]['value'] == 0.0 and by_name(metrics, 'launch.stall')[0]['value'] == 0.0
    assert abs(by_name(metrics, 'handbrake.per_km')[0]['value'] - 2 / (d / 1000)) < 0.01
    # Braking at 30 % throttle on a straight is drag, not the overlap of a corner's entry
    assert abs(by_name(metrics, 'pedal.drag')[0]['value'] - 1.0 / (d / 1000)) < 1e-6
    assert not by_name(metrics, 'pedal.overlap_entry') and not by_name(metrics, 'pedal.overlap')
    assert abs(by_name(metrics, 'pedal.coast_straight')[0]['value']) < 1e-9
    # No brake sent: no pedal metrics rather than a wrong zero
    for r in rows:
        r.pop('brake')
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0})
    assert not by_name(metrics, 'pedal.drag') and not by_name(metrics, 'pedal.coast_straight')


def test_the_limiter_between_corners_is_not_a_fault_but_a_held_straight_is():
    """The owner's complaint: on gravel being on the limiter between corners is common. The corner 60 m on
    makes the same seconds a held-corner, which counts for nothing; with nothing ahead they are the held
    straight the coach talks about."""
    def run(corner_gap):
        rows = []
        for i in range(600):
            on_limiter = 100 <= i < 130
            rows.append({'t': i * 0.1, 'distance': i * 2.0, 'speed': 20.0, 'rpm': 7450.0 if on_limiter else 6000.0,
                         'gear': 3.0, 'throttle': 1.0 if on_limiter or i > 400 else 0.3, 'brake': 0.0, 'yaw_rate': 0.0})
        corners = []
        if corner_gap is not None:
            d0 = 129 * 2.0 + corner_gap
            corners = [{'d': d0 + 20, 'd0': d0, 'd1': d0 + 40, 'min_speed': 12.0, 'direction': 1}]
        summary = {'distance': 1200.0, 'gears': 5}
        return coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, corners, {'limiter': 7500.0})
    [near] = by_name(run(60.0), 'limiter.held')
    assert near['value'] == 0.0                                  # 3 s on the cut, a corner 60 m on: the gearing
    [far] = by_name(run(None), 'limiter.held')
    assert abs(far['value'] - 3.0 / 1.2) < 0.1                   # 3 s on the cut on a straight


def test_limiter_time_without_the_games_gear_count():
    """Forza, OutGauge and the AC bridge send no gear count: time on the
    limiter in the highest gear learnt is not "below top gear", and the
    top gear's own metrics are not measured."""
    rows, t, d = [], 0.0, 0.0
    for i in range(1200):
        on_limiter = 300 <= i < 330 or 600 <= i < 650
        rows.append({'t': t, 'distance': d, 'speed': 20.0, 'rpm': 7450.0 if on_limiter else 6000.0,
                     'gear': 5.0 if i >= 600 else 3.0, 'throttle': 1.0})
        t += 0.1
        d += 2.0
    summary = {'distance': d}
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0, 'top_gear': 5})
    [held] = by_name(metrics, 'limiter.held')
    assert abs(held['value'] - 3.0 / 2.4) < 0.1                  # 3rd only, not the 5 s in 5th
    assert not by_name(metrics, 'limiter.top') and not by_name(metrics, 'top.share')
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0})
    assert not by_name(metrics, 'limiter.held')                  # which gear is top: unknown
    # The shipped gear set gives the count the game does not send
    metrics = coach.trace_metrics(summary, trace(rows), TRACE_CHANNELS, [], {'limiter': 7500.0, 'shipped_top': 5})
    assert by_name(metrics, 'limiter.top') and by_name(metrics, 'top.share')


def test_an_early_shifter_is_not_on_the_limiter():
    """The highest rpm reached is not the limiter: pulls to 6500 of 8000
    are no time on it."""
    from oversteer.shift_learner import CarModel
    car = CarModel('forza-fm/1')
    car.set_limiter(8000.0, 'game')
    car.top_seen = 6500.0
    car.ratios = {g: [100.0 * g] * 20 for g in range(1, 6)}
    context = coach.car_context(car)
    assert context['limiter'] == 8000.0 and context['top_gear'] == 5 and context['shipped_top'] is None
    rows = [{'t': i * 0.1, 'distance': i * 2.0, 'speed': 20.0, 'rpm': 6500.0 if i % 20 < 5 else 5000.0,
             'gear': 3.0, 'throttle': 1.0} for i in range(1000)]
    [held] = by_name(coach.trace_metrics({'distance': 2000.0}, trace(rows), TRACE_CHANNELS, [], context),
                     'limiter.held')
    assert held['value'] == 0.0


def test_exits_bog_only_when_the_car_is_slow_and_in_a_gear_that_would_not_spin():
    """Revs under the power band a second after the throttle goes back on are a bog only when the car pulls
    less than the in-band exits at the same speed do; a slide or a gear change is not read; spin is
    measured from the revs."""
    corners, rows = [], []
    t = d = 0.0
    for n in range(10):
        low = n < 5                                             # five exits in 3rd below the band, five in band
        rows += [{'t': t + 0.1 * k, 'distance': d + 2.0 * k, 'speed': 15.0, 'rpm': 4500.0 if low else 5800.0,
                  'gear': 3.0, 'throttle': 0.0 if k < 5 else 1.0, 'yaw_rate': 0.0,
                  'a_long': (2.0 if n in (0, 1, 2) else 3.2) if low else 3.2} for k in range(40)]
        corners.append({'d': d + 10.0, 'd0': d, 'd1': d + 12.0, 'min_speed': 12.0, 'direction': 1})
        t += 4.0
        d += 80.0
    c = {name: i for i, name in enumerate(TRACE_CHANNELS)}
    exits = coach.corner_exits(trace(rows), c, corners, band_low=5000.0)
    bogs = [e['bog'] for e in exits]
    assert bogs == [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]      # low and slow; low but pulling well; in band
    # nothing to compare a low exit with: not judged
    alone = coach.corner_exits(trace(rows[:40]), c, corners[:1], band_low=5000.0)
    assert alone[0]['bog'] is None
    # a slide (yaw) in that second is no verdict on the gear
    slid = [dict(r, yaw_rate=0.6) if i < 40 else r for i, r in enumerate(rows)]
    assert len(coach.corner_exits(trace(slid), c, corners, band_low=5000.0)) == 9
    # spin: slip_rpm over 0.15 in that second
    slip = [0.3 if i < 40 else 0.0 for i in range(len(rows))]
    spun = coach.corner_exits(trace(rows), c, corners, band_low=5000.0, slip=slip)
    assert spun[0]['spun'] == 1.0 and spun[1]['spun'] == 0.0


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
    """Each run's metrics carry its discipline and surface; the first run of
    a car on a stage is a learning run (the reference for the next, but
    not a clean one for consistency, which needs three finished clean runs
    of the car), corner time a reference run to compare with."""
    learner, reader = drive_stages(tmp_path, (30.0, 30.0, 26.0, 30.0))
    runs = reader.db.execute('SELECT id, run_class, course FROM runs ORDER BY id').fetchall()
    assert [r[1] for r in runs] == ['learning', 'clean', 'clean', 'clean']
    assert all(r[2] and r[2] > 1000 for r in runs)                 # the finish cut is stored
    ids = [r[0] for r in runs]
    names = [{m['name'] for m in reader.run_metrics(r)} for r in ids]
    assert 'consistency.split_sd' not in names[2] and 'consistency.split_sd' in names[3]
    assert 'corner.loss' not in names[0] and 'corner.loss' in names[1]
    rows = reader.metrics('_no_profile')
    assert all(m['discipline'] == 'rally-stage' and m['surface'] == 'unknown' for m in rows)
    [loss] = [m for m in rows if m['name'] == 'corner.loss' and m['run'] == ids[2]]
    assert loss['value'] > 0.5                                  # the slower run lost time in its sections
    [sd] = [m for m in rows if m['name'] == 'consistency.split_sd']
    assert sd['value'] > 0.1
    series = reader.metric_series('_no_profile', 'corner.loss', discipline='rally-stage')
    assert [m['run'] for m in series] == [ids[3], ids[2], ids[1]]
    # the corners carry their windows, sections and what the run lost in them
    corners = reader.corners(ids[2])
    assert corners and all(k['d0'] is not None and k['complex'] is not None for k in corners)
    assert any(k['loss_entry'] is not None for k in corners)
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
                days=1.0, tune=None):
        store = self.store
        self.t += days * DAY
        store.begin()
        session = store.start_session(self.profile, self.car, 'eawrc', self.t, stage=stage)
        if tune is not None:
            store.update_session(session, tune=tune)
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


def shares(gear, early=0.0, cut=0.0, on=None, count=8, method='sequential', cost=0.5):
    """The metrics a run writes for the changes up from `gear`: the shares
    early, on the cut and on the band, and what they cost the stage."""
    on = 1.0 - early - cut if on is None else on
    out = [{'name': 'shift.in_band', 'value': on, 'count': count, 'gear': gear, 'method': method},
           {'name': 'shift.early_share', 'value': early, 'count': count, 'gear': gear, 'method': method},
           {'name': 'shift.cut_share', 'value': cut, 'count': count, 'gear': gear, 'method': method}]
    if cost is not None:
        out.append({'name': 'shift.cost', 'value': cost, 'count': 1, 'gear': gear, 'method': method})
    return out


def fabia_history(tmp_path, surface, grip=11000.0, runs=5):
    """A Fabia whose model knows the game's data (its lights band is 6900
    to 7100) and, on `surface`, its grip in each gear (1st held to `grip`
    newtons)."""
    from tests.test_shift_learner import fabia_pulls, FABIA
    learner = ShiftLearner(str(tmp_path / 't.db'), profile='rally')
    fabia_pulls(learner, grip=grip, surface=surface, runs=runs)
    learner.close()
    h = History(tmp_path / 't.db')
    h.car = h.store.car_id('rally', FABIA, 'acr')
    return h


def test_a_cut_every_session_is_the_focus(tmp_path):
    h = fabia_history(tmp_path, 'tarmac')
    for _ in range(6):
        h.session(shares(2, cut=0.5, cost=0.6) + shares(3, on=1.0, cost=None)
                  + [{'name': 'limiter.held', 'value': 0.1, 'count': 10}])
    tips = h.tips()
    assert [t.kind for t in tips] == ['focus', 'praise']          # and the other gear is praised, not forgotten
    assert tips[0].id == 'shift.cut:2:sequential:rally-stage:tarmac'
    assert tips[0].text == ('2→3 with the sequential: 50 % of your changes up sit on the limiter cut. Change inside the '
                            'lights band, 6900 to 7100 rpm; about 0.6 s a stage.')
    assert tips[0].evidence == ['8 changes up flat out in 1 session (rally stage, tarmac).',
                                "The lights band for 2→3: 6900 to 7100 rpm (the game's own shift lights).",
                                'The same pattern in 6 of your last 6 sessions.']
    assert tips[1].text == 'Your changes up are on the lights band in 3→4 on tarmac.'


def test_a_shift_fault_that_costs_nothing_is_described_not_coached(tmp_path):
    """A tenth of a second a stage is not worth a tip: described once, in the cost's words."""
    h = fabia_history(tmp_path, 'tarmac')
    h.session(shares(2, cut=0.4, cost=0.05))
    tips = h.tips()
    assert [t.kind for t in tips] == ['note'] and tips[0].id.startswith('shift.cheap:2:sequential')
    assert tips[0].text == ('2→3 with the sequential: 0 % of your changes up are early and 40 % on the cut, about 0.1 s '
                            'a stage: too little to coach.')
    h.show(tips)
    assert h.tips() == []                                         # said once


def test_changing_up_early_depends_on_the_surface(tmp_path):
    """Short-shifting can be right on a loose surface: on gravel it is a tip only from 3rd up where the gear
    is measured not to be grip-limited; before it is measured, a note once; the same when the surface is
    unknown; never on snow, ice, or in 1st and 2nd. On tarmac it is a tip."""
    tarmac = fabia_history(tmp_path / 'a', 'tarmac', grip=1e9) if (tmp_path / 'a').mkdir() is None else None
    tarmac.session(shares(2, early=0.8), surface='tarmac')
    [tip] = tarmac.tips()
    assert tip.kind == 'tip' and tip.id == 'shift.early:2:sequential:rally-stage:tarmac'
    assert tip.text == ('2→3 with the sequential: 80 % of your changes up come before 6800 rpm, the start of the '
                        'lights band (6900 to 7100); about 0.5 s a stage. Stay in the gear to the lights.')
    (tmp_path / 'b').mkdir()
    gravel = fabia_history(tmp_path / 'b', 'gravel', grip=9000.0, runs=8)         # 3rd measured not grip-limited
    gravel.session(shares(2, early=0.8) + shares(3, early=0.8), surface='gravel')
    tips = gravel.tips()
    assert [t.id for t in tips] == ['shift.early:3:sequential:rally-stage:gravel']      # 2nd is silent
    assert 'Not a fault on every gear on loose ground' in ' '.join(tips[0].evidence)
    (tmp_path / 'c').mkdir()
    unmeasured = fabia_history(tmp_path / 'c', 'tarmac', grip=1e9)                # only tarmac was driven
    unmeasured.session(shares(3, early=0.8), surface='gravel')
    [note] = unmeasured.tips()
    assert note.kind == 'note' and note.id == 'gate.grip' and 'wait until the grip of that gear' in note.text
    unmeasured.show([note])
    assert unmeasured.tips() == []
    for n, (surface, expect) in enumerate((('unknown', 'gate.surface'), ('snow', None), ('ice', None))):
        (tmp_path / 'd{}'.format(n)).mkdir()
        h = fabia_history(tmp_path / 'd{}'.format(n), 'tarmac', grip=1e9)
        h.session(shares(3, early=0.8), surface=surface)
        found = h.tips()
        assert [t.id for t in found] == ([expect] if expect else []), surface


def test_a_grip_limited_gear_gets_a_note_not_a_tip(tmp_path):
    h = fabia_history(tmp_path, 'gravel', grip=6000.0, runs=8)                     # 3rd measured grip-limited
    h.session(shares(3, early=0.8), surface='gravel')
    [note] = h.tips()
    assert note.kind == 'note' and note.id == 'grip:3:gravel' and 'grip-limited there (measured)' in note.text
    # and the band's low end moves down to where the next gear reaches the grip limit too
    from oversteer import coach_context
    model = Coach(h.store).reader.car_by_id(h.car)
    from oversteer.shift_learner import CarModel
    car = CarModel.from_dict(model['model'])
    low = coach_context.shift_band(coach.car_context(car, 'gravel'), 3, 'sequential')[0]
    assert low < 6900.0


def test_early_and_cut_together_is_one_tip_for_both_places(tmp_path):
    h = fabia_history(tmp_path, 'tarmac')
    h.session(shares(2, early=0.4, cut=0.3, cost=0.7), surface='tarmac')
    [tip] = h.tips()
    assert tip.id == 'shift.band:two:2:sequential:rally-stage:tarmac'
    assert tip.text == ('2→3 with the sequential: 40 % of your changes up come before 6800 rpm and 30 % sit on the '
                        'limiter cut; the lights band is 6900 to 7100 rpm. About 0.7 s a stage.')


def test_the_lights_are_praised_once_per_surface(tmp_path):
    h = fabia_history(tmp_path, 'gravel')
    h.session(shares(2, on=0.8, cut=0.0) + shares(3, on=0.9) + shares(4, on=0.3, early=0.7, cost=None),
              surface='gravel')
    praise = [t for t in h.tips() if t.kind == 'praise']
    assert [t.text for t in praise] == ['Your changes up are on the lights band in 2→3 and 3→4 on gravel.']


def test_progress_is_praised_with_its_numbers(tmp_path):
    h = fabia_history(tmp_path, 'gravel')
    for _ in range(3):
        h.session(shares(2, on=0.2, early=0.4, cut=0.4))
    for _ in range(3):
        h.session(shares(2, on=0.5, early=0.3, cut=0.2))
    [praise] = [t for t in h.tips() if t.kind == 'praise']
    assert praise.text == ('Better: 2→3 with the sequential is on the lights band 50 % of the time over your last 3 '
                           'sessions; 3 days ago it was 20 %.')


def test_growth_needs_twenty_events_a_side(tmp_path):
    h = fabia_history(tmp_path, 'gravel')
    h.session(shares(2, on=0.2, early=0.4, cut=0.4, count=10))
    for _ in range(3):
        h.session(shares(2, on=0.5, early=0.3, cut=0.2))
    assert not [t for t in h.tips() if t.kind == 'praise']


def test_tips_go_quiet_and_come_back(tmp_path):
    """Shown twice without change, a tip becomes a quiet "still:" line; it
    comes back when it gets 20 % worse or after two weeks. At most three
    tips at a time, the costliest first."""
    h = History(tmp_path / 't.db')
    rates = [{'name': 'hpattern.missed', 'value': 10.0, 'count': 40, 'method': 'h-pattern'},
             {'name': 'hpattern.skip', 'value': 8.0, 'count': 40, 'method': 'h-pattern'},
             {'name': 'downshift.over_rev', 'value': 20.0, 'count': 40, 'method': 'h-pattern'},
             {'name': 'limiter.held', 'value': 2.0, 'count': 10}]
    h.session(rates)
    tips = h.tips()
    assert [t.kind for t in tips] == ['tip', 'tip', 'tip']
    assert [t.id for t in tips] == ['limiter.held', 'hpattern.missed:h-pattern', 'hpattern.skip:h-pattern']
    h.show(tips)
    h.show(h.tips())                                                # the tab visited again, the same evening
    h.show(h.tips(days=0.2), days=0.2)
    assert [t.kind for t in h.tips(days=0.2)] == ['tip', 'tip', 'tip']   # one sitting is one showing
    h.show(h.tips(days=1), days=1)
    tips = h.tips(days=1)
    assert [t.id for t in tips] == ['downshift.over_rev:h-pattern', 'limiter.held', 'hpattern.missed:h-pattern',
                                    'hpattern.skip:h-pattern']
    assert [t.kind for t in tips] == ['tip', 'still', 'still', 'still']
    assert tips[1].text.startswith('Still: You held a gear on the limiter on straights')
    h.session([dict(rates[3], value=2.5)])                          # worse: back as a tip
    assert [t.id for t in h.tips(days=1) if t.kind == 'tip'][0] == 'limiter.held'
    assert 'limiter.held' in [t.id for t in h.tips(days=15) if t.kind == 'tip']
    assert len([t for t in h.tips(show_all=True) if t.kind == 'tip']) == 4


def test_the_limiter_tip_says_nothing_of_a_target_or_a_per_km_figure(tmp_path):
    h = History(tmp_path / 't.db')
    h.session([{'name': 'limiter.held', 'value': 2.0, 'count': 10}])
    [tip] = h.tips()
    assert 'Hold it to' not in tip.text and 'per km' not in tip.text
    assert tip.evidence == ['2.0 s per km over 10 km in 1 session.']
    # the old name is not read: nothing held, nothing said
    other = History(tmp_path / 'u.db')
    other.session([{'name': 'limiter.per_km', 'value': 2.0, 'count': 10}])
    assert other.tips() == []


def test_the_way_of_changing_is_compared_with_itself(tmp_path):
    h = fabia_history(tmp_path, 'tarmac')
    h.session(shares(2, on=0.2, early=0.4, cut=0.4, count=12, method='h-pattern', cost=None)
              + shares(2, on=0.9, early=0.1, count=12, method='paddles', cost=None))
    [tip] = [t for t in h.tips() if t.id.startswith('shift.method')]
    assert tip.text == ('2→3 on rally stage, tarmac: 90 % of your changes up with the paddles are on the shift band, '
                        '20 % with the H-pattern.')


def test_both_pedals_on_a_straight_only_matter_on_tarmac_and_circuits(tmp_path):
    for surface, discipline, expected in (('gravel', 'rally-stage', False), ('tarmac', 'rally-stage', True),
                                          ('snow', 'circuit', True), ('tarmac', 'drift', True)):
        h = History(tmp_path / '{}-{}.db'.format(surface, discipline))
        h.session([{'name': 'pedal.drag', 'value': 0.8, 'count': 10}], surface=surface, discipline=discipline)
        found = [t for t in h.tips() if t.id.startswith('pedal.drag')]
        assert bool(found) == expected, (surface, discipline)
    h = History(tmp_path / 'low.db')
    h.session([{'name': 'pedal.drag', 'value': 0.3, 'count': 10}], surface='tarmac')
    assert not [t for t in h.tips() if t.id.startswith('pedal.drag')]


def test_stage_tips_compare_with_the_same_stage(tmp_path):
    h = History(tmp_path / 't.db')
    h.session([{'name': 'corner.loss', 'value': 4.4, 'count': 1}])
    h.session([{'name': 'corner.loss', 'value': 9.0, 'count': 1}], stage='eawrc:5:3')       # another stage
    tips = [t for t in h.tips() if t.id.startswith('corner.loss')]
    assert [t.id for t in tips] == ['corner.loss:eawrc:5:3', 'corner.loss:eawrc:4:12']
    assert tips[1].text == ('On stage eawrc:4:12, your last run lost 4.4 s in its three worst sections against your '
                            'best run there in this car.')
    for value in (1.0, 1.1, 1.0, 1.2):
        h.session([{'name': 'consistency.split_sd', 'value': value, 'count': 4}])
    h.session([{'name': 'consistency.split_sd', 'value': 0.4, 'count': 4}])
    [praise] = [t for t in h.tips() if t.kind == 'praise']
    assert praise.text == 'Steadier on stage eawrc:4:12: your splits vary by 0.4 s, from 1.2.'


def test_a_tip_must_cost_a_tenth_of_the_costliest(tmp_path):
    """A coach opens with where the time is: a tip of 0.3 s beside one of 4 s is quiet, as is a rough-constant
    tip beside it that cost less than a tenth."""
    h = fabia_history(tmp_path, 'tarmac')
    h.session(shares(2, cut=0.5, cost=0.3) + [{'name': 'corner.loss', 'value': 10.0, 'count': 1},
                                               {'name': 'hpattern.skip', 'value': 4.0, 'count': 40,
                                                'method': 'h-pattern'}])
    tips = h.tips()
    assert [(t.id, t.kind) for t in tips] == [('corner.loss:eawrc:4:12', 'tip'),
                                              ('hpattern.skip:h-pattern', 'still'),
                                              ('shift.cut:2:sequential:rally-stage:tarmac', 'still')]
    # with nothing costlier the same tip is shown
    from oversteer.shift_learner import CarModel
    other = History(tmp_path / 'u.db')
    other.store.save_model('rally', 'acr/Skoda Fabia RS Rally2', 'acr', 'Fabia',
                           CarModel('acr/Skoda Fabia RS Rally2').to_dict(), create=True)
    other.car = other.store.car_id('rally', 'acr/Skoda Fabia RS Rally2', 'acr')
    other.session(shares(2, cut=0.5, cost=0.3))
    assert [t.kind for t in other.tips()] == ['tip']
    assert len([t for t in h.tips(show_all=True) if t.kind == 'tip']) == 3


def test_a_drift_profile_is_silent_about_the_gearbox(tmp_path):
    h = fabia_history(tmp_path, 'tarmac')
    h.session(shares(2, cut=0.5, cost=0.6) + [{'name': 'limiter.held', 'value': 2.0, 'count': 10}],
              discipline='drift')
    assert h.tips() == []


def test_the_coach_uses_the_cars_model(tmp_path):
    """With the car's learnt model and no lights, the band runs from the crossover; the tip names it."""
    from tests.sim import drive, exits, analytic_shift
    learner = ShiftLearner(str(tmp_path / 't.db'), profile='rally')
    exits(learner)
    drive(learner, shift_at=6000, runs=3)
    learner.close()
    h = History(tmp_path / 't.db')
    h.car = h.store.car_id('rally', 'test-car', 'unknown')
    h.session(shares(2, early=0.8, cost=0.6), surface='tarmac')
    [tip] = h.tips()
    best = learner.car.best_shift(2)[0]
    assert abs(best - analytic_shift(2)) < 150
    assert tip.text.startswith('2→3 with the sequential: 80 % of your changes up come before {:.0f} rpm, the start of '
                               'the shift band'.format(best - 100))
    assert tip.text.endswith('about 0.6 s a stage. Stay in the gear longer.')


def test_nothing_the_coach_says_claims_to_know_the_drivers_mind(tmp_path):
    """The words the design keeps out of every sentence (docs/coach-techniques.md, 7.5)."""
    import ast
    import os
    banned = ('trust', 'never', 'always', 'that is how', 'you are not sure', 'hold it to about',
              'stay on one pedal', "the best from the game's engine data")
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'oversteer')
    for name in ('coach.py', 'tuning.py', 'shift_learner.py'):
        with open(os.path.join(here, name), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Module)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                text = node.value.lower()
                for word in banned:
                    assert word not in text, (name, word, node.value)
