"""Tuning advice from the runs on the car's current setup
(docs/telemetry-coaching.md, section 10)."""
from oversteer import tuning
from tests.test_coach import History, T0


def setup(tmp_path, name='t.db'):
    h = History(tmp_path / name)
    h.store.begin()
    tune = h.store.add_tune(h.car, T0, {1: 480.0, 2: 330.0, 3: 250.0}, 'first')
    h.store.commit()
    return h, tune


def notes(h, surface=None):
    return tuning.advice(h.store, h.profile, h.car, surface)


def test_advice_waits_for_three_runs(tmp_path):
    h, tune = setup(tmp_path)
    for _ in range(2):
        h.session([{'name': 'limiter.top', 'value': 3.0, 'count': 1}], tune=tune)
    [note] = notes(h)
    assert note.id == 'collecting' and note.text == 'Tuning advice waits for 3 runs on this setup (2 so far).'


def test_final_drive(tmp_path):
    h, tune = setup(tmp_path)
    for value in (2.0, 3.0, 2.5):
        h.session([{'name': 'limiter.top', 'value': value, 'count': 1}], tune=tune)
    [note] = notes(h)
    assert note.kind == 'setup' and note.levers == ['final drive', 'top gear']
    assert note.text == ('On stage eawrc:4:12 you sat on the limiter in top gear for 2.5 s a run (3 of 3 runs): lift '
                         'before the limiter in top, or lengthen the final drive or top gear if the setup allows.')
    # One long straight on one run is normal
    h2, tune2 = setup(tmp_path, 'b.db')
    for value in (2.0, 0.0, 0.2, 0.0):
        h2.session([{'name': 'limiter.top', 'value': value, 'count': 1}], tune=tune2)
    assert notes(h2) == []
    h3, tune3 = setup(tmp_path, 'c.db')
    for _ in range(3):
        h3.session([{'name': 'top.share', 'value': 0.2, 'count': 1}, {'name': 'top.peak', 'value': 0.8, 'count': 1}],
                   tune=tune3)
    [note] = notes(h3)
    assert note.id == 'final-drive.long:eawrc:4:12' and 'did not get past 80 % of the limiter' in note.text


def test_only_the_current_tune_counts(tmp_path):
    h, tune = setup(tmp_path)
    for _ in range(3):
        h.session([{'name': 'limiter.top', 'value': 3.0, 'count': 1}], tune=tune)
    h.store.begin()
    longer = h.store.add_tune(h.car, h.t + 10, {1: 440.0, 2: 300.0, 3: 230.0}, 'final-drive')
    h.store.commit()
    h.session([{'name': 'limiter.top', 'value': 0.0, 'count': 1}], tune=longer)
    assert [n.id for n in notes(h)] == ['collecting']


def test_a_long_gear_out_of_corners(tmp_path):
    h, tune = setup(tmp_path)
    for _ in range(3):
        h.session([{'name': 'exit.low', 'value': 0.75, 'count': 4, 'gear': 3},
                   {'name': 'exit.low', 'value': 0.1, 'count': 4, 'gear': 2}], tune=tune)
    [note] = notes(h)
    assert note.kind == 'driving' and note.text == ('Out of corners in 3rd the revs were below the power band and the '
                                                    'car was slow to pull on 75 % of 12 exits: use 2nd there, or '
                                                    'shorten 3rd if the setup allows.')


def test_a_long_gear_needs_more_on_loose_surfaces_and_in_front_wheel_drive(tmp_path):
    """A low exit is technique more often on gravel and in a FWD car: 70 % of 15 exits, not 50 % of 10."""
    for surface, runs, share, expected in (('tarmac', 3, 0.6, True), ('gravel', 3, 0.6, False),
                                           ('gravel', 4, 0.8, True), ('gravel', 3, 0.8, False),
                                           ('unknown', 3, 0.6, False)):
        h, tune = setup(tmp_path, '{}{}{}.db'.format(surface, runs, share))
        for _ in range(runs):
            h.session([{'name': 'exit.low', 'value': share, 'count': 4, 'gear': 3}], surface=surface, tune=tune)
        assert bool(notes(h, surface if surface != 'unknown' else None)) == expected, (surface, runs, share)
    h, tune = setup(tmp_path, 'fwd.db')
    h.store.save_model('rally', 'eawrc/17', 'eawrc', 'Test car', {'key': 'eawrc/17', 'ratios': {}, 'drivetrain': 'fwd'},
                       drivetrain='fwd', create=True)
    for _ in range(3):
        h.session([{'name': 'exit.low', 'value': 0.6, 'count': 4, 'gear': 3}], surface='tarmac', tune=tune)
    assert notes(h, 'tarmac') == []                              # FWD on tarmac: the 70 % threshold too


