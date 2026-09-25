"""Functional tests for the proxy devices, using uinput-made fake sources.

Needs /dev/uinput access and permission to read the input nodes it creates
(as a normal user that means the udev uaccess ACL, which is the case on a
desktop session). Run with `python3 -m pytest tests/`.
"""

import os
import select
import time

import pytest
from evdev import InputDevice, UInput, AbsInfo, ecodes as e, ff, list_devices

from oversteer.proxy import ProxySpec, ProxyDevice, ProxyState
from oversteer.proxy import uinput_ff

pytestmark = pytest.mark.skipif(not os.access('/dev/uinput', os.W_OK), reason="needs /dev/uinput")

FAKE_VENDOR, FAKE_PRODUCT = 0x1234, 0x5678


def find_by_name(name, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for path in list_devices():
            dev = InputDevice(path)
            if dev.name == name:
                return dev
            dev.close()
        time.sleep(0.05)
    raise AssertionError("device {!r} did not appear".format(name))


def wait_state(proxy, state, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proxy.state == state:
            return
        time.sleep(0.02)
    raise AssertionError("proxy state {} (expected {})".format(proxy.state, state))


def read_events(dev, timeout=1.0):
    out = []
    t0 = time.time()
    while time.time() - t0 < timeout:
        r, _, _ = select.select([dev.fd], [], [], 0.05)
        if r:
            for ev in dev.read():
                if ev.type != e.EV_SYN:
                    out.append((ev.type, ev.code, ev.value))
    return out


@pytest.fixture
def fake_handbrake():
    caps = {e.EV_ABS: [(e.ABS_THROTTLE, AbsInfo(0, 0, 65535, 255, 4095, 0))], e.EV_KEY: [e.BTN_TRIGGER]}
    ui = UInput(caps, name='Fake ANNX', vendor=FAKE_VENDOR, product=FAKE_PRODUCT, version=1, phys='usb-fake/input0')
    time.sleep(0.2)
    yield ui
    ui.close()


def test_axis_mapping_and_identity(fake_handbrake):
    spec = ProxySpec.from_dict({
        'id': 'test-hb',
        'name': 'test handbrake',
        'identity': {'name': 'Proxied Handbrake', 'vendor': '0eb7', 'product': '00e5', 'version': '0001'},
        'sources': {'hb': {'match': {'vendor': '1234', 'product': '5678'}, 'grab': True}},
        'capabilities': {'keys': ['BTN_TRIGGER'], 'abs': {'ABS_Z': {'min': 0, 'max': 65535}}},
        'mappings': [{'source': 'hb', 'from': 'ABS_THROTTLE', 'to': 'ABS_Z', 'invert': True},
                     {'source': 'hb', 'from': 'BTN_TRIGGER', 'to': 'BTN_TRIGGER'}],
    })
    proxy = ProxyDevice(spec)
    proxy.start()
    try:
        wait_state(proxy, ProxyState.RUNNING)
        virt = find_by_name('Proxied Handbrake')
        assert (virt.info.vendor, virt.info.product) == (0x0eb7, 0x00e5)
        caps = virt.capabilities(absinfo=True)
        assert e.ABS_THROTTLE not in [c for c, _ in caps[e.EV_ABS]]
        assert [c for c, _ in caps[e.EV_ABS]] == [e.ABS_Z]

        # The virtual axis was seeded from the source's current value (0 -> inverted 65535)
        assert virt.absinfo(e.ABS_Z).value == 65535
        fake_handbrake.write(e.EV_ABS, e.ABS_THROTTLE, 30000)
        fake_handbrake.syn()
        fake_handbrake.write(e.EV_ABS, e.ABS_THROTTLE, 65535)
        fake_handbrake.syn()
        fake_handbrake.write(e.EV_KEY, e.BTN_TRIGGER, 1)
        fake_handbrake.syn()
        events = read_events(virt)
        assert (e.EV_ABS, e.ABS_Z, 35535) in events      # 30000 inverted
        assert (e.EV_ABS, e.ABS_Z, 0) in events          # 65535 inverted
        assert (e.EV_KEY, e.BTN_TRIGGER, 1) in events
        assert proxy.events_out >= 3
    finally:
        proxy.stop()
    assert proxy.state == ProxyState.STOPPED


def test_source_hotplug():
    spec = ProxySpec.from_dict({
        'id': 'test-hotplug', 'name': 'hotplug',
        'identity': {'name': 'Hotplug Proxy'},
        'sources': {'hb': {'match': {'vendor': '1234', 'product': '5678'}}},
        'capabilities': {'abs': {'ABS_Z': {'min': 0, 'max': 65535}}},
        'mappings': [{'from': 'ABS_THROTTLE', 'to': 'ABS_Z'}],
    })
    proxy = ProxyDevice(spec)
    proxy.start()
    try:
        wait_state(proxy, ProxyState.WAITING)
        caps = {e.EV_ABS: [(e.ABS_THROTTLE, AbsInfo(0, 0, 65535, 0, 0, 0))]}
        ui = UInput(caps, name='Late ANNX', vendor=FAKE_VENDOR, product=FAKE_PRODUCT, version=1, phys='usb-fake/input1')
        try:
            proxy.poke()
            wait_state(proxy, ProxyState.RUNNING)
        finally:
            ui.close()
        # the source vanished: the virtual device stays, state degrades
        t0 = time.time()
        while proxy.state != ProxyState.DEGRADED and time.time() - t0 < 3:
            time.sleep(0.05)
        assert proxy.state == ProxyState.DEGRADED
        assert proxy.devnode is not None
    finally:
        proxy.stop()


def test_ff_passthrough():
    """A fake FF-capable wheel receives the effects a game uploads to the proxy."""
    wheel = UInput({e.EV_ABS: [(e.ABS_X, AbsInfo(32768, 0, 65535, 0, 0, 0))],
                    e.EV_KEY: [e.BTN_TRIGGER],
                    e.EV_FF: [e.FF_CONSTANT, e.FF_SPRING, e.FF_GAIN]},
                   name='Fake Wheel', vendor=FAKE_VENDOR, product=0x9999, version=1, max_effects=8, phys='usb-fake/input2')
    time.sleep(0.2)
    spec = ProxySpec.from_dict({
        'id': 'test-ff', 'name': 'ff',
        'identity': {'name': 'Proxied Wheel', 'vendor': '046d', 'product': 'c24f'},
        'sources': {'wheel': {'match': {'vendor': '1234', 'product': '9999'}}},
        'passthrough': 'wheel',
        'ff': {'source': 'wheel'},
    })
    proxy = ProxyDevice(spec)
    proxy.start()
    uploads = []
    try:
        wait_state(proxy, ProxyState.RUNNING)
        virt = find_by_name('Proxied Wheel')
        assert e.FF_CONSTANT in virt.capabilities()[e.EV_FF]

        # Service the fake wheel's uinput FF requests (uploads and erases) while the test runs
        erases = []
        stop = {'now': False}

        def service_wheel():
            while not stop['now']:
                try:
                    r, _, _ = select.select([wheel.fd], [], [], 0.05)
                except (OSError, ValueError):
                    return
                if not r:
                    continue
                for ev in wheel.read():
                    if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                        up = uinput_ff.begin_upload(wheel.fd, ev.value)
                        uploads.append((up.effect.type, up.effect.u.ff_constant_effect.level))
                        up.retval = 0
                        uinput_ff.end_upload(wheel.fd, up)
                    elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                        er = uinput_ff.begin_erase(wheel.fd, ev.value)
                        erases.append(er.effect_id)
                        er.retval = 0
                        uinput_ff.end_erase(wheel.fd, er)

        import threading
        t = threading.Thread(target=service_wheel, daemon=True)
        t.start()
        effect = ff.Effect(e.FF_CONSTANT, -1, 0x4000, ff.Trigger(0, 0), ff.Replay(100, 0),
                           ff.EffectType(ff_constant_effect=ff.Constant(level=1234)))
        eid = uinput_ff.upload_effect(virt.fd, effect)      # fcntl.ioctl releases the GIL
        assert uploads == [(e.FF_CONSTANT, 1234)]
        real_id = proxy.ff_effects.get(eid)
        assert real_id is not None
        uinput_ff.erase_effect(virt.fd, eid)
        time.sleep(0.2)
        assert eid not in proxy.ff_effects
        assert erases == [real_id]
        stop['now'] = True
        t.join(1.0)
    finally:
        proxy.stop()
        wheel.close()


def test_key_state_seeded_and_released_on_detach():
    """A shifter already in gear shows the gear on attach; unplugging it releases the gear."""
    caps = {e.EV_KEY: [e.BTN_TRIGGER, e.BTN_THUMB]}
    ui = UInput(caps, name='Fake Shifter', vendor=FAKE_VENDOR, product=0x7777, version=1, phys='usb-fake/input3')
    time.sleep(0.2)
    ui.write(e.EV_KEY, e.BTN_THUMB, 1)     # in 2nd gear before the proxy exists
    ui.syn()
    closed = {'ui': False}
    spec = ProxySpec.from_dict({
        'id': 'test-gear', 'name': 'gear',
        'identity': {'name': 'Proxied Shifter'},
        'sources': {'sh': {'match': {'vendor': '1234', 'product': '7777'}}},
        'mappings': [{'from': 'BTN_TRIGGER', 'to': '300'}, {'from': 'BTN_THUMB', 'to': '301'}],
    })
    proxy = ProxyDevice(spec)
    proxy.start()
    try:
        wait_state(proxy, ProxyState.RUNNING)
        virt = find_by_name('Proxied Shifter')
        time.sleep(0.2)
        assert 301 in virt.active_keys()           # seeded from the source
        ui.close(); closed['ui'] = True            # shifter unplugged while in gear
        t0 = time.time()
        while 301 in virt.active_keys() and time.time() - t0 < 3:
            time.sleep(0.05)
        assert 301 not in virt.active_keys()       # neutral
    finally:
        proxy.stop()
        if not closed['ui']:
            ui.close()


def test_auto_invert_from_rest_position():
    """'auto' inverts an axis that rests high (so games see it released) and leaves one resting low alone."""
    caps = {e.EV_ABS: [(e.ABS_THROTTLE, AbsInfo(65535, 0, 65535, 0, 0, 0)), (e.ABS_RUDDER, AbsInfo(0, 0, 65535, 0, 0, 0))]}
    ui = UInput(caps, name='Fake Levers', vendor=FAKE_VENDOR, product=0x5555, version=1, phys='usb-fake/input4')
    time.sleep(0.2)
    spec = ProxySpec.from_dict({
        'id': 'test-auto', 'name': 'auto',
        'identity': {'name': 'Proxied Levers'},
        'sources': {'lv': {'match': {'vendor': '1234', 'product': '5555'}}},
        'capabilities': {'abs': {'ABS_GAS': {'min': 0, 'max': 65535}, 'ABS_BRAKE': {'min': 0, 'max': 65535}}},
        'mappings': [{'from': 'ABS_THROTTLE', 'to': 'ABS_GAS', 'invert': 'auto'},
                     {'from': 'ABS_RUDDER', 'to': 'ABS_BRAKE', 'invert': 'auto'}],
    })
    proxy = ProxyDevice(spec)
    proxy.start()
    try:
        wait_state(proxy, ProxyState.RUNNING)
        virt = find_by_name('Proxied Levers')
        time.sleep(0.2)
        assert virt.absinfo(e.ABS_GAS).value == 0        # rested high -> inverted -> released
        assert virt.absinfo(e.ABS_BRAKE).value == 0      # rested low -> untouched
        ui.write(e.EV_ABS, e.ABS_THROTTLE, 0); ui.write(e.EV_ABS, e.ABS_RUDDER, 65535); ui.syn()
        events = read_events(virt)
        assert (e.EV_ABS, e.ABS_GAS, 65535) in events
        assert (e.EV_ABS, e.ABS_BRAKE, 65535) in events
    finally:
        proxy.stop()
        ui.close()


def test_load_specs_from_candidate_dir(tmp_path):
    """The GUI hands the installer a '.candidate-*' directory; its specs must load."""
    from oversteer.proxy.manager import load_specs
    cand = tmp_path / '.candidate-abc'
    cand.mkdir()
    (cand / 'x.json').write_text('{"id":"x","identity":{"name":"X"},"sources":{"s":{"match":{"vendor":"1234","product":"5678"}}},'
                                 '"capabilities":{"abs":{"ABS_Z":{"min":0,"max":255}}},"mappings":[{"from":"ABS_THROTTLE","to":"ABS_Z"}],"enabled":true}')
    specs, errors = load_specs([str(cand)])
    assert not errors and 'x' in specs and specs['x'].enabled


def test_shifter_sequential_buttons_are_mapped():
    """A T500 RS with the sequential plate reports two buttons past the gear
    positions; they must reach the combined device, not be dropped."""
    from types import SimpleNamespace
    from oversteer.proxy.equipment import build_combined_spec, KIND_WHEEL, KIND_SHIFTER

    def equipment(kind, name, usb_id, buttons, abs_codes=()):
        vendor, product = (int(x, 16) for x in usb_id.split(':'))
        return SimpleNamespace(kind=kind, name=name, usb_id=usb_id, vendor=vendor, product=product,
                               keys=list(buttons), buttons=list(buttons),
                               abs=list(abs_codes), axes=list(abs_codes),
                               node='/dev/input/event0', sys_path='/sys/x', readable=True,
                               version=0x0111, bustype=3, phys='usb-0000:00:14.0-1/input0',
                               ff=(kind == KIND_WHEEL))

    wheel = equipment(KIND_WHEEL, 'Logitech G29 Driving Force Racing Wheel', '046d:c24f',
                      list(range(288, 304)) + list(range(704, 713)),
                      [e.ABS_X, e.ABS_Y, e.ABS_Z, e.ABS_RZ])
    shifter = equipment(KIND_SHIFTER, 'Thustmaster T500 RS Gear Shift', '044f:b660', range(288, 298))
    spec = build_combined_spec(wheel, [shifter], spec_id='t').to_dict()
    targets = {m['from']: m['to'] for m in spec['mappings'] if m['source'].startswith('shifter')}
    # gears 1-7 + reverse, then the sequential pair on the next free codes
    assert targets['BTN_BASE3'] == 'BTN_TRIGGER_HAPPY11'      # 714, sequential down
    assert targets['BTN_BASE4'] == 'BTN_TRIGGER_HAPPY12'      # 715, sequential up
    assert len(set(targets.values())) == len(targets)         # no two controls share a code
    assert 'sequential down' in spec['description'] and 'sequential up' in spec['description']

    # Which control a press came from, as the shift learner needs it
    from oversteer.proxy.equipment import shift_button_kinds
    kinds = shift_button_kinds(ProxySpec.from_dict(spec))
    assert kinds[714] == kinds[715] == 'sequential'
    assert all(kinds[c] == 'gear' for c in (300, 301, 302, 303, 704, 705, 706, 713))   # 7 gears + R
    assert kinds[e.BTN_TOP2] == kinds[e.BTN_PINKIE] == 'paddle'                       # the G29's own
    assert shift_button_kinds(None, logitech=False) == {}
