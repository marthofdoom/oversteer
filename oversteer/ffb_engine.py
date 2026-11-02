"""Countersteer FFB: our own steering force, played through the driver's
replace mode (new-lg4ff README, "game_ffb_level, ext_force, ext_force_cap"):
game_ffb_level 0 mutes the game's force feedback (ramped over 200 ms) and
ext_force, written at the telemetry rate, plays ours in its place.

Three parts:

- SupportClassifier: from the telemetry arriving now (its rate, its gaps,
  the fields it carries), what Countersteer can do for the game, as a
  state and a reason. 'replace' (green): AC, ACC or ACR through the
  bridge's version 5 at 250 packets a second or more, no gap over 20 ms,
  the front tyres' Mz there and not flat. 'model' (amber): a game whose
  force could be modelled (Richard Burns Rally once the driver exposes the
  game's raw force, game_ffb_raw; Forza Horizon at 200 a second or more);
  nothing is played for it yet. 'add-on' (grey): every other game heard,
  and AC games that fail a 'replace' rule: the game keeps its own force.
  'none': no telemetry. Measured live over a sliding second, not taken
  from a list: Forza's rate follows its frame rate, and ACC or AC go
  green only if they really send the forces.
- RackForce: ACR's rack force from the front tyres' Mz (FL + FR), at the
  physics rate (333 a second), scaled to the game's own finalFF: finalFF
  follows front Mz at r +0.92, about Mz / 582 (marth's dump, ffb-research
  2026-11-01), and the running ratio of |finalFF| to |Mz| keeps the
  strength at the user's in-game setting. Played only once the fit holds
  (finalFF and Mz agree, and the force is centring against the steering);
  anti-correlated, it refuses.
- FfbEngine: its own thread. The telemetry listener hands each decoded
  sample over through offer(): a slot holding the newest sample and an
  Event, so the listener never waits for the engine and nothing queues
  (an engine that falls behind skips to the newest sample and counts the
  ones it skipped). For each new sample it classifies, fits and, while
  Countersteer FFB is on and the state is 'replace' with the fit ready,
  writes ext_force through one open file. Any other case (switched off,
  the state drops, telemetry stalls over 50 ms, the device changes, the
  app quits, a write fails) stops writing ext_force, so the driver's
  watchdog fades it, and writes game_ffb_level 100 at once: the game is
  never left muted. The driver restores the game by itself after 1 s
  without writes, should this process die.

Every sysfs write happens on the engine thread; the GUI only sets requests
(set_enabled, set_device) and reads `status`."""
import atexit
import collections
import logging
import math
import os
import threading
import time
from locale import gettext as _

AC_GAMES = ('acr', 'acc', 'ac')          # the bridge's games (OVST version 3 on names them)

WINDOW = 1.0                             # s: the sliding window the classifier measures over
MIN_SPEED = 5.0 / 3.6                    # m/s: Mz is judged only above this
REPLACE_RATE = 250.0                     # packets/s to go green
REPLACE_GAP = 0.020                      # s: the longest gap allowed to go green
# Once replacing, the window may sag to these before it stops (a single late
# packet must not hand the force back and forth, 200 ms ramps each way); a
# gap past STALL stops it anyway.
HOLD_RATE = 200.0
HOLD_GAP = 0.050
STALL = 0.050                            # s without a sample while replacing: stop
NONE_AFTER = 1.0                         # s without a sample: no telemetry
MODEL_RATE = 200.0                       # packets/s for a model-based force (Forza Horizon)
MZ_FLAT = 1.0                            # Nm: front Mz spanning less over the moving window is flat
MZ_MOVING_MIN = 50                       # moving samples in the window to judge Mz on

FULL = 32767                             # ext_force full scale (an FF_CONSTANT level at 0x4000)


Verdict = collections.namedtuple('Verdict', 'state reason game rate gap_ms')


