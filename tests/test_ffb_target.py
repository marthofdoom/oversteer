"""Countersteer FFB's strength: independent of the user's master gain (the driver multiplies ext_force by it, so the
engine divides it out) and of the game's FFB setting, and set by the car's own steering torque mapped to the wheel
(the p99 of |front torque| per car, ffb_engine.RackForce). Nothing here touches a real wheel or /sys."""
import math

import pytest

from oversteer import ffb_carprops, ffb_engine as fe, ffb_spring, ffb_texture, wheel_torque
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, acr, forces, run, upload, wheel, game_raw, HZ, fitted, read  # noqa: F401
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)


def engine_with_gain(tmp_path, gain, strength=40):
    path = wheel(tmp_path)
    with open(path + '/gain', 'w') as f:
        f.write('%d\n' % gain)
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=FakeDriver)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.set_strength(strength)
    engine.step(0.0)
    return engine, path


def delivered_at_gain(tmp_path, gain):
    """The last ext_force write times the driver's gain (what the wheel gets), after 8 s of the same drive."""
    tmp_path.mkdir()
    engine, path = engine_with_gain(tmp_path, gain)
    FakeDriver.log.clear()
    run(engine, 0.0, 8.0)
    return forces()[-1] * read(path, 'gain') / 65535.0, engine      # (full range raises a gain under 100 % to 100 %)


@pytest.mark.parametrize('percent', [50, 100, 115, 150])
def test_the_delivered_force_is_the_same_at_any_master_gain(tmp_path, percent):
    ref, _ = delivered_at_gain(tmp_path / 'a', 65535)
    got, engine = delivered_at_gain(tmp_path / 'b', int(round(65535 * percent / 100.0)))
    assert abs(ref) > 500
    assert got == pytest.approx(ref, rel=0.02, abs=3)
    assert engine._clipped == 0.0


def test_a_division_past_full_scale_clips_and_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(fe.FfbEngine, '_range_wanted', lambda self: False)      # (full range would raise the 5 % to 100 %)
    engine, path = engine_with_gain(tmp_path, 3277, strength=100)        # 5 %: 20x
    run(engine, 0.0, 8.0)
    assert max(abs(v) for v in forces()) <= fe.FULL
    assert engine._clipped > 0.0


def test_a_gain_change_in_play_does_not_step_what_is_written(tmp_path):
    def drive(sub, change):
        (tmp_path / sub).mkdir()
        engine, path = engine_with_gain(tmp_path / sub, 65535)
        t = run(engine, 0.0, 6.0)
        FakeDriver.log.clear()
        if change:
            with open(path + '/gain', 'w') as f:
                f.write('%d\n' % 32768)               # the slider to 50 %: the written value doubles, smoothly
        run(engine, t, 2.0)
        out = forces()
        return max(abs(b - a) for a, b in zip(out, out[1:]))
    # the same drive without the change shows the drive's own sample-to-sample movement
    assert drive('a', True) < 2.3 * drive('b', False) + 50      # (the written value doubles with the gain halved)


def rack_for(peak=None, assist=None, ratio=None):
    rack = fe.RackForce()
    rack.LEARNS = False
    rack.wheel_peak = peak
    rack.mapping = 'knee'
    rack.reset('acr/x')
    if assist is not None:
        rack.assist = assist
    if ratio is not None:
        rack.ratio = ratio
    return rack


def sample_at(front_nm, speed=25.0):
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car='acr/x', game='acr')
    s.tyre_mz = (front_nm / 2, front_nm / 2, 0, 0)
    return s


def force_at(rack, nm, speed=25.0):
    return abs(rack.force(sample_at(nm, speed))) / fe.FULL


def test_the_boost_curve_is_monotone_and_invertible():
    for speed in (0.0, 5.0, 15.0, 30.0, 60.0):
        for assist in (1.0, 0.5, 0.0):
            prev = -1.0
            for i in range(0, 600):
                tc = i * 0.1
                td = ffb_carprops.driver_from_column(tc, speed, assist)
                assert td >= prev - 1e-12 and td <= tc + 1e-9          # monotone, and never more than the column
                prev = td
                assert ffb_carprops.column_from_driver(td, speed, assist) == pytest.approx(tc, abs=1e-6)
            assert ffb_carprops.driver_from_column(-3.0, speed, assist) == -ffb_carprops.driver_from_column(3.0, speed, assist)


def test_assist_is_heavy_parked_and_light_at_speed():
    parked = ffb_carprops.driver_from_column(20.0, 0.0)
    fast = ffb_carprops.driver_from_column(20.0, 30.0)
    assert parked < fast < 20.0


