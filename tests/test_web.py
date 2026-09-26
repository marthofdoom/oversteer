"""The read-only web page: endpoints, limits, headers and the Host check
(docs/telemetry-coaching.md, sections 11 and 13)."""
import http.client
import json
import socket
import time

import pytest

from oversteer import telemetry_web
from oversteer.telemetry_web import TelemetryWeb, allowed_host
from tests.test_coach import History, error


@pytest.fixture
def served(tmp_path):
    h = History(tmp_path / 'telemetry.db')
    h.store.save_model('rally', 'eawrc/17', 'eawrc', 'Test car', {'key': 'eawrc/17', 'ratios': {}})
    session = None
    for _ in range(3):
        session = h.session([error(2, 450.0), {'name': 'limiter.per_km', 'value': 0.2, 'count': 10}])
    other = h.store.car_id('circuit', 'eawrc/17', 'eawrc')
    h.store.begin()
    other_session = h.store.start_session('circuit', other, 'eawrc', 1.8e9)
    h.store.commit()
    web = TelemetryWeb(port=0, bind='local', reader_path=str(tmp_path / 'telemetry.db'), profile=lambda: 'rally',
                       live=lambda: {'gear': 3, 'rpm': 6500.0, 'speed': 30.0, 'shift_rpm': float('nan')},
                       status=lambda: {'udp_port': 5310, 'receiving': True, 'game': 'eawrc', 'session': 7},
                       version='0.14')
    assert web.start()
    yield web, h, session, other, other_session
    web.stop()


