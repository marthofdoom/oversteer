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