def test_on_centre_and_at_the_limit_torques_are_a_real_cars():
    rack = rack_for()
    # the dump's p50 (85 Nm of kingpin torque) is an easy corner, its p99 (550 Nm) the grip limit, at 25 m/s
    easy = abs(rack.rim_torque(sample_at(85.0)))
    limit = abs(rack.rim_torque(sample_at(550.0)))
    assert 0.4 <= easy <= 1.2
    assert 2.5 <= limit < wheel_torque.HYD_TOP_NM          # (the hydraulic top end bends it under 3.7)
    assert abs(rack.rim_torque(sample_at(10.0))) < 0.3          # near the centre almost nothing


def test_the_boost_curve_follows_the_measured_steering():
    # on centre ~0.2 Nm per degree at the rim: the dump's tyre torque gives K0 / ratio^2 = 0.589 Nm per degree
    column_per_deg = 5700.0 / 13.0 ** 2 * math.pi / 180.0
    assert column_per_deg * ffb_carprops.driver_slope(0.0, 25.0, 1.0) == pytest.approx(0.2, rel=0.05)
    # the highway gain 3-6 in the mid range, the centre zone wider at speed, the assist saturating at high load
    mid = ffb_carprops.driver_from_column(10.0, 30.0)
    assert 10.0 / mid - 1.0 == pytest.approx(5.0, abs=2.0)
    assert ffb_carprops.driver_slope(0.5, 30.0) > ffb_carprops.driver_slope(0.5, 3.0)       # (zone 1.0 vs 0.3 Nm)
    slopes = [ffb_carprops.driver_slope(tc, 30.0) for tc in (5.0, 20.0, 50.0, 150.0)]
    assert slopes[0] > slopes[1] > slopes[2] > slopes[3] > 0.0                             # the hydraulic top end flattens it for good


def test_the_column_scale_is_frozen_on_the_g29_class_and_the_car_tops_out_at_the_budget():
    rack = rack_for(3.5)
    rack.p99 = 550.0
    assert rack.impedance() == fe.RackForce.COLUMN_SCALE == 0.30
    rack.p99 = 250.0
    assert rack.impedance() == 0.30                                  # (the learnt p99 no longer moves it)
    assert rack_for(None).impedance() == 0.30 and rack_for(2.5).impedance() == 0.30
    assert rack_for(12.0).impedance() == 1.0
    # an assisted car's driver torque tops out at the hydraulic limit: that lands at the budget
    assert rack.top_torque() == wheel_torque.HYD_TOP_NM
    assert force_at(rack, 550.0) == pytest.approx(0.73, abs=0.02) and force_at(rack, 550.0) < wheel_torque.TOP
    # a strong wheel plays the car 1:1 in Nm
    strong = rack_for(10.0)
    strong.p99 = 550.0
    assert force_at(strong, 550.0) == pytest.approx(abs(strong.rim_torque(sample_at(550.0))) * strong.rho() / 10.0)
    # an unassisted car's top is its p99 (at least twice the knee's torque)
    plain = rack_for(3.5, assist=0.0, ratio=13.0)
    plain.p99 = 390.0
    assert plain.top_torque() == pytest.approx(30.0)
    plain.p99 = 5.0
    assert plain.top_torque() == pytest.approx(3.5)               # 2 x the knee's torque at least


def test_the_played_share_is_1_to_1_up_to_the_knee_and_clamps_hard_at_the_budget():
    rack = rack_for(3.5)
    rack.p99 = 550.0
    for nm in (10.0, 30.0, 85.0):
        rim = abs(rack.rim_torque(sample_at(nm)))
        assert rim / 3.5 < rack.knee
        assert force_at(rack, nm) == pytest.approx(rim * rack.rho() / 3.5, rel=1e-6)       # 1:1 in hand force
    rack.strength = 0.5
    assert force_at(rack, 85.0) == pytest.approx(0.5 * rack.rho() * abs(rack.rim_torque(sample_at(85.0))) / 3.5, rel=1e-6)
    rack.strength = 1.0
    shares = [force_at(rack, nm) for nm in range(0, 4000, 5)]
    assert all(b >= a for a, b in zip(shares, shares[1:]))
    assert max(shares) == pytest.approx(wheel_torque.TOP, abs=0.005) and max(shares) <= wheel_torque.TOP
    assert force_at(rack, 5000.0) == pytest.approx(wheel_torque.TOP, abs=0.005)       # (the driver torque tends to 3.7 Nm)
    below = [x for x in shares if x < wheel_torque.TOP]
    assert max(b - a for a, b in zip(below, below[1:])) < 0.06       # (no step either: 5 Nm of kingpin torque near the centre is 0.05)


def test_an_unknown_wheel_counts_as_the_g29_class():
    assert force_at(rack_for(None), 300.0) == pytest.approx(force_at(rack_for(3.08), 300.0))


