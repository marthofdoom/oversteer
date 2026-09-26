"""The read-only telemetry web page, for a phone or a laptop on the LAN
(docs/telemetry-coaching.md, section 11).

Off by default. It serves one self-contained page and a small JSON API
over what the telemetry listener hears now and what the telemetry
database holds: the live gear and revs, the learnt shift tables,
sessions with their verdicts and evidence, coaching and tuning advice.
Nothing can be changed through it: GET and HEAD only, no cookies, no
CORS headers (a page from elsewhere cannot read the JSON), a Host check
against DNS rebinding, at most MAX_REQUESTS in flight, one request per
connection (an open page's idle keep-alive connections would otherwise
hold every slot), and clients on this computer's own networks only (a
public address, as on a VPN or public Wi-Fi, is refused). There is no
authentication: anyone on the same network can read it, which the tab
says next to the switch.

Each request opens its own read-only connection to the database and
closes it: a server thread lives for one request only, so a per-thread
connection would leak one per request.
"""

import base64
import hashlib
import http.server
import ipaddress
import json
import logging
import math
import os
import re
import socket
import threading
import urllib.parse

from . import coach, tuning
from .telemetry_store import open_reader

DEFAULT_PORT = 5301
MAX_REQUESTS = 8                 # in flight at once; more get 503
TIMEOUT = 10.0                   # s a connection may take to send its request
BINDS = {'lan': '0.0.0.0', 'local': '127.0.0.1'}
API = '/api/v1/'


def find_page(datadir=None):
    """The page's path: installed with the telemetry data, or in the
    source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if datadir:
        candidates.append(os.path.join(datadir, 'telemetry', 'web', 'index.html'))
    candidates.append(os.path.join(here, '..', 'data', 'telemetry', 'web', 'index.html'))
    for path in candidates:
        if os.path.exists(path):
            return os.path.normpath(path)
    return None


def _hashes(page, tag):
    """CSP sources for the page's inline <script> or <style> blocks."""
    return ["'sha256-{}'".format(base64.b64encode(hashlib.sha256(block.encode('utf-8')).digest()).decode('ascii'))
            for block in re.findall(r'<{0}>(.*?)</{0}>'.format(tag), page, re.S)]


