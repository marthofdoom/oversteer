"""The FFB tools (scripts/ffb_tools.py, ffb_situations.py, ffb-sim-match.py, ffb-wheel-model.py, calib-check.py,
new-game-check.py) on small synthetic data; no capture, wheel or user file is read."""
import gzip
import importlib.util
import json
import os
import sys

import numpy as np
import pytest

_HERE = os.path.dirname(__file__)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, '..', 'scripts', file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, os.path.join(_HERE, '..', 'scripts'))
import ffb_situations as sit  # noqa: E402
import ffb_tools as tools  # noqa: E402
match = _load('ffb_match_launcher', 'ffb-match.py')
calib = _load('calib_check_launcher', 'calib-check.py')
wheel = _load('ffb_wheel_model_launcher', 'ffb-wheel-model.py')


def test_ffb_match_and_sim_match_share_the_same_bands():
    assert match.AY is sit.AY and match.SLIP is sit.SLIP and match.SPEED is sit.SPEED
    assert match.compare is sit.compare and match.band is sit.band


def test_compare_weights_by_given_seconds_not_sample_count():
    a = {(1, 0, 0, 0): [0.5] * 10, (2, 0, 0, 0): [0.2] * 10}
    b = {(1, 0, 0, 0): [0.4] * 10, (2, 0, 0, 0): [0.1] * 10}
    rows = sit.compare(a, b, 1.0, {(1, 0, 0, 0): 3.0, (2, 0, 0, 0): 0.5}, {(1, 0, 0, 0): 2.0, (2, 0, 0, 0): 5.0})
    assert [r[0] for r in rows] == [(1, 0, 0, 0)]          # the second has only 0.5 s on one side
    assert rows[0][4] == 3.0 and rows[0][8] == 2.0
    line, mean, size = sit.summary(rows)
    assert mean == pytest.approx(0.1) and size == pytest.approx(0.1) and '1 situations' in line


def test_summary_weights_by_shared_time():
    a = {(1, 0, 0, 0): [0.6] * 5, (2, 0, 0, 0): [0.1] * 5}
    b = {(1, 0, 0, 0): [0.5] * 5, (2, 0, 0, 0): [0.3] * 5}
    secs_a = {(1, 0, 0, 0): 3.0, (2, 0, 0, 0): 1.0}
    secs_b = {(1, 0, 0, 0): 3.0, (2, 0, 0, 0): 1.0}
    _line, mean, size = sit.summary(sit.compare(a, b, 1.0, secs_a, secs_b))
    assert mean == pytest.approx((0.1 * 3 + -0.2 * 1) / 4)
    assert size == pytest.approx((0.1 * 3 + 0.2 * 1) / 4)


def test_sample_spacing_is_the_median_step():
    assert tools.sample_spacing([0, 1, 2, 3, 10]) == pytest.approx(1.0)
    assert tools.sample_spacing([0.0]) == 0.0


def test_unknown_game_says_which_rack_is_missing():
    with pytest.raises(tools.NoRack) as e:
        tools.rack_for('dirt', 'abc')
    assert "'dirt'" in str(e.value) and 'no rack' in str(e.value) and 'acr' in str(e.value)
    with pytest.raises(tools.NoRack) as e:
        tools.rack_for(None, 'abc')
    assert 'could not tell' in str(e.value)
    assert tools.rack_for('rbr')[0] == 'RbrRack'


def test_sim_match_with_an_unknown_game_exits_with_the_message(capsys, tmp_path):
    cap = tmp_path / 'x.ovcap.gz'
    cap.write_bytes(b'')
    sim = _load('ffb_sim_match_launcher', 'ffb-sim-match.py')
    import ffb_parity
    orig = ffb_parity.game_of
    ffb_parity.game_of = lambda path: 'dirt'
    try:
        assert sim.main([str(cap), '--vs', str(cap)]) == 2
    finally:
        ffb_parity.game_of = orig
    assert "no rack for it yet" in capsys.readouterr().err


def test_power_class_comes_from_the_profile_else_the_game_default(tmp_path):
    (tmp_path / 'profiles').mkdir()
    (tmp_path / 'profiles' / 'Forza.ini').write_text('[DEFAULT]\ncountersteer_power_class = rally\n')
    assert tools.profile_power_class('forza-fh', str(tmp_path)) == 'rally'
    assert tools.profile_power_class('acr', str(tmp_path)) == 'rally'
    (tmp_path / 'profiles' / 'Forza.ini').write_text('[DEFAULT]\nmode = G29\n')
    assert tools.profile_power_class('forza-fh', str(tmp_path)) == 'street'


def test_channel_report_counts_what_is_present():
    class S:
        speed = 10.0
        vel = (10.0, 0.0, 0.0)
        tyre_load = None
        brake = 0.0
    share, present, missing = tools.channel_report([S(), S()], ('speed', 'vel', 'tyre_load', 'brake', 'accel'))
    assert present == ['speed', 'vel', 'brake'] and missing == ['tyre_load', 'accel']
    assert share['speed'] == 1.0


def test_channels_are_all_sample_fields():
    from oversteer import telemetry_formats as tf
    fields = set(tf.Sample.__dataclass_fields__) if hasattr(tf.Sample, '__dataclass_fields__') else set(tf.Sample.__slots__)
    assert set(tools.CHANNELS) <= fields