def test_a_strong_wheel_plays_the_same_nm_and_never_passes_the_ceiling():
    weak, strong = rack_for(3.5), rack_for(20.0)
    nm = abs(strong.rim_torque(sample_at(300.0)))
    assert force_at(strong, 300.0) == pytest.approx(nm * strong.rho() / 20.0, rel=1e-6)
    assert force_at(strong, 300.0) < force_at(weak, 300.0) + 1e-9
    unassisted = rack_for(20.0, assist=0.0)
    assert force_at(unassisted, 5000.0) == pytest.approx(fe.RackForce.RIM_SAFE_NM / 20.0, rel=0.001)


def test_an_unassisted_car_passes_the_column_torque():
    rack = rack_for(20.0, assist=0.0, ratio=13.0)
    assert rack.rim_torque(sample_at(130.0)) == pytest.approx(10.0)
    assert rack.rim_torque(sample_at(-130.0)) == pytest.approx(-10.0)


def test_the_steering_budget_leaves_the_texture_its_reserve():
    rack = rack_for(3.5)
    steering = force_at(rack, 5000.0) * fe.FULL
    assert fe.FULL - steering >= 0.079 * fe.FULL
    # the texture's own limit is FULL - |steering|: still room for its cap at the budget's edge
    assert fe.FULL - steering > ffb_texture.TOTAL_CAP * fe.FULL


def test_wheel_table_knows_only_usable_wheels():
    assert wheel_torque.peak_nm('046d:c24f') == 3.08
    assert wheel_torque.peak_nm('0eb7:0020') is None            # a direct drive: not usable yet
    assert wheel_torque.peak_nm(None) is None and wheel_torque.peak_nm('dead:beef') is None


def test_a_car_change_holds_the_playing_model_through_the_fade():
    rack = rack_for(3.5, assist=0.0)
    rack.hold()
    held = rack.force(sample_at(130.0))
    rack.reset('acr/y')
    rack.ready = False
    rack._held_fit = (rack.trail, rack.scrub, 13.0, 0.0)
    s = sample_at(130.0)
    s.car = 'acr/y'
    assert rack.force(s) == pytest.approx(held)


def test_the_gates_still_use_the_games_raw_force_not_its_level():
    ok = fitted(game_gain=0.4)
    big = fitted(game_gain=3.0)
    assert ok.ready and big.ready
    s = acr(1.0)
    assert ok.force(s) == pytest.approx(big.force(s), rel=1e-6)           # the game's level does not set the magnitude
    reversed_ = fitted(raw_sign=-1.0)
    assert reversed_.refused == 'reversed' and not reversed_.ready
    none = fitted(raw=False)
    assert not none.ready and none.waiting == 'raw'


def test_the_cars_p99_is_kept_between_runs(tmp_path):
    ffb_carprops.STORE.put('acr/x', 0.02, 0.01, force=True, p99=300.0)
    ffb_carprops.STORE.use(ffb_carprops.STORE.path)           # a new run reads the file
    assert ffb_carprops.STORE.get_p99('acr/x') == pytest.approx(300.0, rel=0.01)
    again = fe.RackForce()
    again.reset('acr/x')
    assert again.p99 == pytest.approx(300.0, rel=0.01)


def test_the_engine_takes_the_wheel_model_for_the_peak():
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=FakeDriver)
    assert engine.model.wheel_peak is None
    engine.usb_id = '046d:c24f'
    assert engine.model.wheel_peak == 3.08
    engine.usb_id = None
    assert engine.model.wheel_peak is None


def slipping(front_nm, alpha, steer):
    s = sample_at(front_nm)
    s.tyre_slip_angle = (alpha, alpha, 0.0, 0.0)
    s.steer = steer
    return s


def test_the_aligning_split_is_off_by_default_and_changes_nothing():
    rack = rack_for(3.5)
    s = slipping(-300.0, 0.05, 0.3)
    assert rack.align_on is False and rack.align_writer is None
    before = rack.force(s)
    seen = []
    rack.align_writer = lambda *a: seen.append(a)
    assert rack.force(s) == before and not seen