def _clean(value):
    """JSON-safe: NaN and infinities (not JSON) become null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def local_addresses():
    """The machine's IPv4 addresses other than loopback: the local entries
    of /proc/net/fib_trie, or, when that cannot be read, the address a
    UDP socket would send from (connecting one sends no packet)."""
    found = []
    try:
        with open('/proc/net/fib_trie') as f:
            last = None
            for line in f:
                match = re.search(r'\|--\s+(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    last = match.group(1)
                elif '/32 host LOCAL' in line and last and not last.startswith('127.') and last not in found:
                    found.append(last)
    except OSError:
        pass
    if not found:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(('192.0.2.1', 9))                     # TEST-NET-1: never routed anywhere real
                address = s.getsockname()[0]
                if not address.startswith('127.'):
                    found.append(address)
        except OSError:
            pass
    return found


def urls(bind, port):
    """The URLs the page can be opened at, for the tab to list."""
    if BINDS.get(bind, bind) == '127.0.0.1':
        return ['http://localhost:{}/'.format(port)]
    return ['http://{}:{}/'.format(address, port) for address in local_addresses()] or \
        ['http://localhost:{}/'.format(port)]


def allowed_host(host, names):
    """True for a Host header naming this machine by an IP literal (IPv4, or
    IPv6 in brackets), localhost, its hostname or <hostname>.local; a DNS
    name pointing here from elsewhere (DNS rebinding) is refused."""
    if not host:
        return True                          # HTTP/1.0 tools; browsers always send one
    host = host.strip().lower()
    if host.startswith('['):
        literal = host[1:host.find(']')] if ']' in host else ''
        try:
            ipaddress.IPv6Address(literal.split('%')[0])
            return True
        except ValueError:
            return False
    name = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
    try:
        ipaddress.IPv4Address(name)
        return True
    except ValueError:
        pass
    if name.endswith('.ts.net') and name.split('.')[0] in names:
        # This machine's Tailscale name (<host>.<tailnet>.ts.net), as
        # `tailscale serve` sends it: it only resolves inside the tailnet,
        # and gives the page real HTTPS, which the screen wake lock needs
        return True
    return name in names


def live_dict(telemetry):
    """The live strip from the listener, read without a lock (a published
    Sample is never changed), or None when nothing arrives."""
    if telemetry is None:
        return None
    sample = telemetry.live
    if sample is None:
        return None
    limiter = telemetry.reference_max() or sample.max_rpm or None
    target = telemetry.using_learnt or telemetry.shift_rpm or (limiter * telemetry.shift if limiter else None)
    return {'gear': sample.gear, 'rpm': sample.rpm, 'speed': sample.speed, 'shift_rpm': target,
            'learnt': bool(telemetry.using_learnt), 'limiter': limiter, 'game': sample.game, 'car': sample.car,
            'distance': sample.distance if sample.distance is not None else sample.lap_distance,
            'stage_length': sample.stage_length, 'progress': sample.progress, 'throttle': sample.throttle,
            'brake': sample.brake, 'stage': sample.stage, 'track': sample.track}


def current_stage(reader, learner, live):
    """The stage being driven, for the page's live panel: the stage the
    current run was matched to (drive_log), else the game's own stage
    key, named from the stage tables; else the track as the game names
    it. None when nothing says."""
    key = None
    runs = getattr(learner, 'runs', None) if learner is not None else None
    number = getattr(runs, 'run', None)
    if number is not None:
        row = runs.run_rows.get(number)
        session = reader.run_session(row) if row is not None else None
        if session is not None:
            key = next((r['stage'] for r in reader.runs(session) if r['id'] == row), None)
    live = live or {}
    key = key or live.get('stage')
    stage = reader.stage(key) if key else None
    if stage is not None and stage.get('name'):
        return {'key': key, 'name': stage['name'], 'location': stage.get('location'), 'length': stage.get('length')}
    if live.get('track'):
        return {'key': key, 'name': live['track'], 'location': None, 'length': None}
    return None


class TelemetryWeb(http.server.ThreadingHTTPServer):
    """The server. `live()` returns the live strip (or None), `profile()`
    the Oversteer profile whose data is shown, `status()` extra status
    fields (the UDP port, the current car and session); `learner` (a
    ShiftLearner, optional) gives the car being driven its live shift
    table. start() binds and serves from a daemon thread; stop() shuts it
    down."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=DEFAULT_PORT, bind='0.0.0.0', live=None, reader_path=None, profile=None, status=None,
                 learner=None, page=None, version=''):
        self.port = int(port)
        self.bind = BINDS.get(bind, bind)
        self.live = live or (lambda: None)
        self.reader_path = reader_path
        self.profile = profile or (lambda: '_no_profile')
        self.status = status or (lambda: {})
        self.learner = learner
        self.version = version
        self.error = None
        self.remote_seen = False             # a request came from another machine
        self._slots = threading.BoundedSemaphore(MAX_REQUESTS)
        self._thread = None
        self._page = None
        self._csp = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        path = page if page is not None else find_page()
        if path is not None:
            with open(path, encoding='utf-8') as f:
                text = f.read()
            self._page = text.encode('utf-8')
            scripts, styles = _hashes(text, 'script'), _hashes(text, 'style')
            # media-src blob:: the keep-awake video, recorded in the page
            # from a canvas (no file is fetched for it)
            self._csp = ("default-src 'self'; script-src {}; style-src {}; img-src 'self' data:; "
                         "media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'").format(
                             ' '.join(scripts) or "'none'", ' '.join(styles) or "'none'")
        hostname = socket.gethostname().lower()
        self.names = {'localhost', hostname, hostname + '.local', hostname.split('.')[0],
                      hostname.split('.')[0] + '.local'}

    def start(self):
        """Bind and serve; False (with `error` set) when the port is taken
        or cannot be bound."""
        try:
            http.server.ThreadingHTTPServer.__init__(self, (self.bind, self.port), Handler)
        except OSError as e:
            self.error = str(e)
            logging.warning("telemetry web page: can't listen on %s:%d: %s", self.bind, self.port, e)
            return False
        self.port = self.server_address[1]              # port 0 (tests): the one given
        self.error = None
        self._thread = threading.Thread(target=self.serve_forever, name='telemetry-web', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._thread is not None:
            self.shutdown()
            self.server_close()
            self._thread.join(2.0)
            self._thread = None

    def verify_request(self, request, client_address):
        # "Every network" means the networks this computer is on: a
        # laptop on public Wi-Fi, or a VPN handing out public addresses,
        # must not serve the page to whoever is there
        try:
            return not ipaddress.ip_address(client_address[0]).is_global
        except ValueError:
            return False

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\n'
                                b'Connection: close\r\n\r\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    # -- the data --

    def reader(self):
        """A fresh read-only connection, or None without a database."""
        if not self.reader_path or not os.path.exists(self.reader_path):
            return None
        try:
            return open_reader(self.reader_path)
        except Exception as e:
            logging.debug("telemetry web page: can't read the database: %s", e)
            return None

    def api(self, path, query):
        """(status, body) of an API path."""
        profile = self.profile()
        if path == 'status':
            body = {'version': self.version, 'profile': None if profile == '_no_profile' else profile,
                    'web_port': self.port}
            body.update(self.status() or {})
            reader = self.reader()
            if reader is not None:
                try:
                    body['stage'] = current_stage(reader, self.learner, self.live())
                except Exception as e:
                    logging.debug("telemetry web page: no stage: %s", e)
                finally:
                    reader.close()
            return 200, body
        if path == 'live':
            return 200, self.live()
        reader = self.reader()
        if reader is None:
            return (200, []) if path in ('cars',) else (404, {'error': 'no telemetry database yet'})
        try:
            return self._query(reader, profile, path, query)
        finally:
            reader.close()

    def _query(self, reader, profile, path, query):
        parts = path.split('/')
        if parts == ['cars']:
            return 200, reader.car_list(profile)
        if parts[0] == 'cars' and len(parts) >= 2:
            car = self._car(reader, profile, parts[1])
            if car is None:
                return 404, {'error': 'no such car'}
            if len(parts) == 2:
                return 200, self._car_detail(reader, profile, car)
            if parts[2:] == ['sessions']:
                limit = max(1, min(100, _int(query.get('limit'), 20)))
                sessions = reader.sessions(car['id'], limit)
                names = {}
                for session in sessions:
                    session['runs'] = reader.runs(session['id'])
                    key = session['stage'] or next((r['stage'] for r in session['runs'] if r['stage']), None)
                    if key and key not in names:
                        names[key] = (reader.stage(key) or {}).get('name')
                    session['stage_name'] = names.get(key) if key else None
                return 200, sessions
            return 404, {'error': 'not found'}
        if parts[0] == 'sessions' and len(parts) == 2:
            session = reader.session(_int(parts[1], -1))
            if session is None or session['profile'] != profile:
                return 404, {'error': 'no such session'}
            for run in session['runs']:
                run['metrics'] = reader.run_metrics(run['id'])
                run['laps'] = reader.laps(run['id'])
                run['corners'] = reader.corners(run['id'])
            session['shifts'] = len(reader.shifts(session['id']))
            return 200, session
        if parts == ['coach']:
            car = self._car(reader, profile, query.get('car')) if query.get('car') else None
            if query.get('car') and car is None:
                return 404, {'error': 'no such car'}
            # The page only reads: tips are not marked as seen here
            tips = coach.Coach(reader).tips(profile, car['id'] if car else None)
            return 200, {'tips': [t.to_dict() for t in tips]}
        return 404, {'error': 'not found'}

    @staticmethod
    def _car(reader, profile, car_id):
        car = reader.car_by_id(_int(car_id, -1))
        if car is None:
            return None
        cars = {c['id'] for c in reader.car_list(profile)}
        return car if car['id'] in cars else None

    def _car_detail(self, reader, profile, car):
        from .shift_learner import CarModel
        snapshot = None
        learner = self.learner
        if learner is not None and learner.car is not None and learner.car.key == car['key'] \
                and learner.profile == profile:
            snapshot = learner.snapshot()                  # the live one, with its ranges
        if snapshot is None and car['model'].get('key'):
            try:
                model = CarModel.from_dict(dict(car['model'], key=car['key']))
                snapshot = model.snapshot()
            except (ValueError, KeyError, TypeError, AttributeError):
                snapshot = None
        methods = reader.method_shifts(profile, car['key'])
        gears = []
        for row in (snapshot or {}).get('gears', []):
            row = dict(row)
            row['methods'] = {m: {'rpm': v[0], 'count': v[1]} for m, v in methods.get(row['gear'], {}).items()}
            gears.append(row)
        sessions = reader.sessions(car['id'], 1)
        surface = sessions[0]['surface'] if sessions else None
        return {'id': car['id'], 'key': car['key'], 'name': car['name'] or car['key'], 'game': car['game'],
                'limiter': (snapshot or {}).get('limiter'), 'limiter_source': (snapshot or {}).get('limiter_source'),
                'gears': gears, 'tune': tuning.tune_summary(reader, car['id'], car['game']),
                'tuning': [n.to_dict() for n in tuning.advice(reader, profile, car['id'], surface)]}


def _int(text, default):
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'Oversteer'
    sys_version = ''
    timeout = TIMEOUT
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        logging.debug("telemetry web page: " + format, *args)

    def __getattr__(self, name):
        # Every method but GET and HEAD: 405, not the default 501
        if name.startswith('do_'):
            return self._not_allowed
        raise AttributeError(name)

    def _not_allowed(self):
        self._send(405, b'', 'text/plain', extra={'Allow': 'GET, HEAD'})

    def do_HEAD(self):
        self._get(head=True)

    def do_GET(self):
        self._get()

    def _get(self, head=False):
        server = self.server
        if not allowed_host(self.headers.get('Host'), server.names):
            self._send(421, b'Misdirected request\n', 'text/plain', head=head)
            return
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            server.remote_seen = True
        url = urllib.parse.urlsplit(self.path)
        if url.path in ('/', '/index.html'):
            if server._page is None:
                self._send(404, b'The page is not installed.\n', 'text/plain', head=head)
            else:
                self._send(200, server._page, 'text/html; charset=utf-8', head=head)
            return
        if not url.path.startswith(API):
            self._send(404, b'Not found\n', 'text/plain', head=head)
            return
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            status, body = server.api(url.path[len(API):].strip('/'), query)
        except Exception:
            logging.exception("telemetry web page")
            status, body = 500, {'error': 'internal error'}
        data = json.dumps(_clean(body), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self._send(status, data, 'application/json; charset=utf-8', head=head)

    def _send(self, status, data, kind, head=False, extra=None):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', self.server._csp)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        # One request per connection: the slot is held for as long as the
        # connection stays open, and the page polls every second
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()
        if not head and data:
            self.wfile.write(data)