class _WindowMax:
    """The maximum of the values pushed in the last `window` seconds (a
    monotonic deque: O(1) amortised per push)."""

    def __init__(self):
        self._items = collections.deque()

    def push(self, t, value):
        items = self._items
        while items and items[-1][1] <= value:
            items.pop()
        items.append((t, value))

    def expire(self, before):
        items = self._items
        while items and items[0][0] < before:
            items.popleft()

    def max(self):
        return self._items[0][1] if self._items else None

    def clear(self):
        self._items.clear()


class SupportClassifier:
    """What Countersteer can do for the game sending telemetry now (see the
    module's docstring for the states). feed() each sample with its arrival
    time and how many arrivals it stands for (1, more when the engine
    skipped some); verdict() at any time."""

    def __init__(self):
        self.has_game_ffb_raw = False    # the driver exposes the game's raw force (RBR's model hook)
        self.reset()

    def reset(self, game=None):
        self.game = game
        self._entries = collections.deque()    # (t, arrivals, v5, moving)
        self._arrivals = 0                     # sum of arrivals in _entries
        self._old = 0                          # entries in the window that are not version 5
        self._gaps = _WindowMax()
        self._mz_max = _WindowMax()            # front Mz of the moving samples
        self._mz_min = _WindowMax()            # minus it
        self._moving = 0                       # moving entries in the window
        self._first = None                     # when this game's stream started
        self._last = None
        self._mz_now = False                   # the newest sample has front Mz
        self.mz_live = None                    # Mz judged live (True) or flat (False); None not yet

    def feed(self, t, sample, arrivals=1):
        game = sample.game
        if game != self.game or (self._last is not None and t - self._last > NONE_AFTER):
            self.reset(game)
        if self._first is None:
            self._first = t
        if self._last is not None:
            self._gaps.push(t, t - self._last)
        self._last = t
        v5 = sample.packet_id is not None
        moving = sample.speed is not None and sample.speed > MIN_SPEED
        mz = front_mz(sample)
        self._mz_now = mz is not None
        self._entries.append((t, arrivals, v5, moving))
        self._arrivals += arrivals
        self._old += not v5
        if moving:
            self._moving += 1
            if mz is not None:
                self._mz_max.push(t, mz)
                self._mz_min.push(t, -mz)
        self._expire(t)

    def _expire(self, now):
        before = now - WINDOW
        entries = self._entries
        while entries and entries[0][0] < before:
            _t, arrivals, v5, moving = entries.popleft()
            self._arrivals -= arrivals
            self._old -= not v5
            self._moving -= moving
        self._gaps.expire(before)
        self._mz_max.expire(before)
        self._mz_min.expire(before)

    def verdict(self, now, holding=False):
        """The Verdict at `now`. `holding`: 'replace' is in use, so the
        looser HOLD_* limits apply to stay in it."""
        if self._last is None or now - self._last > NONE_AFTER:
            return Verdict('none', 'no-telemetry', self.game, 0.0, None)
        self._expire(now)
        game = self.game
        span = min(WINDOW, now - self._first)
        gap = max(self._gaps.max() or 0.0, now - self._last)
        gap_ms = gap * 1000.0
        if span < WINDOW * 0.9:
            return Verdict('none', 'measuring', game, 0.0, gap_ms)
        rate = self._arrivals / WINDOW
        if game in AC_GAMES:
            return self._ac(game, rate, gap, gap_ms, holding)
        if game == 'rbr':
            if self.has_game_ffb_raw:
                return Verdict('model', 'rbr-model', game, rate, gap_ms)
            return Verdict('add-on', 'rbr-no-raw', game, rate, gap_ms)
        if game == 'forza-fh' and rate >= MODEL_RATE:
            return Verdict('model', 'fh-model', game, rate, gap_ms)
        return Verdict('add-on', 'no-forces', game, rate, gap_ms)

    def _ac(self, game, rate, gap, gap_ms, holding):
        if self._old:
            return Verdict('add-on', 'old-bridge', game, rate, gap_ms)
        if rate < (HOLD_RATE if holding else REPLACE_RATE):
            return Verdict('add-on', 'low-rate', game, rate, gap_ms)
        if gap > (HOLD_GAP if holding else REPLACE_GAP):
            return Verdict('add-on', 'gaps', game, rate, gap_ms)
        if not self._mz_now:
            self.mz_live = False
            return Verdict('add-on', 'no-mz', game, rate, gap_ms)
        if self._moving >= MZ_MOVING_MIN:
            top, bottom = self._mz_max.max(), self._mz_min.max()
            if top is not None:
                self.mz_live = top + bottom >= MZ_FLAT       # max - min
        # Standing or slow: the last verdict on Mz stands (a stop at a start line keeps the force)
        if self.mz_live is None:
            return Verdict('add-on', 'drive', game, rate, gap_ms)
        if not self.mz_live:
            return Verdict('add-on', 'mz-flat', game, rate, gap_ms)
        return Verdict('replace', 'ok', game, rate, gap_ms)