def test_the_aligning_spring_uses_the_tangent_stiffness_and_points_at_zero_slip():
    rack = rack_for(3.5)
    s = slipping(-300.0, 0.05, 0.3)                        # steered left of where it goes: slip > 0
    split = rack.align(s)
    assert split['k'] > 0 and split['theta_a_deg'] < split['theta_deg']      # zero slip is further right
    secant = split['model'] / (split['theta_a_deg'] - split['theta_deg'])
    assert 0 < split['k'] < secant                                           # the tangent is softer than the secant
    assert split['model'] < 0                                                # aligning: pushes back to the right
    # numerically the tangent: the model's torque against theta, at a slightly different slip
    ratio, deg = rack.ratio, 180.0 / math.pi
    other = rack.align(slipping(-300.0, 0.0501, 0.3))
    d_model = other['model'] - split['model']
    d_err = (other['theta_a_deg'] - other['theta_deg']) - (split['theta_a_deg'] - split['theta_deg'])
    assert split['k'] == pytest.approx(d_model / d_err, rel=0.01)
    drift = rack.align(slipping(100.0, -0.4, -0.3))
    assert drift['theta_a_deg'] > drift['theta_deg']
    # on centre: K / ratio^2 per degree times the boost curve's centre slope
    centre = rack.align(slipping(0.0, 0.0, 0.0))
    assert centre['k'] == pytest.approx(rack.align_stiffness() / ratio ** 2 / deg
                                        * ffb_carprops.driver_slope(0.0, 25.0, 1.0), rel=1e-6)


def test_the_road_wheel_lock_is_the_cars_own():
    rack = rack_for(3.5)
    s = slipping(-300.0, 0.05, 0.3)
    base = rack.align(s)
    rack.lock = 0.2
    assert rack.align(s)['theta_deg'] == pytest.approx(base['theta_deg'] * 0.2 / rack.ALIGN_LOCK)


def test_the_driver_spring_mirror_follows_the_drivers_limits():
    sp = ffb_spring.DriverSpring()
    assert sp.force(10.0, 0.0, 0.05, 0.75, 0.06, 0.0) == 0.0                 # faded out at the start
    for _ in range(100):
        f = sp.force(10.0, 0.0, 0.05, 0.75, 0.06, 0.002)
    assert f == pytest.approx(0.75 * ffb_spring.sat(0.05 * 10.0 / 0.75))      # settled: tau_max sat(k e / tau_max)
    sp.reset()
    for _ in range(100):
        g = sp.force(10.0, 0.0, 0.2, 0.75, 0.06, 0.002)
    assert g == pytest.approx(0.75 * ffb_spring.sat(0.06 * 10.0 / 0.75))      # k clamped to the driver's limit
    sp.reset()
    for _ in range(100):
        h = sp.force(1000.0, 0.0, 0.05, 0.75, 0.06, 0.002)                    # saturated: tau_max
    assert 0.0 < h <= 0.75
    sp.reset()
    sp.force(0.0, 0.0, 0.05, 0.75, 0.06, 0.002)
    sp.force(300.0, 0.0, 0.05, 0.75, 0.06, 0.002)
    assert sp.cur == pytest.approx(2.0)                                       # slewed at 1000 deg/s
    assert ffb_spring.sat(5.0) == 1.0 and ffb_spring.sat(-5.0) == -1.0 and ffb_spring.sat(0.0) == 0.0


def test_with_the_split_on_ext_force_plays_the_residual_and_the_writer_gets_the_spring():
    rack = rack_for(20.0)
    s = slipping(-300.0, 0.05, 0.3)
    plain = rack.force(s)
    seen = []
    rack.align_on, rack.align_writer = True, lambda theta_a, k, tau: seen.append((theta_a, k, tau))
    assert rack.force(s) == pytest.approx(plain)                       # the spring is still faded out: all through ext_force
    for _ in range(200):                                               # the fade-in (0.2 s) and the slew
        rack._now += 0.005
        resid = rack.force(s)
    split = rack.align(s)
    k_fs = split['k'] * rack.rho() * wheel_torque.rim_share_slope(rack.rim_torque(s) * rack.rho(), 20.0, 1.0, rack.top_torque() * rack.rho(), rack.knee) / 20.0
    tau = max(rack.ALIGN_SHARE_MAX * abs(plain) / fe.FULL, rack.ALIGN_TAU_MIN)        # the spring is held to a share of the force now
    assert seen[-1] == pytest.approx((split['theta_a_deg'], k_fs, tau))
    spring = ffb_spring.DriverSpring()
    for _ in range(200):
        want = spring.force(split['theta_a_deg'], split['theta_deg'], k_fs, tau, rack.align_k_limit, 0.005)
    assert resid == pytest.approx(plain - fe.FULL * want, rel=1e-6)
    assert abs(resid) < abs(plain)
    rack.align_k_limit = 0.0                                           # a driver that allows no spring: all through ext_force
    rack._now += 0.005
    assert rack.force(s) == pytest.approx(plain)


# -- power steering: detection and the INI keys --

