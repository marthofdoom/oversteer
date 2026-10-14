"""The web page's Controls and FFB tabs: who may change settings (the switch, the pairing token, same origin,
rate limits), what is checked, and that every change is made on the main thread through the real Model, so the
window and the page stay in step (oversteer/web_controls.py, oversteer/telemetry_web.py)."""
import hashlib
import http.client
import json
import threading

import pytest

from oversteer import qr, telemetry_web, web_controls
from oversteer.telemetry_web import Bucket, Lockout, TelemetryWeb
from oversteer.web_controls import Bridge
from tests.fake_wheel import FakeController, FakeDevice, MainLoop

TOKEN = 'pair-me-1234'


class Rig:
    def __init__(self, tmp_path, device=None, enabled=True, timeout=5.0):
        self.main = MainLoop()
        self.profiles = tmp_path / 'profiles'
        self.profiles.mkdir()
        self.controller = FakeController(self.profiles, device, enabled, TOKEN)
        self.bridge = Bridge(self.controller, self.main.schedule, timeout)
        self.bridge.refresh()
        self.web = TelemetryWeb(port=0, bind='local', control=self.bridge)
        assert self.web.start()
        self.device = self.controller.device
        self.model = self.controller.model
        self.ui = self.controller.ui

    def stop(self):
        self.web.stop()
        self.main.stop()

    def save_profile(self, name, **changes):
        """A profile file as the window would have saved it."""
        for key, value in changes.items():
            self.model.data[key] = value
        self.model.save(str(self.profiles / (name + '.ini')))

    def conn(self):
        return http.client.HTTPConnection('127.0.0.1', self.web.port, timeout=5)

    def post(self, body, token=TOKEN, path='/api/v1/controls', headers=None, raw=None, origin=True):
        head = {'Content-Type': 'application/json'}
        if token is not None:
            head[telemetry_web.TOKEN_HEADER] = token
        if origin:
            head['Origin'] = 'http://127.0.0.1:{}'.format(self.web.port)
        head.update(headers or {})
        conn = self.conn()
        conn.request('POST', path, body=raw if raw is not None else json.dumps(body), headers=head)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def get(self, path='/api/v1/controls'):
        conn = self.conn()
        conn.request('GET', path)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data)

    def state(self):
        status, body = self.get()
        assert status == 200
        return body


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.stop()


def set_(rig, **changes):
    return rig.post({'set': changes})


# -- who may change anything --

def test_reading_is_open_and_shows_the_wheel(rig):
    state = rig.state()
    assert state['writable'] is True and state['device']['name'] == 'Fake Logitech G29'
    assert state['settings']['range'] == {'value': 900, 'min': 40, 'max': 900, 'step': 10, 'enabled': True,
                                          'presets': [270, 360, 540, 720, 900]}
    assert state['settings']['ff_gain']['max'] == 150 and state['settings']['sensitivity']['value'] == 50
    assert set(state['pedals']) == {'clutch', 'accelerator', 'brakes'}
    assert state['pedals']['brakes']['response']['presets'][1]['id'] == 'spring_brake'
    assert 'token' not in json.dumps(state) and TOKEN not in json.dumps(state)


def test_a_change_needs_the_token(rig):
    assert rig.post({'set': {'range': 540}}, token=None)[0] == 401
    assert rig.post({'set': {'range': 540}}, token='wrong')[0] == 401
    assert rig.post({'set': {'range': 540}}, token='')[0] == 401
    assert rig.model.get_range() == 900 and not rig.device.log
    status, body = set_(rig, range=540)
    assert status == 200 and body['ok'] and body['state']['settings']['range']['value'] == 540


def test_pairing_checks_the_token_only(rig):
    assert rig.post({}, path='/api/v1/pair')[0] == 200
    assert rig.post({}, token='nope', path='/api/v1/pair')[0] == 401
    assert not rig.device.log


