import os

from oversteer.device import Device
from oversteer.ffb_meter import FfbMeter, FULL_SCALE, HOLD_SECONDS, level_fraction


def test_level_fraction_clamps():
    assert level_fraction(0) == 0.0
    assert level_fraction(None) == 0.0
    assert level_fraction(FULL_SCALE // 2) == FULL_SCALE // 2 / FULL_SCALE
    assert level_fraction(FULL_SCALE * 2) == 1.0


def test_peak_holds_then_decays():
    m = FfbMeter()
    m.update(FULL_SCALE * 0.8, 0.0)
    assert abs(m.peak - 0.8) < 1e-3
    m.update(0, 1.0)
    assert abs(m.peak - 0.8) < 1e-3          # held
    m.update(0, HOLD_SECONDS + 0.1)
    assert m.peak < 0.8                      # started to fall
    m.update(0, HOLD_SECONDS + 5)
    assert m.peak == 0.0
    m.update(FULL_SCALE * 0.3, HOLD_SECONDS + 5.1)
    assert abs(m.peak - 0.3) < 1e-3          # a new higher value takes over


def test_fallback_counts_polled_full_scale_peaks():
    m = FfbMeter()
    for i, level in enumerate((100, FULL_SCALE, FULL_SCALE + 500, 10)):
        m.update(level, i * 0.1)
    assert m.clipped == 2 and m.approximate
    m.clear_clips()
    assert m.clipped == 0


def test_driver_counter_is_a_difference_since_reset():
    m = FfbMeter()
    m.update(0, 0.0, 100)
    assert m.clipped == 0 and not m.approximate
    m.update(0, 0.1, 107)
    assert m.clipped == 7
    m.clear_clips()
    m.update(0, 0.2, 110)
    assert m.clipped == 0
    m.update(0, 0.3, 112)
    assert m.clipped == 2
    m.update(FULL_SCALE, 0.4, 112)           # the polled peak is not double counted
    assert m.clipped == 2


def test_driver_counter_wraps():
    m = FfbMeter()
    m.update(0, 0.0, 0xfffffffe)
    m.update(0, 0.1, 1)
    assert m.clipped == 3


def _device(tmp_path):
    d = Device.__new__(Device)
    d.dev_path = str(tmp_path)
    return d


def test_device_reads_clip_count(tmp_path):
    d = _device(tmp_path)
    assert d.get_ffb_clip_count() is None    # old driver: no attribute
    path = tmp_path / 'ffb_clip_count'
    path.write_text('42\n')
    os.chmod(path, 0o444)                    # read-only, as the driver makes it
    assert d.get_ffb_clip_count() == 42
    path.chmod(0o644)
    path.write_text('junk\n')
    assert d.get_ffb_clip_count() is None