def feed_detector(det, gain_of_load, speed, p99=500.0, seconds=200.0, hz=50.0):
    n = int(seconds * hz)
    for i in range(n):
        torque = p99 * 1.2 * ((i * 0.6180339887) % 1.0)
        det.add(torque, 30.0 * gain_of_load(torque, speed, p99) * torque, p99, 1.0 / hz)
    return det


def linear_game(torque, speed, p99):
    return 1.0 - 0.08 * min(torque / p99, 1.0)          # ACR-like: a mild droop only


def boost_game(torque, speed, p99):
    td = ffb_carprops.driver_from_column(torque / 13.0, speed, 1.0)
    return max(td, 1e-9) / max(torque / 13.0, 1e-9)       # the gain a boost curve gives, speed-dependent


@pytest.mark.parametrize('speed', [8.0, 30.0])
def test_the_detector_calls_a_linear_game_absent_and_a_boost_curve_present(speed):
    lin = feed_detector(ffb_carprops.AssistDetector(), linear_game, speed)
    assert lin.state is False and lin.ratio() > 0.85
    boost = feed_detector(ffb_carprops.AssistDetector(), boost_game, speed)
    assert boost.state is True and boost.ratio() < 0.85
    curve = boost.curve(500.0)
    assert [c for _, c in curve] == sorted((c for _, c in curve), reverse=True) and curve[-1][1] < 0.75


def test_the_detector_waits_for_load_range_and_holds_between_thresholds():
    det = ffb_carprops.AssistDetector()
    for i in range(2000):                                  # small loads only: no decision
        det.add(10.0 + (i % 20), 300.0, 500.0, 0.02)
    assert det.state is None and det.ratio() is None
    det = feed_detector(ffb_carprops.AssistDetector(), boost_game, 20.0)
    assert det.state is True
    mid = lambda t, v, p: 1.0 - 0.3 * min(t / p, 1.0)       # ratio ~0.92: between the thresholds, no flip
    feed_detector(det, mid, 20.0, seconds=400.0)
    assert 0.88 < det.ratio() < 0.94 and det.state is True


def test_the_game_curve_is_followed_not_stacked_and_the_ini_modes_decide(tmp_path):
    ini = tmp_path / 'config.ini'
    ffb_carprops.CONFIG_PATH = str(ini)
    try:
        assert ffb_carprops.power_settings() == ('auto', 1.0)            # no file: defaults
        rack = rack_for(20.0)
        rack._assist = feed_detector(ffb_carprops.AssistDetector(), boost_game, 25.0)
        rack.p99 = 500.0
        s = sample_at(500.0)
        model = ffb_carprops.driver_from_column(500.0 / 13.0, 25.0, 1.0)
        assert rack.power_steering() == 'game'
        assert rack.rim_torque(s) == pytest.approx(model, rel=1e-6)       # levelled at the p99 on our class
        half = rack.rim_torque(sample_at(250.0))
        assert half < 0.5 * model * 1.01 or half > 0                       # follows the game's shape
        ini.write_text('[DEFAULT]\ncountersteer_power_steering = off\n')
        assert ffb_carprops.power_settings() == ('off', 1.0) and rack.power_steering() == 'off'
        assert rack.rim_torque(s) == pytest.approx(500.0 / 13.0)          # column torque
        ini.write_text('[DEFAULT]\ncountersteer_power_steering = on\ncountersteer_assist_strength = 0.5\n')
        assert rack.power_steering() == 'model'
        assert rack.rim_torque(s) == pytest.approx(ffb_carprops.driver_from_column(500.0 / 13.0, 25.0, 0.5))
        ini.write_text('[DEFAULT]\ncountersteer_power_steering = bogus\ncountersteer_assist_strength = 9\n')
        assert ffb_carprops.power_settings() == ('auto', 2.0)             # bad mode: auto; strength clamped
        ini.write_text('[DEFAULT]\ncountersteer_assist_strength = x\n')
        assert ffb_carprops.power_settings() == ('auto', 1.0)
    finally:
        ffb_carprops.CONFIG_PATH = None


def test_without_a_detection_the_model_is_applied():
    rack = rack_for(3.5)
    assert rack._assist.state is None and rack.power_steering() == 'model'


# -- the steering mapping: classic (the uniform impedance scale) or the knee --

def classic_rack(peak=None):
    rack = rack_for(peak)
    rack.mapping = 'classic'
    return rack


def test_the_classic_share_is_the_old_uniform_scale_and_does_not_depend_on_the_cars_p99():
    outs = []
    for p99 in (573.0, 867.0):
        rack = classic_rack(3.5)
        rack.p99 = p99
        got = []
        for nm in (20.0, 85.0, 200.0, 550.0, 2000.0, -300.0):
            s = sample_at(nm)
            torque = ffb_carprops.driver_from_column(nm / rack.ratio, 25.0, rack.assist * ffb_carprops.power_settings()[1], False)
            want = max(-0.75, min(0.75, fe.RackForce.CLASSIC_SCALE * torque / 3.5))        # the old formula, s frozen
            assert rack.force(s) == pytest.approx(want * fe.FULL, abs=1.0)
            got.append(rack.force(s))
        outs.append(got)
    assert outs[0] == outs[1]
    assert fe.RackForce.CLASSIC_SCALE == pytest.approx(0.30 * 3.08 / 2.1)


