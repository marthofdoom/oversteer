import json
import os

import pytest
from evdev import ecodes

from oversteer import handbrake_calibration as hbcal
from oversteer.proxy import calibration
from oversteer.proxy.spec import Mapping, ProxySpec, one_sided_fraction

F = one_sided_fraction


# --- the mapping ---------------------------------------------------------

def test_rest_and_dead_band_read_zero_then_linear_to_full():
    # real travel 6000..60000 of 0..65535, dead 3 % of 54000 = 1620
    assert F(6000, 0, 65535, 6000, 60000, 0.03, False) == 0
    assert F(7620, 0, 65535, 6000, 60000, 0.03, False) == 0
    assert F(60000, 0, 65535, 6000, 60000, 0.03, False) == 1
    assert F(5000, 0, 65535, 6000, 60000, 0.03, False) == 0
    mid = F(33810, 0, 65535, 6000, 60000, 0.03, False)
    assert mid == pytest.approx(0.5)


def test_clamped_beyond_the_travel():
    assert F(65535, 0, 65535, 6000, 60000, 0.03, False) == 1
    assert F(0, 0, 65535, 6000, 60000, 0.03, False) == 0


def test_invert_rests_at_the_high_end():
    # lever rests at 60000 (drifting down to 58000), pulled it goes to 6000
    assert F(60000, 0, 65535, 6000, 60000, 0.03, True) == 0
    assert F(58500, 0, 65535, 6000, 60000, 0.03, True) == 0     # inside the dead band
    assert F(6000, 0, 65535, 6000, 60000, 0.03, True) == 1
    assert F(0, 0, 65535, 6000, 60000, 0.03, True) == 1
    assert F(65535, 0, 65535, 6000, 60000, 0.03, True) == 0
    assert F(33000, 0, 65535, 6000, 60000, 0.03, True) == pytest.approx(0.5, abs=0.02)


def test_degenerate_travel_reads_zero():
    assert F(5, 0, 10, 4, 4, 0.03, False) == 0


def _spec(extra=None):
    m = {"source": "handbrake1", "from": "ABS_THROTTLE", "to": "ABS_THROTTLE", "invert": "auto"}
    m.update(extra or {})
    return {"id": "t", "name": "t",
            "identity": {"name": "T", "vendor": "0eb7", "product": "00e5", "version": "0001", "bustype": "usb"},
            "sources": {"handbrake1": {"match": {"vendor": "8086", "product": "0a01"}}},
            "capabilities": {"abs": {"ABS_THROTTLE": {"min": 0, "max": 65535}}},
            "mappings": [m]}


def test_spec_without_fields_is_unchanged():
    spec = ProxySpec.from_dict(_spec())
    m = spec.mappings[0]
    assert m.low is None and m.high is None
    d = m.to_dict()
    assert 'low' not in d and 'high' not in d and 'dead' not in d


def test_spec_round_trip(tmp_path):
    spec = ProxySpec.from_dict(_spec({"low": 5600, "high": 59000}))
    m = spec.mappings[0]
    assert (m.low, m.high, m.dead) == (5600, 59000, 0.03)
    path = str(tmp_path / 't.json')
    spec.save(path)
    again = ProxySpec.load(path).mappings[0]
    assert (again.low, again.high, again.dead, again.invert) == (5600, 59000, 0.03, 'auto')


@pytest.mark.parametrize("extra", [{"low": 5}, {"low": 9, "high": 5}, {"low": 1, "high": 5, "dead": 1.0},
                                   {"low": 1, "high": 5, "deadzone": 0.1}, {"low": "x", "high": 5}])
def test_spec_rejects_bad_fields(extra):
    from oversteer.proxy.spec import SpecError
    with pytest.raises(SpecError):
        ProxySpec.from_dict(_spec(extra))


# --- the run-time channel -----------------------------------------------

def test_calibration_file_round_trip_and_validation(tmp_path):
    d = str(tmp_path)
    calibration.write("combined", {"handbrake1:6": {"low": 10, "high": 900, "dead": 0.03}}, d)
    entries = calibration.read(calibration.path_for("combined", d))
    assert entries == {"handbrake1:6": {"low": 10.0, "high": 900.0, "dead": 0.03}}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"a:1": {"low": 5, "high": 1}, "b:1": {"low": "x", "high": 2},
                               "c:1": {"low": 1, "high": 2, "dead": 2}, "ok:1": {"low": 1, "high": 2}}))
    assert list(calibration.read(str(bad))) == ["ok:1"]
    bad.write_text("not json")
    assert calibration.read(str(bad)) is None


