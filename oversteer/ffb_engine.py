"""Countersteer FFB: our own steering force, played through the driver's
replace mode (new-lg4ff README, "game_ffb_level, ext_force, ext_force_cap"):
game_ffb_level 0 mutes the game's force feedback (ramped over 200 ms) and
ext_force, written at the telemetry rate, plays ours in its place.

Three parts:

- SupportClassifier: from the telemetry arriving now (its rate, its gaps,
  the fields it carries), what Countersteer can do for the game, as a
  state and a reason. 'replace' (green): AC, ACC or ACR through the
  bridge's version 5 at 250 packets a second or more, no gap over 20 ms,
  the front tyres' Mz there and not flat, and a driver with game_ffb_raw. 'model' (amber): a game whose
  force could be modelled (Richard Burns Rally once the driver exposes the
  game's raw force, game_ffb_raw; Forza Horizon at 200 a second or more);
  nothing is played for it yet. 'add-on' (grey): every other game heard,
  and AC games that fail a 'replace' rule: the game keeps its own force.
  'none': no telemetry. Measured live over a sliding second, not taken
  from a list: Forza's rate follows its frame rate, and ACC or AC go
  green only if they really send the forces.
- RackForce: ACR's rack force from the front tyres' Mz (FL + FR), at the
  physics rate (333 a second), fitted to the force the game itself sends
  the wheel (the driver's game_ffb_raw: ext_force's units and sign, read
  the same while the game is muted): played only while the two agree
  (r >= 0.7 to start, checked all along), at the strength of the game's
  own (|raw| / |Mz|). finalFF (about Mz / 582 in marth's dump) gives the
  starting strength and a cross-check. A driver without game_ffb_raw gets
  no 'replace'.
- FfbEngine: its own thread. The telemetry listener hands each decoded
  sample over through offer(): a slot holding the newest sample and an
  Event, so the listener never waits for the engine and nothing queues
  (an engine that falls behind skips to the newest sample and counts the
  ones it skipped). For each new sample it classifies, fits and, while
  Countersteer FFB is on, the state is 'replace' and the fit ready, writes
  ext_force through one open file, crossfading with the driver's
  game_ffb_level ramp both ways (FfbEngine's docstring). It holds the
  wheel through wheel_owner while it plays and lets go at once for an
  exclusive owner (the wheel test, the calibration). On every way out the
  game gets game_ffb_level 100 at once: it is never left muted. The driver
  restores the game by itself after 1 s without writes, should this
  process die.
- Texture (ffb_texture): small vibrations from the same samples (road,
  landings, front lock), added on top of RackForce's force before the
  clamp and the fade gain, very low by default. ACR only for now.

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

from . import wheel_owner
from .ffb_texture import Texture

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
MZ_MISSING_RUN = 10                      # samples in a row without Mz (30 ms at 333/s) before it counts as gone

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
        self._mz_seen = False                  # this stream has sent front Mz
        self._mz_missing = 0                   # samples in a row without it
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
        if mz is None:
            self._mz_missing += arrivals
        else:
            self._mz_seen, self._mz_missing = True, 0
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
        if not self._mz_seen or self._mz_missing >= MZ_MISSING_RUN:
            return Verdict('add-on', 'no-mz', game, rate, gap_ms)
        if not self.has_game_ffb_raw:
            # The fit and the sign are checked against the force the game sends the wheel: no replace without it
            return Verdict('add-on', 'old-driver', game, rate, gap_ms)
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




class _Moments:
    """Exponentially weighted moments of a pair (x, y) over `tau` seconds:
    their correlation, x's spread and the ratio of the |y| to the |x| of
    the pairs marked unclipped."""

    def __init__(self, tau):
        self.tau = tau
        self.clear()

    def clear(self):
        self.s = [0.0] * 8               # w, x, y, xx, yy, xy, |x|, |y|

    def add(self, x, y, dt, unclipped=True):
        s = self.s
        decay = math.exp(-dt / self.tau)
        for i in range(8):
            s[i] *= decay
        s[0] += 1.0
        s[1] += x
        s[2] += y
        s[3] += x * x
        s[4] += y * y
        s[5] += x * y
        if unclipped:
            s[6] += abs(x)
            s[7] += abs(y)

    def _var(self, sq, mean):
        return self.s[sq] / self.s[0] - mean * mean if self.s[0] else 0.0

    def spread(self):
        """x's standard deviation (0 with nothing added)."""
        if not self.s[0]:
            return 0.0
        return math.sqrt(max(self._var(3, self.s[1] / self.s[0]), 0.0))

    def r(self):
        w = self.s[0]
        if not w:
            return None
        mx, my = self.s[1] / w, self.s[2] / w
        vx, vy = self._var(3, mx), self._var(4, my)
        if vx <= 0.0 or vy <= 0.0:
            return None
        return (self.s[5] / w - mx * my) / math.sqrt(vx * vy)

    def ratio(self):
        return self.s[7] / self.s[6] if self.s[6] > 0.0 else None