def front_mz(sample):
    """FL + FR self-aligning torque (Nm, as the game writes it), or None."""
    mz = sample.tyre_mz
    if mz is None:
        return None
    total = mz[0] + mz[1]
    return total if math.isfinite(total) else None


class RackForce:
    """The steering force from the front tyres' Mz, scaled to the game's own
    finalFF (see the module's docstring). Moments are exponentially weighted
    over TAU seconds of moving samples."""

    INITIAL_SCALE = 1.0 / 582.0          # finalFF per Nm of front Mz (marth's ACR dump)
    SCALE_RANGE = (INITIAL_SCALE / 8.0, INITIAL_SCALE * 3.0)
    TAU = 10.0                           # s
    CLIPPED = 0.98                       # |finalFF| at or above: the game clipped it, not used for the scale
    MIN_TIME = 3.0                       # s of moving samples before it may play
    MIN_SPREAD = 25.0                    # Nm: front Mz standard deviation that counts as evidence (corners)
    READY_FIT = 0.7                      # r(finalFF, Mz) to start
    KEEP_FIT = 0.5                       # and to keep playing
    READY_CENTRING = 0.15                # r(force, steering to the right) to start: the force centres
    REVERSED_FIT = -0.3                  # anti-correlated: refuse
    REVERSED_CENTRING = -0.15

    def __init__(self):
        self.reset()

    def reset(self, car=None):
        self.car = car
        self._last = None
        self.moving_time = 0.0
        # EW sums: weight; mz, ff, steer; their squares; mz*ff, mz*steer; |ff|, |mz| unclipped
        self._s = [0.0] * 11
        self.ready = False
        self.refused = None              # 'reversed' once the fit is anti-correlated (until the car changes)
        self.scale = self.INITIAL_SCALE
        self.fit = self.centring = None

    def feed(self, t, sample):
        if sample.car != self.car:
            self.reset(sample.car)
        mz, ff, steer = front_mz(sample), sample.game_ffb, sample.steer
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 1.0)
        self._last = t
        if mz is None or ff is None or steer is None or not (sample.speed or 0.0) > MIN_SPEED:
            return
        self.moving_time += dt
        decay = math.exp(-dt / self.TAU)
        right = -steer                   # Sample.steer is positive left; the driver's position is positive right
        s = self._s
        for i in range(11):
            s[i] *= decay
        s[0] += 1.0
        s[1] += mz
        s[2] += ff
        s[3] += right
        s[4] += mz * mz
        s[5] += ff * ff
        s[6] += right * right
        s[7] += mz * ff
        s[8] += mz * right
        if abs(ff) < self.CLIPPED:
            s[9] += abs(ff)
            s[10] += abs(mz)
        self._judge()

    def _judge(self):
        s = self._s
        w = s[0]
        mean_mz, mean_ff, mean_st = s[1] / w, s[2] / w, s[3] / w
        var_mz = s[4] / w - mean_mz * mean_mz
        var_ff = s[5] / w - mean_ff * mean_ff
        var_st = s[6] / w - mean_st * mean_st
        if var_mz <= 0.0 or var_ff <= 0.0 or var_st <= 0.0:
            return
        self.fit = (s[7] / w - mean_mz * mean_ff) / math.sqrt(var_mz * var_ff)
        # The force we would play is scale * Mz with scale > 0: it centres if Mz follows the steering
        self.centring = (s[8] / w - mean_mz * mean_st) / math.sqrt(var_mz * var_st)
        if s[10] > 0.0:
            low, high = self.SCALE_RANGE
            self.scale = min(max(s[9] / s[10], low), high)
        if self.moving_time < self.MIN_TIME or math.sqrt(var_mz) < self.MIN_SPREAD:
            return                       # not enough evidence either way yet
        if self.fit <= self.REVERSED_FIT or self.centring <= self.REVERSED_CENTRING:
            if self.refused is None:
                logging.warning("Countersteer FFB: %s refused (r finalFF~Mz %.2f, r Mz~steering %.2f)",
                                self.car, self.fit, self.centring)
            self.refused, self.ready = 'reversed', False
        elif self.refused is None:
            if self.ready:
                self.ready = self.fit >= self.KEEP_FIT and self.centring > 0.0
            else:
                self.ready = self.fit >= self.READY_FIT and self.centring >= self.READY_CENTRING

    def force(self, sample):
        """ext_force for this sample (0 without Mz)."""
        mz = front_mz(sample)
        if mz is None:
            return 0
        return int(round(min(max(FULL * self.scale * mz, -FULL), FULL)))


