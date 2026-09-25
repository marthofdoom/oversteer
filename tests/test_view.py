"""The Telemetry tab's strings, built without a display
(docs/telemetry-coaching.md, section 12)."""
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


def test_web_status():
    assert view.web_status(False, None, [], False, 'lan', 5301).startswith('Off.')
    assert 'could not start: in use' in view.web_status(False, 'in use', [], False, 'lan', 5301)
    text = view.web_status(True, None, ['http://192.168.1.20:5301/'], False, 'lan', 5301)
    assert text.startswith('Open http://192.168.1.20:5301/') and 'TCP port 5301' in text and 'plain HTTP' in text
    assert 'firewall' not in view.web_status(True, None, ['http://localhost:5301/'], False, 'local', 5301)


def test_gather_from_the_database(tmp_path):
    from tests.test_coach import History, error
    h = History(tmp_path / 't.db')
    for _ in range(3):
        h.session([error(2, 450.0)])
    found = view.gather(h.store, 'rally', 'eawrc/17')
    assert found['car_id'] == h.car and found['tips'][0].id.startswith('shift.late:2')
    assert found['context'][0].startswith('discipline unknown') and len(found['sessions']) == 3
    assert found['tuning'][0].startswith('No setup recorded yet')
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
    assert headers == ('Gear', 'rpm per km/h', 'Best upshift', 'of limiter', 'You change up', 'H-pattern', 'Samples')
    assert rows[0] == ('1', '100.0', '7100 rpm (6950–7200)', '95 %', '6800 rpm (12)', '6790 rpm (10)', '80')
    assert rows[1][2:5] == ('learning…', '', '—') and rows[2][2] == 'top gear'
    # No band yet, no method used: the plain figure and no method columns
    gears[0].update(best_low=None, best_high=None)
    headers, rows = view.shift_table(dict(snapshot, methods={}))
    assert 'H-pattern' not in headers and rows[0][2] == '7100 rpm'
    assert view.shift_summary(snapshot).startswith('Limiter 7500 rpm  ·  30 rev bands of power known (engine power '
                                                   'from the game)')
    assert view.shift_summary(None).startswith('Nothing learnt yet')


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