class RackForce:
    """The steering force from the front tyres' Mz, fitted to the force the
    game itself sends the wheel: the driver's game_ffb_raw level, in
    ext_force's own units and sign, read the same with the game muted (it is
    taken before game_ffb_level), so the check goes on while ours plays.

    - Strength: scale = sum |raw| / sum |Mz| (ext_force units per Nm), over
      TAU seconds of moving samples; raw near full scale (clipped) is left
      out. Until there is any, FULL / 582 from finalFF (marth's dump). The
      same ratio from finalFF is kept as a cross-check (`cross`: the raw
      scale over finalFF's, 1.0 when the game sends finalFF at FF_GAIN 100 %).
    - Ready to play: MIN_TIME s of moving samples with raw, front Mz spread
      over MIN_SPREAD Nm (corners), r(raw, Mz) of READY_FIT or more (positive:
      the sign matches, since ext_force = scale * Mz with scale > 0).
    - While ready: r(raw, Mz) over SHORT_TAU s under KEEP_FIT (while Mz
      moves) for MISMATCH_FOR s, or no fresh raw for RAW_STALE s while front
      Mz moved RAW_STALE_DMZ from where it was at the last one, drops it
      ('mismatch' / 'raw'); the fit then starts again from nothing. Stale
      raw with Mz steady (a straight, a game that uploads only on change)
      freezes the fit and keeps it playing.
    - r(raw, Mz) at REVERSED_FIT or lower over the long window: refused
      until the car changes.
    - `centring`: r(Mz, steering to the right) on samples with little
      front slip, logged as a hint at activation; it never gates (in a
      drift the force rightly pulls toward the countersteer)."""

    FF_SCALE = FULL / 582.0              # ext_force per Nm of front Mz from finalFF (finalFF ~ Mz / 582)
    SCALE_RANGE = (FF_SCALE / 8.0, FF_SCALE * 3.0)
    TAU = 10.0                           # s
    SHORT_TAU = 2.0
    RAW_CLIPPED = 32000                  # |raw| at or above: the game clipped it, not used for the scale
    FF_CLIPPED = 0.98
    MIN_TIME = 3.0                       # s of moving samples with raw before it may play
    MIN_SPREAD = 25.0                    # Nm: front Mz standard deviation that counts as evidence (corners)
    SHORT_SPREAD = 15.0                  # Nm: over the short window, for the keep check to judge
    READY_FIT = 0.7
    KEEP_FIT = 0.3
    MISMATCH_FOR = 0.5                   # s
    REVERSED_FIT = -0.5
    RAW_STALE = 0.5                      # s without a fresh raw level while ready ...
    RAW_STALE_DMZ = 20.0                 # ... and front Mz this far (Nm) from where it was then: dropped
    LOW_SLIP = 0.05                      # rad: front slip angle under this counts for the centring hint

    def __init__(self):
        self.reset()

    def reset(self, car=None):
        self.car = car
        self.scale = self.FF_SCALE
        self.refused = None
        self.cross = None
        self.centring = None
        self._ff = _Moments(self.TAU)
        self._centre = _Moments(self.TAU)
        self._restart()

    def _restart(self):
        """Forget the fit (the strength stays as it was)."""
        self._long = _Moments(self.TAU)
        self._short = _Moments(self.SHORT_TAU)
        self._last = None
        self._raw_at = None
        self._mz_at_raw = None
        self._bad_since = None
        self.moving_time = 0.0
        self.ready = False
        self.waiting = 'raw'             # why not ready: 'raw', 'corners', 'fit', 'mismatch'
        self.fit = self.fit_now = None

    def feed(self, t, sample, raw, new=True):
        """A sample, with the game's raw level now (None if it sent none
        lately); `new`: it came with an upload since the last sample (not a
        level held from an earlier one)."""
        if sample.car != self.car:
            self.reset(sample.car)
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 1.0)
        self._last = t
        mz = front_mz(sample)
        if raw is not None and new:
            self._raw_at = t
            if mz is not None:
                self._mz_at_raw = mz
        if mz is None or not (sample.speed or 0.0) > MIN_SPEED:
            return                       # standing: a game may well stop uploading a force that does not change
        ff = sample.game_ffb
        if ff is not None:
            self._ff.add(mz, ff, dt, abs(ff) < self.FF_CLIPPED)
        angles = sample.tyre_slip_angle
        if sample.steer is not None and angles is not None and max(abs(angles[0]), abs(angles[1])) < self.LOW_SLIP:
            self._centre.add(mz, -sample.steer, dt)     # Sample.steer is positive left
        if raw is None:
            self._check_raw(t, mz)
            return                       # the fit is frozen until the game uploads again
        self.moving_time += dt
        self._long.add(mz, raw, dt, abs(raw) < self.RAW_CLIPPED)
        self._short.add(mz, raw, dt)
        self._judge(t)

    def _check_raw(self, t, mz):
        """No fresh raw level. A game may upload its constant force only when
        it changes: on a steady straight that is no sign of trouble, and ours
        keeps playing with the fit frozen. It counts against the fit only once
        front Mz has moved RAW_STALE_DMZ Nm from where it was at the last
        upload, with none for RAW_STALE s: the game's force should have
        changed with it and did not reach the wheel."""
        stale = self._raw_at is None or t - self._raw_at > self.RAW_STALE
        if not stale:
            return
        moved = self._mz_at_raw is None or abs(mz - self._mz_at_raw) > self.RAW_STALE_DMZ
        if self.ready and moved:
            logging.info("Countersteer FFB: %s: front Mz moved %.0f Nm with no new constant force from the game "
                         "in %.1f s", self.car, abs(mz - (self._mz_at_raw or 0.0)),
                         t - (self._raw_at if self._raw_at is not None else t))
            self._restart()
            self.waiting = 'raw'
        elif not self.ready:
            self.waiting = 'raw'

    def _judge(self, t):
        long = self._long
        ratio = long.ratio()
        ff_ratio = self._ff.ratio()
        if ratio is not None:
            low, high = self.SCALE_RANGE
            self.scale = min(max(ratio, low), high)
            self.cross = ratio / (FULL * ff_ratio) if ff_ratio else None
        self.fit, self.fit_now = long.r(), self._short.r()
        self.centring = self._centre.r()
        if self.moving_time < self.MIN_TIME or long.spread() < self.MIN_SPREAD or self.fit is None:
            if not self.ready and self.waiting != 'mismatch':     # 'mismatch' stays said until it fits again
                self.waiting = 'corners'
            return
        if self.fit <= self.REVERSED_FIT:
            if self.refused is None:
                logging.warning("Countersteer FFB: %s refused: the game's force runs against the tyres' Mz "
                                "(r %.2f)", self.car, self.fit)
            self.refused, self.ready, self.waiting = 'reversed', False, None
            return
        if self.refused:
            return
        if self.ready:
            bad = self._short.spread() >= self.SHORT_SPREAD and (self.fit_now is None or self.fit_now < self.KEEP_FIT)
            if not bad:
                self._bad_since = None
            elif self._bad_since is None:
                self._bad_since = t
            elif t - self._bad_since >= self.MISMATCH_FOR:
                logging.info("Countersteer FFB: %s: the game's force stopped following the tyres (r %.2f)",
                             self.car, self.fit_now)
                self._restart()
                self.waiting = 'mismatch'
        elif self.fit >= self.READY_FIT:
            self.ready, self.waiting, self._bad_since = True, None, None
        elif self.waiting != 'mismatch':
            self.waiting = 'fit'

    def force(self, sample):
        """ext_force for this sample at full strength (0 without Mz)."""
        mz = front_mz(sample)
        if mz is None:
            return 0
        return min(max(self.scale * mz, -FULL), FULL)