class DriverReplace:
    """The driver's replace-mode attributes of one wheel (its sysfs
    directory): ext_force through one file kept open and written at offset
    0, game_ffb_level opened for each (rare) write."""

    def __init__(self, path):
        self.path = path
        self._fd = None

    def _file(self, name):
        return os.path.join(self.path, name)

    def available(self):
        return all(os.access(self._file(name), os.R_OK | os.W_OK) for name in ('ext_force', 'game_ffb_level'))

    def has_game_ffb_raw(self):
        return os.access(self._file('game_ffb_raw'), os.R_OK)

    def game_level(self):
        """game_ffb_level's target (None if unreadable)."""
        try:
            with open(self._file('game_ffb_level')) as f:
                return int(f.readline())
        except (OSError, ValueError):
            return None

    def set_game_level(self, level):
        fd = os.open(self._file('game_ffb_level'), os.O_WRONLY)
        try:
            os.write(fd, b'%d\n' % level)
        finally:
            os.close(fd)

    def force(self, value):
        if self._fd is None:
            self._fd = os.open(self._file('ext_force'), os.O_WRONLY)
        os.pwrite(self._fd, b'%d\n' % value, 0)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


Status = collections.namedtuple('Status', 'state reason game rate gap_ms enabled available active ready '
                                          'refused last_stop device')
IDLE_STATUS = Status('none', 'no-telemetry', None, 0.0, None, False, False, False, False, None, None, False)


