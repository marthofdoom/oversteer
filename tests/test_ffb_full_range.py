"""Full range (oversteer/ffb_engine.py): while Countersteer's force fully plays the driver's ext_force_cap is 100 % and
its master gain at least 100 %, restored before the glide-out and on every way out. On the fake driver directory."""
import logging
import os

import pytest

from oversteer import ffb_engine as fe, ffb_process as fp
from tests.test_ffb_engine import FakeDriver, HZ, acr, engine_on, forces, game_raw, levels, read, run, upload, \
    _clean_log  # noqa: F401

G75 = 49151                       # 75 % of 65535


def full_wheel(tmp_path, gain=G75, cap=80):
    engine, path = engine_on(tmp_path)
    return engine, path


def setup(tmp_path, gain=G75, cap=80, name='wheel'):
    from tests.test_ffb_engine import wheel
    path = wheel(tmp_path, name)
    if gain is not None:
        open(path + '/gain', 'w').write('%d\n' % gain)
    if cap is None:
        os.remove(path + '/ext_force_cap')
    else:
        open(path + '/ext_force_cap', 'w').write('%d\n' % cap)
    engine = fe.FfbEngine(driver=FakeDriver)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    return engine, path


def play(tmp_path, **kw):
    engine, path = setup(tmp_path, **kw)
    t = run(engine, 0.0, fe.GLIDE_IN + 5.5)
    assert engine.status.active and engine._w == 1.0
    return engine, path, t


def test_raised_only_once_our_force_fully_plays(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    engine, path = setup(tmp_path)
    t, raised_at, seen = 0.0, None, []
    while t < 12.0:
        t = run(engine, t, 0.05)
        cap, gain = read(path, 'ext_force_cap'), read(path, 'gain')
        if (cap, gain) == (100, 65535) and raised_at is None:
            raised_at = (t, engine._w, engine._phase)
        seen.append((engine._w, engine._phase, cap, gain))
    assert raised_at is not None and raised_at[1] >= 1.0 and raised_at[2] == 'on'
    assert all((c, g) == (80, G75) for w, ph, c, g in seen if w < 1.0)
    assert any('full range: cap 80 -> 100, gain 75 -> 100, restored at hand-back' in r.message for r in caplog.records)


def test_what_is_delivered_does_not_step_across_the_switch(tmp_path):
    engine, path = setup(tmp_path)
    t, last, worst, jumps = 0.0, None, None, []
    steps = int((fe.GLIDE_IN + 6.5) * HZ)
    for _ in range(steps):
        s = acr(t)
        upload(game_raw(s))
        engine.offer(t, s)
        engine.step(t)
        gain = read(path, 'gain') / 65535.0
        cap = read(path, 'ext_force_cap') / 100.0 * fe.FULL
        value = forces()[-1] if forces() else 0
        delivered = max(-cap, min(cap, value * gain))
        if last is not None and engine._phase == 'on':
            jumps.append((gain, abs(delivered - last)))
        last = delivered
        t += 1.0 / HZ
    at = next(i for i, (g, _j) in enumerate(jumps) if g > 0.99)          # the step where the gain was raised
    assert at > 0 and jumps[at - 1][0] < 0.76
    neighbours = [j for _g, j in jumps[max(at - 30, 0):at + 30]]
    assert jumps[at][1] < 1.5 * sorted(neighbours)[-2] + 1             # the signal's own slope only, no step
    assert read(path, 'gain') == 65535 and engine._comp == pytest.approx(1.0)


def test_restored_on_a_handback_before_the_glide_out(tmp_path):
    engine, path, t = play(tmp_path)
    assert (read(path, 'ext_force_cap'), read(path, 'gain')) == (100, 65535)
    began = None
    while t < 40.0 and len(levels()) < 2:
        t = run(engine, t, 0.01, hz=100.0)
        if engine._handback and began is None:
            began = t
            assert engine._w > 0.9                          # the glide-out has not got anywhere yet
            assert len(levels()) == 1
    assert (read(path, 'ext_force_cap'), read(path, 'gain')) == (80, G75)
    assert engine._range is None


def test_restored_on_stop_and_a_device_change(tmp_path):
    engine, path, t = play(tmp_path)
    other_path = setup(tmp_path, name='other')[1]
    engine.set_device(other_path)
    run(engine, t, 0.5)
    assert (read(path, 'ext_force_cap'), read(path, 'gain')) == (80, G75)
    engine2, path2, t2 = play(tmp_path, name='w2')
    assert read(path2, 'gain') == 65535
    engine2.stop()
    assert (read(path2, 'ext_force_cap'), read(path2, 'gain')) == (80, G75)


def test_an_exclusive_owner_taking_the_wheel_gets_it_restored_first(tmp_path):
    import threading
    from oversteer import wheel_owner
    engine, path, t = play(tmp_path)
    got = []

    def take():
        got.append(wheel_owner.acquire(path, 'wheel-test', exclusive=True))
        got.append((read(path, 'ext_force_cap'), read(path, 'gain')))      # what calibration would read as saved
    thread = threading.Thread(target=take)
    thread.start()
    for _ in range(100):
        engine.step(t)
        t += 0.005
        if not thread.is_alive():
            break
        thread.join(0.005)
    thread.join(2.0)
    wheel_owner.release(path, 'wheel-test')
    assert got == [True, (80, G75)]


def test_a_gain_above_100_percent_is_left_and_still_divided_out(tmp_path):
    engine, path, t = play(tmp_path, gain=80000)
    assert read(path, 'gain') == 80000 and read(path, 'ext_force_cap') == 100
    assert engine._comp == pytest.approx(80000 / 65535.0)
    engine.stop()
    assert (read(path, 'ext_force_cap'), read(path, 'gain')) == (80, 80000)


def test_a_users_gain_change_is_the_new_original(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    engine, path, t = play(tmp_path)
    open(path + '/gain', 'w').write('%d\n' % 32768)         # the GUI's apply: 50 %
    t = run(engine, t, 1.0)
    assert read(path, 'gain') == 65535
    t = run(engine, t, 1.0)
    assert sum('was set to 50 %' in r.message for r in caplog.records) == 1       # not on every poll
    engine.stop()
    assert read(path, 'gain') == 32768 and read(path, 'ext_force_cap') == 80


def test_a_killed_child_is_restored_by_the_gui(tmp_path):
    engine, path = setup(tmp_path)
    reports = []
    engine.range_hook = lambda wheel, state: reports.append((wheel, None if state is None else state.wire()))
    run(engine, 0.0, fe.GLIDE_IN + 5.5)
    assert reports[-1] == (path, (80, 100, G75, 65535))
    assert read(path, 'gain') == 65535
    fp._restore_wheel(path, reports[-1][1])                  # the child is gone: the GUI's path
    assert (read(path, 'ext_force_cap'), read(path, 'gain')) == (80, G75)
    # a value the user changed meanwhile is left
    open(path + '/gain', 'w').write('40000\n')
    fp._restore_wheel(path, reports[-1][1])
    assert read(path, 'gain') == 40000


def test_unreadable_attributes_do_not_break_play(tmp_path):
    engine, path, t = play(tmp_path, gain=None, cap=None)
    assert engine.status.active and engine._range is None
    engine.stop()
    engine, path, t = play(tmp_path, gain=None, name='capdown')
    assert read(path, 'ext_force_cap') == 100 and not os.path.exists(path + '/gain')
    engine.stop()
    assert read(path, 'ext_force_cap') == 80
