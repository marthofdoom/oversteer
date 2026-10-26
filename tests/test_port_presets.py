import pytest

from oversteer import telemetry
from oversteer.telemetry import DEFAULT_PORT, PORT_MAX, PORT_MIN, PORT_PRESETS, parse_port


@pytest.mark.parametrize('text, port', [
    ('5310', 5310), (' 20777 ', 20777), ('1024', 1024), ('65535', 65535),
    ('5310: Oversteer default (Assetto Corsa Rally bridge, Forza)', 5310),
])
def test_parse_port_accepts(text, port):
    assert parse_port(text) == port


@pytest.mark.parametrize('text', [None, '', ' ', 'abc', '1023', '65536', '0', '-5310', '53.10', '5e3', '99999999',
                                  '٥٣١٠', ': x', '53 10'])
def test_parse_port_rejects(text):
    assert parse_port(text) is None


def test_preset_table_is_sane():
    ports = [p for p, _label in PORT_PRESETS]
    assert ports[0] == DEFAULT_PORT
    assert len(set(ports)) == len(ports)
    assert all(PORT_MIN <= p <= PORT_MAX for p in ports)
    assert all(label for _p, label in PORT_PRESETS)
    assert telemetry.LEGACY_PORT not in ports        # inside Forza Horizon 6's own range


def _ui():
    gi = pytest.importorskip('gi')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    from oversteer.gtk_ui import GtkUi
    if not Gtk.init_check()[0]:
        pytest.skip('no display')
    ui = GtkUi.__new__(GtkUi)
    ui.updating_rev_leds = False
    ui.rev_leds_port = Gtk.ComboBoxText.new_with_entry()
    for port, label in PORT_PRESETS:
        ui.rev_leds_port.append_text(GtkUi.port_preset_text(port, label))
    return ui, GtkUi


def test_set_port_shows_the_preset_row_or_the_bare_number():
    ui, GtkUi = _ui()
    entry = ui.rev_leds_port.get_child()
    GtkUi._show_rev_leds_port(ui, 20777)
    assert ui.rev_leds_port.get_active() == 1 and entry.get_text().startswith('20777: ')
    GtkUi._show_rev_leds_port(ui, 6000)
    assert ui.rev_leds_port.get_active() == -1 and entry.get_text() == '6000'


def test_typed_port_applies_only_when_valid():
    ui, GtkUi = _ui()
    entry = ui.rev_leds_port.get_child()
    GtkUi._show_rev_leds_port(ui, DEFAULT_PORT)
    entry.set_text('abc')
    assert GtkUi.rev_leds_port_typed(ui) is None
    assert 'error' in entry.get_style_context().list_classes()
    assert ui._rev_leds_port_valid == DEFAULT_PORT
    entry.set_text('20777')
    assert GtkUi.rev_leds_port_typed(ui) == 20777
    assert 'error' not in entry.get_style_context().list_classes()
    assert ui.rev_leds_port.get_active() == 1
    assert GtkUi.rev_leds_port_typed(ui) is None     # unchanged
