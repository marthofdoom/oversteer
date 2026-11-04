"""Countersteer FFB in a process of its own: its own interpreter and GIL, so nothing else in Countersteer (the shift
learner's bands, the drive log, the GUI) can hold up the engine's passes. The engine (ffb_engine.FfbEngine) is the
same class tests and the replay scripts use in-process; this module is the boundary.

    GUI process                                          engine process (spawned, 'countersteer-ffb')
    -----------                                          --------------------------------------------
    telemetry listener --offer()--> EngineProcess        _child_main: FfbEngine.loop(_Host.wait), one thread
                         | header + pickled Sample  data ------------------> drained each pass: every arrival
                         | non-blocking, dropped     (SEQPACKET)             counted, only the newest unpickled
                         | when full                                         and played
    gui.py set_*(), usb_id, set_position_source      ctrl ------------------> setters, wheel owners mirrored,
    wheel_owner watcher, inhibit, FFB record, ping   (SEQPACKET)             inhibit, record start/stop; EOF or no
                                                                             ping for HEARTBEAT_TIMEOUT: stop
    status / details / log records / owned, range  <------- up -------------------- after each pass when changed, at
    (supervisor thread: also spawns, restarts)       (SEQPACKET, the child    least every ALIVE s; log records
                                                      sends non-blocking)     forwarded to the app's log

- The child is spawned (a fresh interpreter exec'd, never a fork of the GTK process; it gets the parent's sys.path,
  so the package is found from the source tree and in the Flatpak alike, _Child) by the supervisor thread,
  which lives as long as the app: the child's PR_SET_PDEATHSIG (SIGTERM) fires when that thread or the app goes. EOF
  on the control socket and a heartbeat timeout stop it too. Every way out runs FfbEngine.stop(): the force handed
  back, the dynamics stage zeroed, game_ffb_level 100. The driver's own watchdog remains the last line.
- The GUI's EngineProcess has the engine's interface (the set_* setters, usb_id, status, car_properties, align_note,
  model for the details line, offer, start, stop). It keeps every setting and replays them to a new child. A child
  that dies is logged, shown as stopped (status_text: 'engine-restart' / 'engine-failed') and started again up to
  len(RESTART_DELAYS) times with a growing delay (the count starts again after HEALTHY s of life). If it held the
  wheel, the GUI restores the game's level, scale and the dynamics stage itself, and the cap and gain full range raised.
- wheel_owner: the engine holds the wheel in the child's registry; the GUI mirrors that into its own (an exclusive
  owner's acquire asks the proxy to let go, which asks the child) and mirrors its other owners into the child's
  (wheel_owner.set_remote), so the engine never takes a wheel held in the GUI.
- FFB tuning records (ffb_record): the sidecar is written in the engine process, on the capture's clock
  (CLOCK_MONOTONIC is the machine's). Its records never cross the boundary, none are dropped when the GUI stalls, and
  the engine never waits on the GUI.
- The game's UDP: while Countersteer FFB is on, the engine process receives it first. The GUI's listener
  (telemetry.Telemetry) still chooses and binds the ports (Auto, probing, the legacy port, the user's setting) and
  hands the bound sockets over (SCM_RIGHTS on the control socket: the same sockets, so the ports stay bound and
  nothing binds twice); the engine process reads them, plays the locked source's samples (the listener tells it
  which source is locked and RBR's car) and passes every datagram on over the relay channel with its receive time,
  drop-oldest and counted if the GUI does not read; the listener handles them as if it had read them itself. Handing
  back (the switch off, a restart, the listener's stop): the engine process closes its copies and sends a mark; the
  listener handles everything up to the mark, then reads its sockets again. With the switch off, or without an
  engine process, the listener reads its sockets and offers samples over the data socket as before.

    game --UDP--> [engine process] recv --decode, locked source--> FfbEngine
                        |  every datagram + receive time (relay, SEQPACKET, drop-oldest)
                        v
                  GUI listener: forward targets, capture, learner, LEDs, coach (Telemetry.handle, relayed=True)"""
import atexit
import collections
import ctypes
import faulthandler
import importlib
import locale
import logging
import os
import pickle
import queue
import select
import shutil
import signal
import socket
import struct
import sys
import threading
import time

from . import ffb_engine, ffb_record, wheel_owner

HEADER = struct.Struct('<dqq')           # arrival time (monotonic s), the listener's number, packet_id (NO_ID: none)
NO_ID = -(1 << 63)
PICKLE = pickle.HIGHEST_PROTOCOL
MAX_MESSAGE = 1 << 20
DATA_BUFFER = 1 << 20                    # bytes asked for the sample socket (the kernel caps it at wmem_max)
MAX_DRAIN = 4096                         # samples taken off the socket in one pass at most
HEARTBEAT = 0.25                         # s between the GUI's pings
HEARTBEAT_TIMEOUT = 1.0                  # s without a control message: the GUI is gone or hung, the engine stops
ALIVE = 0.25                             # s: the child sends its status at least this often
HUNG = 3.0                               # s without a message from the child: it is killed and started again
STARTUP_GRACE = 15.0                     # s a new child has to say it is up (its imports, a cold cache)
STOP_WAIT = 1.5                          # s the child has to stop (FfbEngine.stop's 100 ms fade) before it is killed
RESTART_DELAYS = (1.0, 3.0, 10.0)        # s before each restart of a child that died; then it stays off
HEALTHY = 60.0                           # s of life after which a death counts as the first again
CTRL_TIMEOUT = 0.1                       # s a control message may wait for room (a child that hung)
SETTERS = ('set_enabled', 'set_dynamics', 'set_position_source', 'set_wheel_result', 'set_texture', 'set_linearity',
           'set_lightness', 'set_strength', 'set_knee', 'set_rbr_cut', 'set_mapping', 'set_forza_model', 'set_drive_pull', 'set_kick', 'set_column_damping', 'set_rim_mm', 'set_car_wheel_mm', 'set_power_class', 'set_device')