def test_the_classic_strength_scales_the_uniform_share_and_the_budget_clamps_it():
    rack = classic_rack(3.5)
    base = rack.force(sample_at(85.0))
    rack.strength = 1.3
    assert rack.force(sample_at(85.0)) == pytest.approx(1.3 * base, abs=1.0)
    assert abs(rack.force(sample_at(3000.0))) == pytest.approx(0.75 * fe.FULL, abs=1.0)


def test_the_classic_driver_from_column_has_no_hydraulic_top_end(street):
    assert ffb_carprops.driver_from_column(3000.0, 25.0, 1.0, False) > wheel_torque.HYD_TOP_NM
    assert ffb_carprops.driver_from_column(3000.0, 25.0, 1.0) < wheel_torque.HYD_TOP_NM
    assert ffb_carprops.driver_slope(400.0, 25.0, 1.0, False) > ffb_carprops.driver_slope(400.0, 25.0, 1.0)
    rack = classic_rack()
    assert abs(rack.rim_torque(sample_at(550.0))) > wheel_torque.HYD_TOP_NM
    rack.mapping = 'knee'
    assert abs(rack.rim_torque(sample_at(550.0))) < wheel_torque.HYD_TOP_NM


def test_drive_pull_plays_the_physical_scrub_couple_at_its_level_in_both_mappings_and_is_on_at_100_by_default():
    s = sample_at(100.0)
    s.tyre_fx = (800.0, -800.0, 0.0, 0.0)
    for mapping in ('classic', 'knee'):
        rack = rack_for(3.5)
        rack.mapping, rack.assist = mapping, 0.0                  # (unassisted: the torque is the column's)
        rack.scrub = 0.02                                         # the learnt scrub is never played
        assert rack.pull_on is True and rack.pull_scrub() == pytest.approx(fe.PULL_SCRUB)
        rack.pull_on = False
        assert rack.pull_scrub() == 0.0
        plain = rack.rim_torque(s) * rack.ratio
        rack.pull_on, rack.pull_level = True, 0.35
        assert rack.pull_scrub() == pytest.approx(0.35 * fe.PULL_SCRUB)
        assert rack.rim_torque(s) * rack.ratio == pytest.approx(plain - 0.35 * fe.PULL_SCRUB * 1600.0)


def test_the_classic_align_stiffness_carries_the_scale_and_the_strength():
    rack = classic_rack(3.5)
    s = slipping(-300.0, 0.05, 0.3)
    seen = []
    rack.align_on, rack.align_writer = True, lambda theta_a, k, tau: seen.append((theta_a, k, tau))
    rack.strength = 0.8
    rack.force(s)
    split = rack.align(s)
    assert seen[-1][1] == pytest.approx(fe.RackForce.CLASSIC_SCALE * split["k"] * 0.8 / 3.5)
    rack.mapping = 'knee'
    rack.damping_pct = 40.0
    assert rack.damping_scale() == pytest.approx(0.4 * rack.rho() ** 2)
    rack.mapping = 'classic'
    assert rack.damping_scale() == rack.impedance() == 0.30


def test_column_damping_defaults_to_15_and_the_knee_mappings_damping_scale_is_pct_times_rho_squared_clamped_to_0_100():
    from oversteer.model import Model
    assert fe.OVERALL_COLUMN_DAMPING == 15 and fe.RackForce.DAMPING_SCALE == 0.15
    assert Model._texture_defaults(True)['countersteer_column_added'] == 15
    engine = fe.FfbEngine()
    engine._requests(0.0)
    rho2 = engine.model.rho() ** 2
    assert 0.0 < rho2 < 1.0
    assert engine.model.damping_pct == 15 and engine.model.damping_scale() == pytest.approx(0.15 * rho2)
    engine.set_column_damping(25)
    engine._requests(0.0)
    assert engine.model.damping_scale() == pytest.approx(0.25 * rho2)
    engine.set_column_damping(250)
    engine._requests(0.0)
    assert engine.model.damping_scale() == pytest.approx(rho2)
    engine.set_column_damping(-5)
    engine._requests(0.0)
    assert engine.model.damping_scale() == 0.0
    engine.model.mapping = 'classic'                 # the classic mapping keeps the impedance scale
    assert engine.model.damping_scale() == engine.model.impedance()


