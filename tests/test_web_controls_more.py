"""Web controls, second batch: address order, a mode and its shift point in one batch, the try-effect path,
timeouts, and the lockout's keys and size."""
import threading

import pytest

from oversteer import telemetry_web, web_controls
from oversteer.telemetry_web import Lockout
from tests.test_web_controls import rig, set_  # noqa: F401  (the fixture)


def test_addresses_are_offered_route_first_then_lan_then_the_rest():
    rank = telemetry_web._address_rank
    found = ['172.17.0.1', '100.127.1.2', '192.168.1.20', '192.168.122.1', '10.0.0.5', '8.8.4.4']
    assert sorted(found, key=lambda a: rank(a, '192.168.1.20')) == [
        '192.168.1.20', '10.0.0.5', '8.8.4.4', '100.127.1.2', '172.17.0.1', '192.168.122.1']
    assert sorted(found, key=lambda a: rank(a, '10.0.0.5'))[0] == '10.0.0.5'


def test_rev_mode_and_shift_in_one_batch(rig):  # noqa: F811
    assert set_(rig, rev_leds_shift=7500, rev_leds_mode='rpm')[0] == 200      # checked as rpm, written after the mode
    assert rig.model.get_rev_leds_shift() == 7500
    assert set_(rig, rev_leds_shift=90, rev_leds_mode='percent')[0] == 200
    assert set_(rig, rev_leds_shift=7500, rev_leds_mode='percent')[0] == 400
    assert set_(rig, rev_leds_shift=90, rev_leds_mode='auto')[0] == 409


def test_try_effect_goes_to_the_device_without_a_dialog(rig):  # noqa: F811
    rig.device.play_demo = lambda kind, on_error=None: False
    assert rig.post({'do': 'try_effect', 'arg': 'spring'})[0] == 409
    assert not [c for c in rig.ui.calls if c[0] == 'info_dialog']


def test_a_change_that_never_finishes_is_a_timeout():
    gate = threading.Event()

    def run_in_thread(fn):
        threading.Thread(target=fn, daemon=True).start()
    with pytest.raises(TimeoutError):
        web_controls.call_on_main(lambda: gate.wait(2), run_in_thread, 0.1)      # started, not done
    gate.set()


def test_lockout_counts_an_ipv6_network_and_stays_small():
    now = [0.0]
    lock = Lockout(limit=2, window=10, clock=lambda: now[0])
    lock.fail('2001:db8:1:2::1')
    lock.fail('2001:db8:1:2:ffff::9')
    assert lock.blocked('2001:db8:1:2::77') and not lock.blocked('2001:db8:1:3::1')
    for i in range(1000):
        lock.fail('10.1.{}.{}'.format(i // 250, i % 250))
    assert len(lock.failures) <= Lockout.MAX_TRACKED
