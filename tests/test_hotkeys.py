import os
import time
from evdev import ecodes
from oversteer import hotkeys
from oversteer.telemetry import RevLeds


def test_bindings_round_trip_in_catalog_order():
    text = 'ff_gain_down=btn:705,rev_shift_up=hat:{}:-1'.format(ecodes.ABS_HAT0Y)
    bindings = hotkeys.parse(text)
    assert bindings == {'ff_gain_down': 'btn:705', 'rev_shift_up': 'hat:17:-1'}
    # rev_shift_up comes first in the catalog, so the saved text is stable
    assert hotkeys.serialize(bindings) == 'rev_shift_up=hat:17:-1,ff_gain_down=btn:705'
    assert hotkeys.parse(hotkeys.serialize(bindings)) == bindings


def test_parse_drops_what_it_does_not_know():
    text = 'future_action=btn:300, ff_gain_up=btn:nope,spring_up=hat:17:5,damper_up=btn:0,,garbage,ffb_toggle=btn:291'
    assert hotkeys.parse(text) == {'ffb_toggle': 'btn:291'}
    assert hotkeys.parse(None) == {}
    assert hotkeys.parse('') == {}


def test_input_names_match_the_controls_tab():
    assert hotkeys.input_name('btn:288') == 'Button 0'
    assert hotkeys.input_name('btn:714') == 'Button 26'          # T500 sequential up
    assert hotkeys.input_name(hotkeys.hat_input(ecodes.ABS_HAT0X, 1)) == 'D-pad right'
    assert hotkeys.input_name('btn:{}'.format(ecodes.BTN_SOUTH)) == 'Button 0'
    assert hotkeys.input_name('btn:{}'.format(ecodes.KEY_A)) == 'KEY_A'


def test_catalog_ids_are_unique_and_described():
    ids = [a.id for a in hotkeys.ACTIONS]
    assert len(ids) == len(set(ids))
    for action in hotkeys.ACTIONS:
        assert ':' in action.description()
        if action.kind in ('step', 'range', 'shift', 'profile'):
            assert action.delta


def _fake_leds(tmp_path, count=5):
    for i in range(count):
        d = tmp_path / 'leds' / 'dev::RPM{}'.format(i + 1)
        d.mkdir(parents=True)
        (d / 'brightness').write_text('0')
    return RevLeds(str(tmp_path))


def _lit(leds):
    return tuple(open(p).read() == '1' for p in leds.paths)


def test_level_display_holds_off_telemetry_then_gives_the_leds_back(tmp_path):
    leds = _fake_leds(tmp_path)
    leds.set_count(2)
    assert _lit(leds) == (True, True, False, False, False)
    leds.show_level(0.8, seconds=0.2)
    assert _lit(leds) == (True, True, True, True, False)
    leds.set_count(1)                          # telemetry during the display: not shown yet
    assert _lit(leds) == (True, True, True, True, False)
    time.sleep(0.35)
    assert _lit(leds) == (True, False, False, False, False)   # what telemetry wants by now


def test_level_display_without_telemetry_goes_dark(tmp_path):
    leds = _fake_leds(tmp_path)
    leds.show_level(0.0, seconds=0.1)
    assert _lit(leds) == (True, False, False, False, False)    # lowest still shows one LED
    time.sleep(0.25)
    assert _lit(leds) == (False,) * 5