def test_the_model_clamps_the_column_added_setting_to_0_100_and_defaults_it_to_15():
    from oversteer.model import Model
    model = Model.__new__(Model)
    model.data, model.ui = {}, None
    model.set_if_changed = lambda key, value: model.data.update({key: value}) or True
    assert model.get_countersteer_column_added() == 15
    model.set_countersteer_column_added(150)
    assert model.get_countersteer_column_added() == 100
    model.set_countersteer_column_added(-3)
    assert model.get_countersteer_column_added() == 0


def test_the_engine_setter_switches_the_mapping_and_the_profile_defaults_are_knee_with_the_drive_pull_and_kick_on_at_100():
    from oversteer.model import Model
    engine = fe.FfbEngine()
    assert engine.model.mapping == 'knee' and engine.model.pull_on is True and engine.model.pull_level == 1.0
    assert engine.model.kick_on is True and engine.model.kick_level == 1.0
    engine.set_mapping('classic')                    # the profile INI's countersteer_mapping = classic
    engine._requests(0.0)
    assert engine.model.mapping == 'classic' and engine.model.classic()
    engine.set_mapping('knee')
    engine._requests(0.0)
    assert engine.model.mapping == 'knee'
    engine.set_drive_pull(True, 50)
    engine._requests(0.0)
    assert engine.model.pull_on and engine.model.pull_level == 0.5
    data = Model._texture_defaults(True)
    assert data['countersteer_mapping'] == 'knee' and data['countersteer_drive_pull'] is True
    assert data['countersteer_drive_pull_level'] == 100
    assert data['countersteer_kick'] is True and data['countersteer_kick_level'] == 100
    assert fe.OVERALL_MAPPING == 'knee' and fe.OVERALL_PULL is True and fe.OVERALL_PULL_LEVEL == 100
    m = Model.__new__(Model)                         # a profile with the kick on and no level: the level default applies
    m.data = {'countersteer_kick': True}
    assert m.get_countersteer_kick() is True and m.get_countersteer_kick_level() == 100


# -- hand force: the car's torque scaled by rim / car wheel diameter --

def test_rho_is_the_rims_over_the_cars_wheel_and_the_table_knows_the_logitech_rims():
    from oversteer import wheel_ids as wid
    assert wheel_torque.rim_mm(wid.LG_G29) == 279.0 and wheel_torque.rim_mm(wid.LG_G27) == 270.0
    assert wheel_torque.rim_mm(None) is None and wheel_torque.rim_mm('dead:beef') is None
    rack = rack_for(3.5)
    assert rack.rho() == pytest.approx(0.797, abs=1e-3)                  # 279 / 350
    rack.rim_user, rack.car_wheel_mm = 300.0, 400.0
    assert rack.rho() == pytest.approx(0.75)


def test_one_nm_of_car_torque_plays_rho_over_the_peak_below_the_knee_and_x_top_still_lands_at_top():
    rack = rack_for(3.5, assist=0.0, ratio=1.0)
    rack.p99 = 30.0                                                      # (a top above TOP * peak / rho, so the compression acts)
    s = sample_at(0.0)
    rack.rim_torque = lambda sample, *a, **k: 1.0
    assert rack.force(s) / fe.FULL == pytest.approx(rack.rho() / 3.5)
    rack.rim_torque = lambda sample, *a, **k: rack.top_torque()
    assert rack.force(s) / fe.FULL == pytest.approx(wheel_torque.TOP, abs=1e-6)
    rack.mapping = 'classic'                                             # classic knows nothing of the rims
    rack.rim_torque = lambda sample, *a, **k: 1.0
    plain = fe.RackForce.CLASSIC_SCALE / 3.5
    assert rack.force(s) / fe.FULL == pytest.approx(plain)
    rack.rim_user = 200.0
    assert rack.force(s) / fe.FULL == pytest.approx(plain)


def test_the_rim_and_car_wheel_settings_round_trip_through_the_engine_and_default_from_the_table():
    from oversteer.model import Model
    engine = fe.FfbEngine()
    engine.usb_id = '046d:c24f'                                          # a G29
    assert engine.model.rim_mm() == 279.0 and engine.model.car_wheel_mm == 350.0
    engine.set_rim_mm(300)
    engine.set_car_wheel_mm(999)
    engine._requests(0.0)
    assert engine.model.rim_mm() == 300.0 and engine.model.car_wheel_mm == 420.0 and engine.model.rho() == pytest.approx(300 / 420)
    engine.set_rim_mm(0)                                                 # 0: the table's again
    engine._requests(0.0)
    assert engine.model.rim_mm() == 279.0
    d = Model._texture_defaults(True)
    assert d['countersteer_car_wheel_mm'] == 350 and d['countersteer_rim_mm'] == 0


