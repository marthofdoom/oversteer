"""The hands-off wheel calibration (oversteer/wheel_calibration.py, oversteer/wheel_calib.py) against a simulated
wheel (tests/wheel_sim.py): nothing here opens a real device, sysfs or evdev."""
import gzip
import json
import math
import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from oversteer import wheel_calib as wc
from oversteer import wheel_calibration as wcal
from oversteer.wheel_calibration import Aborted, Calibration

from .wheel_sim import SimWheel


DEFAULT_ATTRS = {'game_ffb_level': '100', 'game_const_scale': '100', 'ext_force': '0', 'ext_force_cap': '80',
                 'ffb_lut': 'identity', 'ffb_deadband': '0', 'ffb_dither': '0', 'ffb_soft_clip': '0'}


def linearity_error(sim, result, low=0.05):
    """Largest |torque - request| (share of full) over the range, with the correction as the driver applies it
    (the LUT, then the deadband lift). The wheel's 8-bit step comes on top (dither's part, not the LUT's)."""
    worst = 0.0
    for i in range(int(low * 200), 201):
        x = i / 200
        u = wc.shaped(x * wc.FULL, result['lut'], result['deadband']) / wc.FULL
        worst = max(worst, abs(sim.torque_of(u) - x))
    return worst


def uncorrected_error(sim, low=0.05):
    return max(abs(sim.torque_of(i / 200) - i / 200) for i in range(int(low * 200), 201))


def assert_put_back(sim, attrs=None, gain=110, autocenter=30, rng=540):
    """The run left the wheel as it found it: force 0 last, the game's force at 100, the shaping, cap, gain,
    centring spring and range as before."""
    assert sim.last('ext_force') == '0'
    assert sim.last('game_ffb_level') == '100'
    expected = dict(DEFAULT_ATTRS, **(attrs or {}))
    assert sim.attrs == expected
    assert (sim.gain, sim.autocenter, sim.range) == (gain, autocenter, rng)
    # nothing written after the final ext_force 0 / game_ffb_level 100
    names = [n for _t, n, _v in sim.writes]
    assert names[-1] == 'game_ffb_level' and names[-2] == 'ext_force'


def assert_safe_run(sim, cal):
    """Limits that hold whatever happened: forces above 60 % at most 150 ms (+ one tick), never within the
    hard margin of the lock, and every test force played with the game muted and the shaping off."""
    assert all(b - a <= Calibration.PULSE_MAX_S + 0.0045 for a, b in sim.high_runs)
    assert sim.max_abs_theta < cal.lock - Calibration.HARD_MARGIN_DEG
    assert sim.impure_ticks == 0


@pytest.fixture(scope='module')
def g29_like():
    """A G29-like wheel: 5000 deg/s^2 at full scale, 6 % dead zone, a curve strong at the bottom, friction,
    12 ms latency, a 4096-count encoder, ext_force turning it the negative way."""
    sim = SimWheel()
    cal = Calibration(sim)
    result = cal.run()
    return sim, cal, result


def test_the_correction_linearises_a_simulated_wheel(g29_like):
    sim, cal, result = g29_like
    assert uncorrected_error(sim) > 0.25
    assert linearity_error(sim, result) < 0.03
    lut = result['lut']
    assert len(lut) == wc.LUT_POINTS and lut[0] == 0 and lut[-1] == wc.FULL
    assert all(b >= a for a, b in zip(lut, lut[1:]))
    assert wcal.valid_lut(lut)
    # the model of the wheel
    assert abs(result['deadband'] / wc.FULL - sim.dead_zone) < 0.01
    assert abs(result['fit_latency_ms'] - sim.latency * 1000) <= 2
    assert result['latency_ms'] >= sim.latency * 1000            # command to the first change, so later
    assert abs(result['a_fs'] * sim.J - 1) < 0.1
    assert abs(result['inertia'] / sim.J - 1) < 0.1
    assert abs(result['friction'] / sim.coulomb - 1) < 0.3
    assert 0 < result['damping'] < 5 * sim.b
    assert abs(result['encoder_deg'] / sim.q - 1) < 0.05
    assert sim.dead_zone < result['breakaway'] < sim.dead_zone + 0.1
    assert len(result['breakaway_by_angle']) == 2 + 2 * Calibration.BREAKAWAY_ANGLES
    # strong at the bottom: the LUT is flat there, the 8-bit steps get coarse and dither is recommended
    assert result['dither'] is True
    assert result['range'] == 900
    assert 90 < result['duration_s'] < 180


def test_the_run_stays_within_its_limits_and_puts_everything_back(g29_like):
    sim, cal, result = g29_like
    assert_safe_run(sim, cal)
    assert sim.max_abs_omega < Calibration.V_ABORT
    assert sim.max_abs_theta < cal.lock - Calibration.LOCK_MARGIN_DEG
    assert sim.max_motor > 0.95                                  # full scale was played, in pulses
    assert_put_back(sim)
    # muted and identity before the first force, at 100 % gain, cap 100 and the full range
    first_force = next(i for i, (_t, n, v) in enumerate(sim.writes) if n == 'ext_force' and v != '0')
    before = {n: v for _t, n, v in sim.writes[:first_force]}
    assert before['game_ffb_level'] == '0' and before['ffb_lut'] == 'identity' and before['ext_force_cap'] == '100'
    assert before['gain'] == 100 and before['autocenter'] == 0 and before['range'] == 900
    assert sim.closed


@pytest.mark.parametrize('params, tolerance', [
    (dict(curve=0.0, ext_sign=1), 0.02),                                   # linear past its dead zone
    (dict(curve=-2.0, dead_zone=0.03, inertia=1 / 8000), 0.03),            # weak at the bottom
    (dict(inertia=1 / 15000, latency=0.025), 0.05),                        # strong, slow to answer
    (dict(inertia=1 / 1500, coulomb=0.1, static=0.14, curve=0.0), 0.03),   # heavy, high friction
    (dict(dead_zone=0.0, encoder_deg=900 / 65536), 0.03),                  # no dead zone, fine encoder
    (dict(max_range=270, range=270, stop_deg=135), 0.03),                  # a small range
    (dict(dead_zone=0.0, coulomb=0.08, static=0.1), 0.03),                 # friction, no dead zone...
    (dict(dead_zone=0.08, coulomb=0.01, static=0.015), 0.03),              # ...and the other way round
])
def test_other_wheels(params, tolerance):
    sim = SimWheel(**params)
    cal = Calibration(sim)
    result = cal.run()
    assert linearity_error(sim, result) < tolerance
    # (friction is not taken for dead zone; a curve weak at the bottom blurs where it starts)
    assert abs(result['deadband'] / wc.FULL - sim.dead_zone) < (0.015 if params.get('curve', 3.0) < 0 else 0.01)
    assert_safe_run(sim, cal)
    assert_put_back(sim, rng=sim._max_range if params.get('range') else 540)


def test_starting_close_to_the_lock_centres_the_wheel_first():
    sim = SimWheel(start_deg=435.0, game_spring=0.0, autocenter=0)
    cal = Calibration(sim)
    result = cal.run()
    assert result['near_lock_events'] == 1
    assert linearity_error(sim, result) < 0.03
    assert sim.max_abs_theta < cal.lock - Calibration.HARD_MARGIN_DEG
    assert_put_back(sim, autocenter=0)


# --- abort paths: each ends with ext_force 0 and the game's force back ---

@pytest.mark.parametrize('at', [1.0, 8.0, 40.0, 100.0])
def test_a_hand_on_the_wheel_stops_the_run(at):
    sim = SimWheel()
    sim.grab(at)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason in ('grabbed', 'stalled')
    # noticed within a few pulses; in the first seconds (no pushes to compare with yet) by the first moves
    assert sim.t - sim.grab_at < (6.0 if at < 3 else 3.0)
    assert_safe_run(sim, cal)
    assert_put_back(sim)


@pytest.mark.parametrize('at', [0.2, 5.0, 60.0])
def test_unplugging_stops_the_run(at):
    sim = SimWheel()
    sim.unplug(at)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'unplugged'
    assert sim.t - sim.unplug_at < 0.01
    # the writes fail (the driver starts from its defaults when the wheel is back), but they were all tried,
    # ext_force 0 and game_ffb_level 100 last
    tried = [(n, v) for _t, n, v in sim.attempts if _t >= sim.unplug_at]
    assert tried[-2:] == [('ext_force', '0'), ('game_ffb_level', '100')]
    assert ('ext_force', '0') in tried[:2]
    assert_safe_run(sim, cal)


def test_cancel_stops_the_run_at_once():
    sim = SimWheel()
    seen = []

    def progress(fraction, text):
        seen.append(fraction)
        if fraction > 0.3:
            cal.cancel()

    cal = Calibration(sim, progress)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'cancelled'
    assert max(seen) < 0.4
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_cancel_from_another_thread():
    """As the window does it (Cancel, close, quit): the run, on its own thread, stops and puts the wheel back."""
    sim = SimWheel()
    cal = Calibration(sim)
    errors = []

    def run():
        try:
            cal.run()
        except Aborted as e:
            errors.append(e.reason)

    thread = threading.Thread(target=run)
    thread.start()
    while sim.t < 110 and thread.is_alive():
        threading.Event().wait(0.001)
    cal.cancel()
    thread.join(10)
    assert not thread.is_alive()
    assert errors == ['cancelled']
    assert_put_back(sim)


def test_quitting_cancels_a_running_calibration():
    """The process's exit hook cancels every run still going and waits for its finally."""
    sim = SimWheel()
    cal = Calibration(sim)
    done = []

    def run():
        try:
            cal.run()
        except Aborted as e:
            done.append(e.reason)

    thread = threading.Thread(target=run)
    thread.start()
    while sim.t < 105 and thread.is_alive():
        threading.Event().wait(0.001)
    wcal._cancel_all()
    thread.join(10)
    assert done == ['cancelled']
    assert not wcal._running
    assert_put_back(sim)


