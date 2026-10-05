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


def test_a_late_release_does_not_cut_a_newer_display_short(tmp_path):
    leds = _fake_leds(tmp_path)
    leds.show_level(0.2, seconds=5)
    stale = leds._generation
    leds.show_level(1.0, seconds=5)
    leds._release(stale)                       # the first timer, already past cancel()
    assert _lit(leds) == (True,) * 5
    leds.set_count(0)
    assert _lit(leds) == (True,) * 5           # still held for the newer display
    leds._release(leds._generation)
    assert _lit(leds) == (False,) * 5


def test_switch_state_display(tmp_path):
    leds = _fake_leds(tmp_path)
    leds.show_state(True, seconds=5)
    assert _lit(leds) == (True,) * 5
    leds.show_state(False, seconds=5)
    assert _lit(leds) == (True, False, False, False, True)
    leds._release(leds._generation)


def test_profile_switching_is_app_wide():
    assert set(hotkeys.GLOBAL_ACTIONS) <= set(hotkeys.BY_ID)
    assert all(hotkeys.BY_ID[a].kind == 'profile' for a in hotkeys.GLOBAL_ACTIONS)


class FakeTimers:
    """add_timeout/remove_timeout that run on demand instead of on a main loop."""

    def __init__(self):
        self.pending = {}
        self.next = 1

    def add(self, ms, callback):
        handle = self.next
        self.next += 1
        self.pending[handle] = (ms, callback)
        return handle

    def remove(self, handle):
        del self.pending[handle]

    def fire(self):
        """Fire the one pending timer; returns the delay it was set for."""
        (handle, (ms, callback)), = self.pending.items()
        del self.pending[handle]
        callback()
        return ms


def _repeater(results):
    timers = FakeTimers()
    steps = []

    def step(action_id):
        steps.append(action_id)
        return results.pop(0) if results else True
    return hotkeys.Repeater(step, timers.add, timers.remove), timers, steps


def test_hold_repeats_after_400_then_every_120():
    repeater, timers, steps = _repeater([])
    repeater.press('wheel:btn:300', 'ff_gain_up')
    assert [ms for ms, _cb in timers.pending.values()] == [400]
    assert timers.fire() == 400
    assert timers.fire() == 120
    assert timers.fire() == 120
    assert steps == ['ff_gain_up'] * 3


def test_release_stops_repeating_and_other_buttons_do_not():
    repeater, timers, steps = _repeater([])
    repeater.press('wheel:btn:300', 'ff_gain_up')
    repeater.release('wheel:btn:301')
    assert len(timers.pending) == 1
    repeater.release('wheel:btn:300')
    assert not timers.pending and not steps


def test_limit_stops_repeating():
    repeater, timers, steps = _repeater([True, False])
    repeater.press('key:range_up', 'range_up')
    timers.fire()
    timers.fire()
    assert not timers.pending and len(steps) == 2


def test_toggles_and_profile_switches_never_repeat():
    repeater, timers, steps = _repeater([])
    for action_id in ('ffb_toggle', 'profile_next', 'nonsense'):
        repeater.press('wheel:btn:300', action_id)
        assert not timers.pending


def test_a_new_press_and_stop_replace_the_old_hold():
    repeater, timers, steps = _repeater([])
    repeater.press('wheel:btn:300', 'ff_gain_up')
    repeater.press('wheel:btn:301', 'ff_gain_down')
    assert len(timers.pending) == 1
    timers.fire()
    assert steps == ['ff_gain_down']
    repeater.stop()
    assert not timers.pending


def test_step_stopping_the_repeater_does_not_reschedule():
    timers = FakeTimers()
    holder = []
    repeater = hotkeys.Repeater(lambda a: holder[0].stop() or True, timers.add, timers.remove)
    holder.append(repeater)
    repeater.press('x', 'ff_gain_up')
    timers.fire()
    assert not timers.pending


# -- Gui wiring, driven on a stand-in for self --

class FakeGui:
    """The parts of Gui the hotkey gating touches."""
    def __init__(self, results=None):
        from types import SimpleNamespace
        self.timers = FakeTimers()
        self.ran = []
        self.results = results if results is not None else []
        self.hotkey_repeater = hotkeys.Repeater(lambda a: self.step(a), self.timers.add, self.timers.remove)
        self.keyboard_release_seen = False
        self.hotkey_capture = None
        self.device = SimpleNamespace(input_device=None)
        self.hat_held = {}
        self.posted = []
        self.ui = SimpleNamespace(safe_call=lambda cb, *a: self.posted.append((cb, a)))

    def step(self, action_id):
        from oversteer.gui import Gui
        return Gui._hotkey_repeat_step(self, action_id)

    def _hotkeys_suppressed(self):
        return False

    def run_hotkey(self, action_id):
        self.ran.append(action_id)
        return self.results.pop(0) if self.results else True

    def _wheel_key_down(self, code):
        from oversteer.gui import Gui
        return Gui._wheel_key_down(self, code)


def test_keyboard_hold_repeats_only_after_a_release_was_seen():
    from oversteer.gui import Gui
    gui = FakeGui()
    Gui.on_keyboard_hotkey(gui, 'ff_gain_up')
    assert gui.hotkey_repeater.holder is None and not gui.timers.pending   # first hold: no repeat
    Gui.on_keyboard_hotkey_released(gui, 'ff_gain_up')
    assert gui.keyboard_release_seen
    Gui.on_keyboard_hotkey(gui, 'ff_gain_up')
    assert gui.hotkey_repeater.holder == 'key:ff_gain_up'
    assert gui.timers.fire() == hotkeys.REPEAT_FIRST_MS
    Gui.on_keyboard_hotkey_released(gui, 'ff_gain_up')
    assert gui.hotkey_repeater.holder is None


def test_wheel_repeat_stops_when_the_key_is_no_longer_down():
    from types import SimpleNamespace
    from oversteer.gui import Gui
    gui = FakeGui()
    keys = [288]
    gui.device.input_device = SimpleNamespace(active_keys=lambda: list(keys))
    gui.hotkey_repeater.press('wheel:btn:288', 'ff_gain_up')
    gui.timers.fire()
    assert gui.ran == ['ff_gain_up'] and gui.hotkey_repeater.holder == 'wheel:btn:288'
    keys.clear()                                   # the release was lost
    gui.timers.fire()
    assert gui.ran == ['ff_gain_up'] and gui.hotkey_repeater.holder is None
    # a device that can't say keeps repeating
    gui.device.input_device = SimpleNamespace()
    gui.hotkey_repeater.press('wheel:btn:288', 'ff_gain_up')
    gui.timers.fire()
    assert gui.ran == ['ff_gain_up', 'ff_gain_up']


def test_syn_dropped_clears_what_is_held():
    from oversteer.gui import Gui
    gui = FakeGui()
    gui.hat_held[ecodes.ABS_HAT0X] = 1
    gui.hotkey_repeater.press('wheel:btn:288', 'ff_gain_up')
    Gui._wheel_input_lost(gui)
    assert gui.hat_held == {}
    (callback, args), = gui.posted
    callback(*args)
    assert gui.hotkey_repeater.holder is None
