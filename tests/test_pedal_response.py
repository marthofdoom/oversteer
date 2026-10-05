import pytest
from evdev import ecodes
from oversteer.device import Device
from oversteer.model import Model


class FakeDevice:
    """Just enough of Device for the model: records what reaches the driver."""
    PEDAL_NAMES = Device.PEDAL_NAMES

    def __init__(self, supported=True, mask=0, held=None, combine=0, fail=False):
        self.supported = supported
        self.mask = mask
        self.held = held or {}      # bit -> response the driver holds
        self.combine = combine
        self.fail = fail
        self.written = []           # (bit, start, end, sensitivity)
        self.invert_writes = []

    def has_pedal_response(self):
        return self.supported

    def pedal_axes(self, mask=None):
        # clutch / accelerator / brakes on raw Y / Z / RZ
        return {ecodes.ABS_Y: (65535, 0, 1), ecodes.ABS_Z: (65535, 0, 2), ecodes.ABS_RZ: (65535, 0, 4)}

    def get_pedal_response(self, bit):
        return self.held.get(bit, (0, 100, 50)) if self.supported else None

    def get_combine_pedals(self):
        return self.combine

    def set_combine_pedals(self, value):
        self.combine = value

    def set_pedal_response(self, bit, start, end, sensitivity):
        if self.fail:
            raise PermissionError("denied")
        self.held[bit] = (start, end, sensitivity)
        self.written.append((bit, start, end, sensitivity))

    def set_invert_pedals(self, mask):
        self.invert_writes.append(mask)
        return True

    def get_invert_pedals(self):
        return self.mask

    def __getattr__(self, name):
        # every other device setting: absent
        if name.startswith('get_'):
            return lambda *a: None
        if name.startswith('has_'):
            return lambda *a: False
        raise AttributeError(name)


def make_model(supported=True, mask=0, **kwargs):
    model = Model(FakeDevice(supported, mask, **kwargs))
    return model


def test_defaults_follow_the_driver():
    assert make_model().get_brakes_response() == (0, 100, 50)
    old = make_model(supported=False)
    assert old.get_clutch_response() is None
    old.set_clutch_response(10, 90, 50)          # nothing to set: ignored
    assert old.get_clutch_response() is None
    assert old.device.written == []


@pytest.mark.parametrize('args', [(-1, 100, 50), (50, 50, 50), (60, 40, 50), (0, 101, 50), (0, 100, -1), (0, 100, 101)])
def test_validation(args):
    model = make_model()
    with pytest.raises(ValueError):
        model.set_accelerator_response(*args)
    assert model.get_accelerator_response() == (0, 100, 50)
    assert model.device.written == []


@pytest.mark.parametrize('mask', [0, 7])
def test_written_unchanged_whichever_way_the_axis_runs(mask):
    model = make_model(mask=mask)
    model.data['invert_pedals'] = mask
    model.set_accelerator_response(10, 90, 30)
    model.set_brakes_response(5, 60, 40)
    assert model.device.written == [(2, 10, 90, 30), (4, 5, 60, 40)]


def test_invert_change_writes_nothing():
    model = make_model()
    model.set_clutch_response(10, 90, 30)
    model.device.written.clear()
    model.set_invert_pedals(2)
    assert model.device.invert_writes == [2]
    assert model.device.written == []


def test_reading_the_device_keeps_the_drivers_curves():
    model = make_model(held={4: (3, 85, 40)})
    assert model.get_brakes_response() == (3, 85, 40)
    assert model.get_clutch_response() == (0, 100, 50)
    model.flush_device()
    assert model.device.held[4] == (3, 85, 40)


def test_driver_values_outside_the_ui_ranges_are_clamped():
    model = make_model(held={4: (60, 70, 50), 2: (0, 50, 20), 1: (9, 3, 50)})
    assert model.get_brakes_response() == (45, 70, 50)
    assert model.get_accelerator_response() == (0, 55, 20)
    assert model.get_clutch_response() is None          # not a valid response


