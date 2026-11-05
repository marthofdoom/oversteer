"""The estimated force-reversal delay (oversteer/ffb_delay.py): each game, the filters, the gear crossing against the
research's sweep and the driver stage's mirror, the soft limit, the "50 ms" level, the GUI's view."""
import types

import pytest

from oversteer import ffb_delay as fd, ffb_engine as fe, ffb_forza, ffb_gear as fg

OFF = {'gear_guard': False, 'gear_level': 61, 'rbr_cut_hz': 120}
GEAR = {'drive': 'gear', 'fit_latency_ms': None}
DEFAULT = {'gear_guard': True, 'gear_level': fg.OVERALL_GEAR_GUARD_LEVEL, 'rbr_cut_hz': 30}


@pytest.mark.parametrize('game,expect', [('rbr', 2.0 + 5.0), ('acr', 3.0 + 5.0), ('beamng', 3.0 + 5.0),
                                         ('fh6', 8.0 + 0.225 / 24.0 * 1e3 + 5.0)])
def test_each_game_without_the_guard(game, expect):
    assert fd.estimated_total_ms(game, OFF, GEAR) == pytest.approx(expect)


@pytest.mark.parametrize('name,key', [('rbr', 'rbr'), ('acr', 'acr'), ('acc', 'acr'), ('forza', 'fh6'), ('forza-rack', 'fh6'),
                                      ('fh6', 'fh6'), ('beamng', 'beamng'), ('beamng-ffb', 'beamng'), (None, 'acr')])
def test_game_keys(name, key):
    assert fd.game_key(name) == key


def test_rbr_cut_group_delay_only_on_rbr_and_off_at_the_top():
    cut = dict(OFF, rbr_cut_hz=30)
    assert fd.estimated_total_ms('rbr', cut, GEAR) == pytest.approx(7.0 + 7.5)
    assert fd.estimated_total_ms('acr', cut, GEAR) == fd.estimated_total_ms('acr', OFF, GEAR)
    assert fd.RBR_CUT_OFF == fe.RBR_CUT_OFF
    assert fd.estimated_total_ms('rbr', dict(OFF, rbr_cut_hz=119), GEAR) > fd.estimated_total_ms('rbr', OFF, GEAR)


def test_fh6_filter_follows_the_packet_rate_within_the_filters_corners():
    f = ffb_forza.OutputFilter
    assert fd.fh_corner(60.0) == 24.0 and fd.fh_corner(30.0) == f.FC_MIN and fd.fh_corner(500.0) == f.FC_MAX
    assert fd.estimated_total_ms('fh6', dict(OFF, fh_rate=120.0), GEAR) < fd.estimated_total_ms('fh6', OFF, GEAR)


def test_wheel_latency_from_the_fit_when_known():
    assert fd.estimated_total_ms('acr', OFF, {'drive': 'gear', 'fit_latency_ms': 9.0}) == pytest.approx(12.0)


@pytest.mark.parametrize('rate,flip,late', [(3.3, 6, 22), (2.0, 6, 40), (1.0, 6, 90), (0.5, 6, 190), (3.3, 15, 26), (2.0, 15, 44),
                                            (1.0, 15, 94), (3.3, 25, 30)])
def test_the_crossing_matches_the_research_sweep(rate, flip, late):
    level = fg.RATE * fg.OVERALL_GEAR_GUARD_LEVEL / rate
    if level > 100.0:                            # past the slider's end, the formula itself
        values = (fg.PRELOAD, flip / 100.0, fg.BAND, rate, fg.REJOIN)
        original = fg.params
        fg.params = lambda *a: values
        try:
            got = fg.reversal_late_ms(50)
        finally:
            fg.params = original
    else:
        got = fg.reversal_late_ms(level, flip_pct=flip)
    assert got == pytest.approx(late, abs=2.0)


def test_the_flip_point_moves_the_estimate_and_the_mark():
    low, high = dict(DEFAULT, gear_flip=3), dict(DEFAULT, gear_flip=30)
    assert fd.estimated_total_ms('rbr', low, GEAR) < fd.estimated_total_ms('rbr', DEFAULT, GEAR) < fd.estimated_total_ms('rbr', high, GEAR)
    assert fd.soft_level('rbr', low, GEAR) > fd.soft_level('rbr', DEFAULT, GEAR) > fd.soft_level('rbr', high, GEAR)


@pytest.mark.parametrize('level', [100, 81, 61, 40])
def test_the_estimate_matches_the_stage_itself(level):
    dt = 0.002
    stage = fg.Crossing(fg.params(level))
    n = int(fg.REF_RAMP_S / dt)
    force = [0.5] * 100 + [0.5 - (i + 1) / n for i in range(n)] + [-0.5] * 400
    out = [stage.step(f, dt) for f in force]
    at = next(i for i in range(100, len(out)) if out[i] < -0.45)
    assert (at - (100 + n - 1)) * dt * 1e3 == pytest.approx(fg.reversal_late_ms(level), abs=3.0)