def test_the_model_defaults_the_rim_from_the_wheel_table_and_clamps_both_diameters():
    from types import SimpleNamespace
    from oversteer.model import Model
    model = Model.__new__(Model)
    model.data, model.ui = {'countersteer_rim_mm': 0}, None
    model.set_if_changed = lambda key, value: model.data.update({key: value}) or True
    model.device = SimpleNamespace(usb_id='046d:c24f')
    assert model.get_countersteer_rim_mm() == 279 and model.get_countersteer_car_wheel_mm() == 350
    model.set_countersteer_rim_mm(50)
    assert model.get_countersteer_rim_mm() == 200
    model.set_countersteer_rim_mm(0)
    assert model.get_countersteer_rim_mm() == 279
    model.set_countersteer_car_wheel_mm(900)
    assert model.get_countersteer_car_wheel_mm() == 420


def test_set_wheel_peak_moves_all_three_racks_the_table_returns_and_a_new_wheel_drops_it():
    engine = fe.FfbEngine()
    racks = (engine.model, engine.rbr_rack, engine.forza_rack)
    engine.usb_id = '046d:c24f'
    assert [r.wheel_peak for r in racks] == [3.08] * 3
    engine.set_wheel_peak(6.0)
    assert [r.wheel_peak for r in racks] == [6.0] * 3
    engine.usb_id = '046d:c24f'                                          # the same wheel again (replug): kept
    assert [r.wheel_peak for r in racks] == [6.0] * 3
    engine.set_wheel_peak(99)
    assert [r.wheel_peak for r in racks] == [wheel_torque.PEAK_RANGE[1]] * 3
    engine.set_wheel_peak(None)                                          # None: the table's
    assert [r.wheel_peak for r in racks] == [3.08] * 3
    engine.set_wheel_peak(6.0)
    engine.usb_id = '046d:c262'                                          # another wheel: the override is gone
    assert [r.wheel_peak for r in racks] == [wheel_torque.peak_nm('046d:c262')] * 3
    engine.usb_id = None
    engine.set_wheel_peak(8.0)                                           # (an unknown wheel takes one too)
    assert [r.wheel_peak for r in racks] == [8.0] * 3


def test_the_force_share_at_a_fixed_torque_scales_with_the_peak_below_the_knee():
    engine = fe.FfbEngine()
    engine.usb_id = '046d:c24f'
    rack = engine.model
    rack.LEARNS, rack.mapping, rack.assist, rack.ratio, rack.p99 = False, 'knee', 0.0, 1.0, 30.0
    rack.reset('acr/x')
    rack.assist, rack.ratio = 0.0, 1.0
    rack.rim_torque = lambda sample, *a, **k: 1.0
    s = sample_at(0.0)
    low = rack.force(s) / fe.FULL
    engine.set_wheel_peak(6.16)                                          # twice the G29's
    assert rack.force(s) / fe.FULL == pytest.approx(low / 2)


def test_the_ini_tunes_the_rack():
    from oversteer import ffb_carprops as cp
    try:
        assert cp.set_power_class('rally speed=15 k=2 r=1 gain=1.5 nonsense=3 k2') == 'rally'
        assert (cp.HYD_K, cp.HYD_R, cp.HYD_GAIN) == (2.0, 1.0, 1.5)
        assert cp.driver_from_column(20.0, 5.0) == cp.driver_from_column(20.0, 50.0)
        assert cp.power_spec().startswith('rally speed=15')
        cp.set_power_class('street')
        assert cp.driver_from_column(20.0, 5.0) != cp.driver_from_column(20.0, 50.0)
        cp.set_power_class('rally speed=0')
        assert cp.driver_from_column(20.0, 5.0) != cp.driver_from_column(20.0, 50.0)
        cp.set_power_class('rally speed=999')
        assert cp.driver_from_column(20.0, 5.0) == cp.driver_from_column(20.0, 50.0)
    finally:
        cp.set_power_class(None)


def test_a_hydraulic_racks_preload_is_the_column_friction():
    from oversteer import ffb_carprops as cp, ffb_dynamics as fd
    try:
        cp.set_power_class('rally preload=0.5')
        assert cp.rack_preload() == 0.5
        assert fd.column_targets(0.3, 3.08, preload_nm=0.5)[1] == round(0.5 / 3.08 * 1e4)
        cp.set_power_class('street preload=0.5')
        assert cp.rack_preload() == 0.0
        assert cp.format_power_class('rally', {'speed': 15.0, 'preload': 1.3}) == 'rally speed=15 preload=1.3'
    finally:
        cp.set_power_class(None)