def test_set_is_kept_to_the_ui_ranges():
    model = make_model()
    with pytest.raises(ValueError):
        model.set_brakes_response(50, 90, 50)
    with pytest.raises(ValueError):
        model.set_brakes_response(0, 50, 50)
    model.set_brakes_response(45, 55, 50)


def test_combined_pedals_skip_the_writes_until_uncombined():
    model = make_model(combine=1)
    model.set_brakes_response(5, 60, 40)
    assert model.get_brakes_response() == (5, 60, 40)
    assert model.device.written == []
    model.flush_device()
    assert model.device.written == []
    model.set_combine_pedals(0)
    assert (4, 5, 60, 40) in model.device.written


def test_write_failure_does_not_abort_the_flush():
    model = make_model(fail=True)
    model.data['clutch_response'] = (5, 95, 20)
    model.data['brakes_response'] = (0, 80, 60)
    model.flush_device()                    # no exception
    model.set_accelerator_response(10, 90, 30)
    assert model.get_accelerator_response() == (10, 90, 30)


def test_refresh_reads_what_was_unreadable():
    model = make_model(held={4: (3, 85, 40)})
    model.data['brakes_response'] = None
    model.refresh_pedal_responses()
    assert model.get_brakes_response() == (3, 85, 40)


def test_flush_device_applies_the_responses():
    model = make_model()
    model.data['invert_pedals'] = 7
    model.data['brakes_response'] = (0, 80, 60)
    model.flush_device()
    assert (4, 0, 80, 60) in model.device.written


def test_profile_round_trip_and_old_profile(tmp_path):
    model = make_model()
    model.data['invert_pedals'] = 0
    model.set_clutch_response(5, 95, 20)
    path = str(tmp_path / 'p.ini')
    model.save(path)
    other = make_model()
    other.load(path)
    assert other.get_clutch_response() == (5, 95, 20)
    assert other.get_brakes_response() == (0, 100, 50)

    # a profile from before this existed keeps what the device has now
    old = tmp_path / 'old.ini'
    old.write_text("[DEFAULT]\nrange = 540\n")
    other.load(str(old))
    assert other.get_clutch_response() == (5, 95, 20)


def test_invalid_profile_values_keep_the_current_ones(tmp_path):
    model = make_model(held={4: (3, 85, 40)})
    for bad in ("60,40,50", "0,100", "a,b,c", "0,100,101", "-1,100,50", "5,5,50", "0,100,50,1"):
        path = tmp_path / 'bad.ini'
        path.write_text("[DEFAULT]\nbrakes_response = {}\nclutch_response = 5,95,20\n".format(bad))
        model.profile = None
        model.load(str(path))
        assert model.get_brakes_response() == (3, 85, 40), bad
        assert model.get_clutch_response() == (5, 95, 20)


def test_old_driver_has_no_attributes(tmp_path):
    device = Device.__new__(Device)
    device.device_file = lambda name: str(tmp_path / name)
    assert not device.has_pedal_response()
    assert device.get_pedal_response(1) is None
    assert device.set_pedal_response(1, 0, 100, 50) is False
    assert device.get_pedal_response(8) is None


def test_sysfs_read_and_write(tmp_path):
    device = Device.__new__(Device)
    device.device_file = lambda name: str(tmp_path / name)
    (tmp_path / 'pedal_response_rz').write_text("0 100 50\n")
    assert device.has_pedal_response()
    assert device.get_pedal_response(4) == (0, 100, 50)
    assert device.get_pedal_response(1) is None
    assert device.set_pedal_response(4, 10, 90, 70)
    assert (tmp_path / 'pedal_response_rz').read_text() == "10 90 70"


def test_presets_are_written_as_they_are():
    model = make_model()
    model.data['invert_pedals'] = 0
    assert model.set_pedal_preset('brakes', 'spring_brake') == (3, 85, 40)
    assert model.get_brakes_response() == (3, 85, 40)
    assert model.device.written == [(4, 3, 85, 40)]
    model.set_pedal_preset('brakes', 'linear')
    assert model.get_brakes_response() == (0, 100, 50)


def test_spring_brake_preset_is_brakes_only():
    model = make_model()
    with pytest.raises(ValueError):
        model.set_pedal_preset('clutch', 'spring_brake')
