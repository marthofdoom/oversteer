"""One way to write an absolute time (oversteer/timefmt.py), and no absolute time in the coach's words as plain seconds."""
import re

from oversteer import timefmt
from tests.test_coach_places import Stage, KMH, corners_with, reference_corners


def test_clock_is_m_ss_s_with_hours_and_rounding():
    c = timefmt.clock
    assert c(None) == '–' and c(28.7) == '28.7' and c(0.04) == '0.0' and c(59.94) == '59.9'
    assert c(59.96) == '1:00.0' and c(60.0) == '1:00.0' and c(188.7) == '3:08.7' and c(85.7) == '1:25.7'
    assert c(161.95) == '2:42.0' and c(3599.96) == '1:00:00.0' and c(3600.0) == '1:00:00.0' and c(3723.4) == '1:02:03.4'
    assert timefmt.span(28.7) == '28.7 s' and timefmt.span(197.7) == '3:17.7' and timefmt.span(59.96) == '1:00.0'


def test_no_absolute_time_in_the_tips_is_plain_seconds_from_a_minute_up(tmp_path):
    h = Stage(tmp_path / 't.db')
    h.drive(reference_corners(), result_time=200.0)
    h.drive(corners_with(second={'brake_d': 75.0, 'min_speed': 10.0 - 7 * KMH, 'exit_speed': 20.0 - 5 * KMH,
                                 'loss': (0.4, 0.2)}), result_time=203.0)
    h.drive(corners_with(second={'brake_d': 30.0, 'min_speed': 10.0, 'exit_speed': 20.0 + 5 * KMH, 'loss': (-0.5, -1.4)}),
            result_time=197.0)
    texts = [t.text + ' ' + ' '.join(t.evidence or []) for t in h.tips(show_all=True)]
    assert texts and any('3:20.0' in t or '3:17.0' in t for t in texts)
    for text in texts:
        assert not re.search(r'(?<![\d.:])(?:[6-9]\d|\d{3,})\.\d s\b', text), text