class DriverReplace:
    """The driver's replace-mode attributes of one wheel (its sysfs
    directory): ext_force through one file kept open and written at offset
    0, game_ffb_raw through one kept open and read at offset 0,
    game_ffb_level opened for each (rare) write."""

    def __init__(self, path):
        self.path = path
        self._fd = None
        self._raw_fd = None

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

    def _int(self, name):
        try:
            with open(self._file(name)) as f:
                return int(f.readline())
        except (OSError, ValueError):
            return None

    def app_gain(self):
        """The driver applies the game's FF_GAIN to the game's force (not to ext_force); None unknown."""
        value = self._int('app_gain')
        return None if value is None else bool(value)

    def master_gain(self):
        """`gain`, 0..65535 = 0..100 % (more with the higher maximum), applied to ext_force too."""
        return self._int('gain')

    def ext_force_cap(self):
        """ext_force's ceiling, % of full scale after the gain."""
        return self._int('ext_force_cap')

    def read_raw(self):
        """game_ffb_raw as (level, count, id)."""
        if self._raw_fd is None:
            self._raw_fd = os.open(self._file('game_ffb_raw'), os.O_RDONLY)
        level, count, effect = os.pread(self._raw_fd, 64, 0).split()[:3]
        return int(level), int(count), int(effect)

    def force(self, value):
        if self._fd is None:
            self._fd = os.open(self._file('ext_force'), os.O_WRONLY)
        os.pwrite(self._fd, b'%d\n' % value, 0)

    def close(self):
        for name in ('_fd', '_raw_fd'):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)