@pytest.mark.parametrize('settings,wheel', [(dict(DEFAULT, gear_guard=False), GEAR), (DEFAULT, {'drive': 'belt'}),
                                            (DEFAULT, {'drive': 'direct'}), (dict(DEFAULT, gear_level=0), GEAR)])
def test_no_crossing_delay(settings, wheel):
    assert fd.gear_ms(settings, wheel) == 0.0


def test_defaults_are_under_the_limit_for_every_game():
    for game in fd.SOURCE_MS:
        assert fd.estimated_total_ms(game, DEFAULT, GEAR) < fd.DELAY_SOFT_MS


def test_the_threshold_is_strict():
    assert fd.DELAY_SOFT_MS == 50
    assert not fd.over_soft(50.0) and fd.over_soft(50.01)


def test_the_strongest_end_reads_over_on_rbr():
    assert fd.over_soft(fd.estimated_total_ms('rbr', dict(DEFAULT, gear_level=100), GEAR))


def test_the_50_ms_level_on_rbr_with_the_defaults():
    level = fd.soft_level('rbr', DEFAULT, GEAR)
    assert fd.estimated_total_ms('rbr', dict(DEFAULT, gear_level=level), GEAR) == pytest.approx(50.0, abs=0.01)
    assert fg.params(level)[3] == pytest.approx(2.5, abs=0.1)        # flip 15 %
    # at the flip of 6 % it was asked for at first, near 2.3 FS/s
    assert fg.params(fd.soft_level('rbr', dict(DEFAULT, gear_flip=6), GEAR))[3] == pytest.approx(2.3, abs=0.1)


@pytest.mark.parametrize('game', sorted(fd.SOURCE_MS))
def test_the_50_ms_level_separates_each_game(game):
    level = fd.soft_level(game, DEFAULT, GEAR)
    if level is None:
        assert fd.estimated_total_ms(game, dict(DEFAULT, gear_level=100), GEAR) <= 50.0
    else:
        assert 0.0 < level < 100.0
        assert fd.estimated_total_ms(game, dict(DEFAULT, gear_level=level - 0.5), GEAR) < 50.0
        assert fd.estimated_total_ms(game, dict(DEFAULT, gear_level=level + 0.5), GEAR) > 50.0


def test_a_stronger_cut_moves_the_mark_down():
    assert fd.soft_level('rbr', dict(DEFAULT, rbr_cut_hz=15), GEAR) < fd.soft_level('rbr', DEFAULT, GEAR)


def test_no_mark_on_belt_or_direct():
    assert fd.soft_level('rbr', DEFAULT, {'drive': 'belt'}) is None


def test_the_guards_switch_does_not_hide_the_mark():
    assert fd.soft_level('rbr', dict(DEFAULT, gear_guard=False), GEAR) == fd.soft_level('rbr', DEFAULT, GEAR)


def test_it_never_changes_a_setting():
    settings = dict(DEFAULT, gear_level=100)
    before = dict(settings)
    fd.estimated_total_ms('rbr', settings, GEAR)
    fd.soft_level('rbr', settings, GEAR)
    assert settings == before


def test_the_guis_view():
    from oversteer.gui import Gui
    model = types.SimpleNamespace(
        get_countersteer_wheel_drive=lambda: 'gear', get_countersteer_gear_guard=lambda: True,
        get_countersteer_gear_guard_level=lambda: 100, get_countersteer_gear_flip=lambda: 15, get_countersteer_rbr_cut=lambda: 30, data={})
    gui = types.SimpleNamespace(model=model, device=None, ffb_engine=types.SimpleNamespace(status=types.SimpleNamespace(game='rbr', rate=300.0)),
                                _wheel_entry=lambda: None)
    view = Gui.countersteer_delay(gui)
    assert view['gear']['over'] is True and view['gear']['text'].startswith('about ') and view['gear']['text'].endswith(' ms total')
    assert 75 < view['gear']['mark'] < 90
    gui.ffb_engine.status = types.SimpleNamespace(game=None, rate=0.0)       # between games: the last one seen
    assert Gui.countersteer_delay(gui)['gear']['mark'] == view['gear']['mark']
    model.get_countersteer_gear_guard_level = lambda: 61
    assert Gui.countersteer_delay(gui)['rbr_cut']['over'] is False


def test_take_over_line_mentions_it():
    engine = fe.FfbEngine.__new__(fe.FfbEngine)
    engine._gear_on, engine._gear_level, engine._gear_drive, engine._gear_flip, engine._dyn, engine._mode = True, 100.0, 'gear', 15.0, None, 'rbr'
    engine.rbr_rack = types.SimpleNamespace(cut_hz=30.0)
    engine._verdict = types.SimpleNamespace(rate=300.0)
    engine._wheel_req = None
    note = engine._gear_note()
    assert '100 %' in note and 'ms total' in note and 'over the 50 ms soft limit' in note
