"""The force in the hand, in newtons (ffb_handforce): the conversion, the cases and the per-drive quantiles."""
import pytest

from oversteer import ffb_handforce as hf


def test_the_newton_conversion_is_torque_over_the_rim_radius():
    assert hf.newtons(1.0, 280.0) == pytest.approx(1.0 / 0.14)
    assert hf.newtons(-2.1, 280.0) == pytest.approx(-15.0)
    assert hf.newtons(7.0, 350.0) == pytest.approx(40.0)


def test_the_cases():
    assert hf.case_of(20.0, 0.5, 0.01) == 'cruise'
    assert hf.case_of(20.0, 6.0, 0.1) == 'cornering'
    assert hf.case_of(20.0, 6.0, -0.5) == 'limit'
    assert hf.case_of(20.0, 2.0, 0.1) is None                      # between cruise and cornering
    assert hf.case_of(20.0, None, 0.1) is None


def test_the_drive_summary_has_p50_and_p90_in_newtons_and_starts_over():
    h = hf.HandForce()
    assert h.summary() == '' and h.take() == ''
    for i in range(1, 101):                                         # a second each: 0.1 .. 10 N played, twice that modelled
        h.add('cornering', i / 10.0 * 0.14, i / 10.0 * 0.28, 280.0, 1.0)
    h.add(None, 5.0, 5.0, 280.0, 1.0)                               # not counted
    assert h.take() == ('hand force p50/p90 N: played cruise none, cornering 5/9, limit none; '
                        'model cruise none, cornering 10/18, limit none')
    assert h.summary() == ''
    h.add('limit', 1.4, 2.8, 280.0, 2.0)
    q = h.q['played', 'limit']
    assert q.value(0.5) == pytest.approx(10.0, rel=0.06) and h.q['model', 'limit'].value(0.9) == pytest.approx(20.0, rel=0.06)