def test_the_long_gear_is_the_right_one_where_the_gear_below_would_have_spun(tmp_path):
    h, tune = setup(tmp_path)
    for _ in range(3):
        h.session([{'name': 'exit.low', 'value': 0.8, 'count': 4, 'gear': 3},
                   {'name': 'exit.spin', 'value': 0.6, 'count': 4, 'gear': 2}], tune=tune)
    assert notes(h) == []                                        # the exits in 2nd spun: use 3rd, it is right
    h, tune = setup(tmp_path, 'b.db')
    for _ in range(3):
        h.session([{'name': 'exit.low', 'value': 0.8, 'count': 4, 'gear': 3},
                   {'name': 'exit.spin', 'value': 0.0, 'count': 4, 'gear': 2}], tune=tune)
    assert [n.id for n in notes(h)] == ['gear.long:3']
    # 1st is never recommended, and 2nd only where 1st is measured not to spin
    h, tune = setup(tmp_path, 'c.db')
    for _ in range(3):
        h.session([{'name': 'exit.low', 'value': 0.9, 'count': 4, 'gear': 1},
                   {'name': 'exit.low', 'value': 0.9, 'count': 4, 'gear': 2}], tune=tune)
    assert notes(h) == []
    h, tune = setup(tmp_path, 'd.db')
    for _ in range(3):
        h.session([{'name': 'exit.low', 'value': 0.9, 'count': 4, 'gear': 2},
                   {'name': 'exit.spin', 'value': 0.0, 'count': 4, 'gear': 1}], tune=tune)
    assert [n.id for n in notes(h)] == ['gear.long:2']


def test_balance_is_fitted_to_the_driver(tmp_path):
    """Sliding on tarmac in a car that oversteers: the setup. Sliding a
    balanced car: the driving. Sliding on gravel: nothing, it is how it
    is driven."""
    for surface, k, expected in (('tarmac', -0.08, 'balance.oversteer'), ('tarmac', 0.0, 'balance.driver'),
                                 ('gravel', -0.08, None)):
        h, tune = setup(tmp_path, '{}{}.db'.format(surface, k))
        for _ in range(3):
            h.session([{'name': 'counter_steer', 'value': 0.4, 'count': 10},
                       {'name': 'balance.gradient', 'value': k, 'count': 100}], surface=surface, tune=tune)
        found = [n.id for n in notes(h, surface)]
        assert found == ([expected] if expected else []), (surface, k)


def test_balance_against_the_setup_before(tmp_path):
    h, tune = setup(tmp_path)
    for _ in range(3):
        h.session([{'name': 'balance.gradient', 'value': 0.0, 'count': 100}], tune=tune)
    h.store.begin()
    softer = h.store.add_tune(h.car, h.t + 10, {1: 480.0, 2: 330.0, 3: 250.0}, 'user')
    h.store.commit()
    for _ in range(3):
        h.session([{'name': 'balance.gradient', 'value': -0.05, 'count': 100}], tune=softer)
    [note] = notes(h)
    assert note.id == 'balance.change:tarmac'
    assert note.text == ('Since the last setup change the car asks for less lock as the grip used rises on tarmac: '
                         'more oversteer than before.')


def test_balance_is_never_pooled_across_surfaces_and_the_car_is_not_called_understeering(tmp_path):
    """A gradient of +0.1 on gravel and -0.1 on tarmac averages to a balanced car that neither is; and the
    absolute "the car understeers" is not said: only a change between setups on one surface is."""
    h, tune = setup(tmp_path)
    for _ in range(3):
        h.session([{'name': 'balance.gradient', 'value': 0.1, 'count': 400}], surface='gravel', tune=tune)
        h.session([{'name': 'balance.gradient', 'value': -0.1, 'count': 400}], surface='tarmac', tune=tune)
    assert notes(h) == []
    h2, tune2 = setup(tmp_path, 'b.db')
    for _ in range(3):
        h2.session([{'name': 'balance.gradient', 'value': 0.2, 'count': 400}], tune=tune2)
    assert notes(h2) == []                                       # understeer, from one setup: nothing absolute
    h.store.begin()
    other = h.store.add_tune(h.car, h.t + 10, {1: 480.0, 2: 330.0, 3: 250.0}, 'user')
    h.store.commit()
    for _ in range(3):
        h.session([{'name': 'balance.gradient', 'value': 0.1, 'count': 400}], surface='gravel', tune=other)
        h.session([{'name': 'balance.gradient', 'value': 0.1, 'count': 400}], surface='tarmac', tune=other)
    assert [n.id for n in notes(h)] == ['balance.change:tarmac']  # tarmac moved by 0.2, gravel did not


def test_tune_summary_says_what_the_game_does_not_send(tmp_path):
    h, tune = setup(tmp_path)
    summary = tuning.tune_summary(h.store, h.car, 'eawrc')
    assert summary['ratios'] == {1: 480.0, 2: 330.0, 3: 250.0} and summary['change'] == 'first'
    assert summary['ride_height_f'] == tuning.NOT_SENT and summary['brake_bias'] == tuning.NOT_SENT
    assert tuning.tune_summary(h.store, h.car, 'acpmf')['ride_height_f'] == 'not measured yet'
