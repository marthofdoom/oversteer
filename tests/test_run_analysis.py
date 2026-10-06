"""The Run view's data (oversteer/run_analysis.py): a run and its comparison on a distance grid, the coach's
sections with their times and loss, the advice placed on them, and the web endpoints that serve them. Runs are
written as the store holds them (stored corners, a synthetic trace), as tests/test_coach_places.py does."""
import http.client
import json

import pytest

from oversteer import coach, run_analysis as ra
from oversteer.coach import Tip
from oversteer.telemetry_store import TRACE_CHANNELS
from oversteer.telemetry_web import TelemetryWeb
from tests.test_coach_context import C, stage
from tests.test_coach_places import Stage, STAGE_KEY, reference_corners, corners_with

LENGTH = 2100.0


def trace_of(dips=(), speed=25.0, positions=False):
    """A run down the stage at `speed` m/s, slower through each (d, to) of `dips`: the trace, and its time."""
    points = [(0.0, speed)]
    for d, to in dips:
        points += [(d - 100.0, speed), (d, to), (d + 100.0, speed)]
    points.append((LENGTH, speed))
    rows = stage(points)
    if positions:
        rows = [row[:C['x']] + (row[C['distance']] * 0.5, 0.0, row[C['distance']] * 0.1 + 100.0) + row[C['z'] + 1:]
                for row in rows]
    return rows, rows[-1][C['t']] + 0.1


def drive(h, dips=(), positions=False, **kw):
    rows, took = trace_of(dips, positions=positions)
    return h.drive(kw.pop('corners', reference_corners()), trace=rows, result_time=took, course=LENGTH, **kw)


@pytest.fixture
def runs(tmp_path):
    """Three runs of the stage: an even one (the PB), a slow one, and the last, slow through the 600 m corner."""
    h = Stage(tmp_path / 'telemetry.db')
    first = drive(h)
    second = drive(h, dips=[(1200.0, 20.0)], corners=corners_with())
    third = drive(h, dips=[(600.0, 12.0)], corners=corners_with(second={'loss': (0.4, 0.2), 'brake_d': 75.0}))
    return h, first, second, third


def test_both_runs_are_on_one_distance_grid(runs):
    h, first, second, third = runs
    found = ra.analysis(h.store, third, 'pb')
    n = len(found['this']['t'])
    assert found['vs'] == 'pb' and found['ref']['id'] == first and found['step'] == 2.0
    assert all(len(found['this'][name]) == n == len(found['cmp'][name]) for name in found['channels'])
    assert found['this']['t'][0] == 0.0 and found['this']['t'][-1] == pytest.approx(found['time'], abs=0.2)
    assert found['x'] is None and found['z'] is None                         # no position in these runs
    # the delta grows through the slow corner and not before it
    delta = [a - b for a, b in zip(found['this']['t'], found['cmp']['t'])]
    assert abs(delta[200]) < 0.05 and delta[400] > 0.5 and delta[-1] == pytest.approx(found['time'] - found['ref_time'], abs=0.2)
    assert found['this']['speed'][100] == pytest.approx(25.0 * 3.6, abs=0.2) and found['this']['speed'][300] == pytest.approx(12.0 * 3.6, abs=1.0)


def test_the_sections_agree_with_the_splits(runs):
    h, first, second, third = runs
    found = ra.analysis(h.store, third, 'pb')
    splits = coach.splits(h.store, h.profile, h.car)
    assert splits['ref_run'] == first
    by_name = {s['name']: s for s in found['sections']}
    assert splits['splits']
    for split in splits['splits']:
        section = by_name[split['name']]
        assert section['time'] == pytest.approx(split['last'], abs=1e-6)
        assert section['ref_time'] == pytest.approx(split['pb'], abs=1e-6)
        assert (section['d0'], section['d1']) == (split['d0'], split['d1'])
    # the cumulative delta at a split's end is the trace's delta there
    for split in splits['splits']:
        section = by_name[split['name']]
        k = min(len(found['this']['t']) - 1, int(section['d1'] / found['step']))
        at = found['this']['t'][k] - found['cmp']['t'][k]
        assert split['cum'] == pytest.approx(at, abs=0.15)
    # the slow corner is where the time went
    worst = ra.loss_rows(found['sections'])[0]
    assert worst['loss'] > 0.5 and worst['d0'] < 600.0 < worst['d1'] and ra.biggest_loss(found['sections']) == worst['i']
    assert worst['dmin'] is not None and worst['dbrake'] is not None


def test_a_first_run_has_no_comparison_and_its_own_sections(tmp_path):
    h = Stage(tmp_path / 't.db')
    only = drive(h)
    found = ra.analysis(h.store, only, 'pb')
    assert found['ref'] is None and found['cmp'] is None and found['this']['t']
    assert found['sections'] and all(s['loss'] is None for s in found['sections'])
    assert ra.biggest_loss(found['sections']) is None and ra.analysis(h.store, 9999) is None


