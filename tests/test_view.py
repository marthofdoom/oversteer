"""The Telemetry tab's strings, built without a display
(docs/telemetry-coaching.md, section 12)."""
from pathlib import Path

from oversteer import telemetry_view as view
from oversteer.coach import Tip


def test_context_line():
    session = {'discipline': 'rally-stage', 'discipline_conf': 'game', 'surface': 'unknown', 'surface_conf': None,
               'wet': 'unknown', 'stage': 'eawrc:4:12', 'shifter': 'h-pattern'}
    runs = [{'n': 1, 'distance': 9800.0, 'discipline_evidence': ['EA SPORTS WRC sends telemetry from its stages only.'],
             'surface_evidence': ['Needs the location name.'], 'wet_evidence': None}]
    line, evidence = view.context_line(session, runs, {'surface_prior': 'gravel', 'name': None})
    assert line == 'Rally stage (game)  ·  surface unknown  ·  usually gravel here  ·  eawrc:4:12  ·  H-pattern'
    assert evidence == ['Run 1, 9.8 km: EA SPORTS WRC sends telemetry from its stages only. Needs the location name.']
    assert view.context_line(None, [], None)[0] == 'No session recorded yet.'
    line, _ = view.context_line(dict(session, surface='mixed:tarmac,gravel', surface_conf='low'), [], None)
    assert 'mixed (tarmac, gravel) (low)' in line


def test_coaching_lines():
    tips = [Tip('a', 'focus', 'Change up later.'), Tip('b', 'praise', 'Steadier.'), Tip('c', 'still', 'Still: x.')]
    assert view.coaching_lines(tips) == [('Focus: Change up later.', 'focus'), ('Better: Steadier.', 'praise'),
                                         ('Still: x.', 'still')]
    assert view.coaching_lines([], ['Live advice.']) == [('Live advice.', 'tip')]
    assert view.coaching_lines(tips[2:], ['Live advice.']) == [('Live advice.', 'tip'), ('Still: x.', 'still')]


def test_tuning_lines():
    tune = {'ratios': {1: 360.0, 2: 252.0}, 'change': 'gears:3,4', 'first_seen': 1.7e9, 'ride_height_f':
            'not sent by this game', 'brake_bias': 0.58, 'tyre_radius': None}
    lines = view.tuning_lines(tune, [{'kind': 'setup', 'text': 'Lengthen it.'}])
    assert lines[0].startswith('Gearing (rpm per km/h) 1: 100.0, 2: 70.0  ·  gears 3, 4 changed ')
    assert lines[1] == 'ride height: not sent by this game  ·  brake bias 58 %'
    assert lines[2] == 'Setup: Lengthen it.'
    assert view.tuning_lines(None, [])[0].startswith('No setup recorded yet')


def test_session_lines():
    [line] = view.session_lines([{'started': 1.7e9, 'stage': 'eawrc:4:12', 'discipline': 'rally-stage',
                                  'surface': 'gravel', 'shifts': 30, 'error': -120.0, 'distance': 9800.0,
                                  'limiter_time': 4.9}])
    assert line.endswith('eawrc:4:12  ·  Rally stage gravel  ·  30 changes up, -120 rpm from the best  ·  '
                         '9.8 km, 0.5 s/km on the limiter')


def test_coaching_items():
    tips = [Tip('a', 'focus', 'Change up later.'), Tip('b', 'tip', 'Brake earlier.'), Tip('c', 'still', 'Still: x.')]
    assert view.coaching_items(tips) == [('Focus', 'Change up later.', 'focus'), ('', 'Brake earlier.', 'tip'),
                                         ('', 'Still: x.', 'still')]
    assert view.coaching_items(tips[2:], ['Live advice.'])[0] == ('', 'Live advice.', 'tip')


def test_session_rows():
    [row] = view.session_rows([{'started': 1.7e9, 'stage': 'Col de Turini', 'discipline': 'rally-stage',
                                'surface': 'gravel', 'shifts': 30, 'error': -120.0, 'distance': 9800.0,
                                'limiter_time': 4.9}])
    assert row[1:] == ('Col de Turini  ·  Rally stage gravel',
                       '30 changes up, -120 rpm from the best  ·  9.8 km, 0.5 s/km on the limiter')
    [row] = view.session_rows([{'started': 1.7e9}])
    assert row[1:] == ('', '')


