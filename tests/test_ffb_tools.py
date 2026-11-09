"""The FFB tools (scripts/ffb_tools.py, ffb_situations.py, ffb-sim-match.py, ffb-force-curve.py, ffb-wheel-model.py, calib-check.py,
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


# --- ffb-force-curve ------------------------------------------------------------------------------------------------

def test_force_curve_bins_by_fyf_and_theta_with_real_seconds():
    nm = [1.0, 1.0, 2.0, 5.0, 3.0]
    fyf = [100, 250, 700, 1e5, 700]            # 0-300, 0-300, 600-1000, 9000+, 600-1000
    theta = [0.1, 0.1, 0.5, 0.1, np.nan]       # the last has no theta: left out
    cells, secs = sit.curve_cells(nm, fyf, theta, 0.5)
    assert cells == {(0, 0): [1.0, 1.0], (2, 2): [2.0], (len(sit.FYF) - 2, 0): [5.0]}
    assert secs[(0, 0)] == pytest.approx(1.0) and secs[(2, 2)] == pytest.approx(0.5)
    assert sit.curve_label(sit.FYF, len(sit.FYF) - 2) == '9000+' and sit.curve_label(sit.THETA, 1) == '0.2-0.4'


def test_force_curve_rows_need_the_seconds_on_both_sides_and_rank_by_gap_times_root_seconds():
    a = ({(0, 0): [1.0], (1, 0): [2.0], (2, 0): [3.0]}, {(0, 0): 4.0, (1, 0): 9.0, (2, 0): 0.5})
    b = ({(0, 0): [0.8], (1, 0): [1.9], (2, 0): [1.0]}, {(0, 0): 4.0, (1, 0): 9.0, (2, 0): 9.0})
    rows = sit.curve_rows(a, b, 1.0)
    assert sorted(r[0] for r in rows) == [(0, 0), (1, 0)]
    assert (2, 0) not in [r[0] for r in rows]            # 0.5 s on one side
    by = {r[0]: r for r in rows}
    assert by[(0, 0)][5] == pytest.approx(0.2) and by[(0, 0)][6] == pytest.approx(25.0)
    assert rows[0][0] == (0, 0)                          # 0.2 x 2 beats 0.1 x 3
    tiny = sit.curve_rows(({(0, 0): [0.02]}, {(0, 0): 2.0}), ({(0, 0): [0.01]}, {(0, 0): 2.0}), 1.0)
    assert np.isnan(tiny[0][6])                          # a B median under MIN_PCT_NM gives no percent


def test_force_curve_summary_weights_by_shared_time_and_picks_the_low_slip_low_force_cells():
    a = ({(1, 0): [1.0], (2, 6): [2.0]}, {(1, 0): 3.0, (2, 6): 1.0})
    b = ({(1, 0): [0.5], (2, 6): [1.0]}, {(1, 0): 3.0, (2, 6): 1.0})
    rows = sit.curve_rows(a, b, 1.0)
    allc, low = sit.curve_summary(rows, knee_nm=1.5)
    assert '2 cells' in allc and '+100.0 %' in allc
    assert '1 cells' in low and '3 s' in low             # theta band 6 (1.5-3) and 2.0 Nm are outside


def test_theta_table_has_one_definition_per_game():
    assert set(tools.THETA_TABLE) == set(tools.RACKS)
    assert tools.THETA_TABLE['forza-fh'][1] == 0.7 and tools.THETA_TABLE['acr'][1] == 'stretch'
    from oversteer import ffb_engine
    assert tools.THETA_TABLE['rbr'][1]() == ffb_engine.RbrRack.DERIVED_SLIP_PEAK

    class S:
        slip_angle = (0.3, -0.5)
        tyre_slip_angle = (0.1, -0.3)
    assert tools.THETA_TABLE['forza-fh'][0](S, None) == pytest.approx(0.4)
    assert tools.THETA_TABLE['acr'][0](S, None) == pytest.approx(0.2)
    S.slip_angle = None
    assert tools.THETA_TABLE['forza-fh'][0](S, None) is None


def test_acr_peaks_finds_the_slip_where_grip_tops_out_per_stretch():
    rng = np.random.default_rng(1)
    n = 4000
    t = np.concatenate([np.linspace(0, 59, n), np.linspace(61, 119, n)])
    sa = rng.uniform(0.0, 0.3, (2 * n, 2))
    fz = np.full((2 * n, 2), 3000.0)
    fy = fz * np.minimum(sa / 0.1, 1.0)                  # grip tops out at 0.1 rad
    peaks = tools.acr_peaks(t, sa, fy, fz)
    assert peaks[0] == peaks[n - 1] and 0.1 <= peaks[0] <= 0.13
    assert peaks[-1] == pytest.approx(peaks[0])
    # a stretch with too little data takes the median of the others'
    t2 = np.concatenate([t, np.array([130.0, 131.0])])
    p2 = tools.acr_peaks(t2, np.vstack([sa, sa[:2]]), np.vstack([fy, fy[:2]]), np.vstack([fz, fz[:2]]))
    assert p2[-1] == pytest.approx(peaks[0])
    assert np.isnan(tools.acr_peaks([0.0, 1.0], sa[:2], fy[:2], fz[:2])).all()


def test_set_overrides_a_module_constant_and_a_class_attribute_and_restores_them():
    from oversteer import ffb_engine, ffb_forza
    before = (ffb_forza.TP_S_HALF, ffb_engine.RbrRack.DERIVED_SLIP_PEAK)
    with tools.overrides(['ffb_forza.TP_S_HALF=0.4', 'RbrRack.DERIVED_SLIP_PEAK=0.3']) as lines:
        assert ffb_forza.trail(0.4) == pytest.approx(ffb_forza.TP0 / 2)         # reaches the function that reads it
        assert tools.THETA_TABLE['rbr'][1]() == 0.3                             # and the theta table
        assert any(l.startswith('SET ffb_forza.TP_S_HALF = 0.4') for l in lines)
    assert (ffb_forza.TP_S_HALF, ffb_engine.RbrRack.DERIVED_SLIP_PEAK) == before
    with pytest.raises(RuntimeError):
        with tools.overrides(['ffb_forza.TP_S_HALF=0.4']):
            raise RuntimeError
    assert ffb_forza.TP_S_HALF == before[0]


def test_set_refuses_unknown_names_non_numbers_and_bad_values_before_setting_anything():
    from oversteer import ffb_forza
    shipped = ffb_forza.TP_S_HALF
    for spec in ('ffb_forza.NOPE=1', 'nomodule.X=1', 'ffb_forza.trail=1', 'ffb_forza.TP_S_HALF', 'ffb_forza.TP_S_HALF=abc',
                 'ffb_forza.TP_S_HALF=nan', 'TP_S_HALF=1', 'RbrRack.NOPE=1', 'ffb_forza.CASTER.x=1'):
        with pytest.raises(ValueError):
            with tools.overrides(['ffb_forza.TP_S_HALF=0.4', spec]):
                pass
        assert ffb_forza.TP_S_HALF == shipped


def test_set_warns_where_a_constant_is_copied_so_the_override_does_not_reach():
    with tools.overrides(['RbrRack.ALIGN_SPEED=5']) as lines:
        assert any('ForzaRack' in l and 'not reached' in l for l in lines)       # ForzaRack.ALIGN_SPEED = RbrRack.ALIGN_SPEED
    with tools.overrides(['ffb_forza.DI=0.5']) as lines:
        assert any('front_lateral' in l and 'fixed as a default' in l for l in lines)
    with tools.overrides(['ffb_forza.TP_S_HALF=0.4']) as lines:
        assert not any('WARNING' in l for l in lines)


def test_effective_trail_is_nm_per_fyf_in_mm_by_theta():
    side = {'nm': np.array([1.0, 1.0, 2.0]), 'fyf': np.array([1000.0, 2000.0, 100.0]),
            'theta': np.array([0.1, 0.1, 0.1]), 'dt': np.full(3, 0.5)}
    lines = tools.trail_lines(side, side)
    row = [l for l in lines if l.startswith('0-0.2')][0]
    assert '1.000' in row or '0.750' in row               # median of 1.0 mm and 0.5 mm (the 100 N sample is left out)
    assert '0.750' in row


# --- ffb-stages -------------------------------------------------------------------------------------------------------

def _stage_cols(ay, speed, scale=1.0, n=200):
    """n samples (0.01 s each) of every stage at ay / speed; stage values are scale times a constant per stage."""
    cols = {k: np.full(n, float(i + 1) * scale) for i, (k, _n) in enumerate(sit.STAGES)}
    cols['ay'], cols['speed'] = np.full(n, float(ay)), np.full(n, float(speed))
    return cols


def test_stage_cells_bin_by_ay_and_speed_with_real_seconds():
    cols = {k: np.concatenate([_stage_cols(0.5, 10)[k], _stage_cols(5.5, 25)[k], _stage_cols(13.0, 40)[k]]) for k, _n in sit.STAGES}
    meds, secs = sit.stage_cells(cols, 0.5)
    assert sorted(meds) == [(0, 0), (5, 1), (11, 2)]
    assert secs[(0, 0)] == pytest.approx(100.0) and meds[(5, 1)]['fyf'] == 4.0
    assert sit.label(sit.STAGE_AY, 11) == '12-15' and sit.label(sit.STAGE_AY, 12) == '15+'
    assert sit.label(sit.STAGE_SPEED, 2) == '30+'


def test_stage_summary_gives_the_ratio_per_stage_for_all_low_and_high_ay():
    def side(scale_low, scale_high):
        c = {k: np.concatenate([_stage_cols(2.5, 25, scale_low)[k], _stage_cols(7.5, 25, scale_high)[k]]) for k, _n in sit.STAGES}
        c['ay'] = np.concatenate([np.full(200, 2.5), np.full(200, 7.5)])
        c['speed'] = np.full(400, 25.0)
        return sit.stage_cells(c, 0.01)
    a, b = side(2.0, 1.0), side(1.0, 1.0)
    low = sit.stage_ratios(a, b, 1.0, lambda k: k[0] < sit.STAGE_SPLIT)
    high = sit.stage_ratios(a, b, 1.0, lambda k: k[0] >= sit.STAGE_SPLIT)
    allr = sit.stage_ratios(a, b, 1.0)
    assert low['kingpin'][3] == pytest.approx(2.0) and high['kingpin'][3] == pytest.approx(1.0)
    assert allr['kingpin'][3] == pytest.approx(1.5)         # (2 + 1) / 2, the two cells having the same seconds
    text = sit.stage_summary(a, b, 1.0)
    assert [l.split('|')[0].strip() for l in text[3:]] == [n.strip() for _k, n in sit.STAGES]
    assert '|ay| < 5' in text[1] and '|ay| >= 5' in text[1]


def test_stage_ratios_drop_cells_short_of_time_and_stages_without_values():
    a = sit.stage_cells(_stage_cols(2.5, 25, 1.0, n=50), 0.01)        # 0.5 s only
    b = sit.stage_cells(_stage_cols(2.5, 25, 1.0, n=50), 0.01)
    assert all(v is None for v in sit.stage_ratios(a, b, 1.0).values())
    cols = _stage_cols(2.5, 25)
    cols['mass'] = np.full(200, np.nan)
    a = sit.stage_cells(cols, 0.01)
    assert sit.stage_ratios(a, a, 1.0)['mass'] is None and sit.stage_ratios(a, a, 1.0)['fyf'][3] == 1.0


def test_mass_is_refused_for_acr_and_ratio_is_not():
    tools.check_overrides('acr', None, 13.0)
    tools.check_overrides('rbr', 1250.0, 13.0)
    with pytest.raises(ValueError, match='ACR'):
        tools.check_overrides('acr', 1250.0, None)
    with pytest.raises(ValueError):
        tools.check_overrides('rbr', -5.0, None)


def test_a_pinned_ratio_and_mass_survive_set_car_and_the_learners():
    from oversteer import car_data
    rack, _fresh, _feed = tools._build_rack('rbr', 'rally')
    tools.pin_values('rbr', rack, 1250.0, 13.0)
    rack.set_car('rbr/106')                                    # restores the table's ratio (10.58)
    assert rack.ratio != 13.0 and rack.DERIVED_RATIO == 13.0
    tools.pin_values('rbr', rack, 1250.0, 13.0)
    assert rack.ratio == 13.0 and rack.DERIVED_RATIO == 13.0
    with tools.rbr_mass(1250.0):
        assert car_data.entry('rbr/106')['mass_kg'] == 1250.0
    assert car_data.entry('rbr/106')['mass_kg'] != 1250.0
    forza, _f, _g = tools._build_rack('forza-fh', 'street')
    forza.reset(None)
    tools.pin_values('forza-fh', forza, 1250.0, 11.0)
    assert forza.mass == 1250.0 and forza.ratio == 11.0
    forza.reset(None)                                          # back to the prior mass and STEER_RATIO
    assert forza.mass != 1250.0
    tools.pin_values('forza-fh', forza, 1250.0, 11.0)
    assert forza.mass == 1250.0


def test_acr_true_mass_is_the_slope_of_the_four_tyres_fy_on_ay():
    t = np.arange(2000) * 0.01
    ay = 6.0 * np.sin(t * 0.7) + np.where(np.sin(t * 0.7) >= 0, 3.0, -3.0)      # mostly |ay| > 3
    cols = {'t': t, 'ay_s': ay, 'yaw': np.zeros_like(t), 'fy_all': 1200.0 * ay, 'fy_front': 700.0 * ay}
    m, mf, share, n = tools.acr_true_mass(cols)
    assert m == pytest.approx(1200.0) and mf == pytest.approx(700.0) and share == pytest.approx(700 / 1200)
    cols['yaw'] = 5.0 * t * 0 + np.sin(t * 40.0) * 3.0                          # a violent yaw acceleration: nothing qualifies
    assert tools.acr_true_mass(cols) is None


def test_components_fit_recovers_a_known_coefficient_per_band():
    rng = np.random.default_rng(3)
    n = 4000
    ay, yaw_acc = rng.normal(size=n), rng.normal(size=n)
    force = 0.8 * tools.band_of(ay, 2, 10) - 0.5 * tools.band_of(yaw_acc, 2, 10) + 0.001 * rng.normal(size=n)
    fit, full = tools.joint_fit(force, {'ay': tools.band_of(ay, 2, 10), 'yawacc': tools.band_of(yaw_acc, 2, 10)})
    assert fit['ay'][0] == pytest.approx(0.8, abs=0.02) and fit['yawacc'][0] == pytest.approx(-0.5, abs=0.02)
    assert full > 0.99 and fit['yawacc'][2] > 0.1


def test_tyre_envelope_recovers_known_mu_and_peak_and_flags_unsaturated():
    rng = np.random.default_rng(5)
    x = rng.uniform(0.0, 0.3, 6000)
    y = np.minimum(x / 0.1, 1.2) * 0.9 + 0.1 * rng.uniform(0, 1, x.size)    # C ~9, p80 top ~1.16, knee at 0.12
    f = tools.envelope_fit(x, y, 0.01, tools.TYRE_BINS['rad'])
    assert f['reached'] and f['mu'] == pytest.approx(1.16, abs=0.06) and f['peak'] == pytest.approx(0.12, abs=0.04)
    assert tools.mu_band(f['mu']) == 2
    xs = rng.uniform(0.0, 0.1, 6000)                                    # never leaves the rising part
    g = tools.envelope_fit(xs, xs * 10 + 0.05 * rng.uniform(0, 1, xs.size), 0.01, tools.TYRE_BINS['rad'])
    assert g['reached'] is False


def _store_file(tmp_path, ratio=9.87):
    path = tmp_path / 'live.json'
    path.write_text(json.dumps({'acr/Test Car': {'trail': 0.0264, 'scrub': 0.0, 'ratio': ratio, 'assist': 1.0, 'lock': 0.477}}))
    return path


def test_a_seeded_store_reaches_the_replayed_rack_and_the_header(tmp_path):
    import ffb_score as fs
    path = _store_file(tmp_path)
    try:
        fs.use_store(path=str(path))
        with fs._sandbox():
            rack, _fresh, _feed = tools._build_rack('acr', 'rally')
            rack.reset('acr/Test Car')
            assert rack.ratio == pytest.approx(9.87) and rack.trail == pytest.approx(0.0264) and rack.lock == pytest.approx(0.477)
            line = tools.props_line('acr/Test Car')
        assert 'ratio 9.87 (stored)' in line and 'trail 0.0264 (stored)' in line and 'lock 0.477 (stored)' in line
        fs.use_store(empty=True)
        with fs._sandbox():
            rack, _fresh, _feed = tools._build_rack('acr', 'rally')
            rack.reset('acr/Test Car')
            assert rack.ratio == rack.STEER_RATIO and rack.ratio != 9.87
            assert '(default)' in tools.props_line('acr/Test Car')
    finally:
        fs.use_store(empty=True)


def test_the_live_store_is_copied_never_written(tmp_path):
    import ffb_score as fs
    path = _store_file(tmp_path)
    os.utime(path, (1_000_000, 1_000_000))
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    try:
        fs.use_store(path=str(path))
        with fs._sandbox() as tmp:
            rack, _fresh, _feed = tools._build_rack('acr', 'rally')
            rack.reset('acr/Test Car')
            from oversteer import ffb_carprops as cp
            cp.STORE.set_rack('acr/Test Car', 11.0, 1.0)
            cp.STORE.put('acr/Test Car', 0.03, 0.0, force=True)
            assert json.load(open(os.path.join(tmp, 'props.json')))['acr/Test Car']['ratio'] == 11.0
    finally:
        fs.use_store(empty=True)
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before


def _held_grid(opposite):
    """60 Hz grid: a held wheel, yaw rate peaking at 5 s then falling, |ay| falling; the force relaxes or (opposite) reverses."""
    n = 60 * 10
    t = np.arange(n) / 60.0
    yaw = np.exp(-((t - 5.0) / 1.0) ** 2) * 0.8 * np.ones(n)
    ay = 8.0 * np.exp(-((t - 4.5) / 1.0) ** 2) * np.ones(n)
    force = 2.0 * np.where(t < 5.0, 1.0, np.exp(-(t - 5.0) / 2.0))
    if opposite:
        force = force - 3.0 * np.clip((t - 5.0) / 0.3, 0, 1) * (t >= 5.0)
    return {'t': t, 'yaw': yaw, 'ay': ay, 'F': force, 'ok': np.ones(n, bool), 'steer': np.full(n, 0.2),
            'yawacc': np.gradient(yaw) * 60.0, 'steerrate': np.zeros(n), 'speed': np.full(n, 30.0),
            'brake': np.zeros(n), 'throttle': np.zeros(n)}


def test_settle_finds_a_held_wheel_and_the_opposite_pull():
    comp = _load('ffb_components_launcher', 'ffb-components.py')
    plain = comp.settle_events(_held_grid(False))
    assert plain['n'] == 1 and plain['force'][0.3][0] > 0 and plain['force'][0.0][0] == pytest.approx(2.0, abs=0.1)
    opp = comp.settle_events(_held_grid(True))
    assert opp['n'] == 1 and opp['force'][0.5][0] < 0
    g = _held_grid(False)
    g['steer'] = 0.2 + 0.3 * np.sin(np.arange(len(g['t'])) / 60.0 * 6)             # the driver keeps steering: no held settle
    assert comp.settle_events(g)['n'] == 0


def test_inventory_marks_missing_present_and_absent_forces():
    inv = _load('ffb_inventory_launcher', 'ffb-inventory.py')
    rng = np.random.default_rng(7)
    n = 6000
    g = {k: rng.normal(size=n) for k in ('ay', 'yawacc', 'steerrate', 'brake', 'throttle', 'load_sum')}
    g.update(t=np.arange(n) / 60.0, ok=np.ones(n, bool), speed=np.full(n, 30.0), steer=np.zeros(n), yaw=np.zeros(n))
    g['F'] = 1.0 * tools.band_of(g['ay'], 10, 60) + 0.001 * rng.normal(size=n)          # follows ay only
    res = inv.analyse('test', g, ['fx_diff', 'mz_front'])
    assert inv.status(res['rows']['ay']) == 'in force'
    assert inv.status(res['rows']['fx_diff']) == 'MISSING IN DATA' and inv.status(res['rows']['mz_front']) == 'MISSING IN DATA'
    assert inv.status(res['rows']['throttle']) == 'IN DATA, NOT IN FORCE'
    text = '\n'.join(inv.table([('x', res)]))
    assert 'MISSING IN DATA' in text and 'held-wheel settle' in text