def request(web, path, method='GET', host=None):
    conn = http.client.HTTPConnection('127.0.0.1', web.port, timeout=5)
    headers = {'Host': host} if host is not None else {}
    conn.request(method, path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def get_json(web, path):
    response, body = request(web, path)
    assert response.status == 200, (path, response.status, body)
    assert response.getheader('Content-Type') == 'application/json; charset=utf-8'
    return json.loads(body)


def test_every_endpoint(served):
    web, h, session, other, _ = served
    status = get_json(web, '/api/v1/status')
    assert status == {'version': '0.14', 'profile': 'rally', 'web_port': web.port, 'udp_port': 5310,
                      'receiving': True, 'game': 'eawrc', 'session': 7, 'stage': None}
    assert '127.0.0.1' not in json.dumps(status)                 # never the telemetry source's address
    assert get_json(web, '/api/v1/live') == {'gear': 3, 'rpm': 6500.0, 'speed': 30.0, 'shift_rpm': None}
    [car] = get_json(web, '/api/v1/cars')
    assert car['id'] == h.car and car['name'] == 'Test car'
    detail = get_json(web, '/api/v1/cars/{}'.format(h.car))
    assert detail['key'] == 'eawrc/17' and detail['gears'] == [] and detail['tuning'] == []
    sessions = get_json(web, '/api/v1/cars/{}/sessions?limit=2'.format(h.car))
    assert len(sessions) == 2 and sessions[0]['id'] == session and len(sessions[0]['runs']) == 1
    one = get_json(web, '/api/v1/sessions/{}'.format(session))
    assert one['runs'][0]['metrics'][0]['name'] == 'shift.error' and one['runs'][0]['laps'] == []
    tips = get_json(web, '/api/v1/coach?car={}'.format(h.car))['tips']
    assert tips and tips[0]['id'].startswith('shift.late:2')
    # The page reads: nothing is marked as seen
    assert h.store.coach_state('rally', h.car) == {}


def test_another_profile_is_not_served(served):
    web, h, session, other, other_session = served
    assert request(web, '/api/v1/cars/{}'.format(other))[0].status == 404
    assert request(web, '/api/v1/sessions/{}'.format(other_session))[0].status == 404
    assert request(web, '/api/v1/coach?car={}'.format(other))[0].status == 404
    assert request(web, '/api/v1/nothing')[0].status == 404


def test_the_page_and_its_headers(served):
    web = served[0]
    response, body = request(web, '/')
    assert response.status == 200 and b'Oversteer telemetry' in body
    csp = response.getheader('Content-Security-Policy')
    assert "default-src 'self'" in csp and "script-src 'sha256-" in csp and "style-src 'sha256-" in csp
    assert 'unsafe-inline' not in csp
    # The keep-awake video is made in the page: blob: media, nothing fetched
    assert "media-src 'self' blob:" in csp
    page = body.decode('utf-8')
    assert "navigator.wakeLock.request(\"screen\")" in page and 'visibilitychange' in page
    assert 'captureStream' in page and 'MediaRecorder' in page
    for external in ('src="http', "src='http", 'href="http', '@import', 'url('):
        assert external not in page
    for name, value in (('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                        ('Cache-Control', 'no-store')):
        assert response.getheader(name) == value
    assert response.getheader('Access-Control-Allow-Origin') is None and response.getheader('Set-Cookie') is None
    response, body = request(web, '/', method='HEAD')
    assert response.status == 200 and body == b''


def test_only_reading(served):
    web = served[0]
    for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
        response, _ = request(web, '/api/v1/status', method=method)
        assert response.status == 405 and response.getheader('Allow') == 'GET, HEAD'


def test_the_host_check(served):
    web = served[0]
    assert request(web, '/api/v1/status', host='evil.example:5301')[0].status == 421
    assert request(web, '/api/v1/status', host='[fe80::1]:{}'.format(web.port))[0].status == 200
    assert request(web, '/api/v1/status', host='192.168.1.20:5301')[0].status == 200
    assert request(web, '/api/v1/status', host='localhost:5301')[0].status == 200
    names = {'rig', 'rig.local', 'localhost'}
    assert allowed_host('rig.local:5301', names) and allowed_host('RIG', names)
    assert not allowed_host('rig.attacker.net', names) and not allowed_host('[not-an-address]', names)
    # tailscale serve: this machine's name in its tailnet, for real HTTPS
    assert allowed_host('rig.example-tailnet.ts.net', names)
    assert not allowed_host('other.example-tailnet.ts.net', names) and not allowed_host('rig.ts.net.evil.com', names)
    assert allowed_host('RIG.example-tailnet.ts.net.:443', names)
    assert not allowed_host('rig.ts.net', names) and not allowed_host('rig.a.b.ts.net', names)
    assert not allowed_host('localhost.example-tailnet.ts.net', names)


def test_a_ninth_request_waits_its_turn(served):
    """Eight connections that never finish their request hold every slot:
    the ninth is told to come back."""
    web = served[0]
    held = []
    for _ in range(telemetry_web.MAX_REQUESTS):
        s = socket.create_connection(('127.0.0.1', web.port))
        s.sendall(b'GET /api/v1/status HTTP/1.1\r\n')            # never finished
        held.append(s)
    time.sleep(0.2)
    try:
        assert request(web, '/api/v1/status')[0].status == 503
    finally:
        for s in held:
            s.close()
    for _ in range(50):
        time.sleep(0.05)
        if request(web, '/api/v1/status')[0].status == 200:
            break
    else:
        pytest.fail('the slots were not given back')


def test_a_taken_port(tmp_path):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        s.listen()
        web = TelemetryWeb(port=s.getsockname()[1], bind='local')
        assert not web.start() and web.error
        web.stop()


def test_without_a_database(tmp_path):
    web = TelemetryWeb(port=0, bind='local', reader_path=str(tmp_path / 'none.db'))
    assert web.start()
    try:
        assert get_json(web, '/api/v1/cars') == []
        assert get_json(web, '/api/v1/live') is None
        assert request(web, '/api/v1/cars/1')[0].status == 404
    finally:
        web.stop()


def test_bound_urls(monkeypatch):
    monkeypatch.setattr(telemetry_web, 'local_addresses', lambda: ['192.168.1.20', '100.64.0.3'])
    assert telemetry_web.urls('lan', 5301) == ['http://192.168.1.20:5301/', 'http://100.64.0.3:5301/']
    assert telemetry_web.urls('local', 5301) == ['http://localhost:5301/']


def test_the_stage_being_driven(served):
    web, h, session, _, _ = served
    h.store.begin()
    h.store.upsert_stage('eawrc:4:12', 'eawrc', 9800.0, 'Col de Turini', 'Monte-Carlo')
    h.store.commit()
    live = {'gear': 3, 'rpm': 6500.0, 'stage': 'eawrc:4:12'}
    web.live = lambda: live
    assert get_json(web, '/api/v1/status')['stage'] == {'key': 'eawrc:4:12', 'name': 'Col de Turini',
                                                         'location': 'Monte-Carlo', 'length': 9800.0}
    live = {'gear': 3, 'rpm': 6500.0, 'stage': None, 'track': 'Monza'}
    assert get_json(web, '/api/v1/status')['stage']['name'] == 'Monza'
    live = None
    assert get_json(web, '/api/v1/status')['stage'] is None
    # The recent sessions carry the stage's name
    sessions = get_json(web, '/api/v1/cars/{}/sessions?limit=1'.format(h.car))
    assert sessions[0]['stage'] == 'eawrc:4:12' and sessions[0]['stage_name'] == 'Col de Turini'


def test_the_stage_of_the_run_going_on(tmp_path):
    """The run's matched stage wins over the game's key."""
    from types import SimpleNamespace
    h = History(tmp_path / 't.db')
    session = h.session([error(2, 450.0)], stage='dirt:1')
    h.store.begin()
    h.store.upsert_stage('dirt:1', 'dirt', 5000.0, 'Kakaristo')
    h.store.commit()
    [run] = h.store.runs(session)
    learner = SimpleNamespace(runs=SimpleNamespace(run=1, run_rows={1: run['id']}))
    stage = telemetry_web.current_stage(h.store, learner, {'stage': 'other'})
    assert stage['name'] == 'Kakaristo'


def test_live_from_the_listener():
    from oversteer.telemetry import Telemetry, Sample
    telemetry = Telemetry(None, learner=None)
    assert telemetry_web.live_dict(telemetry) is None
    sample = Sample(6000.0, 7500.0, gear=3, speed=25.0, car='eawrc/17', game='eawrc')
    sample.distance, sample.stage_length = 1200.0, 9800.0
    telemetry.live = sample
    live = telemetry_web.live_dict(telemetry)
    assert live['gear'] == 3 and live['distance'] == 1200.0 and live['shift_rpm'] > 0 and live['car'] == 'eawrc/17'
    assert live['stage'] is None and 'track' in live


def test_open_pages_do_not_hold_the_slots(served):
    """Browsers keep connections open between the page's polls: each
    request closes its connection, so eight open pages leave room."""
    web = served[0]
    idle = []
    for _ in range(telemetry_web.MAX_REQUESTS):
        conn = http.client.HTTPConnection('127.0.0.1', web.port, timeout=5)
        conn.request('GET', '/api/v1/status')
        response = conn.getresponse()
        response.read()
        assert response.getheader('Connection') == 'close'
        idle.append(conn)
    time.sleep(0.1)
    try:
        assert request(web, '/api/v1/status')[0].status == 200
    finally:
        for conn in idle:
            conn.close()


def test_public_addresses_are_refused(served):
    web = served[0]
    assert web.verify_request(None, ('127.0.0.1', 1)) and web.verify_request(None, ('192.168.1.20', 1))
    assert web.verify_request(None, ('100.101.102.103', 1))        # a tailnet's shared addresses
    assert not web.verify_request(None, ('8.8.8.8', 1)) and not web.verify_request(None, ('not an address', 1))