def test_live_status():
    from oversteer.telemetry_formats import Sample
    state, text = view.live_status(None)
    assert state == 'off' and 'Learn from game telemetry' in text
    assert view.live_status(5300) == ('waiting', 'Waiting for telemetry on UDP 5300.')
    state, text = view.live_status(5300, elsewhere=5310)
    assert state == 'waiting' and 'arriving on UDP 5310' in text and 'save the profile' in text
    sample = Sample(6120.0, 7500.0, gear=3, speed=26.7, car='eawrc/17')
    sample.car_name, sample.distance, sample.stage_length = 'Fabia <R5>', 2300.0, 9800.0
    state, text = view.live_status(5300, sample, 6650.0)
    assert state == 'live'
    assert text == ('<b>Fabia &lt;R5&gt;</b>  ·  gear 3  ·  6120 rpm  ·  96 km/h  ·  lights at the learnt 6650 rpm'
                    '  ·  2.3 of 9.8 km')
    sample.gear, sample.distance = -1, None
    assert 'gear R' in view.live_status(5300, sample)[1] and 'km  ·' not in view.live_status(5300, sample)[1]


def test_web_status():
    assert view.web_status(False, None, [], False, 'lan', 5301).startswith('Off.')
    assert 'could not start: in use' in view.web_status(False, 'in use', [], False, 'lan', 5301)
    text = view.web_status(True, None, ['http://192.168.1.20:5301/'], False, 'lan', 5301)
    assert text.startswith('Open http://192.168.1.20:5301/') and 'TCP port 5301' in text and 'plain HTTP' in text
    assert 'firewall' not in view.web_status(True, None, ['http://localhost:5301/'], False, 'local', 5301)


def test_gather_from_the_database(tmp_path):
    from tests.test_coach import History
    h = History(tmp_path / 't.db')
    for _ in range(3):
        h.session([{'name': 'limiter.held', 'value': 2.0, 'count': 10}])
    found = view.gather(h.store, 'rally', 'eawrc/17')
    assert found['car_id'] == h.car and found['tips'][0].id == 'limiter.held'
    assert found['context'][0].startswith('discipline unknown') and len(found['sessions']) == 3
    assert found['tuning'][0].startswith('No setup recorded yet')
    assert found['splits'] is None                      # no run with corners to split
    assert view.gather(h.store, 'rally', 'nothing/1')['car_id'] is None


def test_shift_table():
    gears = [{'gear': 1, 'ratio': 360.0, 'ratio_samples': 80, 'best': 7100.0, 'best_low': 6950.0,
              'best_high': 7200.0, 'average_shift': 6800.0, 'shifts': 12, 'last': False},
             {'gear': 2, 'ratio': 252.0, 'ratio_samples': 60, 'best': None, 'best_low': None, 'best_high': None,
              'average_shift': None, 'shifts': 0, 'last': False},
             {'gear': 3, 'ratio': 190.0, 'ratio_samples': 5, 'best': None, 'best_low': None, 'best_high': None,
              'average_shift': None, 'shifts': 0, 'last': True}]
    snapshot = {'limiter': 7500.0, 'gears': gears, 'power_bands': 30, 'power_source': 'game',
                'methods': {1: {'h-pattern': (6790.0, 10)}}}
    headers, rows = view.shift_table(snapshot)
    assert headers == ('Change', 'Best upshift', 'Known to', 'of limiter', 'You change up', 'H-pattern',
                       'rpm per km/h', 'Samples')
    assert rows[0] == ('1→2', '7100 rpm', '6950–7200', '95 %', '6800 rpm (12)', '6790 rpm (10)', '100.0', '80')
    assert rows[1][1:5] == ('learning…', '', '', '—') and rows[2][:2] == ('3 (top)', 'top gear')
    # No band yet, no method used: the plain figure and no method columns
    gears[0].update(best_low=None, best_high=None)
    headers, rows = view.shift_table(dict(snapshot, methods={}))
    assert 'H-pattern' not in headers and rows[0][1:3] == ('7100 rpm', '')
    assert view.shift_summary(snapshot).startswith('Limiter 7500 rpm  ·  30 rev bands of power known (engine power '
                                                   'from the game)')
    assert view.shift_summary(None).startswith('Nothing learnt yet')
    # The game's own data, for a surface: where each best comes from
    gears[0].update(best=7500.0, source='game', grip_limited=False)
    gears[1].update(best=5200.0, source='game', grip_limited=True, ratio=None)
    shipped = dict(snapshot, methods={}, surface='gravel', power_source='game data')
    headers, rows = view.shift_table(shipped)
    assert headers[1] == 'Best upshift (gravel)'
    assert rows[0][1] == '7500 rpm (game)' and rows[1][1] == '5200 rpm (grip)' and rows[1][-2] == '—'
    gears[1].update(engine_best=7500.0)                     # on target up to the engine's best
    assert view.shift_table(snapshot)[1][1][1] == '5200–7500 rpm (grip)'
    assert view.shift_summary(shipped) == ("Limiter 7500 rpm  ·  best changes up from the game's engine data  ·  "
                                           "checked for grip on gravel")