def test_previous_is_the_run_before_and_any_run_can_be_picked(runs):
    h, first, second, third = runs
    assert ra.analysis(h.store, third, 'prev')['ref']['id'] == second
    assert ra.analysis(h.store, third, str(first))['ref']['id'] == first and ra.analysis(h.store, third, str(first))['vs'] == 'run'
    assert ra.analysis(h.store, third, '99999')['ref'] is None            # not a run of this stage
    head = ra.head(h.store, third)
    assert [c['run'] for c in head['compare']] == [first, second] and head['time'] > head['compare'][0]['time']
    assert head['delta_pb'] == pytest.approx(head['time'] - head['compare'][0]['time'])
    assert [r['id'] for r in ra.recent_runs(h.store, h.car)] == [third, second, first]
    assert [r['pb'] for r in ra.recent_runs(h.store, h.car)] == [False, False, True]


def test_a_partial_run_is_not_the_previous_run(runs):
    """A partial run's time holds the slow-down to the stop control: not a time to compare with."""
    h, first, second, third = runs
    h.store.update_run(second, run_class='partial')
    assert ra.previous_run(h.store, h.store.run(third))['id'] == first


def test_the_position_comes_through_when_the_run_has_one(tmp_path):
    h = Stage(tmp_path / 't.db')
    drive(h, positions=True)
    run = drive(h, positions=True, corners=corners_with())
    found = ra.analysis(h.store, run, 'pb')
    assert found['x'][100] == pytest.approx(100.0, abs=0.5) and found['z'][100] == pytest.approx(-120.0, abs=0.5)    # ACR's plan is (x, -z)


def test_the_result_is_kept_per_run(runs):
    h, first, second, third = runs
    assert ra.analysis(h.store, third, 'pb') is ra.analysis(h.store, third, 'pb')
    assert ra.analysis(h.store, third, 'pb') is not ra.analysis(h.store, third, 'prev')


def test_advice_goes_to_the_section_its_place_falls_in():
    sections = [{'i': 0, 'd0': 0.0, 'd1': 300.0, 'last': False}, {'i': 1, 'd0': 300.0, 'd1': 900.0, 'last': False},
                {'i': 2, 'd0': 900.0, 'd1': 1500.0, 'last': True}]
    one = {'id': 'a', 'kind': 'tip', 'place': {'d': 350.0}}
    several = {'id': 'b', 'kind': 'tip', 'place': {'d': 100.0, 'ds': [100.0, 1000.0]}}
    habit = {'id': 'c', 'kind': 'tip', 'place': None}
    past = {'id': 'd', 'kind': 'praise', 'place': {'d': 1600.0}}
    found = ra.advice_by_section([one, several, habit, past], sections)
    assert found == {1: [one], 0: [several], 2: [several, past]}
    assert ra.advice_by_section([one], []) == {}
    assert ra.advice_line({'kind': 'tip', 'cost': 0.84, 'place': {'d': 510.0}, 'text': 'x'}) == ('+0.8', 'tip', '0.51 km', 'x')
    assert ra.advice_line({'kind': 'praise', 'cost': 0.3, 'text': 'y'}) == ('−0.3', 'praise', '', 'y')


def test_tips_carry_their_place_in_the_coach_json(tmp_path):
    h = Stage(tmp_path / 't.db')
    first = h.drive(reference_corners(), result_time=200.0)
    last = h.drive(corners_with(second={'brake_d': 75.0, 'loss': (0.4, 0.2)}), result_time=203.0)
    [tip] = [t for t in h.tips() if t.id.startswith('corner.section')]
    assert tip.place['run'] == last and tip.place['stage'] == STAGE_KEY and tip.place['d'] == 600.0
    assert tip.place['d0'] < 600.0 < tip.place['d1'] and 'section_index' in tip.place
    as_dict = tip.to_dict()
    assert as_dict['place'] == tip.place and as_dict['cost'] == pytest.approx(0.6) and as_dict['text'] == tip.text
    assert Tip('x', 'tip', 'y').to_dict()['place'] is None
    assert first != last


@pytest.fixture
def web(runs, tmp_path):
    h = runs[0]
    server = TelemetryWeb(port=0, bind='local', reader_path=str(tmp_path / 'telemetry.db'), profile=lambda: h.profile)
    assert server.start()
    yield server
    server.stop()


def fetch(web, path):
    conn = http.client.HTTPConnection('127.0.0.1', web.port, timeout=10)
    conn.request('GET', path)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, json.loads(body)


def test_the_run_endpoints(runs, web):
    h, first, second, third = runs
    status, listed = fetch(web, '/api/v1/runs?car={}'.format(h.car))
    assert status == 200 and [r['id'] for r in listed] == [third, second, first]
    status, head = fetch(web, '/api/v1/runs/{}'.format(third))
    assert status == 200 and head['id'] == third and head['compare'][0]['run'] == first
    status, trace = fetch(web, '/api/v1/runs/{}/trace'.format(third))
    assert status == 200 and trace['ref']['id'] == first and len(trace['this']['t']) > 1000 and trace['x'] is None
    status, prev = fetch(web, '/api/v1/runs/{}/trace?vs=prev&step=10'.format(third))
    assert status == 200 and prev['ref']['id'] == second and prev['step'] == 10.0 and len(prev['this']['t']) < len(trace['this']['t'])
    assert fetch(web, '/api/v1/runs/99999')[0] == 404 and fetch(web, '/api/v1/runs/{}/nothing'.format(third))[0] == 404
    assert fetch(web, '/api/v1/runs?car=99999')[0] == 404