class FfbEngine:
    """The engine thread (see the module's docstring)."""

    POLL = 0.01                          # s: the longest wait for a sample (bounds the stall check)
    STATUS_EVERY = 0.1                   # s between status updates while nothing changes

    def __init__(self, clock=time.monotonic, driver=DriverReplace):
        self._clock = clock
        self._driver_type = driver
        self._slot = None                # (seq, arrival time, sample): the newest, written by the listener only
        self._seq = 0
        self._wake = threading.Event()
        self._enabled_req = False        # GUI requests, applied on the engine thread
        self._path_req = None
        self._running = False
        self._thread = None
        self._stop_lock = threading.Lock()
        self.classifier = SupportClassifier()
        self.model = RackForce()
        self._enabled = False
        self._path = None
        self._driver = None
        self._available = False
        self._active = False             # writing ext_force
        self._muted = False              # game_ffb_level 0 written and not yet 100 again
        self._seen = 0                   # the last slot seq taken
        self._last_t = None              # arrival time of the newest sample taken
        self._verdict = Verdict('none', 'no-telemetry', None, 0.0, None)
        self._status_at = None
        self.last_stop = None            # why the force last stopped ('off', 'state', 'stall', ...)
        self.writes = 0                  # ext_force writes (tests, the log)
        self.skipped = 0                 # samples the engine never saw (it was behind)
        self.status = IDLE_STATUS

    # -- the listener's side: never blocks --

    def offer(self, now, sample):
        """The listener thread: a decoded sample that arrived at `now`
        (monotonic s). O(1): the slot is replaced, the engine woken."""
        self._seq += 1
        self._slot = (self._seq, now, sample)
        if not self._wake.is_set():
            self._wake.set()

    # -- the GUI's side --

    def set_enabled(self, on):
        """The "Countersteer FFB" switch."""
        self._enabled_req = bool(on)
        self._wake.set()

    def set_device(self, path):
        """The wheel's sysfs directory, None for none. The old one's game
        force is restored by the engine thread."""
        self._path_req = path
        self._wake.set()

    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name='countersteer-ffb', daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self, timeout=1.0):
        """Stop the thread; the game's force is restored before this returns."""
        with self._stop_lock:
            thread, self._thread = self._thread, None
            self._running = False
            self._wake.set()
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    logging.warning("Countersteer FFB: the engine thread did not stop; restoring the game's force")
            # Here as well as on the thread: idempotent, and certain even if the thread is stuck
            self._release('quit')
            if self._driver is not None:
                self._driver.close()

    def _run(self):
        while self._running:
            self._wake.wait(self.POLL)
            self._wake.clear()           # before the slot is read: an offer after this wakes the next wait
            if not self._running:
                break
            try:
                self.step()
            except Exception:
                logging.exception("Countersteer FFB")
                self._release('error')
        self._release('quit')

    # -- the engine thread --

    def step(self, now=None):
        """One pass: requests, then the newest sample if there is one, else
        the stall check. The thread's loop body (tests call it directly)."""
        self._requests()
        slot = self._slot
        if now is None:
            now = self._clock()
        if slot is not None and slot[0] != self._seen:
            self._sample(slot)
        else:
            self._idle(now)

    def _requests(self):
        path = self._path_req
        if path != self._path:
            self._release('device')
            if self._driver is not None:
                self._driver.close()
            self._path, self._driver, self._available = path, None, False
            if path is not None:
                self._driver = self._driver_type(path)
                self._available = self._driver.available()
                self.classifier.has_game_ffb_raw = self._driver.has_game_ffb_raw()
                if self._available:
                    level = self._driver.game_level()
                    if level is not None and level < 100:
                        # Left muted by an earlier run (the driver would restore it after 1 s anyway)
                        self._safe_game_level(100)
            self._publish(None, force=True)
        enabled = self._enabled_req
        if enabled != self._enabled:
            self._enabled = enabled
            if not enabled:
                self._release('off')
            self._publish(None, force=True)

    def _sample(self, slot):
        seq, t, sample = slot
        arrivals = seq - self._seen      # the ones skipped arrived too: the rate counts them
        self.skipped += arrivals - 1
        self._seen, self._last_t = seq, t
        self.classifier.feed(t, sample, arrivals)
        self.model.feed(t, sample)
        verdict = self._verdict = self.classifier.verdict(t, holding=self._active)
        want = (self._enabled and self._available and verdict.state == 'replace' and self.model.ready)
        if want and not self._active:
            self._activate()
        elif not want and self._active:
            self._release('off' if not self._enabled else 'state')
        if self._active:
            try:
                self._driver.force(self.model.force(sample))
                self.writes += 1
            except OSError as e:
                logging.warning("Countersteer FFB: ext_force: %s", e)
                self._available = False          # until the device is set again
                self._release('write')
        self._publish(t)

    def _idle(self, now):
        if self._active and self._last_t is not None and now - self._last_t > STALL:
            self._release('stall')
        self._verdict = self.classifier.verdict(now, holding=self._active)
        self._publish(now)

    def _activate(self):
        try:
            self._driver.set_game_level(0)
        except OSError as e:
            logging.warning("Countersteer FFB: game_ffb_level: %s", e)
            self._available = False
            self._safe_game_level(100)
            return
        self._muted = self._active = True
        logging.info("Countersteer FFB: replacing %s's force (%.0f/s, scale 1/%.0f)", self._verdict.game,
                     self._verdict.rate, 1.0 / self.model.scale)

    def _release(self, reason):
        """Stop playing (the driver's watchdog fades the last ext_force) and
        give the game its force back. Safe to call at any time, twice too."""
        was = self._active or self._muted
        self._active = False
        if self._muted:
            if self._safe_game_level(100):
                self._muted = False
        if was:
            self.last_stop = reason
            logging.info("Countersteer FFB: the game's force is back (%s)", reason)

    def _safe_game_level(self, level):
        driver = self._driver
        if driver is None:
            return True                  # no device: nothing muted can be reached
        try:
            driver.set_game_level(level)
            return True
        except OSError as e:
            logging.warning("Countersteer FFB: game_ffb_level %d: %s", level, e)
            return False

    def _publish(self, now, force=False):
        """A new `status` (one attribute swap, read by the GUI) when
        anything but the figures changed, else every STATUS_EVERY."""
        v = self._verdict
        status = Status(v.state, v.reason, v.game, v.rate, v.gap_ms, self._enabled, self._available,
                        self._active, self.model.ready, self.model.refused, self.last_stop, self._path is not None)
        old = self.status
        changed = force or status[:2] != old[:2] or status[5:] != old[5:] or status.game != old.game
        if not changed and now is not None and self._status_at is not None \
                and now - self._status_at < self.STATUS_EVERY:
            return
        self._status_at = now
        self.status = status