def test_shift_table_lights_band():
    """A Rally2's best change up is the coach's lights band, not the limiter the engine data ends at."""
    row = {'gear': 1, 'ratio': 360.0, 'ratio_samples': 80, 'best': 7500.0, 'engine_best': 7500.0, 'source': 'game',
           'grip_limited': False, 'best_low': None, 'best_high': None, 'lights_low': 6900.0, 'lights_high': 7100.0,
           'average_shift': None, 'shifts': 0, 'last': False}
    snapshot = {'limiter': 7500.0, 'gears': [row, dict(row, gear=2, engine_best=6600.0, lights_high=6900.0),
                                             dict(row, gear=3, last=True)], 'power_bands': 30,
                'power_source': 'game data', 'methods': {}, 'surface': 'gravel'}
    rows = view.shift_table(snapshot)[1]
    assert rows[0][1] == '6900–7100 rpm (lights)' and rows[0][3] == '92 %'
    assert rows[1][1] == '6900 rpm (lights)  engine 6600'


def test_capture_status():
    text = view.capture_status(False, False, '/data/captures', (0, 0, 0), 1)
    assert text.startswith('Off.') and '/data/captures (no captures yet)' in text
    text = view.capture_status(True, True, '/data/captures', (3, 45e6, 1), 2)
    assert text.startswith('Recording to /data/captures (3 captures, 45 MB of 2 GB, 1 labelled).')
    assert 'Nothing is listening' in view.capture_status(True, False, '/c', (0, 0, 0), 1)
    assert '7 packets were lost' in view.capture_status(True, True, '/c', (0, 0, 0), 1, dropped=7)
    assert view.capture_status(True, True, '/c', (0, 0, 0), 1, error='No space left').startswith(
        'Recording stopped: No space left.')


def test_preferences_round_trip():
    defaults = view.read_preferences({})
    assert defaults == {'telemetry_learn': False, 'telemetry_web_on': False, 'telemetry_web_port': 5301,
                        'telemetry_web_bind': 'lan', 'telemetry_capture_on': False, 'telemetry_capture_cap': 1}
    chosen = dict(defaults, telemetry_learn=True, telemetry_web_on=True, telemetry_web_port=8080,
                  telemetry_web_bind='local', telemetry_capture_on=True, telemetry_capture_cap=5)
    written = view.write_preferences(chosen)
    assert written['telemetry_web'] == '1' and written['telemetry_capture_cap'] == '5'
    assert view.read_preferences(written) == chosen
    # Written by hand or by another version: kept in range, else the default
    odd = view.read_preferences({'telemetry_web_port': '80', 'telemetry_capture_cap': 'lots',
                                 'telemetry_web_bind': 'everywhere', 'telemetry_learn': 'yes'})
    assert odd['telemetry_web_port'] == 1024 and odd['telemetry_capture_cap'] == 1
    assert odd['telemetry_web_bind'] == 'lan' and odd['telemetry_learn'] is False


def test_preferences_through_configparser(tmp_path):
    import configparser
    config = configparser.ConfigParser()
    config['DEFAULT'] = dict(view.write_preferences(dict(view.read_preferences({}), telemetry_web_port=6000)),
                             locale='')
    path = tmp_path / 'config.ini'
    with open(str(path), 'w') as f:
        config.write(f)
    again = configparser.ConfigParser()
    again.read(str(path))
    assert view.read_preferences(again['DEFAULT'])['telemetry_web_port'] == 6000


def test_a_technique_line_is_labelled_neutrally_in_both_windows():
    tips = [Tip('t', 'technique', 'You left-foot brake.')]
    assert view.coaching_items(tips) == [('Technique', 'You left-foot brake.', 'technique')]
    assert view.coaching_lines(tips) == [('Technique: You left-foot brake.', 'technique')]
    root = Path(__file__).resolve().parent.parent
    page = (root / 'data/telemetry/web/index.html').read_text()
    assert 'technique: "Technique"' in page and '.k-technique' in page          # not the raw kind, not the tip's blue
    assert '.telemetry-technique' in (root / 'oversteer/main.css').read_text()
    assert "'technique': 'telemetry-technique'" in (root / 'oversteer/gtk_ui.py').read_text()


