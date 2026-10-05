"""The coach's tips by place and cause (docs/coach-techniques.md, section 7.3, build step 2): the sections a
run lost or gained time in, the pedals and the throttle leaving corners, spins and offs, the speed through
the same corner from run to run, the limiter held on a straight against the reference, launches, and what
select() does with praise and technique lines. Runs here are written as the store holds them (corners, events,
metrics, a trace where the rule reads one)."""

from oversteer import coach, coach_context as cc
from oversteer.coach import Coach, DAY, Tip, select
from oversteer.telemetry_store import TRACE_CHANNELS
from tests.test_coach import History
from tests.test_coach_context import C, NAN, road, stored

STAGE_KEY = 'acr:test:stage'


class Stage(History):
    """A car on one stage (named 'Test Stage'), a run at a time, a day apart, each with the corners, events and
    metrics the context layer would have written."""

    def __init__(self, path, game='acr', **kw):
        super().__init__(path, **kw)
        self.car = self.store.car_id(self.profile, game + '/17', game, 'Test car', {})
        self.runs = []

    def drive(self, corners, events=(), metrics=(), surface='gravel', discipline='rally-stage', run_class='clean',
              finished=1, result_time=200.0, wet='dry', trace=None, days=1.0, course=3000.0, stage=STAGE_KEY):
        store = self.store
        self.t += days * DAY
        store.begin()
        store.upsert_stage(stage, 'acr', course, name='Test Stage')
        session = store.start_session(self.profile, self.car, 'acr', self.t, stage=stage)
        run = store.start_run(session, 1, self.t, stage)
        store.end_run(run, ended=self.t + 300, distance=course, finished=finished, course=course, run_class=run_class,
                      result_time=result_time if finished else None, surface=surface, discipline=discipline, wet=wet)
        store.add_corners(run, corners)
        store.add_events(run, list(events))
        if trace is not None:
            store.add_trace(run, trace)
        store.add_metrics(session, run, [dict(m, discipline=discipline, surface=surface) for m in
                                         (metrics or [{'name': 'corner.loss', 'value': 0.0, 'count': 1}])])
        store.commit()
        self.runs.append(run)
        return run

    def by_id(self, prefix, **kwargs):
        return [t for t in self.tips(show_all=True, **kwargs) if t.id.startswith(prefix)]


KMH = 1 / 3.6


def start():
    """The stage's first corner: its section holds the launch, and is not coached as a corner."""
    return stored(150.0, complex_=0, direction=1)


def reference_corners(**kw):
    return [start(), stored(600.0, complex_=1, direction=-1, **kw), stored(1200.0, complex_=2, direction=1)]


def corners_with(second=None, third=None, first_loss=None):
    """The run's corners against reference_corners(): the second corner (600 m) and the third (1200 m) as
    given (stored() fields, `loss` for what it lost)."""
    return [stored(150.0, complex_=0, direction=1, loss=first_loss),
            stored(600.0, complex_=1, direction=-1, **(second or {})),
            stored(1200.0, complex_=2, direction=1, **(third or {}))]


def two_runs(h, **mine):
    h.drive(reference_corners(), result_time=200.0)
    return h.drive(corners_with(**mine), result_time=203.0)


def test_a_section_that_lost_time_is_named_with_its_place_numbers_and_action(tmp_path):
    h = Stage(tmp_path / 't.db')
    two_runs(h, second={'brake_d': 75.0, 'min_speed': 10.0 - 7 * KMH, 'exit_speed': 20.0 - 5 * KMH, 'loss': (0.4, 0.2)})
    [tip] = [t for t in h.tips() if t.id.startswith('corner.section')]
    assert tip.id == 'corner.section:{}:600'.format(STAGE_KEY) and tip.kind == 'tip'
    ref = 'Test car, 200.0 s on {}'.format(coach._date(h.t - DAY))
    assert tip.text == ('On Test Stage, the 3 right at 0.6 km, 0.6 s behind your best clean run here ({}): you braked 25 m '
                        'earlier, were 7 km/h slower at the slowest point and left 5 km/h slower. '
                        'Brake about 25 m later, as your best run did.'.format(ref))
    assert abs(tip.cost - 0.6) < 1e-9 and tip.ref
    assert tip.evidence == ['0.4 s of it before the slowest point and 0.2 s after.']


