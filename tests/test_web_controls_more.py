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


def test_the_pairing_row_shows_when_offered_and_stays_hidden_when_not():
    gi = __import__('gi')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    from oversteer.gtk_ui import GtkUi
    ui = GtkUi.__new__(GtkUi)
    row = Gtk.Box()
    row.add(Gtk.Label(label='x'))
    row.set_no_show_all(True)
    win = Gtk.OffscreenWindow()
    win.add(row)
    win.show_all()
    ui.web_pair_row, ui.web_pair_text, ui.web_pair_qr, ui.web_pair_link = row, Gtk.Label(), Gtk.DrawingArea(), None
    GtkUi.set_web_pairing(ui, 'http://192.168.1.2:5301/?pair=abc', 'abc')
    assert row.get_visible()                       # show_all() alone skipped a no_show_all row
    GtkUi.set_web_pairing(ui, None, 'abc')
    win.show_all()
    assert not row.get_visible()


# -- Countersteer FFB on the FFB tab --

CS = dict(ext_force_support=True)


def cs_rig(tmp_path):
    from tests.fake_wheel import FakeDevice
    from tests.test_web_controls import Rig
    r = Rig(tmp_path, device=FakeDevice(**CS))
    return r


def test_countersteer_state(tmp_path):
    r = cs_rig(tmp_path)
    try:
        state = r.state()
        s = state['settings']
        assert s['countersteer_ffb'] == {'value': False, 'enabled': True}
        assert s['countersteer_strength']['min'] == 0 and s['countersteer_strength']['max'] == 130
        assert s['countersteer_knee']['min'] == 30 and s['countersteer_knee']['max'] == 60 and s['countersteer_drive_pull']['value'] is False
        assert s['countersteer_drive_pull_level']['value'] == 35 and 'countersteer_mapping' not in s
        assert s['countersteer_strength']['enabled'] is False            # the switch is off
        assert s['countersteer_texture']['max'] == 100 and 'countersteer_linearity' in s and 'countersteer_lightness' in s
        assert all(s['countersteer_texture_' + p]['value'] is True for p in ('road', 'impact', 'lock', 'engine'))
        cs = state['countersteer']
        assert set(cs) == {'light', 'text', 'car', 'wheel', 'calibration', 'sensitivity'}
        assert cs['light'] == 'off' and cs['text'] and 'caster trail 30 mm' in cs['car'] and cs['calibration']
        assert 'steering ratio 14.0' in cs['car'] and 'impedance scale 0.80' in cs['car'] and 'power steering' in cs['car']
        assert s['countersteer_column'] == {'value': True, 'enabled': False}
        assert s['countersteer_align'] == {'value': True, 'enabled': False}
        assert s['countersteer_friction_removal'] == {'value': False, 'enabled': False}
    finally:
        r.stop()


def test_the_steering_toggles_write_only_when_paired(tmp_path):
    r = cs_rig(tmp_path)
    try:
        body = {'set': {'countersteer_column': False, 'countersteer_align': False, 'countersteer_friction_removal': True}}
        assert r.post(body, token=None)[0] == 401
        assert r.post(body, headers={'Origin': 'http://evil.example'})[0] == 403
        m = r.model
        assert m.get_countersteer_dynamics() == (True, True, False)
        assert r.post(body)[0] == 200
        assert m.get_countersteer_dynamics() == (False, False, True)
        assert ('set_dynamics', False, False, True) in r.controller.ffb_engine.calls
        assert ('set_countersteer_toggles', False, False, True) in r.ui.calls
        assert r.post({'set': {'countersteer_column': 1}})[0] == 400
        assert r.post({'set': {'countersteer_ffb': True}})[0] == 200
        flags = r.state()['settings']
        assert flags['countersteer_column'] == {'value': False, 'enabled': True}
        assert flags['countersteer_friction_removal'] == {'value': True, 'enabled': True}
    finally:
        r.stop()


def test_countersteer_absent_without_driver_support(rig):  # noqa: F811
    state = rig.state()
    assert not [k for k in state['settings'] if k.startswith('countersteer')]
    assert set_(rig, countersteer_ffb=True)[0] == 409


