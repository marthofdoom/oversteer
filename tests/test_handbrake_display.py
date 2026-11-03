from oversteer.handbrake_display import HandbrakeDisplay

ABS_THROTTLE = 6


def test_rest_reads_zero_and_full_pull_reads_one():
    hb = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    assert hb.update(0) == 0.0
    assert hb.update(65535) == 1.0
    assert hb.update(0) == 0.0


def test_rest_above_the_declared_minimum_still_reads_zero():
    # the sensor never reports its declared bottom: rest sits at 12 %
    hb = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    rest = int(0.12 * 65535)
    assert hb.update(rest) == 0.0
    assert hb.update(rest + 20) == 0.0              # noise at rest


def test_travel_short_of_the_declared_top_reads_full():
    hb = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    hb.update(int(0.12 * 65535))
    assert hb.update(int(0.78 * 65535)) == 1.0


def test_a_nudge_does_not_read_as_a_full_pull():
    hb = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    hb.update(0)
    assert hb.update(int(0.1 * 65535)) < 0.2


def test_direction_is_the_axis_as_reported_never_flipped():
    # whichever way the proxy left the axis, a higher reading is a higher bar
    hb = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    hb.update(0)
    hb.update(65535)
    assert hb.update(60000) > hb.update(30000) > hb.update(5000)


def test_event_and_poll_scaling_agree():
    a = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    b = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    values = [0, 100, 20000, 40000, 65000, 30000, 0]
    for v in values:
        assert a.update(v) == b.update(v)


def test_sensor_noise_does_not_make_the_bar_jump():
    hb = HandbrakeDisplay(ABS_THROTTLE, 0, 65535)
    hb.update(0)
    hb.update(65535)
    hb.update(32000)
    shown = hb.shown
    for jitter in (-300, 250, -150, 400, -50):
        assert hb.update(32000 + jitter) == shown


def test_new_axis_after_a_proxy_restart_starts_clean():
    hb = HandbrakeDisplay.for_axis((ABS_THROTTLE, 0, 65535))
    hb.update(int(0.3 * 65535))
    hb.update(int(0.9 * 65535))
    fresh = HandbrakeDisplay.for_axis((ABS_THROTTLE, 0, 4095))
    assert fresh.update(0) == 0.0 and fresh.update(4095) == 1.0
    assert HandbrakeDisplay.for_axis(None) is None


def test_degenerate_range_is_ignored():
    hb = HandbrakeDisplay(ABS_THROTTLE, 10, 10)
    assert hb.update(10) == 0.0