def test_the_setting_off_refuses_everything(tmp_path):
    r = Rig(tmp_path, enabled=False)
    try:
        status, body = set_(r, range=540)
        assert status == 403 and body['code'] == 'off'
        assert r.post({}, path='/api/v1/pair')[0] == 403
        assert r.post({'set': {'range': 540}}, token='wrong')[0] == 403     # not even a hint about the token
        assert r.model.get_range() == 900 and not r.device.log
        assert r.state()['writable'] is False                 # reading still works, and says so
    finally:
        r.stop()


def test_wrong_tokens_lock_the_address_out(rig):
    for _ in range(telemetry_web.MAX_FAILURES):
        assert rig.post({'set': {}}, token='guess')[0] == 401
    status, body = set_(rig, range=540)                       # even the right token waits
    assert status == 429 and body['code'] == 'locked'
    assert not rig.device.log


def test_only_the_pages_own_origin(rig):
    other = rig.post({'set': {'range': 540}}, headers={'Origin': 'http://evil.example'})
    assert other[0] == 403 and other[1]['code'] == 'origin'
    assert rig.post({'set': {'range': 540}}, headers={'Origin': 'null'})[0] == 403
    assert rig.post({'set': {'range': 540}}, headers={'Sec-Fetch-Site': 'cross-site'})[0] == 403
    assert rig.post({'set': {'range': 540}}, headers={'Sec-Fetch-Site': 'same-site'})[0] == 403
    assert not rig.device.log
    assert rig.post({'set': {'range': 540}}, headers={'Sec-Fetch-Site': 'same-origin'})[0] == 200
    assert rig.post({'set': {'range': 450}}, origin=False)[0] == 200        # a tool without a page: token only


def test_json_only_and_small(rig):
    assert rig.post(None, headers={'Content-Type': 'text/plain'}, raw='{"set": {"range": 540}}')[0] == 415
    assert rig.post(None, headers={'Content-Type': 'application/x-www-form-urlencoded'}, raw='range=540')[0] == 415
    assert rig.post(None, raw='not json')[0] == 400
    assert rig.post(None, raw='[1, 2]')[0] == 400
    assert rig.post(None, raw=json.dumps({'set': {'range': 540}, 'pad': 'x' * 5000}))[0] == 413
    assert not rig.device.log


def test_no_side_effects_from_get_or_other_methods(rig):
    for method in ('GET', 'HEAD'):
        conn = rig.conn()
        conn.request(method, '/api/v1/controls?set=range:540', headers={telemetry_web.TOKEN_HEADER: TOKEN})
        assert conn.getresponse().status == 200
    for method in ('PUT', 'DELETE', 'PATCH', 'OPTIONS'):
        conn = rig.conn()
        conn.request(method, '/api/v1/controls', headers={telemetry_web.TOKEN_HEADER: TOKEN})
        response = conn.getresponse()
        assert response.status == 405 and response.getheader('Allow') == 'GET, HEAD, POST'
    assert rig.post({}, path='/api/v1/status')[0] == 404
    assert not rig.device.log and rig.model.get_range() == 900


def test_a_host_that_is_not_this_machine_is_refused(rig):
    conn = rig.conn()
    conn.request('POST', '/api/v1/controls', body='{}', headers={'Host': 'evil.example', 'Content-Type': 'application/json',
                                                               telemetry_web.TOKEN_HEADER: TOKEN})
    assert conn.getresponse().status == 421