def test_the_splits_carry_the_pb_and_the_bounds(runs):
    h, first, second, third = runs
    splits = coach.splits(h.store, h.profile, h.car)
    for r in splits['splits']:
        assert r['pb'] is not None and r['d0'] < r['d1'] and r['cum'] is not None


def test_a_failing_splits_never_takes_the_tips_down(runs, web, monkeypatch):
    h = runs[0]

    def boom(*args):
        raise ValueError('no')
    monkeypatch.setattr(coach, 'splits', boom)
    status, body = fetch(web, '/api/v1/coach?car={}'.format(h.car))
    assert status == 200 and body['splits'] is None and isinstance(body['tips'], list)


def test_the_page_has_the_run_view_and_stays_inside_its_csp():
    from oversteer.telemetry_web import find_page, _hashes
    text = open(find_page(), encoding='utf-8').read()
    for needle in ('id="tele-run"', 'id="c-strips"', 'id="loss"', '/api/v1/runs/', 'data-cmp="prev"'):
        assert needle in text, needle
    assert len(_hashes(text, 'script')) >= 2 and len(_hashes(text, 'style')) >= 2     # the Run view's own blocks are hashed


def surface_of(draw, w, h):
    import cairo
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surface)
    draw(cr)
    return surface


def pixel_of(surface, x, y):
    data = surface.get_data()
    o = y * surface.get_stride() + x * 4
    return data[o + 2], data[o + 1], data[o]


def hexrgb(colour):
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def test_the_cairo_strips_map_and_gg_draw_the_analysis(runs, tmp_path):
    from oversteer import telemetry_plot as plot
    h, first, second, third = runs
    data = ra.analysis(h.store, third, 'pb')
    height = int(plot.strips_height(data, 1.0))
    assert height == plot.LANE + sum(hh + plot.GAP for _k, _l, hh in plot._strips(data, 1.0))
    seen = {}

    def draw(cr):
        seen['frame'] = plot.strips(cr, 500, height, data, (0.0, data['length']), 600.0)
    surface = surface_of(draw, 500, height)
    gutter, width = seen['frame']
    assert gutter == plot.GUT and width == 500 - plot.GUT - 6
    x = int(gutter + 600.0 / data['length'] * width)
    assert pixel_of(surface, x, height - 3) == hexrgb(plot.TEXT)                  # the cursor runs through every strip
    assert pixel_of(surface, x + 40, 2) != hexrgb(plot.TEXT)
    assert plot.section_at(data, 650.0) == next(s['i'] for s in data['sections'] if s['d0'] <= 650.0 < s['d1'])
    assert plot.section_at(data, 1e9) == data['sections'][-1]['i'] and plot.section_at({'sections': []}, 5) is None
    # the map without a position is a bar of the splits' tones, the slow corner's in red
    assert plot.map_points(data, 300, 100) is None and plot.stage_map_size(data, 400) == 44
    bar = surface_of(lambda cr: plot.stage_map(cr, 300, 44, data, 600.0), 300, 44)
    slow = next(s for s in data['sections'] if s['loss'] and s['loss'] > 0.5)
    mid = int(4 + (slow['d0'] + slow['d1']) / 2 / data['length'] * 292)
    assert pixel_of(bar, mid, 10)[0] > 200 > pixel_of(bar, mid, 10)[1]
    assert plot.loss_tone(0.02) == plot.NEUTRAL and plot.loss_tone(0.3) == plot.SLOWER and plot.loss_tone(-0.3) == plot.FASTER
    surface_of(lambda cr: plot.gg(cr, 200, 200, data), 200, 200)
    # no comparison: no delta strip, nothing breaks
    alone = ra.analysis(h.store, first, 'pb')
    assert alone['cmp'] is None and plot.strips_height(alone) < plot.strips_height(data)
    surface_of(lambda cr: plot.strips(cr, 400, 300, alone, (0.0, alone['length']), 0.0), 400, 300)
    surface_of(lambda cr: plot.stage_map(cr, 300, 44, alone, 0.0), 300, 44)


def test_the_map_with_a_position_gives_the_road(tmp_path):
    from oversteer import telemetry_plot as plot
    h = Stage(tmp_path / 'p.db')
    drive(h, positions=True)
    run = drive(h, positions=True, corners=corners_with())
    data = ra.analysis(h.store, run, 'pb')
    pts = plot.map_points(data, 300, 200)
    assert pts is not None and len(pts) == len(data['x']) and pts[0] != pts[-1]
    assert plot.stage_map_size(data, 400) == 320
    got = {}
    surface_of(lambda cr: got.setdefault('pts', plot.stage_map(cr, 300, 200, data, 500.0)), 300, 200)
    assert got['pts'] is not None
