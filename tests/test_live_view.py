"""The live view's data shaping (telemetry_view.LiveTrack and the live_* strings) and its cairo drawing
(telemetry_plot.live_*), without a display: the GTK Live view's pure parts, on real LiveBuffer bodies."""
import cairo

from oversteer import telemetry_plot as plot
from oversteer import telemetry_view as view
from oversteer.live_buffer import LiveBuffer
from tests.test_live_buffer import drive, reference, row
from tests.test_plot import colour, pixel


def buffer_with_run(speed=19.0, until=1100.0):
    buffer = LiveBuffer(clock=lambda: 0.0)
    buffer.start_run(1, 1000.0, 'acr', 'acr:test', 'Test', 2000.0)
    buffer.offer_reference(1, reference(), {'key': 'acr:test', 'name': 'Test', 'length': 2000.0}, 7)
    end = drive(buffer, 1, speed, until)
    return buffer, end


def test_the_track_holds_30_s_and_ignores_rows_it_has():
    buffer, end = buffer_with_run(until=1500.0)
    track = view.LiveTrack()
    track.ingest(buffer.read(0, now=end))
    assert track.n == 1 and track.since == buffer.seq
    n, last = len(track.rows), track.last_t
    assert 290 <= n <= 301 and last - track.rows[0]['t'] <= view.LIVE_KEEP + 0.01
    track.resync()                                         # back on the tab: the whole buffer again, nothing twice
    track.ingest(buffer.read(track.since, now=end))
    assert track.since == buffer.seq
    track.since = 0
    track.ingest(buffer.read(0, now=end))
    assert len(track.rows) == n and track.last_t == last
    # g, not m/s2
    assert abs(track.rows[-1]['along'] - 0.1 / view.GRAVITY) < 1e-3


def test_rows_missed_while_away_leave_a_gap_and_a_new_run_starts_clean():
    buffer, end = buffer_with_run(until=500.0)
    track = view.LiveTrack()
    track.ingest(buffer.read(0, now=end))
    before = track.last_t
    drive(buffer, 1, 19.0, 1500.0, start=end + 0.1, t0=before + 0.1)       # 30 s and more nobody read
    track.ingest(buffer.read(track.since, now=end + 60))
    times = [r['t'] for r in track.rows]
    assert times == sorted(set(times)) and times[-1] > before + 30
    assert any(b - a > 0.5 for a, b in zip(times, times[1:])) or times[0] > before        # a hole, or nothing older
    buffer.finish(1, end + 70, 80.0)
    buffer.start_run(2, end + 80, 'acr', 'acr:test')
    buffer.push(2, end + 80, row(0.0, 0.0))
    track.ingest(buffer.read(track.since, now=end + 80))
    assert track.n == 2 and len(track.rows) == 1 and track.tones == {}
    body = buffer.read(9999, now=end + 80)                  # Oversteer restarted under the page
    track.ingest(body)
    assert body['reset'] and len(track.rows) == 1


def test_the_phase_the_delta_and_the_ribbon_follow_the_run():
    buffer, end = buffer_with_run(until=750.0)
    track = view.LiveTrack()
    body = buffer.read(0, now=end)
    track.ingest(body)
    assert view.live_phase(body, track) == 'live'
    bounds = [(0.0, 500.0), (500.0, 1000.0), (1000.0, 2000.0)]
    shown = view.live_delta_view(body, bounds)
    assert shown['text'].startswith('+1.') and shown['pb'] == '1:40.0' and shown['split'].startswith('2 +')
    assert shown['finish'] != '–' and not shown.get('dim')
    assert view.live_cursor(body, bounds) == 1
    assert view.live_cursor(body, [(0.0, 100.0), (100.0, 200.0)]) is None                  # past them: none lit
    tones, current = view.live_ribbon(body, track, bounds)
    assert current == 1 and tones[0] in ('behind-lose', 'behind-gain') and tones[1:] == ['none', 'none']
    # by distance, not list position: a row without the launch section leaves the cursor where the car is
    assert view.live_cursor(body, [(500.0, 1000.0), (1000.0, 2000.0)]) == 0
    # no reference: a note, never a delta
    buffer2 = LiveBuffer(clock=lambda: 0.0)
    buffer2.start_run(1, 0.0)
    buffer2.offer_reference(1, None, None, 1)
    drive(buffer2, 1, 20.0, 100.0, start=0.0)
    none = buffer2.read(0, now=1.0)
    assert view.live_delta_view(none, None)['note'].startswith('No PB yet') and 'text' not in view.live_delta_view(none)
    # pending: nothing to say yet
    buffer3 = LiveBuffer(clock=lambda: 0.0)
    buffer3.start_run(1, 0.0)
    buffer3.push(1, 0.0, row(0.0, 0.0))
    assert view.live_delta_view(buffer3.read(0, now=0.0)) is None
    # stale dims; finished is the card until put away
    assert view.live_delta_view(buffer.read(0, now=end + 10), bounds)['dim']
    buffer.finish(1, end, 61.0)
    done = buffer.read(0, now=end)
    assert view.live_phase(done, track) == 'finished'
    card = view.live_done_view(done, {'sob': 98.0, 'tones': ['gold', 'gold', 'none']}, '3 calls')
    assert card['time'] == '1:01.0' and card['delta'].startswith('−') and card['stage'] == 'Test'
    assert card['line'] == 'PB 1:40.0 · SoB 1:38.0 · 2 gold splits · 3 calls in the debrief'
    track.dismissed = 1
    assert view.live_phase(done, track) == 'put-away'