def test_each_cause_has_its_own_action(tmp_path):
    cases = [
        ({'brake_d': 50.0, 'min_speed': 10.0 - 6 * KMH, 'exit_speed': 20.0 - 5 * KMH},
         'Brake at the same place and carry more speed in: your best run was 6 km/h quicker through the middle.'),
        ({'brake_d': 30.0, 'min_speed': 10.0, 'exit_speed': 20.0 - 6 * KMH},
         'you braked 20 m later, took the same minimum speed and left 6 km/h slower. Brake 20 m earlier, where your '
         'best run did.'),
        ({'brake_d': 50.0, 'entry_speed': 25.0 + 8 * KMH, 'exit_speed': 20.0 - 6 * KMH},
         'Enter 8 km/h slower, as your best run did.'),
        ({'throttle_on_t': 1.1, 'exit_speed': 20.0 - 4 * KMH},
         'Throttle sooner, as soon as the nose points out: your best run was 0.6 s earlier.'),
        ({'min_speed': 10.0 - 6 * KMH, 'counter_steer': 0.3, 'exit_speed': 20.0 - 1 * KMH},
         'Rotate the car less on the way in'),
        ({'brake_d': None, 'min_speed': 10.0 - 9 * KMH, 'exit_speed': 20.0 - 8 * KMH},
         'Carry more speed through it: your best run was 9 km/h quicker at the slowest point.'),
    ]
    for n, (fields, expected) in enumerate(cases):
        h = Stage(tmp_path / 'c{}.db'.format(n))
        two_runs(h, second=dict(fields, loss=(0.1, 0.5)))
        [tip] = [t for t in h.tips() if t.id.startswith('corner.section')]
        assert expected in tip.text, (fields, tip.text)


def test_a_section_with_no_difference_in_the_numbers_says_where_the_time_went(tmp_path):
    h = Stage(tmp_path / 't.db')
    two_runs(h, second={'loss': (0.1, 0.6)})
    [tip] = [t for t in h.tips() if t.id.startswith('corner.section')]
    assert 'were within a few km/h and metres of it' in tip.text
    assert tip.text.endswith('The time went after the slowest point: look at how early you were back on the throttle '
                             'and the line you took out of it.')
    h = Stage(tmp_path / 'u.db')
    two_runs(h, second={'loss': (0.7, 0.1)})
    [tip] = [t for t in h.tips() if t.id.startswith('corner.section')]
    assert tip.text.endswith('The time went before the slowest point: look at where you braked and the line you '
                             'took in.')


def test_the_start_is_not_a_corner(tmp_path):
    """The first section holds the launch: a slow start is not a corner fault and a quick one is not praised."""
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(first_loss=(2.0, 1.0)), result_time=203.0)
    assert not [t for t in h.tips(show_all=True) if t.id.startswith('corner.')]


def test_only_the_three_costliest_sections_and_those_that_lost_a_tenth_are_named(tmp_path):
    h = Stage(tmp_path / 't.db')
    ref = [start()] + [stored(300.0 * n, complex_=n) for n in range(1, 7)]
    h.drive(ref, result_time=200.0)
    losses = [0.5, 0.05, 0.4, 0.4, 0.4, 0.4]
    mine = [stored(150.0, complex_=0)] + [stored(300.0 * n, complex_=n, loss=(x, 0.0), min_speed=10.0 - 4 * KMH)
                                          for n, x in enumerate(losses, 1)]
    h.drive(mine, result_time=203.0)
    tips = [t for t in h.tips(show_all=True) if t.id.startswith('corner.section')]
    assert [t.id.split(':')[-1] for t in tips] == ['300', '1200', '900']     # by cost, then by name
    assert tips[0].evidence[1].startswith('2.1 s behind your best clean run over 5 sections. Spread over the stage; the '
                                          'biggest are')


def test_most_of_it_in_two_places_only_when_two_sections_hold_half(tmp_path):
    h = Stage(tmp_path / 't.db')
    ref = [start()] + [stored(300.0 * n, complex_=n) for n in range(1, 5)]
    h.drive(ref, result_time=200.0)
    mine = [stored(150.0, complex_=0)] + [stored(300.0 * n, complex_=n, loss=(x, 0.0)) for n, x in
                                          enumerate([1.5, 1.0, 0.1, 0.1], 1)]
    h.drive(mine, result_time=203.0)
    [first, *_] = [t for t in h.tips(show_all=True) if t.id.startswith('corner.section')]
    assert first.evidence[1].endswith('Most of it in two places.')