RESULT = {'knots': [0.0, 0.5, 1.0], 'curve': [0.0, 0.4, 1.0], 'lut': [0, 16383, 32767], 'deadband': 0, 'friction': 0.05,
          'date': '2026-01-01T00:00:00'}


def test_delivered_follows_the_curve_times_the_peak():
    # an identity table: asking 1.54 of a 3.08 peak is half the share, the curve there is 0.4 of the peak
    assert tools.delivered(1.54, RESULT, RESULT, 3.08) == pytest.approx(0.4 * 3.08, rel=1e-3)
    assert tools.delivered(10.0, RESULT, RESULT, 3.08) == pytest.approx(3.08)            # an ask over the peak is the peak
    # a build that asked against a peak of 3.5 sends a smaller share for the same Nm
    assert tools.delivered(1.54, RESULT, RESULT, 3.08, 3.5) < tools.delivered(1.54, RESULT, RESULT, 3.08)


def test_weight_check_is_the_curve_net_of_friction_against_the_load():
    motor, load = tools.weight_check(2.0, 0.5, RESULT, 3.08)
    assert motor == pytest.approx((0.4 - 0.05) * 3.08)
    assert load == pytest.approx(2.0 * 9.80665 * 0.1395)
    assert tools.weight_check(1.0, 1.0, RESULT, 3.08, radius=0.2)[1] == pytest.approx(9.80665 * 0.2)


def test_wheel_model_reads_a_store_and_prints_the_table(tmp_path, capsys):
    store = tmp_path / 'wheel_calibration.json'
    store.write_text(json.dumps({'a': {'result': dict(RESULT, date='2026-01-01')}, 'b': {'result': dict(RESULT, date='2026-02-01')}}))
    assert tools.stored_result(str(store))[0] == 'b'
    assert tools.stored_result(str(store), 'a')[0] == 'a'
    with pytest.raises(ValueError):
        tools.stored_result(str(store), 'zz')
    with pytest.raises(ValueError):
        tools.stored_result(str(tmp_path / 'none.json'))
    wheel.LOG = str(tmp_path / 'log.txt')
    assert wheel.main(['--store', str(store), '--asked', '1.54', '--weight', '2', '0.5']) == 0
    out = capsys.readouterr().out
    assert '1.54' in out and '2.00 kg' in out and os.path.exists(wheel.LOG)


def _run(path, data):
    with gzip.open(path, 'wt') as f:
        json.dump(data, f)


def test_calib_check_survives_a_run_with_no_result_and_empty_records(tmp_path, capsys):
    empty = {'format': 1, 'date': '2026-01-01T00:00:00', 'outcome': 'permission', 'message': 'no', 'proxied': False, 't0': 0.0,
             'summary': {'kick_accels': {'1': [], '-1': []}}, 'log': [],
             'rec': {'cmd_t': [], 'cmd': [], 'pos_t': [], 'pos': []}}
    _run(tmp_path / 'calibration-1-permission.json.gz', empty)
    short = dict(empty, outcome='grabbed', log=[[1.0, 'odd push 10 against 50']],
                 rec={'cmd_t': [0.0, 1.0], 'cmd': [0.0, 0.5], 'pos_t': [0.0, 0.5, 1.0], 'pos': [0.0, 1.0, 2.0]})
    _run(tmp_path / 'calibration-2-grabbed.json.gz', short)
    calib.LOG = str(tmp_path / 'log.txt')
    assert calib.main(['--dir', str(tmp_path), 'calibration-1-permission', 'calibration-2-grabbed']) == 0
    out = capsys.readouterr().out
    assert 'no pulses recorded' in out and 'refit refused' in out and 'odd push 10 against 50' in out
    assert 'too little held-level data' in out


def test_calib_check_default_is_the_newest_run_and_an_empty_folder_exits(tmp_path, capsys):
    calib.LOG = str(tmp_path / 'log.txt')
    assert calib.main(['--dir', str(tmp_path)]) == 2
    data = {'date': 'd', 'outcome': 'ok', 'summary': {}, 'log': [], 'rec': {}}
    _run(tmp_path / 'calibration-1-ok.json.gz', data)
    assert calib.main(['--dir', str(tmp_path)]) == 0
    assert 'calibration-1-ok' in capsys.readouterr().out
    (tmp_path / 'bad.json.gz').write_bytes(b'not gzip')
    assert calib.main(['--dir', str(tmp_path), 'bad']) == 0               # unreadable is a line, not a crash
    assert 'cannot read' in capsys.readouterr().out


def test_pulse_tables_read_a_synthetic_run():
    t = np.arange(0, 3, 0.001)
    pos = 100 * np.sin(t)                                            # a moving wheel
    cmd_t = [0.0, 0.5, 0.6, 1.0, 1.1, 2.0]
    cmd = [0.0, 0.35, 0.0, -0.5, 0.5, 0.0]
    data = {'rec': {'cmd_t': cmd_t, 'cmd': cmd, 'pos_t': list(t), 'pos': list(pos)}, 't0': 0.0, 'summary': {}}
    seen, runups = tools.pulse_tables(data)
    assert len(runups) == 1 and runups[0][1] == pytest.approx(0.1)
    assert 0.5 in seen and len(seen[0.5]) == 1                       # the reversal from -0.5 to +0.5 is a pulse