def test_getting_close_to_the_lock_brakes_centres_and_finally_stops(monkeypatch):
    # pulses that would start inside the margin (as if the range were wrong)
    monkeypatch.setattr(Calibration, '_shot_start', lambda self, d: -d * (self.lock - 20.0))
    sim = SimWheel()
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'near_lock'
    assert cal.near_lock_events == Calibration.NEAR_LOCK_LIMIT
    events = [t for t, what in cal.log if what.startswith('near lock')]
    assert len(events) == Calibration.NEAR_LOCK_LIMIT
    assert sim.max_abs_theta < cal.lock - Calibration.HARD_MARGIN_DEG
    assert abs(sim.theta) < 20                                   # centred before it stopped
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_too_fast_stops_the_run(monkeypatch):
    monkeypatch.setattr(Calibration, 'V_ABORT', 150.0)
    sim = SimWheel()
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'overspeed'
    assert sim.last('ext_force') == '0'
    assert_put_back(sim)


def test_the_time_limit_stops_the_run(monkeypatch):
    monkeypatch.setattr(Calibration, 'TOTAL_S', 20.0)
    sim = SimWheel()
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'timeout'
    assert_put_back(sim)


def _run_cut_after(force_units, cancel=False):
    """A run whose time limit falls (or which is cancelled) right after `force_units` force-response steps."""
    sim = SimWheel()
    state = {'n': 0}

    def progress(fraction, text):
        if text.startswith('Force response'):
            state['n'] += 1
            if state['n'] == force_units:
                if cancel:
                    cal.cancel()
                else:
                    cal.TOTAL_S = cal._now() - cal.t0 + 0.5

    cal = Calibration(sim, progress)
    return sim, cal


def test_the_time_limit_in_the_second_pass_keeps_the_first_pass(caplog):
    sim, cal = _run_cut_after(2 * len(wc.KNOTS) + 3)
    with caplog.at_level('INFO'):
        result = cal.run()
    assert result['partial'] and result['timed_out_in']
    assert cal.timed_out_in == result['timed_out_in']
    assert result['timed_out_in'] in wcal.summary(result)
    assert any(r.levelname == 'WARNING' and result['timed_out_in'] in r.getMessage() for r in caplog.records)
    assert sim.t - cal.t0 < cal.TOTAL_S + Calibration.WIND_DOWN_S + 1.0
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_the_time_limit_in_the_first_pass_still_aborts():
    sim, cal = _run_cut_after(3)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'timeout'
    assert_put_back(sim)


def test_cancel_in_the_second_pass_still_aborts():
    sim, cal = _run_cut_after(2 * len(wc.KNOTS) + 3, cancel=True)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'cancelled'
    assert_put_back(sim)


def test_the_time_limit_says_where_and_the_summary_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(Calibration, 'TOTAL_S', 20.0)
    sim = SimWheel()
    cal = Calibration(sim)
    with caplog.at_level('INFO'):
        with pytest.raises(Aborted) as e:
            cal.run()
    assert e.value.reason == 'timeout'
    assert cal.step_text and '"{}"'.format(cal.step_text) in str(e.value)
    text = [r.getMessage() for r in caplog.records]
    assert any(m.startswith('calibration: ') and 'Finding the direction' in m for m in text)     # each step's start
    summary = next(m for m in text if m.startswith('calibration summary: timeout'))
    for part in ('phases', 'direction', 'breakaway', 'move timeouts', 'near-lock events', 'latency', 'a_kick',
                 'a_fs', 'breakaway_max', 'last log'):
        assert part in summary


def test_a_good_run_logs_its_summary_too(g29_like, caplog):
    sim, cal, result = g29_like
    with caplog.at_level('INFO'):
        cal._log_summary('ok')
    summary = caplog.records[-1].getMessage()
    assert summary.startswith('calibration summary: ok') and 'force response' in summary and 'rim breakaway' in summary


def test_a_pulse_that_cannot_be_started_is_dropped_not_waited_for(monkeypatch):
    real = Calibration._move_to
    state = {'failed': 0}

    def move_to(self, target, *args, **kwargs):
        if self.phase == 'force response' and not state['failed']:
            state['failed'] = 1
            self._hold(0.05)
            self.log.append((self._now(), 'move timeout'))
            self.move_timeouts += 1
            return False
        return real(self, target, *args, **kwargs)

    monkeypatch.setattr(Calibration, '_move_to', move_to)
    sim = SimWheel()
    cal = Calibration(sim)
    result = cal.run()
    assert cal.move_skips == 1 and any(e.startswith('shot skipped') for _t, e in cal.log)
    assert result['duration_s'] < 180
    assert_safe_run(sim, cal)
    assert_put_back(sim)


@pytest.mark.parametrize('delay, batch', [(0.015, 0.004), (0.030, 0.0)])
def test_a_g29_like_wheel_behind_a_proxy_finishes_well_inside_the_limit(delay, batch):
    """Breakaway about 13 %, positions arriving late (a proxy hop) and in batches: no move time-outs, and the
    whole run far from TOTAL_S."""
    sim = SimWheel(dead_zone=0.11, static=0.07, coulomb=0.05, read_delay=delay, read_batch=batch)
    cal = Calibration(sim)
    result = cal.run()
    assert cal.move_timeouts == 0 and cal.move_skips == 0
    assert sim.t - 100.0 < 215                 # (190 before the two pulse levels around the torque step)
    assert result['duration_s'] < 215
    assert_safe_run(sim, cal)
    assert_put_back(sim)


@pytest.mark.parametrize('batch', [0.008, 0.016])
def test_positions_arriving_in_bunches_still_run_and_fit(batch):
    """Reports delivered together (one time for the bunch) must not read as stops and spikes in the speed, nor
    as steps in the fit."""
    sim = SimWheel(dead_zone=0.11, static=0.07, coulomb=0.05, read_batch=batch)
    cal = Calibration(sim)
    result = cal.run()
    assert cal.move_timeouts == 0 and cal.move_skips == 0
    assert result['lut'] and not result.get('partial')
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_the_speed_comes_from_the_reports_times():
    cal = Calibration(SimWheel())
    cal.lock = 450.0
    # the newest sample against the one a window older: 30 degrees in 10 ms, whatever the arrival pattern
    cal.hist.extend([(1.000, 0.0), (1.010, 10.0), (1.020, 40.0)])
    assert cal._velocity(1.021) == pytest.approx(3000.0)
    assert cal._velocity(1.0205) == pytest.approx(3000.0)
    # no reports for a while: a still wheel, the speed decays
    assert abs(cal._velocity(1.2)) < 250.0
    cal.hist.clear()
    cal.hist.append((1.0, 5.0))
    assert cal._velocity(1.0) == 0.0


def test_a_wheel_that_does_not_move_stops_the_run():
    sim = SimWheel(static=0.9, coulomb=0.8)                      # blocked
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'stalled'
    assert sim.max_motor <= Calibration.SWEEP_MAX + 1 / 128
    assert_put_back(sim)


def test_a_wheel_driven_by_something_else_is_left_alone():
    # a live writer: ext_force changes between the reads
    sim = SimWheel()
    values = iter(['1200', '1350', '900', '0', '0'])
    read = sim.read
    sim.read = lambda name: next(values) if name == 'ext_force' else read(name)
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'busy'
    assert sim.writes == []
    # the game muted by something else
    sim = SimWheel()
    sim.attrs['game_ffb_level'] = '0'
    with pytest.raises(Aborted):
        Calibration(sim).run()
    assert sim.writes == []


def test_a_stale_ext_force_is_cleared(monkeypatch):
    """The driver keeps the last value written after its watchdog faded it: the same non-zero value over
    150 ms with the game's force at 100 % is a leftover, cleared before the run."""
    sim = SimWheel()
    sim.attrs['ext_force'] = '1200'
    monkeypatch.setattr(Calibration, 'TOTAL_S', 3.0)
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'timeout'                          # it ran
    assert sim.writes[0][1:] == ('ext_force', '0')
    assert_put_back(sim)


def test_settings_asked_for_during_the_run_are_the_ones_put_back(monkeypatch):
    sim = SimWheel()
    sim.asked = {'gain': 95, 'range': 720}
    monkeypatch.setattr(Calibration, 'TOTAL_S', 5.0)
    with pytest.raises(Aborted):
        Calibration(sim).run()
    assert (sim.gain, sim.range, sim.autocenter) == (95, 720, 30)


def test_shaping_already_set_is_put_back(monkeypatch):
    sim = SimWheel()
    sim.attrs.update(ffb_lut='0 20000 32767', ffb_deadband='900', ffb_dither='1', ffb_soft_clip='80')
    monkeypatch.setattr(Calibration, 'TOTAL_S', 5.0)
    with pytest.raises(Aborted):
        Calibration(sim).run()
    assert_put_back(sim, attrs=dict(ffb_lut='0 20000 32767', ffb_deadband='900', ffb_dither='1',
                                    ffb_soft_clip='80'))


def test_full_force_only_in_short_pulses():
    cal = Calibration(SimWheel())
    t = 0.0
    out = []
    while t < 1.0:
        out.append((t, cal._limit(1.0, t)))
        t += 0.002
    assert any(w > Calibration.SWEEP_MAX for _t, w in out)
    # then at most SWEEP_MAX for the cool-down, then again at most 150 ms
    runs, start = [], None
    for t, w in out:
        if w > Calibration.SWEEP_MAX and start is None:
            start = t
        elif w <= Calibration.SWEEP_MAX and start is not None:
            runs.append((start, t))
            start = None
    assert all(b - a <= Calibration.PULSE_MAX_S + 0.0021 for a, b in runs)
    assert all(b2 - e1 >= Calibration.PULSE_COOLDOWN_S - 0.0021 for (_, e1), (b2, _) in zip(runs, runs[1:]))
    assert cal._limit(-0.4, 2.0) == -0.4 and cal._limit(5, 3.0) == 1.0


# --- the math on its own ---