def test_a_late_throttle_in_two_corners_is_one_tip_and_the_corners_are_not_told_twice(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    late = {'throttle_on_t': 1.0, 'exit_speed': 20.0 - 4 * KMH, 'loss': (0.0, 0.4)}
    h.drive(corners_with(second=dict(late), third=dict(late)), result_time=203.0)
    [tip] = h.by_id('throttle.late')
    assert tip.text.startswith('On Test Stage, the throttle came 0.5 s later on average than in your best clean run here (')
    assert 'leaving the 3 right at 0.6 km and the 3 left at 1.2 km: 0.8 s of exit time. Throttle as soon as the ' \
           'nose points out.' in tip.text
    assert not h.by_id('corner.section')                          # said once, as a pattern
    # one corner late is the section's own tip
    h = Stage(tmp_path / 'u.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second=dict(late)), result_time=203.0)
    assert not h.by_id('throttle.late') and len(h.by_id('corner.section')) == 1
    # snow and ice: partial throttle leaving a corner is the surface's doing
    h = Stage(tmp_path / 'v.db')
    h.drive(reference_corners(), result_time=200.0, surface='snow')
    h.drive(corners_with(second=dict(late), third=dict(late)), result_time=203.0, surface='snow')
    assert not h.by_id('throttle.late')


def test_both_pedals_leaving_corners_is_a_tip_on_tarmac_with_three_corners_that_cost(tmp_path):
    def run(surface, n, discipline='rally-stage', tag=''):
        h = Stage(tmp_path / (surface + str(n) + discipline + tag + '.db'))
        ref = [start()] + [stored(300.0 * k, complex_=k) for k in range(1, 5)]
        h.drive(ref, surface=surface, discipline=discipline, result_time=200.0)
        mine = [stored(150.0, complex_=0)] + [
            stored(300.0 * k, complex_=k, overlap_exit=0.5 if k <= n else 0.0, loss=(0.0, 0.3 if k <= n else 0.0))
            for k in range(1, 5)]
        h.drive(mine, surface=surface, discipline=discipline, result_time=203.0)
        return h.by_id('pedal.exit')
    [tip] = run('tarmac', 3)
    assert tip.text.startswith('On Test Stage, you were on both pedals for 1.5 s leaving the 3 left at 0.3 km, '
                               'the 3 left at 0.6 km and the 3 left at 0.9 km, which cost 0.9 s of exit time '
                               'against your best clean run here (')
    assert tip.text.endswith('Come off the brake before the throttle goes down.')
    assert not run('tarmac', 2)                                   # two corners are not a habit
    assert not run('gravel', 4)                                   # on gravel it is how a Rally2 is driven
    assert not run('snow', 4, 'circuit')
    assert run('tarmac', 3, 'circuit', 'x')


def test_coasting_into_a_corner_is_coached_on_tarmac_and_not_in_a_hairpin_or_on_gravel(tmp_path):
    def run(surface, tightness='3', tag=''):
        h = Stage(tmp_path / (surface + tightness + tag + '.db'))
        h.drive(reference_corners(), surface=surface, result_time=200.0)
        h.drive(corners_with(second={'coast_entry': 0.6, 'tightness': tightness, 'loss': (0.4, 0.0)}),
                surface=surface, result_time=203.0)
        return h.by_id('coast.entry')
    [tip] = run('tarmac')
    assert ('you coasted 0.6 s longer than in your best clean run here (' in tip.text
            and 'going into the 3 right at 0.6 km, which lost 0.4 s before the slowest point. Go from the brake to '
                'the throttle without a gap.' in tip.text)
    assert not run('tarmac', 'hairpin')                           # rotated like a gravel corner
    assert not run('gravel')                                      # the rotation, not dead time


def test_the_gravel_entry_that_gained_is_praised_on_gravel_only(tmp_path):
    def run(surface):
        h = Stage(tmp_path / (surface + '.db'))
        h.drive(reference_corners(), surface=surface, result_time=200.0)
        h.drive(corners_with(second={'brake_d': 70.0, 'exit_speed': 20.0 + 6 * KMH, 'loss': (-0.1, -0.4)}),
                surface=surface, result_time=203.0)
        return h.by_id('corner.entry')
    [praise] = run('gravel')
    assert praise.kind == 'praise'
    assert praise.text.startswith('On Test Stage, the 3 right at 0.6 km, 0.5 s up on your best clean run here (')
    assert praise.text.endswith('you braked 20 m earlier, took the same minimum speed and left 6 km/h faster.')
    assert not run('tarmac')


def test_the_best_run_yet_is_praised_with_where_it_was_won(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'brake_d': 30.0, 'min_speed': 10.0, 'exit_speed': 20.0 + 5 * KMH,
                                 'loss': (-0.5, -1.4)}), result_time=197.0)
    [praise] = h.by_id('corner.best')
    assert praise.text == ('On Test Stage, your best clean run yet in the Test car: 3.0 s quicker than {} (200.0 s). '
                           '1.9 s of it in the 3 right at 0.6 km, where you braked 20 m later, took the same '
                           'minimum speed and left 5 km/h faster.'.format(coach._date(h.t - DAY)))
    assert praise.kind == 'praise'
    # not for a run that went off: it is not a reference, and the praise is for a clean run
    h = Stage(tmp_path / 'u.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(), result_time=197.0, run_class='off')
    assert not h.by_id('corner.best')


def test_a_learning_run_a_first_run_and_other_conditions_get_no_corner_tips(tmp_path):
    loss = {'brake_d': 75.0, 'min_speed': 10.0 - 7 * KMH, 'loss': (0.4, 0.2)}
    h = Stage(tmp_path / 't.db')
    h.drive(corners_with(second=dict(loss)), result_time=203.0)               # the first run: no reference
    assert not h.by_id('corner.section')
    h = Stage(tmp_path / 'u.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second=dict(loss)), result_time=203.0, run_class='learning')
    assert not h.by_id('corner.section')
    h = Stage(tmp_path / 'v.db')
    h.drive(reference_corners(), result_time=200.0, wet='dry')                 # a dry best against a wet run
    h.drive(corners_with(second=dict(loss)), result_time=203.0, wet='wet')
    assert not h.by_id('corner.section')
    h = Stage(tmp_path / 'w.db')
    h.drive(reference_corners(), result_time=200.0, run_class='off')           # an off run is not a reference
    h.drive(corners_with(second=dict(loss)), result_time=203.0)
    assert not h.by_id('corner.section')


def test_a_restart_is_not_coached_and_the_run_before_it_is(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'brake_d': 75.0, 'min_speed': 10.0 - 7 * KMH, 'loss': (0.4, 0.2)}), result_time=203.0)
    h.drive(corners_with(), run_class='restart', finished=0)
    assert [t.id for t in h.tips() if t.id.startswith('corner.section')] == ['corner.section:{}:600'.format(STAGE_KEY)]