def test_calibration_file_refuses_symlinks_and_big_files(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    os.symlink(real, link)
    assert calibration.read(str(link)) is None
    big = tmp_path / "big.json"
    big.write_text(" " * 5000)
    assert calibration.read(str(big)) is None


def test_fit_clamps_and_rejects_short_travel():
    assert calibration.fit({"low": -50, "high": 70000, "dead": 0.03}, 0, 65535) == (0, 65535, 0.03)
    assert calibration.fit({"low": 100, "high": 200, "dead": 0.03}, 0, 65535) is None


def test_device_applies_calibration_without_uinput():
    from oversteer.proxy.device import ProxyDevice

    class Info:
        min, max = 0, 65535

    class Src:
        path = "/dev/null"
        absinfo = {ecodes.ABS_THROTTLE: Info()}

    dev = ProxyDevice(ProxySpec.from_dict(_spec()))
    dev.sources['handbrake1'] = Src()
    dev.apply_calibration({"handbrake1:%d" % ecodes.ABS_THROTTLE: {"low": 6000.0, "high": 60000.0, "dead": 0.03},
                           "nonsense": {"low": 1.0, "high": 2.0, "dead": 0.0}})
    rule = dev.spec.mappings[0]
    assert (rule.low, rule.high) == (6000.0, 60000.0)
    dev._auto_invert[('handbrake1', ecodes.ABS_THROTTLE)] = False
    assert dev._normalise(rule, Src(), 7000) == 0
    assert dev._normalise(rule, Src(), 60000) == 1
    assert dev.status()['calibration']


# --- learning ------------------------------------------------------------

def _run(rest_pattern, peak, duration=5.0, hz=50, rest_hi=None):
    """Released (drifting) for the first and last second, a pull between."""
    samples = []
    for i in range(int(duration * hz)):
        t = i / hz
        if t < 1.0 or t > duration - 1.0:
            v = rest_pattern[i % len(rest_pattern)]
        else:
            ramp = min(t - 1.0, 1.0, duration - 1.0 - t) / 1.0
            v = rest_pattern[0] + ramp * (peak - rest_pattern[0])
        samples.append((t, v))
    return samples


def test_learn_released_drift_and_full_pull():
    samples = _run([5600, 6200, 8500, 7000], 60000)
    low, high = hbcal.learn(samples, 5.0, False, 0, 65535, 0, 65535)
    assert low == pytest.approx(8500 + 0.01 * 65535)
    assert high == 60000
    # a reading at the drifted rest then reads 0 through the mapping
    assert one_sided_fraction(8500, 0, 65535, low, high, 0.03, False) == 0


def test_learn_inverted_axis_gives_source_units():
    # the virtual axis is already flipped: rest low, pulled high; source is the reverse
    samples = _run([3000, 4000], 50000)
    low, high = hbcal.learn(samples, 5.0, True, 0, 65535, 0, 65535)
    assert low == pytest.approx(65535 - 50000)
    assert high == pytest.approx(65535 - 4000 - 0.01 * 65535)


def test_learn_over_an_existing_calibration():
    # the axis already reads 0..1 over source 6000..60000 (dead 0.03): rest drifted up to 0.1
    samples = _run([0.0, 0.1 * 65535], 65535)
    low, high = hbcal.learn(samples, 5.0, False, 0, 65535, 0, 65535, (6000, 60000, 0.03))
    assert 6000 < low < 60000 and high == pytest.approx(60000)


def test_learn_refuses_bad_sequences():
    flat = [(i / 50, 7000) for i in range(250)]
    with pytest.raises(hbcal.CalibrationError):
        hbcal.learn(flat, 5.0, False, 0, 65535, 0, 65535)            # never pulled
    held = _run([40000, 41000], 60000)
    held = [(t, 40000 + 20000 if t > 4.5 else v) for t, v in held]
    with pytest.raises(hbcal.CalibrationError):
        hbcal.learn(held, 5.0, False, 0, 65535, 0, 65535)            # not released at the end
    with pytest.raises(hbcal.CalibrationError):
        hbcal.learn([(0, 1)], 5.0, False, 0, 65535, 0, 65535)       # no readings


# --- calibrating mode and ownership --------------------------------------

def _device():
    from oversteer.proxy.device import ProxyDevice

    class Info:
        min, max = 0, 65535

    class Src:
        path = "/dev/null"
        absinfo = {ecodes.ABS_THROTTLE: Info()}

    dev = ProxyDevice(ProxySpec.from_dict(_spec()))
    dev.sources['handbrake1'] = Src()
    return dev, Src()


NAME = "handbrake1:%d" % ecodes.ABS_THROTTLE


def test_calibrating_passes_raw_and_restores_the_previous_values():
    dev, src = _device()
    rule = dev.spec.mappings[0]
    dev._auto_invert[('handbrake1', ecodes.ABS_THROTTLE)] = False
    dev.apply_calibration({NAME: {"low": 20000.0, "high": 60000.0, "dead": 0.03}})
    assert dev._normalise(rule, src, 10000) == 0                       # below the old low
    dev.apply_calibration({NAME: {"calibrating": True}})
    assert dev._normalise(rule, src, 10000) == pytest.approx(10000 / 65535)     # the true range
    assert dev._normalise(rule, src, 65535) == 1
    # cancel: the app writes the previous entry again
    dev.apply_calibration({NAME: {"low": 20000.0, "high": 60000.0, "dead": 0.03}})
    assert (rule.low, rule.high) == (20000.0, 60000.0)
    # new values after a calibration replace them
    dev.apply_calibration({NAME: {"calibrating": True}})
    dev.apply_calibration({NAME: {"low": 9000.0, "high": 50000.0, "dead": 0.03}})
    assert (rule.low, rule.high) == (9000.0, 50000.0)
    assert not dev.calibrating


def test_calibrating_times_out_to_the_saved_values_and_to_uncalibrated():
    dev, src = _device()
    rule = dev.spec.mappings[0]
    dev.apply_calibration({NAME: {"low": 20000.0, "high": 60000.0, "dead": 0.03}})
    dev.apply_calibration({NAME: {"calibrating": True}})
    assert rule.low is None
    dev._restore_expired(10 ** 9)
    assert (rule.low, rule.high) == (20000.0, 60000.0)
    dev, src = _device()                                               # nothing saved: back to the declared range
    rule = dev.spec.mappings[0]
    dev.apply_calibration({NAME: {"calibrating": True}})
    dev._restore_expired(10 ** 9)
    assert rule.low is None


def test_only_root_or_the_installing_user_may_calibrate(tmp_path):
    d = str(tmp_path)
    calibration.write("t", {NAME: {"low": 1000, "high": 60000, "dead": 0.03}}, d)
    path = calibration.path_for("t", d)
    mine = os.getuid()
    assert calibration.read(path, {mine}) is not None
    assert calibration.read(path, {mine + 1, 0}) is None               # someone else's file
    uid_file = tmp_path / "uid"
    assert calibration.allowed_uids(str(uid_file)) == {0}              # none recorded: root only
    uid_file.write_text("%d\n" % (mine + 1))
    assert calibration.allowed_uids(str(uid_file)) == {0, mine + 1}


def test_device_ignores_a_file_of_another_user(tmp_path):
    dev, _src = _device()
    dev.calibration_dir = str(tmp_path)
    dev.calibration_uid_file = str(tmp_path / "nouid")                # nobody recorded: only root's files
    calibration.write(dev.spec.id, {NAME: {"low": 1000, "high": 60000, "dead": 0.03}}, dev.calibration_dir)
    dev._check_calibration(now=1.0)
    assert dev.spec.mappings[0].low is None
    (tmp_path / "nouid").write_text("%d\n" % os.getuid())
    calibration.write(dev.spec.id, {NAME: {"low": 1000, "high": 60000, "dead": 0.03}}, dev.calibration_dir)    # rewritten
    dev._calibration_next = 0
    dev._check_calibration(now=2.0)
    assert dev.spec.mappings[0].low == 1000.0


# --- carrying over and re-sending ----------------------------------------

def _hb_spec(vendor, product, low=None):
    extra = {"low": low, "high": 60000} if low else {}
    d = _spec(extra)
    d["sources"] = {"hb": {"match": {"vendor": vendor, "product": product}}}
    d["mappings"][0]["source"] = "hb"
    return ProxySpec.from_dict(d)


def test_rebuilt_specs_keep_the_calibration_of_the_same_device_and_axis():
    from oversteer.proxy.equipment import carry_calibration
    old = _hb_spec("8086", "0a01", low=7000)
    new = _hb_spec("8086", "0a01")
    other = _hb_spec("1111", "2222")
    carry_calibration([new, other], [old])
    assert (new.mappings[0].low, new.mappings[0].high, new.mappings[0].dead) == (7000, 60000, 0.03)
    assert other.mappings[0].low is None


def test_the_installed_spec_carries_low_and_high(tmp_path):
    spec = _hb_spec("8086", "0a01", low=7000)
    spec.save(str(tmp_path / "x.json"))
    assert json.load(open(tmp_path / "x.json"))["mappings"][0]["low"] == 7000


def test_stale_detects_a_restarted_service():
    wanted = {NAME: {"low": 7000.0, "high": 60000.0, "dead": 0.03}}
    assert not calibration.stale({NAME: [7000.0, 60000.0, 0.03]}, wanted)
    assert calibration.stale({}, wanted)                                # restarted: nothing applied
    assert calibration.stale({NAME: [7000.0, 59000.0, 0.03]}, wanted)


def test_status_reports_the_calibration_so_the_resend_stops():
    dev, _src = _device()
    assert dev.status()['calibration'] == {}
    dev.apply_calibration({NAME: {"low": 7000.0, "high": 60000.0, "dead": 0.03}})
    assert dev.status()['calibration'] == {NAME: [7000.0, 60000.0, 0.03]}


def test_one_tick_resends_after_a_service_restart(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from oversteer.gui import Gui
    from oversteer.proxy.manager import ProxyManager
    spec = ProxySpec.from_dict(_spec({"low": 7000, "high": 60000}))
    sent = []
    status = {'pid': 1, 'proxies': [{'id': 't', 'runtime_calibration': True, 'calibration': {}}]}
    monkeypatch.setattr(ProxyManager, 'read_status', staticmethod(lambda: status))
    monkeypatch.setattr(calibration, 'write', lambda pid, entries, *a: sent.append((pid, entries)))
    fake = SimpleNamespace(hb_cal=None, combine_busy=False, _load_combined_spec=lambda: spec)
    fake._push_handbrake_calibration = lambda pid, sp, calibrating=None: Gui._push_handbrake_calibration(fake, pid, sp, calibrating)
    proxied = ('t', 'handbrake1', ecodes.ABS_THROTTLE, False)
    Gui._resend_handbrake_calibration(fake, proxied)
    assert sent == [('t', {NAME: {'low': 7000.0, 'high': 60000.0, 'dead': 0.03}})]
    status['proxies'][0]['calibration'] = {NAME: [7000.0, 60000.0, 0.03]}      # the daemon applied it
    Gui._resend_handbrake_calibration(fake, proxied)
    assert len(sent) == 1
    fake.hb_cal = {}                                                    # a calibration in progress is left alone
    status['proxies'][0]['calibration'] = {}
    Gui._resend_handbrake_calibration(fake, proxied)
    assert len(sent) == 1


def _check_fake(monkeypatch, proxy, spec):
    from types import SimpleNamespace
    from oversteer.gui import Gui
    from oversteer.proxy import manager
    from oversteer.proxy.manager import ProxyManager
    status = {'pid': 1, 'proxies': [proxy]}
    monkeypatch.setattr(ProxyManager, 'read_status', staticmethod(lambda: status))
    monkeypatch.setattr(manager, 'wine_running', lambda: False)
    proxied = ('t', 'handbrake1', ecodes.ABS_THROTTLE, False)
    fake = SimpleNamespace(hb_cal=None, combine_busy=False, device=object(), handbrake_axis=(ecodes.ABS_RZ, 0, 255),
            _proxied_handbrake=lambda: proxied, _load_combined_spec=lambda: spec)
    fake._handbrake_status = lambda p: Gui._handbrake_status(fake, p)
    fake._handbrake_source_range = lambda p: Gui._handbrake_source_range(fake, p)
    return Gui, fake


def test_old_daemon_goes_to_the_update_flow_not_a_range_error(monkeypatch):
    spec = ProxySpec.from_dict(_spec())
    Gui, fake = _check_fake(monkeypatch, {'id': 't', 'sources': {'handbrake1': '/dev/null-nope'}}, spec)
    reason, ctx = Gui._handbrake_calibration_check(fake, False)
    assert reason is None and ctx['needs_update'] and not ctx['live']
    reason, ctx = Gui._handbrake_calibration_check(fake, True)         # the web page names the window
    assert ctx is None and 'from the window' in reason


def test_new_daemon_calibrates_directly(monkeypatch):
    spec = ProxySpec.from_dict(_spec())
    proxy = {'id': 't', 'runtime_calibration': True, 'source_axes': {'handbrake1:%d' % ecodes.ABS_THROTTLE: [0, 65535]}}
    Gui, fake = _check_fake(monkeypatch, proxy, spec)
    for runtime_only in (False, True):
        reason, ctx = Gui._handbrake_calibration_check(fake, runtime_only)
        assert reason is None and not ctx['needs_update'] and ctx['live'] and ctx['range'] == (0, 65535)


def test_new_daemon_without_a_range_says_which_lookup_failed(monkeypatch):
    spec = ProxySpec.from_dict(_spec())
    proxy = {'id': 't', 'runtime_calibration': True, 'sources': {'handbrake1': '/nonexistent'}}
    Gui, fake = _check_fake(monkeypatch, proxy, spec)
    reason, ctx = Gui._handbrake_calibration_check(fake, False)
    assert ctx is None and 'device node could not be read' in reason
