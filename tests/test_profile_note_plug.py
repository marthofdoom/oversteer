"""Per-profile note and the "apply when the wheel is plugged in" flag: the file round trip and which profile a plugged wheel gets."""
from oversteer.model import Model, pick_plug_profile, read_apply_on_plug


class StubDevice:
    def get_invert_pedals(self):
        return 0


def _model():
    model = Model()
    model.device = StubDevice()
    return model


def test_round_trip(tmp_path):
    path = str(tmp_path / 'rally.ini')
    model = _model()
    model.data['profile_note'] = 'RBR, Group A  50% grip\nline two'
    model.data['apply_on_plug'] = True
    model.save(path)
    assert read_apply_on_plug(path)
    other = _model()
    other.load(path)
    assert other.get_profile_note() == 'RBR, Group A 50% grip line two'
    assert other.get_apply_on_plug() is True


def test_older_file_has_empty_note_and_no_flag(tmp_path):
    path = tmp_path / 'old.ini'
    path.write_text('[DEFAULT]\nrange = 900\n')
    model = _model()
    model.load(str(path))
    assert model.get_profile_note() == ''
    assert model.get_apply_on_plug() is False
    assert not read_apply_on_plug(str(path))


def test_force_reloads_the_same_profile(tmp_path):
    path = tmp_path / 'p.ini'
    path.write_text('[DEFAULT]\nrange = 900\n')
    model = _model()
    model.load(str(path))
    path.write_text('[DEFAULT]\nrange = 540\n')
    model.load(str(path))
    assert model.data['range'] == 900
    model.load(str(path), force=True)
    assert model.data['range'] == 540


def test_unreadable_flag_is_false(tmp_path):
    assert not read_apply_on_plug(str(tmp_path / 'missing.ini'))


def test_pick_plug_profile():
    assert pick_plug_profile([], ['a']) is None
    assert pick_plug_profile(['a'], []) == 'a'
    assert pick_plug_profile(['a', 'b'], ['b', 'a']) == 'b'
    assert pick_plug_profile(['a', 'b'], ['x', 'a', 'b']) == 'a'
    assert pick_plug_profile(['z', 'b'], ['a']) == 'b'          # none recent: alphabetical
    assert pick_plug_profile(['z', 'b'], ['z']) == 'z'          # a recent one beats an unknown one