PR_SET_PDEATHSIG = 1
PR_SET_NAME = 15


def _pair():
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)


def _executable():
    """The interpreter the child is spawned with: this one (the Flatpak's /app/bin or /usr/bin python3, or the one
    scripts/run-dev.sh started), else python3 on the PATH."""
    exe = sys.executable
    if exe and os.access(exe, os.X_OK):
        return exe
    return shutil.which('python3') or exe


def _resolve(value):
    """'module:attribute' to the object (the tests' fake driver class), anything else as it is."""
    if isinstance(value, str) and ':' in value:
        module, _, name = value.partition(':')
        obj = importlib.import_module(module)
        for part in name.split('.'):
            obj = getattr(obj, part)
        return obj
    return value


def _position_from_spec(spec):
    """The wheel position reader of ffb_dynamics.position_source, rebuilt in the engine process from the Device's
    fields."""
    if spec is None:
        return None
    from .device import Device
    from .ffb_dynamics import position_source
    return position_source(Device(None, dict(spec)))


RELAY_DATAGRAM = struct.Struct('<dH4sH')   # receive time (monotonic s), port, sender's IPv4 address and port
RELAY_MARK = struct.Struct('<q')
RELAY_PENDING = 8192                     # datagrams the engine process keeps for a GUI that is not reading
RELAY_READ = 512                         # datagrams read off one port in one pass at most


def relay_pack(t, port, addr, data):
    return b'D' + RELAY_DATAGRAM.pack(t, port, socket.inet_aton(addr[0]), addr[1]) + data


def relay_unpack(blob):
    """('datagram', t, port, (ip, port), data) or ('mark', token)."""
    if blob[:1] == b'M':
        return ('mark', RELAY_MARK.unpack_from(blob, 1)[0])
    t, port, ip, sport = RELAY_DATAGRAM.unpack_from(blob, 1)
    return ('datagram', t, port, (socket.inet_ntoa(ip), sport), bytes(blob[1 + RELAY_DATAGRAM.size:]))


# -- the GUI's side --

CHILD_CODE = ("import sys, pickle, base64; sys.path[:] = pickle.loads(base64.b64decode(sys.argv[1])); "
              "from oversteer.ffb_process import _child_entry; _child_entry(sys.argv[2])")


class _Child:
    """The engine process: a fresh interpreter (the spawn model: never a fork of the GTK process), exec'd with this
    process's sys.path so the package is found wherever it was (the source tree, the Flatpak's /app). Not
    multiprocessing.Process: its spawn runs the launcher script again as __mp_main__."""

    def __init__(self, options, data, ctrl, up, relay=None):
        import base64
        import subprocess
        fds = (data.fileno(), ctrl.fileno(), up.fileno()) + ((relay.fileno(),) if relay is not None else ())
        options = dict(options, fds=fds)
        argv = [_executable(), '-c', CHILD_CODE, base64.b64encode(pickle.dumps(list(sys.path))).decode(),
                base64.b64encode(pickle.dumps(options)).decode()]
        self.popen = subprocess.Popen(argv, pass_fds=fds, stdin=subprocess.DEVNULL, close_fds=True)
        self.pid = self.popen.pid
        try:
            self.sentinel = os.pidfd_open(self.pid)
        except (AttributeError, OSError):
            self.sentinel = None                 # (an older kernel: noticed at the next HEARTBEAT)

    def is_alive(self):
        return self.popen.poll() is None

    @property
    def exitcode(self):
        return self.popen.poll()

    def join(self, timeout=None):
        try:
            self.popen.wait(timeout)
        except Exception:
            pass

    def terminate(self):
        if self.is_alive():
            self.popen.terminate()

    def kill(self):
        if self.is_alive():
            self.popen.kill()

    def close(self):
        if self.sentinel is not None:
            os.close(self.sentinel)
            self.sentinel = None


class _ModelView:
    """The few RackForce figures gui.countersteer_details reads, from the child's last details."""

    def __init__(self, details):
        self.ratio = details.get('ratio')
        self.assist = details.get('assist') or 0.0
        self._impedance = details.get('impedance')

    def impedance(self):
        if self._impedance is None:
            raise ValueError('no impedance yet')
        return self._impedance


class _RemoteSampler:
    """ffb_record.sampler's sampler while the engine runs in its own process: start() and stop() ask the child."""

    def __init__(self, host, path, origin, header):
        self.host, self.path, self.origin, self.header = host, path, origin, header
        self.records = 0

    def start(self):
        self.host._record_start(self.path, self.origin, self.header)

    def stop(self, timeout=2.0):
        self.records = self.host._record_stop(self.path, timeout)