def test_the_splits_summary_and_rows():
    assert view.splits_lines(None) == (None, [])
    found = {'name': 'Afon Bidno - Severn', 'best': 197.74, 'possible': 194.51, 'gain': 3.23, 'splits': [
        {'name': 'the left-left at 0.3 km', 'last': 6.76, 'best': 6.76, 'gold': True, 'delta': 0.0, 'finish': False},
        {'name': 'the 1 left at 0.7 km', 'last': 8.38, 'best': 7.94, 'gold': False, 'delta': 0.44, 'finish': False},
        {'name': 'the 2 right at 1.6 km', 'last': None, 'best': 6.36, 'gold': False, 'delta': None, 'finish': False},
        {'name': 'the 6 right at 4.8 km', 'last': 70.0, 'best': 71.0, 'gold': False, 'delta': -1.0, 'finish': True}]}
    summary, rows = view.splits_lines(found)
    assert summary == 'Afon Bidno  ·  Best 3:17.7  ·  SoB 3:14.5 (−3.2)'
    assert rows[0] == ('left-left at 0.3 km', '0:06.8', '0:06.8', 'gold', 'gold')
    assert rows[1][3:] == ('+0.4', 'bad') and rows[2][1:] == ('–', '0:06.4', '–', '')
    assert rows[3][3:] == ('−1.0', 'good') and rows[3][0].endswith('(finish)')


def test_the_splits_row_tones_rows_and_sectors():
    assert view.splits_row(None) is None
    found = {'name': 'Afon Bidno - Severn', 'best': 197.74, 'possible': 194.51, 'gain': 3.23, 'last': 198.8, 'runs': 3, 'splits': [
        {'name': 'the left-left at 0.3 km', 'last': 6.76, 'best': 6.76, 'gold': True, 'delta': 0.0, 'finish': False},
        {'name': 'the 1 left at 0.7 km', 'last': 8.38, 'best': 7.94, 'gold': False, 'delta': 0.44, 'finish': False},
        {'name': 'the 2 right', 'last': 9.9, 'best': 9.0, 'gold': False, 'delta': 0.9, 'finish': False},
        {'name': 'the 5 left', 'last': None, 'best': 3.5, 'gold': False, 'delta': None, 'finish': True}],
        'sectors': [{'name': 'S1', 'last': 75.9, 'best': 75.9, 'gold': True, 'delta': 0.0, 'confidence': 'medium'},
                    {'name': 'S2', 'last': 70.0, 'best': 69.6, 'gold': False, 'delta': 0.46, 'confidence': 'low'}]}
    row = view.splits_row(found)
    assert row['stage'] == 'Afon Bidno' and row['tones'] == ['gold', 'behind', 'behind-lose', 'none']
    assert row['rows'][0][:4] == ('1. left-left at 0.3 km', '6.8', '★ ±0.0', '6.8')
    assert row['rows'][3][1:3] == ('–', '–')                                  # not run
    assert row['rows'][-2][:2] == ('Stage', '3:18.8') and row['rows'][-1][2] == '−3.2'
    # Sectors: n is whatever the game has; low confidence is marked
    assert [s[:2] for s in row['sectors']] == [('S1', '75.9'), ('S2', '≈70.0')] and row['estimated']
    assert [s[3] for s in row['sectors']] == ['gold', 'behind']
    none = view.splits_row(dict(found, sectors=None, last=None))
    assert none['sectors'] == [] and not none['estimated'] and none['delta'] is None
    assert view.splits_row(dict(found, splits=[dict(r, last=r['best'], delta=0.0, gold=True) for r in found['splits']]))['tones'] \
        == ['gold'] * 4


def test_splits_row_takes_a_sector_with_no_times():
    from oversteer import telemetry_view as tv
    found = {'name': 'Afon Bidno - Severn', 'last': 190.0, 'best': 186.1, 'possible': 186.1, 'gain': 0.0, 'runs': 3,
             'splits': [{'name': 'left-left at 0.3 km', 'last': 6.8, 'best': 6.8, 'gold': True, 'delta': 0.0,
                         'finish': False}],
             'sectors': [{'name': 'S3', 'last': None, 'best': None, 'gold': False, 'delta': None,
                          'confidence': 'medium'}]}
    row = tv.splits_row(found)
    assert row is not None and [s for s in row['sectors'] if s[0] == 'S3']


def test_gather_survives_a_failing_splits(monkeypatch):
    from oversteer import coach, telemetry_view as tv
    monkeypatch.setattr(coach, 'splits', lambda *a: 1 / 0)
    assert tv._splits(None, 'p', 1) is None