# -- for the UI --

LIGHTS = {'replace': 'green', 'model': 'amber', 'add-on': 'grey', 'none': 'off'}


def game_name(game):
    try:
        from .telemetry_view import game_label
        return game_label(game)
    except Exception:
        return game or _("the game")


def status_text(status, listening=True):
    """(light, text) for the FFB tab: light is 'green', 'amber', 'grey' or
    'off'. `listening`: the telemetry listener runs (it does while the
    switch is on)."""
    if not status.device:
        return 'off', _("No wheel")
    if not status.available and status.last_stop == 'write':
        return 'off', _("The wheel stopped taking the force: plug it in again")
    if not status.available:
        return 'off', _("Needs the new-lg4ff driver with replace mode (ext_force): update the driver")
    light = LIGHTS.get(status.state, 'off')
    game = game_name(status.game)
    keeps = _("the game keeps its own force feedback")
    if status.state == 'none':
        if status.reason == 'measuring':
            return light, _("{}: measuring the telemetry…").format(game)
        if not listening:
            return light, _("Turn on to check the game that is running")
        return light, _("No game telemetry")
    rate = int(round(status.rate))
    if status.state == 'replace':
        if status.refused:
            return 'grey', _("{}: the tyre forces do not match the game's own force: {}").format(game, keeps)
        if not status.ready:
            return light, _("{}: supported ({} packets/s with tyre forces); learning the game's force "
                            "strength, drive a few corners").format(game, rate)
        if status.active:
            return light, _("{}: Countersteer's force from the tyres, {} packets/s").format(game, rate)
        if status.enabled:
            return light, _("{}: supported ({} packets/s with tyre forces)").format(game, rate)
        return light, _("{}: supported ({} packets/s with tyre forces): turn on to use Countersteer's "
                        "force").format(game, rate)
    why = {
        'rbr-model': _("its force could be modelled (not built yet)"),
        'fh-model': _("{} packets/s: its force could be modelled (not built yet)").format(rate),
        'rbr-no-raw': _("sends no tyre forces"),
        'no-forces': _("{} packets/s, no tyre forces").format(rate),
        'old-bridge': _("the telemetry bridge is too old to send tyre forces: start the game with this "
                        "version's launch options"),
        'low-rate': _("{} packets/s, needs {}").format(rate, int(REPLACE_RATE)),
        'gaps': _("gaps up to {:.0f} ms in the telemetry, needs under {:.0f}").format(
            status.gap_ms or 0.0, REPLACE_GAP * 1000.0),
        'no-mz': _("sends no tyre Mz"),
        'mz-flat': _("its tyre Mz does not change"),
        'drive': _("drive off to check the tyre forces"),
    }.get(status.reason, status.reason)
    return light, "{}: {}: {}".format(game, why, keeps)