def test_the_write_rate_is_limited(rig):
    now = [0.0]
    rig.web.write_limit = Bucket(2, 3, clock=lambda: now[0])
    codes = [set_(rig, sensitivity=40 + i)[0] for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    now[0] += 1.0
    assert set_(rig, sensitivity=60)[0] == 200


def test_bucket_and_lockout_clocks():
    now = [0.0]
    bucket = Bucket(1, 1, clock=lambda: now[0])
    assert bucket.take() and not bucket.take()
    now[0] = 1.0
    assert bucket.take()
    lock = Lockout(limit=2, window=10, clock=lambda: now[0])
    lock.fail('a')
    assert not lock.blocked('a')
    lock.fail('a')
    assert lock.blocked('a') and not lock.blocked('b')
    now[0] = 12.0
    assert not lock.blocked('a')


# -- what is checked --

@pytest.mark.parametrize('changes', [
    {'range': 30}, {'range': 1000}, {'range': 545}, {'range': 540.5}, {'range': '540'}, {'range': True},
    {'sensitivity': 101}, {'sensitivity': -1}, {'ff_gain': 151}, {'autocenter': 101}, {'spring_level': 200},
    {'ffb_enabled': 1}, {'app_gain': 'yes'}, {'combine_pedals': 3}, {'invert_clutch': 'true'},
    {'brakes_response': [3, 85]}, {'brakes_response': [60, 85, 40]}, {'brakes_response': [3, 40, 40]},
    {'brakes_response': [3, 85, 101]}, {'brakes_response': [3.5, 85, 40]}, {'brakes_response': 'x'},
    {'rev_leds_mode': 'sometimes'}, {'nonsense': 1}, {'invert_pedals': 7},
    {'mode': 'ps3'}, {'center_wheel': True}, {'hotkeys': 'x'},
])
def test_values_out_of_range_are_refused(rig, changes):
    status, body = set_(rig, **changes)
    assert status == 400 and 'error' in body
    assert not rig.device.log and rig.model.get_range() == 900


def test_one_bad_value_applies_nothing(rig):
    assert set_(rig, range=540, sensitivity=500)[0] == 400
    assert rig.model.get_range() == 900 and not rig.device.log


def test_the_shape_of_the_request(rig):
    assert rig.post({})[0] == 400
    assert rig.post({'set': {}})[0] == 400
    assert rig.post({'set': {'range': 540}, 'do': 'reset_ffb'})[0] == 400
    assert rig.post({'do': 'format_disk'})[0] == 400
    assert rig.post({'set': {'range': 540}, 'extra': 1})[0] == 400


def test_the_ranges_follow_the_wheel(tmp_path):
    r = Rig(tmp_path, FakeDevice(max_range=540, app_gain=None))
    try:
        state = r.state()['settings']
        assert state['range']['max'] == 540 and state['range']['presets'] == [270, 360, 540]
        assert state['ff_gain']['max'] == 100 and 'app_gain' not in state
        assert set_(r, range=720)[0] == 400 and set_(r, ff_gain=120)[0] == 400
        assert set_(r, range=540, ff_gain=100)[0] == 200
    finally:
        r.stop()


def test_a_driver_without_a_setting_hides_it(tmp_path):
    r = Rig(tmp_path, FakeDevice(spring_level=None, friction_level=None, rumble_level=None, sensitivity=None,
                                 inertia_mode=None, pedal_response=False, rev_leds=False, ffb_leds=None))
    try:
        state = r.state()
        for key in ('spring_level', 'friction_level', 'rumble_level', 'sensitivity', 'inertia_mode', 'rev_leds',
                    'rev_leds_mode', 'rev_leds_shift', 'rev_leds_launch', 'ffb_leds'):
            assert key not in state['settings'], key
        assert 'damper_level' in state['settings'] and 'range' in state['settings']
        assert all('response' not in pedal for pedal in state['pedals'].values())     # an older driver: no curves
        assert state['pedals']['clutch']['invert'] is False
        for changes in ({'spring_level': 10}, {'sensitivity': 10}, {'brakes_response': [3, 85, 40]}, {'rev_leds': True},
                        {'rev_leds_launch': False}, {'inertia_mode': True}):
            assert set_(r, **changes)[0] == 409, changes
        assert not r.device.log
    finally:
        r.stop()


def test_no_wheel_no_changes(tmp_path):
    r = Rig(tmp_path)
    try:
        r.controller.device = None
        r.bridge.refresh()
        state = r.state()
        assert state['device'] is None and state['settings'] == {} and state['writable'] is True
        assert set_(r, range=540)[0] == 409
    finally:
        r.stop()


def test_strengths_are_greyed_while_force_feedback_is_off(rig):
    assert set_(rig, ffb_enabled=False)[0] == 200
    state = rig.state()['settings']
    assert state['ffb_enabled']['value'] is False
    for key in ('ff_gain', 'spring_level', 'damper_level', 'friction_level', 'rumble_level', 'app_gain', 'inertia_mode'):
        assert state[key]['enabled'] is False, key
    assert state['autocenter']['enabled'] is True
    assert set_(rig, spring_level=10)[0] == 409 and set_(rig, ff_gain=50)[0] == 409
    assert set_(rig, autocenter=20)[0] == 200
    assert set_(rig, ffb_enabled=True)[0] == 200 and set_(rig, spring_level=10)[0] == 200


def test_pedal_responses_are_ignored_while_combined(rig):
    assert set_(rig, combine_pedals=1)[0] == 200
    assert rig.state()['pedals']['brakes']['response']['enabled'] is False
    assert set_(rig, brakes_response=[3, 85, 40])[0] == 409
    assert set_(rig, combine_pedals=0)[0] == 200
    assert rig.state()['pedals']['brakes']['response']['enabled'] is True
    assert set_(rig, brakes_response=[3, 85, 40])[0] == 200


# -- changes are made on the main thread, as the window makes them --

def test_writes_run_on_the_main_thread(rig):
    seen = []
    original = rig.bridge._apply

    def spy(payload):
        seen.append(threading.get_ident())
        return original(payload)
    rig.bridge._apply = spy
    status, _ = set_(rig, range=540, sensitivity=70, ff_gain=120, spring_level=40, autocenter=10, inertia_mode=True,
                     brakes_response=[3, 85, 40], invert_brakes=True)
    assert status == 200
    assert seen == [rig.main.ident] and rig.main.ident != threading.get_ident()
    writes = {entry[0]: entry for entry in rig.device.log}
    assert {'range', 'sensitivity', 'ff_gain', 'spring_level', 'autocenter', 'inertia_mode', 'pedal_response',
            'invert_pedals'} <= set(writes)
    assert all(entry[2] == rig.main.ident for entry in rig.device.log)       # not one write from a web thread
    assert writes['range'][1] == 540 and writes['ff_gain'][1] == 120
    assert writes['pedal_response'][1] == (4, 3, 85, 40) and writes['invert_pedals'][1] == 4


def test_reading_never_touches_the_device(rig):
    rig.device.touched.clear()
    for _ in range(3):
        rig.state()
    assert rig.device.touched == [] and rig.device.log == []


def test_a_window_that_does_not_answer_times_out(tmp_path):
    r = Rig(tmp_path, timeout=0.3)
    try:
        r.bridge.schedule = lambda fn: None                  # the main loop is stuck
        status, body = set_(r, range=540)
        assert status == 504 and 'did not answer' in body['error']
        assert r.model.get_range() == 900 and not r.device.log
    finally:
        r.stop()


def test_a_timed_out_change_does_not_run_late():
    jobs = []
    with pytest.raises(TimeoutError):
        web_controls.call_on_main(lambda: jobs.append('ran'), jobs.append, 0.05)
    jobs.pop()()                                             # the main loop gets round to it afterwards
    assert jobs == []


def test_an_error_on_the_main_thread_comes_back(tmp_path):
    r = Rig(tmp_path)
    try:
        def refuse(value, *a):
            raise PermissionError(13, 'Permission denied')
        r.device.set_range = refuse
        status, body = set_(r, range=540)
        assert status == 500 and 'refused' in body['error']
        assert r.state()['settings']['range']['value'] == 540          # the model has it, as in the window
    finally:
        r.stop()


# -- window and page stay in step --

def test_a_web_change_shows_in_the_window(rig):
    set_(rig, range=540, sensitivity=70, ff_gain=120, autocenter_persistent=True, combine_pedals=2, invert_clutch=True,
         brakes_response=[3, 85, 40], rumble_level=10)
    calls = {c[0]: c[1:] for c in rig.ui.calls}
    assert calls['set_range'] == (540,) and calls['set_sensitivity'] == (70,) and calls['set_ff_gain'] == (120,)
    assert calls['set_autocenter_persistent'] == (True,) and calls['set_combine_pedals'] == (2,)
    assert calls['set_invert_pedals'] == (1,) and calls['set_pedal_response'] == ('brakes', (3, 85, 40))
    assert calls['set_rumble_level'] == (10,)
    assert rig.model.get_range() == 540 and rig.model.get_combine_pedals() == 2     # the window's model is the one changed


def test_a_window_change_shows_on_the_page(rig):
    rig.main.schedule(lambda: rig.model.set_range(720))              # the range slider in the window
    rig.main.schedule(lambda: rig.model.set_spring_level(55))
    rig.main.schedule(lambda: rig.model.set_ffb_enabled(False))
    rig.main.schedule(lambda: rig.model.set_pedal_response('accelerator', 0, 90, 30))
    rig.main.schedule(lambda: rig.model.set_invert_pedals(2))
    rig.main.schedule(lambda: None)
    done = threading.Event()
    rig.main.schedule(done.set)
    assert done.wait(2)
    state = rig.state()
    assert state['settings']['range']['value'] == 720 and state['settings']['spring_level']['value'] == 55
    assert state['settings']['ffb_enabled']['value'] is False
    assert state['pedals']['accelerator']['response']['value'] == [0, 90, 30]
    assert state['pedals']['accelerator']['invert'] is True and state['pedals']['brakes']['invert'] is False


def test_invert_keeps_the_other_pedals(rig):
    set_(rig, invert_clutch=True, invert_brakes=True)
    assert rig.model.get_invert_pedals() == 5
    set_(rig, invert_clutch=False)
    assert rig.model.get_invert_pedals() == 4
    pedals = rig.state()['pedals']
    assert [pedals[n]['invert'] for n in ('clutch', 'accelerator', 'brakes')] == [False, False, True]


def test_the_meter_and_the_rev_lights_do_not_share_the_leds(rig):
    assert set_(rig, rev_leds=True)[0] == 200
    assert rig.model.get_rev_leds() is True and rig.controller.revs
    assert set_(rig, ffb_leds=True)[0] == 200
    assert rig.model.get_rev_leds() is False and rig.model.get_ffb_leds()
    assert ('set_rev_leds', False, None) in rig.ui.calls               # the window's switch follows


def test_the_rev_light_shift_point(rig):
    settings = rig.state()['settings']
    assert settings['rev_leds_mode']['value'] == 'auto' and settings['rev_leds_shift']['enabled'] is False
    assert set_(rig, rev_leds_shift=90)[0] == 409                      # automatic: no single point
    assert set_(rig, rev_leds_mode='percent')[0] == 200
    settings = rig.state()['settings']
    assert settings['rev_leds_mode']['value'] == 'percent' and settings['rev_leds_shift']['enabled'] is True
    assert (settings['rev_leds_shift']['min'], settings['rev_leds_shift']['max']) == (50, 100)
    assert set_(rig, rev_leds_shift=90)[0] == 200 and rig.model.get_rev_leds_shift() == 90
    assert set_(rig, rev_leds_shift=120)[0] == 400
    assert set_(rig, rev_leds_mode='rpm')[0] == 200
    shift = rig.state()['settings']['rev_leds_shift']
    assert (shift['min'], shift['max'], shift['unit']) == (1000, 25000, 'rpm')
    assert set_(rig, rev_leds_shift=7500, rev_leds_launch=False)[0] == 200
    assert rig.model.get_rev_leds_launch() is False


def test_actions(rig):
    assert rig.post({'do': 'reset_ffb'})[0] == 200
    assert rig.model.get_ff_gain() == 100 and rig.model.get_spring_level() == 30
    assert set_(rig, ffb_enabled=False)[0] == 200
    assert rig.post({'do': 'try_effect', 'arg': 'spring'})[0] == 409     # force feedback is off
    assert rig.post({'do': 'reset_ffb'})[0] == 200 and rig.model.get_ffb_enabled() is True
    assert rig.post({'do': 'try_effect', 'arg': 'spring'})[0] == 200
    assert rig.device.demos == [('spring', rig.main.ident)]
    assert rig.post({'do': 'try_effect', 'arg': 'earthquake'})[0] == 400


def test_profiles(rig):
    rig.save_profile('race', range=540, spring_level=50)
    rig.save_profile('drift', range=270, spring_level=10)
    assert rig.state()['profile'] == {'current': None, 'list': ['drift', 'race'], 'dirty': False}
    assert rig.post({'do': 'load_profile', 'arg': '../../etc/passwd'})[0] == 400
    assert rig.post({'do': 'load_profile', 'arg': 'missing'})[0] == 400
    assert rig.post({'do': 'load_profile', 'arg': 7})[0] == 400
    status, body = rig.post({'do': 'load_profile', 'arg': 'race'})
    assert status == 200 and rig.controller.loaded == ['race']              # through the profile box
    assert ('set_profile', 'race') in rig.ui.calls and rig.model.get_range() == 540
    profile = body['state']['profile']
    assert profile['current'] == 'race' and profile['dirty'] is False and body['state']['settings']['range']['value'] == 540
    assert all(entry[2] == rig.main.ident for entry in rig.device.log)
    # an unsaved change marks the profile, saving it clears the mark
    assert set_(rig, spring_level=70)[0] == 200
    assert rig.state()['profile']['dirty'] is True
    assert rig.post({'do': 'save_profile', 'arg': 'drift'})[0] == 409        # only the profile in use
    assert rig.post({'do': 'save_profile', 'arg': 'race'})[0] == 200
    assert rig.state()['profile']['dirty'] is False
    assert 'spring_level = 70' in (rig.profiles / 'race.ini').read_text()


def test_saving_without_a_profile(rig):
    assert rig.state()['profile']['current'] is None
    assert rig.post({'do': 'save_profile', 'arg': 'race'})[0] == 409
    assert rig.post({'do': 'save_profile'})[0] == 409


def test_the_page_carries_the_tabs_and_no_inline_styles():
    page = telemetry_web.find_page()
    text = open(page, encoding='utf-8').read()
    for tab in ('controls', 'ffb'):
        assert 'data-tab="{}"'.format(tab) in text and 'id="tab-{}"'.format(tab) in text
    import re
    assert not re.search(r'<[a-z][^>]*\sstyle=', text) and not re.search(r'<[a-z][^>]*\son[a-z]+=', text)
    web = TelemetryWeb(port=0, bind='local')
    assert len(re.findall(r"sha256-", web._csp)) >= 5 and "unsafe-inline" not in web._csp


# -- the pairing code --

def test_qr_codes():
    code = qr.encode('http://192.168.1.20:5301/?pair=abcdefghijklmnop')
    assert len(code) == len(code[0]) == 29                         # version 3
    assert all(len(row) == 29 for row in code)
    finder = [row[:7] for row in code[:7]]
    assert finder[0] == [True] * 7 and finder[1] == [True] + [False] * 5 + [True] and finder[3][3] is True
    assert qr.encode('a') != qr.encode('b')
    assert len(qr.encode('x' * 271)) == 57
    with pytest.raises(ValueError):
        qr.encode('x' * 272)
    # The same modules as `qrencode -l L oversteer` makes (for other texts it may pick another of the 8 masks,
    # which reads as well; checked against it mask by mask when written)
    rows = ''.join(''.join('1' if c else '0' for c in row) for row in qr.encode('oversteer'))
    assert hashlib.sha256(rows.encode()).hexdigest() == QR_A


QR_A = '6c3c5b03468dadc8da9fef25d26839cbaf067b1a2821f305f430ded1e5a866e9'
