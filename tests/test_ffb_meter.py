import os

from oversteer.device import Device
from oversteer.ffb_meter import FfbMeter, FfbPoller, FULL_SCALE, HOLD_SECONDS, level_fraction


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
    for i, level in enumerate((100, FULL_SCALE, FULL_SCALE + 1, FULL_SCALE + 500, 10)):
        m.update(level, i * 0.1)
    assert m.clipped == 2 and m.approximate and not m.over
    m.clear_clips()
    assert m.clipped == 0


def test_driver_counter_is_a_difference_since_reset():
    m = FfbMeter()
    m.update(0, 0.0, 100)
    assert m.clipped == 0 and not m.approximate
    m.update(0, 0.1, 107)
    assert m.clipped == 7
    m.clear_clips()
    assert not m.approximate and m.clipped == 0
    m.update(0, 0.2, 110)
    assert m.clipped == 3                    # kept the clips since the reset
    m.update(0, 0.3, 112)
    assert m.clipped == 5
    m.update(FULL_SCALE + 1, 0.4, 112)       # the polled peak is not double counted
    assert m.clipped == 5


def test_driver_counter_wraps():
    m = FfbMeter()
    m.update(0, 0.0, 0xfffffffe)
    m.update(0, 0.1, 1)
    assert m.clipped == 3


def test_driver_counter_restart_continues():
    m = FfbMeter()
    m.update(0, 0.0, 1000)
    m.update(0, 0.1, 1010)
    assert m.clipped == 10
    m.update(0, 0.2, 3)                      # driver reloaded: counter back near 0
    assert m.clipped == 10
    m.update(0, 0.3, 8)
    assert m.clipped == 15


class FakeController:
    def __init__(self):
        self.levels = []
        self.started = []

    def ffb_consumer_start(self, consumer):
        self.started.append(consumer)

    def read_ffbmeter(self, consumer):
        level = self.levels.pop(0)
        if isinstance(level, Exception):
            raise level
        return level

    def read_ffb_clips(self):
        return None


def test_poller_survives_unplug_and_rearms_after_unmap():
    mapped = [True]
    c = FakeController()
    p = FfbPoller(FfbMeter(), c, lambda: mapped[0])
    assert p.arm() and not p.arm() and c.started == ['meter']
    c.levels = [None, OSError('gone'), 100]
    assert p.tick(0.0) and p.tick(0.1) and p.tick(0.2)
    assert p.meter.fraction > 0
    mapped[0] = False
    assert not p.tick(0.3) and not p.running
    mapped[0] = True
    assert p.arm()


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


def test_shared_reader_folds_peaks_into_each_consumer():
    from types import SimpleNamespace
    from oversteer.gui import Gui

    class Dev:
        level = 0
        def get_peak_ffb_level(self):
            if self.level is None:
                raise OSError('gone')
            return self.level
        def set_peak_ffb_level(self, v):
            self.level = v

    gui = SimpleNamespace(device=Dev(), ffb_pending={})
    Gui.ffb_consumer_start(gui, 'overlay')
    Gui.ffb_consumer_start(gui, 'meter')
    gui.device.level = 500
    assert Gui.read_ffbmeter(gui, 'overlay') == 500
    gui.device.level = 100
    assert Gui.read_ffbmeter(gui, 'overlay') == 100
    assert Gui.read_ffbmeter(gui, 'meter') == 500      # kept the peak the overlay reset
    assert Gui.read_ffbmeter(gui, 'meter') == 0
    gui.device.level = None
    assert Gui.read_ffbmeter(gui, 'meter') is None     # unplugged mid-read
    gui.device = None
    assert Gui.read_ffbmeter(gui, 'meter') is None
