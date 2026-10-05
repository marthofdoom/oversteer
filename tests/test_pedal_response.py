import pytest
from evdev import ecodes
from oversteer.device import Device, pedal_response_to_raw
from oversteer.model import Model


class FakeDevice:
    """Just enough of Device for the model: records what reaches the driver."""
    PEDAL_NAMES = Device.PEDAL_NAMES

    def __init__(self, supported=True, mask=0):
        self.supported = supported
        self.mask = mask
        self.written = []           # (bit, start, end, sensitivity)
        self.invert_writes = []

    def has_pedal_response(self):
        return self.supported

    def pedal_axes(self, mask=None):
        # clutch / accelerator / brakes on raw Y / Z / RZ
        return {ecodes.ABS_Y: (65535, 0, 1), ecodes.ABS_Z: (65535, 0, 2), ecodes.ABS_RZ: (65535, 0, 4)}

    def set_pedal_response(self, bit, start, end, sensitivity):
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


def make_model(supported=True, mask=0):
    model = Model(FakeDevice(supported, mask))
    return model


def test_translation_both_directions():
    assert pedal_response_to_raw((10, 90, 30), False) == (10, 90, 30)
    assert pedal_response_to_raw((10, 90, 30), True) == (10, 90, 70)
    assert pedal_response_to_raw((0, 100, 50), True) == (0, 100, 50)
    assert pedal_response_to_raw((20, 80, 100), True) == (20, 80, 0)
    assert pedal_response_to_raw((5, 60, 40), True) == (40, 95, 60)


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


def test_released_at_axis_minimum_is_written_as_is():
    model = make_model(mask=7)
    model.data['invert_pedals'] = 7
    model.set_accelerator_response(10, 90, 30)
    assert model.device.written == [(2, 10, 90, 30)]


def test_released_at_axis_maximum_is_mirrored():
    model = make_model()
    model.data['invert_pedals'] = 0
    model.set_accelerator_response(10, 90, 30)
    assert model.device.written == [(2, 10, 90, 70)]
    model.device.written.clear()
    model.set_brakes_response(5, 60, 40)
    assert model.device.written == [(4, 40, 95, 60)]


def test_invert_change_reapplies_every_pedal():
    model = make_model()
    model.data['invert_pedals'] = 0
    model.set_clutch_response(10, 90, 30)
    model.set_accelerator_response(0, 100, 20)
    model.device.written.clear()
    model.set_invert_pedals(2)                   # only the accelerator flips
    assert model.device.invert_writes == [2]
    assert sorted(model.device.written) == [(1, 10, 90, 70), (2, 0, 100, 20), (4, 0, 100, 50)]


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


def test_presets_go_through_the_same_translation():
    model = make_model()
    model.data['invert_pedals'] = 0
    assert model.set_pedal_preset('brakes', 'spring_brake') == (3, 85, 40)
    assert model.get_brakes_response() == (3, 85, 40)
    assert model.device.written == [(4, 15, 97, 60)]          # released high: mirrored
    model.device.written.clear()
    model.set_invert_pedals(4)
    assert (4, 3, 85, 40) in model.device.written              # released low: as is
    model.set_pedal_preset('brakes', 'linear')
    assert model.get_brakes_response() == (0, 100, 50)


def test_spring_brake_preset_is_brakes_only():
    model = make_model()
    with pytest.raises(ValueError):
        model.set_pedal_preset('clutch', 'spring_brake')