def test_countersteer_writes_need_pairing_csrf_and_origin(tmp_path):
    r = cs_rig(tmp_path)
    try:
        body = {'set': {'countersteer_ffb': True}}
        assert r.post(body, token=None)[0] == 401
        assert r.post(body, token='wrong')[0] == 401
        assert r.post(body, headers={'Origin': 'http://evil.example'})[0] == 403
        assert r.post(body, headers={'Sec-Fetch-Site': 'cross-site'})[0] == 403
        assert r.model.get_countersteer_ffb() is False and not r.controller.ffb_engine.calls
    finally:
        r.stop()
    from tests.fake_wheel import FakeDevice
    from tests.test_web_controls import Rig
    (tmp_path / 'off').mkdir()
    off = Rig(tmp_path / 'off', device=FakeDevice(**CS), enabled=False)
    try:
        assert off.post({'set': {'countersteer_ffb': True}})[0] == 403
        assert off.model.get_countersteer_ffb() is False
    finally:
        off.stop()


def test_a_paired_countersteer_write_takes_the_gtk_path(tmp_path):
    r = cs_rig(tmp_path)
    try:
        status, body = r.post({'set': {'countersteer_ffb': True, 'countersteer_strength': 60,
                                       'countersteer_texture': 40, 'countersteer_texture_lock': False,
                                       'countersteer_linearity': 20, 'countersteer_lightness': 80}})
        assert status == 200
        m = r.model
        assert m.get_countersteer_ffb() and m.get_countersteer_strength() == 60 and m.get_countersteer_linearity() == 20
        assert m.get_countersteer_lightness() == 80
        level, parts = m.get_countersteer_texture()
        assert level == 0.4 and parts['lock'] is False and parts['road'] is True
        calls = [c[0] for c in r.controller.ffb_engine.calls]
        for name in ('set_enabled', 'set_texture', 'set_strength', 'set_linearity', 'set_lightness'):
            assert name in calls
        assert ('set_strength', 60) in r.controller.ffb_engine.calls
        ui = [c[0] for c in r.ui.calls]
        assert 'set_countersteer_ffb' in ui and 'set_countersteer_texture' in ui and 'set_countersteer_strength' in ui
        assert body['state']['settings']['countersteer_strength'] == {'value': 60, 'min': 0, 'max': 130, 'step': 1, 'enabled': True}

    finally:
        r.stop()


def test_countersteer_ranges_are_clamped_and_types_checked(tmp_path):
    r = cs_rig(tmp_path)
    try:
        assert r.post({'set': {'countersteer_strength': 3, 'countersteer_texture': 250,
                               'countersteer_linearity': -5}})[0] == 200
        assert r.model.get_countersteer_strength() == 3 and r.model.data['countersteer_texture'] == 100
        assert r.model.get_countersteer_linearity() == 0
        for bad in ('50', 50.5, True, None):
            assert r.post({'set': {'countersteer_strength': bad}})[0] == 400
        assert r.post({'set': {'countersteer_ffb': 1}})[0] == 400
        assert r.post({'set': {'countersteer_texture_road': 'yes'}})[0] == 400
    finally:
        r.stop()


def test_the_countersteer_page_builds_and_follows_the_toggles():
    gi = __import__('gi')
    gi.require_version('Gtk', '3.0')
    from types import SimpleNamespace
    from gi.repository import Gtk
    from oversteer.gtk_ui import GtkUi
    ui = GtkUi.__new__(GtkUi)
    ui.main_notebook = Gtk.Notebook()
    for label in ('Controls', 'Force Feedback', 'Tools'):
        ui.main_notebook.append_page(Gtk.Box(), Gtk.Label(label=label))
    sets = []
    ui.controller = SimpleNamespace(model=SimpleNamespace(
        set_countersteer_dynamics=lambda k, v: sets.append((k[len('countersteer_'):], v)),
        set_countersteer_ffb=lambda v: None))
    GtkUi._build_countersteer_page(ui)
    assert ui.main_notebook.get_n_pages() == 4
    assert ui.main_notebook.get_tab_label_text(ui.main_notebook.get_nth_page(2)) == 'Countersteer FFB'
    assert hasattr(ui, 'calibrate_button') and hasattr(ui, 'wheel_friction_comp')
    assert ui.wheel_friction_comp.get_adjustment().get_upper() == 50
    GtkUi.set_countersteer_toggles(ui, True, False, True)
    assert [s.get_active() for s in ui.countersteer_toggles.values()] == [True, False, True]
    ui.countersteer_toggles['align'].set_active(True)
    assert ('align', True) in sets
    GtkUi.set_countersteer_toggles(ui, None, None, None)           # a driver without replace mode: untouched
    assert ui.countersteer_toggles['align'].get_active()