def test_lut_of_a_linear_wheel_is_identity():
    r = np.array(wc.KNOTS)
    lut, deadband = wc.build_lut(wc.KNOTS, r, 0.0)
    assert deadband == 0
    assert all(abs(v - round(i * wc.FULL / 32)) <= 1 for i, v in enumerate(lut))
    assert wc.recommend_dither(lut, deadband)[0] is False


def test_lut_and_deadband_of_a_wheel_with_a_dead_zone():
    knots = np.array(wc.KNOTS)
    d = 0.08
    r = np.clip((knots - d) / (1 - d), 0, None)
    r = wc.isotonic(r)
    found = wc.dead_zone(wc.KNOTS, r)
    assert abs(found - d) < 1e-6
    lut, deadband = wc.build_lut(wc.KNOTS, r, found)
    assert abs(deadband - d * wc.FULL) <= 1
    # linear above the dead zone: the LUT is the identity, the deadband does the lift
    assert all(abs(v - i * wc.FULL / 32) <= 2 for i, v in enumerate(lut))


def test_isotonic_and_inverse():
    assert list(wc.isotonic([0, 0.2, 0.1, 0.3])) == pytest.approx([0, 0.15, 0.15, 0.3])
    xs, ys = np.array([0, 0.1, 0.5, 1.0]), np.array([0, 0, 0.5, 1.0])
    assert wc.inverse_pl(xs, ys, 0.25) == pytest.approx(0.3)
    assert wc.inverse_pl(xs, ys, 0.0) == 0.0
    assert wc.inverse_pl(xs, ys, 2.0) == 1.0


def test_encoder_step_and_onset_latency():
    steps = [4, 5, 4, 5, 4, 9, 13, 4, 18, 5] * 20
    # the cluster of smallest steps is 4 and 5 (4 of one, 3 of the other in every 10)
    assert wc.encoder_step(steps, 65536 / 900) == pytest.approx((4 * 4 + 5 * 3) / 7 / (65536 / 900), rel=0.01)
    assert wc.encoder_step([], 1) is None
    assert wc.onset_latency([(0, 0.012), (1, 1.014), (2, None)]) == pytest.approx(0.013)


def test_a_recording_without_movement_is_refused():
    rec = wc.Recording()
    for i in range(3000):
        rec.command(i * 0.002, 0.0)
    rec.position(0.0, 0.0)
    rec.position(6.0, 0.0)
    with pytest.raises(wc.FitError):
        wc.analyse(rec)


# --- storage and the correction ---

def test_store_round_trip(tmp_path, g29_like):
    _sim, _cal, result = g29_like
    path = str(tmp_path / wcal.STORE_NAME)
    assert wcal.load_store(path) == {}
    entry = wcal.new_entry(json.loads(json.dumps(result)))
    assert entry['use'] is True and entry['dither'] is True and entry['soft_clip'] == 0
    wcal.save_store(path, {'046d:c24f': entry})
    back = wcal.load_store(path)
    assert back == {'046d:c24f': json.loads(json.dumps(entry))}
    assert not os.path.exists(path + '.tmp')
    # the user's own dither and soft clip survive another calibration
    mine = dict(back['046d:c24f'], dither=False, dither_chosen=True, soft_clip=80)
    again = wcal.new_entry(result, mine)
    assert again['dither'] is False and again['soft_clip'] == 80 and again['use'] is True
    # a damaged file is an empty store
    with open(path, 'w') as f:
        f.write('{not json')
    assert wcal.load_store(path) == {}


def test_correction_values_and_applying_them(g29_like):
    _sim, _cal, result = g29_like
    entry = wcal.new_entry(result)
    values = wcal.correction_values(entry)
    assert values['ffb_lut'] == ' '.join(map(str, result['lut']))
    assert values['ffb_deadband'] == str(result['deadband'])
    assert values['ffb_dither'] == '1' and values['ffb_soft_clip'] == '0'
    off = wcal.correction_values(dict(entry, use=False))
    assert off['ffb_lut'] == 'identity' and off['ffb_deadband'] == '0' and off['ffb_dither'] == '1'
    broken = wcal.correction_values(dict(entry, result=dict(result, lut=[5, 3])))
    assert broken['ffb_lut'] == 'identity'
    # the driver (as the simulator checks it) takes them
    sim = SimWheel()
    assert wcal.apply_correction(sim.write, entry)
    assert sim.attrs['ffb_lut'] == values['ffb_lut'] and sim.attrs['ffb_deadband'] == values['ffb_deadband']

    def refuse(name, value):
        raise OSError(22, 'Invalid argument')
    assert wcal.apply_correction(refuse, entry) is False


def test_wheel_key_and_driver_support(tmp_path):
    assert wcal.wheel_key(SimpleNamespace(usb_id='046d:c24f', name='G29', get_id=lambda: '/x')) == '046d:c24f'
    from oversteer.device import Device
    dev = Device(None, {'dev_path': str(tmp_path), 'usb_id': '046d:c24f'})
    assert not dev.has_wheel_correction()
    for name in wcal.ATTRS[:-1]:
        (tmp_path / name).write_text('0\n')
    assert not dev.has_wheel_correction()                       # one missing: an older driver
    (tmp_path / wcal.ATTRS[-1]).write_text('0\n')
    assert dev.has_wheel_correction()
    dev.write_attr('ffb_dither', 1)
    assert (tmp_path / 'ffb_dither').read_text() == '1'


def test_settings_are_held_while_a_calibration_runs(tmp_path):
    from oversteer.device import Device
    dev = Device(None, {'dev_path': str(tmp_path)})
    (tmp_path / 'range').write_text('540')
    (tmp_path / 'gain').write_text('65535')
    io = wcal.DeviceIO.__new__(wcal.DeviceIO)
    io.device = dev
    io.hold()
    dev.set_range(270)
    dev.set_ff_gain(80)
    assert (tmp_path / 'range').read_text() == '540'
    io.set_range(900)                                            # the calibration itself writes
    assert (tmp_path / 'range').read_text() == '900'
    assert io.release() == {'range': 270, 'gain': 80}
    dev.set_range(720)
    assert (tmp_path / 'range').read_text() == '720'
    assert io.release() == {}


# --- the window's side (oversteer/gui.py, oversteer/gtk_ui.py) ---

class _Ui:
    def __init__(self, confirm=True):
        self.calls = []
        self.confirm = confirm

    def confirmation_dialog(self, message):
        self.calls.append(('confirm', message))
        return self.confirm

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return lambda *a, **k: self.calls.append((name,) + a)


class _Wheel:
    usb_id = '046d:c24f'
    name = 'G29'
    dev_path = '/sys/fake-hid'

    def __init__(self, supported=True):
        self.supported = supported
        self.attrs = {}

    def get_id(self):
        return '/sys/fake'

    def is_ready(self):
        return True

    def has_wheel_correction(self):
        return self.supported

    def get_ffb_tick_ms(self):
        return 2

    def write_attr(self, name, value):
        self.attrs[name] = str(value)


def _gui(tmp_path, wheel, confirm=True):
    from oversteer.gui import Gui
    gui = Gui.__new__(Gui)
    gui.config_path = str(tmp_path)
    gui.device = wheel
    gui.ui = _Ui(confirm)
    gui.calibration = None
    gui.calibration_thread = None
    gui.test = None
    gui.grab_input = False
    gui._wheel_retry = None
    gui.ffb_engine = SimpleNamespace(set_device=lambda path: None)
    return gui


def test_a_finished_calibration_is_stored_and_applied(tmp_path, g29_like):
    _sim, cal, result = g29_like
    wheel = _Wheel()
    gui = _gui(tmp_path, wheel)
    gui.calibration = cal
    gui._calibration_finished(cal, wheel, result, None)
    assert gui.calibration is None
    store = wcal.load_store(str(tmp_path / wcal.STORE_NAME))
    assert store['046d:c24f']['use'] is True
    assert wheel.attrs['ffb_lut'] == ' '.join(map(str, result['lut']))
    assert wheel.attrs['ffb_deadband'] == str(result['deadband']) and wheel.attrs['ffb_dither'] == '1'
    names = [c[0] for c in gui.ui.calls]
    assert 'close_calibration_dialog' in names and 'info_dialog' in names
    shown = [c for c in gui.ui.calls if c[0] == 'set_wheel_calibration'][-1]
    assert shown[1] is True and shown[2]['use'] is True and shown[3] is False
    # the switch off: identity and no deadband, dither kept
    gui.set_use_wheel_correction(False)
    assert wheel.attrs['ffb_lut'] == 'identity' and wheel.attrs['ffb_deadband'] == '0'
    gui.set_wheel_dither(False)
    gui.set_wheel_soft_clip(80)
    assert wheel.attrs['ffb_dither'] == '0' and wheel.attrs['ffb_soft_clip'] == '80'
    entry = wcal.load_store(str(tmp_path / wcal.STORE_NAME))['046d:c24f']
    assert entry['dither_chosen'] is True and entry['soft_clip'] == 80 and entry['use'] is False
    # the wheel plugged in again (the driver starts from its defaults): the stored choice goes back
    wheel.attrs = {}
    gui.set_use_wheel_correction(True)
    assert wheel.attrs['ffb_lut'] != 'identity'
    wheel.attrs = {}
    gui.apply_wheel_correction()
    assert wheel.attrs['ffb_lut'] == ' '.join(map(str, result['lut'])) and wheel.attrs['ffb_soft_clip'] == '80'


def test_a_failed_or_cancelled_calibration_changes_nothing(tmp_path):
    wheel = _Wheel()
    gui = _gui(tmp_path, wheel)
    gui._calibration_finished(object(), wheel, None, Aborted('grabbed', 'hands'))
    assert ('error_dialog', 'The wheel calibration did not finish.', 'hands') in gui.ui.calls
    gui.ui.calls.clear()
    gui._calibration_finished(object(), wheel, None, Aborted('cancelled', 'x'))
    assert not any(c[0] == 'error_dialog' for c in gui.ui.calls)
    assert wcal.load_store(str(tmp_path / wcal.STORE_NAME)) == {}
    assert wheel.attrs == {}
    gui.apply_wheel_correction()                                 # never calibrated: the driver is left alone
    assert wheel.attrs == {}