def test_signed2_never_says_plus_or_minus_zero():
    assert view.signed2(0.004) == '±0.00' and view.signed2(-0.004) == '±0.00'
    assert view.signed2(1.03) == '+1.03' and view.signed2(-0.5) == '−0.50' and view.signed2(None) == '–'


def rows(n=120, **fill):
    out = []
    for i in range(n):
        r = {'t': i / 10.0, 'thr': 1.0, 'brk': 0.0, 'clu': None, 'hb': None, 'steer': 0.5, 'alat': 0.5, 'along': -0.5}
        r.update(fill)
        out.append(r)
    return out


def test_the_delta_block_colours_and_bar():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 90)
    plot.live_delta(cairo.Context(surface), 400, 90, {'text': '+1.00', 'delta': 1.0, 'pb': '4:03.2', 'split': '2 +0.05', 'finish': '4:04.2'})
    assert pixel(surface, 300, 33)[0] > 200 and pixel(surface, 250, 33)[0] < 100      # the bar fills right of its centre, in red
    left = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 90)
    plot.live_delta(cairo.Context(left), 400, 90, {'text': '−1.00', 'delta': -1.0, 'pb': '4:03.2', 'split': '–', 'finish': '4:02.2'})
    assert pixel(left, 250, 33)[1] > 150 and pixel(left, 300, 33)[1] < 100                  # and left of it in green
    plot.live_delta(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 50)), 100, 50, None)
    plot.live_delta(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 70)), 300, 70, {'note': 'No PB yet', 'dim': True})


def test_the_rolling_strips_draw_the_last_12_s_and_break_at_a_gap():
    w, h = 300, int(plot.live_rolling_height())
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    plot.live_rolling(cairo.Context(surface), w, h, rows(), 11.9)
    y = 10 + 16 + 1                                          # the top of the throttle strip: full throttle runs along it
    assert pixel(surface, 150, y)[1] > 150 and pixel(surface, 150, y)[0] < 100
    gap = [r for r in rows() if not 5.0 < r['t'] < 8.0]
    cut = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    plot.live_rolling(cairo.Context(cut), w, h, gap, 11.9)
    assert pixel(cut, 150, y) == colour(plot.PANEL) or pixel(cut, 150, y)[1] < 100       # nothing drawn across the gap
    assert plot.live_rolling_height(True) > plot.live_rolling_height()
    plot.live_rolling(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, w, 300)), w, 300, rows(hb=0.5, clu=0.5), 11.9)
    plot.live_rolling(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)), w, h, [], -1.0)


def test_gg_and_the_stage_map():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 210)
    plot.live_gg(cairo.Context(surface), 200, 210, rows(), 0.7)
    # the car as a dot: a_lat 0.5 g to the left of the centre, braking 0.5 g toward the bottom
    sc = (min(200, 210 - 16) / 2.0 - 6) / 1.4
    assert pixel(surface, int(100 - 0.5 * sc), int(105 + 8 + 0.5 * sc)) == colour(plot.TEXT)
    plot.live_gg(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 210)), 200, 210, [])
    # with positions: the way driven; without: a straight bar by distance with ticks
    path = [(i * 3.0, 40.0 * (i % 7)) for i in range(60)]
    mapped = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 210)
    plot.live_map(cairo.Context(mapped), 200, 210, path, 100.0, 2000.0, (), '0.1 km')
    assert any(pixel(mapped, x, y) == colour(plot.TEXT) for x in range(10, 190) for y in range(30, 200))      # the dot
    bar = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 100)
    plot.live_map(cairo.Context(bar), 200, 100, [], 1000.0, 2000.0, (500.0, 1000.0, 1500.0), '1.0 / 2.0 km')
    y = 28 + (100 - 28 - 10) // 2
    assert pixel(bar, 100, y + 5) == colour(plot.TEXT) or pixel(bar, 100, y) == colour(plot.TEXT)                  # the car half way
    assert pixel(bar, 50, y) == colour(plot.DIM)                                                                  # the driven part
    plot.live_map(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 100)), 200, 100, [], None, None)


def test_the_ribbon_lights_the_current_split():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 10)
    plot.ribbon(cairo.Context(surface), 300, 10, ['ahead-gain', 'none', 'none'], current=1)
    assert pixel(surface, 150, 5) == colour(plot.TEXT) and pixel(surface, 30, 5) == colour(plot.TONES['ahead-gain'])


def test_the_live_reference_is_named_by_what_it_is():
    """'vs PB' only over the PB: over any other run the delta block and the done card name its number."""
    assert view.ref_name({'pb': True, 'n': 3}) == 'PB' and view.ref_name({'time': 100.0}) == 'PB'      # the old shape
    assert view.ref_name({'pb': False, 'n': 4}) == 'Run 4' and view.ref_name({'pb': False, 'n': None}) == 'best run'
    buffer, end = buffer_with_run(until=750.0)
    assert view.live_delta_view(buffer.read(0, now=end), [(0.0, 500.0), (500.0, 2000.0)])['ref_name'] == 'PB'


def test_the_ribbons_first_sector_matches_the_reference_whose_line_is_a_few_metres_on():
    """The sectors' lines are placed from where the reference began: S1 starts 2 m on in the reference, at 0 in the row."""
    import types
    track = types.SimpleNamespace(tones={0: 'behind-lose', 1: 'ahead-gain'})
    body = {'state': 'live', 'distance': 1500.0,
            'ref': {'splits': [{'name': 'S1', 'd0': 2.06, 'd1': 1022.7}, {'name': 'S2', 'd0': 1022.7, 'd1': 3642.9}]}}
    tones, current = view.live_ribbon(body, track, [(0.0, 1020.0), (1020.0, 3640.0), (3640.0, 5330.0)])
    assert tones == ['behind-lose', 'ahead-gain', 'none'] and current == 1