def event(kind, d0, d1, klass=None, value=1.0, detail=None, gear=None):
    return {'kind': kind, 'class': klass, 'd0': d0, 'd1': d1, 't0': None, 't1': None, 'gear': gear, 'value': value,
            'detail': detail}


def test_a_spin_or_a_near_stop_is_a_corner_fault_and_an_off_is_named(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(corners_with(), events=[event('spin', 580.0, 620.0, value=230.0), event('stall', 1190.0, 1215.0),
                                    event('off', 2100.0, 2130.0, 'reverse', 4.0)])
    spin, = h.by_id('corner.spin')
    assert spin.text == ('On Test Stage, you spun in the 3 right at 0.6 km. Rotate the car less on the way in (a smaller '
                         'flick, a shorter handbrake pull) and get the throttle on sooner.')
    stall, = h.by_id('corner.stall')
    assert stall.text == ('On Test Stage, the car nearly stopped in the 3 left at 1.2 km. Look at what slowed it there, '
                          'the braking point, the line or the gear, and get the throttle on sooner.')
    assert spin.cost > stall.cost
    off, = h.by_id('corner.off')
    assert off.kind == 'note' and off.text == ('On Test Stage, off at 2.1 km: the corners within 100 m of it are left '
                                               'out of the comparisons.')
    # a game whose attitude signals are unchecked has neither a spin nor a stall, and still has the off
    h = Stage(tmp_path / 'u.db', game='wrcg')
    h.drive(corners_with(), events=[event('spin', 580.0, 620.0, value=230.0), event('off', 2100.0, 2130.0, 'long')])
    assert not h.by_id('corner.spin') and len(h.by_id('corner.off')) == 1
    # with steering against the yaw in that corner it is the rotation
    h = Stage(tmp_path / 'r.db')
    h.drive([start(), stored(600.0, complex_=1, direction=-1), stored(1200.0, complex_=2, direction=1, counter_steer=0.4)],
            events=[event('stall', 1190.0, 1215.0)])
    assert h.by_id('corner.stall')[0].text.endswith('Rotate the car less on the way in (a smaller flick, a shorter '
                                                    'handbrake pull) and get the throttle on sooner.')


def spread_event(apex, sd_kmh, median_kmh, runs=6):
    return event('spread', apex - 20.0, apex + 20.0, value=sd_kmh * KMH, detail={
        'median': median_kmh * KMH, 'min': (median_kmh - sd_kmh) * KMH, 'max': (median_kmh + sd_kmh) * KMH,
        'runs': runs, 'apex': apex})


def test_the_speed_through_the_same_corner_varying_run_to_run_names_the_corners(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(min_speed=16.0), result_time=200.0)
    # the corners at 600 m and 1200 m vary by 13 and 9 km/h; the start's section by 20 (the launch: left out)
    h.drive(corners_with(), result_time=203.0,
            events=[spread_event(150.0, 20.0, 60.0), spread_event(600.0, 13.0, 60.0), spread_event(1200.0, 9.0, 70.0)])
    [tip] = h.by_id('corner.spread')
    assert tip.text == ('On Test Stage, your speed through the 3 right at 0.6 km and the 3 left at 1.2 km varies from '
                        'run to run, by 13 and 9 km/h over the last 6 runs. Your best clean run took them at 58 and 36 '
                        'km/h: aim for that each time.')
    assert tip.evidence == ['47 to 73 km/h through the 3 right at 0.6 km.', '61 to 79 km/h through the 3 left at '
                            '1.2 km.']
    assert not tip.ref and tip.cost > 0
    # a spread under 8 km/h, or under a tenth of the speed, is not a tip
    h = Stage(tmp_path / 'u.db')
    h.drive(corners_with(), events=[spread_event(600.0, 6.0, 40.0), spread_event(1200.0, 9.0, 120.0)])
    assert not h.by_id('corner.spread')
    # and with no reference the advice does not know the best run's speed
    h = Stage(tmp_path / 'v.db')
    h.drive(corners_with(), events=[spread_event(600.0, 13.0, 60.0)])
    [tip] = h.by_id('corner.spread')
    assert tip.text.endswith('varies from run to run, by 13 km/h over the last 6 runs. Pick the speed of your quickest '
                             'run through it and repeat it.')


def test_five_costly_corners_taken_at_the_same_speed_every_time_is_praised(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive([start()] + [stored(300.0 * n, complex_=n) for n in range(1, 7)], result_time=200.0)
    mine = [stored(150.0, complex_=0)] + [stored(300.0 * n, complex_=n, loss=(0.05 * n, 0.0)) for n in range(1, 7)]
    events = [spread_event(300.0 * n, 3.0, 60.0) for n in range(1, 7)]
    h.drive(mine, result_time=203.0, events=events)
    [praise] = h.by_id('corner.steady')
    assert praise.kind == 'praise' and praise.text.startswith(
        'Your speed through the five costliest corners on Test Stage is within 4 km/h from run to run (')
    events[3] = spread_event(1200.0, 6.0, 60.0)
    h2 = Stage(tmp_path / 'u.db')
    h2.drive([start()] + [stored(300.0 * n, complex_=n) for n in range(1, 7)], result_time=200.0)
    h2.drive(mine, result_time=203.0, events=events)
    assert not h2.by_id('corner.steady')


def with_overlap(corners, overlapping):
    return [dict(k, overlap_entry=0.5 if n < overlapping else 0.0) for n, k in enumerate(corners)]


def ten_corners():
    return [stored(300.0 * n, complex_=n) for n in range(1, 11)]


def test_left_foot_braking_is_described_once_and_never_praised(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(with_overlap(ten_corners(), 7))
    [tip] = [t for t in h.tips() if t.id.startswith('technique:overlap')]
    assert tip.kind == 'technique' and tip.text == 'You left-foot brake into 7 in 10 corners on gravel.'
    assert tip.evidence == ['7 of 10 corners with both pedals down for 0.2 s or more going in.']
    assert abs(tip.value - 0.7) < 1e-9
    assert not [t for t in h.tips(show_all=True) if t.kind == 'praise']
    h.show([tip])                                                # shown once
    assert not [t for t in h.tips() if t.kind == 'technique']
    h.drive(with_overlap(ten_corners(), 6))                      # a tenth less: the same line
    assert not [t for t in h.tips() if t.kind == 'technique']
    h.drive(with_overlap(ten_corners(), 3))                      # forty points less: said again
    [again] = [t for t in h.tips() if t.kind == 'technique']
    assert again.text == 'You left-foot brake into 3 in 10 corners on gravel.'
    h = Stage(tmp_path / 'u.db')
    h.drive(with_overlap(ten_corners(), 2))                      # a few corners are not a technique
    assert not [t for t in h.tips() if t.kind == 'technique']
    h = Stage(tmp_path / 'v.db')
    h.drive(with_overlap(ten_corners()[:8], 8))                  # nor are eight corners enough to say
    assert not [t for t in h.tips() if t.kind == 'technique']


def test_left_foot_braking_on_tarmac_is_only_technique_in_a_front_wheel_drive_or_turbo_car(tmp_path):
    from oversteer.shift_learner import CarModel
    h = Stage(tmp_path / 't.db')
    h.drive(with_overlap(ten_corners(), 8), surface='tarmac')
    assert not [t for t in h.tips() if t.kind == 'technique']      # an all-wheel drive on tarmac: nothing said
    model = CarModel('acr/17')
    model.drivetrain = 'fwd'
    h.store.begin()
    h.store.save_model(h.profile, 'acr/17', 'acr', 'Test car', model.to_dict(), create=True)
    h.store.commit()
    [tip] = [t for t in h.tips() if t.kind == 'technique']
    assert tip.text == 'You left-foot brake into 8 in 10 corners on tarmac.'
    hairpins = [dict(k, tightness='hairpin') for k in with_overlap(ten_corners(), 8)]
    h.drive(hairpins, surface='tarmac')                            # tarmac hairpins are driven like gravel corners
    assert not [t for t in h.tips() if t.kind == 'technique']


def test_a_gear_held_into_the_same_corner_again_and_again_is_gearing(tmp_path):
    def run(h, d0, seconds=1.5, gear=3):
        return h.drive(corners_with(), events=[event('limiter', d0, d0 + 100.0, 'held-corner', seconds, gear=gear)])
    h = Stage(tmp_path / 't.db')
    for d0 in (2000.0, 2020.0, 1990.0):
        run(h, d0)
    [tip] = [t for t in h.tips() if t.id.startswith('technique:held')]
    assert tip.kind == 'technique'
    assert tip.text == ('On Test Stage, 3rd is short for the run from 2.0 to 2.1 km; holding it on the limiter there '
                        'is fine.')
    assert tip.evidence == ['3 of your last 3 runs, 1.5 s on average.']
    h = Stage(tmp_path / 'u.db')                                     # twice is not a place
    run(h, 2000.0)
    run(h, 2010.0)
    run(h, 4000.0)
    assert not [t for t in h.tips() if t.id.startswith('technique:held')]
    h = Stage(tmp_path / 'v.db')                                     # a brief touch is not worth a word
    for d0 in (2000.0, 2020.0, 1990.0):
        run(h, d0, seconds=0.6)
    assert not [t for t in h.tips() if t.id.startswith('technique:held')]


def cruise(speed, gear, n=300, brake_after=200):
    from tests.test_coach_context import rows
    return rows(n, speed=speed, gear=gear, brake=lambda i, t: 0.6 if i > brake_after else 0.0, throttle=1.0,
                rpm=7480.0)


def test_a_held_straight_is_a_tip_only_when_the_reference_was_quicker_to_the_next_braking(tmp_path):
    held = event('limiter', 100.0, 250.0, 'held-straight', 3.0, gear=3)
    metrics = [{'name': 'limiter.held', 'value': 2.0, 'count': 10}]

    def run(ref_speed, ref_gear, tag):
        h = Stage(tmp_path / (tag + '.db'))
        h.drive(reference_corners(), result_time=200.0, trace=cruise(ref_speed, ref_gear), metrics=metrics)
        h.drive(corners_with(), result_time=203.0, trace=cruise(20.0, 3), events=[held], metrics=metrics)
        return h
    h = run(24.0, 4, 'quicker')
    [tip] = h.by_id('limiter.held:')
    assert tip.ref and tip.cost > 2.0
    assert tip.text.startswith('On Test Stage, from 0.1 km you held 3rd on the limiter for 3.0 s with no corner to use '
                               'it for: your best clean run was 2.')
    assert tip.text.endswith(' s quicker to the next braking, in 4th. Change up as the lights flash.')
    assert not [t for t in h.tips(show_all=True) if t.id == 'limiter.held']      # the per-km figure is not used here
    h = run(20.0, 3, 'same')                                                      # the reference held it too
    assert not h.by_id('limiter.held')
    # in 4th at the same speed: the reference changed up
    h = run(20.0, 4, 'gear')
    assert len(h.by_id('limiter.held:')) == 1
    # with no reference run yet the per-km figure stands in
    h = Stage(tmp_path / 'first.db')
    h.drive(corners_with(), result_time=203.0, trace=cruise(20.0, 3), events=[held],
            metrics=[{'name': 'limiter.held', 'value': 2.0, 'count': 10}])
    assert [t.id for t in h.tips() if t.id.startswith('limiter.held')] == ['limiter.held']


def launches(h, t50s, bog=0.0, cut=None, surface='gravel', driver=True):
    for n, t50 in enumerate(t50s):
        metrics = [{'name': 'launch.t50', 'value': t50, 'count': 1}, {'name': 'launch.g', 'value': 0.6, 'count': 1}]
        if driver:
            metrics.append({'name': 'launch.bog', 'value': bog, 'count': 1})
        if cut is not None:
            metrics.append({'name': 'launch.cut', 'value': cut[n], 'count': 1})
        h.drive([], surface=surface, metrics=metrics)


def test_launches_that_repeat_are_praised_and_the_games_own_are_not(tmp_path):
    h = Stage(tmp_path / 't.db')
    launches(h, [1.90, 1.83, 1.85, 1.84, 1.86])
    [praise] = [t for t in h.tips() if t.id.startswith('launch.steady')]
    assert praise.kind == 'praise' and praise.id == 'launch.steady:gravel'
    assert praise.text == ('Your last 5 launches on gravel took 1.83 to 1.90 s to 50 km/h, within a tenth of your best '
                           '(1.83 s).')
    h = Stage(tmp_path / 'u.db')                                         # a tenth apart is not the same launch
    launches(h, [1.90, 1.83, 1.85, 1.84, 2.1])
    assert not [t for t in h.tips() if t.id.startswith('launch.steady')]
    h = Stage(tmp_path / 'v.db')                                         # steady, but not the car's best on gravel
    launches(h, [1.50, 1.9, 1.95, 1.9, 1.92, 1.93])
    assert not [t for t in h.tips() if t.id.startswith('launch.steady')]
    h = Stage(tmp_path / 'w.db')                                         # the game's auto-clutch: no bog verdict, no praise
    launches(h, [3.36, 3.37, 3.38, 3.37, 3.36], driver=False)
    assert not [t for t in h.tips(show_all=True) if t.id.startswith('launch.')]


def test_a_game_whose_speed_is_unchecked_gets_no_launch_coaching(tmp_path):
    h = Stage(tmp_path / 't.db', game='wrcg')
    launches(h, [0.2, 0.4, 0.3, 0.2, 0.3], bog=1.0)
    assert not [t for t in h.tips(show_all=True) if t.id.startswith('launch.')]
    h = Stage(tmp_path / 'u.db')
    launches(h, [2.0, 2.4, 2.1, 2.5, 2.2], bog=1.0)
    [tip] = [t for t in h.tips() if t.id == 'launch.bog']
    assert tip.text.startswith('5 of your last 5 launches bogged')


def test_holding_the_launch_gear_on_the_cut_is_a_late_change_and_a_brief_touch_is_not(tmp_path):
    h = Stage(tmp_path / 't.db')
    launches(h, [1.9] * 5, cut=[0.5, 0.5, 0.5, 0.0, 0.0])
    [tip] = [t for t in h.tips() if t.id == 'launch.cut:gravel']
    assert tip.text == ('3 of your last 5 launches on gravel sat on the limiter in the launch gear for 0.5 s: change up '
                        'as the cut comes in.')
    assert tip.cost > 0 and not tip.ref
    h = Stage(tmp_path / 'u.db')
    launches(h, [1.9] * 5, cut=[0.2, 0.1, 0.5, 0.0, 0.5])
    assert not [t for t in h.tips(show_all=True) if t.id.startswith('launch.cut')]


def praise(id, cost=1.0, value=1.0):
    return Tip(id, 'praise', id, value=value, cost=cost)


def candidate(id, kind='tip', cost=1.0, ref=True, value=1.0):
    return Tip(id, kind, id, value=value, cost=cost, count=1, ref=ref)


def seen_state(times=3, value=1.0, last=0.0):
    return {'first_shown': 0.0, 'last_shown': last, 'times': times, 'value': value, 'quiet': 0}


def test_two_praise_lines_a_view_and_one_when_two_tips_show():
    out = select([], [praise('a', 3.0), praise('b', 2.0), praise('c', 1.0)], [], {}, 100.0)
    assert [t.id for t in out] == ['a', 'b']
    # the praise already shown twice is quiet... until two tips show, when the best one is said again
    stale = {'a': seen_state(), 'b': seen_state()}
    out = select([candidate('t1'), candidate('t2', cost=0.9)], [praise('a', 3.0), praise('b', 2.0)], [], stale, 100.0)
    assert [t.id for t in out] == ['t1', 't2', 'a']
    out = select([candidate('t1')], [praise('a', 3.0), praise('b', 2.0)], [], stale, 100.0)
    assert [t.id for t in out] == ['t1']
    assert [t.id for t in select([], [praise('a')], [], stale, 100.0)] == []


def test_a_habit_does_not_outrank_a_costlier_tip():
    habit = candidate('shift.habit', kind='focus', cost=0.4)
    corner = candidate('corner.section:x:1', cost=1.2)
    out = select([habit, corner], [], [], {}, 100.0)
    assert [(t.id, t.kind) for t in out] == [('corner.section:x:1', 'tip'), ('shift.habit', 'tip')]
    habit = candidate('shift.habit', kind='focus', cost=2.0)
    out = select([habit, candidate('corner.section:x:1', cost=1.2)], [], [], {}, 100.0)
    assert [(t.id, t.kind) for t in out] == [('shift.habit', 'focus'), ('corner.section:x:1', 'tip')]


def test_a_technique_line_is_shown_once_and_comes_back_when_its_share_moves_twenty_points():
    line = Tip('technique:overlap:gravel', 'technique', 'You left-foot brake into 7 in 10 corners on gravel.',
               value=0.7)
    assert [t.id for t in select([], [], [], {}, 100.0, techniques=[line])] == ['technique:overlap:gravel']
    state = {line.id: seen_state(times=1, value=0.7)}
    assert select([], [], [], state, 100.0, techniques=[line]) == []
    near = Tip(line.id, 'technique', line.text, value=0.55)
    assert select([], [], [], state, 100.0, techniques=[near]) == []
    far = Tip(line.id, 'technique', line.text, value=0.45)
    assert [t.id for t in select([], [], [], state, 100.0, techniques=[far])] == [line.id]
    # the same line from two stages is one line
    twice = [line, Tip(line.id, 'technique', 'again', value=0.6)]
    assert [t.text for t in select([], [], [], {}, 100.0, techniques=twice)] == [line.text]


def three_corner_run(v2, v3, v1=10.0):
    from tests.test_coach_context import stage as make_stage
    from oversteer.drive_log import find_corners
    points = [(0.0, 28.0), (430.0, 28.0), (500.0, v1), (600.0, 28.0), (1000.0, 28.0), (1100.0, v2), (1200.0, 28.0),
              (1600.0, 28.0), (1700.0, v3), (1800.0, 28.0), (2300.0, 28.0)]
    corners = [(470.0, 530.0, 1, 0.5), (1070.0, 1130.0, -1, 0.5), (1670.0, 1730.0, 1, 0.5)]
    tr = make_stage(points, corners, brake=lambda i, t, d, a: 0.6 if a < -1.0 else 0.0)
    return tr, find_corners(tr)


def drive_traced(h, v2, v3, reference=None):
    """A run through three corners written with its trace, its corners and what it lost to `reference` (its trace
    and corners)."""
    tr, corners = three_corner_run(v2, v3)
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    cc.mark_off(corners, [])
    if reference is not None:
        cc.section_loss(tr, sections, {'trace': reference[0], 'corners': reference[1],
                                       'course': reference[0][-1][C['distance']]})
    h.drive(corners, trace=tr, result_time=tr[-1][C['t']], course=tr[-1][C['distance']])
    return tr, corners


def test_the_best_sections_put_together_and_the_section_where_this_run_was_best(tmp_path):
    h = Stage(tmp_path / 't.db')
    a = drive_traced(h, v2=14.0, v3=10.0)                          # the best run: quick through the second corner
    drive_traced(h, v2=10.0, v3=13.0, reference=a)                 # quick through the third, slower overall
    drive_traced(h, v2=12.0, v3=16.0, reference=a)                 # the latest run: its third corner is the best yet
    [best] = h.by_id('corner.bestsection')
    assert best.kind == 'praise'
    assert best.text.startswith('On Test Stage, your best yet through the ') and ' left at 1.7 km: ' in best.text \
        and best.text.endswith(' s up on your best before this run.')
    [possible] = h.by_id('corner.possible')
    assert possible.kind == 'note'
    assert possible.text.startswith('On Test Stage, your best sections put together make ')
    assert 'under your best clean run in the Test car (' in possible.text and ' left at 1.7 km, ' in possible.text
    # fewer than three runs: nothing to put together
    h = Stage(tmp_path / 'u.db')
    a = drive_traced(h, v2=14.0, v3=10.0)
    drive_traced(h, v2=10.0, v3=16.0, reference=a)
    assert not h.by_id('corner.possible')


def test_a_run_through_the_real_path_is_coached_section_by_section(tmp_path):
    """A slower second run through the sim's stage: written by the drive log, read back by the coach."""
    from tests.test_coach import drive_stages
    learner, reader = drive_stages(tmp_path, (30.0, 26.0))
    car = reader.car_list('_no_profile')[0]['id']
    tips = Coach(reader, now=2e9).tips('_no_profile', car, show_all=True)
    sections = [t for t in tips if t.id.startswith('corner.section')]
    assert sections and all(t.ref for t in sections)
    assert sections[0].text.startswith('On stage eawrc:4:12, the ') and 's behind your best clean run here (' in sections[0].text
    assert not [t for t in tips if t.id.startswith('corner.loss')]     # the section tips replace the total
    learner.close()


def test_a_stop_in_the_corner_of_a_spin_is_the_spin_and_two_offs_close_together_are_one_note(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(corners_with(), events=[event('spin', 580.0, 620.0, value=230.0), event('stall', 600.0, 640.0),
                                    event('off', 2100.0, 2130.0, 'reverse', 4.0),
                                    event('off', 2102.0, 2140.0, 'hit', 3.5)])
    assert len(h.by_id('corner.spin')) == 1 and not h.by_id('corner.stall')
    assert len(h.by_id('corner.off')) == 1 or len({t.id for t in h.by_id('corner.off')}) == 1


def test_a_shift_habit_that_costs_next_to_nothing_says_so(tmp_path):
    from tests.test_coach import fabia_history, shares
    h = fabia_history(tmp_path, 'tarmac')
    h.session(shares(2, cut=0.4, cost=0.01))
    [tip] = h.tips()
    assert tip.text == ('2→3 with the sequential on tarmac: 0 % of your changes up come early and 40 % sit on the cut; '
                        'that costs next to nothing a stage, too little to coach.')


def test_a_learning_run_has_its_spin_described_and_a_run_that_beat_the_reference_is_judged_against_the_run_it_beat(
        tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(corners_with(), events=[event('spin', 580.0, 620.0, value=230.0)], run_class='learning')
    [note] = h.by_id('corner.spin')
    assert note.kind == 'note' and note.text == 'On Test Stage, you spun in the 3 right at 0.6 km.'
    h = Stage(tmp_path / 'u.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'brake_d': 75.0, 'min_speed': 10.0 - 7 * KMH, 'loss': (0.4, 0.2)},
                         third={'brake_d': 30.0, 'exit_speed': 20.0 + 5 * KMH, 'loss': (-0.5, -1.4)}),
            result_time=197.0)
    [tip] = [t for t in h.tips(show_all=True) if t.id.startswith('corner.section')]
    assert 's behind your previous best clean run here (Test car, 200.0 s on ' in tip.text


def test_a_section_is_named_net_of_the_gain_right_before_it(tmp_path):
    """Fast through one corner and too careful for the next is one trade, not a 1.1 s fault."""
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'loss': (-0.5, -0.4)}, third={'loss': (0.6, 0.5)}), result_time=200.2)
    [tip] = [t for t in h.tips() if t.id.startswith('corner.section')]
    assert ' 0.2 s behind ' in tip.text and abs(tip.cost - 0.2) < 1e-9
    assert 'Net of the 0.9 s the section before it gained.' in tip.evidence
    h = Stage(tmp_path / 'u.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'loss': (-0.8, -0.4)}, third={'loss': (0.6, 0.5)}), result_time=199.9)
    assert not [t for t in h.tips() if t.id.startswith('corner.section')]


def test_the_last_section_of_a_run_that_did_not_finish_is_never_named(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'loss': (0.4, 0.4)}, third={'loss': (7.0, 7.0)}), finished=0, run_class='partial',
            course=1500.0)
    named = [t.id for t in h.tips(show_all=True) if t.id.startswith('corner.section')]
    assert named == ['corner.section:{}:600'.format(STAGE_KEY)]
