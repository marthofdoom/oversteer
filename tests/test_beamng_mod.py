"""Countersteer's BeamNG mod: the CSB1 packet, the zip built from the shipped files and its install."""
import math
import os
import zipfile

import pytest

from oversteer import beamng_mod, telemetry
from oversteer.telemetry_formats import CSB_SIZE, csb1_packet, decode_sample

WHEELS = [(4000.0 + i, 0.1 * i, -0.2 * i, 5.0 * i) for i in range(4)]


def test_csb1_round_trip():
    data = csb1_packet(7, 12.5, -33.25, 0.5, 21.0, WHEELS)
    assert len(data) == CSB_SIZE == 96
    sample = decode_sample(data)
    assert sample.game == 'beamng-ffb'
    assert sample.rack_force == pytest.approx(-33.25)
    assert sample.speed == pytest.approx(21.0)
    assert sample.game_time == pytest.approx(12.5)
    assert sample.packet_id == 7
    assert sample.tyre_load == pytest.approx(tuple(w[0] for w in WHEELS))
    assert sample.tyre_slip == pytest.approx(tuple(w[1] for w in WHEELS))
    assert sample.tyre_side_slip == pytest.approx(tuple(w[2] for w in WHEELS))
    assert sample.tyre_slip_energy == pytest.approx(tuple(w[3] for w in WHEELS))


def test_csb1_missing_wheels_and_bad_packets():
    sample = decode_sample(csb1_packet(1, 1.0, 5.0, 0.0, 3.0, WHEELS, mask=7))
    assert sample.rack_force == 5.0 and sample.tyre_load is None
    assert decode_sample(csb1_packet(1, 1.0, 5.0, 0.0, 3.0, WHEELS)[:95]) is None     # cut short
    assert decode_sample(csb1_packet(1, math.nan, 5.0, 0.0, 3.0, WHEELS)) is None
    nan = decode_sample(csb1_packet(1, 1.0, math.nan, 0.0, 3.0, WHEELS))
    assert nan.rack_force is None


def test_csb1_is_not_mistaken_for_outgauge():
    # same 96 bytes: the magic decides
    data = bytearray(csb1_packet(1, 1.0, 0.0, 0.0, 0.0, WHEELS))
    assert decode_sample(bytes(data)).game == 'beamng-ffb'


def test_port_is_a_preset_and_unique():
    ports = [p for p, _label in telemetry.PORT_PRESETS]
    assert 5320 in ports and len(set(ports)) == len(ports)
    assert 5320 in telemetry.held_ports(True, None)


def _source(tmp_path, version):
    src = tmp_path / 'src'
    lua = src / beamng_mod.LUA_PATH
    lua.parent.mkdir(parents=True, exist_ok=True)
    lua.write_text('local M = {}\nlocal MOD_VERSION = ' + str(version) + '\nreturn M\n')
    return str(src)


def test_shipped_source_has_a_version_and_the_zip_holds_it():
    src = beamng_mod.find_source()
    assert src and beamng_mod.source_version(src) >= 1
    data = beamng_mod.build_zip(src)
    assert data == beamng_mod.build_zip(src)               # reproducible
    with zipfile.ZipFile(__import__('io').BytesIO(data)) as z:
        assert z.namelist() == [beamng_mod.LUA_PATH]
    assert beamng_mod.zip_version(data) == beamng_mod.source_version(src)


def _fake_home(tmp_path):
    home = tmp_path / 'home'
    (home / '.local/share/BeamNG/BeamNG.drive/current').mkdir(parents=True)
    lib = tmp_path / 'lib'
    prefix = lib / 'steamapps/compatdata/284160/pfx/drive_c/users/steamuser/AppData/Local/BeamNG.drive/current'
    prefix.mkdir(parents=True)
    return str(home), str(lib)


def test_user_folders_native_and_proton(tmp_path):
    home, lib = _fake_home(tmp_path)
    found = beamng_mod.user_folders(home, {}, [lib, str(tmp_path / 'nolib')])
    assert [label for label, _ in found] == ['native', 'Proton']
    assert found[0][1] == os.path.join(home, '.local/share/BeamNG/BeamNG.drive/current/mods')
    assert found[1][1].endswith('AppData/Local/BeamNG.drive/current/mods')
    assert beamng_mod.user_folders(str(tmp_path / 'empty'), {}, []) == []


def test_install_never_overwrites_newer(tmp_path, monkeypatch):
    home, lib = _fake_home(tmp_path)
    src = _source(tmp_path, 3)
    monkeypatch.setattr(beamng_mod, 'find_source', lambda datadir=None: src)

    def run():
        return beamng_mod.install(home=home, environ={}, libraries=[lib])
    first = run()
    assert [r[2] for r in first['results']] == [beamng_mod.INSTALLED] * 2 and first['version'] == 3
    dest = os.path.join(home, '.local/share/BeamNG/BeamNG.drive/current/mods', beamng_mod.ZIP_NAME)
    assert beamng_mod.zip_version(dest) == 3
    assert [r[2] for r in run()['results']] == [beamng_mod.CURRENT] * 2
    # a newer one is kept
    newer = beamng_mod.build_zip(_source(tmp_path / 'n', 9))
    with open(dest, 'wb') as f:
        f.write(newer)
    again = run()
    assert again['results'][0][2:] == (beamng_mod.NEWER, 9)
    assert beamng_mod.zip_version(dest) == 9
    # an older one and a broken one are replaced
    for old in (beamng_mod.build_zip(_source(tmp_path / 'o', 1)), b'not a zip'):
        with open(dest, 'wb') as f:
            f.write(old)
        assert run()['results'][0][2] == beamng_mod.UPDATED
        assert beamng_mod.zip_version(dest) == 3
    assert not os.path.exists(dest + '.tmp')


def test_install_without_a_game_folder_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(beamng_mod, 'find_source', lambda datadir=None, src=_source(tmp_path, 1): src)
    monkeypatch.setattr(beamng_mod.steam_build, 'in_flatpak', lambda: False)
    report = beamng_mod.install(home=str(tmp_path / 'none'), environ={}, libraries=[])
    assert report['results'] == [] and 'user folder' in report['error']
    monkeypatch.setattr(beamng_mod.steam_build, 'in_flatpak', lambda: True)
    report = beamng_mod.install(home=str(tmp_path / 'none'), environ={}, libraries=[])
    assert 'Flatpak' in report['error'] and 'override' in beamng_mod.summary(report)