def test_calibrating_needs_the_driver_and_a_yes(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(wcal.Calibration, 'run', lambda self: started.append(self))
    gui = _gui(tmp_path, _Wheel(supported=False))
    gui.start_calibration()
    gui.refresh_wheel_correction()
    assert not any(c[0] == 'confirm' for c in gui.ui.calls)
    assert ('set_wheel_calibration', False, None, False) in gui.ui.calls
    gui = _gui(tmp_path, _Wheel(), confirm=False)
    gui.start_calibration()
    assert gui.calibration is None and not started
    confirm = [c for c in gui.ui.calls if c[0] == 'confirm'][0][1]
    assert confirm.startswith('Hands off the wheel: it will turn by itself.')


def test_the_ffb_tab_rows(tmp_path):
    gi = pytest.importorskip('gi')
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    from oversteer.gtk_ui import GtkUi
    if not Gtk.init_check()[0]:
        pytest.skip('no display')
    calls = []
    ui = GtkUi.__new__(GtkUi)
    ui.controller = SimpleNamespace(start_calibration=lambda: calls.append('start'),
                                    set_use_wheel_correction=lambda s: calls.append(('use', s)),
                                    set_wheel_dither=lambda s: calls.append(('dither', s)),
                                    set_wheel_soft_clip=lambda v: calls.append(('clip', v)),
                                    set_wheel_friction_comp=lambda v: calls.append(('comp', v)))
    listbox = Gtk.ListBox()
    row = Gtk.ListBoxRow()
    ui.autocenter_persistent = Gtk.Switch()
    row.add(ui.autocenter_persistent)
    listbox.add(row)
    ui._build_wheel_calibration()
    assert len(listbox.get_children()) == 6
    assert not ui.calibrate_button.get_sensitive()
    assert 'new-lg4ff' in ui.calibrate_button.get_tooltip_text()
    ui.set_wheel_calibration(True, None, False)
    assert ui.calibrate_button.get_sensitive() and not ui.wheel_correction_switch.get_sensitive()
    entry = wcal.new_entry({'lut': [0, 32767], 'deadband': 1966, 'dither': True, 'fit_latency_ms': 12.0,
                            'date': '2026-11-01T10:00:00'})
    ui.set_wheel_calibration(True, entry, False)
    assert calls == []                                           # showing a state is not a change
    assert ui.wheel_correction_switch.get_active() and ui.wheel_dither_switch.get_active()
    assert ui.calibration_status.get_text().startswith('Calibrated 2026-11-01: dead zone 6.0 %')
    ui.wheel_correction_switch.set_active(False)
    ui.wheel_soft_clip.set_value(80)
    assert calls == [('use', False), ('clip', 80)]
    ui.set_wheel_calibration(True, entry, True)
    assert not ui.calibrate_button.get_sensitive() and ui.calibration_status.get_text() == 'Calibrating...'


# --- review round 1: blind position, stalls, range, permissions, crash record, validation ---

def test_reports_stopping_while_the_wheel_turns_stops_the_run():
    """Another program grabbing the input node stops the reports; the axis's current value still moves."""
    sim = SimWheel()
    sim.blind(20.0)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'blind'
    assert sim.t - sim.blind_at < 2.0
    assert sim.max_abs_theta < cal.lock - Calibration.LOCK_MARGIN_DEG
    assert_put_back(sim)


def test_blind_from_the_start_never_drives_to_the_stop():
    sim = SimWheel()
    sim.blind(0.0)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'blind'
    assert sim.max_abs_theta < 10
    assert_put_back(sim)


def test_a_pause_during_a_pulse_stops_the_run():
    sim = SimWheel()
    sim.stall_high = (sim.t + 30.0, 0.03)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'stall'
    assert_put_back(sim)


def test_a_range_that_does_not_take_stops_the_run():
    sim = SimWheel()
    sim.set_range = lambda v: None                              # the driver ignores it
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'range'
    assert not any(n == 'ext_force' and v != '0' for _t, n, v in sim.writes)
    assert sim.last('game_ffb_level') == '100'


def test_no_permission_is_said_so():
    sim = SimWheel()
    write = sim.write

    def refuse(name, value):
        if name == 'ffb_lut' and value == 'identity' and sim.attrs['game_ffb_level'] == '0':
            raise PermissionError(13, 'Permission denied')
        return write(name, value)
    sim.write = refuse
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'permission'
    assert 'udev' in str(e.value)
    assert sim.last('game_ffb_level') == '100' and sim.last('ext_force') == '0'


def test_the_garbage_collector_is_back_on_after_a_run(monkeypatch):
    import gc
    monkeypatch.setattr(Calibration, 'TOTAL_S', 2.0)
    assert gc.isenabled()
    with pytest.raises(Aborted):
        Calibration(SimWheel()).run()
    assert gc.isenabled()


def test_the_crash_record(tmp_path, monkeypatch):
    path = str(tmp_path / wcal.PENDING_NAME)
    sim = SimWheel()
    sim.attrs.update(ffb_lut='0 20000 32767', ffb_deadband='900')
    seen = []

    def progress(fraction, text):
        seen.append(os.path.exists(path))
        if fraction > 0.1:
            cal.cancel()
    cal = Calibration(sim, progress, pending_path=path, pending_key='046d:c24f')
    with pytest.raises(Aborted):
        cal.run()
    assert seen and all(seen)                                    # written before anything changed
    assert not os.path.exists(path)                              # and gone once the wheel is back
    # the process dies mid-run (no finally): the record stays and the next start puts the wheel back
    sim = SimWheel()
    monkeypatch.setattr(Calibration, '_restore', lambda self: None)
    cal = Calibration(sim, lambda f, t: f > 0.05 and cal.cancel(), pending_path=path, pending_key='046d:c24f')
    with pytest.raises(Aborted):
        cal.run()
    assert os.path.exists(path)
    assert sim.attrs['game_ffb_level'] == '0' and sim.range == 900
    assert wcal.take_pending(path, 'other') is None
    assert wcal.recover_pending(path, 'other', sim) is False
    assert wcal.recover_pending(path, '046d:c24f', sim) is True
    assert_put_back(sim)
    assert not os.path.exists(path)
    assert wcal.recover_pending(path, '046d:c24f', sim) is False


def test_implausible_measurements_are_refused():
    knots = np.array(wc.KNOTS)
    r = np.clip((knots - 0.2) / 0.8, 0, None)                    # "nothing below 20 %"
    assert wc.dead_zone(wc.KNOTS, r) > wc.MAX_DEAD_ZONE
    ident = [round(i * wc.FULL / 32) for i in range(33)]
    assert wc.plausible_lut(ident)
    assert not wc.plausible_lut(ident[:-1])
    assert not wc.plausible_lut([1] + ident[1:])
    assert not wc.plausible_lut(ident[:-1] + [32000])
    flat = list(ident)
    flat[2] = flat[1]
    assert not wc.plausible_lut(flat)                            # a flat run
    steep = list(ident)
    steep[1] = 4 * 1024 + 1024 + 10
    steep[2] = max(steep[2], steep[1] + 1)
    assert not wc.plausible_lut(steep)
    assert not wc.plausible_lut([float(v) for v in ident])


def _entry(result):
    return wcal.new_entry(json.loads(json.dumps(result)))


@pytest.mark.parametrize('damage', [
    lambda e: e.update(version=2),
    lambda e: e['result'].update(lut=e['result']['lut'][:-1]),
    lambda e: e['result'].update(lut=[1] + e['result']['lut'][1:]),
    lambda e: e['result'].update(lut=e['result']['lut'][:-1] + [30000]),
    lambda e: e['result'].update(lut=list(reversed(e['result']['lut']))),
    lambda e: e['result'].update(deadband=int(0.2 * wc.FULL)),
    lambda e: e['result'].update(deadband=-1),
    lambda e: e['result'].update(deadband='1966'),
    lambda e: e.update(result='x'),
    lambda e: e['result'].pop('lut'),
])
def test_a_damaged_entry_writes_identity(g29_like, damage):
    entry = _entry(g29_like[2])
    assert wcal.valid_entry(entry)
    damage(entry)
    assert not wcal.valid_entry(entry)
    values = wcal.correction_values(entry)
    assert values['ffb_lut'] == 'identity' and values['ffb_deadband'] == '0'


def test_friction_compensation_is_folded_into_the_lut_in_torque_terms(g29_like):
    result = g29_like[2]
    entry = _entry(result)
    plain = wcal.correction_values(entry)
    assert plain['ffb_lut'] == ' '.join(str(v) for v in result['lut'])
    half_values = wcal.correction_values(dict(entry, friction_comp=50))
    full_values = wcal.correction_values(dict(entry, friction_comp=100))
    half = [int(v) for v in half_values['ffb_lut'].split()]
    assert half_values['ffb_deadband'] == plain['ffb_deadband'] == str(result['deadband'])   # the dead zone stays
    assert half[0] == 0 and half[1] > result['lut'][1] and wc.plausible_lut(half)
    assert full_values['ffb_lut'] == half_values['ffb_lut']                  # the setting stops at 50 %
    assert wcal.FRICTION_COMP_MAX_PERCENT == 50
    # junk is off
    assert wcal.correction_values(dict(entry, friction_comp='x'))['ffb_lut'] == plain['ffb_lut']
    # a huge friction is held to 80 % of the torque at the breakaway
    big = dict(entry, result=dict(entry['result'], friction=0.5), friction_comp=50)
    lut = [int(v) for v in wcal.correction_values(big)['ffb_lut'].split()]
    assert wc.plausible_lut(lut) and lut[1] <= wc.LUT_SEGMENT_SLOPE * wc.FULL / 32 + 1


def test_a_damaged_store_does_not_break_the_window(tmp_path):
    wheel = _Wheel()
    gui = _gui(tmp_path, wheel)
    for store in ({'046d:c24f': 'junk'}, {'046d:c24f': {'result': 5}}, {'046d:c24f': {'version': 1, 'use': True,
                  'result': {'lut': 'a b c', 'deadband': 'x'}, 'soft_clip': 'many'}}):
        wcal.save_store(str(tmp_path / wcal.STORE_NAME), store)
        wheel.attrs = {}
        gui.apply_wheel_correction()
        gui.refresh_wheel_correction()
        assert wheel.attrs in ({}, {'ffb_lut': 'identity', 'ffb_deadband': '0', 'ffb_dither': '0',
                                    'ffb_soft_clip': '0'})


def test_no_calibration_while_the_overlay_holds_the_input(tmp_path):
    gui = _gui(tmp_path, _Wheel())
    gui.grab_input = True
    gui.start_calibration()
    assert gui.calibration is None
    assert not any(c[0] == 'confirm' for c in gui.ui.calls)


def test_a_replugged_wheel_gets_its_correction_back(tmp_path, g29_like):
    wheel = _Wheel()
    gui = _gui(tmp_path, wheel)
    wcal.save_store(str(tmp_path / wcal.STORE_NAME), {'046d:c24f': _entry(g29_like[2])})
    gui.device_manager = SimpleNamespace(is_changed=lambda: False, take_plugged=lambda: ['/sys/fake'])
    gui.apply_plug_profile = lambda device_id: None
    gui.populate_devices()
    assert wheel.attrs['ffb_lut'] != 'identity'


def test_term_and_hup_quit_through_on_quit():
    from oversteer import gui as gui_module
    text = open(gui_module.__file__).read()
    assert 'GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, self._quit_on_signal, signum)' in text
    assert 'for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):' in text
    assert 'signal.signal(signal.SIGINT' not in text
    g = gui_module.Gui.__new__(gui_module.Gui)
    calls = []
    g.on_quit = lambda: calls.append('on_quit')
    g.ui = SimpleNamespace(quit=lambda: calls.append('quit'))
    assert g._quit_on_signal(15) is False
    assert calls == ['on_quit', 'quit']


# --- review round 2 ---

@pytest.mark.parametrize('field, value', [('breakaway', float('nan')), ('breakaway', float('inf')),
                                          ('friction', float('nan')), ('friction', float('inf')),
                                          ('breakaway', 0.0), ('friction', 1.5), ('breakaway', True)])
def test_non_finite_numbers_never_lift_the_deadband(g29_like, field, value):
    entry = _entry(g29_like[2])
    entry['result'][field] = value
    entry['friction_comp'] = 100
    assert not wcal.valid_entry(entry)
    values = wcal.correction_values(entry)
    assert values['ffb_lut'] == 'identity' and values['ffb_deadband'] == '0'
    # and the function on its own, whatever it is given, stays under the ceiling
    deadband = wcal.compensated_deadband(entry['result'], 100)
    assert 0 <= deadband <= wcal.DEADBAND_CEILING * wc.FULL


def test_the_deadband_ceiling_holds_for_any_input(g29_like):
    result = dict(g29_like[2])
    for deadband in (10 ** 9, -5, 32767):
        assert 0 <= wcal.compensated_deadband(dict(result, deadband=deadband), 100) <= \
            wcal.DEADBAND_CEILING * wc.FULL
    assert wcal.compensated_deadband(dict(result, friction=1.0, breakaway=1.0), 100) <= \
        wcal.DEADBAND_CEILING * wc.FULL


def test_a_crash_record_is_tried_once_then_only_its_safe_part(tmp_path):
    path = str(tmp_path / wcal.PENDING_NAME)
    saved = {'game_ffb_level': '100', 'ext_force_cap': '80', 'ffb_lut': 'identity', 'ffb_deadband': '0',
             'ffb_dither': '0', 'ffb_soft_clip': '0', 'gain': 110, 'autocenter': 30, 'range': 540}
    wcal.write_pending(path, 'k', saved)
    sim = SimWheel()
    sim.set_range = lambda v: (_ for _ in ()).throw(OSError(5, 'I/O error'))     # one write fails
    assert wcal.recover_pending(path, 'k', sim) is False
    with open(path) as f:
        assert json.load(f)['saved'] == {}                       # consumed: only force 0 and the game's force left
    sim = SimWheel()
    assert wcal.recover_pending(path, 'k', sim) is True
    assert [n for _t, n, _v in sim.writes] == ['ext_force', 'game_ffb_level', 'ext_force', 'game_ffb_level']
    assert not os.path.exists(path)


def test_a_crash_record_from_another_boot_is_dropped(tmp_path, monkeypatch):
    path = str(tmp_path / wcal.PENDING_NAME)
    wcal.write_pending(path, 'k', {'range': 900})
    monkeypatch.setattr(wcal, 'boot_id', lambda: 'another-boot')
    sim = SimWheel()
    assert wcal.recover_pending(path, 'k', sim) is False
    assert sim.writes == [] and not os.path.exists(path)


def test_a_crash_record_belongs_to_one_wheel():
    one = SimpleNamespace(usb_id='046d:c24f', serial=None,
                          dev_path='/sys/devices/pci0000:00/usb1/1-3/1-3:1.0/0003:046D:C24F.0005')
    same_port_replugged = SimpleNamespace(usb_id='046d:c24f', serial=None,
                                          dev_path='/sys/devices/pci0000:00/usb1/1-3/1-3:1.0/0003:046D:C24F.0009')
    other_port = SimpleNamespace(usb_id='046d:c24f', serial=None,
                                 dev_path='/sys/devices/pci0000:00/usb1/1-4/1-4:1.0/0003:046D:C24F.0006')
    assert wcal.instance_key(one) == wcal.instance_key(same_port_replugged) == '046d:c24f@1-3:1.0'
    assert wcal.instance_key(other_port) != wcal.instance_key(one)
    assert wcal.instance_key(SimpleNamespace(usb_id='046d:c24f', serial='ABC', dev_path='x')) == '046d:c24f@ABC'


NODE = '/dev/input/event21'                                      # the proxy's virtual device
REAL = '/dev/input/event7'


def _proxied_io(monkeypatch):
    """A DeviceIO behind a proxy whose status is faked: nothing is opened."""
    from oversteer.proxy.manager import ProxyManager
    status = {'proxies': [{'id': 'combined-wheel', 'devnode': NODE, 'sources': {'wheel': REAL}}]}
    monkeypatch.setattr(ProxyManager, 'read_status', staticmethod(lambda *a, **k: status))
    io = wcal.DeviceIO.__new__(wcal.DeviceIO)
    io.device = SimpleNamespace(dev_name=REAL)
    return io, status


def test_the_active_combined_device_is_the_one_calibrated(monkeypatch):
    io, status = _proxied_io(monkeypatch)
    assert io._proxy_id_of(NODE) == 'combined-wheel'
    assert io._proxy_id_of('/dev/input/event30') is None
    # its own range, whatever the mapping did: no inversion or rescale is undone
    io._abs_min, io._abs_max = 0, 1000
    assert io._from_proxy(0) == 0 and io._from_proxy(1000) == 65535
    io._abs_min, io._abs_max = -100, 100
    assert io._from_proxy(-100) == 0 and io._from_proxy(100) == 65535


def test_an_inverted_active_axis_is_calibrated_with_the_direction_learnt():
    sim = SimWheel()
    raw = sim.raw
    sim.raw = lambda: 65535 - raw()                              # the axis (and its reports) read the other way
    cal = Calibration(sim)
    cal.run()
    assert cal.sign_known and cal.sign == 1.0                   # the sim's motor is -1 when read the right way round
    assert_put_back(sim)


def test_stored_corrections_are_kept_per_active_device():
    direct = SimpleNamespace(usb_id='046d:c24f', dev_name=REAL, _input_node=lambda: REAL, dev_path='x/1-3:1.0/h')
    proxied = SimpleNamespace(usb_id='046d:c24f', dev_name=REAL, _input_node=lambda: NODE, dev_path='x/1-3:1.0/h')
    assert wcal.wheel_key(direct) == '046d:c24f'
    assert wcal.wheel_key(proxied) == '046d:c24f+combined'
    # the crash record is the physical wheel's, found whichever device is active
    assert wcal.instance_key(direct) == wcal.instance_key(proxied)


def test_a_restarted_proxy_is_noticed(monkeypatch, tmp_path):
    io, status = _proxied_io(monkeypatch)
    node = tmp_path / 'event21'
    node.write_text('')
    io._proxy_id, io._node, io._ino, io._lost = 'combined-wheel', str(node), os.stat(node).st_ino, 0
    status['proxies'][0]['devnode'] = str(node)
    io._next_check = 0.0
    io._check_proxy()                                           # fine
    io._next_check = 0.0
    node.unlink()
    node.write_text('')                                         # a new node under the same name
    while os.stat(node).st_ino == io._ino:                      # (a freed inode can be handed out again)
        node.unlink()
        other = tmp_path / 'filler'
        other.write_text('')
        node.write_text('')
    with pytest.raises(wcal.ProxyLost):
        io._check_proxy()
    io._ino = os.stat(node).st_ino
    io._next_check = 0.0
    node.unlink()                                               # gone
    with pytest.raises(wcal.ProxyLost):
        io._check_proxy()


def test_a_proxy_that_stops_listing_the_wheel_is_noticed(monkeypatch, tmp_path):
    io, status = _proxied_io(monkeypatch)
    node = tmp_path / 'event21'
    node.write_text('')
    status['proxies'][0]['devnode'] = str(node)
    io._proxy_id, io._node, io._ino, io._lost = 'combined-wheel', str(node), os.stat(node).st_ino, 0
    io._next_check = 0.0
    io._check_proxy()
    status['proxies'].clear()
    io._next_check = 0.0
    io._check_proxy()                                           # one missed look is allowed
    io._next_check = 0.0
    with pytest.raises(wcal.ProxyLost):
        io._check_proxy()


def test_a_proxy_lost_mid_run_stops_the_run_with_the_forces_zeroed(monkeypatch):
    class LosingWheel(SimWheel):
        def poll(self):
            if self.t > 3.0:
                raise wcal.ProxyLost('the combined device restarted')
            return super().poll()

    sim = LosingWheel()
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'proxy'
    assert sim.max_abs_theta < cal.lock - Calibration.LOCK_MARGIN_DEG
    assert_put_back(sim)


def test_a_proxy_lost_while_preparing_is_an_abort_and_everything_is_put_back():
    from oversteer import wheel_owner

    class LosingWheel(SimWheel):
        def position_raw(self):
            raise wcal.ProxyLost('the combined device restarted')

    sim = LosingWheel()
    with pytest.raises(Aborted) as e:
        Calibration(sim).run()
    assert e.value.reason == 'proxy'
    assert wheel_owner.current(sim.wheel_id) is None
    assert_put_back(sim)


def test_the_correction_follows_the_active_device(tmp_path):
    wheel = _Wheel()
    wheel.dev_name = REAL
    wheel.is_ready = lambda: True
    wheel._input_node = lambda: REAL
    gui = _gui(tmp_path, wheel)
    calls = []
    gui._wheel_entry = lambda device=None: None
    gui.apply_wheel_correction = lambda: (calls.append('apply'), setattr(gui, 'correction_key', wcal.wheel_key(wheel)))
    gui.refresh_wheel_correction = lambda: calls.append('refresh')
    gui.correction_key = wcal.wheel_key(wheel)
    gui.check_active_device()
    assert calls == []                                           # nothing changed
    wheel._input_node = lambda: NODE                             # the proxy takes the wheel
    gui.check_active_device()
    assert calls == ['apply', 'refresh']
    assert wheel.attrs['ffb_lut'] == 'identity' and wheel.attrs['ffb_deadband'] == '0'   # no entry: identity
    gui.check_active_device()
    assert calls == ['apply', 'refresh']                         # once
    wheel.attrs.clear()
    wheel._input_node = lambda: REAL                             # the proxy goes; an exclusive holder has it
    gui._wheel_held = lambda: ('test', True)
    gui.check_active_device()
    assert calls == ['apply', 'refresh'] and wheel.attrs == {}   # nothing written, tried again later
    gui._wheel_held = lambda: ('countersteer-ffb', False)        # a shared one (Countersteer FFB) stops it too
    gui.check_active_device()
    assert calls == ['apply', 'refresh'] and wheel.attrs == {}
    gui._wheel_held = lambda: None                               # free: the next tick puts it on
    gui.check_active_device()
    assert calls == ['apply', 'refresh', 'apply', 'refresh'] and wheel.attrs['ffb_lut'] == 'identity'
    del calls[:]
    gui._wheel_held = lambda: None
    gui.calibration = object()                                   # not while a run is going
    wheel._input_node = lambda: NODE
    gui.check_active_device()
    assert calls == []


def test_the_owner_hook_is_released(monkeypatch):
    calls = []
    monkeypatch.setattr(Calibration, '_acquire_owner', lambda self: calls.append('acquire') or 'token')
    monkeypatch.setattr(Calibration, '_release_owner', lambda self, owner: calls.append(('release', owner)))
    monkeypatch.setattr(Calibration, 'TOTAL_S', 1.0)
    with pytest.raises(Aborted):
        Calibration(SimWheel()).run()
    # released right after the restore; the outer finally's call is the no-op safety net
    assert calls == ['acquire', ('release', 'token'), ('release', None)]


# --- wheel_owner: one owner of the wheel's replace-mode attributes at a time ---

def test_a_wheel_held_exclusively_elsewhere_is_left_alone():
    from oversteer import wheel_owner
    sim = SimWheel()
    assert wheel_owner.acquire(sim.wheel_id, 'wheel-test', exclusive=True)
    try:
        with pytest.raises(Aborted) as e:
            Calibration(sim).run()
        assert e.value.reason == 'busy' and 'wheel-test' in str(e.value)
        assert sim.writes == [] and sim.attempts == []
    finally:
        wheel_owner.release(sim.wheel_id, 'wheel-test')


def test_countersteer_lets_go_and_the_run_holds_the_wheel(monkeypatch):
    """A shared owner (the FFB engine) is asked to let go; it puts the game's force back and releases; only
    then does the run look at ext_force (the engine's last value is stale by then) and start."""
    from oversteer import wheel_owner
    sim = SimWheel()
    sim.attrs['ext_force'] = '2500'                              # what the engine last wrote
    order = []

    def let_go():
        order.append(('inhibit', sim.attrs['ext_force']))
        sim.write('game_ffb_level', '100')
        wheel_owner.release(sim.wheel_id, 'countersteer-ffb')
    assert wheel_owner.acquire(sim.wheel_id, 'countersteer-ffb', on_inhibit=let_go)
    holders = []

    def progress(fraction, text):
        holders.append(wheel_owner.current(sim.wheel_id))
        cal.cancel()
    cal = Calibration(sim, progress)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'cancelled'
    assert order == [('inhibit', '2500')]
    assert holders and all(h == ('calibration', True) for h in holders)
    first_clear = next(i for i, (_t, n, v) in enumerate(sim.writes) if n == 'ext_force')
    assert [n for _t, n, _v in sim.writes[:first_clear]] == ['game_ffb_level']     # only the engine's own
    assert wheel_owner.current(sim.wheel_id) is None                               # released after the restore
    assert_put_back(sim)


def test_the_wheel_is_released_after_every_ending(monkeypatch):
    from oversteer import wheel_owner
    sim = SimWheel()
    sim.unplug(5.0)
    with pytest.raises(Aborted):
        Calibration(sim).run()
    assert wheel_owner.current(sim.wheel_id) is None


def test_writes_while_the_wheel_is_held(tmp_path, g29_like, monkeypatch):
    """A crash record waits for any holder; the correction (shaping only) waits for an exclusive one, but
    goes in at once under Countersteer FFB (shared), whose force is shaped too."""
    from oversteer import wheel_owner
    from oversteer import gui as gui_module
    timers = []
    monkeypatch.setattr(gui_module.GLib, 'timeout_add', lambda ms, fn: timers.append(fn) or 7)
    wheel = _Wheel()
    gui = _gui(tmp_path, wheel)
    wcal.save_store(str(tmp_path / wcal.STORE_NAME), {'046d:c24f': _entry(g29_like[2])})
    path = str(tmp_path / wcal.PENDING_NAME)
    key = wcal.instance_key(wheel)
    recovered = []
    monkeypatch.setattr(wcal, 'DeviceIO', lambda device: recovered.append(device) or SimWheel())

    # exclusive (the test): nothing at all
    assert wheel_owner.acquire(wheel.dev_path, 'wheel-test', exclusive=True)
    try:
        wcal.write_pending(path, key, {'range': 900})
        sim = SimWheel()
        assert wcal.recover_pending(path, key, sim, wheel=wheel.dev_path) is False
        assert sim.writes == [] and os.path.exists(path)
        gui.apply_wheel_correction()
        gui.recover_wheel()
        assert wheel.attrs == {} and not recovered
        assert len(timers) == 1                                  # one retry timer, not one per call
        assert timers[0]() is True                               # still held: keeps waiting
        assert wheel.attrs == {}
    finally:
        wheel_owner.release(wheel.dev_path, 'wheel-test')

    # shared (Countersteer FFB): the correction and the forces wait; a killed run's cap, gain and range go back now
    assert wheel_owner.acquire(wheel.dev_path, 'countersteer-ffb', on_inhibit=lambda: None)
    try:
        gui.apply_wheel_correction()
        assert wheel.attrs == {}
        gui.recover_wheel()
        assert len(recovered) == 1 and os.path.exists(path)      # the partial restore; the record stays for the rest
        assert wcal.take_pending(path, key) == {}
        assert timers[0]() is True
    finally:
        wheel_owner.release(wheel.dev_path, 'countersteer-ffb')
    wheel.attrs = {}
    assert timers[0]() is False                                  # free: both written now
    assert len(recovered) == 2 and wheel.attrs['ffb_lut'] != 'identity'
    assert not os.path.exists(path)


def test_the_wheel_is_free_while_the_fit_runs(monkeypatch):
    from oversteer import wheel_owner
    sim = SimWheel()
    seen = []

    def analyse(rec, knots=wc.KNOTS):
        seen.append(wheel_owner.current(sim.wheel_id))
        raise wc.FitError('stop here')
    monkeypatch.setattr(wc, 'analyse', analyse)
    monkeypatch.setattr(Calibration, '_measure', lambda self: None)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'fit' and seen == [None]


def test_a_replugged_wheel_gives_the_engine_its_new_path(tmp_path):
    wheel = _Wheel()
    wheel.dev_path = '/sys/devices/usb1/1-3/1-3:1.0/0003:046D:C24F.0009'     # bound again under a new number
    gui = _gui(tmp_path, wheel)
    paths = []
    gui.ffb_engine = SimpleNamespace(set_device=paths.append)
    gui.device_manager = SimpleNamespace(is_changed=lambda: False, take_plugged=lambda: ['/sys/fake'])
    gui.apply_plug_profile = lambda device_id: None
    gui.populate_devices()
    assert paths == [wheel.dev_path]
    gui.device_manager = SimpleNamespace(is_changed=lambda: False, take_plugged=lambda: [])
    gui.populate_devices()
    assert paths == [wheel.dev_path]                             # nothing plugged: left alone


def test_quitting_drops_the_retry_timer(tmp_path, monkeypatch):
    from oversteer import gui as gui_module
    removed = []
    monkeypatch.setattr(gui_module.GLib, 'source_remove', removed.append)
    g = gui_module.Gui.__new__(gui_module.Gui)
    g._wheel_retry = 42
    g.calibration = None
    g.calibration_thread = None
    g.test = None
    g._forward_stop = SimpleNamespace(set=lambda: None)
    g.ffb_engine = SimpleNamespace(stop=lambda: None)
    g.telemetry_web = g.telemetry_recorder = None
    g.shift_learner = SimpleNamespace(save=lambda: None)
    try:
        g.on_quit()
    except AttributeError:
        pass                                                     # later parts need a full window
    assert removed == [42] and g._wheel_retry is None


# --- moving between positions, the push's strength, the recording of each run ---

# A strong motor with a steep torque curve behind a dead zone near the breakaway force: the old move loop (a
# feed-forward below the dead zone, a 12/s velocity loop sized from a full-scale figure three times too high)
# crept at 20..30 deg/s and stopped short of nearly every target (39 time-outs, 36 pulses dropped, no fit)
STEEP = dict(inertia=1 / 12000.0, dead_zone=0.135, static=0.03, coulomb=0.02, latency=0.03, curve=6.0)


def _prepared(**kw):
    """A Calibration on a simulated wheel, set up the way _measure leaves it before the pushes."""
    sim = SimWheel(**kw)
    cal = Calibration(sim)
    cal._prepare()
    cal.sign = 1.0 if sim.ext_sign > 0 else -1.0
    cal.sign_known = True
    cal.breakaway_max = sim.dead_zone + sim.static            # about what the slow ramps measure
    return sim, cal


@pytest.mark.parametrize('kw', [
    STEEP,
    dict(STEEP, curve=0.0, inertia=1 / 9000.0, read_delay=0.015, read_batch=0.004),
    dict(inertia=1 / 9000.0, dead_zone=0.135, static=0.03, coulomb=0.02, latency=0.03, rim_zone=(60.0, 0.03)),
], ids=['steep', 'linear-proxied', 'heavy-rim'])
def test_a_wheel_that_crept_and_stopped_short_now_reaches_every_position(kw):
    """No move time-outs, no pulses dropped, a fit and a run well inside the time limit."""
    sim = SimWheel(**kw)
    cal = Calibration(sim)
    result = cal.run()
    assert cal.move_timeouts == 0 and cal.move_skips == 0
    assert result['lut'] and not result.get('partial')
    assert result['duration_s'] < 230
    assert result['fit_uncertainty'] < wc.MAX_UNCERTAINTY
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_a_move_reaches_plus_and_minus_90_degrees_from_the_far_side():
    sim, cal = _prepared(**STEEP)
    try:
        cal.kick = 0.35
        cal._learn_strength(cal._kick(1.0))          # a push, as the procedure does before moving about
        cal._brake()
        for target in (-90.0, 90.0, -90.0, 90.0, 0.0):
            t = sim.t
            assert cal._move_to(target, timeout=3.0), 'stopped at {:.1f} for {}'.format(cal.theta, target)
            assert abs(cal.theta - target) < 3.5 and sim.t - t < 2.5
    finally:
        cal._restore()


@pytest.mark.parametrize('kw', [
    dict(),
    dict(latency=0.03),
    dict(latency=0.03, read_delay=0.015, read_batch=0.004),
    dict(inertia=1 / 9000.0, dead_zone=0.11, static=0.07, coulomb=0.05, latency=0.03),
    dict(inertia=1 / 9000.0, dead_zone=0.11, static=0.07, coulomb=0.05, latency=0.03, curve=0.0, read_delay=0.03),
    dict(inertia=1 / 9000.0, dead_zone=0.11, static=0.07, coulomb=0.05, latency=0.05, curve=0.0, read_delay=0.03,
         read_batch=0.008),
    STEEP,
])
def test_the_pushs_acceleration_matches_the_simulated_wheels(kw):
    """a_kick from the first movement to V_KICK, with the wheel's latency and a proxy's delay and bunching,
    stays within 30 % of what the simulated motor gives."""
    sim, cal = _prepared(**kw)
    try:
        cal.kick = 0.35
        truth = (sim.torque_of(cal.kick) - sim.coulomb) / sim.J
        for d in (1.0, -1.0, 1.0):
            accel = cal._kick(d)
            assert 0.7 * truth < accel < 1.3 * truth, (accel, truth)
            cal._learn_strength(accel)
            cal._brake()
            cal._settle(0.2, check=False)
    finally:
        cal._restore()


@pytest.fixture(scope='module')
def recorded_run(tmp_path_factory):
    directory = tmp_path_factory.mktemp('calibration-runs')
    sim = SimWheel(dead_zone=0.11, static=0.07, coulomb=0.05, latency=0.03)
    cal = Calibration(sim, record_dir=str(directory))
    result = cal.run()
    return directory, cal, result


def test_every_run_leaves_its_recording_which_loads_and_analyses(recorded_run):
    directory, cal, result = recorded_run
    files = sorted(directory.iterdir())
    assert len(files) == 1 and files[0].name.startswith('calibration-') and files[0].name.endswith('-ok.json.gz')
    data = wcal.load_run(str(files[0]))
    assert data['outcome'] == 'ok' and data['proxied'] is False and data['format'] == wcal.RUN_FORMAT
    assert data['summary']['a_kick'] == cal.a_kick and data['constants']['MOVE_MAX'] == Calibration.MOVE_MAX
    assert data['log'] and data['summary']['result']['lut'] == result['lut']
    rec = data['rec']
    assert isinstance(rec, wc.Recording) and len(rec.cmd_t) == len(cal.rec.cmd_t)
    assert len(rec.pos_t) == len(cal.rec.pos_t) and len(rec.onsets) == len(cal.rec.onsets)
    assert rec.breakaway == [tuple(b) for b in cal.rec.breakaway] and rec.counts_per_deg == cal.rec.counts_per_deg
    again = wc.analyse(rec)                                  # offline, from the file alone
    assert again['deadband'] == pytest.approx(result['deadband'], abs=300)
    assert again['a_fs'] == pytest.approx(result['a_fs'], rel=0.02)
    assert again['fit_r2'] == pytest.approx(result['fit_r2'], abs=0.01)


def test_an_aborted_run_leaves_its_recording_too(tmp_path):
    sim = SimWheel()
    sim.unplug(20.0)
    cal = Calibration(sim, record_dir=str(tmp_path / 'runs'))
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'unplugged'
    (path,) = list((tmp_path / 'runs').iterdir())
    data = wcal.load_run(str(path))
    assert data['outcome'] == 'unplugged' and 'unplugged' in data['message']
    assert data['rec'].pos_t and data['summary']['result'] is None


def test_the_recordings_are_rotated_to_the_newest_five(tmp_path):
    paths = []
    for i in range(8):
        paths.append(wcal.write_run(str(tmp_path), {'outcome': 'ok', 'n': i}, 5))
        os.utime(paths[-1], (1000 + i, 1000 + i))
    left = sorted(tmp_path.iterdir())
    assert len(left) == 5 and {str(p) for p in left} == set(paths[3:])
    # the same second twice does not overwrite
    assert len(list(tmp_path.iterdir())) == 5 and wcal.write_run(str(tmp_path), {'outcome': 'ok'}, 5) not in left


def test_a_recording_that_cannot_be_written_does_not_change_the_outcome(tmp_path, caplog):
    blocked = tmp_path / 'file'
    blocked.write_text('x')
    sim = SimWheel()
    sim.unplug(5.0)
    cal = Calibration(sim, record_dir=str(blocked / 'runs'))
    with caplog.at_level('WARNING'):
        with pytest.raises(Aborted) as e:
            cal.run()
    assert e.value.reason == 'unplugged'
    assert any('recording not kept' in r.getMessage() for r in caplog.records)


def test_a_file_that_is_not_a_recording_is_refused(tmp_path):
    path = tmp_path / 'x.json.gz'
    with gzip.open(path, 'wt') as f:
        json.dump({'format': 99}, f)
    with pytest.raises(ValueError):
        wcal.load_run(str(path))


def test_the_window_keeps_its_recordings_next_to_the_store(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(wcal.Calibration, 'run', lambda self: made.append(self))
    gui = _gui(tmp_path, _Wheel())
    gui.start_calibration()
    cal = gui.calibration
    assert cal.record_dir == os.path.join(str(tmp_path), 'calibration-runs')
    gui.cancel_calibration(wait=1.0)


def test_pulses_starting_fifteen_degrees_short_of_the_start_still_fit(monkeypatch):
    """The field wheel stopped about 15 degrees short of +-90 before every pulse: within SHOT_START_TOL_DEG
    the pulse is played from there, and neither the fit nor the run is worse for it."""
    real = Calibration._move_to

    def short(self, target, tol=3.0, timeout=3.0, fatal=False):
        if self.phase == 'force response' and abs(abs(target) - 90.0) < 1.0:
            real(self, target - math.copysign(15.0, target), tol, timeout, fatal)
            return False
        return real(self, target, tol, timeout, fatal)

    monkeypatch.setattr(Calibration, '_move_to', short)
    sim = SimWheel(dead_zone=0.11, static=0.07, coulomb=0.05, latency=0.03)
    cal = Calibration(sim)
    result = cal.run()
    assert cal.move_skips == 0 and result['lut'] and result['fit_uncertainty'] < wc.MAX_UNCERTAINTY
    assert result['duration_s'] < 200
    assert_safe_run(sim, cal)
    assert_put_back(sim)


# --- a real G29, fitted ---

def g29_field_recording():
    """The calibration run of a real G29 behind the proxy (2026-11-02), as tests/data/g29-calibration-fit.npz
    keeps it: times in 0.1 ms and positions in encoder counts (1/72.8 degree), both as differences, the
    commands only where they changed. This run was refused ("fit 0.69, uncertainty 16 %") by the first
    version of the fit, whose single window length could not see the brief hard pulses."""
    z = np.load(os.path.join(os.path.dirname(__file__), 'data', 'g29-calibration-fit.npz'))
    rec = wc.Recording()
    rec.pos_t = [t / 1e4 for t in np.cumsum(z['pos_dt'].astype(np.int64)).tolist()]
    rec.pos = (np.cumsum(z['pos_dc'].astype(np.int64)) / float(z['cpd'])).tolist()
    rec.cmd_t = [t / 1e4 for t in z['cmd_t'].tolist()]
    rec.cmd = (z['cmd_w'].astype(float) / 30000).tolist()
    rec.breakaway = [tuple(b) for b in z['breakaway'].tolist()]
    rec.counts_per_deg = float(z['cpd'])
    return rec


@pytest.fixture(scope='module')
def field_result():
    return wc.analyse(g29_field_recording())


def test_the_real_g29_recording_fits_well(field_result):
    r = field_result
    assert r['fit_r2'] > 0.85
    assert r['fit_uncertainty'] < wc.MAX_UNCERTAINTY
    assert 2 <= r['fit_latency_ms'] <= 10
    # a motor that gives nothing below ~8 % of full scale and about 15000 deg/s^2 at full scale
    assert 0.05 < r['deadband'] / wc.FULL < 0.12
    assert 10000 < r['a_fs'] < 20000
    assert 0.03 < r['friction'] < 0.1


def test_the_real_g29s_correction_is_sane(field_result):
    lut = field_result['lut']
    assert wcal.valid_lut(lut) and wc.plausible_lut(lut)
    assert all(b > a for a, b in zip(lut, lut[1:]))
    # no large gain where the wheel is touchy (the bottom quarter), the soft end stays inside the range
    n = len(lut) - 1
    assert all(lut[i + 1] - lut[i] < 3 * wc.FULL / n for i in range(n // 4))
    # no segment steeper than the cap (the raw curve's step asked for 4.8 times the identity)
    assert max(b - a for a, b in zip(lut, lut[1:])) <= wc.LUT_SEGMENT_SLOPE * wc.FULL / n + 1
    assert field_result['dither'] is True
    curve = field_result['curve']
    assert all(b >= a for a, b in zip(curve, curve[1:])) and curve[0] == 0.0 and curve[-1] == 1.0


def _with_a_hand(rec, seed, strength):
    """The recording with a random hand on the wheel: a force that changes sign every 20-60 ms is added to the
    wheel's movement (positions only, the commands stay as written)."""
    rng = np.random.default_rng(seed)
    t = np.asarray(rec.pos_t)
    grid = np.arange(t[0], t[-1], 0.001)
    steps = rng.integers(20, 60, size=len(grid) // 20 + 2)
    force = np.repeat(rng.uniform(-1, 1, size=len(steps)), steps)[:len(grid)]
    acc = strength * force
    v = np.cumsum(acc) * 0.001
    x = np.cumsum(v) * 0.001
    out = wc.Recording()
    out.__dict__.update(rec.__dict__)
    out.pos = (np.asarray(rec.pos) + np.interp(t, grid, x)).tolist()
    return out


@pytest.mark.parametrize('seed', [1, 2, 3])
def test_a_hand_on_the_real_wheel_is_refused(seed):
    with pytest.raises(wc.FitError):
        wc.analyse(_with_a_hand(g29_field_recording(), seed, 15000.0))


@pytest.mark.parametrize('seed', [1, 2])
def test_a_hand_on_a_simulated_wheel_is_refused(g29_like, seed):
    sim, cal, result = g29_like
    with pytest.raises(wc.FitError):
        wc.analyse(_with_a_hand(cal.rec, seed, 15000.0))


def test_the_lut_slope_cap_gives_up_linearity_for_a_gentler_curve():
    n = wc.LUT_POINTS - 1
    steep = [0] + [wc.FULL // 5] * (n - 1) + [wc.FULL]            # a jump at the start, a flat run, a jump at the end
    capped = wc.cap_slope(steep)
    assert capped[0] == 0 and capped[-1] == wc.FULL
    assert all(0 < b - a <= wc.LUT_SEGMENT_SLOPE * wc.FULL / n + 1 for a, b in zip(capped, capped[1:]))
    ident = [round(i * wc.FULL / n) for i in range(wc.LUT_POINTS)]
    assert wc.cap_slope(ident) == ident


# --- a hand on the rim after the push, and what is put back afterwards ---

def _stop_dead(sim):
    """A hand that stops the rim at once and holds it."""
    sim.coulomb = sim.static = 2.0


def test_a_hand_stopping_the_rim_in_the_run_up_stops_the_run(monkeypatch):
    sim = SimWheel()
    real = Calibration._kick
    calls = []

    def kick(self, d):
        accel = real(self, d)
        calls.append(sim.t)
        if len(calls) == 12:                                     # a pulse's push, well into the force response
            _stop_dead(sim)
        return accel

    monkeypatch.setattr(Calibration, '_kick', kick)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'grabbed'
    assert sim.t - calls[11] < 1.0                               # (it used to hold the push until TOTAL_S)
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_a_push_that_turns_nothing_stops_the_run_whatever_the_loop(monkeypatch):
    sim = SimWheel(coulomb=2.0, static=2.0)
    seen = []

    def measure(self):
        seen.append(sim.t)
        while True:
            self._tick(0.4)

    monkeypatch.setattr(Calibration, '_measure', measure)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'grabbed'
    assert sim.t - seen[0] < 1.2
    assert sim.last('ext_force') == '0'


def test_centring_after_a_near_lock_gives_up_after_a_second_and_a_half(monkeypatch):
    sim = SimWheel(coulomb=2.0, static=2.0, start_deg=100.0)
    seen = []

    def measure(self):
        self.sign_known, self.sign = True, -1.0
        self.recovering = True
        seen.append(sim.t)
        self._move_to(0.0, tol=10.0, timeout=8.0, fatal=True)

    monkeypatch.setattr(Calibration, '_measure', measure)
    cal = Calibration(sim)
    with pytest.raises(Aborted) as e:
        cal.run()
    assert e.value.reason == 'grabbed'
    assert sim.t - seen[0] < 2.5
    assert sim.last('ext_force') == '0'


def _abort_with_wheel_at(sim, monkeypatch, breakaway=0.12):
    def measure(self):
        self.sign_known, self.sign = True, -1.0
        self.breakaway_max = breakaway
        raise Aborted('cancelled', 'x')
    monkeypatch.setattr(Calibration, '_measure', measure)
    ranges = []
    real = sim.set_range
    sim.set_range = lambda v: (ranges.append((v, sim.theta)), real(v))
    cal = Calibration(sim)
    with pytest.raises(Aborted):
        cal.run()
    return ranges


def test_the_users_smaller_range_is_never_put_back_with_the_wheel_outside_it(monkeypatch):
    sim = SimWheel(range=360, start_deg=200.0)                   # the lock of 360 degrees is at 180
    ranges = _abort_with_wheel_at(sim, monkeypatch)
    assert ranges[-1][0] == 360 and abs(ranges[-1][1]) <= 180 - Calibration.LOCK_MARGIN_DEG
    assert sim.range == 360


def test_a_wheel_that_cannot_be_centred_gets_a_wider_range_instead(monkeypatch):
    sim = SimWheel(range=360, start_deg=200.0, coulomb=2.0, static=2.0)
    ranges = _abort_with_wheel_at(sim, monkeypatch)
    v, theta = ranges[-1]
    assert v > 360 and v / 2 >= abs(theta) + Calibration.LOCK_MARGIN_DEG - 1


def test_a_killed_runs_cap_gain_and_range_go_back_under_countersteer_ffb(tmp_path):
    from oversteer import wheel_owner
    path = str(tmp_path / 'pending.json')
    saved = {'ext_force_cap': '80', 'ffb_lut': 'identity', 'gain': 110, 'range': 540, 'autocenter': 30,
             'ffb_output_cap': '70'}
    wcal.write_pending(path, 'k', saved)
    sim = SimWheel()
    assert wheel_owner.acquire('w', 'countersteer-ffb', on_inhibit=lambda: None)
    try:
        assert wcal.recover_pending(path, 'k', sim, wheel='w') is False
        written = {n for _t, n, _v in sim.writes}
        assert written == {'ext_force_cap', 'ffb_output_cap', 'gain', 'range', 'autocenter'}
        assert sim.attrs['ffb_output_cap'] == '70' and sim.gain == 110 and sim.range == 540
        assert wcal.take_pending(path, 'k') == {'ffb_lut': 'identity'}
    finally:
        wheel_owner.release('w', 'countersteer-ffb')
    assert wheel_owner.acquire('w', 'wheel-test', exclusive=True)
    try:
        sim.writes.clear()
        assert wcal.recover_pending(path, 'k', sim, wheel='w') is False and sim.writes == []
    finally:
        wheel_owner.release('w', 'wheel-test')
    assert wcal.recover_pending(path, 'k', sim, wheel='w') is True
    assert sim.last('ext_force') == '0' and sim.attrs['game_ffb_level'] == '100'


def test_the_output_cap_is_set_to_100_for_the_run_and_put_back():
    sim = SimWheel(inertia=1 / 15000, latency=0.025)
    sim.attrs['ffb_output_cap'] = '60'
    cal = Calibration(sim)
    result = cal.run()
    assert result['lut']
    first_force = next(i for i, (_t, n, v) in enumerate(sim.writes) if n == 'ext_force' and v != '0')
    assert {n: v for _t, n, v in sim.writes[:first_force]}['ffb_output_cap'] == '100'
    assert_put_back(sim, attrs={'ffb_output_cap': '60'})


def test_the_drivers_12_ms_slew_does_not_bias_the_latency_or_the_strength():
    sim = SimWheel(slew_s=0.012)
    cal = Calibration(sim)
    result = cal.run()
    assert abs(result['fit_latency_ms'] - sim.latency * 1000) <= 4
    assert abs(result['a_fs'] * sim.J - 1) < 0.15
    assert linearity_error(sim, result) < 0.04
    assert_safe_run(sim, cal)
    assert_put_back(sim)


def test_the_deadband_lift_fades_in_over_the_luts_first_512_like_the_drivers():
    ident = [round(i * wc.FULL / 32) for i in range(33)]
    d = 3000
    outs = [wc.shaped(x, ident, d) for x in range(0, 6000, 50)]
    assert outs[0] == 0.0 and all(b >= a for a, b in zip(outs, outs[1:]))
    # m = 256 of the LUT's output: half the lift's usual size, and continuous at 512
    assert wc.shaped(256, ident, d) == pytest.approx((d + (wc.FULL - d) * 256 / wc.FULL) * 0.5, rel=1e-3)
    assert wc.shaped(512, ident, d) == pytest.approx(d + (wc.FULL - d) * 512 / wc.FULL, rel=1e-3)