Status = collections.namedtuple('Status', 'state reason game rate gap_ms enabled available active ready '
                                          'refused last_stop device waiting held_by raw_rate app_gain clipping')
IDLE_STATUS = Status('none', 'no-telemetry', None, 0.0, None, False, False, False, False, None, None, False,
                     None, None, None, False, False)

OWNER = 'countersteer-ffb'               # the engine's name with wheel_owner
FADE = 0.2                               # s: ours fades in and out over the driver's game_ffb_level ramp
REARM = 2.5                              # s after any release before it may take over again
RAW_FRESH = 0.1                          # s: a raw level whose count has not moved for longer is not the game's now
RAW_TWO_EFFECTS = 1.0                    # s: a change of effect id within this: two constant effects, raw unusable
QUICK_FADE = 0.1                         # s: the last value faded out when the wheel is wanted elsewhere or at quit
IDLE_POLL = 0.5                          # s: the wait while off or with no wheel (samples still wake it)
CLIP_NOTE = 0.05                         # share of ext_force writes past the driver's cap that the status mentions


class FfbEngine:
    """The engine thread (see the module's docstring).

    Taking over: game_ffb_level 0 (the driver ramps the game out over
    200 ms) while ours fades in over FADE, so the sum stays the force.
    Handing back while samples still flow (switched off, the state or the
    fit dropping): game_ffb_level 100 at once (the game ramps in over
    200 ms) while ours fades out over the same FADE: a crossfade, never a
    gap with neither force. Handing back with no samples to play (stall,
    device change, quit, failed write, an error, another owner taking the
    wheel): ext_force is no longer written (the driver holds it 30 ms and
    fades it over 100 ms) and game_ffb_level 100 is written at once. After
    any release it waits REARM s before it takes over again."""

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
        self._inhibit_req = False        # wheel_owner: an exclusive owner wants the wheel
        self._texture_req = None         # (seq, level 0..1, {part: on}) from the GUI, applied on the engine thread
        self._texture_seq = 0            # the GUI's last request number
        self._texture_seen = 0           # the engine's last applied
        self._running = False
        self._thread = None
        self._stop_lock = threading.Lock()
        self.classifier = SupportClassifier()
        self.model = RackForce()
        self.texture = Texture()
        self._enabled = False
        self._path = None
        self._driver = None
        self._available = False
        self._broken = False             # a write failed or the loop raised: nothing until the device is set again
        self._phase = 'idle'             # 'idle', 'in' (fading in), 'on', 'out' (fading out)
        self._fade_at = None
        self._fade_from = 1.0
        self._muted = False              # game_ffb_level 0 written and not yet 100 again
        self._owned = False              # holds the wheel with wheel_owner
        self._released_at = None
        self._raw_count = None
        self._raw_at = None              # when game_ffb_raw's count last moved
        self._raw_id = None
        self._raw_id_at = None           # when its effect id last changed
        self._raw_error = False
        self._raw_counts = collections.deque()      # (t, count) over the last second: the game's upload rate
        self.raw_rate = None             # constant uploads a second (diagnosis: logged at activation, in the status)
        self._app_gain = False           # the driver applies the game's FF_GAIN to its force, not to ours
        self._clip_at = None             # |ext_force| past which the driver's cap flattens it
        self._clipped = 0.0              # EW share of writes past it
        self._last_value = 0             # the last ext_force written
        self._closed = False             # stop() has begun: never take over again
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

    def set_texture(self, level, parts):
        """The texture's master level (0..1) and its parts' switches ({'road': bool, ...})."""
        # One tuple, swapped whole and never cleared: the engine applies it when its number is new, so a
        # request made while the engine reads the previous one is not lost
        self._texture_seq += 1
        self._texture_req = (self._texture_seq, level, dict(parts))
        self._wake.set()

    def set_device(self, path):
        """The wheel's sysfs directory, None for none. The old one's game
        force is restored by the engine thread."""
        self._path_req = path
        self._wake.set()

    def _on_inhibit(self):
        """wheel_owner, from the thread of an exclusive acquire: let go now."""
        self._inhibit_req = True
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
            self._closed = True
            thread, self._thread = self._thread, None
            self._running = False
            self._wake.set()
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    # Stuck: only the level is restored from here (its own file); the thread's open
                    # ext_force file and its state are left to it
                    logging.warning("Countersteer FFB: the engine thread did not stop; restoring the game's force")
                    self._safe_game_level(100)
                    self._let_go()
                    return
            self._quick_release('quit', self._clock())
            if self._driver is not None:
                self._driver.close()

    def _run(self):
        while self._running:
            busy = self._phase != 'idle' or (self._enabled and self._path is not None)
            self._wake.wait(self.POLL if busy else IDLE_POLL)
            self._wake.clear()           # before the slot is read: an offer after this wakes the next wait
            if not self._running:
                break
            try:
                self.step()
            except Exception:
                logging.exception("Countersteer FFB: stopped until the wheel is set again")
                self._broken, self._available = True, False
                try:
                    self._release('error', self._clock())
                except Exception:
                    logging.exception("Countersteer FFB: restoring the game's force")

    # -- the engine thread --

    def step(self, now=None):
        """One pass: requests, then the newest sample if there is one, else
        the stall check. The thread's loop body (tests call it directly)."""
        if now is None:
            now = self._clock()
        self._requests(now)
        slot = self._slot
        if self._broken:
            if slot is not None:
                self._seen = slot[0]     # latched off (an error, a failed write): samples are not looked at
            self._publish(now)
        elif slot is not None and slot[0] != self._seen:
            self._sample(slot)
        else:
            self._idle(now)

    def _requests(self, now):
        texture = self._texture_req
        if texture is not None and texture[0] != self._texture_seen:
            self._texture_seen = texture[0]
            self.texture.set(*texture[1:])
        if self._inhibit_req:
            self._inhibit_req = False
            self._quick_release('inhibited', now)
        path = self._path_req
        if path != self._path:
            self._release('device', now)
            self._muted = False          # the old wheel: restored above, or gone (its watchdog restores it)
            if self._driver is not None:
                self._driver.close()
            self._path, self._driver, self._available, self._broken = path, None, False, False
            self._raw_count = self._raw_at = self._raw_id = self._raw_id_at = None
            self._raw_counts.clear()
            self.raw_rate = None
            self.classifier.has_game_ffb_raw = False
            if path is not None:
                self._driver = self._driver_type(path)
                self._available = self._driver.available()
                self.classifier.has_game_ffb_raw = self._driver.has_game_ffb_raw()
                if self._available:
                    self._read_gains()
                    level = self._driver.game_level()
                    if level is not None and level < 100:
                        # Left muted by an earlier run (the driver would restore it after 1 s anyway)
                        self._safe_game_level(100)
            self._publish(None, force=True)
        enabled = self._enabled_req
        if enabled != self._enabled:
            self._enabled = enabled
            self._publish(None, force=True)

    def _raw(self, t):
        """(level, new): the game's constant force level now, or None (none
        sent lately, two effects, no attribute); `new`: uploaded since the
        last sample."""
        driver = self._driver
        if driver is None or not self.classifier.has_game_ffb_raw:
            return None, False
        try:
            level, count, effect = driver.read_raw()
        except (OSError, ValueError) as e:
            if not self._raw_error:
                self._raw_error = True
                logging.warning("Countersteer FFB: game_ffb_raw: %s", e)
            return None, False
        if count == 0:
            return None, False
        new = count != self._raw_count
        if new:
            self._raw_count, self._raw_at = count, t
        counts = self._raw_counts
        counts.append((t, count))
        while counts and counts[0][0] < t - 1.0:
            counts.popleft()
        span = counts[-1][0] - counts[0][0]
        if span >= 0.5:
            self.raw_rate = ((counts[-1][1] - counts[0][1]) & 0xffffffff) / span
        if effect != self._raw_id:
            if self._raw_id is not None:
                self._raw_id_at = t
            self._raw_id = effect
        if t - self._raw_at > RAW_FRESH:
            return None, False
        if self._raw_id_at is not None and t - self._raw_id_at < RAW_TWO_EFFECTS:
            return None, False
        return level, new

    def _sample(self, slot):
        seq, t, sample = slot
        arrivals = seq - self._seen      # the ones skipped arrived too: the rate counts them
        self.skipped += arrivals - 1
        self._seen, self._last_t = seq, t
        self.classifier.feed(t, sample, arrivals)
        self.model.feed(t, sample, *self._raw(t))
        playing = self._phase in ('in', 'on')
        verdict = self._verdict = self.classifier.verdict(t, holding=playing)
        want = (self._enabled and self._available and not self._broken and verdict.state == 'replace'
                and self.model.ready)
        if playing and not want:
            self._fade_out(t, 'off' if not self._enabled else 'state')
        elif self._phase == 'idle' and want and (self._released_at is None or t - self._released_at >= REARM):
            self._activate(t)
        if self._phase != 'idle':
            self._play(t, sample)
        self._publish(t)

    def _gain(self, t):
        if self._phase == 'in':
            g = (t - self._fade_at) / FADE
            if g >= 1.0:
                self._phase, g = 'on', 1.0
            return max(g, 0.0)
        if self._phase == 'out':
            # Down at the full rate from wherever it was (a fade-in cut short ends sooner)
            return max(self._fade_from - (t - self._fade_at) / FADE, 0.0)
        return 1.0

    def _play(self, t, sample):
        g = self._gain(t)
        rack = self.model.force(sample)
        # The texture on top (0.0 exactly when off: the steering force then goes out unchanged)
        value = int(round(min(max(rack + self.texture.force(t, sample, rack), -FULL), FULL) * g))
        try:
            self._driver.force(value)
            self.writes += 1
            self._last_value = value
            if self._clip_at is not None:
                self._clipped += ((abs(value) > self._clip_at) - self._clipped) / 333.0   # about a second
        except OSError as e:
            logging.warning("Countersteer FFB: ext_force: %s", e)
            self._broken, self._available = True, False      # until the device is set again
            self._release('write', t)
            return
        if self._phase == 'out' and g <= 0.0:
            self._phase = 'idle'         # faded out (0 written last): the game has its force back
            self._let_go()

    def _idle(self, now):
        if self._phase != 'idle' and self._last_t is not None and now - self._last_t > STALL:
            self._release('stall', now)
        self._verdict = self.classifier.verdict(now, holding=self._phase in ('in', 'on'))
        self._publish(now)

    def _activate(self, t):
        if self._closed:
            return
        if not wheel_owner.acquire(self._path, OWNER, exclusive=False, on_inhibit=self._on_inhibit):
            return                       # another owner has the wheel (status.held_by says who)
        self._owned = True
        try:
            self._driver.set_game_level(0)
        except OSError as e:
            logging.warning("Countersteer FFB: game_ffb_level: %s", e)
            self._broken, self._available = True, False
            self._safe_game_level(100)
            self._let_go()
            return
        self._muted = True
        self._phase, self._fade_at = 'in', t
        self._read_gains()
        m = self.model
        logging.info("Countersteer FFB: replacing %s's force (%.0f/s, game uploads %s/s, %.1f per Nm, r %.2f, "
                     "finalFF cross-check %s, centring hint %s, texture %s%s)", self._verdict.game, self._verdict.rate,
                     'unknown' if self.raw_rate is None else '%.0f' % self.raw_rate, m.scale, m.fit,
                     'none' if m.cross is None else '%.2f' % m.cross,
                     'none' if m.centring is None else '%.2f' % m.centring,
                     self.texture.describe(),
                     "; app_gain on: the game's FF_GAIN scales its force but not ours" if self._app_gain else '')

    def _read_gains(self):
        driver = self._driver
        self._app_gain = bool(driver.app_gain())
        gain, cap = driver.master_gain(), driver.ext_force_cap()
        # The driver: min(|ext| * gain / 65535, cap % of full scale)
        self._clip_at = FULL * cap / 100.0 * 65535.0 / gain if gain and cap is not None else None
        self._clipped = 0.0

    def _fade_out(self, t, reason):
        """Crossfade back: the game ramps in (game_ffb_level 100 now) while ours fades out."""
        self._fade_from = self._gain(t)
        self._phase, self._fade_at = 'out', t
        if self._safe_game_level(100):
            self._muted = False
        self._released_at = t
        self.last_stop = reason
        logging.info("Countersteer FFB: handing the force back (%s)", reason)

    def _release(self, reason, now):
        """Stop playing at once (the driver's watchdog fades the last
        ext_force) and give the game its force back. Safe at any time, twice too."""
        was = self._phase != 'idle' or self._muted
        playing = self._phase in ('in', 'on')
        self._phase = 'idle'
        if self._muted and self._safe_game_level(100):
            self._muted = False
        self._let_go()
        if was:
            self._released_at = now
            if playing or self.last_stop is None:
                self.last_stop = reason
            logging.info("Countersteer FFB: the game's force is back (%s)", reason)

    def _quick_release(self, reason, now):
        """The wheel wanted elsewhere, or quitting: the game's level back at
        once, the last value faded out over QUICK_FADE (in 10 ms steps, on
        this thread: there are no samples to wait for), then released."""
        if self._phase != 'idle' and not self._broken and self._driver is not None:
            if self._muted and self._safe_game_level(100):
                self._muted = False
            value, steps = self._last_value, 10
            try:
                for i in range(1, steps + 1):
                    self._driver.force(int(round(value * (1.0 - i / steps))))
                    time.sleep(QUICK_FADE / steps)
            except OSError as e:
                logging.warning("Countersteer FFB: ext_force: %s", e)
        self._release(reason, now)

    def _let_go(self):
        if self._owned:
            self._owned = False
            wheel_owner.release(self._path, OWNER)

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
        held = wheel_owner.current(self._path) if self._path is not None else None
        held_by = held[0] if held is not None and held[0] != OWNER else None
        status = Status(v.state, v.reason, v.game, v.rate, v.gap_ms, self._enabled,
                        self._available and not self._broken, self._phase in ('in', 'on'), self.model.ready,
                        self.model.refused, self.last_stop, self._path is not None, self.model.waiting, held_by,
                        None if self.raw_rate is None else round(self.raw_rate), self._app_gain,
                        self._phase != 'idle' and self._clipped > CLIP_NOTE)
        old = self.status
        figures = ('rate', 'gap_ms', 'raw_rate')
        changed = force or any(getattr(status, f) != getattr(old, f) for f in Status._fields if f not in figures)
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
    if not status.available and status.last_stop in ('write', 'error'):
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
        supported = _("{}: supported ({} packets/s with tyre forces)").format(game, rate)
        if status.refused:
            return 'grey', _("{}: the tyre forces run against the game's own force: {}").format(game, keeps)
        if status.held_by:
            return light, _("{}; the wheel is in use ({}): {}").format(supported, status.held_by, keeps)
        if not status.ready:
            waiting = {
                'raw': _("waiting for the game's own force on the wheel to check against"),
                'corners': _("learning the game's force, drive a few corners"),
                'fit': _("the game's force does not follow the tyres yet"),
                'mismatch': _("the game's force stopped following the tyres, learning again"),
            }.get(status.waiting, _("learning the game's force"))
            return light, "{}; {}".format(supported, waiting)
        if status.active:
            text = _("{}: Countersteer's force from the tyres, {} packets/s").format(game, rate)
            if status.raw_rate is not None:
                text += _(" (the game's own: {} a second)").format(status.raw_rate)
            if status.clipping:
                text += _("; it reaches the driver's ceiling for it (ext_force_cap, 80 % by default) and is "
                          "flattened there: lower the strength in the game")
            if status.app_gain:
                text += _("; the game's own gain setting is not applied to it, so the strength may differ")
            return light, text
        if status.enabled:
            return light, supported
        return light, _("{}: turn on to use Countersteer's force").format(supported)
    why = {
        'rbr-model': _("its force could be modelled (not built yet)"),
        'fh-model': _("{} packets/s: its force could be modelled (not built yet)").format(rate),
        'rbr-no-raw': _("sends no tyre forces"),
        'no-forces': _("{} packets/s, no tyre forces").format(rate),
        'old-bridge': _("the telemetry bridge is too old to send tyre forces: start the game with this "
                        "version's launch options"),
        'old-driver': _("the wheel driver is too old to check the game's force (game_ffb_raw): update the "
                        "driver"),
        'low-rate': _("{} packets/s, needs {}").format(rate, int(REPLACE_RATE)),
        'gaps': _("gaps up to {:.0f} ms in the telemetry, needs under {:.0f}").format(
            status.gap_ms or 0.0, REPLACE_GAP * 1000.0),
        'no-mz': _("sends no tyre Mz"),
        'mz-flat': _("its tyre Mz does not change"),
        'drive': _("drive off to check the tyre forces"),
    }.get(status.reason, status.reason)
    return light, "{}: {}: {}".format(game, why, keeps)