class EngineProcess:
    """FfbEngine's interface in the GUI process, the engine itself in a child process (see the module's docstring).
    `engine_kwargs`: FfbEngine's arguments, objects as 'module:attribute' strings (tests); `setup`: a 'module:function'
    the child calls first (tests isolate its files)."""

    def __init__(self, engine_kwargs=None, setup=None, restart_delays=RESTART_DELAYS, healthy=HEALTHY):
        self.engine_kwargs = dict(engine_kwargs or {})
        self.setup = setup
        self.restart_delays = tuple(restart_delays)
        self.healthy = healthy
        self._lock = threading.RLock()           # the control socket and the settings replayed through it
        self._state = {}                         # name -> control message, replayed to every new child
        self._remote = {}                        # wheel -> (owner, exclusive): the GUI's other owners
        self._recording = None                   # (path, origin, header) of the FFB record running
        self._record_done = {}                   # path -> [Event, records]
        self._probe = [threading.Event(), None]
        self._path = None
        self._enabled = False
        self._usb_id = None
        self._seq = 0
        self._data = None                        # the sample socket (GUI end), None while no child runs
        self._ctrl = None
        self._up = None
        self._relay = None                       # the engine process's relay of the game's datagrams (GUI end)
        self._relay_token = 0
        self._old = []                           # sockets of a dead child, closed at the next spawn
        self._proc = None
        self._thread = None
        self._want = threading.Event()           # the child is wanted (set_enabled(True), a sample, an FFB record)
        self._stopping = threading.Event()
        self._status = None                      # the child's last Status
        self._details = {}
        self._down = None                        # None, 'engine-restart' or 'engine-failed'
        self._failed = False
        self._restarts = 0
        self._next_start = 0.0
        self._started_at = None
        self._heard_at = None
        self._ready = False
        self._child_holds = None                 # the wheel the child said it holds (its writes may be live)
        self._held_here = None                   # ... as held in this process's wheel_owner for it
        self._child_range = None                 # (wheel, RangeState.wire()) while the child has the cap/gain raised
        self._available = (None, False)          # (path, DriverReplace.available()) for the status before a child
        self.dropped = 0                         # samples the full socket refused (a child that stalled)
        self.stats = {}                          # the child's latency figures (ALIVE)
        self.priority = None                     # what the child's scheduling became
        self.pid = None

    # -- the listener's side: never blocks --

    def offer(self, now, sample):
        """The listener thread: the sample to the child, or dropped. O(1), never waits: the socket is non-blocking."""
        self._seq += 1
        sock = self._data
        if sock is None:
            if self._path is not None and not self._want.is_set():
                self._want.set()
            return
        packet_id = getattr(sample, 'packet_id', None)
        try:
            sock.send(HEADER.pack(now, self._seq, packet_id if isinstance(packet_id, int) else NO_ID)
                      + pickle.dumps(sample, PICKLE))
        except BlockingIOError:
            self.dropped += 1
        except (OSError, pickle.PicklingError, TypeError, struct.error):
            pass

    # -- the GUI's side --

    def _set(self, key, message):
        with self._lock:
            self._state[key] = message
            self._send(message)

    def _call(self, name, *args):
        self._set(name, ('call', name, args))

    def set_enabled(self, on):
        self._enabled = bool(on)
        self._call('set_enabled', bool(on))
        if on:
            self._want.set()

    def set_dynamics(self, column, align, removal):
        self._call('set_dynamics', column, align, removal)

    def set_position_source(self, source):
        spec = getattr(source, 'spec', None) if source is not None else None
        if source is not None and spec is None:
            logging.debug("Countersteer FFB: this wheel position reader cannot reach the engine process")
        self._set('position', ('position', spec))

    def set_wheel_result(self, result):
        self._call('set_wheel_result', result if isinstance(result, dict) else None)

    def set_texture(self, level, parts):
        self._call('set_texture', level, dict(parts))

    def set_linearity(self, value):
        self._call('set_linearity', value)

    def set_lightness(self, value):
        self._call('set_lightness', value)

    def set_strength(self, value):
        self._call('set_strength', value)

    def set_knee(self, value):
        self._call('set_knee', value)

    def set_rbr_cut(self, value):
        self._call('set_rbr_cut', value)

    def set_mapping(self, value):
        self._call('set_mapping', value)

    def set_forza_model(self, on):
        self._call('set_forza_model', on)

    def set_drive_pull(self, on, level):
        self._call('set_drive_pull', on, level)

    def set_kick(self, on, level):
        self._call('set_kick', on, level)

    def set_column_damping(self, pct):
        self._call('set_column_damping', pct)

    def set_rim_mm(self, value):
        self._call('set_rim_mm', value)

    def set_car_wheel_mm(self, value):
        self._call('set_car_wheel_mm', value)

    def set_power_class(self, name):
        self._call('set_power_class', name)

    def set_device(self, path):
        self._path = path
        self._call('set_device', path)

    @property
    def usb_id(self):
        return self._usb_id

    @usb_id.setter
    def usb_id(self, usb_id):
        self._usb_id = usb_id
        self._set('usb_id', ('usb_id', usb_id))

    @property
    def status(self):
        status = self._status
        if self._down is not None:
            return self._idle_status(self._down)
        return status if status is not None else self._idle_status(None)

    def _idle_status(self, stop):
        path = self._path
        if stop is not None or path is None:
            available = False
        else:
            if self._available[0] != path:
                self._available = (path, ffb_engine.DriverReplace(path).available())
            available = self._available[1]
        return ffb_engine.IDLE_STATUS._replace(enabled=self._enabled, available=available, last_stop=stop,
                                               device=path is not None)

    def car_properties(self):
        return self._details.get('car_properties')

    @property
    def align_note(self):
        return self._details.get('align_note')

    @property
    def model(self):
        details = self._details
        return _ModelView(details) if details.get('ratio') is not None else None

    def reset_figures(self):
        """The child's latency figures (stats) start again (tests, diagnosis)."""
        with self._lock:
            self._send(('reset_figures',))

    def probe(self, timeout=2.0):
        """The child's engine requests as it holds them (tests, diagnosis), None without an answer."""
        event = self._probe[0]
        event.clear()
        with self._lock:
            if not self._send(('probe',)):
                return None
        return self._probe[1] if event.wait(timeout) else None

    # -- the engine process receives the game's UDP first (telemetry.Telemetry's relay) --

    def relay_channel(self):
        """The relay channel to read the game's datagrams from, while the engine process should receive them first:
        Countersteer FFB on and its engine process running; None otherwise (the listener reads its sockets)."""
        if self._enabled and self._ready and self._down is None and not self._stopping.is_set():
            return self._relay
        return None

    def relay_ports(self, socks):
        """The listener's bound sockets ({port: socket}) to the engine process, which receives on them from now on
        (copies of the same sockets: the ports stay bound throughout). False if it could not be sent."""
        ports = list(socks)
        fds = [socks[p].fileno() for p in ports]
        with self._lock:
            sock = self._ctrl
            if sock is None:
                return False
            self._relay_token += 1
            try:
                socket.send_fds(sock, [pickle.dumps(('udp_ports', self._relay_token, ports), PICKLE)], fds)
                return True
            except socket.timeout:
                logging.warning("Countersteer FFB: the engine process takes no requests; stopping it")
                self._kill()
            except OSError:
                pass
            return False

    def relay_release(self):
        """The engine process lets go of its copies of the sockets and marks the end of what it relayed; the mark's
        token, None if it could not be asked."""
        with self._lock:
            self._relay_token += 1
            token = self._relay_token
            return token if self._send(('udp_ports', token, [])) else None

    def relay_lock(self, source):
        """The source the listener locked on ((port, address, game) or None): the only one the engine plays."""
        self._set('udp_lock', ('udp_lock', source))

    def relay_car(self, car):
        """Richard Burns Rally's car as the listener's learner told it: (car, name, epoch, provisional)."""
        self._set('udp_car', ('udp_car', car))

    # -- FFB tuning records --

    def _sampler(self, path, origin, header):
        return _RemoteSampler(self, path, origin, header)

    def _record_start(self, path, origin, header):
        with self._lock:
            self._recording = (path, origin, header)
            self._record_done[path] = [threading.Event(), 0]
            self._send(('record', path, origin, header))
        self._want.set()

    def _record_stop(self, path, timeout):
        with self._lock:
            sent = False
            if self._recording is not None and self._recording[0] == path:
                self._recording = None
                sent = self._send(('record_stop', path))
            done = self._record_done.get(path)
        if done is None:
            return 0
        if sent:
            done[0].wait(timeout)
        self._record_done.pop(path, None)
        return done[1]

    # -- wheel owners --

    def _on_owner(self, wheel, holder):
        """This process's wheel_owner changed: its holders other than the engine to the child."""
        if holder is not None and holder[0] == ffb_engine.OWNER:
            holder = None
        with self._lock:
            if holder is None:
                self._remote.pop(wheel, None)
            else:
                self._remote[wheel] = holder
            self._send(('remote', wheel, holder))

    def _inhibit(self):
        """An exclusive owner wants the wheel (wheel_owner, from its thread): the child lets go, its 'owned' message
        releases it here. Without a child it is released at once."""
        with self._lock:
            sent = self._send(('inhibit',))
        if not sent:
            self._release_here()

    def _release_here(self):
        wheel, self._held_here = self._held_here, None
        if wheel is not None:
            wheel_owner.release(wheel, ffb_engine.OWNER)

    # -- start, stop --

    def start(self):
        if self._thread is not None:
            return
        if sys.getswitchinterval() > ffb_engine.SWITCH_INTERVAL:
            sys.setswitchinterval(ffb_engine.SWITCH_INTERVAL)     # the listener's turns at this process's GIL
        wheel_owner.watch(self._on_owner)
        ffb_record.remote = self._sampler
        self._thread = threading.Thread(target=self._supervise, name='countersteer-ffb-host', daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self, timeout=STOP_WAIT):
        """The child stops (the game's force restored) before this returns; killed if it does not in `timeout`."""
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stopping.set()
        self._want.set()
        with self._lock:
            self._send(('stop',))
        thread.join(timeout + 2.0)
        wheel_owner.unwatch(self._on_owner)
        if ffb_record.remote == self._sampler:
            ffb_record.remote = None

    def _send(self, message):
        """A control message to the child (the lock held); False without a child. A child that leaves no room for
        CTRL_TIMEOUT is hung: it is killed (the supervisor starts another)."""
        sock = self._ctrl
        if sock is None:
            return False
        try:
            sock.send(pickle.dumps(message, PICKLE))
            return True
        except socket.timeout:
            logging.warning("Countersteer FFB: the engine process takes no requests; stopping it")
            self._kill()
        except OSError:
            pass
        return False

    def _kill(self):
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()                      # our own child
            except Exception:
                pass

    # -- the supervisor thread --

    def _supervise(self):
        try:
            while not self._stopping.is_set():
                if self._proc is None:
                    if not self._want.wait(HEARTBEAT) or self._stopping.is_set() or self._failed:
                        continue
                    wait = self._next_start - time.monotonic()
                    if wait > 0:
                        self._stopping.wait(min(wait, HEARTBEAT))
                        continue
                    try:
                        self._spawn()
                    except Exception:
                        logging.exception("Countersteer FFB: starting the engine process")
                        self._died()
                    continue
                self._watch_once()
        except Exception:
            logging.exception("Countersteer FFB: the engine process's supervisor")
        finally:
            self._shutdown()

    def _spawn(self):
        for sock in self._old:
            sock.close()
        self._old = []
        data_rx, data_tx = _pair()
        ctrl_rx, ctrl_tx = _pair()
        up_rx, up_tx = _pair()
        relay_rx, relay_tx = _pair()
        try:
            data_tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, DATA_BUFFER)
        except OSError:
            pass
        options = {
            'parent': os.getpid(),
            'engine': self.engine_kwargs,
            'setup': self.setup,
            'log_level': logging.getLogger().getEffectiveLevel(),
            'stall_dumps': ffb_engine.FfbEngine.STALL_DUMPS,
            'localedir': locale.bindtextdomain('oversteer', None),
        }
        try:
            proc = _Child(options, data_rx, ctrl_rx, up_tx, relay_tx)
        finally:
            for sock in (data_rx, ctrl_rx, up_tx, relay_tx):
                sock.close()
        data_tx.setblocking(False)
        ctrl_tx.settimeout(CTRL_TIMEOUT)
        now = time.monotonic()
        self._proc, self.pid = proc, proc.pid
        self._started_at, self._heard_at, self._ready = now, now, False
        self._up = up_rx
        self._relay = relay_rx
        with self._lock:
            self._ctrl = ctrl_tx
            for message in list(self._state.values()):
                self._send(message)
            for wheel, holder in self._remote.items():
                self._send(('remote', wheel, holder))
            if self._recording is not None:
                self._send(('record',) + self._recording)
        self._data = data_tx
        logging.info("Countersteer FFB: engine process %d started", proc.pid)

    def _watch_once(self):
        proc, up = self._proc, self._up
        watched = [up] if proc.sentinel is None else [up, proc.sentinel]
        try:
            readable = select.select(watched, [], [], HEARTBEAT)[0]
        except (OSError, ValueError):
            readable = []
        now = time.monotonic()
        if up in readable:
            self._read_up()
        if not proc.is_alive():
            self._read_up()                      # its last words (the release, the log lines) first
            self._died()
            return
        with self._lock:
            self._send(('ping',))
        grace = HUNG if self._ready else STARTUP_GRACE
        if now - self._heard_at > grace and not self._stopping.is_set():
            logging.warning("Countersteer FFB: the engine process %d has not answered for %.1f s; stopping it",
                            proc.pid, now - self._heard_at)
            self._kill()

    def _read_up(self):
        up = self._up
        while True:
            try:
                blob = up.recv(MAX_MESSAGE, socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not blob:
                return
            self._heard_at = time.monotonic()
            try:
                self._handle(pickle.loads(blob))
            except Exception:
                logging.exception("Countersteer FFB: a message from the engine process")

    def _handle(self, message):
        kind = message[0]
        if kind == 'status':
            self._status, self._details, self.stats = message[1], message[2], message[3]
            self._down = None
        elif kind == 'log':
            record = logging.makeLogRecord(message[1])
            logger = logging.getLogger(record.name)
            if logger.isEnabledFor(record.levelno):
                logger.handle(record)
        elif kind == 'owned':
            self._owned(message[1], message[2])
        elif kind == 'range':
            self._child_range = None if message[2] is None else (message[1], message[2])
        elif kind == 'record_done':
            done = self._record_done.get(message[1])
            if done is not None:
                done[1] = message[2]
                done[0].set()
        elif kind == 'probe':
            self._probe[1] = message[1]
            self._probe[0].set()
        elif kind == 'ready':
            self._ready = True
            self.priority = message[2]
            logging.info("Countersteer FFB: engine process %d running (%s)", message[1], message[2])

    def _owned(self, wheel, on):
        if on:
            self._child_holds = wheel
            if self._held_here is not None and self._held_here != wheel:
                self._release_here()
            if wheel_owner.acquire(wheel, ffb_engine.OWNER, exclusive=False, on_inhibit=self._inhibit):
                self._held_here = wheel
            else:
                with self._lock:
                    self._send(('inhibit',))     # someone took it here meanwhile: the engine lets go
        else:
            if self._child_holds == wheel:
                self._child_holds = None
            if self._held_here == wheel:
                self._release_here()

    def _died(self):
        proc, self._proc = self._proc, None
        data, self._data = self._data, None      # the listener stops sending; the sockets close at the next spawn
        with self._lock:
            ctrl, self._ctrl = self._ctrl, None
        up, self._up = self._up, None
        relay, self._relay = self._relay, None   # the listener reads it to its end, then its sockets again
        self._old += [s for s in (data, ctrl, up, relay) if s is not None]
        if proc is not None:
            proc.join(0.1)
            proc.close()
        code = None if proc is None else proc.exitcode
        holds, self._child_holds = self._child_holds, None
        raised, self._child_range = self._child_range, None
        if holds is not None or raised is not None:
            _restore_wheel(holds if holds is not None else raised[0], raised[1] if raised is not None else None)
        self._release_here()
        self._status = None
        if self._recording is not None:
            logging.warning("Countersteer FFB: the FFB tuning record's engine channels end here (the engine process "
                            "stopped)")
            self._recording = None
        for done in self._record_done.values():
            done[0].set()
        if self._stopping.is_set():
            return
        now = time.monotonic()
        if self._started_at is not None and now - self._started_at >= self.healthy:
            self._restarts = 0
        if self._restarts >= len(self.restart_delays):
            self._failed = True
            self._down = 'engine-failed'
            logging.error("Countersteer FFB: the engine process ended (exit code %s) and is not started again: it "
                          "stopped %d times; the game keeps its force feedback", code, self._restarts + 1)
            return
        delay = self.restart_delays[self._restarts]
        self._restarts += 1
        self._next_start = now + delay
        self._down = 'engine-restart'
        logging.warning("Countersteer FFB: the engine process ended (exit code %s); starting it again in %.0f s",
                        code, delay)
        self._want.set()

    def _shutdown(self):
        """The supervisor's end (stop(), or the app's exit): the child stops, or is made to."""
        proc = self._proc
        if proc is not None:
            with self._lock:
                self._send(('stop',))
            proc.join(STOP_WAIT)
            if proc.is_alive():
                logging.warning("Countersteer FFB: the engine process did not stop; ending it")
                proc.terminate()
                proc.join(0.5)
            if proc.is_alive():
                proc.kill()
                proc.join(0.5)
            self._read_up()
            self._died()
        for sock in self._old:
            sock.close()
        self._old = []


def _restore_wheel(path, raised=None):
    """What FfbEngine.stop() does, from the GUI after a child that held the wheel died: the game's level and scale
    back, the dynamics stage zeroed (each write on its own; the driver's watchdog would do the first two), and
    the cap and master gain full range raised (`raised`: the originals the child reported, each put back only
    where the driver still shows the child's value)."""
    driver = ffb_engine.DriverReplace(path)
    if raised is not None:
        ffb_engine.apply_restore(driver, raised)
    try:
        if driver.available():
            driver.set_game_level(100)
        if driver.has_const_scale() and (driver.const_scale() or 100) < 100:
            driver.set_const_scale(100)
    except OSError as e:
        logging.warning("Countersteer FFB: restoring the game's force after the engine process: %s", e)
    finally:
        driver.close()
    try:
        from .ffb_dynamics import Dynamics
        dyn = Dynamics(path)
        if dyn.available():
            dyn.off()
    except Exception as e:
        logging.warning("Countersteer FFB: virtual steering dynamics off after the engine process: %s", e)
    logging.info("Countersteer FFB: the game's force is back (the engine process ended)")


# -- the engine process --

class _UpHandler(logging.Handler):
    """The child's log records to the GUI (its log file and console), as plain dicts; dropped if the GUI is not
    reading (the engine never waits)."""

    def __init__(self, host):
        super().__init__()
        self.host = host

    def emit(self, record):
        try:
            d = dict(record.__dict__)
            d['msg'] = record.getMessage()
            d['args'] = None
            if record.exc_info:
                d['exc_text'] = logging.Formatter().formatException(record.exc_info)
            d['exc_info'] = None
            d['stack_info'] = record.stack_info
            d['processName'] = 'countersteer-ffb'
            self.host.send(('log', d))
        except Exception:
            pass


class _Host:
    """The child's side of the sockets, driven from FfbEngine.loop's wait: one thread does the waiting, the
    requests and the passes."""

    def __init__(self, engine, data, ctrl, up, relay=None):
        self.engine = engine
        self.data, self.ctrl, self.up, self.relay = data, ctrl, up, relay
        for sock in (data, ctrl, up, relay):
            if sock is not None:
                sock.setblocking(False)
        self.udp = {}                            # port -> socket: the listener's, while this process receives first
        self.out = collections.deque()           # what waits for room on the relay channel, in order
        self.relayed = self.relay_dropped = 0
        self.lock = None                         # the listener's locked source (port, address, game)
        self.rbr_car = None                      # (car, name, epoch, provisional) as the listener's learner told it
        self.engine_seq = 0                      # the arrivals offered to the engine, counted over both feeds
        self.gui_last = None                     # the listener's number of its last sample offered here
        from .telemetry_formats import decode_sample
        self.decode = decode_sample
        self.sig_r, self.sig_w = os.pipe()
        os.set_blocking(self.sig_r, False)
        os.set_blocking(self.sig_w, False)
        self.stop_reason = None
        self.last_ctrl = time.monotonic()
        self.recorder = None                     # the FfbSampler running (its worker's)
        self.jobs = None                         # its worker's queue
        self.worker = None
        self.sent = None
        self.sent_at = 0.0
        self.seen = 0
        self.lags = collections.deque(maxlen=2000)
        self.passes = collections.deque(maxlen=2000)
        self.lag_max = self.pass_max = self.oversleep_max = 0.0
        self.samples = 0
        self.dropped_up = 0
        self.owned = None

    def send(self, message):
        try:
            self.up.send(pickle.dumps(message, PICKLE))
        except (BlockingIOError, InterruptedError):
            self.dropped_up += 1
        except OSError:
            pass

    def stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
        self.engine._running = False

    def on_signal(self, signum, _frame):
        self.stop('signal %d' % signum)

    def on_owner(self, wheel, holder):
        """The child's wheel_owner: the engine's own holding to the GUI."""
        mine = holder is not None and holder[0] == ffb_engine.OWNER
        if mine:
            self.owned = wheel
            self.send(('owned', wheel, True))
        elif self.owned == wheel:
            self.owned = None
            self.send(('owned', wheel, False))

    def on_range(self, wheel, state):
        """The engine raised (or restored) the driver's cap and gain: the originals to the GUI, for a child that dies."""
        self.send(('range', wheel, None if state is None else state.wire()))

    # FfbEngine.loop's wait

    def wait(self, timeout):
        engine = self.engine
        self._after_pass()
        if engine._wake.is_set():
            timeout = 0.0
        began = time.monotonic()
        self._flush()
        inputs = [self.data, self.ctrl, self.sig_r] + list(self.udp.values())
        outputs = [self.relay] if self.out else []
        watch = engine.raw_wait()
        try:
            if watch is None:
                readable = select.select(inputs, outputs, [], timeout)[0]
            else:
                readable = self._poll(inputs, outputs, watch, timeout)
        except (InterruptedError, ValueError, OSError):
            readable = []
        now = time.monotonic()
        if watch is not None and watch[0] in readable:
            engine.raw_woke(now)
            readable.remove(watch[0])
        if not readable and timeout > 0.0:
            self.oversleep_max = max(self.oversleep_max, now - began - timeout)
        if self.sig_r in readable:
            try:
                os.read(self.sig_r, 512)
            except OSError:
                pass
        self._control(now)
        self._samples()
        if self.udp:
            self._receive([port for port, sock in self.udp.items() if sock in readable])
        elif self.out:
            self._flush()
        if now - self.last_ctrl > HEARTBEAT_TIMEOUT:
            self.stop('no word from the GUI for %.1f s' % (now - self.last_ctrl))

    @staticmethod
    def _poll(inputs, outputs, watch, timeout):
        """select's readable list, by poll() so game_ffb_raw's fd (watch: fd, events: POLLPRI|POLLERR) can be
        waited on too; the fd itself is in the list when it was signalled."""
        poller = select.poll()
        for sock in inputs:
            poller.register(sock, select.POLLIN)
        for sock in outputs:
            poller.register(sock, select.POLLOUT)
        poller.register(watch[0], watch[1])
        fds = {sock.fileno() if hasattr(sock, 'fileno') else sock: sock for sock in inputs}
        readable = []
        for fd, event in poller.poll(timeout * 1000.0):
            if fd == watch[0]:
                readable.append(fd)
            elif fd in fds and event & ~select.POLLOUT:
                readable.append(fds[fd])
        return readable

    def _samples(self):
        engine = self.engine
        blobs = []
        while len(blobs) < MAX_DRAIN:
            try:
                blob = self.data.recv(MAX_MESSAGE)
            except (BlockingIOError, InterruptedError):
                break
            if not blob:
                break                            # the GUI closed it (the control socket's EOF stops us)
            blobs.append(blob)
        if not blobs:
            return
        for blob in blobs[:-1]:
            t, _seq, packet_id = HEADER.unpack_from(blob)
            engine.arrived(t, None if packet_id == NO_ID else packet_id)
        blob = blobs[-1]
        t, seq, _packet_id = HEADER.unpack_from(blob)
        # The listener's numbers go on across children and relay spells: the gap since its last counts its arrivals
        self.engine_seq += len(blobs) if self.gui_last is None else max(seq - self.gui_last, len(blobs))
        self.gui_last = seq
        engine.offer(t, pickle.loads(blob[HEADER.size:]), self.engine_seq)
        self.samples += len(blobs)

    # the game's datagrams, received here first

    def _receive(self, ports):
        """Read the listener's ports that have datagrams: each to the relay (with its receive time), the locked
        source's decoded samples to the engine (only the newest offered, every one counted)."""
        decode_sample = self.decode
        lock, picked = self.lock, []
        for port in ports:
            sock = self.udp.get(port)
            for _ in range(RELAY_READ):
                try:
                    data, addr = sock.recvfrom(4096)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                t = time.monotonic()
                self._queue(relay_pack(t, port, addr, data))
                if lock is None or lock[0] != port or lock[1] != addr[0]:
                    continue
                try:
                    sample = decode_sample(data)
                except Exception:
                    sample = None
                if sample is not None and sample.game == lock[2]:
                    picked.append((t, sample))
        self._flush()
        if not picked:
            return
        engine = self.engine
        for t, sample in picked[:-1]:
            engine.arrived(t, getattr(sample, 'packet_id', None))
        t, sample = picked[-1]
        car = self.rbr_car
        if sample.game == 'rbr' and car is not None and car[0] is not None:
            sample.car, sample.car_name, sample.car_epoch, sample.car_provisional = car
        self.engine_seq += len(picked)
        self.gui_last = None                     # the listener's own feed counts afresh when it comes back
        engine.offer(t, sample, self.engine_seq)
        self.samples += len(picked)

    def _queue(self, blob):
        out = self.out
        if len(out) >= RELAY_PENDING:
            # The oldest datagram goes (never a mark: the listener waits for those)
            for i, item in enumerate(out):
                if item[:1] == b'D':
                    del out[i]
                    self.relay_dropped += 1
                    break
        out.append(blob)

    def _flush(self):
        out, relay = self.out, self.relay
        if relay is None:
            out.clear()
            return
        while out:
            try:
                relay.send(out[0])
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                out.clear()
                return
            out.popleft()
            self.relayed += 1

    def _udp_ports(self, token, ports, fds):
        """The listener's sockets, replacing the ones held; then the mark: everything relayed before it was received
        on the old set."""
        old, self.udp = self.udp, {}
        for sock in old.values():
            sock.close()
        for port, fd in zip(ports, fds):
            sock = socket.socket(fileno=fd)
            sock.setblocking(False)
            self.udp[port] = sock
        for fd in fds[len(ports):]:
            os.close(fd)
        self._queue(b'M' + RELAY_MARK.pack(token))
        self._flush()

    def _control(self, now):
        while True:
            try:
                blob, fds, _flags, _addr = socket.recv_fds(self.ctrl, MAX_MESSAGE, 64)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                blob, fds = b'', []
            if not blob:
                for fd in fds:
                    os.close(fd)
                self.stop('the GUI closed the control channel')
                return
            self.last_ctrl = now
            message = pickle.loads(blob)
            if message[0] == 'udp_ports':
                self._udp_ports(message[1], message[2], fds)
                continue
            for fd in fds:
                os.close(fd)
            self._dispatch(message)

    def _dispatch(self, message):
        engine, kind = self.engine, message[0]
        if kind == 'call':
            if message[1] in SETTERS:
                getattr(engine, message[1])(*message[2])
        elif kind == 'usb_id':
            engine.usb_id = message[1]
        elif kind == 'position':
            try:
                engine.set_position_source(_position_from_spec(message[1]))
            except Exception:
                logging.exception("Countersteer FFB: the wheel's position reader")
                engine.set_position_source(None)
        elif kind == 'remote':
            wheel_owner.set_remote(message[1], message[2])
        elif kind == 'inhibit':
            engine._on_inhibit()
        elif kind == 'udp_lock':
            self.lock = None if message[1] is None else tuple(message[1])
        elif kind == 'udp_car':
            self.rbr_car = message[1]
        elif kind == 'record':
            self._record_start(*message[1:])
        elif kind == 'record_stop':
            self._record_stop(message[1])
        elif kind == 'probe':
            self.send(('probe', self.probe()))
        elif kind == 'reset_figures':
            self.lags.clear()
            self.passes.clear()
            self.lag_max = self.pass_max = self.oversleep_max = 0.0
            self.samples = 0
        elif kind == 'stop':
            self.stop('asked')

    def probe(self):
        e = self.engine
        return {'enabled': e._enabled_req, 'path': e._path_req, 'usb_id': e.usb_id, 'strength': e._strength_req,
                'knee': e._knee_req, 'rbr_cut': e._rbr_cut_req, 'mapping': e._mapping_req, 'forza_model': e._forza_model_req, 'pull': e._pull_req,
                'kick': getattr(e, '_kick_req', None), 'damping': e._damping_req, 'rim': e._rim_req, 'car_wheel': e._car_wheel_req, 'power_class': e._power_class_req, 'lightness': e._lightness_req, 'linearity': e._linearity_req,
                'texture': None if e._texture_req is None else e._texture_req[1:], 'dynamics': e._dyn_req,
                'wheel': e._wheel_req, 'position': e._pos_req is not None and e._pos_req[0] is not None,
                'remote': dict(wheel_owner._remote), 'recording': ffb_record.current is not None,
                'skipped': e.skipped, 'seq': e._seq, 'seen': e._seen}

    def _after_pass(self):
        """After each pass: latency figures, and the status to the GUI when it changed or every ALIVE s."""
        engine = self.engine
        now = time.monotonic()
        took = engine._pass_took
        self.passes.append(took)
        self.pass_max = max(self.pass_max, took)
        if engine._seen != self.seen:
            self.seen = engine._seen
            self.lags.append(engine._lag)
            self.lag_max = max(self.lag_max, engine._lag)
        status = engine.status
        if status is not self.sent or now - self.sent_at >= ALIVE:
            self.sent, self.sent_at = status, now
            self.send(('status', status, engine.details(), self.figures()))

    def figures(self):
        lags = sorted(self.lags)
        passes = sorted(self.passes)

        def p99(values):
            return values[min(len(values) - 1, int(len(values) * 0.99))] * 1000.0 if values else None
        return {'samples': self.samples, 'skipped': self.engine.skipped, 'lag_max_ms': self.lag_max * 1000.0,
                'lag_p99_ms': p99(lags), 'pass_max_ms': self.pass_max * 1000.0, 'pass_p99_ms': p99(passes),
                'oversleep_max_ms': self.oversleep_max * 1000.0, 'dropped_up': self.dropped_up,
                'relayed': self.relayed, 'relay_dropped': self.relay_dropped, 'udp_ports': sorted(self.udp)}

    # FFB tuning records: the file opened and closed off the engine's thread, by one worker, in order

    def _record_start(self, path, origin, header):
        ffb_record.current = None                # one record at a time: the previous one ends here
        self._record_job(('start', path, origin, header))

    def _record_stop(self, path):
        ffb_record.current = None                # nothing more is logged from this pass on
        self._record_job(('stop', path))

    def _record_job(self, job):
        if self.jobs is None:
            self.jobs = queue.Queue()
            self.worker = threading.Thread(target=self._record_worker, name='ffb-record-files', daemon=True)
            self.worker.start()
        self.jobs.put(job)

    def _record_worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                ended = self._record_finish()
                if job[0] == 'start':
                    sampler = ffb_record.FfbSampler(*job[1:])
                    sampler.start()
                    self.recorder = sampler
                elif job[1] is not None and not ended:
                    self.send(('record_done', job[1], 0))      # (none was running for it)
            except Exception as e:
                logging.warning("ffb sidecar: %s", e)

    def _record_finish(self):
        sampler, self.recorder = self.recorder, None
        if sampler is not None:
            if ffb_record.current is sampler:
                ffb_record.current = None
            sampler.stop()
            self.send(('record_done', sampler.path, sampler.records))
        return sampler is not None

    def close(self):
        for sock in self.udp.values():
            sock.close()                         # (the listener's own copies keep the ports bound)
        self.udp = {}
        self._flush()
        if self.jobs is not None:
            self.jobs.put(('stop', None))
            self.jobs.put(None)
            self.worker.join(3.0)
        self._after_pass()


def _prctl(option, value):
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if isinstance(value, bytes):
            return libc.prctl(option, ctypes.c_char_p(value), 0, 0, 0) == 0
        return libc.prctl(option, ctypes.c_ulong(value), 0, 0, 0) == 0
    except Exception:
        return False


def _priority():
    """A low real-time priority if the system grants one, else a little nicer than the rest, else as it is; never a
    prompt. Returns what applied."""
    for policy, name in ((getattr(os, 'SCHED_RR', None), 'SCHED_RR'), (getattr(os, 'SCHED_FIFO', None), 'SCHED_FIFO')):
        if policy is None:
            continue
        try:
            prio = os.sched_get_priority_min(policy)
            os.sched_setscheduler(0, policy, os.sched_param(prio))
            return '%s %d' % (name, prio)
        except (OSError, AttributeError):
            pass
    try:
        before = os.getpriority(os.PRIO_PROCESS, 0)
        if before > -5:
            os.setpriority(os.PRIO_PROCESS, 0, -5)
        return 'nice -5'
    except (OSError, AttributeError):
        pass
    return 'normal scheduling'


def _child_entry(blob):
    import base64
    options = pickle.loads(base64.b64decode(blob))
    socks = [socket.socket(fileno=fd) for fd in options['fds']]
    _child_main(*socks[:3], options, relay=socks[3] if len(socks) > 3 else None)


def _child_main(data, ctrl, up, options, relay=None):
    """The engine process: FfbEngine's passes in this (main) thread until the GUI asks it to stop, closes the control
    channel, stops pinging, or goes (PR_SET_PDEATHSIG); then FfbEngine.stop()."""
    _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    _prctl(PR_SET_NAME, b'countersteer-ffb')
    if os.getppid() != options['parent']:
        return                                   # the GUI went before the signal was armed
    logging.root.handlers[:] = []
    logging.root.setLevel(options.get('log_level', logging.INFO))
    engine = None
    host = None
    try:
        faulthandler.enable()
        localedir = options.get('localedir')
        try:
            if localedir:
                locale.bindtextdomain('oversteer', localedir)
            locale.textdomain('oversteer')
            locale.setlocale(locale.LC_ALL, '')
        except Exception:
            pass
        setup = _resolve(options.get('setup'))
        if setup is not None:
            setup()
        priority = _priority()
        sys.setswitchinterval(ffb_engine.SWITCH_INTERVAL)
        kwargs = {k: _resolve(v) for k, v in (options.get('engine') or {}).items()}
        engine = ffb_engine.FfbEngine(**kwargs)
        engine.STALL_DUMPS = options.get('stall_dumps', True)
        host = _Host(engine, data, ctrl, up, relay)
        engine.range_hook = host.on_range
        logging.root.addHandler(_UpHandler(host))
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, host.on_signal)
        signal.set_wakeup_fd(host.sig_w, warn_on_full_buffer=False)
        wheel_owner.watch(host.on_owner)
        host.send(('ready', os.getpid(), priority))
        engine._running = True
        engine.loop(host.wait)
    except Exception:
        logging.exception("Countersteer FFB: the engine process")
        if host is not None:
            host.stop('error')
    finally:
        if engine is not None:
            try:
                engine.stop()                    # the force handed back, the stage zeroed, the game's level 100
            except Exception:
                logging.exception("Countersteer FFB: stopping the engine")
        if host is not None:
            logging.info("Countersteer FFB: engine process stopped (%s)", host.stop_reason or 'done')
            host.close()
