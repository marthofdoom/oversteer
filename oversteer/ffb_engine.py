"""Countersteer FFB: our own steering force, played through the driver's
replace mode (new-lg4ff README, "game_ffb_level, ext_force, ext_force_cap"):
game_ffb_level 0 mutes the game's force feedback (ramped over 200 ms) and
ext_force, written at the telemetry rate, plays ours in its place.

Three parts:

- SupportClassifier: from the telemetry arriving now (its rate, its gaps,
  the fields it carries), what Countersteer can do for the game, as a
  state and a reason. 'replace' (green): AC, ACC or ACR through the
  bridge's version 5 at 250 packets a second or more, no gap over 20 ms,
  the front tyres' Mz there and not flat, and a driver with game_ffb_raw.
  Richard Burns Rally: 'replace' when the game's own force reaches
  game_ffb_raw at RBR_RAW_RATE uploads a second or more and RbrForce's checks
  pass (ffb_rbr), 'model' (amber) with the reason otherwise. BeamNG: from
  the Countersteer mod's CSB1 (BeamNGForceSource: started when BeamNG's
  OutGauge is heard with a wheel set, kept while OutGauge or CSB1 is; CSB1
  alone cannot start it, OutGauge must be on): 'replace' at CSB_RATE packets per second of game time
  with its clock moving and a rack force, grey otherwise. 'add-on' (grey): every other game heard, and AC games that fail a
  'replace' rule: the game keeps its own force. Forza Horizon is 'add-on'
  too, with the reason 'fh-addon' (amber) where Countersteer adds to the
  game's force (ForzaLightness below) and an fh-* reason (grey) where it
  cannot.
  'none': no telemetry. Measured live over a sliding second, not taken
  from a list: Forza's rate follows its frame rate, and ACC or AC go
  green only if they really send the forces.
- RackForce: ACR's rack force from the front tyres' steering torque (FL + FR Mz plus the caster arm times
  their Fy, front_torque), at the
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
- RbrForce (ffb_rbr): RBR's own force with NGP's saturating curve undone and
  played through a knee (the linearity setting), only while that keeps
  more detail than the game's curve. BeamNGRack: BeamNG's rack force from
  the mod, fitted to the game's own force like RackForce, with a soft lock.
  Both are read each pass (FAST_POLL, 1 ms) while they may play, not per
  telemetry sample: the telemetry only names the game and the car.
- ForzaLightness (ffb_addon): Forza Horizon keeps its own force (its
  game_ffb_level stays 100, never written) and the wheel gets g x the
  game's constant force as the front tyres run out of grip, g from the
  front slip. With a driver that has game_const_scale, the driver scales
  the game's constant effects itself: game_const_scale = round(100 g),
  written with each Forza sample and at least every FH_SCALE_KEEP s while
  under 100 (the driver's watchdog puts it back to 100 after 500 ms
  without a write), ext_force never written; every stop writes 100 at once
  (or leaves it to that watchdog if the write fails). An older driver
  without it: ext_force takes the share off, (g - 1) x the game's level
  read each pass like RBR. Either way it stops at once on stale raw, a low
  upload rate, two constant effects, Forza using no constant effect, or a
  constant force that is not the steering force (Forza on the
  spring/damper only); on app_gain too with ext_force (with the scale the
  game's FF_GAIN and ours apply to the same force).
- ForzaRack (with countersteer_forza_model on, the default; ffb_forza): Forza Horizon plays Countersteer's own force through
  the ACR chain. Forza sends no tyre forces, so the front kingpin torque is Newton's (the learnt mass, a_y and the yaw
  acceleration, the pneumatic trail fading with the combined slip), through the boost curve and hand-force mapping, the 60/s
  packets rebuilt by a low-pass that follows the packet rate (ffb_forza.OutputFilter), column damping, lock stop, texture
  and Kickback beside it. The game's force (game_ffb_raw) is only a safety check. Off, ForzaLightness above.
- Texture (ffb_texture): small vibrations from the same samples (road,
  landings, front lock), added on top of RackForce's force before the
  clamp and the fade gain, very low by default. ACR only for now.

Every sysfs write happens on the engine thread; the GUI only sets requests
(set_enabled, set_device) and reads `status`. In the app the engine runs in a
process of its own (ffb_process: its own interpreter and GIL, so nothing else
in Countersteer can hold up its passes); tests and the replay scripts use
these classes in-process."""
import atexit
import collections
import faulthandler
import logging
import math
import os
import select
import sys
import threading
import time
from locale import gettext as _

from . import wheel_owner
from . import car_data
from . import ffb_carprops
from . import ffb_delay
from . import ffb_dynamics
from . import ffb_forza
from . import ffb_gear
from . import ffb_handforce
from . import ffb_kick
from . import ffb_record
from . import ffb_spring
from . import wheel_torque
from .ffb_addon import ForzaLightness
from . import ffb_rbr
from .ffb_rbr import RbrForce
from .ffb_sources import BeamNGForceSource
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
HOLD_GAP = 0.050                         # s: a gap in the packets that the game's physics did not catch up on
# A game hitch (ACR: the physics step counter stalls, then the packets come in a burst with the counter catching
# up) is not a fault of the stream: it is held through (the last value written again, easing down) up to HITCH_GAP.
HITCH_GAP = 0.300
STALL = 0.300                            # s without a packet while replacing: stop (no burst can follow a gap this long)
HITCH_CATCHUP = 0.25                     # s after a gap's end in which a step burst marks it a hitch
HITCH_BURST = 2.0                        # a burst: the step counter moved this many times what the time explains ...
BURST_SPAN = 0.008                       # s: the step counter's rate is judged over at least this
HITCH_BURST_MIN = 3                      # ... and at least this many steps
STEP_RATE = 333.0                        # physics steps a second until the stream shows its own
NONE_AFTER = 1.0                         # s without a sample: no telemetry
FH_RATE = 30.0                           # Forza Horizon packets/s for the add-on (it sends ~60, frame-locked)
FH_RAW_RATE = 30.0                       # the game's constant uploads a second for the add-on ...
FH_RAW_HOLD = 20.0                       # ... and to keep it
RBR_RAW_RATE = 200.0                     # the game's constant uploads a second for RBR to go green (NGP: ~810)
RBR_HOLD_RATE = 150.0                    # ... and to stay
CSB_RATE = 200.0                         # BeamNG: CSB1 packets per second of the game's clock to go green (the mod: 500)
CSB_HOLD_RATE = 150.0
MZ_FLAT = 1.0                            # Nm: front Mz spanning less over the moving window is flat
MZ_MOVING_MIN = 50                       # moving samples in the window to judge Mz on
MZ_STILL = 0.01                          # Nm: front Mz that moves less than this is not being sent (a real Mz, ACR's tiny
                                         # one at low lateral force too, always moves more over a second)
MZ_STILL_FIRST = 1.0                     # s of unchanging Mz (moving) that call a stream flat that never showed it live
MZ_STILL_LIVE = 10.0                     # ... and one that did: a quiet straight is not a stream that stopped sending
MZ_MISSING_RUN = 10                      # samples in a row without Mz (30 ms at 333/s) before it counts as gone

OVERALL_STRENGTH = 1.0                   # "Steering strength" default: Countersteer's own force as the model gives it
STRENGTH_MAX = 1.3                       # ... and the most: 130 %, applied inside wheel_torque.rim_share before the mapping
OVERALL_KNEE = wheel_torque.KNEE         # "1:1 up to" default: the share of the wheel's peak played 1:1 in Nm
OVERALL_RBR_CUT = 30                     # "RBR force cut" default (Hz): the low-pass corner on RBR's column torque
RBR_CUT_OFF = 120                        # ... and the top of its range: no filter
OVERALL_ENGINE_FEEL = 100                # "Engine feel" default (profile key countersteer_engine_feel, %): the engine hum as a share of ffb_texture's EngineHum.CAP, 0 = off
OVERALL_CASTER_FLOOR = 100               # "Caster in slides" default (profile key countersteer_caster_floor, %): 0 = off
OVERALL_MAPPING = 'knee'                 # steering mapping default (profile INI only): 'knee' (1:1 up to a knee) or 'classic' (the uniform impedance scale)
OVERALL_FORZA_MODEL = True              # Forza Horizon through the ACR chain (profile INI only: countersteer_forza_model); False: ForzaLightness
OVERALL_FORZA_SPRING = True             # Forza's driver-side aligning spring (profile INI only: countersteer_forza_spring)
OVERALL_RBR_SPRING = True               # RBR's driver-side aligning spring, as ACR's (profile INI only: countersteer_rbr_spring); False: column damping and friction only
OVERALL_RBR_SOURCE = 'derived'           # RBR's force from the car's physics (RbrRack; profile INI only: countersteer_rbr_source); 'ngp': the game's own force, recovered and played through the chain
RBR_SOURCES = ('derived', 'ngp')
FORZA_CAR_WHEEL_MM = 375.0               # Forza's default car steering wheel (the profile's countersteer_car_wheel_mm wins once set)
OVERALL_PULL = True                      # "Drive pull" default (the scrub couple of the drive's left/right split in the force)
OVERALL_PULL_LEVEL = 100                 # ... and its level, % of the physical scrub radius (100 = physically correct)
OVERALL_COLUMN_DAMPING = 15              # "Column damping" default (profile key countersteer_column_added): 0 % = the wheel as it is; 100 % adds the real column's damping in hand force on top, rho^2 x COLUMN_DAMPING_NMS_RAD
                                         # (rho^2 for a 279 mm rim on a 350 mm car wheel: a column's damping is torque per rad/s, and
                                         # the hand feels it as torque / radius while the rim turns as radius, so it scales as rho^2)
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
        self.has_game_ffb_raw = False    # the driver exposes the game's raw force
        self.raw_rate = None             # the game's constant uploads a second (the engine measures it): RBR's force
        self.external_gaps = False       # the gaps come from note() (every packet), not from the samples fed
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
        self._hitch = _WindowMax()             # gaps that ended in a step burst (the game's hitches)
        self._pending = []                     # (end, gap) of gaps still waiting for a burst to be told from a loss
        self._prev = None                      # (t, packet_id) of the newest packet noted
        self._step_rate = STEP_RATE
        self._recent = collections.deque()     # (t, packet_id) over the last BURST_SPAN or so
        self._plain_gap = 0.0                  # the longest gap that was not a hitch (the last verdict's)
        self._mz_seen = False                  # this stream has sent front Mz
        self._mz_missing = 0                   # samples in a row without it
        self.mz_live = None                    # Mz judged live (True) or flat (False); None not yet
        self._mz_ever_live = False             # the stream has shown Mz spanning MZ_FLAT in a window
        self._still_anchor = None              # (mz, since): the moving Mz the run of unchanging values started at
        self._moving_at = None                 # when the last moving sample came

    def feed(self, t, sample, arrivals=1):
        game = sample.game
        if game != self.game or (self._last is not None and t - self._last > NONE_AFTER):
            self.reset(game)
        if self._first is None:
            self._first = t
        if self._last is not None and not self.external_gaps:
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
                anchor = self._still_anchor
                if anchor is None or abs(mz - anchor[0]) > MZ_STILL or t - (self._moving_at or t) > WINDOW / 2:
                    self._still_anchor = (mz, t)
            self._moving_at = t
        self._expire(t)

    def note(self, t, packet_id):
        """One packet's arrival (every one, in order, from the listener: not only the samples the engine took).
        A gap over REPLACE_GAP is a hitch when the step counter then moves HITCH_BURST times faster than the time
        explains within HITCH_CATCHUP of its end (the game catching up); until then it waits as pending, held to
        the hitch limit, and past it without a burst it is a loss."""
        self.external_gaps = True
        prev, self._prev = self._prev, (t, packet_id)
        if prev is None or t - prev[0] > NONE_AFTER or t < prev[0]:
            self._pending = []
            self._recent.clear()
            self._recent.append((t, packet_id))
            return
        dt = t - prev[0]
        steps = None if packet_id is None or prev[1] is None else packet_id - prev[1]
        # a burst: over the last BURST_SPAN or more the counter moved far faster than the time explains
        recent = self._recent
        recent.append((t, packet_id))
        while len(recent) > 1 and t - recent[1][0] >= BURST_SPAN:
            recent.popleft()
        span = t - recent[0][0]
        moved = None if packet_id is None or recent[0][1] is None else packet_id - recent[0][1]
        burst = moved is not None and span >= BURST_SPAN and moved >= HITCH_BURST_MIN \
            and moved >= HITCH_BURST * self._step_rate * span
        if not burst and steps is not None and steps > 0 and 0.002 < dt < 0.025:
            rate = min(max(steps / dt, 100.0), 1000.0)
            self._step_rate += (rate - self._step_rate) * 0.01
        keep = []
        for end, gap in self._pending:
            if burst and t - end <= HITCH_CATCHUP:
                self._hitch.push(end, gap)
            elif t - end > HITCH_CATCHUP:
                self._gaps.push(end, gap)
            else:
                keep.append((end, gap))
        self._pending = keep
        if dt > REPLACE_GAP:
            if burst:
                self._hitch.push(t, dt)
            else:
                self._pending.append((t, dt))

    def _expire(self, now):
        before = now - WINDOW
        if self._pending:
            late = [g for g in self._pending if now - g[0] > HITCH_CATCHUP]
            if late:
                for end, gap in late:
                    self._gaps.push(end, gap)
                self._pending = [g for g in self._pending if now - g[0] <= HITCH_CATCHUP]
        self._hitch.expire(before)
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
        if self.external_gaps and self._prev is not None:
            plain = self._gaps.max() or 0.0
            hitch = max([self._hitch.max() or 0.0] + [g[1] for g in self._pending] + [now - self._prev[0]])
            gap = max(plain, hitch)
            self._plain_gap = plain
        else:
            gap = max(self._gaps.max() or 0.0, now - self._last)
            self._plain_gap = gap
        gap_ms = gap * 1000.0
        if span < WINDOW * 0.9:
            return Verdict('none', 'measuring', game, 0.0, gap_ms)
        rate = self._arrivals / WINDOW
        if game in AC_GAMES:
            return self._ac(game, rate, gap, gap_ms, holding)
        if game == 'rbr':
            # The force is the game's own (game_ffb_raw), not the telemetry's: its upload rate decides
            if not self.has_game_ffb_raw:
                return Verdict('add-on', 'rbr-no-raw', game, rate, gap_ms)
            if self.raw_rate is None or self.raw_rate < (RBR_HOLD_RATE if holding else RBR_RAW_RATE):
                return Verdict('model', 'rbr-raw', game, rate, gap_ms)
            return Verdict('replace', 'ok', game, rate, gap_ms)
        if game == 'forza-fh':
            # The add-on's own checks (the game's constant force, its rate, the slip channels) are the engine's
            if rate < FH_RATE:
                return Verdict('add-on', 'fh-rate', game, rate, gap_ms)
            return Verdict('add-on', 'fh-addon', game, rate, gap_ms)
        return Verdict('add-on', 'no-forces', game, rate, gap_ms)

    def _ac(self, game, rate, gap, gap_ms, holding):
        if self._old:
            return Verdict('add-on', 'old-bridge', game, rate, gap_ms)
        if rate < (HOLD_RATE if holding else REPLACE_RATE):
            return Verdict('add-on', 'low-rate', game, rate, gap_ms)
        if self._plain_gap > (HOLD_GAP if holding else REPLACE_GAP) or gap > (HITCH_GAP if holding else REPLACE_GAP):
            return Verdict('add-on', 'gaps', game, rate, gap_ms)
        if not self._mz_seen or self._mz_missing >= MZ_MISSING_RUN:
            return Verdict('add-on', 'no-mz', game, rate, gap_ms)
        if not self.has_game_ffb_raw:
            # The fit and the sign are checked against the force the game sends the wheel: no replace without it
            return Verdict('add-on', 'old-driver', game, rate, gap_ms)
        if self._moving >= MZ_MOVING_MIN:
            top, bottom = self._mz_max.max(), self._mz_min.max()
            if top is not None:
                # Live when it spans MZ_FLAT; flat only when the values really do not change (the stream is not
                # sending Mz): ACR's front Mz is tiny at low lateral force and on straights, but never identical.
                # A stream that has shown Mz must stay unchanging for MZ_STILL_LIVE before it is called flat.
                if top + bottom >= MZ_FLAT:                  # max - min
                    self.mz_live = self._mz_ever_live = True
                elif self._still_anchor is not None and self._moving_at - self._still_anchor[1] >= \
                        (MZ_STILL_LIVE if self._mz_ever_live else MZ_STILL_FIRST):
                    self.mz_live = False
                elif self.mz_live is False and self._still_anchor is not None \
                        and self._moving_at - self._still_anchor[1] < MZ_STILL_FIRST:
                    self.mz_live = True                      # it moves again, if only a little
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


# ACR's steering force carries the caster and the scrub radius as well as the pneumatic trail: on marth's dump
# (14,247 moving snapshots in 8 segments of 12 sessions/cars; no car names in it) finalFF fits
# a * (front Mz - CASTER_TRAIL * front Fy - SCRUB * (Fx_FL - Fx_FR)); Mz alone R^2 0.85, with Fy 0.948, with
# the Fx couple 0.973 (the couple is uncorrelated with Fy, r 0.03). Game units: Fy is positive to the left and
# Mz comes out negative for positive Fy (Mz ~ -tp * Fy), so the mechanical part adds as -t * Fy.
#   - Pooled: a = 9.5e-4 per Nm, trail 27 mm (25.8 mm without the Fx term), scrub 26 mm. Per segment the trail
#     runs 6-46 mm and a 8e-4 to 1.6e-3 (the cars differ; a is refitted live per car by RackForce, the trail is
#     one number for all). The linear-slip slope (|slip angle| < 0.12 rad) agrees, 6-46 mm per segment too.
#   - Scrub: the left-right difference of the front tyres' Fx, -15 mm on near-straight samples (|Fy| < 800 N,
#     -14.7 to -15.5 mm in every segment with enough of them); -13 to -35 mm per segment in the full fit. The
#     smaller, steadier straight-line value is used (a real scrub radius is 10-30 mm; a driven axle's torque
#     steer is this couple).
#   - Past the peak (|slip angle| > 0.4 rad) Mz has fallen to a third and finalFF still follows Fy (r -0.94
#     against Mz's 0.65): the pull toward countersteer in a drift.
# The cars' own suspension data (DA_*_Suspension_Front, unversioned UE data assets, hardpoints without field
# names) was not decodable, and the dump has no surface input (grip 0 throughout), so one trail serves every car
# and surface; 27 mm is also a typical rally-car mechanical trail.
CASTER_TRAIL = 0.027                    # m
SCRUB = 0.0155                          # m: the physical scrub radius (the Kickback's amplitude)
PULL_SCRUB = 0.0094                     # m: Drive pull at 100 %: ACR's own (finalFF fit on 20261103-152713: 9.4 mm; in ACR the
                                        # pull is ~5 % of the force, mostly 5-150 Hz; 15.5 mm came from another drive)


# The pneumatic trail the steering force believes: tp = -Mz / Fy of the front pair, kept within [TP_MIN, TP_MAX] m
# (audit of drives 20261102-235822 and -192230: a real trail is at most ~1/6 of the contact length; p99 under
# 0.4 rad of slip is 30-46 mm; ACR's Mz spikes at landings imply 107-110 mm and flipped the force by +-0.68 of full
# scale in 25 ms; with the clamp the opposite-sign high-slip runs fell from 8 to 1). Only the steering path clamps
# (rim_torque); the fit and the classifier read the game's Mz as it is.
TP_MIN = -0.02                          # m
TP_MAX = 0.05                           # m
TP_FY_MIN = 500.0                       # N: under this |Fy| the implied trail means nothing (Mz over a near-zero Fy) and the game's Mz stands


def front_torque(sample, trail=CASTER_TRAIL, scrub=SCRUB, clamp=False):
    """Steering torque about the kingpins (Nm, front Mz's sign): FL + FR Mz plus the lateral forces' caster
    arm, -trail * (FL + FR Fy) (CASTER_TRAIL unless the car's own is known), and the scrub-radius couple of the driven or braked front axle,
    -scrub * (Fx_FL - Fx_FR) (SCRUB likewise). Terms without their forces are left out; None without Mz.
    `clamp`: Mz is first limited to the implied pneumatic trail TP_MIN..TP_MAX of the front Fy (Mz ~ -tp * Fy)."""
    mz = front_mz(sample)
    if mz is None:
        return None
    fy, fx = sample.tyre_fy, sample.tyre_fx
    if fy is not None:
        side = fy[0] + fy[1]
        if math.isfinite(side):
            if clamp and abs(side) >= TP_FY_MIN:
                lo, hi = sorted((-TP_MAX * side, -TP_MIN * side))
                mz = min(max(mz, lo), hi)
            mz -= trail * side
    if fx is not None:
        couple = fx[0] - fx[1]
        if math.isfinite(couple):
            mz -= scrub * couple
    return mz




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
    """The steering force from the front tyres' torque (Mz plus the caster arm times Fy, front_torque: a real
    steering's torque, and what ACR's own force follows; in a drift Mz fades and the Fy term pulls toward
    countersteer), fitted to the force the
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
      moves) for MISMATCH_FOR s drops it ('mismatch'; the fit then starts again
      from nothing); no fresh raw for RAW_STALE s while front Mz moved what the
      game's force is due an upload for (_stale_dmz) withdraws it ('raw') with
      the fit frozen, judged again at the next upload. Stale
      raw with Mz steady (a straight, a game that uploads only on change)
      freezes the fit and keeps it playing.
    - r(raw, Mz) at REVERSED_FIT or lower over the long window: refused
      until the car changes.
    - Magnitude (the physical model; the game's raw force does not set it, it only gates): the car's steering
      as a driver feels it. Column torque = front torque / ratio (13:1 by default, a rally car's typical;
      per car in ffb_carprops.STORE); power assist is a boost curve (ffb_carprops.driver_from_column: the
      driver's torque at the rim for that column torque and the speed, heavy assist parked, light at speed;
      assist class per car, 1.0 road / rally, 0.0 unassisted; an assisted car's driver torque tops out at
      wheel_torque.HYD_TOP_NM). The rim torque in Nm is played 1:1 against the wheel's peak up to the "1:1 up to"
      share of it (knee) and compressed above (wheel_torque.rim_share, with the overall steering strength applied
      inside it; an unknown wheel counts as the G29's 3.08 Nm) so the car's top torque (top_torque()) lands at
      wheel_torque.STEER_BUDGET of full scale, which is also its hard clamp: the rest stays free for texture and
      impacts. The column's damping, friction and stiffness the driver renders keep a frozen scale
      (COLUMN_SCALE, impedance()). The steering force plays the scrub couple only as "Drive pull" (pull_scrub: the
      physical SCRUB times the level, off by default; the learnt scrub no longer plays, it only serves the cross-check
      against the game's force). The output never passes RIM_SAFE_NM at the rim. The ratio is learnt per car from the road-wheel angle where the steering
      wheel's lock is known (ffb_carprops.RatioLearner), from the car table (+ a 0.13 m steering arm) until
      then. The car's
      learnt p99 of |front torque| (ffb_carprops.Quantile) is kept per car as a diagnostic only.
    - `centring`: r(Mz, steering to the right) on samples with little
      front slip, logged as a hint at activation; it never gates (in a
      drift the force rightly pulls toward the countersteer)."""

    _held_fit = None                     # (scale, trail, scrub) playing: kept through a fade out (hold())
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
    RAW_STALE = 2.0                      # s without a fresh raw level while ready ...
    RAW_STALE_DMZ = None                 # ... and front Mz this far (Nm) from where it was then: held off (None: relative
    RAW_STALE_FS = 0.1                   # to the force it should have moved: this share of full scale at the fit's scale,
    RAW_STALE_P99 = 0.15                 # or this share of the car's p99 of |front torque|, whichever is more)
    STALE_FREEZES = True                 # stale raw freezes the fit until uploads return (False: the fit starts again)
    LOW_SLIP = 0.05                      # rad: front slip angle under this counts for the centring hint
    LOCK_STEER = 0.97                    # |steer| at or over: at the game's lock. ACR adds a lock-stop force to the wheel's
                                         # that telemetry lacks, so those samples say nothing about the fit or the keep check
    FLOOR_LOW, FLOOR_MAX = 300, 4000     # |raw| between which uploads are counted for the game's minimum-force floor
    FLOOR_TOL = 3                        # a level this near the floor's is at it (ACR's sits at 1965 and 1967)
    FLOOR_SHARE = 0.08                   # the most common small |level| is the floor when it is this share of uploads
    FLOOR_MIN = 300                      # uploads at that level, and in all, before a floor is believed
    LEARNS = True                        # the car's trail and scrub are learnt from the game's force (ffb_carprops)
    POOLED_P99 = 540.0                   # Nm: p99 of |front torque| over marth's dump (per car 233-857), the prior
    PRIOR_S = 120.0                      # s of weight the prior counts for
    P99_EVERY = 0.25                     # s of sample time between p99 updates
    STEER_RATIO = ffb_carprops.STEER_RATIO   # steering wheel : road wheel, overall, a rally car's typical 12-15
    GAME_SHAPE_SPEED = 25.0              # m/s: the speed the game-shaped assist is levelled at (its p99 torque)
    ASSIST = 1.0                         # assist class: 1.0 road / rally power steering, 0.0 unassisted (race cars)
    RIM_SAFE_NM = 10.0                   # hard ceiling on the torque at the rim whatever the car and the wheel
    wheel_peak = None                    # Nm: the wheel's peak (wheel_torque), None unknown: the 3.08 Nm class
    rim_user = None                      # mm: the user's rim diameter (the profile's), None: the table's
    last_torque = None                   # Nm: the rim torque force() worked out last (ffb_handforce)
    wheel_rim = None                     # mm: the wheel's rim diameter (wheel_torque.rim_mm), None unknown: DEFAULT_RIM_MM
    car_wheel_mm = wheel_torque.CAR_WHEEL_MM   # mm: the car's steering wheel (the profile INI's countersteer_car_wheel_mm)
    strength = 1.0                       # the overall steering strength (the engine's), applied inside rim_share
    knee = wheel_torque.KNEE             # share of the wheel's peak played 1:1 in Nm (the engine's, "1:1 up to")
    mapping = OVERALL_MAPPING            # the steering mapping (the engine's, the profile INI's countersteer_mapping):
                                         # 'knee' = 1:1 up to a knee; 'classic' = the uniform impedance scale (share =
                                         # CLASSIC_SCALE x strength x torque / peak, clamped at the budget, no hydraulic
                                         # top end, damping at the same scale)
    pull_on = OVERALL_PULL               # "Drive pull": the scrub couple of the drive's left/right split in the force ...
    pull_level = OVERALL_PULL_LEVEL / 100.0     # ... at this share of the physical SCRUB
    kick_on = ffb_kick.OVERALL_KICK      # "Kickback": the onset pulse of the scrub couple (ffb_kick) ...
    kick_level = ffb_kick.OVERALL_KICK_LEVEL / 100.0   # ... at this share of its amplitude
    COLUMN_SCALE = 0.30                  # the column damping, friction and stiffness scale on the G29 class (frozen: it
                                         # was the p99-based impedance scale of a 2.1 Nm wheel on the usual car; the Nm values it
                                         # gives are kept, the driver units follow the peak)
    CLASSIC_SCALE = 0.30 * 3.08 / 2.1     # the classic mapping's uniform scale: the p99-based impedance scale of the drive
                                         # the classic feel was tuned on (the Fabia, p99 573 Nm), frozen. Its share was
                                         # 0.30 T / 2.1; the G29 peak is 3.08 now, so 0.44 keeps share = scale T / peak the same
    DAMPING_SCALE = OVERALL_COLUMN_DAMPING / 100.0   # ('knee' mapping) the share of the real column's damping (0.17 Nm s/rad) added
                                         # to the wheel's own, the "Column damping" setting (damping_pct). (These figures are
                                         # from when the setting was a total, the wheel's own included:) 1:1 settles best (slide sim, drive
                                         # 20261102-220623: 0.51 s, 4 % overshoot; the wheel's own 0.055 alone rings, 13
                                         # crossings) but drags 0.9 Nm at the p90 wheel speed (308 deg/s) on gravel (capture
                                         # 20261103-112548), too much resistance. Sweep there: 100 %: overshoot 1 % median /
                                         # 5 % worst, drag 0.91 Nm; 60 %: total 0.102 Nm s/rad, settle 0.59 s, overshoot 7 % /
                                         # 19 %, 2 crossings (the default); 50 %: 11 % / 26 %
    damping_pct = OVERALL_COLUMN_DAMPING # the setting as applied (%), added to the wheel's own (damping_scale() = pct/100 * rho^2); DAMPING_SCALE stays the default
    # The aligning torque split (feature flag, off: the driver's local spring `ffb_align` does not exist yet). With
    # it on, force() plays only the residual: the wanted rim torque less the spring the driver will render
    # (ffb_spring.DriverSpring: k clamped to `align_k_limit`, tau_max, slew and fades), through ext_force; the spring's
    # parameters go to `align_writer(theta_a_deg, k_fs_per_deg, tau_max_fs)` (None: nothing written; the writer is
    # abstract). k is the tangent stiffness of the model's rim torque against the angle error at this slip.
    align_on = False
    align_writer = None
    wheel_range = None                   # degrees: the wheel's range. Set, the split is worked out in the wheel's own
                                         # position frame (steer x range / 2, what the driver's target is in); None: in
                                         # the car's steering wheel degrees
    wheel_slope = 1.0                    # the measured wheel position per degree of -steer x range / 2 (FrameCheck.slope)
    wheel_lock_deg = None                # the steering wheel's measured lock (degrees, centre to full lock: FrameCheck.slope x
                                         # range / 2), the ratio's numerator where the car table has none; None: not measured
    ALIGN_RESET_JUMP = 15.0              # m beyond what the speed explains between two samples: a reset/teleport
    ALIGN_RESET_TURN = 0.5               # cos of the heading change between two samples (60 deg) under which it is one
    ALIGN_RESET_HOLD = 0.5               # s the spring is held at k = 0 after one (the driver fades it in 50 ms)
    align_k_limit = 0.060                # FS per degree: what the driver reports as ffb_align's 4th field (the report's
                                         # k_limit at a 6 ms delay); the writer's owner updates it from the driver
    ALIGN_LOCK = 0.52                    # rad: the road wheels' steering lock until the car's is learnt (RatioLearner);
                                         # steer -1..1 spans +-this
    ALIGN_FIT_ALPHA = 0.03               # rad: the stiffness is fitted on front slip angles under this (small signal)
    ALIGN_PK = 0.04                      # rad: front slip angle at which the kingpin torque's growth has halved
    ALIGN_K0 = 5700.0                    # Nm/rad: the small-signal kingpin torque per front slip angle (pooled dump)
    ALIGN_K_RANGE = (300.0, 20000.0)     # the per-car learnt stiffness is held in this (dump cars: 650-9100 linear)
    ALIGN_SHARE_MAX = 0.35               # the spring's most, as a share of the steering force now (measured 0.14-0.32 at front slip over 0.03 rad, left as it was)
    ALIGN_TAU_MIN = 0.01                 # ... and never under this share of full scale
    ALIGN_MIN_N = 600.0                  # samples of weight before the learnt stiffness replaces the prior
    ALIGN_EPS = 1e-3                     # rad: under this slip the small-signal stiffness is used
    ENGINE_OFF_RPM = 50.0                # rpm under which the engine is off (the power steering pump with it: no assist) ...
    ENGINE_OFF_S = 0.3                   # ... for this long (a single zero packet is not an engine stop)
    ASSIST_RAMP_S = 0.5                  # s the assist takes to fade out when the engine stops, and back in

    def __init__(self):
        self.learner = None
        self.car = None
        self._kick = ffb_kick.Kick()
        self.floor = None                # |raw| the game sends as its minimum force (ACR: 6 % of full scale), None unknown
        self._floor_counts = collections.Counter()
        self._floor_uploads = 0
        self.reset()

    def _feed_grip(self, t, sample):
        """The grip scale (ffb_forza.GripScale) from the sample's lateral acceleration and speed."""
        accel = getattr(sample, 'accel', None)
        if accel is not None and len(accel) > 1:
            self._grip.feed(t, accel[1], getattr(sample, 'speed', None))

    def reset(self, car=None):
        if self.learner is not None and self.car is not None:
            ffb_carprops.STORE.put(self.car, self.learner.trail, self.learner.scrub, force=True, p99=self.p99)
        self.car = car
        self._grip = ffb_forza.GripScale()
        self.trail, self.scrub = ffb_carprops.default_trail(CASTER_TRAIL), SCRUB    # (rally class: 37 mm until learnt)
        self.learner = None
        if self.LEARNS:
            kept = ffb_carprops.STORE.get(car) if car is not None else None
            self.trail, self.scrub = kept or (ffb_carprops.default_trail(CASTER_TRAIL), SCRUB)
            self.learner = ffb_carprops.TorqueLearner(self.trail, self.scrub)
        self.scale = self.FF_SCALE
        stored = ffb_carprops.STORE.get_p99(car) if car is not None and self.LEARNS else None
        self._q = ffb_carprops.Quantile()
        self.p99 = stored or self.POOLED_P99
        self._q.seed(self.p99, self.PRIOR_S)
        self._q_since = 0.0
        self._align = _Moments(self.TAU)
        self._ratio_l = ffb_carprops.RatioLearner()
        self._ratio_since = 0.0
        self._ratio_logged = False
        self.engine_known = False                        # a game with rpm has fed (BeamNG's CSB1 rpm is always 0: not used)
        self._boost = 1.0                                # the assist's share: 1 running, 0 with the engine off
        self._off_since = None
        self._engine_at = None
        self._spring = ffb_spring.DriverSpring()
        self._spring_at = None
        self._now = 0.0
        self._assist = ffb_carprops.AssistDetector()
        kept = ffb_carprops.STORE.get_rack(car) if car is not None else None
        table = None if kept else ffb_carprops.table_ratio(car_data.entry(car))
        self.ratio, self.assist = kept or (table or self.STEER_RATIO, self.ASSIST)
        self.ratio_known = bool(kept or table)           # learnt, or from the car table: not the rally default
        stored_lock = ffb_carprops.STORE.get_lock(car) if car is not None else None
        self.lock = stored_lock or self.ALIGN_LOCK
        self.lock_known = bool(stored_lock)
        self._align_prev = None                          # (time, pos, forward) at the last aligning split
        self._align_hold = None                          # time before which the spring is held at k = 0 (a reset)
        self.refused = None
        self.cross = None
        self.centring = None
        self._ff = _Moments(self.TAU)
        self._centre = _Moments(self.TAU)
        self._restart()

    def _ceiling(self):
        """The most of full scale the output may be: RIM_SAFE_NM at the rim on a wheel with a known peak."""
        peak = self.wheel_peak
        return min(1.0, self.RIM_SAFE_NM / peak) if peak else 1.0

    def _engine(self, t, sample):
        """Engine state from the rpm (ACR, Forza, RBR): under ENGINE_OFF_RPM for ENGINE_OFF_S, the boost ramps to 0
        over ASSIST_RAMP_S (the assist is hydraulic: no engine, no pump); it returns the same way. BeamNG is left out."""
        last, self._engine_at = self._engine_at, t
        dt = 0.0 if last is None else min(max(t - last, 0.0), 1.0)
        rpm = getattr(sample, 'rpm', None)
        off = False
        if getattr(sample, 'game', None) != 'beamng-ffb' and rpm is not None and math.isfinite(rpm):
            self.engine_known = True
            if rpm < self.ENGINE_OFF_RPM:
                if self._off_since is None:
                    self._off_since = t
                off = t - self._off_since >= self.ENGINE_OFF_S
            else:
                self._off_since = None
        else:
            self._off_since = None
        step = dt / self.ASSIST_RAMP_S
        self._boost = max(0.0, self._boost - step) if off else min(1.0, self._boost + step)

    def assist_level(self):
        """The assist class multiplier: countersteer_assist_strength times the engine's share (0 with the engine off)."""
        return ffb_carprops.power_settings()[1] * self._boost

    def _track(self, mz, dt):
        """The p99 of |front torque| (moving samples), a diagnostic."""
        self._q.add(abs(mz), dt)
        self._q_since += dt
        if self._q_since >= self.P99_EVERY:
            self._q_since = 0.0
            p99 = self._q.value()
            if p99 is not None:
                self.p99 = p99
                if self.LEARNS and self.car is not None:
                    ffb_carprops.STORE.put(self.car, self.trail, self.scrub, p99=p99)

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
        self._stale = False              # ready was withdrawn on stale raw; the fit is kept and judged again at the next upload

    def _torque(self, sample):
        return front_torque(sample, self.trail, self.scrub)

    def feed(self, t, sample, raw, new=True):
        """A sample, with the game's raw level now (None if it sent none
        lately); `new`: it came with an upload since the last sample (not a
        level held from an earlier one)."""
        if sample.car != self.car:
            self.reset(sample.car)
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 1.0)
        self._last = self._now = t
        self._engine(t, sample)
        mz = self._torque(sample)
        if raw is not None and new:
            self._raw_at = t
            if mz is not None:
                self._mz_at_raw = mz
            self._count_floor(raw)
        if mz is None or not (sample.speed or 0.0) > MIN_SPEED:
            return                       # standing: a game may well stop uploading a force that does not change
        self._track(mz, dt)
        if self.LEARNS and self.car is not None:
            self._learn_ratio(sample, dt)
        alpha = self._slip(sample)
        if alpha is not None and dt > 0.0 and abs(alpha) < self.ALIGN_FIT_ALPHA:
            self._align.add(self._sat(alpha), mz, dt)
        ff = sample.game_ffb
        if ff is not None:
            self._ff.add(mz, ff, dt, abs(ff) < self.FF_CLIPPED)
        angles = sample.tyre_slip_angle
        if sample.steer is not None and angles is not None and max(abs(angles[0]), abs(angles[1])) < self.LOW_SLIP:
            self._centre.add(mz, -sample.steer, dt)     # Sample.steer is positive left
        if raw is None:
            self._check_raw(t, mz)
            return                       # the fit is frozen until the game uploads again
        if self._stale:
            self._stale = False          # uploads are back: the frozen fit is judged again
        steer = getattr(sample, 'steer', None)
        if (steer is not None and abs(steer) >= self.LOCK_STEER) or (self.floor is not None and abs(abs(raw) - self.floor) <= self.FLOOR_TOL):
            return                       # at the game's lock (its stop force is not in telemetry) or at its minimum force: no information
        self.moving_time += dt
        self._long.add(mz, raw, dt, abs(raw) < self.RAW_CLIPPED)
        if abs(raw) < self.RAW_CLIPPED:
            self._assist.add(mz, raw, self.p99, dt)
        self._short.add(mz, raw, dt)
        if self.learner is not None and abs(raw) < self.RAW_CLIPPED:
            self._learn(sample, raw, dt)
        self._judge(t)

    def _count_floor(self, raw):
        """The game's minimum-force floor: ACR's raw uploads sit at one |level| (6 % of full scale, its "minimum
        force" setting; 1965 and 1967 in marth's drive) for a large share of the time. The |level| with the most
        uploads within FLOOR_TOL of it is the floor once that is FLOOR_SHARE of the uploads."""
        self._floor_uploads += 1
        level = abs(int(raw))
        if self.FLOOR_LOW <= level < self.FLOOR_MAX:
            self._floor_counts[level] += 1
        if self._floor_uploads % 256 or self._floor_uploads < self.FLOOR_MIN:
            return
        counts, tol = self._floor_counts, self.FLOOR_TOL
        best, best_n = None, 0
        for level in counts:
            n = sum(counts.get(level + d, 0) for d in range(-tol, tol + 1))
            if n > best_n:
                best, best_n = level, n
        self.floor = best if best is not None and best_n >= max(self.FLOOR_SHARE * self._floor_uploads, self.FLOOR_MIN / 2) \
            else None
        if self._floor_uploads >= 65536:             # a game setting, kept for the session: the tallies halve
            self._floor_uploads //= 2
            for k in list(counts):
                counts[k] //= 2
                if not counts[k]:
                    del counts[k]

    def _learn_ratio(self, sample, dt):
        """The road wheels' lock and, where the car table gives the steering wheel's lock, the overall steering ratio
        (ffb_carprops.RatioLearner), looked at once a second; the ratio slews."""
        learner = self._ratio_l
        learner.add(getattr(sample, 'steer', None), getattr(sample, 'yaw_rate', None), sample.speed, dt,
                    self._slip(sample), getattr(sample, 'wheel_speed', None) or getattr(sample, 'wheel_rot', None),
                    self._rear_slip(sample))
        self._ratio_since += dt
        if self._ratio_since < 1.0:
            return
        elapsed, self._ratio_since = self._ratio_since, 0.0
        lock = learner.lock()
        if lock is None:
            return
        self.lock, self.lock_known = lock, True
        ffb_carprops.STORE.set_lock(self.car, lock)
        entry = car_data.entry(self.car)
        wheel_deg = (entry.get('steering_wheel_lock_deg') if entry else None) or self.wheel_lock_deg
        ratio = learner.ratio(wheel_deg)
        if ratio is not None:
            step = ffb_carprops.RATIO_SLEW * elapsed
            self.ratio = min(max(ratio, self.ratio - step), self.ratio + step)
            self.ratio_known = True
            ffb_carprops.STORE.set_rack(self.car, self.ratio, self.assist)
            if not self._ratio_logged:
                self._ratio_logged = True
                logging.info("Countersteer FFB: aligning spring: lock %.3f rad, ratio %.1f (wheel mapping)", lock, ratio)

    def _learn(self, sample, raw, dt):
        """The car's trail and scrub from this sample (ffb_carprops), when it has the front forces."""
        mz, fy, fx = front_mz(sample), sample.tyre_fy, sample.tyre_fx
        if mz is None or fy is None or fx is None:
            return
        side, couple = fy[0] + fy[1], fx[0] - fx[1]
        if not (math.isfinite(side) and math.isfinite(couple)):
            return
        learner = self.learner
        accel = getattr(sample, 'accel', None)
        braking = (getattr(sample, 'brake', 0.0) or 0.0) > 0.3 and accel is not None and abs(accel[1]) < 3.0
        learner.add(mz, side, couple, raw, dt, self.scale, braking)
        self.trail, self.scrub = learner.trail, learner.scrub
        ffb_carprops.STORE.put(self.car, self.trail, self.scrub)

    def _stale_dmz(self):
        """How far front Mz (Nm) must have moved for the game's force to be due an upload: the change that makes
        RAW_STALE_FS of full scale at the fit's scale, or RAW_STALE_P99 of the car's p99, the larger."""
        if self.RAW_STALE_DMZ is not None:
            return self.RAW_STALE_DMZ
        return max(self.RAW_STALE_FS * FULL / max(self.scale, 1e-6), self.RAW_STALE_P99 * self.p99)

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
        moved = self._mz_at_raw is None or abs(mz - self._mz_at_raw) > self._stale_dmz()
        if self.ready and moved:
            logging.info("Countersteer FFB: %s: front Mz moved %.0f Nm with no new constant force from the game "
                         "in %.1f s", self.car, abs(mz - (self._mz_at_raw or 0.0)),
                         t - (self._raw_at if self._raw_at is not None else t))
            if self.STALE_FREEZES:
                self.ready, self._stale = False, True        # the fit is frozen as it is; judged at the next upload
            else:
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

    def hold(self):
        """The engine plays this fit: should a new car reset it, the fade out goes on with its trail, scrub, steering
        ratio and assist rather than stepping to the defaults."""
        self._held_fit = (self.trail, self.scrub, self.ratio, self.assist)

    def release(self):
        self._held_fit = None

    @staticmethod
    def _slip(sample):
        """The mean front slip angle (rad, positive when the car is steered further left than it travels), None
        without it."""
        sa = sample.tyre_slip_angle
        if sa is None or len(sa) < 2 or not (math.isfinite(sa[0]) and math.isfinite(sa[1])):
            return None
        return 0.5 * (sa[0] + sa[1])

    @staticmethod
    def _rear_slip(sample):
        """The mean rear slip angle (rad, same sign as _slip), None without it."""
        sa = sample.tyre_slip_angle
        if sa is None or len(sa) < 4 or not (math.isfinite(sa[2]) and math.isfinite(sa[3])):
            return None
        return 0.5 * (sa[2] + sa[3])

    def _sat(self, alpha):
        """The kingpin torque's shape in the slip angle: alpha / (1 + |alpha| / ALIGN_PK), saturating like the
        lateral force (so past the peak the caster share stays while the pneumatic part fades)."""
        return alpha / (1.0 + abs(alpha) / self.ALIGN_PK)

    def _sat_div(self, alpha):
        """What the stiffness is divided by for the spring's k: the inverse slope of _sat, (1 + |alpha| / ALIGN_PK)^2."""
        return (1.0 + abs(alpha) / self.ALIGN_PK) ** 2

    def align_stiffness(self):
        """Kingpin torque per unit of _sat(slip), Nm: learnt per car (through the origin) once there is enough."""
        s = self._align.s
        if s[0] < self.ALIGN_MIN_N or s[3] <= 0.0:
            return self.ALIGN_K0
        return min(max(-s[5] / s[3], self.ALIGN_K_RANGE[0]), self.ALIGN_K_RANGE[1])

    def align(self, sample):
        """The aligning split at this sample: a dict of theta_a_deg (the steering wheel angle where the front tyres'
        slip angle would be zero, left positive, in degrees of steering wheel with the car's road-wheel lock
        (`lock`, learnt; ALIGN_LOCK until then) and the overall ratio), theta_deg (where the wheel is now, same
        frame), k (Nm per degree: the TANGENT of the model's rim torque against the angle error at this slip, so
        the spring follows the model's stiffness as the wheel moves; the model's torque itself is not k times the
        error, the residual carries the difference), and model (that torque, Nm: the kingpin torque from the learnt
        stiffness and the saturating shape, through the column ratio and the boost curve); None without steer or
        slip. At zero slip it is K / ratio^2 per radian times the boost curve's centre slope."""
        alpha, steer = self._slip(sample), getattr(sample, 'steer', None)
        if alpha is None or steer is None or not math.isfinite(steer):
            return None
        ratio = self.ratio
        deg = 180.0 / math.pi
        theta = steer * self.lock * ratio * deg
        theta_a = theta - alpha * ratio * deg
        stiffness = self.align_stiffness()
        column = -stiffness * self._sat(alpha) / ratio
        source = self.power_steering()
        cls = 0.0 if source == 'off' else self.assist * self.assist_level()
        speed = getattr(sample, 'speed', None)
        hyd = not self.classic()
        curve = self._assist.curve(self.p99) if source == 'game' else None
        if curve:
            # the same assist curve rim_torque plays (the game's fitted one): its slope against the column torque by a
            # central difference, so the aligning spring never carries more or less than the played torque's own stiffness
            def game(c):
                return self._game_torque(c * ratio, cls, hyd, ratio, curve)
            model = game(column)
            h = 0.02 * abs(column) + 1e-3
            slope = max((game(column + h) - game(column - h)) / (2.0 * h), 0.0)
        else:
            model = ffb_carprops.driver_from_column(column, speed, cls, hyd)
            slope = ffb_carprops.driver_slope(column, speed, cls, hyd)
        k = slope * stiffness / self._sat_div(alpha) / (ratio * ratio) / deg
        return {'theta_a_deg': theta_a, 'theta_deg': theta, 'k': k, 'model': model}

    def align_ready(self):
        """The split may play: the car's steering ratio and lock are known (learnt or from the table) and the
        stiffness fit has enough samples."""
        return bool(self.ratio_known and self.lock_known and self.ready and self._align.s[0] >= self.ALIGN_MIN_N)

    def _reset_jump(self, sample):
        """The car was put somewhere else since the last aligning split: its position jumped further than its speed
        explains, or its heading turned past ALIGN_RESET_TURN in one sample."""
        pos = getattr(sample, 'pos', None)
        fwd = getattr(sample, 'forward', None)
        prev, self._align_prev = self._align_prev, (self._now, pos, fwd)
        if prev is None:
            return False
        dt = min(max(self._now - prev[0], 0.0), 1.0)
        try:
            if pos is not None and prev[1] is not None:
                dx, dz = pos[0] - prev[1][0], pos[2] - prev[1][2]
                speed = abs(getattr(sample, 'speed', 0.0) or 0.0)
                if math.hypot(dx, dz) > self.ALIGN_RESET_JUMP + 2.0 * speed * dt:
                    return True
            if fwd is not None and prev[2] is not None and dt <= 0.25:
                a, b = math.hypot(fwd[0], fwd[2]), math.hypot(prev[2][0], prev[2][2])
                if a > 1e-6 and b > 1e-6 and (fwd[0] * prev[2][0] + fwd[2] * prev[2][2]) / (a * b) < self.ALIGN_RESET_TURN:
                    return True
        except (TypeError, IndexError):
            return False
        return False

    def power_steering(self):
        """Where the assist comes from now: 'game' (the game's force has it: its curve is followed), 'model' (ours)
        or 'off' (column torque), by countersteer_power_steering (auto / on / off) and the detector."""
        mode = ffb_carprops.power_settings()[0]
        if mode == 'off' or self._boost <= 0.0:
            return 'off'                 # (a stopped engine: the column torque; during the ramp the scaled class slides to it)
        if mode == 'auto' and self._assist.state:
            return 'game'
        return 'model'

    def _front(self, sample, trail, scrub):
        """The kingpin torque (Nm) the chain starts from: the front tyres' Mz plus the caster and scrub terms
        (front_torque, Mz limited to the implied trail); None without Mz. A game without tyre forces overrides it."""
        return front_torque(sample, trail, scrub, clamp=True)

    def kick_trigger(self, sample):
        """An injected Kickback event (the step of Fx_FL - Fx_FR, N) for ffb_kick, None: the Fx and contact-normal test."""
        return None

    def rim_torque(self, sample, trail=None, scrub=None, ratio=None, assist=None):
        """The driver's torque at the rim (Nm, signed like the front torque) for this sample, None without Mz.
        Our boost curve (class `assist` x countersteer_assist_strength) where the game's force has no assist;
        where it has, the game's own gain-against-load curve (fitted live, AssistDetector.curve) scaled so the
        car's p99 lands where our class model puts it, so the assist is not stacked."""
        trail = self.trail if trail is None else trail
        # the scrub couple only as "Drive pull" (pull_scrub), unfiltered like ACR's own finalFF has it; the learnt
        # per-car scrub (self.scrub) is no longer played, it only serves the fit and the cross-check
        scrub = self.pull_scrub() if scrub is None else scrub
        hyd = not self.classic()
        mz = self._front(sample, trail, scrub)
        if mz is None:
            return None
        ratio = self.ratio if ratio is None else ratio
        speed = getattr(sample, 'speed', None)
        cls = self.assist if assist is None else assist
        source = self.power_steering()
        if source == 'off':
            return ffb_carprops.driver_from_column(mz / ratio, speed, 0.0, hyd)
        cls *= self.assist_level()
        if source == 'game':
            curve = self._assist.curve(self.p99)
            if curve:
                return self._game_torque(mz, cls, hyd, ratio, curve)
        return ffb_carprops.driver_from_column(mz / ratio, speed, cls, hyd)

    def _game_torque(self, mz, cls, hyd, ratio, curve):
        """The driver's torque (Nm) for a kingpin torque `mz` where the game's force has the assist: the game's fitted gain
        curve scaled so the car's p99 lands where our class model puts it (rim_torque and align share it)."""
        target = ffb_carprops.driver_from_column(self.p99 / ratio, self.GAME_SHAPE_SPEED, cls, hyd)
        level = target / (self.p99 * ffb_carprops.game_shape(self.p99, curve))
        game = math.copysign(level * abs(mz) * ffb_carprops.game_shape(mz, curve), mz)
        return mz / ratio + self._boost * (game - mz / ratio)       # (the engine stopping fades the curve back to the column)

    def classic(self):
        """The classic steering mapping is on (the INI's countersteer_mapping = classic)."""
        return self.mapping == 'classic'

    def pull_scrub(self):
        """The scrub radius (m) the steering force plays: PULL_SCRUB times the Drive pull level, 0 with it off."""
        return PULL_SCRUB * self.pull_level if self.pull_on else 0.0

    def rim_mm(self):
        """The wheel's rim diameter in mm: the user's, else the table's, else the G29's."""
        return self.rim_user or self.wheel_rim or wheel_torque.DEFAULT_RIM_MM

    def rho(self):
        """The hand-force scale of a car's torque on this wheel: rim diameter / the car's wheel diameter (0.797 for 279 /
        350 mm). The hand feels torque / radius, so 1:1 in hand force plays the car's torque times rho (knee mapping)."""
        return (self.rim_mm() / self.car_wheel_mm)

    def damping_scale(self):
        """The column damping's scale: the impedance scale in the classic mapping (a total), the "Column damping" share
        (damping_pct, 15 % by default) of the real column's in hand force in the knee one, on top of the wheel's own: pct/100 *
        rho^2 (ffb_dynamics.column_targets, additive)."""
        return self.impedance() if self.classic() else self.damping_pct / 100.0 * self.rho() ** 2

    def impedance(self, ratio=None, assist=None):
        """The scale of the column's damping, friction and stiffness sent to the driver (ffb_dynamics.column_targets):
        COLUMN_SCALE (frozen) on the G29 class, in proportion to the wheel's peak above wheel_torque.G29_CLASS_MAX_NM, at most 1. The torque
        itself no longer uses a scale (wheel_torque.rim_share plays it 1:1 up to the knee); the car's p99-based
        wheel_torque.impedance_scale is a diagnostic only."""
        peak = self.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
        return self.COLUMN_SCALE if peak <= wheel_torque.G29_CLASS_MAX_NM else min(1.0, self.COLUMN_SCALE * peak / wheel_torque.DEFAULT_PEAK_NM)

    def top_torque(self, ratio=None, assist=None):
        """The driver torque (Nm) the compression lands at the steering budget (rim_share's x_top): the hydraulic top
        end's HYD_TOP_NM for an assisted car; for an unassisted one its p99 (through the column ratio), at least
        twice the knee's torque so the compression stays gentle."""
        ratio = self.ratio if ratio is None else ratio
        cls = self.assist if assist is None else assist
        cls = 0.0 if ffb_carprops.power_settings()[0] == 'off' else cls * self.assist_level()
        if cls > 0.0:
            return ffb_carprops.hyd_top()
        peak = self.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
        return max(abs(self.p99 / ratio), 2.0 * self.knee * peak)

    def force(self, sample):
        """ext_force for this sample at the overall steering strength (0 without Mz): the rim torque (rim_torque) as a
        share of the wheel's peak, 1:1 up to the knee and compressed above, clamped at the steering budget
        (wheel_torque.rim_share; the classic mapping: the uniform scale times the strength). A reversing car (backwards over REVERSE_SPEED) keeps the
        game's own sign of Mz and Fy, which turn with the travel (the caster trail is negative to it: the wheel swings
        to the lock, and a driver holds it); the force is not faded, only bounded to REVERSE_MAX of full scale, so a
        spin never holds the wheel at the stop with the whole of it."""
        held = None if self.ready else self._held_fit
        torque = self.rim_torque(sample, held[0], None, held[2], held[3]) if held is not None else self.rim_torque(sample)
        if torque is None:
            return 0
        self.last_torque = torque                # (for the hand-force summary: the car's torque this sample)
        ratio, assist = (held[2], held[3]) if held is not None else (None, None)
        peak = self.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
        classic = self.classic()
        if classic:
            # the uniform impedance scale (frozen) times the strength, hard-clamped at the budget
            scale = self.CLASSIC_SCALE if peak <= wheel_torque.G29_CLASS_MAX_NM else self.impedance()
            share = scale * self.strength * torque / peak
            share = max(-wheel_torque.STEER_BUDGET, min(wheel_torque.STEER_BUDGET, share))
        else:
            rho = self.rho()
            x_top = self.top_torque(ratio, assist) * rho         # the car's top, in hand force at this rim
            share = wheel_torque.rim_share(torque * rho, self.wheel_peak, self.strength, x_top, self.knee)
        if self.align_on:
            split = self.align(sample)
            if split is not None:
                # the driver's spring is in FS: the model's stiffness at the same scale; what it renders (its own
                # k limit, tau_max, slew and fades) comes off, the residual goes through ext_force
                # (the hydraulic top end is in split['k'] already; the mapping's slope and the strength are the
                # steering force's own, so the spring and the residual add up to the played share)
                if classic:
                    k_fs = scale * split['k'] * self.strength / peak
                else:
                    k_fs = split['k'] * rho * wheel_torque.rim_share_slope(torque * rho, self.wheel_peak, self.strength,
                                                                           x_top, self.knee) * self.strength / peak
                # never the only force: near zero slip the spring was 77-105 % of the total and ext_force about 0, a clean
                # force locked to the wheel's position; held to a share of the total there, ext_force carries the rest
                tau = min(wheel_torque.TOP, max(self.ALIGN_SHARE_MAX * abs(share), self.ALIGN_TAU_MIN))
                theta_a, theta = split['theta_a_deg'], split['theta_deg']
                if self.wheel_range:
                    # the wheel's own position frame: the game maps the wheel's travel to steer -1..1, so one degree
                    # of the wheel is `g` degrees of the car's steering wheel (the torque is the same, so k is per
                    # wheel degree); the target is held to the wheel's range
                    half = self.wheel_range / 2.0
                    # (the game may map less or more of the wheel's range than the steering sweeps: `wheel_slope`
                    # is the measured wheel position per expected degree, 1.0 when the mapping is the full range)
                    g = half / max(self.lock * self.ratio * 180.0 / math.pi, 1e-6) * self.wheel_slope
                    theta, theta_a, k_fs = theta * g, theta_a * g, k_fs / g
                    theta_a = min(max(theta_a, -half), half)
                if self._reset_jump(sample):
                    self._align_hold = self._now + self.ALIGN_RESET_HOLD
                if self._align_hold is not None and self._now < self._align_hold:
                    k_fs = 0.0                   # a reset: the driver fades the spring out; the mirror starts again after
                    self._spring.reset()
                    self._spring_at = None
                dt = 0.0 if self._spring_at is None else min(max(self._now - self._spring_at, 0.0), 0.1)
                self._spring_at = self._now
                share -= self._spring.force(theta_a, theta, k_fs, tau, self.align_k_limit, dt)
                if self.align_writer is not None:
                    self.align_writer(theta_a, k_fs, tau)
        share = self._kicked(sample, share, ratio, assist)
        limit = FULL * self._ceiling()
        vel = getattr(sample, 'vel', None)
        if vel is not None and len(vel) and vel[0] is not None and vel[0] < -REVERSE_SPEED:
            limit = min(limit, FULL * REVERSE_MAX)
        return min(max(FULL * share, -limit), limit)


    def _kicked(self, sample, share, ratio, assist):
        """The steering share plus Kickback's pulse (ffb_kick)."""
        return self._kick.share(self, sample, share, ratio, assist)


class BeamNGRack(RackForce):
    """BeamNG's rack force (the Countersteer mod's CSB1: the steering hydros' beam stress in N, the mean over the
    physics steps since the last packet, up to 500 a second), fitted to the game's own force on the wheel
    (game_ffb_raw) the way RackForce fits ACR's Mz. BeamNG's own force is this rack force smoothed, limited to
    400 Hz, with damping and its soft lock added: ours is the rack force itself.

    Differences from ACR: the input is the rack force; there is no finalFF cross-check; and the sign between the
    beam stress and the wheel's force is learnt, not assumed (the stress's sign convention is the hydro's,
    unconfirmed on a capture): ready needs |r| >= READY_FIT, and the sign of r is then kept (a fit whose sign
    turns is a mismatch). It is the game's own force that sets it, so ours never pushes the other way. The engine
    resets it all (scale, sign, centring) when CSB1's seq or game time goes back: a new vehicle or a reset.

    The soft lock: muting the game drops BeamNG's soft lock with its force, so ours keeps one (`softlock`). The
    packet's steering input reaches +-1 at the car's lock; from SOFTLOCK_FROM a push grows (smoothstep) to SOFTLOCK
    of full scale at 1 and is held there (the input stops at 1), with damping from the input's rate (per second of
    game time) while it moves outward, faded in with speed up to SOFTLOCK_SPEED. Its direction is the game's
    centring: the sign of r(game force, steering input) over samples with little front side slip only (in a drift
    the force rightly pulls toward the countersteer, against that relation), latched once (`centre_sign`) from
    CENTRE_MIN_TIME s of such samples at |r| >= CENTRE_LATCH, never flipped while it holds. Ready needs it, so the
    soft lock never plays before that agreement. The same relation turning round on low-slip samples over the
    short window (r * centre_sign <= -CENTRE_FLIP for MISMATCH_FOR s) is a mismatch: the force stops and both
    are learnt again."""

    LEARNS = False                       # no tyre forces: nothing to learn the trail from
    FF_SCALE = FULL / 4000.0             # ext_force per N until the fit has any (a guess: the fit replaces it)
    SCALE_RANGE = (FULL / 200000.0, FULL / 20.0)
    MIN_SPREAD = 150.0                   # N
    SHORT_SPREAD = 80.0
    RAW_STALE = 0.5
    RAW_STALE_DMZ = 150.0
    STALE_FREEZES = False
    SIDE_SLIP_LOW = 1.5                  # front |side slip| (the game's unit, a slip speed in m/s?) under: low slip
    CENTRE_LATCH = 0.6
    CENTRE_FLIP = 0.5                    # r * centre_sign at or under -this over the short window: turned round
    CENTRE_MIN_TIME = 3.0                # s of moving low-slip samples
    CENTRE_SPREAD = 0.05                 # steering input standard deviation that counts as evidence
    SOFTLOCK_FROM = 0.94                 # |steering input| where the soft lock starts
    SOFTLOCK = 0.3                       # its push at the lock, share of full scale
    SOFTLOCK_DAMP = 0.1                  # share of full scale per (steering input / s) moving outward, in the zone
    SOFTLOCK_MAX = 0.5                   # the push and the damping together
    SOFTLOCK_SPEED = 2.0                 # m/s: the soft lock fades in up to this
    RATE_TAU = 0.04                      # s of game time: the steering rate's low-pass (the input steps per frame)
    RATE_GAP = 0.1                       # s of game time between samples past which the rate starts again at 0
    CENTRE_NOTE = 30.0                   # s of moving samples without a centring latched: said in the status

    def reset(self, car=None):
        self._clear_centre()
        self._steer_at = None            # (steering input, game time) of the last sample
        self.steer_rate = 0.0            # its rate per second of game time (smoothed)
        self._steer_smooth = None        # the input low-passed (the rate's first stage)
        if not hasattr(self, '_held'):
            self._held = None            # (sign, scale, centre_sign) playing: kept through a fade out (hold())
        super().reset(car)

    def hold(self):
        """The engine plays this fit: should it be dropped (a mismatch, a new vehicle), the fade out goes on
        with it rather than stepping to nothing."""
        self._held = (self.sign, self.scale, self.centre_sign)

    def release(self):
        self._held = None

    def _playing(self):
        """(sign, scale, centre_sign) to play: the fit's, or the one held while it fades out."""
        if self.sign is None and self._held is not None:
            return self._held
        return self.sign, self.scale, self.centre_sign

    def _clear_centre(self):
        self.centre_sign = None
        self._centre_long = _Moments(self.TAU)
        self._centre_short = _Moments(self.SHORT_TAU)
        self.centre_time = 0.0
        self._flip_since = None

    def _restart(self):
        super()._restart()
        self.sign = None

    def feed(self, t, sample, raw, new=True):
        if sample.car != self.car:
            self.reset(sample.car)
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 1.0)
        self._last = t
        steer = getattr(sample, 'steer_input', None)
        self._rate(steer, sample.game_time)
        x = sample.rack_force
        if raw is not None and new:
            self._raw_at = t
            if x is not None:
                self._mz_at_raw = x
        if x is None or not (sample.speed or 0.0) > MIN_SPEED:
            return
        if raw is None:
            self._check_raw(t, x)
            return
        if steer is not None and self._low_slip(sample):
            self._centre_long.add(steer, raw, dt)
            self._centre_short.add(steer, raw, dt)
            self.centre_time += dt
        self.moving_time += dt
        self._long.add(x, raw, dt, abs(raw) < self.RAW_CLIPPED)
        self._short.add(x, raw, dt)
        self._judge(t)

    def _rate(self, steer, game_time):
        last = self._steer_at
        self._steer_at = None if steer is None or game_time is None else (steer, game_time)
        if last is None or self._steer_at is None:
            return
        dgt = game_time - last[1]
        if dgt <= 0.0:
            return
        if dgt > self.RATE_GAP or self._steer_smooth is None:
            self.steer_rate, self._steer_smooth = 0.0, steer
            return
        # Two low-passes over RATE_TAU s of game time, on the input and on its rate: the input moves in steps once
        # a frame (7 packets flat, then a step 2 ms wide), whose raw derivative would buzz at the frame rate
        alpha = dgt / (self.RATE_TAU + dgt)
        smooth = self._steer_smooth + alpha * (steer - self._steer_smooth)
        self.steer_rate += alpha * ((smooth - self._steer_smooth) / dgt - self.steer_rate)
        self._steer_smooth = smooth

    def _low_slip(self, sample):
        slip = sample.tyre_side_slip
        if slip is None or len(slip) < 2 or not all(math.isfinite(v) for v in slip[:2]):
            return False
        return max(abs(slip[0]), abs(slip[1])) < self.SIDE_SLIP_LOW

    def _centring(self, t):
        """Latch the centring sign; True when the low-slip relation has turned against it (a mismatch)."""
        self.centring = r = self._centre_long.r()
        if self.centre_sign is None:
            if (r is not None and abs(r) >= self.CENTRE_LATCH and self.centre_time >= self.CENTRE_MIN_TIME
                    and self._centre_long.spread() >= self.CENTRE_SPREAD):
                self.centre_sign = 1.0 if r > 0 else -1.0
            return False
        now = self._centre_short.r()
        flipped = (now is not None and now * self.centre_sign <= -self.CENTRE_FLIP
                   and self._centre_short.spread() >= self.CENTRE_SPREAD)
        if not flipped:
            self._flip_since = None
            return False
        if self._flip_since is None:
            self._flip_since = t
        return t - self._flip_since >= self.MISMATCH_FOR

    def _judge(self, t):
        long = self._long
        ratio = long.ratio()
        if ratio is not None:
            low, high = self.SCALE_RANGE
            self.scale = min(max(ratio, low), high)
        self.fit, self.fit_now = long.r(), self._short.r()
        if self._centring(t):
            logging.info("Countersteer FFB: BeamNG: the game's centring turned against the learnt one at low slip "
                         "(r %.2f): learning again", self._centre_short.r() or 0.0)
            self._restart()
            self._clear_centre()
            self.waiting = 'mismatch'
            return
        if self.moving_time < self.MIN_TIME or long.spread() < self.MIN_SPREAD or self.fit is None:
            if not self.ready and self.waiting != 'mismatch':
                self.waiting = 'corners'
            return
        if self.ready:
            now = None if self.fit_now is None else self.fit_now * self.sign
            bad = self._short.spread() >= self.SHORT_SPREAD and (now is None or now < self.KEEP_FIT)
            if not bad:
                self._bad_since = None
            elif self._bad_since is None:
                self._bad_since = t
            elif t - self._bad_since >= self.MISMATCH_FOR:
                logging.info("Countersteer FFB: BeamNG: the game's force stopped following the rack (r %.2f)",
                             self.fit_now or 0.0)
                self._restart()
                self.waiting = 'mismatch'
        elif abs(self.fit) >= self.READY_FIT and self.centre_sign is not None:
            self.sign = 1.0 if self.fit > 0 else -1.0
            self.ready, self.waiting, self._bad_since = True, None, None
        elif self.waiting != 'mismatch' or abs(self.fit) >= self.READY_FIT:
            if abs(self.fit) < self.READY_FIT:
                self.waiting = 'fit'
            else:
                self.waiting = 'no-centring' if self.moving_time >= self.CENTRE_NOTE else 'centring'

    def force(self, sample):
        x = sample.rack_force if sample is not None else None
        sign, scale, _centre = self._playing()
        if x is None or sign is None:
            return 0
        return min(max(sign * scale * x, -FULL), FULL)

    def softlock(self, sample):
        """The soft lock's push (ext_force units; 0 inside SOFTLOCK_FROM, standing, or before the centring is
        latched)."""
        steer = getattr(sample, 'steer_input', None) if sample is not None else None
        centre = self._playing()[2]
        if steer is None or centre is None:
            return 0.0
        depth = (abs(steer) - self.SOFTLOCK_FROM) / (1.0 - self.SOFTLOCK_FROM)
        if depth <= 0.0:
            return 0.0
        depth = min(depth, 1.0)
        side = math.copysign(1.0, steer)
        push = depth * depth * (3.0 - 2.0 * depth) * self.SOFTLOCK
        outward = max(self.steer_rate * side, 0.0)          # moving further into the lock
        push += min(self.SOFTLOCK_DAMP * outward, self.SOFTLOCK) * depth
        gate = min(abs(sample.speed or 0.0) / self.SOFTLOCK_SPEED, 1.0)
        return centre * side * min(push, self.SOFTLOCK_MAX) * gate * FULL


class RbrRack(RackForce):
    """Richard Burns Rally through the same chain as ACR: the column torque NGP computes, recovered from the game's
    raw level, goes through the boost curve (driver_from_column, assist class 1.0: hydraulic), the hand-force
    mapping (rho, knee, budget, RIM_SAFE_NM, the reverse limit: RackForce.force, unchanged), the column damping
    and lock stop and the texture (the engine plays those).

    The column torque (analysis of NGP's curve, ~/.cache/ffb-rbr-nm): x = |level| / FULL at the game's STRENGTH 1,
    u = -ln(1 - x) = |T| * 100 * (-ln 0.975) / S, and NGP's T is the rack force times the rack ratio in metres per
    wheel turn, so the column torque is T / 2 pi: tau = 0.062862 * u * S Nm, signed like the level (ext_force follows the
    level's sign; `set_level`). S is RbrForce's (the car's ForceFeedbackSensitivity_NGP times the ini's surface %),
    100 with a note when it is not known.

    Not used here: the fit to the game's force (ready is the sign latch alone, the upload rate being the
    classifier's), the trail and scrub learner, the assist detector, Drive pull and Kickback (off
    for RBR: no tyre forces). The aligning spring (RackForce.align, played through the driver as ACR's) works from a
    kinematic front slip angle (`_slip`: the telemetry has no tyre slip angles), from the car table's ratio and lock.

    The sign latch is BeamNG's centring latch: on samples with little lateral acceleration
    at speed r(level, steer) is negative when the game's DIRECTION is normal; latched after SIGN_TIME s of them at
    |r| >= SIGN_LATCH, a positive r is an inverted DIRECTION, refused ('inverse')."""

    LEARNS = False
    cut_hz = float(OVERALL_RBR_CUT)      # the column torque's low-pass corner (Hz); None: off (RBR_CUT_OFF and above)
    K_COLUMN = 0.062862                  # Nm per unit of u * S: 1 / (100 * -ln 0.975 * 2 pi)
    S_DEFAULT = 100.0                    # S when the car is not in the table (WRC 2017-21)
    SIGN_AY = 1.5                        # m/s^2: lateral acceleration under which the latch looks
    SIGN_SPEED = 5.0                     # m/s
    SIGN_TIME = 2.0                      # s of such samples
    SIGN_LATCH = 0.5                     # |r| to latch at
    CUT_GAP = 0.1                        # s: a longer pause between levels starts the low-pass again at the current value
    SIGN_SPREAD = 0.02                   # ... and the steering input's spread (of -1..1) under it: a straight's noise
                                         # alone never latches
    FLOOR_MASS = 1390.0                  # kg: the caster floor's car mass when the table has none
    FLOOR_AY = 15.0                      # m/s^2: the most lateral force the floor believes: an impact's spike is not grip
    FLOOR_BETA = (0.05, 0.15)            # rad of body slip: the floor fades in over this ...
    FLOOR_SPEED = (5.0, 8.0)             # ... and over this speed (m/s)
    FLOOR_BIG = (0.40, 0.80, 0.5)        # rad, rad, share: a big slide's fade (ACR's played force over Newton's Fy_f falls like this past 0.4 rad)
    FLOOR_SLOW_HZ = 40.0                 # the column torque's low-pass the floor is held against: quick enough to catch NGP's sudden falls
    FLOOR_LIFT_HZ = 40.0                 # the lift's low-pass: it cannot step
    FLOOR_OPPOSE = (-3.0, 0.0)           # Nm of the column torque along the floor's sign: the lift fades out over this
    FLOOR_FY_MIN = 100.0                 # N: under this |Fy_f| the floor's sign is kept
    YAW_GAP = 0.1                        # s: yaw-rate samples further apart than this give no rdot
    floor_level = 1.0                    # "Caster in slides" (0..1): 0 = off
    ALIGN_K0 = 2150.0                    # Nm/rad: the kingpin torque per front slip angle until learnt (150 Nm/rad at the column x ratio 14.3, measured)
    ALIGN_FIT_SLIP = 0.2                 # rad: the stiffness is learnt on front slip angles under this (the kinematic slip is not small-signal)
    ALIGN_SPEED = 5.0                    # m/s: under it the kinematic slip angle is not defined
    ALIGN_FRESH = 0.25                   # s: the spring plays on telemetry no older than this
    spring_on = OVERALL_RBR_SPRING       # the aligning spring (profile INI only: countersteer_rbr_spring)
    source = OVERALL_RBR_SOURCE          # 'derived': the kingpin torque from the physics; 'ngp': the game's force (profile INI only: countersteer_rbr_source)
    # derived: Newton's front lateral force through the kingpin torque (ffb_forza), the column torque -(RALLY_TRAIL + tp(s)) Fy_f / ratio
    # Smoothing, measured by scripts/ffb-noise-match.py (RBR 20261104-143327 against ACR 20261104-143823, same car, speed over 10 m/s,
    # m/s^2 RMS of each term): a_y 3-15 Hz RBR .536 / ACR .353 (1.52), 15-30 Hz .248 / .469 (.53); a rdot 3-15 Hz .467 / .452 (1.03),
    # 15-30 Hz .342 / .694 (.49). Only a_y's 3-15 Hz is over (the 0.5-3 Hz band is 1.60 over too: that drive was harder, relative
    # to it the band is .95); 1.0 is not reachable inside 15 ms (a second-order corner of 5.3 Hz would need 42 ms), so a_y takes
    # the corner with the most reach inside 15 ms (second order, 16.1 Hz, 14.0 ms: 1.46) and a rdot needs none
    DERIVED_RATIO = 13.0                 # the derived torque's ratio without the car's (the table's wins: ACR learns 9.87 for the Fabia, RBR's table 10.58): the level the owner liked (drive 20261104-153256 ran here through a bug; the Fabia's table 10.58 plays light cornering 19 % heavier, further from ACR). The spring keeps the table's frame
    # (DERIVED_PK and DERIVED_SLOPE_MIN, replayed on 20261104-160408: at 0.08 and 0.1 the spring sat at its limit 74 % of the time at 0.11-0.13 FS, a flat square force; now 25 %, and 0.085 light, 0.026-0.029 hard and sliding, against ACR's 0.068 and 0.032-0.047)
    DERIVED_TP_B = 0.035                 # m: the pneumatic trail at no slip at a grip of 1
    DERIVED_TP_N = 4.0                   # ... and its power of the grip
    DERIVED_MU0 = 0.9                    # the fronts' grip before they have shown any
    DERIVED_MU_RANGE = (0.6, 1.4)
    DERIVED_MU_RISE = 0.5                # s
    DERIVED_MU_FALL = 60.0               # s
    DERIVED_R_TAU = 0.15                 # s: Fy_f / Fz's smoothing
    DERIVED_AY_HZ = 30.0                 # Hz, second order: a_y's low-pass (ffb_forza.LowPass)
    DERIVED_RDOT_HZ = None               # Hz: the yaw acceleration is unfiltered
    # Slip and spring shape, from RBR 20261104-143327 (speed over 10 m/s, _slip bins: median |a_y| 4.0 m/s^2 at .08-.1 rad, 6.1 at
    # .15-.2, 6.8 at .3-.5): the normalised slip is 1 where the lateral acceleration has all but stopped growing; the kingpin torque
    # against alpha / (1 + alpha / PK) x the trail share fits best (K 4800 Nm/rad, r .72 per sample: the kinematic slip is noisy)
    # at PK .08 (the NGP shape's ALIGN_PK .04 gives K 7600 at r .68)
    DERIVED_SLIP_PEAK = 0.15             # rad of front slip at which the normalised slip is 1 (Forza's 1.0 is the grip limit)
    DERIVED_PK = 0.04                    # rad: the saturating shape's knee (ALIGN_PK's role)
    DERIVED_SPEED = (0.5, 1.0)           # x ALIGN_SPEED: the derived torque fades in over this speed
    DERIVED_UNLOADED = 0.10              # share of the fronts' static load (m b/L g) under which they are in the air ...
    DERIVED_UNLOADED_S = 0.05            # ... and the torque fades out within this long (and back in)
    DERIVED_ALIGN_K0 = 4800.0            # Nm/rad: the derived kingpin torque per _sat shape until learnt
    DERIVED_SPRING_PAST = 0.2            # rad of front slip past which the spring's slope floor falls away
    DERIVED_SLOPE_MIN = 0.01              # the spring's k keeps this share of the small-signal slope past the derived shape's peak

    @property
    def pull_on(self):
        return False

    @pull_on.setter
    def pull_on(self, value):
        pass                             # (no Drive pull for RBR: no tyre forces)

    @property
    def kick_on(self):
        return False

    @kick_on.setter
    def kick_on(self, value):
        pass                             # (no Kickback either)

    def reset(self, car=None):
        super().reset(car)
        self._frame_car = car
        self._table_frame(car)
        self.assist = self.ASSIST
        self.column = None               # the column torque (Nm, signed like the level, low-passed), None without a level
        self.column_raw = None           # ... and before the low-pass
        self._cut = None                 # the low-pass's state (y, dy/dt) and the time of its last input
        self._cut_at = None
        self._fed_at = None              # when the last telemetry sample came
        self.s = None
        self.s_note = None
        self._tau_q = ffb_carprops.Quantile()
        self._tau_at = None
        self.direction = None
        self.play_t = None               # the engine's clock at the force pass (derived: telemetry older than ALIGN_FRESH fades out)
        self._restart_derived()
        self._restart_floor()
        self.restart_sign()
        self.direction = None            # 'normal' or 'inverse' once latched: the game's DIRECTION, kept through cars

    def _table_frame(self, car):
        """The car table's steering ratio and, with it, the road wheels' lock (radians(steering wheel lock) / ratio): the
        aligning spring's frame, known only where the table has them (not stored in the learnt car store)."""
        entry = car_data.entry(car) if car is not None else None
        table = ffb_carprops.table_ratio(entry)
        self.ratio, self.ratio_known = table or self.STEER_RATIO, bool(table)
        self.lock, self.lock_known = (math.radians(entry['steering_wheel_lock_deg']) / table, True) if table \
            else (self.ALIGN_LOCK, False)

    def restart_sign(self):
        """The sign is judged again (a new stream: the game's DIRECTION may have been changed in its menus), and the
        low-pass starts again at the next value. The aligning spring starts again too (its mirror, reset watch)."""
        self._cut = self._cut_at = None
        self._spring.reset()
        self._spring_at = self._align_prev = self._align_hold = None
        self._restart_floor()
        prior, self.direction = self.direction, None
        self._sign = _Moments(self.TAU)
        self.sign_time = 0.0
        self.sign_r = None
        self.refused = None
        keep = prior == 'normal'         # (judged again, but a latched 'normal' goes on playing meanwhile)
        self.waiting = None if keep else 'direction'
        self.ready = keep
        self._sign_at = None

    def set_car(self, car):
        """The telemetry's car key (it may narrow): the table ratio and lock (the aligning spring's frame)."""
        self.car = car
        if car != self._frame_car:       # (self.car alone misses it: field, ratio_known False all drive with the car already set)
            self._frame_car = car
            self._table_frame(car)

    def set_level(self, t, level, s, strength=1.0):
        """The game's raw level now (None: none fresh), S (None: not known) and the game's STRENGTH (ffb_rbr.StrengthFit,
        1 until fitted): the column torque."""
        self.s_note = None if s is not None else _("the car's sensitivity is not known: 100 assumed")
        if level is None:
            self.column = self.column_raw = self._cut = self._cut_at = None
            self.floor_lift = 0.0
            self._slow = self._lift_at = None
            return
        x = min(abs(level) / (FULL * strength), 1.0)
        raw = math.copysign(self.K_COLUMN * ffb_rbr.linear(x) * (self.S_DEFAULT if s is None else s), level) \
            if level else 0.0
        self.column_raw = raw
        self.column = self._low_pass(t, raw)
        last, self._tau_at = self._tau_at, t
        if last is not None:
            self._tau_q.add(abs(raw), min(max(t - last, 0.0), 0.05))

    def _low_pass(self, t, u):
        """The column torque through a second-order Butterworth low-pass at cut_hz, exact for an input held over each
        (irregular) step dt: y'' + 2 z w y' + w^2 y = w^2 u, z = 1/sqrt 2, solved in closed form so any dt is stable.
        A gap over CUT_GAP s (or a restart) starts it again at u, with no step. Off: u."""
        cut = self.cut_hz
        if cut is None or cut >= RBR_CUT_OFF:
            self._cut = self._cut_at = None
            return u
        last, self._cut_at = self._cut_at, t
        state = self._cut
        dt = 0.0 if last is None else t - last
        if state is None or dt < 0.0 or dt > self.CUT_GAP:
            self._cut = (u, 0.0)
            return u
        if dt == 0.0:
            return state[0]
        w = 2.0 * math.pi * cut
        a = w / math.sqrt(2.0)                    # z w
        wd = a                                    # w sqrt(1 - z^2): the same at z = 1/sqrt 2
        decay = math.exp(-a * dt)
        c, sn = math.cos(wd * dt), math.sin(wd * dt)
        e0, v0 = state[0] - u, state[1]
        b = (v0 + a * e0) / wd
        y = u + decay * (e0 * c + b * sn)
        v = decay * (v0 * c - (w * w * e0 + a * v0) / wd * sn)
        self._cut = (y, v)
        return y

    def tau_p99(self):
        """The p99 of |column torque| so far (Nm), None before any."""
        return self._tau_q.value()

    def _restart_derived(self):
        """The derived torque's state starts again (a new stream, a new car): nothing played until a sample."""
        self.derived = None              # the column torque of the physics now (Nm, signed like the level), faded by speed and load; None before a sample
        self.derived_raw = None          # ... before the fades (the aligning stiffness is learnt from it)
        self.fy_f = 0.0                  # Newton's front lateral force (N, left positive) of the last sample
        self._gate = 1.0                 # the fronts' load fade (1: loaded)
        self._gate_at = None
        self._derived_fade = 0.0         # the speed fade
        self._d_ay = self._d_rd = None   # the low-passes of a_y and of the yaw acceleration

    def _restart_floor(self):
        """The caster floor's state starts again (a new stream, a new car, no level): no lift."""
        self.grip_mu, self._grip_r = self.DERIVED_MU0, 0.0      # (the derived path's grip: a new stream starts from the prior)
        self.floor_lift = 0.0            # the lift added to the column torque (Nm, signed like it)
        self.floor_sign = None           # the floor's sign: opposite Fy_f's, kept through a small Fy_f
        self._slow = None                # the column torque's slow low-pass
        self._lift_at = None
        self._yaw = collections.deque(maxlen=3)         # (game time, yaw rate)
        self._rdot = 0.0

    @staticmethod
    def _ramp(x, lo_hi):
        lo, hi = lo_hi
        return min(max((x - lo) / (hi - lo), 0.0), 1.0)

    def _track_yaw(self, t, sample):
        """The yaw acceleration, the central difference of the yaw rate over the game's clock (ForzaRack's); None without a yaw rate."""
        r = getattr(sample, 'yaw_rate', None)
        if not ffb_forza.finite(r):
            self._yaw.clear()
            self._rdot = 0.0
            return None
        game = getattr(sample, 'game_time', None)
        self._yaw.append((game if ffb_forza.finite(game) else t, r))
        self._rdot = 0.0
        if len(self._yaw) == 3:
            (t0, r0), _mid, (t2, r2) = self._yaw
            if 0.0 < t2 - t0 <= 2.0 * self.YAW_GAP:
                self._rdot = (r2 - r0) / (t2 - t0)
        return self._rdot

    def _floor_size(self, t, sample):
        """What mechanical caster alone gives at the column (Nm) in a slide, 0 in grip or without the fields; sets the floor's sign.
        floor = level w RALLY_TRAIL |Fy_f| / ratio, w the body slip's and the speed's ramps; the column torque has the sign opposite Fy_f."""
        self._feed_grip(t, sample)
        rdot = self._track_yaw(t, sample)
        accel, vel = getattr(sample, 'accel', None), getattr(sample, 'vel', None)
        speed, drivetrain = getattr(sample, 'speed', None), getattr(sample, 'drivetrain', None)
        if rdot is None or accel is None or len(accel) < 2 or vel is None or len(vel) < 2 or \
                not all(ffb_forza.finite(v) for v in (accel[1], vel[0], vel[1], speed)):
            return 0.0
        entry = car_data.entry(self.car)
        mass = (entry.get('mass_kg') if entry else None) or self.FLOOR_MASS
        fy = ffb_forza.front_lateral(mass, ffb_forza.front_share(drivetrain), accel[1], rdot,
                                     factor=ffb_forza.lateral_factor(self._grip.mu()))
        limit = ffb_forza.front_share(drivetrain) * mass * self.FLOOR_AY
        fy = min(max(fy, -limit), limit)
        if abs(fy) >= self.FLOOR_FY_MIN:
            self.floor_sign = -math.copysign(1.0, fy)
        beta = abs(math.atan2(vel[1], abs(vel[0])))
        w = self._ramp(beta, self.FLOOR_BETA) * self._ramp(speed, self.FLOOR_SPEED)
        w *= 1.0 - (1.0 - self.FLOOR_BIG[2]) * self._ramp(beta, self.FLOOR_BIG[:2])
        return self.floor_level * w * ffb_carprops.RALLY_TRAIL * abs(fy) / self.ratio

    def _floor_step(self, t, sample):
        """The lift: the floor less the column torque's slow part (along the floor's sign, never negative), through a
        low-pass (FLOOR_LIFT_HZ). It is held signed, so a change of sign moves it across without a step."""
        size = self._floor_size(t, sample) if sample is not None else 0.0
        if self.column is None:
            self.floor_lift, self._slow, self._lift_at = 0.0, None, None
            return
        last, self._lift_at = self._lift_at, t
        dt = 0.0 if last is None else t - last
        if self._slow is None or dt < 0.0 or dt > self.CUT_GAP:
            self._slow, self.floor_lift = self.column, 0.0       # again from here, no step
            dt = 0.0
        elif dt > 0.0:
            self._slow += (1.0 - math.exp(-2.0 * math.pi * self.FLOOR_SLOW_HZ * dt)) * (self.column - self._slow)
        sign = self.floor_sign
        # never against NGP: its force the other way fades the lift out, so the two cannot cancel to nothing
        along = 0.0 if sign is None else sign * self._slow
        target = 0.0 if sign is None else sign * max(0.0, size - along) * self._ramp(along, self.FLOOR_OPPOSE)
        if dt > 0.0:
            self.floor_lift += (1.0 - math.exp(-2.0 * math.pi * self.FLOOR_LIFT_HZ * dt)) * (target - self.floor_lift)
            if target == 0.0 and abs(self.floor_lift) < 1e-6:
                self.floor_lift = 0.0                            # (grip: back to exactly the column torque)

    def feed(self, t, sample, level, new=True):
        """A telemetry sample with the game's raw level now (None if none fresh): the caster floor, the sign latch."""
        last, self._sign_at = self._sign_at, t
        derived = self.source == 'derived'
        if sample is not None:
            self._now = self._fed_at = t
            self._engine(t, sample)
            if derived:
                self._derive(t, sample)
        if new and not derived:
            self._floor_step(t, sample)
        column = self.derived_raw if derived else self.column
        if sample is not None and column is not None and self.ratio_known and self.lock_known and last is not None:
            alpha = self._slip(sample)
            if alpha is not None and abs(alpha) < self.ALIGN_FIT_SLIP and 0.0 < t - last:
                # the column torque without the floor's lift, at the kingpin (the ACR path's mz): the stiffness is learnt per drive
                self._align.add(self._sat(alpha), column * self.ratio, min(t - last, 0.1))
        if self.direction is not None or level is None or sample is None:
            return
        dt = 0.0 if last is None else min(max(t - last, 0.0), 0.1)
        accel = getattr(sample, 'accel', None)
        ay = accel[1] if accel is not None and len(accel) > 1 else None
        steer, speed = getattr(sample, 'steer', None), getattr(sample, 'speed', None)
        if not all(v is not None and math.isfinite(v) for v in (ay, steer, speed)):
            return
        if speed <= self.SIGN_SPEED or abs(ay) >= self.SIGN_AY or dt <= 0.0:
            return
        self._sign.add(steer, level, dt)
        self.sign_time += dt
        r = self.sign_r = self._sign.r()
        if self.sign_time >= self.SIGN_TIME and not self.ready and self.refused is None:
            # the generic prior: DIRECTION normal, playing within seconds; judged on until it latches either way, and an
            # inverted one is refused as soon as it shows (a 20 % STRENGTH can stay undecided for a whole drive)
            self.ready, self.waiting = True, None
        if self.sign_time < self.SIGN_TIME or r is None or abs(r) < self.SIGN_LATCH:
            return
        spread = self._sign.spread()
        if spread is None or spread < self.SIGN_SPREAD:
            return
        if r < 0.0:
            self.direction, self.ready, self.waiting, self.refused = 'normal', True, None, None
        else:
            self.direction, self.ready, self.waiting, self.refused = 'inverse', False, None, 'inverse'
            logging.warning("Countersteer FFB: RBR: the game's force follows the steering the wrong way round (r %.2f): "
                            "its DIRECTION is inverted; set it normal", r)

    def _filtered(self, which, t, x):
        """x through the derived path's low-pass `which` ('ay' or 'rdot'), built from the class constants when first used."""
        hz = self.DERIVED_AY_HZ if which == 'ay' else self.DERIVED_RDOT_HZ
        if hz is None:
            return x
        attr = '_d_ay' if which == 'ay' else '_d_rd'
        filt = getattr(self, attr)
        if filt is None or filt.hz != hz:
            filt = ffb_forza.LowPass(hz, 2, self.CUT_GAP)
            setattr(self, attr, filt)
        return filt.step(t, x)

    def _derive(self, t, sample):
        """The column torque of the physics (derived): Newton's front lateral force Fy_f from the lateral acceleration and the
        yaw acceleration (ffb_forza.front_lateral, the car table's mass and b/L, capped as the caster floor caps it), through
        the kingpin torque with the rally caster and the pneumatic trail at the kinematic slip (normalised by DERIVED_SLIP_PEAK),
        over the steering ratio. Faded to 0 by speed (under ALIGN_SPEED the slip is not defined), and out within DERIVED_UNLOADED_S
        when the fronts carry under DERIVED_UNLOADED of their static load (the air). Without the fields: the last value fades out."""
        self._feed_grip(t, sample)
        rdot = self._track_yaw(t, sample)
        accel, speed = getattr(sample, 'accel', None), getattr(sample, 'speed', None)
        last, self._gate_at = self._gate_at, t
        dt = 0.0 if last is None else min(max(t - last, 0.0), 0.1)
        entry = car_data.entry(self.car)
        mass = (entry.get('mass_kg') if entry else None) or self.FLOOR_MASS
        bl = ffb_forza.front_share(getattr(sample, 'drivetrain', None))
        load = getattr(sample, 'tyre_load', None)
        loaded = True
        if load is not None and len(load) > 1 and ffb_forza.finite(load[0]) and ffb_forza.finite(load[1]):
            loaded = load[0] + load[1] >= self.DERIVED_UNLOADED * mass * bl * ffb_forza.G
        ok = rdot is not None and accel is not None and len(accel) > 1 and ffb_forza.finite(accel[1]) and ffb_forza.finite(speed)
        step = dt / self.DERIVED_UNLOADED_S
        self._gate = min(self._gate + step, 1.0) if ok and loaded else max(self._gate - step, 0.0)
        if ok:
            ay = self._filtered('ay', t, min(max(accel[1], -ffb_forza.AY_MAX), ffb_forza.AY_MAX))
            rd = self._filtered('rdot', t, min(max(rdot, -ffb_forza.RDOT_MAX), ffb_forza.RDOT_MAX))
            fy = ffb_forza.front_lateral(mass, bl, ay, rd, factor=ffb_forza.lateral_factor(self._grip.mu()))
            limit = bl * mass * self.FLOOR_AY
            self.fy_f = min(max(fy, -limit), limit)
            alpha = self._slip(sample)
            s = 0.0 if alpha is None else alpha / self.DERIVED_SLIP_PEAK
            fz = load[0] + load[1] if load is not None and len(load) > 1 and ffb_forza.finite(load[0]) \
                and ffb_forza.finite(load[1]) else bl * mass * ffb_forza.G
            # (light fronts over a crest say nothing about the grip: Fy_f / Fz would read the ceiling)
            tp = self._grip_trail(abs(self.fy_f), max(fz, 0.5 * bl * mass * ffb_forza.G), dt, speed) / (1.0 + (abs(s) / ffb_forza.TP_S_HALF) ** ffb_forza.TP_N)
            ratio = self.ratio if self.ratio_known else self.DERIVED_RATIO      # the car's own (table), else the prior
            self.derived_raw = -(ffb_carprops.RALLY_TRAIL + tp) * self.fy_f / ratio
            lo, hi = (self.DERIVED_SPEED[0] * self.ALIGN_SPEED, self.DERIVED_SPEED[1] * self.ALIGN_SPEED)
            self._derived_fade = self._ramp(speed, (lo, hi))
        elif self.derived_raw is None:
            return
        else:
            self._d_ay = self._d_rd = None               # (the low-passes start again at the next sample)
        self.derived = self.derived_raw * self._derived_fade * self._gate

    def _grip_trail(self, fy, fz, dt, speed):
        """The pneumatic trail at no slip (m), from the grip the fronts have shown, so nothing is told the surface: DERIVED_TP_B mu^DERIVED_TP_N,
        mu rising to the highest Fy_f / Fz within about half a second and falling back slowly, from a sane prior for the first
        corners. Replayed on 20261104-212111 it read 1.14 on tarmac and 0.85 on gravel; a brush tyre's (1 - grip in use) did not
        separate the surfaces, this does, as ACR's measured trail does (42-44 mm tarmac, 22-27 mm gravel)."""
        now = fy / fz
        self._grip_r += min(dt / self.DERIVED_R_TAU, 1.0) * (now - self._grip_r)
        if ffb_forza.finite(speed) and speed > 2.0 * self.ALIGN_SPEED:
            tau = self.DERIVED_MU_RISE if self._grip_r > self.grip_mu else self.DERIVED_MU_FALL
            self.grip_mu += min(dt / tau, 1.0) * (self._grip_r - self.grip_mu)
            self.grip_mu = min(max(self.grip_mu, self.DERIVED_MU_RANGE[0]), self.DERIVED_MU_RANGE[1])
        return self.DERIVED_TP_B * self.grip_mu ** self.DERIVED_TP_N

    def _fresh(self):
        """1 while the telemetry is no older than half of ALIGN_FRESH at this force pass, 0 at ALIGN_FRESH (a line between):
        a stream that stops fades out rather than holds. 1 when the engine has not set the pass's time (play_t)."""
        if self.play_t is None or self._fed_at is None:
            return 1.0
        return self._ramp(self.ALIGN_FRESH - (self.play_t - self._fed_at), (0.0, 0.5 * self.ALIGN_FRESH))

    def _sat(self, alpha):
        """The kingpin torque's shape in the slip angle. Derived: the saturating lateral force's shape times the kingpin torque's
        trail share at that slip, (RALLY_TRAIL + tp(|alpha| / DERIVED_SLIP_PEAK)) / (RALLY_TRAIL + tp(0)), so the aligning spring
        follows the torque the derived path plays (the pneumatic trail fades past the peak)."""
        if self.source != 'derived':
            return super()._sat(alpha)
        base = alpha / (1.0 + abs(alpha) / self.DERIVED_PK)
        trail = ffb_carprops.RALLY_TRAIL
        return base * (trail + ffb_forza.trail(alpha / self.DERIVED_SLIP_PEAK)) / (trail + ffb_forza.TP0)

    def _sat_div(self, alpha):
        """The spring's k divisor: the inverse slope of _sat (a central difference where derived), the slope held at
        DERIVED_SLOPE_MIN of its small-signal value where the fading trail would turn it over."""
        if self.source != 'derived':
            return super()._sat_div(alpha)
        h = 1e-4
        slope = (self._sat(alpha + h) - self._sat(alpha - h)) / (2.0 * h)
        # Past the tyre's peak the spring only fades: the floor falls with the slip, so its force never starts to load up again
        floor = self.DERIVED_SLOPE_MIN * min(1.0, (self.DERIVED_SPRING_PAST / max(abs(alpha), 1e-6)) ** 2)
        return 1.0 / max(slope, floor)

    def align_stiffness(self):
        """Kingpin torque per unit of _sat(slip): the derived path's prior until learnt (its torque is not NGP's)."""
        s = self._align.s
        if self.source == 'derived' and (s[0] < self.ALIGN_MIN_N or s[3] <= 0.0):
            return self.DERIVED_ALIGN_K0
        return super().align_stiffness()

    def _slip(self, sample):
        """The front slip angle (rad, positive when steered further left than the car travels, as ACR's) from the car's
        motion: steer x lock less atan2(vy + a r, |vx|), a the front axle's distance to the centre of mass; None without the
        fields or under ALIGN_SPEED. (RBR has no tyre slip angles.)"""
        steer, yaw, vel = getattr(sample, 'steer', None), getattr(sample, 'yaw_rate', None), getattr(sample, 'vel', None)
        speed = getattr(sample, 'speed', None)
        if vel is None or len(vel) < 2 or not all(ffb_forza.finite(v) for v in (steer, yaw, vel[0], vel[1], speed)) \
                or speed < self.ALIGN_SPEED:
            return None
        a = ffb_forza.WHEELBASE * (1.0 - ffb_forza.front_share(getattr(sample, 'drivetrain', None)))
        return steer * self.lock - math.atan2(vel[1] + a * yaw, abs(vel[0]))

    @staticmethod
    def _rear_slip(sample):
        """None: the ratio learner does not run for RBR."""
        return None

    def align_ready(self):
        """The spring may play: the car table's ratio and lock are known and the sign latch is through (the prior stiffness
        serves until the fit has enough), and the INI has not turned it off."""
        return bool(self.spring_on and self.ratio_known and self.lock_known and self.ready)

    def power_steering(self):
        """'off' (the column torque) or 'model' (ours): the game's force carries no assist to follow."""
        return 'off' if ffb_carprops.power_settings()[0] == 'off' else 'model'

    def rim_torque(self, sample, trail=None, scrub=None, ratio=None, assist=None):
        """The driver's torque at the rim for the column torque of the game's level (None without one)."""
        if self.source == 'derived':
            if self.derived is None:
                return None
            column = self.derived * self._fresh()
        elif self.column is None:
            return None
        speed = getattr(sample, 'speed', None)
        hyd = not self.classic()
        if self.source != 'derived':
            column = self.column + self.floor_lift       # (grip: the lift is exactly 0.0)
        if self.power_steering() == 'off':
            return ffb_carprops.driver_from_column(column, speed, 0.0, hyd)
        return ffb_carprops.driver_from_column(column, speed, self.ASSIST * self.assist_level(), hyd)


class ForzaRack(RackForce):
    """Forza Horizon through the same chain as ACR: the front kingpin torque from Newton (ffb_forza: Forza sends no
    tyre forces) goes through the boost curve (driver_from_column, assist class 1.0), the hand-force mapping (rho,
    knee, budget, RIM_SAFE_NM: RackForce.force, unchanged), and the engine plays the output filter (ffb_forza.OutputFilter),
    column damping, lock stop, texture and Kickback beside it.

    - Torque (`_front`): m (b/L) (a_y + DI a rdot) through -(0.027 + tp(s)), m the car's learnt mass (MassLearner, per car,
      ffb_carprops.STORE; 1400 kg until learnt), rdot the yaw rate's central difference over the game's clock (known one
      packet late: the difference is centred on the packet before the newest). A puddle under both fronts halves tp; one
      under a single front adds Drive pull's couple, scrub x PULL_FRACTION m_f g / 2 (only while Drive pull is on, as the
      scrub passed in is 0 otherwise).
    - Constants: ratio 14 (no lock table), the car's wheel FORZA_CAR_WHEEL_MM (the profile's countersteer_car_wheel_mm wins
      once the user has set one), assist 1.0, power steering always the model's. Nothing is learnt from the game's force
      (LEARNS False: no TorqueLearner, AssistDetector, game-shaped assist or fit gate).
    - Ready: the car moving (hysteresis) and known. The game's force, where fresh, is a safety check only: r(game_ffb_raw,
      torque) at REVERSED_FIT or less over TAU s refuses ('mismatch', the engine says 'forza-mismatch').
    - Kickback: the onset of a rumble strip under one front wheel, or of a puddle under one, is an event for ffb_kick with
      a synthetic step of Fx_FL - Fx_FR of KICK_FRACTION m_f g / 2 (the wheel on that side loses forward force); the pulse
      is added after the output filter (kick_force), so its edge stays sharp."""

    LEARNS = False
    STEER_RATIO = 14.0                   # without the wheel's range; with it, the ratio is the half range over FORZA_LOCK
    ALIGN_LOCK = 0.75                    # rad: Forza maps full steer to about 0.75 rad of road wheel (L r / v + slip, steady driving,
                                         # three cars, 0.73-0.76 at every speed and load measured); its learner never closes on Forza
    ASSIST = 1.0
    car_wheel_mm = FORZA_CAR_WHEEL_MM
    MOVING_ON = MIN_SPEED                # m/s to start ...
    MOVING_OFF = 0.3                     # ... and to stay
    YAW_GAP = 0.1                        # s: yaw-rate samples further apart than this give no rdot
    KICK_FRACTION = 0.3                  # of m_f g / 2: the synthetic step of Fx_FL - Fx_FR at a one-sided onset
    PULL_FRACTION = 0.15                 # of m_f g / 2: a one-sided puddle's sustained drag difference
    TRIGGER_HOLD = 0.1                   # s: an event not taken by Kickback in this long is dropped
    _pi = None                           # the tune's PI (the mass is kept per car and PI)
    MASS_SAVE_EVERY = 1.0                # s between hands of the learnt mass to the store
    PUDDLE_TP = 0.5                      # the pneumatic trail's share with a puddle under both fronts
    LOCK_SLEW = 0.02                     # rad/s: the road wheels' lock moves to the learnt one at this rate
    spring_on = OVERALL_FORZA_SPRING     # the aligning spring (profile INI only: countersteer_forza_spring)
    SLIP_RAD = -0.2                    # rad of kinematic front slip per unit of Forza's normalised slip angle (corr -0.93 over 2 drives)
    AY_LEVEL_S = 0.5                     # s: what is slower than this comes from a_y, what is faster from v r
    VR_GAIN = 0.8                        # a_y over v r, measured 0.78 on 20261104-230426
    YAW_WEIGHT = 1.0                     # Iz / (m a b): 0.95-1.05 from ACR's front and rear tyre forces separately (Fabia, Iz about 2100 kg m2)
    SLIP_SCALE = 0.5                     # Forza's normalised slip angle at which the tyre curve bends
    SLIP_GAIN0 = -13.5                   # m/s^2 of a_y per unit of it, before the car's own is learnt (measured 20261104-230426)
    SLIP_GAIN_S = 20.0                   # s: the regression's memory
    SLIP_ALIVE = 0.002                   # the slip's mean square under which it says nothing
    lead_on = False                      # the front slip's lead on Newton's Fy_f (ffb_forza.SlipLead): off, it moved steering to force by nothing on 20261104-220133

    @property
    def mass(self):
        return self.mass_l.mass

    def reset(self, car=None):
        stored = ffb_carprops.STORE.get_mass(car, self._pi) if car is not None else None
        self.mass_l = ffb_forza.MassLearner(stored)
        self._yaw = collections.deque(maxlen=3)         # (game time, yaw rate)
        self._lead = ffb_forza.SlipLead()
        self._ay_lp = self._fast_lp = self._ay_sample = self._ay_at = None
        self._ay_slip, self._gain_xy, self._gain_xx = False, 0.0, 0.0
        self._lead_fy = None
        self.rdot = 0.0
        self._sides = (0, 0)                            # the one-sided rumble / puddle now (+1 FL, -1 FR)
        self._trigger = None                            # (time, step of Fx_FL - Fx_FR)
        self._mass_saved = None
        self.moving = False
        super().reset(car)
        self.ratio, self.assist = self._mapped_ratio(), self.ASSIST
        self.ratio_known = True
        self.lock, self.lock_known = self.ALIGN_LOCK, True        # (the approximation plays at once; _learn_lock moves it)
        self.lock_learnt_at = None

    def _mapped_ratio(self):
        """The overall ratio as the hands feel it: the wheel's half range over the road wheels' lock (as ACR's is learnt from its
        wheel mapping); STEER_RATIO without the range."""
        if not self.wheel_range:
            return self.STEER_RATIO
        return min(max(math.radians(self.wheel_range / 2.0) / self.ALIGN_LOCK, ffb_carprops.RATIO_RANGE[0]), ffb_carprops.RATIO_RANGE[1])

    def _learn_lock(self, t, sample, dt):
        """The aligning spring's frame: the lock and ratio from the car's own motion (RackForce._learn_ratio: Ackermann
        steer-to-road-wheel angle at low lateral acceleration; Forza has no tyre slip angles in rad, no lock, no ratio
        table), the lock slewing from the start value at LOCK_SLEW rad/s so the spring does not step."""
        before = self.lock
        self._learn_ratio(sample, dt)
        learnt, self.lock = self.lock, before
        if learnt != before:
            if self.lock_learnt_at is None:
                self.lock_learnt_at = t
            self.lock += min(max(learnt - before, -self.LOCK_SLEW * dt), self.LOCK_SLEW * dt)
        self.ratio = self._mapped_ratio()          # the wheel's range can arrive or change after the reset

    def _restart(self):
        super()._restart()
        self._corr = _Moments(self.TAU)
        self.waiting = 'wait'

    def power_steering(self):
        return 'model'

    ALIGN_SPEED = RbrRack.ALIGN_SPEED
    ALIGN_FRESH = RbrRack.ALIGN_FRESH
    def _slip(self, sample):
        """The front slip angle (rad, ACR's sign) for the aligning spring: the game's own, on time (the car's velocity is 400 ms behind its force); the kinematic one without it."""
        sa = getattr(sample, 'slip_angle', None)
        if sa is not None and len(sa) > 1 and ffb_forza.finite(sa[0]) and ffb_forza.finite(sa[1]) and (sa[0] or sa[1]):
            return self.SLIP_RAD * 0.5 * (sa[0] + sa[1])
        return RbrRack._slip(self, sample)


    @staticmethod
    def _rear_slip(sample):
        return None

    def align_ready(self):
        """The spring may play: the INI has not turned it off and the car is ready (the ratio and lock start from
        STEER_RATIO and ALIGN_LOCK; the prior stiffness serves until the fit has enough)."""
        return bool(self.spring_on and self.ratio_known and self.lock_known and self.ready)

    def top_torque(self, ratio=None, assist=None):
        return ffb_carprops.hyd_top()

    def _both_puddles(self, sample):
        pud = sample.puddle
        return pud is not None and len(pud) > 1 and bool(pud[0]) and bool(pud[1])

    def _front(self, sample, trail=None, scrub=None):
        """Newton's kingpin torque (ffb_forza), None without the acceleration. `scrub`: Drive pull's radius (m)."""
        accel = sample.accel
        if accel is None or len(accel) < 2 or not ffb_forza.finite(accel[1]):
            return None
        bl = ffb_forza.front_share(sample.drivetrain)
        fy = ffb_forza.front_lateral(self.mass, bl, self._fast_ay(sample, accel[1]), self.rdot, di=self.YAW_WEIGHT,
                                     factor=ffb_forza.lateral_factor(self._grip.mu()))
        lead = self._lead_fy
        if lead is not None and lead[0] is sample:
            fy = lead[1]
        cs = sample.combined_slip
        s = 0.5 * (abs(cs[0]) + abs(cs[1])) if cs is not None and len(cs) > 1 and ffb_forza.finite(cs[0]) \
            and ffb_forza.finite(cs[1]) else 0.0
        torque = ffb_forza.kingpin(fy, s, self.PUDDLE_TP if self._both_puddles(sample) else 1.0)
        if scrub:
            pud = sample.puddle
            if pud is not None and len(pud) > 1 and bool(pud[0]) != bool(pud[1]):
                # the drag of the puddled side: Fx on that wheel falls, -scrub x (Fx_FL - Fx_FR) turns it into torque
                torque += math.copysign(scrub * self.PULL_FRACTION * self.mass * bl * ffb_forza.G / 2.0,
                                        1.0 if pud[0] else -1.0)
        return torque

    def _lead_update(self, t, sample):
        """Newton's Fy_f with the front slip's fast part (ffb_forza.SlipLead), once for this sample; _front plays it."""
        self._lead_fy = None
        accel, sa = sample.accel, sample.slip_angle
        if not self.lead_on or accel is None or len(accel) < 2 or not ffb_forza.finite(accel[1]):
            return
        slip = None
        if sa is not None and len(sa) > 1 and ffb_forza.finite(sa[0]) and ffb_forza.finite(sa[1]):
            slip = 0.5 * (sa[0] + sa[1])
        bl = ffb_forza.front_share(sample.drivetrain)
        fy = ffb_forza.front_lateral(self.mass, bl, accel[1], self.rdot, di=self.YAW_WEIGHT,
                                     factor=ffb_forza.lateral_factor(self._grip.mu()))
        self._lead_fy = (sample, self._lead.step(t, fy, slip))

    def _kicked(self, sample, share, ratio, assist):
        return share                     # (after the output filter: kick_force)

    def kick_force(self, sample):
        """Kickback's pulse alone, ext_force units (0 with it off)."""
        return FULL * self._kick.share(self, sample, 0.0, None, None)

    def kick_trigger(self, sample):
        event, self._trigger = self._trigger, None
        if event is None or self._now - event[0] > self.TRIGGER_HOLD:
            return None
        return event[1]

    def _track_yaw(self, t, sample):
        r = sample.yaw_rate
        if not ffb_forza.finite(r):
            self._yaw.clear()
            self.rdot = 0.0
            return
        self._yaw.append((sample.game_time if ffb_forza.finite(sample.game_time) else t, r))
        if len(self._yaw) < 3:
            self.rdot = 0.0
            return
        (t0, r0), (_t1, _r1), (t2, r2) = self._yaw
        if 0.0 < t2 - t0 <= 2.0 * self.YAW_GAP:
            self.rdot = (r2 - r0) / (t2 - t0)     # centred on the middle packet
        else:
            self.rdot = 0.0

    def _fast_ay(self, sample, ay):
        """The lateral acceleration on time. A scan of every value in FH6's packet against its own force: a_y is 170 ms behind, the
        yaw rate 110, the front slip angle 17, the earliest with a real correlation. So a_y keeps the level and the front slip gives
        the changes: a_y over a half second + gain (f - f over a half second), f = tanh(front slip / 0.5), the gain the regression
        of a_y on f, learnt per car from a prior. Without slip angles: the changes from speed x yaw rate. No surface is told."""
        sa, r, v, g = sample.slip_angle, sample.yaw_rate, sample.speed, sample.game_time
        slip = sa is not None and len(sa) > 1 and ffb_forza.finite(sa[0]) and ffb_forza.finite(sa[1])
        if slip:
            fast, gain = math.tanh(0.5 * (sa[0] + sa[1]) / self.SLIP_SCALE), None
        elif ffb_forza.finite(r) and ffb_forza.finite(v):
            fast, gain = v * r, self.VR_GAIN
        else:
            self._ay_lp = None
            return ay
        if self._ay_lp is None or slip != self._ay_slip:
            self._ay_lp, self._fast_lp, self._ay_sample, self._ay_at, self._ay_slip = ay, fast, sample, g, slip
        elif sample is not self._ay_sample:
            dt = g - self._ay_at if ffb_forza.finite(g) and ffb_forza.finite(self._ay_at) else 1.0 / 60.0
            dt = min(max(dt, 0.0), 0.1)
            k = 1.0 - math.exp(-dt / self.AY_LEVEL_S)
            self._ay_lp += k * (ay - self._ay_lp)
            self._fast_lp += k * (fast - self._fast_lp)
            self._ay_sample, self._ay_at = sample, g
            if slip:
                k = 1.0 - math.exp(-dt / self.SLIP_GAIN_S)
                self._gain_xy += k * (ay * fast - self._gain_xy)
                self._gain_xx += k * (fast * fast - self._gain_xx)
        if gain is None:
            if self._gain_xx <= self.SLIP_ALIVE:
                return ay                        # the slip has not moved yet: nothing to take the changes from, a_y as it comes
            gain = min(max(self._gain_xy / self._gain_xx, -2.0 * abs(self.SLIP_GAIN0)), 0.0)
        return self._ay_lp + gain * (fast - self._fast_lp)

    def _events(self, t, sample, bl):
        """The onset of a rumble strip under one front wheel, or of a puddle under one: an event for Kickback."""
        def side(flags):
            if flags is None or len(flags) < 2:
                return 0
            a, b = bool(flags[0]), bool(flags[1])
            return 1 if a and not b else -1 if b and not a else 0
        sides = (side(sample.rumble), side(sample.puddle))
        for now, before in zip(sides, self._sides):
            if now and now != before:
                step = -self.KICK_FRACTION * self.mass * bl * ffb_forza.G / 2.0 * now
                self._trigger = (self._now, step)
        self._sides = sides

    def feed(self, t, sample, raw, new=True):
        """A telemetry sample with the game's raw level now (None if none fresh): the mass, the yaw acceleration, the
        events, the safety check and readiness."""
        pi = ffb_forza.power_index(sample)
        if sample.car != self.car:
            self._pi = pi
            self.reset(sample.car)
        elif pi is not None and pi != self._pi:         # a tune edit: the mass is the new tune's (the car's until it has one)
            self._pi = pi
            stored = ffb_carprops.STORE.get_mass(self.car, pi)
            self.mass_l = ffb_forza.MassLearner(stored or (self.mass_l.mass if self.mass_l.known else None))
            self._mass_saved = None
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 1.0)
        self._last = self._now = t
        self._engine(t, sample)
        speed = sample.speed if ffb_forza.finite(sample.speed) else 0.0
        self.moving = speed > (self.MOVING_OFF if self.moving else self.MOVING_ON)
        self._feed_grip(t, sample)
        self._track_yaw(t, sample)
        self.mass_l.add(t, sample)
        moved, self.mass_l.moved = self.mass_l.moved, None
        if moved is not None and self.car is not None:
            logging.info("Countersteer FFB: %s (PI %s) mass %.0f -> %.0f kg (estimate %.0f)", self.car, self._pi, *moved)
            self._mass_saved = t
            ffb_carprops.STORE.set_mass(self.car, self.mass, self._pi)
        elif self.car is not None and self.mass_l.accepted and not self.mass_l.holding \
                and (self._mass_saved is None or t - self._mass_saved >= self.MASS_SAVE_EVERY):
            self._mass_saved = t
            ffb_carprops.STORE.set_mass(self.car, self.mass, self._pi)
        self._events(t, sample, ffb_forza.front_share(sample.drivetrain))
        self._lead_update(t, sample)
        if self.car is not None and self.moving:
            self._learn_lock(t, sample, dt)
        torque = self._front(sample, self.trail, 0.0)
        if raw is not None and torque is not None and speed > MIN_SPEED and dt > 0.0:
            self.moving_time += dt
            self._corr.add(torque, raw, dt, abs(raw) < self.RAW_CLIPPED)
        alpha = self._slip(sample)
        if torque is not None and alpha is not None and dt > 0.0 and abs(alpha) < self.ALIGN_FIT_ALPHA and speed > MIN_SPEED:
            self._align.add(self._sat(alpha), torque, dt)       # (the spring's stiffness, as RackForce.feed)
        corr = self._corr
        self.fit = corr.r()
        self.refused = 'mismatch' if (self.moving_time >= self.MIN_TIME and corr.spread() >= self.MIN_SPREAD
                                      and self.fit is not None and self.fit <= self.REVERSED_FIT) else None
        self.ready = bool(self.moving and self.car is not None and not self.refused)
        self.waiting = None if self.ready else 'mismatch' if self.refused else 'wait'


class _CsbRate:
    """CSB1's rate in packets per second of the game's clock (its seq over its game_time, so the bursts of a
    frame do not count against it) and whether that clock is moving."""

    MOVING = 0.1                         # s: the game's clock moved within this, or it is paused

    def __init__(self):
        self.clear()

    def clear(self):
        self._items = collections.deque()    # (t, seq, game_time)
        self._moved_at = None
        self.rate = None
        self.rack = False

    def feed(self, t, sample):
        items = self._items
        gt, seq = sample.game_time, sample.packet_id
        if gt is None or seq is None:
            return
        if items and (gt < items[-1][2] or seq < items[-1][1]):
            items.clear()                    # a reset, a new vehicle (the mod's seq starts again)
        if items and gt > items[-1][2]:
            self._moved_at = t
        items.append((t, seq, gt))
        while items and items[0][0] < t - WINDOW:
            items.popleft()
        self.rack = sample.rack_force is not None
        span = items[-1][2] - items[0][2]
        self.rate = (items[-1][1] - items[0][1]) / span if span >= 0.2 else None

    def advancing(self, now):
        return self._moved_at is not None and now - self._moved_at <= self.MOVING


class _Wake(threading.Event):
    """The engine's wake: an Event that also writes a pipe once wait_fd is asked for, so one poll() can wait on
    it and on game_ffb_raw's fd together."""

    def __init__(self):
        super().__init__()
        self._pipe = None

    def pipe(self):
        """The read end, created on first use; readable while the Event is set."""
        if self._pipe is None:
            r, w = os.pipe()
            os.set_blocking(r, False)
            os.set_blocking(w, False)
            self._pipe = (r, w)
            if self.is_set():
                self._poke()
        return self._pipe[0]

    def _poke(self):
        try:
            os.write(self._pipe[1], b'x')
        except OSError:
            pass                         # full: already readable

    def set(self):
        super().set()
        if self._pipe is not None:
            self._poke()

    def clear(self):
        super().clear()
        if self._pipe is not None:
            try:
                os.read(self._pipe[0], 512)
            except OSError:
                pass

    def close(self):
        pipe, self._pipe = self._pipe, None
        for fd in pipe or ():
            os.close(fd)


class RangeState:
    """What full range changed on one wheel: ext_force_cap and master gain, each (original, ours) or None where it
    was left alone, and the gain the driver should show now."""
    __slots__ = ('cap_orig', 'cap_ours', 'gain_orig', 'gain_ours', 'gain_now')

    def __init__(self, cap_orig, cap_ours, gain_orig, gain_ours, gain_now):
        self.cap_orig, self.cap_ours, self.gain_orig, self.gain_ours, self.gain_now = \
            cap_orig, cap_ours, gain_orig, gain_ours, gain_now

    def wire(self):
        return (self.cap_orig, self.cap_ours, self.gain_orig, self.gain_ours)


def apply_restore(driver, state):
    """The originals back, each only where the driver still shows ours (a value changed meanwhile is the user's).
    `state`: a RangeState or its wire tuple. Failures are logged, not raised."""
    cap_orig, cap_ours, gain_orig, gain_ours = state.wire() if isinstance(state, RangeState) else state
    for name, orig, ours, read, write in (
            ('ext_force_cap', cap_orig, cap_ours, driver.ext_force_cap, driver.set_ext_force_cap),
            ('gain', gain_orig, gain_ours, driver.master_gain, driver.set_master_gain)):
        if orig is None:
            continue
        try:
            if read() == ours:
                write(orig)
        except OSError as e:
            logging.warning("Countersteer FFB: full range: restoring %s: %s", name, e)


class DriverReplace:
    """The driver's replace-mode attributes of one wheel (its sysfs
    directory): ext_force and game_const_scale each through one file kept
    open and written at offset 0, game_ffb_raw through one kept open and
    read at offset 0, game_ffb_level opened for each (rare) write."""

    def __init__(self, path):
        self.path = path
        self._fd = None
        self._raw_fd = None
        self._scale_fd = None
        self._notify = None              # game_ffb_raw wakes poll(): None not yet tried, False it does not

    def _file(self, name):
        return os.path.join(self.path, name)

    def available(self):
        return all(os.access(self._file(name), os.R_OK | os.W_OK) for name in ('ext_force', 'game_ffb_level'))

    def has_game_ffb_raw(self):
        return os.access(self._file('game_ffb_raw'), os.R_OK)

    def has_const_scale(self):
        """The driver scales the game's FF_CONSTANT effects itself (game_const_scale, 0..100 %)."""
        return os.access(self._file('game_const_scale'), os.R_OK | os.W_OK)

    def const_scale(self):
        """game_const_scale's target (None if unreadable)."""
        return self._int('game_const_scale')

    def set_const_scale(self, value):
        """game_const_scale through one file kept open, written at offset 0."""
        if self._scale_fd is None:
            self._scale_fd = os.open(self._file('game_const_scale'), os.O_WRONLY)
        os.pwrite(self._scale_fd, b'%d\n' % value, 0)

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

    def set_ext_force_cap(self, value):
        self._write_attr('ext_force_cap', value)

    def set_master_gain(self, value):
        self._write_attr('gain', value)

    def has_gear_cross(self):
        """The driver has the gear-crossing stage (ffb_gear_cross, new-lg4ff)."""
        return os.access(self._file('ffb_gear_cross'), os.R_OK | os.W_OK)

    def set_gear_cross(self, text):
        """A rare write (a take-over, the slider, its end), opened for each."""
        fd = os.open(self._file('ffb_gear_cross'), os.O_WRONLY)
        try:
            os.write(fd, text.encode() + b'\n')
        finally:
            os.close(fd)

    def gear_cross_state(self):
        """ffb_gear_cross_state's six numbers: side moving output weight ticks bypassed; None if unreadable."""
        try:
            with open(self._file('ffb_gear_cross_state')) as f:
                values = [int(v) for v in f.readline().split()]
        except (OSError, ValueError):
            return None
        return values if len(values) == 6 else None

    def _write_attr(self, name, value):
        """A rare write (full range at a take-over and its restore), opened for each."""
        fd = os.open(self._file(name), os.O_WRONLY)
        try:
            os.write(fd, b'%d\n' % value)
        finally:
            os.close(fd)

    NOTIFY_EVENTS = select.POLLPRI | select.POLLERR      # what sysfs_notify wakes on game_ffb_raw

    def read_raw(self):
        """game_ffb_raw as (level, count, id). The read at offset 0 also re-arms the driver's notification."""
        if self._raw_fd is None:
            self._raw_fd = os.open(self._file('game_ffb_raw'), os.O_RDONLY)
        level, count, effect = os.pread(self._raw_fd, 64, 0).split()[:3]
        if self._notify and self._ready(0):
            self._notify = False         # still signalled after the re-arming read: poll() tells nothing here
        return int(level), int(count), int(effect)

    def _ready(self, timeout):
        poller = select.poll()
        poller.register(self._raw_fd, self.NOTIFY_EVENTS)
        return bool(poller.poll(timeout))

    def notify_fd(self):
        """(fd, events) to poll() for an upload to game_ffb_raw, or None where the driver does not notify (the
        caller keeps polling by time). Tried once: armed by a read, then it must not be signalled at once."""
        if self._notify is None:
            self._notify = False
            try:
                if self._raw_fd is None:
                    self._raw_fd = os.open(self._file('game_ffb_raw'), os.O_RDONLY)
                os.pread(self._raw_fd, 64, 0)
                self._notify = not self._ready(0)
            except (OSError, ValueError):
                pass
        return (self._raw_fd, self.NOTIFY_EVENTS) if self._notify else None

    def force(self, value):
        if self._fd is None:
            self._fd = os.open(self._file('ext_force'), os.O_WRONLY)
        os.pwrite(self._fd, b'%d\n' % value, 0)

    def close(self):
        for name in ('_fd', '_raw_fd', '_scale_fd'):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)


Status = collections.namedtuple('Status', 'state reason game rate gap_ms enabled available active ready '
                                          'refused last_stop device waiting held_by raw_rate app_gain clipping '
                                          'gain note', defaults=(None, None))
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
FAST_POLL = 0.001                        # s: the pass while RBR or BeamNG plays (their force is not the telemetry's)
KEEP_WRITE = 0.010                       # s: ext_force is written at least this often while playing (the driver
                                         # holds a value 30 ms)
MIN_WRITE = 0.001 - 1e-6                 # s: and never more often than this (RBR, BeamNG)
WRITE_GAP = 1.0                          # s without an ext_force write while 'on': the driver's watchdog has given
                                         # the game its force back (and blocks ext_force): a release, then a new take-over
LATE_PASS = 0.030                        # s: a pass (or the time between ext_force writes) longer than this is logged
LATE_DUMP = 0.100                        # s: a pass this long dumps every thread's stack (faulthandler, no GIL needed)
LATE_LOG_EVERY = 10.0                    # s between those warnings (with a count of what was left out)
LATE_DUMP_EVERY = 30.0                   # s between stack dumps
LATE_DUMP_MAX = 262144                   # bytes: no more dumps once the file is this big
SWITCH_INTERVAL = 0.001                  # s: the interpreter's thread switch (default 5 ms). Each GIL-releasing call of a
                                         # pass (a sysfs write, a lock) waits a whole interval for a busy thread (the
                                         # drive log's bands: audit, engine passes of 100-270 ms every ~10 s)
LEVEL_CHECK = 0.25                       # s between reads of game_ffb_level while we hold the game muted
SPIKE = 0.30                             # share of full scale: a jump past this must persist a second sample to pass
SLEW = FULL / 0.010                      # ext_force units/s: the most the written value moves (full scale in 10 ms;
                                         # the driver slews too, this only keeps a kerb's or a snap's edge from
                                         # being a step)
REVERSE_SPEED = 1.5                      # m/s backwards: the car is reversing
REVERSE_MAX = 0.6                        # share of full scale: the most a reversing car's rack force pushes
SOURCE_RETRY = 5.0                       # s before binding BeamNG's port again after it failed
GAIN_POLL = 0.1                          # s between reads of the driver's master gain while playing
GAIN_TAU = 0.15                          # s: the gain we divide by follows the driver's (no step in what is written)
REGAINS = 1.0                            # s between reads of app_gain while RBR or BeamNG may play
RBR_STREAM_GAP = 2.0                     # s without a new upload: RBR's stream restarted (its STRENGTH may differ)
CSB_MAX_RATE = 2000.0                    # CSB1 packets per second of game time at most (one per physics step) ...
CSB_SEQ_SLACK = 20                       # ... plus this: seq running further ahead is another vehicle's
FZ_STALL = 0.25                          # s without a Forza sample while ForzaRack plays: released (frame hitches are shorter)
FZ_LOCK_STEER = 0.97                     # |steer| at or over which the lock stop of Forza's wheel acts
FH_STALL = 0.05                          # s without a Forza sample while adding (3 of its ~60/s): cut at once
FH_FRESH_UPLOADS = 3.0                   # Forza: raw is fresh for this many of the game's upload intervals ...
FH_FRESH_MIN = 0.03                      # ... at least (s), at most RAW_FRESH
FH_SCALE_KEEP = 0.2                      # s: game_const_scale written at least this often while under 100 (the
                                         # driver's watchdog: 500 ms)
SOFT_REASONS = ('gaps', 'low-rate')      # verdicts that must persist SOFT_HOLD before ours is handed back (the rest at once)
SOFT_HOLD = 0.75                         # s
BACKOFF_COUNT = 3                        # releases within BACKOFF_WITHIN s: stay on the game's force for BACKOFF_FOR s
BACKOFF_WITHIN = 30.0
BACKOFF_FOR = 30.0
GLIDE_IN = 2.5                           # s: from the game's level to our model's after a take-over
GLIDE_OUT = 0.5                          # s: from ours back to the game's level before a planned handback's crossfade
HOLD_DECAY_FROM = 0.03                   # s without a packet: the last value starts easing down (the driver holds it 30 ms) ...
HOLD_DECAY = 0.15                        # ... over this long ...
HOLD_FLOOR = 0.5                         # ... to this share
HOLD_RECOVER = 0.1                       # s for the eased value to come back to the force once packets return
FH_REARM = 0.3                           # s after a release before the add-on starts again (the game is never
                                         # muted, so no crossfade to protect: only no flapping)


class LockStop:
    """Our own steering-lock stop for ACR (a real rack has a mechanical one). ACR's force to the wheel carries a
    lock-stop force that telemetry lacks, so with Countersteer playing nothing stops the wheel at the game's lock
    (on marth's 900 degree wheel it went 14 and 44 degrees past it). Beyond the game's lock angle in the wheel's
    own frame (the range / 2 times FrameCheck's measured slope; until the check has one there is no stop: the
    wheel's own end stop remains) a stiff spring grows with a smooth onset (quadratic over the first ONSET
    degrees), with damping on the wheel's speed so it does not bounce, bounded to MAX of full scale. Nothing
    inside the lock. Returns ext_force units (positive pushes toward decreasing ABS_X); the position is ABS_X in
    degrees from the centre (positive toward increasing ABS_X)."""

    ONSET = 3.0                          # degrees past the lock over which the spring comes in
    K = 0.075                            # share of full scale per degree past the lock (0.6 at 8 degrees)
    DAMP = 0.005                         # share of full scale per degree/s (about critical for a light wheel: the driver's
                                         # default inertia 670 gives 0.0045); applied both ways, the sum never pulls outward
    MAX = 0.6                            # share of full scale: the most it pushes
    VEL_TAU = 0.015                      # s: the speed's low-pass
    GAP = 0.1                            # s between samples past which the speed starts again

    def __init__(self):
        self.reset()

    def reset(self):
        self._prev = None                # (t, pos)
        self.vel = 0.0                   # degrees per second, smoothed
        self.active = False

    def force(self, t, steer, pos, lock_deg=None):
        """The push at time `t` (ext_force units). `lock_deg`: the game's lock in wheel degrees; None (unknown): no push."""
        prev, self._prev = self._prev, (t, pos)
        if pos is None or not math.isfinite(pos):
            self.vel, self.active = 0.0, False
            return 0.0
        if prev is None or not 0.0 < t - prev[0] <= self.GAP:
            self.vel = 0.0
        else:
            dt = t - prev[0]
            self.vel += (((pos - prev[1]) / dt) - self.vel) * (dt / (self.VEL_TAU + dt))
        if lock_deg is None:
            self.active = False
            return 0.0
        over = abs(pos) - lock_deg
        if over <= 0.0:
            self.active = False
            return 0.0
        self.active = True
        side = math.copysign(1.0, pos)
        spring = self.K * over * min(over / self.ONSET, 1.0)
        push = spring + self.DAMP * self.vel * side
        return side * min(max(push, 0.0), self.MAX) * FULL


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
    STALL_DUMPS = True                   # faulthandler stack dumps of a long pass (off in the tests)
    STATUS_EVERY = 0.1                   # s between status updates while nothing changes

    def __init__(self, clock=time.monotonic, driver=DriverReplace, beamng_source=BeamNGForceSource):
        self._clock = clock
        self._driver_type = driver
        self._source_type = beamng_source
        self._slot = None                # (seq, arrival time, sample): the newest, written by the listener only
        self._seq = 0
        self._wake = _Wake()
        self._enabled_req = False        # GUI requests, applied on the engine thread
        self._path_req = None
        self._usb_id = None
        self._peak_override = None       # Nm the user set for the current wheel (set_wheel_peak), None: the table's
        self._inhibit_req = False        # wheel_owner: an exclusive owner wants the wheel
        self._texture_req = None         # (seq, level 0..1, {part: on}) from the GUI, applied on the engine thread
        self._texture_seq = 0            # the GUI's last request number
        self._texture_seen = 0           # the engine's last applied
        self._running = False
        self._thread = None
        self._stop_lock = threading.Lock()
        self.classifier = SupportClassifier()
        self.model = RackForce()         # ACR (and AC, ACC): from the front tyres' Mz
        self.rbr = RbrForce()            # Richard Burns Rally: the gates on the game's force (the upload grid, its strength) and S
        self._rbr_why = None             # the last reason logged for not playing
        self.rbr_rack = RbrRack()        # ... and its column torque through our chain (RackForce's)
        self.beamng = BeamNGRack()       # BeamNG: the mod's rack force
        self.addon = ForzaLightness()    # Forza Horizon: lighter as the front tyres run out of grip
        self.forza_rack = ForzaRack()    # ... or, with countersteer_forza_model on (the default), its force from Newton through our chain
        self.forza_filter = ffb_forza.OutputFilter(FULL)
        self._forza_model = OVERALL_FORZA_MODEL      # ForzaRack (True) or ForzaLightness (False) for Forza Horizon
        self._forza_model_req = None     # the profile INI's countersteer_forza_model from the GUI
        self._forza_spring_req = None    # the profile INI's countersteer_forza_spring from the GUI
        self._rbr_spring_req = None      # the profile INI's countersteer_rbr_spring from the GUI
        self._rbr_source_req = None      # the profile INI's countersteer_rbr_source from the GUI
        self._fz_sample = None           # the last Forza sample (ForzaRack)
        self._fz_force_sample = None     # ... the one the rack's share was last worked out for ...
        self._fz_force = 0.0             # ... and that share (ext_force units, before the output filter)
        self._lightness_req = None       # its strength (0..1) from the GUI
        self._strength_req = None        # the overall steering strength (%) from the GUI
        self._strength = OVERALL_STRENGTH   # ... as applied to ACR's, RBR's and BeamNG's own force
        self._knee_req = None            # the "1:1 up to" share (%) from the GUI
        self._rbr_cut_req = None         # the "RBR force cut" corner (Hz) from the GUI
        self._engine_feel_req = None     # the "Engine feel" share (%) from the GUI
        self._caster_req = None          # the "Caster in slides" share (%) from the GUI
        self._mapping_req = None         # the steering mapping ('classic' or 'knee') from the GUI
        self._pull_req = None            # "Drive pull": (on, level %) from the GUI
        self._kick_req = None            # "Kickback": (on, level %) from the GUI
        self._gear_req = None            # "Gear guard": (on, level %, wheel drive type) from the GUI
        self._gear_on = False
        self._gear_level = float(ffb_gear.OVERALL_GEAR_GUARD_LEVEL)
        self._gear_flip = float(ffb_gear.OVERALL_GEAR_FLIP)
        self._gear_drive = 'gear'
        self._gear_written = None        # what this engine last wrote to ffb_gear_cross while it's on; None: it's off
        self._gear_failed = False        # no attribute or a failed write, on this wheel: not tried again
        self.gear_hook = None            # (wheel, on): the host tells the GUI to turn the stage off if this process dies
        self._damping_req = None         # "Column damping" (% of the real column's) from the GUI
        self._rim_req = None             # the wheel's rim diameter (mm, 0: the table's) from the GUI
        self._car_wheel_req = None       # the car's steering wheel diameter (mm, profile INI only) from the GUI
        self._power_class_req = None     # the assist class (rally / street / off, profile INI only) from the GUI
        self._dyn = None                 # ffb_dynamics.DynStage of the wheel (the driver's virtual steering dynamics)
        self._fast_spin_relief = False   # "Fast-spin relief" (the profile's): on a gear-driven wheel the column's damping is the total
        self._dyn_req = (True, True, False)     # column feel, local aligning spring, friction removal (the profile's)
        self._wheel_req = None           # the stored wheel calibration result, for ffb_dyn_wheel and the wheel's own figures
        self._pos_req = None             # () -> (ABS_X 0..65535, ...) of the physical wheel, or None (ffb_dynamics.FrameCheck)
        self._pos_src = None
        self._frame = ffb_dynamics.FrameCheck()
        self._frame_range = None
        self._frame_sens = None          # the driver's sensitivity the check was fed under
        self._lock_stop = LockStop()
        self._prov = None                # the stored wheel slope for this car / range / sensitivity, until the live check judges
        self._prov_key = None            # (car, range, sensitivity) it was read for
        self._prov_saved = None          # (key, slope) last handed to the store
        self._pos_cache = None           # (t, measured degrees, raw frame) of the last wheel position read
        self._slope_logged = False       # the measured wheel mapping has been logged (once per fit of the check)
        self.align_note = None           # why the aligning spring is not played (the info line shows it)
        self._align_why = ''             # the reason last logged (None: playing)
        self._rec_levels = {}            # channel -> (value, time) of the last game_ffb_level / game_const_scale record
        self._rec_at = None              # when the FFB tuning record last read the driver's hands/live/thermal
        self._fh_level = None            # the game's last fresh raw level (Forza; held through a fade out)
        self._fh_fresh = None            # ... as of the last pass, None when it was not fresh
        self._fh_scale = False           # the driver has game_const_scale: Forza is lightened through it
        self._scale_last = None          # the last game_const_scale written (None: none since the wheel was set)
        self._scale_at = None            # ... when
        self._scale_seen = 0             # the slot seq of the Forza sample the last scale write answered
        self.scale_writes = 0            # game_const_scale writes (tests, the log)
        self.texture = Texture()
        ffb_record.settings_source = self.settings_snapshot       # (the header of an FFB tuning capture, ffb_record)
        self._mode = 'acr'               # whose force: 'acr' (per telemetry sample), 'rbr', 'beamng', 'forza' (the add-on),
                                         # 'forza-rack' (each pass)
        self._played_mode = None         # the mode of the force playing
        self._mode_at = None             # when the mode's game was last heard
        self._linearity_req = None       # RBR's linearity from the GUI
        self._source = None              # BeamNGForceSource while BeamNG may be on
        self._source_failed_at = None
        self._beamng_at = None           # when BeamNG was last heard (OutGauge or CSB1)
        self._csb = _CsbRate()
        self._csb_seq = 0                # the source's last packet taken
        self._csb_sample = None
        self._rbr_level = None           # the last fresh raw level (played while fading out)
        self._rbr_new_at = None          # when the last new upload came
        self._rbr_sample = None          # the last RBR telemetry sample (its stage and clock)
        self._rbr_now = None             # the fresh raw level of the last pass (None: none), for the sign latch
        self._rbr_tex_sample = None      # the sample the RBR texture was last stepped for ...
        self._rbr_tex = 0.0              # ... and its value
        self._gains_at = None            # when app_gain was last read
        self._write_pending = False      # a new value not yet written (MIN_WRITE)
        self._written_at = None
        self._smooth = None              # the force last passed by the spike gate and slew (before the fade)
        self._smooth_at = None
        self._spike = None               # a jump held back, waiting for a second sample to agree
        self._level_at = None            # when game_ffb_level was last read back while we hold the game muted
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
        self._raw_wake_at = None         # monotonic: game_ffb_raw's notification woke the wait, not yet read
        self._raw_lags = []              # s: wake to read of each notified game_ffb_raw read, this take-over
        self._raw_reads = 0              # game_ffb_raw reads this take-over
        self._raw_id = None
        self._raw_id_at = None           # when its effect id last changed
        self._raw_error = False
        self._raw_counts = collections.deque()      # (t, count) over the last second: the game's upload rate
        self.raw_rate = None             # constant uploads a second (diagnosis: logged at activation, in the status)
        self._app_gain = False           # the driver applies the game's FF_GAIN to its force, not to ours
        self._clip_at = None             # |ext_force| past which the driver's cap flattens it
        self._cap_level = None           # the cap in the master gain's own frame (what is meant to be delivered)
        self._gain_at = None             # when the master gain was last read
        self._gain_seen = None           # master gain, share of 100 % (None unknown)
        self._range = None               # RangeState while the driver's cap and gain are raised for our force
        self._range_failed = False       # nothing to raise, or a write failed, in this take-over: not tried again
        self.range_hook = None           # (wheel, RangeState or None): the host tells the GUI what to restore if this process dies
        self._comp = 1.0                 # the gain we divide by (smoothed)
        self._comp_at = None
        self._clipped = 0.0              # EW share of writes past it
        self._last_value = 0             # the last ext_force written
        self._closed = False             # stop() has begun: never take over again
        self._seen = 0                   # the last slot seq taken
        self._last_t = None              # arrival time of the newest sample taken
        self._arrive = collections.deque(maxlen=8192)    # (arrival time, packet_id) of every packet, from the listener
        self._arrived_at = None          # arrival time of the newest packet (the stall check; may be newer than _last_t)
        self._base_value = 0             # the last ext_force written by _out (before any easing in a gap)
        self._hold_f = 1.0               # the share of the force the easing in a gap left (back to 1 after HOLD_RECOVER)
        self._hold_at = None
        self._w = 0.0                    # 0: the game's level, 1: our model's (glides at a take-over and a handback)
        self._glide_at = None
        self._handback = False           # a planned handback is gliding down to the game's level
        self._soft_since = None          # when the verdict first turned soft-bad (SOFT_REASONS) this time
        self._stalls = collections.deque()      # times of the recent stall / soft releases
        self._backoff_until = None
        self._taken_skipped = 0          # self.skipped at the take-over (the release log says how many since)
        self._hand = ffb_handforce.HandForce()   # this drive's hand force in N (logged at the hand-back)
        self._hand_t = None              # the last sample time counted in it
        self._hand_last = ''             # the previous drive's summary (the next take-over's line says it)
        self._lag = 0.0                  # s the engine was behind the newest sample when it took it
        self._verdict = Verdict('none', 'no-telemetry', None, 0.0, None)
        self._status_at = None
        self.last_stop = None            # why the force last stopped ('off', 'state', 'stall', ...)
        self.writes = 0                  # ext_force writes (tests, the log)
        self._pass_took = 0.0            # the last pass's length (the late-write warning names it)
        self._write_mono = None          # monotonic time of the last ext_force write (late-write check)
        self._late_at = -LATE_LOG_EVERY  # engine-late warnings: last logged, and how many since
        self._late_count = 0
        self._dump_at = -LATE_DUMP_EVERY # faulthandler stack dumps (engine-stalls.log)
        self._dump_file = None
        self._dump_armed = None
        self.skipped = 0                 # samples the engine never saw (it was behind)
        self.status = IDLE_STATUS

    # -- the listener's side: never blocks --

    def offer(self, now, sample, seq=None):
        """The listener thread: a decoded sample that arrived at `now`
        (monotonic s). O(1): the slot is replaced, the engine woken. `seq`: the
        arrival's number when it is counted elsewhere (the engine process: the
        GUI's listener numbers every packet, ffb_process), else the next."""
        self._seq = self._seq + 1 if seq is None else seq
        self._slot = (self._seq, now, sample)
        self._arrive.append((now, getattr(sample, 'packet_id', None)))
        if not self._wake.is_set():
            self._wake.set()

    def arrived(self, now, packet_id):
        """A packet that arrived at `now` and was never offered (a newer one came with it to the engine process): its
        arrival still counts for the classifier's gaps; offer(..., seq) counts it for the rate."""
        self._arrive.append((now, packet_id))

    # -- the GUI's side --

    def set_enabled(self, on):
        """The "Countersteer FFB" switch."""
        self._enabled_req = bool(on)
        self._wake.set()

    def set_dynamics(self, column, align, removal):
        """The profile's switches for the driver's virtual steering dynamics: the car's column feel (damping and
        friction), the local aligning spring, and friction removal (a column lighter than the wheel's own)."""
        self._dyn_req = (bool(column), bool(align), bool(removal))
        self._wake.set()

    def set_fast_spin_relief(self, on):
        """The profile's "Fast-spin relief": on a gear-driven wheel the knee mapping's column damping becomes a total (the real
        column at this rim), so the driver takes the wheel's extra drag off instead of the column's damping adding to it."""
        self._fast_spin_relief = bool(on)
        self._wake.set()

    def set_position_source(self, source):
        """A callable returning the physical wheel's ABS_X now (0..65535 over its range), None if it can't be read:
        the aligning spring is played only once the wheel's position is seen to follow the game's steering."""
        self._pos_req = (source,)
        self._wake.set()

    def set_wheel_result(self, result):
        """The wheel's stored calibration result (wheel_calibration), None for none."""
        self._wheel_req = result if isinstance(result, dict) else None
        self._wake.set()

    def set_texture(self, level, parts):
        """The texture's master level (0..1) and its parts' switches ({'road': bool, ...})."""
        # One tuple, swapped whole and never cleared: the engine applies it when its number is new, so a
        # request made while the engine reads the previous one is not lost
        self._texture_seq += 1
        self._texture_req = (self._texture_seq, level, dict(parts))
        self._wake.set()

    def set_linearity(self, value):
        """RBR: how much of NGP's curve to undo, 0 (the game's curve) .. 100 (linear); ffb_rbr."""
        self._linearity_req = value
        self._wake.set()

    def set_lightness(self, value):
        """Forza Horizon: "Understeer lightness", 0..100 % of the drop the front slip asks for; ffb_addon."""
        self._lightness_req = value
        self._wake.set()

    def set_strength(self, value):
        """The overall strength of Countersteer's own steering force (ACR, RBR, BeamNG), in % of what the model gives."""
        self._strength_req = value
        self._wake.set()

    def set_knee(self, value):
        """The share of the wheel's peak (%, 30..60) that Countersteer's steering force is played 1:1 in Nm up to."""
        self._knee_req = value
        self._wake.set()

    def set_rbr_cut(self, value):
        """RBR's column torque low-pass corner in Hz (15..120; 120 and above: no filter)."""
        self._rbr_cut_req = value
        self._wake.set()

    def set_caster_floor(self, value):
        """RBR's "Caster in slides": the share (0..100 %) of the mechanical caster's force kept in a slide; 0 is off."""
        self._caster_req = min(max(float(value), 0.0), 100.0)
        self._wake.set()

    def set_engine_feel(self, value):
        """The "Engine feel": the engine hum's strength in % of its base level (0..300; 0 is off)."""
        self._engine_feel_req = min(max(float(value), 0.0), 600.0)
        self._wake.set()

    def set_mapping(self, value):
        """The steering mapping (profile INI only): 'knee' (1:1 in Nm up to the knee, compressed above, hydraulic top
        end) or 'classic' (the uniform impedance scale, the feel before the knee)."""
        self._mapping_req = 'classic' if value == 'classic' else 'knee'
        self._wake.set()

    def set_forza_model(self, on):
        """Forza Horizon (profile INI only, countersteer_forza_model): True plays Countersteer's own force through the ACR
        chain (ForzaRack), False keeps the game's force lightened as the front tyres run out of grip (ForzaLightness)."""
        self._forza_model_req = bool(on)
        self._wake.set()

    def set_forza_spring(self, on):
        """Forza Horizon's aligning spring (profile INI only, countersteer_forza_spring): True plays it through the driver as ACR's,
        False leaves it with the column's damping and friction."""
        self._forza_spring_req = bool(on)
        self._wake.set()

    def set_rbr_spring(self, on):
        """RBR's aligning spring (profile INI only, countersteer_rbr_spring): True plays it through the driver as ACR's,
        False leaves RBR with the column's damping and friction."""
        self._rbr_spring_req = bool(on)
        self._wake.set()

    def set_rbr_source(self, value):
        """RBR's force (profile INI only, countersteer_rbr_source): 'derived' (default) is the kingpin torque of the car's physics
        (RbrRack), 'ngp' the game's own force recovered and played through the chain."""
        value = str(value).strip().lower()
        self._rbr_source_req = value if value in RBR_SOURCES else OVERALL_RBR_SOURCE
        self._wake.set()

    def set_drive_pull(self, on, level):
        """Drive pull: the scrub couple of the drive's left/right split in the steering force, on or off, at `level` %
        of the physical scrub radius."""
        self._pull_req = (bool(on), min(max(float(level), 0.0), 100.0))
        self._wake.set()

    def set_kick(self, on, level):
        """Kickback: the onset pulse of the scrub couple (ffb_kick), on or off, at `level` % of its amplitude."""
        self._kick_req = (bool(on), min(max(float(level), 0.0), 100.0))
        self._wake.set()

    def set_gear_guard(self, on, level, drive='gear', flip=ffb_gear.OVERALL_GEAR_FLIP):
        """Gear guard (the driver's crossing stage, ffb_gear): on or off, at `level` % (the crossing's speed), the flip point at
        `flip` % of full scale; `drive`: the wheel's drive type (belt and direct have no backlash: the stage stays off)."""
        self._gear_req = (bool(on), min(max(float(level), 0.0), 100.0), drive,
                          min(max(float(flip), ffb_gear.GEAR_FLIP_RANGE[0]), ffb_gear.GEAR_FLIP_RANGE[1]))
        self._wake.set()

    def set_column_damping(self, pct):
        """Column damping: 0..100 % of the real steering column's damping added to the wheel's own in the knee mapping; the dynamics stage
        rewrites ffb_dyn_damping when its target changes."""
        self._damping_req = min(max(float(pct), 0.0), 100.0)
        self._wake.set()

    def set_rim_mm(self, value):
        """The wheel's rim diameter in mm, 200..400 (0: the table's for the wheel, wheel_torque.RIM_MM)."""
        value = float(value)
        self._rim_req = 0.0 if value <= 0 else min(max(value, wheel_torque.RIM_RANGE[0]), wheel_torque.RIM_RANGE[1])
        self._wake.set()

    def set_car_wheel_mm(self, value):
        """The car's steering wheel diameter in mm, 280..420: the hand-force scale rho is the wheel's
        rim over it."""
        self._car_wheel_req = min(max(float(value), wheel_torque.CAR_WHEEL_RANGE[0]), wheel_torque.CAR_WHEEL_RANGE[1])
        self._wake.set()

    def set_power_class(self, name):
        """The assist class (profile INI only, countersteer_power_class): 'rally' (default, hydraulic top end 7.0 Nm),
        'street' (3.7 Nm) or 'off' (unassisted); anything else is rally."""
        self._power_class_req = name or ffb_carprops.DEFAULT_POWER_CLASS
        self._wake.set()

    def car_properties(self):
        """(car, caster trail m, scrub radius m) as the ACR model holds them for the current car, None before one."""
        model = self.model
        return None if model.car is None else (model.car, model.trail, model.scrub)

    def hand_force(self):
        """What the hand-force mapping uses now, for the FFB tab's line: the rack of the game playing (the last one seen),
        its rim diameter and the car's wheel with where each came from ('profile': the user set it, 'table': the wheel
        table, 'default'), and rho. The game doesn't send the car's wheel size, so it is the profile's or a default."""
        game = self._verdict.game
        rack = self.forza_rack if game == 'forza-fh' else self.rbr_rack if game == 'rbr' else self.model
        req = self._car_wheel_req
        car_set = req is not None and req != wheel_torque.CAR_WHEEL_MM
        return {'rim_mm': rack.rim_mm(), 'rim_src': 'profile' if rack.rim_user else 'table' if rack.wheel_rim else 'default',
                'car_wheel_mm': rack.car_wheel_mm, 'car_src': 'profile' if car_set else 'default', 'rho': rack.rho(),
                'game': game}

    def details(self):
        """What the FFB tab's read-only lines show besides the status (gui.countersteer_details), as plain values: the
        engine process sends this to the GUI's EngineProcess (ffb_process)."""
        model = self.model
        try:
            impedance = model.impedance()
        except Exception:
            impedance = None
        return {'car_properties': self.car_properties(), 'align_note': self.align_note, 'ratio': model.ratio,
                'assist': model.assist, 'impedance': impedance, 'hand_force': self.hand_force()}

    def settings_snapshot(self):
        """The profile values in effect ("profile": what a change of is logged) and the learnt ones now ("learnt"), as
        JSON-able plain values, for the FFB tuning capture's header (ffb_record)."""
        model, fz = self.model, self.forza_rack
        column, align, removal = self._dyn_req
        profile = {
            'strength': round(self._strength * 100.0, 3), 'knee': round(model.knee * 100.0, 3), 'mapping': model.mapping,
            'rbr_cut_hz': self.rbr_rack.cut_hz, 'caster_floor': round(self.rbr_rack.floor_level * 100.0, 3),
            'engine_feel': round(self.texture.engine.feel * 100.0, 3),
            'column_damping': model.damping_pct, 'drive_pull': [bool(model.pull_on), round(model.pull_level * 100.0, 3)],
            'kick': [bool(model.kick_on), round(model.kick_level * 100.0, 3)],
            'gear_guard': [self._gear_on, round(self._gear_level, 3)], 'gear_flip': self._gear_flip, 'wheel_drive': self._gear_drive, 'rim_mm': model.rim_user,
            'car_wheel_mm': model.car_wheel_mm, 'forza_car_wheel_mm': fz.car_wheel_mm,
            'power_class': ffb_carprops.power_class(), 'texture_level': round(self.texture.level, 4),
            'texture_parts': dict(self.texture.parts), 'column': bool(column), 'align': bool(align),
            'friction_removal': bool(removal), 'forza_model': bool(self._forza_model), 'forza_spring': bool(self.forza_rack.spring_on), 'rbr_spring': bool(self.rbr_rack.spring_on), 'rbr_source': self.rbr_rack.source,
            'lightness': round(self.addon.strength * 100.0, 3)}
        learnt = {'car': model.car, 'ratio': model.ratio, 'lock': model.lock, 'trail_m': model.trail,
                  'scrub_m': model.scrub, 'forza_car': fz.car, 'forza_mass_kg': fz.mass,
                  'rbr_strength': self.rbr.strength}
        return {'profile': profile, 'learnt': learnt}

    @property
    def usb_id(self):
        return self._usb_id

    @usb_id.setter
    def usb_id(self, usb_id):
        """The wheel's model (USB vendor:product), for the peak torque the steering force is mapped to
        (wheel_torque; unknown: the relative target). Set before set_device. A peak set for another wheel
        (set_wheel_peak) is dropped: the GUI then sets the new wheel's own."""
        if usb_id != self._usb_id:
            self._peak_override = None
        self._usb_id = usb_id
        for rack in (self.model, self.rbr_rack, self.forza_rack):
            rack.wheel_peak = self._peak_override or wheel_torque.peak_nm(usb_id)        # one attribute swap, read by the engine thread
            rack.wheel_rim = wheel_torque.rim_mm(usb_id)

    def set_wheel_peak(self, nm):
        """The current wheel's peak torque in Nm, PEAK_RANGE (the user's figure; None: the table's for the wheel). Applies
        at once to every game's rack: one attribute swap each, read by the engine thread."""
        self._peak_override = None if nm is None else min(max(float(nm), wheel_torque.PEAK_RANGE[0]), wheel_torque.PEAK_RANGE[1])
        for rack in (self.model, self.rbr_rack, self.forza_rack):
            rack.wheel_peak = self._peak_override or wheel_torque.peak_nm(self._usb_id)

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
        if sys.getswitchinterval() > SWITCH_INTERVAL:
            sys.setswitchinterval(SWITCH_INTERVAL)
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
                    self._restore_scale(self._clock())
                    self._let_go()
                    return
            self._quick_release('quit', self._clock())
            if self._driver is not None:
                self._driver.close()
            self._wake.close()
            self._stop_source()

    def _run(self):
        self.loop(self._wait)

    def raw_wait(self):
        """(fd, events) whose wake means game_ffb_raw was uploaded, while the force is read each pass and the
        driver notifies; else None (the passes keep to their timeout)."""
        driver = self._driver
        notify = getattr(driver, 'notify_fd', None)
        if notify is None or not self.classifier.has_game_ffb_raw or not self._fast():
            return None
        return notify()

    def raw_woke(self, now):
        """The wait ended on game_ffb_raw's notification at `now` (monotonic s)."""
        if self._raw_wake_at is None:
            self._raw_wake_at = now

    def _wait(self, timeout):
        """loop's wait in the engine thread: the Event, and game_ffb_raw's fd when it notifies."""
        watch = self.raw_wait()
        if watch is None:
            return self._wake.wait(timeout)
        if self._wake.is_set():
            return True
        poller = select.poll()
        poller.register(self._wake.pipe(), select.POLLIN)
        poller.register(watch[0], watch[1])
        try:
            events = poller.poll(timeout * 1000.0)
        except (InterruptedError, OSError):
            return self._wake.is_set()
        if any(fd == watch[0] for fd, _ in events):
            self.raw_woke(time.monotonic())
        return self._wake.is_set()

    def _log_raw_lag(self):
        """Once per take-over: how long after the notification the force was read."""
        lags, reads = self._raw_lags, self._raw_reads
        self._raw_lags, self._raw_reads, self._raw_wake_at = [], 0, None
        if not lags:
            return
        lags.sort()
        logging.info("Countersteer FFB: game_ffb_raw wake to read: median %.2f ms, p99 %.2f ms (%d of %d reads woken)",
                     lags[len(lags) // 2] * 1000.0, lags[min(len(lags) - 1, int(len(lags) * 0.99))] * 1000.0,
                     len(lags), reads)

    def loop(self, wait):
        """The passes until stop(): `wait(timeout)` returns at a sample or a request (the thread's Event; in the engine
        process its sockets, ffb_process) or after `timeout` s."""
        while self._running:
            busy = self._phase != 'idle' or (self._enabled and self._path is not None)
            wait(FAST_POLL if self._fast() else self.POLL if busy else IDLE_POLL)
            self._wake.clear()           # before the slot is read: an offer after this wakes the next wait
            if not self._running:
                break
            began = time.monotonic()
            self._arm_dump(began)
            try:
                self.step()
                self._pass_done(began)
            except Exception:
                logging.exception("Countersteer FFB: stopped until the wheel is set again")
                self._broken, self._available = True, False
                try:
                    self._release('error', self._clock())
                except Exception:
                    logging.exception("Countersteer FFB: restoring the game's force")

    # -- the engine thread --

    def _arm_dump(self, began):
        """Have faulthandler (its own C thread: it needs no GIL) write every thread's stack if this pass takes
        LATE_DUMP, to engine-stalls.log in the log folder; at most once per LATE_DUMP_EVERY, until the file is big."""
        if not self.STALL_DUMPS or began - self._dump_at < LATE_DUMP_EVERY:
            self._cancel_dump()
            return
        try:
            if self._dump_file is None:
                from . import diagnostics
                folder = diagnostics.log_dir()
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, 'engine-stalls.log')
                if os.path.exists(path) and os.path.getsize(path) > LATE_DUMP_MAX:
                    self._dump_at = float('inf')
                    return
                self._dump_file = open(path, 'a')
            faulthandler.dump_traceback_later(LATE_DUMP, file=self._dump_file)
            self._dump_armed = began
        except Exception:
            self._dump_at = float('inf')                # not available here: do not try again

    def _cancel_dump(self):
        if self._dump_armed is not None:
            faulthandler.cancel_dump_traceback_later()
            self._dump_armed = None

    def _pass_done(self, began):
        """After a pass: a dump that fired is noted (and not armed again for a while); a pass longer than LATE_PASS is
        logged, at most once per LATE_LOG_EVERY with how many were left out."""
        end = time.monotonic()
        took = self._pass_took = end - began
        if self._dump_armed is not None:
            self._cancel_dump()
            if took >= LATE_DUMP:
                self._dump_at = end
                logging.warning("Countersteer FFB: a pass took %.0f ms; every thread's stack is in engine-stalls.log",
                                took * 1000.0)
        if took > LATE_PASS:
            self._late(end, "pass took %.0f ms (phase %s, mode %s)" % (took * 1000.0, self._phase, self._mode))

    def _late(self, now, what, paused=False):
        """A late pass or write gap: one warning per LATE_LOG_EVERY, with the count since the last. A gap in which no
        sample arrived (`paused`: the game sent nothing) is logged at INFO instead, on the same limit."""
        self._late_count += 1
        if now - self._late_at >= LATE_LOG_EVERY:
            if paused:
                logging.info("Countersteer FFB: %s (%d in the last %.0f s)", what, self._late_count,
                             min(now - self._late_at, 999.0))
            else:
                logging.warning("Countersteer FFB: engine late: %s (%d in the last %.0f s)", what, self._late_count,
                                min(now - self._late_at, 999.0))
            self._late_at, self._late_count = now, 0

    def step(self, now=None):
        """One pass: requests, then the newest sample if there is one, else
        the stall check. The thread's loop body (tests call it directly)."""
        if now is None:
            now = self._clock()
        self._requests(now)
        ffb_record.check(now)                    # (a changed setting of a running FFB tuning capture is logged)
        self._drain()
        slot = self._slot
        if self._broken:
            if slot is not None:
                self._seen = slot[0]     # latched off (an error, a failed write): samples are not looked at
            self._publish(now)
            return
        self._watchdog(now)
        if slot is not None and slot[0] != self._seen:
            self._sample(slot, now)
        else:
            self._idle(now)
        self._sources(now)
        self._dyn_record(now)
        if self._mode == 'rbr':
            self._rbr_tick(now)
        elif self._mode == 'beamng':
            self._beamng_tick(now)
        elif self._mode == 'forza':
            self._forza_tick(now)
        elif self._mode == 'forza-rack':
            self._forza_rack_tick(now)

    def _drain(self):
        """Every packet the listener offered since the last pass, in order, to the classifier's gap measure."""
        queue = self._arrive
        while True:
            try:
                t, packet_id = queue.popleft()
            except IndexError:
                return
            self.classifier.note(t, packet_id)
            self._arrived_at = t

    def _fast(self):
        """RBR, BeamNG or Forza's add-on playing or about to: their force is read each FAST_POLL, not per telemetry sample."""
        heard = self._mode_at is not None and self._clock() - self._mode_at <= NONE_AFTER
        return (self._mode in ('rbr', 'beamng', 'forza', 'forza-rack') and self._enabled and self._available and not self._broken
                and self._path is not None and (heard or self._phase != 'idle'))

    def _requests(self, now):
        linearity = self._linearity_req
        if linearity is not None and linearity != self.rbr.linearity:
            self.rbr.set_linearity(linearity)
        strength = self._strength_req
        if strength is not None:
            self._strength = min(max(float(strength), 0.0), STRENGTH_MAX * 100.0) / 100.0
        knee = self._knee_req
        if knee is not None:
            for rack in (self.model, self.rbr_rack, self.forza_rack):
                rack.knee = min(max(float(knee) / 100.0, wheel_torque.KNEE_RANGE[0]), wheel_torque.KNEE_RANGE[1])
        cut = self._rbr_cut_req
        if cut is not None:
            self.rbr_rack.cut_hz = None if cut >= RBR_CUT_OFF else float(max(cut, 1))
        if self._caster_req is not None:
            self.rbr_rack.floor_level = self._caster_req / 100.0
        if self._engine_feel_req is not None:
            self.texture.engine.feel = self._engine_feel_req / 100.0
        if self._mapping_req is not None:
            for rack in (self.model, self.rbr_rack, self.forza_rack):
                rack.mapping = self._mapping_req
        if self._pull_req is not None:
            for rack in (self.model, self.forza_rack):
                rack.pull_on, rack.pull_level = self._pull_req[0], self._pull_req[1] / 100.0
        if self._kick_req is not None:
            for rack in (self.model, self.forza_rack):
                rack.kick_on, rack.kick_level = self._kick_req[0], self._kick_req[1] / 100.0
        if self._gear_req is not None:
            self._gear_on, self._gear_level, self._gear_drive, self._gear_flip = self._gear_req
        if self._damping_req is not None:
            for rack in (self.model, self.rbr_rack, self.forza_rack):
                rack.damping_pct = self._damping_req
        if self._rim_req is not None:
            for rack in (self.model, self.rbr_rack, self.forza_rack):
                rack.rim_user = self._rim_req or None
        if self._car_wheel_req is not None:
            for rack in (self.model, self.rbr_rack):
                rack.car_wheel_mm = self._car_wheel_req
            # (the profile's default is the rally car's 350 mm: only a value the user changed wins over Forza's 375)
            if self._car_wheel_req != wheel_torque.CAR_WHEEL_MM:
                self.forza_rack.car_wheel_mm = self._car_wheel_req
            else:
                self.forza_rack.car_wheel_mm = FORZA_CAR_WHEEL_MM
        if self._forza_model_req is not None:
            self._forza_model = self._forza_model_req
        if self._forza_spring_req is not None:
            self.forza_rack.spring_on = self._forza_spring_req
        if self._rbr_spring_req is not None:
            self.rbr_rack.spring_on = self._rbr_spring_req
        if self._rbr_source_req is not None and self._rbr_source_req != self.rbr_rack.source:
            self.rbr_rack.source = self._rbr_source_req      # (the other source's state starts again)
            self.rbr_rack._restart_derived()
            self.rbr_rack._restart_floor()
        if self._power_class_req is not None and self._power_class_req != ffb_carprops.power_spec():
            ffb_carprops.set_power_class(self._power_class_req)
        lightness = self._lightness_req
        if lightness is not None and lightness / 100.0 != self.addon.strength:
            self.addon.set_strength(lightness / 100.0)
        texture = self._texture_req
        if texture is not None and texture[0] != self._texture_seen:
            self._texture_seen = texture[0]
            self.texture.set(*texture[1:])
        if self._inhibit_req:
            self._inhibit_req = False
            self._quick_release('inhibited', now)
        self._dyn_apply()
        if self._pos_req is not None and self._pos_req[0] is not self._pos_src:
            self._pos_src = self._pos_req[0]
            self._frame.reset()
        path = self._path_req
        if path != self._path:
            self._release('device', now)
            self._muted = False          # the old wheel: restored above, or gone (its watchdog restores it)
            if self._driver is not None:
                self._driver.close()
            self._path, self._driver, self._available, self._broken = path, None, False, False
            self._dyn = None
            self._frame.reset()
            if path is not None:
                self._dyn = ffb_dynamics.DynStage(path, ffb_record.log)
                self._dyn_apply()
            self._raw_count = self._raw_at = self._raw_id = self._raw_id_at = None
            self._raw_counts.clear()
            self._raw_lags, self._raw_reads, self._raw_wake_at = [], 0, None
            self.raw_rate = self.classifier.raw_rate = self._rbr_level = None
            self._rbr_new_at = self._rbr_sample = self._gains_at = None
            self.rbr.reset()             # a new stream: what the game sent the old wheel says nothing
            self.rbr_rack.reset()
            self.forza_rack.reset()
            self.forza_filter.reset()
            self.addon.reset()
            self._fh_level = self._fh_fresh = None
            self._fh_scale, self._scale_last, self._scale_at = False, None, None
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
                    self._fh_scale = self._driver.has_const_scale()
                    if self._fh_scale:
                        scale = self._driver.const_scale()
                        if scale is not None and scale < 100:
                            # Left lightened by an earlier run (the driver's watchdog would restore it anyway)
                            self._write_scale(now, 100)
            self._publish(None, force=True)
        enabled = self._enabled_req
        if enabled != self._enabled:
            self._enabled = enabled
            self._publish(None, force=True)

    def _dyn_apply(self):
        stage = self._dyn
        if stage is not None:
            stage.column_on, stage.align_pref, stage.removal = self._dyn_req
            if self._wheel_req is not stage.result:
                stage.set_wheel_result(self._wheel_req)

    def _dyn_start(self, t):
        """Playing ACR's force begins: the calibrated wheel to the driver and the spring's limits read."""
        stage = self._dyn
        if stage is not None:
            stage.start(t)

    def _wheel_position(self, t, stage):
        """(degrees from the centre in the driver's raw frame, converted): the wheel's position now (read once per
        time `t`), None when it can't be read; `converted` False when a bent (sensitivity) position has no raw
        frame to be taken back to."""
        cache = self._pos_cache
        if cache is not None and cache[0] == t and cache[2] == stage.range:
            return cache[1], cache[3]
        measured, converted = None, True
        if self._pos_src is not None and stage.range:
            try:
                raw = self._pos_src()
            except Exception:
                raw = None
            if raw is not None:
                half = stage.range / 2.0
                measured = raw / 65535.0 * stage.range - half
                sens = stage.sensitivity
                if sens is not None and sens != ffb_dynamics.SENSITIVITY_LINEAR:
                    unbent = ffb_dynamics.sensitivity_inverse(measured / half, sens)
                    measured, converted = (None, False) if unbent is None else (unbent * half, True)
        self._pos_cache = (t, measured, stage.range, converted)
        return measured, converted

    def _frame_ok(self, t, sample, stage):
        """The wheel's measured position follows the game's steering (FrameCheck), judged from the moving samples
        while the spring is off; False (and a note, a log line once) when it does not or cannot be seen."""
        check = self._frame
        if stage.range != self._frame_range:
            self._frame_range = stage.range
            check.reset()                # a new range: the window says nothing about it
        # The driver's spring works on the RAW position, the evdev position is bent by the sensitivity curve: the
        # measured position is taken back to raw (the spring's frame); a bent frame must also be linear to be trusted
        sens = stage.sensitivity
        if sens != self._frame_sens:
            self._frame_sens = sens
            check.reset()
            if sens not in (None, ffb_dynamics.SENSITIVITY_LINEAR):
                logging.info("Countersteer FFB: the wheel's sensitivity is %s: the position is taken back to the raw "
                             "frame for the aligning spring", sens)
        bent = sens is not None and sens != ffb_dynamics.SENSITIVITY_LINEAR
        check.linear = bent
        steer = getattr(sample, 'steer', None)
        if steer is not None and math.isfinite(steer) and abs(steer) < RackForce.LOCK_STEER \
                and (sample.speed or 0.0) > MIN_SPEED:      # at the lock the wheel goes past the game's: no pair
            half = (stage.range or 0.0) / 2.0
            measured, converted = self._wheel_position(t, stage)
            if not converted:
                check.reset()
            elif measured is not None:
                check.feed(t, steer, -steer * half, measured)
        if self._pos_src is None:
            self.align_note = _("wheel position unavailable: the aligning spring stays off")
        elif check.failed():
            reason = check.failed()
            self.align_note = _("wheel position doesn't match the game's steering ({}): the aligning spring is off").format(reason)
            if reason.split()[0] not in check.reasons_logged:
                check.reasons_logged.add(reason.split()[0])
                logging.warning("Countersteer FFB: wheel position doesn't match the game's steering: %s; the aligning "
                                "spring stays off", reason)
        else:
            self.align_note = None
        return check.ok and self._pos_src is not None

    def _dyn_play(self, t, sample):
        """The driver's virtual steering dynamics beside ACR's force: the car's column damping and friction, and the
        local aligning spring (the model plays the residual) once the car's ratio and lock are known and the fit is
        ready, the driver reports a spring and the wheel's range is known."""
        stage = self._dyn
        rbr = self._played_mode == 'rbr'
        fz = self._played_mode == 'forza-rack'
        model = self.rbr_rack if rbr else self.forza_rack if fz else self.model
        if stage is None or not stage.usable() or self._played_mode not in ('acr', 'rbr', 'forza-rack'):
            model.align_on = False
            return
        stage.poll(t)
        if stage.k_limit is not None:
            model.align_k_limit = stage.k_limit      # (the model's k already carries the strength)
        model.wheel_range = stage.range
        slope = self._frame.slope
        model.wheel_slope = slope if self._frame.ok and slope else 1.0
        model.wheel_lock_deg = slope * stage.range / 2.0 if self._frame.ok and slope and stage.range else None
        if self._frame.ok and slope and not self._slope_logged and abs(slope - 1.0) > 0.05:
            self._slope_logged = True
            logging.info("Countersteer FFB: wheel mapping: %.2f x the game's steering", slope)
        # (align_ready includes the rack force's own gate: the game's raw force agrees with our sign)
        # (the check is fed whatever the ratio gate says: the slope serves the lock stop as well)
        frame_ok = self._frame_ok(t, sample, stage) if stage.can_align() else False
        if stage.can_align() and self.align_note is None and not (model.ratio_known and model.lock_known):
            self.align_note = _("steering lock not learnt yet: the aligning spring is off")
        if self.align_note is None and model._boost < 1.0:
            self.align_note = _("engine off: unassisted steering")
        want = stage.can_align() and model.align_ready() and frame_ok \
            and self._w >= 1.0 and not self._handback      # (while gliding between the levels the spring would add its own)
        if rbr or fz:
            # RBR's and Forza's slip is worked out from the sample: it must be a live one (the level may outlast the telemetry)
            fed = model._fed_at if rbr else model._last
            want = want and fed is not None and 0.0 <= t - fed <= model.ALIGN_FRESH
        why = None if want else next((name for name, ok in (
            ('the driver or the profile', stage.can_align()),
            ('car or fit not ready: %s' % {k: getattr(model, k, None) for k in ('car', 'ratio_known', 'lock_known', 'ready', 'spring_on')}, model.align_ready()),
            ('wheel position check: %s' % self._frame.reason, frame_ok), ('glide', self._w >= 1.0),
            ('hand-back', not self._handback)) if not ok), 'telemetry not fresh')
        if why != self._align_why:
            self._align_why = why                # (each change once: a spring that never plays must show in the log)
            logging.info("Countersteer FFB: aligning spring %s", 'on' if why is None else 'off: ' + why)
        if model.align_on and not want:
            stage.align_off(t)
        if want and not model.align_on:
            model.align_writer = self._align_write
        model.align_on = want
        relief = self._fast_spin_relief and self._gear_drive == 'gear' and not model.classic()
        stage.column(t, model.impedance(), model.wheel_peak, model.rho() ** 2 if relief else model.damping_scale(),
                     not model.classic() and not relief, ffb_carprops.rack_preload() * model.rho())

    def _align_write(self, theta_a_deg, k_fs, tau_fs):
        """RackForce's spring to the driver (its k already carries the overall steering strength, like the rest of the
        force)."""
        stage = self._dyn
        if stage is not None:
            stage.align(self._last_t if self._last_t is not None else self._clock(), theta_a_deg, k_fs, tau_fs)

    def _dyn_stop(self, t):
        """Nothing plays: the stage's targets back to 0 (the driver slews them out) and the split off."""
        self.model.align_on = self.rbr_rack.align_on = False
        stage = self._dyn
        if stage is not None:
            stage.stop(t)

    def _dyn_record(self, now):
        """While an FFB tuning capture runs: the driver's hands observer, re-identified friction and thermal state,
        about 20 times a second, for the offline replay; game_ffb_level and game_const_scale as the driver holds them,
        on change and at least once a second."""
        if not ffb_record.active():
            self._rec_levels.clear()
            return
        if self._rec_at is not None and 0.0 <= now - self._rec_at < 0.05:
            return
        self._rec_at = now
        driver = self._driver
        if driver is not None:
            self._rec_level(now, 'game_ffb_level', driver.game_level)
            if self._fh_scale:
                self._rec_level(now, 'game_const_scale', driver.const_scale)
        stage = self._dyn
        if stage is None:
            return
        dyn = stage.dyn
        hands, live = dyn.hands(), dyn.live()
        thermal = dyn.numbers('ffb_thermal', 3)
        if hands is not None:
            ffb_record.log(now, 'ffb_hands', *hands)
        if live is not None:
            ffb_record.log(now, 'ffb_dyn_live', *live)
        if thermal is not None:
            ffb_record.log(now, 'ffb_thermal', *thermal)

    def _rec_level(self, now, channel, read):
        """One driver percentage into the FFB tuning record: when it differs from the last one logged, else once a second."""
        value = read()
        if value is None:
            return
        last = self._rec_levels.get(channel)
        if last is None or last[0] != value or not 0.0 <= now - last[1] < 1.0:
            self._rec_levels[channel] = (value, now)
            ffb_record.log(now, channel, value)

    def _raw(self, t, fresh=RAW_FRESH, stops=False):
        """(level, new): the game's constant force level now, or None (none
        sent within `fresh` s, two effects, no attribute); `new`: uploaded
        since the last sample. `stops` (the fits): a level of 0 is None too.
        The driver publishes 0 with a count step, as for an upload, when the
        constant effect stops: no game force, not a force of 0 to fit
        against the car's (an upload of exactly 0 looks the same and is left
        out with it)."""
        driver = self._driver
        if driver is None or not self.classifier.has_game_ffb_raw:
            return None, False
        try:
            level, count, effect = driver.read_raw()
            read_at = time.monotonic()
            self._raw_reads += 1
            if self._raw_wake_at is not None:
                self._raw_lags.append(read_at - self._raw_wake_at)
                self._raw_wake_at = None
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
            if ffb_record.current is not None:
                ffb_record.log(t, 'game_ffb_raw', level, count, effect)
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
        if t - self._raw_at > fresh:
            return None, False
        if self._raw_id_at is not None and t - self._raw_id_at < RAW_TWO_EFFECTS:
            return None, False
        if stops and level == 0:
            return None, False
        return level, new

    def _watchdog(self, now):
        """The driver's own watchdog gives the game its force back after 1 s without an ext_force write and then
        blocks ext_force until game_ffb_level is written again: a gap that long while 'on', or game_ffb_level
        read back as 100 while we hold the game muted, is a release (and a new take-over writes it 0 again)."""
        if self._phase == 'on' and not (self._played_mode == 'forza' and self._fh_scale) and self._written_at is not None \
                and now - self._written_at > WRITE_GAP:
            self._release('gap', now)
            return
        if self._muted and self._phase in ('in', 'on') and self._driver is not None \
                and (self._level_at is None or now - self._level_at >= LEVEL_CHECK):
            self._level_at = now
            if self._driver.game_level() == 100:
                logging.warning("Countersteer FFB: game_ffb_level read back 100: the driver gave the game its force back")
                self._release('level', now)

    def _sample(self, slot, now=None):
        seq, t, sample = slot
        arrivals = seq - self._seen      # the ones skipped arrived too: the rate counts them
        self.skipped += arrivals - 1
        self._seen, self._last_t = seq, t
        self._lag = 0.0 if now is None else max(now - t, 0.0)
        self.classifier.feed(t, sample, arrivals)
        self._mode = self._mode_of(sample.game, t)
        self._mode_at = t
        if self._mode != 'acr':
            if self._mode == 'forza':
                self.addon.feed(t, sample, self._fh_fresh)
            if self._mode == 'forza-rack':
                self._fz_sample = sample
                self.forza_filter.packet(t)
                self.forza_rack.feed(t, sample, self._fh_fresh)
            if self._mode == 'rbr':
                last = self._rbr_sample
                if last is not None and (sample.stage != last.stage or (
                        sample.game_time is not None and last.game_time is not None
                        and sample.game_time < last.game_time)):
                    self._rbr_new_stream('a new stage or a restart')
                self._rbr_sample = sample
                self.rbr.set_car(sample.car, sample.stage, now=t)
                self.rbr_rack.set_car(sample.car)
                self.rbr_rack.feed(t, sample, self._rbr_now)
                self.rbr.feed_physics(t, sample, self._rbr_now)
            if self._phase != 'idle' and self._played_mode != self._mode:
                self._release('state', t)        # another game's force was playing
            return                               # its force is taken in step() after this
        if self._phase != 'idle' and self._played_mode != 'acr':
            self._release('state', t)
        self.model.feed(t, sample, *self._raw(t, stops=True))
        playing = self._phase in ('in', 'on')
        verdict = self._verdict = self.classifier.verdict(t, holding=playing)
        fit = self._enabled and self._available and not self._broken and self.model.ready
        want = fit and verdict.state == 'replace'
        if playing:
            if want:
                self._soft_since = None
                self._handback = False
            elif fit and verdict.reason in SOFT_REASONS:
                # a soft verdict (a gap, a rate dip) must last SOFT_HOLD before ours is handed back, and then
                # glides down to the game's level first; the rest (a refused fit, no Mz, off, ...) hands back at once
                if self._soft_since is None:
                    self._soft_since = t
                if t - self._soft_since >= SOFT_HOLD:
                    self._handback = True
            else:
                self._fade_out(t, 'off' if not self._enabled else 'state',
                               self._why(verdict) if self._enabled else None)
        elif self._phase == 'idle' and want and (self._released_at is None or t - self._released_at >= REARM) \
                and (self._backoff_until is None or t >= self._backoff_until):
            self._activate(t)
        if self._phase in ('in', 'on') and self.model.ready:
            self.model.hold()                    # what plays: kept through the fade out if a new car resets the fit
        elif self._phase == 'idle':
            self.model.release()
        if self._phase != 'idle':
            self._play(t, sample)
            if self._handback and self._w <= 0.0 and self._phase in ('in', 'on'):
                self._fade_out(t, 'state', self._why(verdict), soft=True)
        self._publish(t)

    def _why(self, verdict):
        """What the classifier (or the fit) said, with its numbers, for the release log line."""
        reason = verdict.reason
        if verdict.state == 'replace' or reason == 'ok':
            m = self.model
            return 'fit: %s' % (m.refused or m.waiting or 'not ready')
        if reason == 'gaps':
            return 'gaps %.0f ms' % (verdict.gap_ms or 0.0)
        if reason == 'low-rate':
            return 'rate %.0f/s' % verdict.rate
        return reason

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

    def _glide(self, t):
        """The share of our model's force in what plays: 0 (the game's level) at a take-over, up to 1 over GLIDE_IN;
        down to 0 over GLIDE_OUT while a planned handback is under way."""
        last, self._glide_at = self._glide_at, t
        if last is not None:
            dt = min(max(t - last, 0.0), 0.1)
            if self._handback:
                self._w = max(self._w - dt / GLIDE_OUT, 0.0)
            else:
                self._w = min(self._w + dt / GLIDE_IN, 1.0)
        return self._w

    PROV_TOL = 0.10                      # the live slope may differ from the stored one by this share

    def _lock_slope(self, stage):
        """The wheel mapping the lock stop uses: the live FrameCheck's once it passes, else the one stored for this car,
        wheel range and sensitivity (provisional: the aligning spring still waits for the live check). A live slope that
        disagrees with the stored one (sign, or beyond PROV_TOL) drops the stored one (logged once); the stop then waits
        for the live slope. A passing live slope is stored."""
        car = self.model.car
        key = (car, stage.range, stage.sensitivity)
        if key != self._prov_key:
            self._prov_key = key
            self._prov = ffb_carprops.STORE.get_frame(car, stage.range, stage.sensitivity)
        check = self._frame
        live = check.slope if check.ok and check.slope else None
        if live is None:
            return self._prov
        prov = self._prov
        if prov is not None:
            self._prov = None
            if live * prov <= 0.0 or abs(live / prov - 1.0) > self.PROV_TOL:
                ffb_carprops.STORE.set_frame(car, stage.range, stage.sensitivity, None)
                logging.info("Countersteer FFB: the wheel's measured mapping %.2f disagrees with the stored %.2f for this "
                             "car and wheel setup: stored value dropped", live, prov)
        saved = self._prov_saved
        if saved is None or saved[0] != key or abs(live / saved[1] - 1.0) > 0.02:
            self._prov_saved = (key, live)
            ffb_carprops.STORE.set_frame(car, stage.range, stage.sensitivity, live)
        return live

    def _lock_stop_force(self, t, sample):
        """The lock stop's push (LockStop), at the overall strength and inside the rim ceiling; 0 when the wheel's
        position can't be read or the game is not ACR or RBR (RBR: at the wheel's range / 2)."""
        stop, stage = self._lock_stop, self._dyn
        mode = self._played_mode
        rbr = mode == 'rbr'
        if mode not in ('acr', 'rbr', 'forza-rack') or stage is None or not stage.range:
            stop.reset()
            return 0.0
        pos, _converted = self._wheel_position(t, stage)
        steer = getattr(sample, 'steer', None)
        if rbr:
            lock_deg = stage.range / 2.0       # RBR maps the whole wheel range to steer +-1
        elif mode == 'forza-rack':
            # Forza maps the whole wheel range to steer +-1 too; the stop acts from the wheel's own position once the game's
            # input is at its lock (|steer| >= FZ_LOCK_STEER), from that share of the range
            lock_deg = stage.range / 2.0 * FZ_LOCK_STEER if steer is not None and abs(steer) >= FZ_LOCK_STEER else None
        else:
            slope = self._lock_slope(stage)
            lock_deg = None if slope is None else stage.range / 2.0 * slope     # (at or past the range: the wheel's own stop)
        push = stop.force(t, steer, pos, lock_deg) * self._strength
        limit = FULL * {'rbr': self.rbr_rack, 'forza-rack': self.forza_rack}.get(mode, self.model)._ceiling()
        return min(max(push, -limit), limit)

    def _game_level(self, sample):
        """The game's own force now in ext_force units as the fit has it (the scale times the front torque)."""
        torque = self.model._torque(sample)
        if torque is None or not math.isfinite(torque):
            return 0.0
        return min(max(self.model.scale * torque, -FULL), FULL)

    def _play(self, t, sample):
        self._dyn_play(t, sample)
        self.model.strength = self._strength      # (inside the rim mapping: the steering force is not scaled again)
        base = self.model.force(sample)
        self._hand_note(self.model, sample, base, t)
        ours = base + self._lock_stop_force(t, sample)
        # The texture on top (0.0 exactly when off: the steering force then goes out unchanged)
        texture = self.texture.force(t, sample, ours)
        w = self._glide(t)
        if w >= 1.0:
            self._out(t, ours + texture)
        else:
            game = self._game_level(sample)
            self._out(t, game + w * (ours - game) + w * texture)

    def _hand_note(self, rack, sample, base, t):
        """Count this sample's played force (`base`, ext_force units) and the car's model force in the drive's hand-force
        quantiles (ffb_handforce): both as the force at the wheel's rim."""
        last, self._hand_t = self._hand_t, t
        if last is None or sample is None or not 0.0 < t - last < 0.5:
            return
        case = ffb_handforce.sample_case(sample, MIN_SPEED)
        if case is None or rack.last_torque is None:
            return
        peak = rack.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
        self._hand.add(case, base / FULL * peak, rack.last_torque * rack.rho(), rack.rim_mm(), t - last)

    def _takeover_extra(self):
        """(the assist class, the previous drive's hand-force summary or '') for the take-over log lines."""
        return ffb_carprops.power_class(), self._gear_drive, self._gear_note(), '; last drive: ' + self._hand_last if self._hand_last else ''

    def _delay_inputs(self):
        """(settings, wheel) of ffb_delay.estimated_total_ms as they play now."""
        stage, rack = self._dyn, self.rbr_rack
        return ({'gear_guard': self._gear_on, 'gear_level': self._gear_level, 'gear_flip': self._gear_flip, 'friction_removal': bool(stage is not None and stage.removal),
                 'rbr_cut_hz': rack.cut_hz, 'fh_rate': getattr(self._verdict, 'rate', None)},
                {'drive': self._gear_drive, 'fit_latency_ms': (self._wheel_req or {}).get('fit_latency_ms')})

    def _gear_note(self):
        """The take-over line's gear guard and delay figures."""
        ms = ffb_delay.estimated_total_ms(self._mode, *self._delay_inputs())
        return '%s, a big force reversal %s%s' % ('%.0f %%' % self._gear_level if self._gear_on else 'off', ffb_delay.text(ms),
                                                  ' (over the %.0f ms soft limit)' % ffb_delay.DELAY_SOFT_MS if ffb_delay.over_soft(ms) else '')

    def _hand_report(self):
        """' ; hand force ...' for the drive just ended (once: the summary starts over), '' when nothing was counted."""
        text = self._hand.take()
        self._hand_t = None
        if text:
            self._hand_last = text
            return '; ' + text
        return ''

    def _shape(self, t, force):
        """Spike gate and slew on the force (before the fade). A jump of over SPIKE of full scale from the last
        value is held back one sample: it passes only when the next sample is near it too, so a one-sample outlier
        (a glitch in the telemetry, a bad packet) never reaches the wheel while a real jump (a kerb strike, a snap
        of oversteer) passes at its second sample. What passes then moves at most SLEW (full scale in 10 ms)."""
        last = self._smooth
        if last is None:
            self._smooth, self._smooth_at, self._spike = force, t, None
            return force
        if abs(force - last) > SPIKE * FULL:
            held = self._spike
            self._spike = force
            if held is None or abs(force - held) > SPIKE * FULL:
                force = last                 # the first of a jump (or one that is not where the last was): not yet
        else:
            self._spike = None
        dt = min(max(t - self._smooth_at, 0.0), 0.1)
        step = SLEW * dt
        out = last + min(max(force - last, -step), step)
        self._smooth, self._smooth_at = out, t
        return out

    def _out(self, t, force):
        """`force` (ext_force units, before the fade) clamped, faded and written."""
        g = self._gain(t)
        self._full_range(t)
        self._gear_cross()
        force = min(max(force, -FULL), FULL)
        compensate = self._played_mode != 'forza'
        if compensate:                       # the add-on's is added to the game's own force, which steps: it must too
            force = self._shape(t, force)
        # What is meant to be delivered; the driver multiplies ext_force by the user's master gain, so the value
        # written is divided by it (Countersteer's strength is the same at any gain; the game's own force is not)
        level = force * g * self._hold_gain(t)
        comp = self._master(t) if compensate else 1.0
        value = int(round(level / comp))
        saturated = abs(value) > FULL
        value = min(max(value, -FULL), FULL)
        prev_written = self._written_at
        self._written_at = t
        self._write_pending = False
        try:
            self._driver.force(value)
            self.writes += 1
            self._last_value = self._base_value = value
            mono = time.monotonic()
            if self._write_mono is not None and mono - self._write_mono > LATE_PASS and self._phase == 'on':
                gap = (mono - self._write_mono) * 1000.0
                if prev_written is not None and (self._arrived_at is None or self._arrived_at <= prev_written):
                    self._late(mono, "telemetry paused %.0f ms (the game sent nothing)" % gap, paused=True)
                else:
                    self._late(mono, "ext_force written %.0f ms after the last write (the pass before took %.0f ms)"
                               % (gap, self._pass_took * 1000.0))
            self._write_mono = mono if self._phase == 'on' else None
            if ffb_record.current is not None:
                ffb_record.log(t, 'ext_force', value)
            if self._cap_level is not None:
                over = saturated or abs(level) > self._cap_level or (not compensate and self._clip_at is not None and abs(value) > self._clip_at)
                self._clipped += (over - self._clipped) / 333.0       # about a second
        except OSError as e:
            logging.warning("Countersteer FFB: ext_force: %s", e)
            self._broken, self._available = True, False      # until the device is set again
            self._release('write', t)
            return
        if self._phase == 'out' and g <= 0.0:
            self._phase = 'idle'         # faded out (0 written last): the game has its force back
            self._let_go()

    def _range_wanted(self):
        """True while our force fully plays (after the glide-in, game muted, no handback under way)."""
        if self._phase != 'on' or not self._muted or self._handback or self._played_mode == 'forza':
            return False
        return self._played_mode == 'beamng' or self._w >= 1.0      # (BeamNG has no glide)

    def _full_range(self, t):
        """Full range: while our force fully plays, the driver's ext_force_cap is 100 % and its master gain at least
        100 % (the mapping tops out near 92 % of full scale, and min(|ext| * gain, cap) would clip it at the user's
        values). The compensation follows at once (no GAIN_TAU transient: what is delivered does not step). Restored
        before the glide-out and on every way out."""
        if self._range is not None:
            if not self._range_wanted():
                self._restore_range(refresh=self._driver is not None)
            return
        if self._range_failed or not self._range_wanted():
            return
        driver = self._driver
        try:
            cap, gain = driver.ext_force_cap(), driver.master_gain()
        except OSError:
            cap = gain = None
        new_cap = cap is not None and cap < 100
        new_gain = gain is not None and 0 < gain < 65535
        if not new_cap and not new_gain:
            self._range_failed = True            # already at 100 %, or unreadable (nothing could be restored)
            if cap is None and gain is None:
                logging.info("Countersteer FFB: full range: the driver's ext_force_cap and gain are unreadable; left as they are")
            return
        state = RangeState(cap if new_cap else None, 100 if new_cap else None,
                           gain if new_gain else None, 65535 if new_gain else None, gain)
        # Told to the host first: a process killed between the write and the report would leave the values raised
        if self.range_hook is not None:
            self.range_hook(self._path, state)
        self._range = state
        try:
            if new_cap:
                driver.set_ext_force_cap(100)
            if new_gain:
                driver.set_master_gain(65535)
        except OSError as e:
            logging.warning("Countersteer FFB: full range: %s", e)
            self._range_failed = True
            self._restore_range(refresh=True)
            return
        if new_gain:
            state.gain_now = 65535
        self._read_gains()                   # the compensation, the clip point, RBR's ceiling: now, with no transient
        logging.info("Countersteer FFB: full range: cap %s -> 100, gain %s -> 100, restored at hand-back",
                     '?' if cap is None else cap, '?' if gain is None else '%.0f' % (gain * 100.0 / 65535.0))

    def _gear_wanted(self):
        """What ffb_gear_cross should read while our force fully plays on a gear drive with the guard on, else None."""
        if not self._gear_on or self._gear_drive != 'gear' or not self._range_wanted() or self._gear_level <= 0.0:
            return None
        stage = self._dyn
        return ffb_gear.text(self._gear_level, bool(stage is not None and stage.removal), self._gear_flip)

    def _gear_cross(self):
        """The driver's gear-crossing stage: on for as long as our force fully plays (a change of the slider follows), off
        before the glide-out and on every way out, like full range. A driver without it: no guard, one log line."""
        want = self._gear_wanted()
        if want is None:
            self._gear_restore()
            return
        if want == self._gear_written or self._gear_failed:
            return
        driver = self._driver
        if driver is None:
            return
        if not driver.has_gear_cross():
            self._gear_failed = True
            logging.info("Countersteer FFB: gear guard: this driver has no ffb_gear_cross: no guard (update new-lg4ff)")
            return
        # Told to the host first: a process killed between the write and the report would leave it on
        if self.gear_hook is not None and self._gear_written is None:
            self.gear_hook(self._path, True)
        first = self._gear_written is None
        self._gear_written = want
        try:
            driver.set_gear_cross(want)
        except OSError as e:
            logging.warning("Countersteer FFB: gear guard: %s", e)
            self._gear_failed = True
            self._gear_restore()
            return
        if first:
            logging.info("Countersteer FFB: gear guard: ffb_gear_cross %s, off at hand-back", want)

    def _gear_restore(self):
        """ffb_gear_cross off, if we had it on; safe twice. Logs how much of the drive the stage covered."""
        if self._gear_written is None:
            return
        self._gear_written = None
        driver = self._driver
        if driver is not None:
            try:
                state = driver.gear_cross_state()
                if state is not None and state[4] > 0:
                    logging.info("Countersteer FFB: gear guard off; the driver's bypass had it out for %.1f %% of its ticks so far",
                                 state[5] * 100.0 / state[4])
                driver.set_gear_cross(ffb_gear.OFF)
            except OSError as e:
                logging.warning("Countersteer FFB: gear guard off: %s", e)
        if self.gear_hook is not None:
            self.gear_hook(self._path, False)

    def _adopt_gain(self, raw):
        """The user changed the master gain while we play (the GUI's apply): that is the original now, and ours is
        asserted again. Returns the gain the compensation should use."""
        state = self._range
        if raw < 65535:
            state.gain_orig, state.gain_ours = raw, 65535
        else:
            state.gain_orig = state.gain_ours = None
        state.gain_now = 65535 if raw < 65535 else raw
        logging.info("Countersteer FFB: full range: the master gain was set to %.0f %%; that is the original now",
                     raw * 100.0 / 65535.0)
        if self.range_hook is not None:
            self.range_hook(self._path, state)
        if raw < 65535:
            try:
                self._driver.set_master_gain(65535)
            except OSError as e:
                logging.warning("Countersteer FFB: full range: gain: %s", e)
                state.gain_now = raw
                return raw
            return 65535
        return raw

    def _restore_range(self, refresh=False):
        """Cap and gain back to the originals (each only where still ours), safe twice. `refresh`: our force is still
        playing or fading out, so the compensation follows at once."""
        state, self._range = self._range, None
        if state is None:
            return
        driver = self._driver
        if driver is not None:
            apply_restore(driver, state)
        if self.range_hook is not None:
            self.range_hook(self._path, None)
        if refresh and driver is not None:
            self._read_gains()
            logging.info("Countersteer FFB: full range: cap and gain back to the user's")

    def _hold_gain(self, t):
        """1.0, or the share the easing in a gap left, coming back to 1 over HOLD_RECOVER once packets return."""
        f = self._hold_f
        if f >= 1.0:
            return 1.0
        last, self._hold_at = self._hold_at, t
        if last is not None:
            f = self._hold_f = min(f + max(t - last, 0.0) / HOLD_RECOVER * (1.0 - HOLD_FLOOR), 1.0)
        return f

    def _master(self, t):
        """The driver's master gain as a share of 100 %, read every GAIN_POLL s and followed with GAIN_TAU (1.0
        when unreadable or 0)."""
        if self._gain_at is None or t < self._gain_at or t - self._gain_at >= GAIN_POLL:
            self._gain_at = t
            try:
                raw = self._driver.master_gain()
            except OSError:
                raw = None
            if self._range is not None and raw is not None and raw != self._range.gain_now:
                raw = self._adopt_gain(raw)
            self._gain_seen = raw / 65535.0 if raw else None
        want = self._gain_seen or 1.0
        last = self._comp_at
        self._comp_at = t
        if last is None or not 0.0 < t - last <= 1.0:
            if last is None:
                self._comp = want
            return self._comp
        dt = t - last
        self._comp += (want - self._comp) * dt / (GAIN_TAU + dt)
        return self._comp

    def _rewrite(self, now):
        """The last value again (a gap between ACR packets; STALL ends it). Past HOLD_DECAY_FROM of silence it eases
        down to HOLD_FLOOR of the value over HOLD_DECAY (a game hitch: held, not dropped)."""
        silent = 0.0 if self._arrived_at is None else now - self._arrived_at
        f = 1.0 - (1.0 - HOLD_FLOOR) * min(max(silent - HOLD_DECAY_FROM, 0.0) / HOLD_DECAY, 1.0)
        value = int(round(self._base_value * f))
        if f < 1.0:
            self._hold_f, self._hold_at = f, None
        try:
            self._driver.force(value)
            self._last_value = value
            self.writes += 1
            self._written_at = now
            self._write_mono = time.monotonic() if self._phase == 'on' else None   # (the late-write check measures every write)
            if ffb_record.current is not None:
                ffb_record.log(now, 'ext_force', value)
            if self._dyn is not None:
                self._dyn.keep(now)          # the spring is held too: the driver drops one not written for 20 ms
        except OSError as e:
            logging.warning("Countersteer FFB: ext_force: %s", e)
            self._broken, self._available = True, False
            self._release('write', now)

    def _idle(self, now):
        seen = self._arrived_at if self._arrived_at is not None else self._last_t
        if self._phase != 'idle' and self._played_mode == 'acr' and seen is not None and now - seen > STALL:
            self._release('stall', now)
        elif self._phase != 'idle' and self._played_mode == 'acr' and not self._broken and self._written_at is not None \
                and now - self._written_at >= KEEP_WRITE:
            self._rewrite(now)           # the driver fades a value from 30 ms: HOLD_GAP lets samples come 30-50 ms apart
        if self._mode == 'acr':
            self._verdict = self.classifier.verdict(now, holding=self._phase in ('in', 'on'))
            self._publish(now)

    # -- RBR and BeamNG: their force is read each pass, not per telemetry sample --

    def _mode_of(self, game, t):
        if game == 'rbr':
            return 'rbr'
        if game == 'forza-fh':
            return 'forza-rack' if self._forza_model else 'forza'
        if game == 'beamng':
            self._beamng_at = t
            return 'beamng'
        if game not in AC_GAMES and self._source is not None and self._source.latest(t) is not None:
            return 'beamng'
        return 'acr'

    def _model(self):
        return {'rbr': self.rbr, 'beamng': self.beamng, 'forza': self.addon, 'forza-rack': self.forza_rack}.get(self._mode,
                                                                                                                self.model)

    def _sources(self, now):
        """BeamNGForceSource runs while there is a wheel and BeamNG was heard within NONE_AFTER: started by its
        OutGauge (nothing listens for CSB1 before), then kept by OutGauge or CSB1; stopped otherwise."""
        src = self._source
        if src is not None and src.latest(now) is not None:
            self._beamng_at = now
            if self._mode == 'acr' and self.classifier.game not in AC_GAMES + ('rbr',):
                self._mode = 'beamng'            # CSB1 without OutGauge
            if self._mode == 'beamng':
                self._mode_at = now
        heard = self._beamng_at is not None and now - self._beamng_at <= NONE_AFTER
        want = self._path is not None and not self._closed and heard
        if want and src is None:
            if self._source_failed_at is None or now - self._source_failed_at >= SOURCE_RETRY:
                src = self._source_type()
                if src.start():
                    self._source, self._source_failed_at, self._csb_seq = src, None, src.seq
                    self._csb.clear()
                    logging.info("Countersteer FFB: listening for the BeamNG mod's rack force")
                else:
                    self._source_failed_at = now
        elif not want and src is not None:
            self._stop_source()

    def _stop_source(self):
        src, self._source = self._source, None
        if src is not None:
            src.stop()
            logging.info("Countersteer FFB: stopped listening for the BeamNG mod")
        self._csb.clear()
        self._csb_sample = None

    def _decide(self, now, playing, want, reason):
        if playing and not want:
            self._fade_out(now, 'off' if not self._enabled else reason)
        elif self._phase == 'idle' and want:
            rearm = FH_REARM if self._mode == 'forza' else REARM
            if self._released_at is None or now - self._released_at >= rearm:
                self._activate(now)

    def _write_due(self, now, new):
        """RBR and BeamNG write on a new value (kept pending if it came within MIN_WRITE of the last write), every
        pass of a fade, and every KEEP_WRITE; never twice in MIN_WRITE."""
        if new:
            self._write_pending = True
        if self._written_at is None:
            return True
        since = now - self._written_at
        if since < MIN_WRITE:
            return False
        return self._write_pending or self._phase != 'on' or since >= KEEP_WRITE

    def _regains(self, now):
        """app_gain read again about once a second while RBR or BeamNG may play (it is the driver's setting)."""
        if self._driver is None or not self._available:
            return
        if self._gains_at is None or now - self._gains_at >= REGAINS:
            self._gains_at = now
            self._app_gain = bool(self._driver.app_gain())

    def _texture_for(self, mode, t):
        """The texture on top of BeamNG's force: none yet (the hook for its wheels). RBR's is stepped in _rbr_play."""
        return 0.0

    def _rbr_play(self, now):
        """RBR's force: the column torque of the game's level through the chain (RbrRack.force: boost curve, hand-force
        mapping), the lock stop and the texture on top, glided in from the game's own level (the raw, in ext_force's
        units) after a take-over."""
        sample, rack = self._rbr_sample, self.rbr_rack
        self._dyn_play(now, sample)
        rack.strength = self._strength
        rack.play_t = now
        base = rack.force(sample)
        if sample is not None:
            self._hand_note(rack, sample, base, now)
        ours = base + self._lock_stop_force(now, sample)
        if sample is not None and sample is not self._rbr_tex_sample:      # the texture steps with the telemetry
            self._rbr_tex_sample = sample
            self._rbr_tex = self.texture.force(self._last_t if self._last_t is not None else now, sample, ours)
        w = self._glide(now)
        game = self._rbr_level or 0
        self._out(now, game + w * (ours - game) + w * self._rbr_tex)

    def _rbr_new_stream(self, why):
        """RBR's stream started again: the game's peak is measured again and so is the sign of its DIRECTION."""
        self.rbr.new_stream(why)
        self.rbr_rack.restart_sign()

    def _rbr_tick(self, now):
        before, last_new = self._raw_count, self._rbr_new_at
        level, new = self._raw(now)
        playing = self._phase in ('in', 'on')
        if new:
            self._rbr_new_at = now
            count = self._raw_count
            if (before is not None and count is not None and count < before) or \
                    (last_new is not None and now - last_new > RBR_STREAM_GAP):
                self._rbr_new_stream('the uploads restarted')     # the wheel bound again, or the game away
        self._regains(now)
        if level is not None:
            self._rbr_level = level
            if new:
                self.rbr.feed(now, level, holding=playing)
                self.rbr_rack.set_level(now, level, self.rbr.s, self.rbr.strength)
        self._rbr_now = level
        self.classifier.raw_rate = self.raw_rate
        v = self.classifier.verdict(now, holding=playing)
        if v.state == 'replace' and (self.rbr.refused or self.rbr_rack.refused):
            v = Verdict('model', 'rbr-' + (self.rbr.refused or self.rbr_rack.refused), v.game, v.rate, v.gap_ms)
        self._verdict = v
        want = (self._enabled and self._available and v.state == 'replace' and self.rbr.ready
                and self.rbr_rack.ready and level is not None)
        why = None if want else (self.rbr.refused or self.rbr_rack.refused or (
            v.reason if v.state != 'replace' else None) or self.rbr.waiting or self.rbr_rack.waiting
            or ('no fresh raw level' if level is None else None))
        if self._enabled and why != self._rbr_why:
            self._rbr_why = why
            logging.info("Countersteer FFB: RBR: %s", "waiting/refused: " + why if why else "ready to take over")
        self._decide(now, playing, want, 'raw' if level is None else 'state')
        if self._phase != 'idle' and self._played_mode == 'rbr':
            if self._write_due(now, new):
                self._rbr_play(now)
        self._publish(now)

    def _beamng_tick(self, now):
        src = self._source
        sample = src.latest(now) if src is not None else None
        fresh = sample is not None and src.seq != self._csb_seq
        playing = self._phase in ('in', 'on')
        if fresh:
            self._csb_seq = src.seq
            if self._csb_restarted(self._csb_sample, sample):
                # The mod's seq or the game's clock went back, or seq ran ahead of the clock: another vehicle,
                # or this one reset
                logging.info("Countersteer FFB: BeamNG: a new vehicle or a reset: learning its rack again")
                self.beamng.reset(self.beamng.car)
            self._csb.feed(now, sample)
            self._csb_sample = sample
            self.beamng.feed(now, sample, *self._raw(now, stops=True))
        self._regains(now)
        v = self._verdict = self._beamng_verdict(now, sample, playing)
        want = self._enabled and self._available and v.state == 'replace' and self.beamng.ready
        self._decide(now, playing, want, 'state')
        if self._phase in ('in', 'on') and self.beamng.ready:
            self.beamng.hold()                   # what plays: kept through the fade out if the fit is dropped
        elif self._phase == 'idle':
            self.beamng.release()
        if self._phase != 'idle' and self._played_mode == 'beamng':
            s = self._csb_sample
            if self._write_due(now, fresh):
                self._out(now, (self.beamng.force(s) + self.beamng.softlock(s)) * self._strength + self._texture_for('beamng', now))
        self._publish(now)

    def _forza_tick(self, now):
        """Forza Horizon's add-on: the game's constant force read each pass; game_const_scale = round(100 g) with
        each Forza sample (_scale_play), or on an older driver ext_force = (g - 1) x the game's level written on
        each new upload (and as g moves); the game's own level never touched."""
        rate = self.raw_rate
        fresh = min(max(FH_FRESH_UPLOADS / rate, FH_FRESH_MIN), RAW_FRESH) if rate else RAW_FRESH
        level, new = self._raw(now, fresh)
        addon = self.addon
        if new and level is not None:
            addon.upload(now, level)
        self._regains(now)
        self._fh_fresh = level
        if level is not None:
            self._fh_level = level
        g = addon.gain(now)
        stalled = self._last_t is None or now - self._last_t > FH_STALL
        if self._phase != 'idle' and self._played_mode == 'forza' and (level is None or stalled):
            # An effect stopped or erased publishes 0 (with a count step), but a held level says nothing of a
            # force that is gone when the game publishes nothing (Forza quit or out of focus): never played
            # against it, not even as a fade (with game_const_scale: 100 at once)
            self._cut(now, 'stall' if stalled else 'raw')
        playing = self._phase in ('in', 'on')
        v = self._verdict = self._forza_verdict(now, level, playing)
        want = (self._enabled and self._available and v.reason == 'fh-addon' and addon.ready
                and level is not None and not stalled)
        self._decide(now, playing, want, 'stall' if stalled else 'raw' if level is None else 'state')
        if self._phase != 'idle' and self._played_mode == 'forza':
            if self._fh_scale:
                self._scale_play(now, g)
            else:
                value = addon.force(self._fh_level or 0, g, addon.margin(now))
                if self._write_due(now, new or value != self._last_value):
                    self._out(now, value)
        self._publish(now)

    def _forza_rack_tick(self, now):
        """Forza Horizon through our chain (ForzaRack): the game's constant force is read each pass only to check ours
        against (fresh raw, the mismatch refusal) and as the level the take-over glides in from; our force is worked out
        for each Forza sample and played, filtered, every pass."""
        rate = self.raw_rate
        fresh = min(max(FH_FRESH_UPLOADS / rate, FH_FRESH_MIN), RAW_FRESH) if rate else RAW_FRESH
        level, new = self._raw(now, fresh)
        self._regains(now)
        self._fh_fresh = level
        if level is not None:
            self._fh_level = level
        playing = self._phase in ('in', 'on')
        stalled = self._last_t is None or now - self._last_t > FZ_STALL
        if playing and self._played_mode == 'forza-rack' and stalled:
            self._release('stall', now)
            playing = False
        v = self._verdict = self._forza_rack_verdict(now)
        want = self._enabled and self._available and v.state == 'replace' and self.forza_rack.ready and not stalled
        self._decide(now, playing, want, 'stall' if stalled else 'state')
        if self._phase != 'idle' and self._played_mode == 'forza-rack' and self._write_due(now, True):
            self._forza_rack_play(now)
        self._publish(now)

    def _forza_rack_verdict(self, now):
        v = self.classifier.verdict(now)
        if v.reason != 'fh-addon':
            return v                     # no telemetry, measuring, fh-rate: the classifier's

        def verdict(state, reason):
            return Verdict(state, reason, v.game, v.rate, v.gap_ms)
        if not self.classifier.has_game_ffb_raw:
            return verdict('add-on', 'old-driver')
        if self._app_gain:
            return verdict('model', 'fh-app-gain')        # the driver would scale the game's force but not ours
        rack = self.forza_rack
        if rack.refused:
            return verdict('model', 'forza-' + rack.refused)
        if not rack.ready:
            return verdict('model', 'forza-wait')
        return verdict('replace', 'ok')

    def _forza_rack_play(self, now):
        """ForzaRack's force: the share for the newest sample through the chain (RackForce.force), the 60/s steps rebuilt by
        the output filter (stepped each pass), then Kickback's pulse, the lock stop and the texture on top, glided in from the
        game's own level after a take-over."""
        sample, rack = self._fz_sample, self.forza_rack
        if sample is None:
            return
        self._dyn_play(now, sample)
        rack.strength = self._strength
        rack._now = now
        if sample is not self._fz_force_sample:
            self._fz_force_sample, self._fz_force = sample, rack.force(sample)
            self._hand_note(rack, sample, self._fz_force, now)
        limit = FULL * rack._ceiling()
        ours = self.forza_filter.step(now, self._fz_force) + rack.kick_force(sample)
        ours = min(max(ours, -limit), limit) + self._lock_stop_force(now, sample)
        texture = self.texture.force(now, sample, ours)
        w = self._glide(now)
        game = self._fh_level or 0
        self._out(now, game + w * (ours - game) + w * texture)

    def _scale_play(self, now, g):
        """game_const_scale = round(100 g), faded in and out toward 100 with the engine's fade: written with each
        new Forza sample, at least every FH_SCALE_KEEP s while under 100 (the driver's watchdog), and 100 when
        the fade out ends. The driver slews it (100 % per 50 ms), so steps at 60 a second never step the force.
        ext_force is never written."""
        fade = self._gain(now)
        ended = self._phase == 'out' and fade <= 0.0
        value = 100 if ended else min(max(int(round(100.0 * (1.0 - fade * (1.0 - min(max(g, 0.0), 1.0))))), 0),
                                      100)
        last, at = self._scale_last, self._scale_at
        sample = self._seen != self._scale_seen
        due = (ended or last is None or at is None or (sample and (value < 100 or value != last))
               or (value < 100 and now - at >= FH_SCALE_KEEP))
        if due:
            self._scale_seen = self._seen
            if not self._write_scale(now, value):
                self._broken, self._available = True, False      # until the device is set again
                self._release('write', now)
                return
        if ended:
            self._phase = 'idle'         # faded out (100 written last): the game has all its force back
            self._let_go()

    def _write_scale(self, now, value):
        """game_const_scale = value; False if the write failed (the driver's watchdog then restores 100 within
        0.8 s of the last write)."""
        try:
            self._driver.set_const_scale(value)
        except OSError as e:
            logging.warning("Countersteer FFB: game_const_scale %d: %s", value, e)
            return False
        self.scale_writes += 1
        self._scale_last, self._scale_at = value, now
        return True

    def _restore_scale(self, now):
        """Every way out: game_const_scale 100 at once if ours was written under it (a failed write leaves it to
        the driver's watchdog)."""
        if self._fh_scale and self._driver is not None and self._scale_last is not None \
                and self._scale_last != 100:
            self._write_scale(now, 100)

    def _cut(self, now, reason):
        """Forza's add-on: game_const_scale 100 (an older driver: ext_force 0) now, no fade, and released."""
        if self._fh_scale:
            if not self._write_scale(now, 100):
                self._broken, self._available = True, False     # the driver's watchdog restores it
                reason = 'write'
            self._release(reason, now)
            return
        try:
            self._driver.force(0)
            self.writes += 1
            self._last_value = 0
        except OSError as e:
            logging.warning("Countersteer FFB: ext_force: %s", e)
            self._broken, self._available = True, False
            reason = 'write'
        self._release(reason, now)

    def _forza_verdict(self, now, level, holding):
        v = self.classifier.verdict(now)
        if v.reason != 'fh-addon':
            return v

        def no(reason):
            return Verdict('add-on', reason, v.game, v.rate, v.gap_ms)
        addon = self.addon
        if not self.classifier.has_game_ffb_raw:
            return no('old-driver')
        if self._app_gain and not self._fh_scale:
            # The driver scales the game's force by its FF_GAIN and not ext_force: (g - 1) x raw could even
            # reverse it (game_const_scale scales the game's force itself, so FF_GAIN applies to both alike)
            return no('fh-app-gain')
        if addon.no_slip(now):
            return no('fh-no-slip')
        if self._raw_id_at is not None and now - self._raw_id_at < RAW_TWO_EFFECTS:
            return no('fh-two-effects')
        rate = self.raw_rate
        if self._raw_count is None or rate is None or rate < 1.0:
            # Driving with no constant force from the game: it drives the wheel through the spring and damper
            return no('fh-no-constant' if addon.moving(now) else 'drive')
        if rate < (FH_RAW_HOLD if holding else FH_RAW_RATE):
            return no('fh-raw-rate')
        if addon.refused:
            return no('fh-' + addon.refused)
        if level is None:
            return no('fh-raw-stale')
        return v

    @staticmethod
    def _csb_restarted(last, sample):
        if last is None or last.packet_id is None or sample.packet_id is None:
            return False
        dseq = sample.packet_id - last.packet_id
        dgt = (sample.game_time or 0.0) - (last.game_time or 0.0)
        return dseq < 0 or dgt < 0.0 or dseq > CSB_MAX_RATE * dgt + CSB_SEQ_SLACK

    def _beamng_verdict(self, now, sample, holding):
        tel = self.classifier.verdict(now)
        game = 'beamng'
        if sample is None:
            if self._source is None and self._source_failed_at is not None:
                return Verdict('add-on', 'beamng-port', game, tel.rate, tel.gap_ms)
            return Verdict('add-on', 'beamng-no-mod', game, tel.rate, tel.gap_ms)
        rate = self._csb.rate
        if not self._csb.advancing(now):
            return Verdict('add-on', 'beamng-paused', game, rate or 0.0, None)
        if rate is None:
            return Verdict('none', 'measuring', game, 0.0, None)
        if rate < (CSB_HOLD_RATE if holding else CSB_RATE):
            return Verdict('add-on', 'csb-rate', game, rate, None)
        if not self._csb.rack:
            return Verdict('add-on', 'no-rack', game, rate, None)
        if not self.classifier.has_game_ffb_raw:
            return Verdict('add-on', 'old-driver', game, rate, None)
        return Verdict('replace', 'ok', game, rate, None)

    def _activate(self, t):
        if self._closed:
            return
        if not wheel_owner.acquire(self._path, OWNER, exclusive=False, on_inhibit=self._on_inhibit):
            return                       # another owner has the wheel (status.held_by says who)
        self._owned = True
        if self._mode == 'forza':
            # An add-on: the game keeps its force at its own level (never lowered), ours fades in on top
            self._phase, self._fade_at, self._played_mode = 'in', t, 'forza'
            self._read_gains()
            a = self.addon
            logging.info("Countersteer FFB: lightening Forza Horizon's force through %s (%.0f/s, game uploads %s/s, "
                         "strength %.0f %%, r %s, raw %s with lateral acceleration to the left)",
                         'game_const_scale' if self._fh_scale else 'ext_force', self._verdict.rate,
                         'unknown' if self.raw_rate is None else '%.0f' % self.raw_rate, a.strength * 100.0,
                         'none' if a.fit is None else '%.2f' % a.fit,
                         'rising' if a.sign == 1.0 else 'falling' if a.sign == -1.0 else 'unclear')
            return
        try:
            self._driver.set_game_level(0)
        except OSError as e:
            logging.warning("Countersteer FFB: game_ffb_level: %s", e)
            self._broken, self._available = True, False
            self._safe_game_level(100)
            self._let_go()
            return
        self._muted = True
        self._range_failed = False
        self._gear_failed = False
        self._phase, self._fade_at = 'in', t
        self._played_mode = self._mode
        self._w, self._glide_at, self._handback, self._soft_since = 0.0, None, False, None
        self._hold_f, self._hold_at, self._taken_skipped = 1.0, None, self.skipped
        self._hand.reset()
        self._hand_t = None
        self._read_gains()
        if self._mode == 'rbr':
            k = self.rbr_rack
            p99 = k.tau_p99()
            logging.info("Countersteer FFB: replacing Richard Burns Rally's force with its column torque through our chain "
                         "(game uploads %s/s; car %s, S %s, column torque p99 %s Nm so far, hand-force scale %.2f "
                         "(%.0f/%.0f mm), 1:1 up to %.0f %%, column damping %.0f %%, RBR force cut %s, texture %s, countersteer_power_class %s, wheel drive %s, gear guard %s%s)",
                         'unknown' if self.raw_rate is None else '%.0f' % self.raw_rate, self.rbr.car,
                         'unknown (100 assumed)' if self.rbr.s is None else '%.1f' % self.rbr.s,
                         'none' if p99 is None else '%.1f' % p99, k.rho(), k.rim_mm(), k.car_wheel_mm,
                         k.knee * 100.0, k.damping_pct, 'off' if k.cut_hz is None else '%.0f Hz' % k.cut_hz,
                         self.texture.describe(), *self._takeover_extra())
            self._dyn_start(t)
            return
        if self._mode == 'forza-rack':
            k = self.forza_rack
            logging.info("Countersteer FFB: replacing Forza Horizon's force with our own from the car's motion through our chain "
                         "(%.0f packets/s, game uploads %s/s; car %s, mass %.0f kg (%s), hand-force scale %.2f (%.0f/%.0f mm), "
                         "1:1 up to %.0f %%, column damping %.0f %%, drive pull %s, kickback %s, texture %s, countersteer_power_class %s, wheel drive %s, gear guard %s%s)",
                         self._verdict.rate, 'unknown' if self.raw_rate is None else '%.0f' % self.raw_rate, k.car, k.mass,
                         'learnt' if k.mass_l.known else 'prior', k.rho(), k.rim_mm(), k.car_wheel_mm, k.knee * 100.0,
                         k.damping_pct, '%.0f %%' % (k.pull_level * 100.0) if k.pull_on else 'off',
                         '%.0f %%' % (k.kick_level * 100.0) if k.kick_on else 'off', self.texture.describe(),
                         *self._takeover_extra())
            self._dyn_start(t)
            return
        if self._mode == 'beamng':
            b = self.beamng
            logging.info("Countersteer FFB: replacing BeamNG's force with the mod's rack force (%s packets/s of game "
                         "time, game uploads %s/s, %.3f per N, sign %+.0f, r %.2f, centring %s)",
                         'unknown' if self._csb.rate is None else '%.0f' % self._csb.rate,
                         'unknown' if self.raw_rate is None else '%.0f' % self.raw_rate, b.scale, b.sign, b.fit,
                         'none' if b.centring is None else '%.2f' % b.centring)
            logging.info("Countersteer FFB: gear guard %s", self._gear_note())
            return
        m = self.model
        logging.info("Countersteer FFB: replacing %s's force (%.0f/s, game uploads %s/s, %.1f per Nm, r %.2f, "
                     "finalFF cross-check %s, centring hint %s, rim model %.1f:1 assist %.1f wheel %.1f Nm (car p99 %.0f Nm), "
                     "mapping %s, hand-force scale %.2f (%.0f/%.0f mm), 1:1 up to %.0f %%, compressed to a top of %.1f Nm, drive pull %s, kickback %s, column damping %.0f %%, texture %s%s, countersteer_power_class %s, wheel drive %s, gear guard %s%s)", self._verdict.game, self._verdict.rate,
                     'unknown' if self.raw_rate is None else '%.0f' % self.raw_rate, m.scale, m.fit,
                     'none' if m.cross is None else '%.2f' % m.cross,
                     'none' if m.centring is None else '%.2f' % m.centring,
                     m.ratio, m.assist, m.wheel_peak or wheel_torque.DEFAULT_PEAK_NM, m.p99,
                     m.mapping, m.rho(), m.rim_mm(), m.car_wheel_mm,
                     m.knee * 100.0, m.top_torque(),
                     '%.0f %%' % (m.pull_level * 100.0) if m.pull_on else 'off',
                     '%.0f %%' % (m.kick_level * 100.0) if m.kick_on else 'off',
                     m.damping_pct, self.texture.describe(),
                     "; app_gain on: the game's FF_GAIN scales its force but not ours" if self._app_gain else '',
                     *self._takeover_extra())
        self._dyn_start(t)

    def _read_gains(self):
        driver = self._driver
        self._app_gain = bool(driver.app_gain())
        gain, cap = driver.master_gain(), driver.ext_force_cap()
        # The driver: min(|ext| * gain / 65535, cap % of full scale)
        self._clip_at = FULL * cap / 100.0 * 65535.0 / gain if gain and cap is not None else None
        self._cap_level = FULL * cap / 100.0 if cap is not None else None
        self._gain_seen = gain / 65535.0 if gain else None
        self._gain_at = None
        self._comp, self._comp_at = self._gain_seen or 1.0, None
        self._clipped = 0.0
        self.rbr.set_ceiling(self._cap_level)    # RBR's knee ends under the driver's cap: it never clips there

    def _fade_out(self, t, reason, detail=None, soft=False):
        """Crossfade back: the game ramps in (game_ffb_level 100 now) while ours fades out. `detail`: what the
        classifier said, for the log; `soft`: a verdict that lasted (counts toward the back-off)."""
        self._fade_from = self._gain(t)
        self._restore_range(refresh=True)
        self._gear_restore()
        self._phase, self._fade_at = 'out', t
        # The add-on never lowered it: nothing to write (its level stays the game's 100)
        if (self._muted or self._played_mode != 'forza') and self._safe_game_level(100):
            self._muted = False
        self._released_at = t
        self.last_stop = reason
        if detail is None:
            logging.info("Countersteer FFB: handing the force back (%s)%s", reason, self._hand_report())
        else:
            logging.info("Countersteer FFB: handing the force back (%s: %s; engine skipped %d samples since the "
                         "take-over, %.0f ms behind)%s", reason, detail, self.skipped - self._taken_skipped,
                         self._lag * 1000.0, self._hand_report())
        if soft:
            self._note_stall(t)

    def _note_stall(self, t):
        """A release the telemetry's own stalls caused: BACKOFF_COUNT within BACKOFF_WITHIN s and ours stays off the wheel
        for BACKOFF_FOR s (the game's force plays; one line in the log)."""
        stalls = self._stalls
        stalls.append(t)
        while stalls and t - stalls[0] > BACKOFF_WITHIN:
            stalls.popleft()
        if len(stalls) >= BACKOFF_COUNT:
            stalls.clear()
            self._backoff_until = t + BACKOFF_FOR
            logging.info("Countersteer FFB: the game's telemetry keeps stalling; staying on its force for %.0f s",
                         BACKOFF_FOR)

    def _release(self, reason, now):
        """Stop playing at once (the driver's watchdog fades the last
        ext_force) and give the game its force back. Safe at any time, twice too."""
        was = self._phase != 'idle' or self._muted
        playing = self._phase in ('in', 'on')
        self._phase = 'idle'
        if self._muted and self._safe_game_level(100):
            self._muted = False
        self._restore_scale(now)
        self._let_go()
        if was:
            self._log_raw_lag()
            self._released_at = now
            if playing or self.last_stop is None:
                self.last_stop = reason
            logging.info("Countersteer FFB: the game's force is back (%s)%s", reason, self._hand_report())
            if reason == 'stall' and playing:
                self._note_stall(now)

    def _quick_release(self, reason, now):
        """The wheel wanted elsewhere, or quitting: the game's level back at
        once, the last value faded out over QUICK_FADE (in 10 ms steps, on
        this thread: there are no samples to wait for), then released."""
        live = self._phase != 'idle' and not self._broken and self._driver is not None
        if live and self._played_mode == 'forza':
            self._cut(now, reason)               # its last value is against a level not read again: 0 at once
            return
        if live:
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
        self._restore_range()
        self._gear_restore()
        self._dyn_stop(self._last_t if self._last_t is not None else self._clock())
        self._smooth = self._smooth_at = self._spike = None
        self._written_at = None
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
        model = self._model()
        ready, waiting, note = model.ready, model.waiting, None
        if self._mode == 'rbr':
            rack = self.rbr_rack
            ready, waiting = ready and rack.ready, model.waiting or rack.waiting
            note = ','.join(n for n, on in (('saturated', self.rbr.saturated), ('s', rack.s_note is not None)) if on) or None
        status = Status(v.state, v.reason, v.game, v.rate, v.gap_ms, self._enabled,
                        self._available and not self._broken, self._phase in ('in', 'on'), ready,
                        model.refused if self._mode in ('acr', 'forza-rack') else None, self.last_stop, self._path is not None,
                        waiting, held_by, None if self.raw_rate is None else round(self.raw_rate),
                        self._app_gain, self._phase != 'idle' and self._clipped > CLIP_NOTE, None, note)
        old = self.status
        figures = ('rate', 'gap_ms', 'raw_rate', 'gain')
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
    if status.last_stop == 'engine-restart' and not status.available:
        return 'off', _("Countersteer FFB's engine stopped: starting it again")
    if status.last_stop == 'engine-failed' and not status.available:
        return 'off', _("Countersteer FFB's engine kept stopping and is off until the app restarts: see the log")
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
    if status.reason == 'fh-addon':
        # Forza Horizon: Countersteer adds to the game's force (amber: an add-on, not a replacement)
        adds = _("{}: adds the front grip feel to Forza's own force").format(game)
        if status.held_by:
            return 'amber', _("{}; the wheel is in use ({}): {}").format(adds, status.held_by, keeps)
        if status.active:
            text = _("{}: lightens Forza's own force as the front tyres run out of grip").format(game)
            if status.raw_rate is not None:
                text += _(" (the game's force: {} a second)").format(status.raw_rate)
            return 'amber', text
        if not status.ready:
            waiting = {
                'raw': _("waiting for the game's own force on the wheel"),
                'corners': _("checking the game's force, drive a few corners"),
            }.get(status.waiting, _("checking the game's force"))
            return 'amber', "{}; {}".format(adds, waiting)
        if status.enabled:
            return 'amber', adds
        return 'amber', _("{}: turn on to use it").format(adds)
    if status.state == 'replace':
        if status.game == 'forza-fh':
            supported = _("{}: supported ({} packets/s, the force from the car's motion)").format(game, rate)
        elif status.game == 'rbr':
            supported = _("{}: supported (its own force, {} a second)").format(game, status.raw_rate or 0)
        elif status.game == 'beamng':
            supported = _("{}: supported (the Countersteer mod's rack force, {} a second)").format(game, rate)
        else:
            supported = _("{}: supported ({} packets/s with tyre forces)").format(game, rate)
        if status.refused:
            return 'grey', _("{}: the tyre forces run against the game's own force: {}").format(game, keeps)
        if status.held_by:
            return light, _("{}; the wheel is in use ({}): {}").format(supported, status.held_by, keeps)
        if not status.ready:
            source = _("the rack") if status.game == 'beamng' else _("the tyres")
            waiting = {
                'raw': _("waiting for the game's own force on the wheel to check against"),
                'corners': _("learning the game's force, drive a few corners"),
                'direction': _("checking which way the game's force turns, drive a few gentle corners"),
                'fit': _("the game's force does not follow {} yet").format(source),
                'mismatch': _("the game's force stopped following {}, learning again").format(source),
                'centring': _("learning which way the game centres the wheel, drive a few gentle corners"),
                'no-centring': _("centring not learnt: no gentle corners seen with the tyres gripping"),
                'wait': _("waiting for the car to move"),
            }.get(status.waiting, _("learning the game's force"))
            return light, "{}; {}".format(supported, waiting)
        if status.active:
            if status.game == 'forza-fh':
                text = _("{}: Countersteer's force from the car's motion, {} packets/s").format(game, rate)
            elif status.game == 'rbr':
                text = _("{}: Countersteer's force from the game's column torque, {} a second").format(
                    game, status.raw_rate or 0)
                if status.note and 'saturated' in status.note:
                    text += _("; its force sits at its limit too often to be followed well: lower the game's force "
                              "feedback strength or raise ForceFeedbackSensitivity")
                if status.note and 's' in status.note.split(','):
                    text += _("; the car's sensitivity is not known, 100 assumed")
            elif status.game == 'beamng':
                text = _("{}: Countersteer's force from the rack, {} packets/s").format(game, rate)
            else:
                text = _("{}: Countersteer's force from the tyres, {} packets/s").format(game, rate)
            if status.raw_rate is not None and status.game != 'rbr':
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
        'rbr-raw': _("waiting for the game's own force on the wheel (game_ffb_raw, {} a second, needs {})").format(
            status.raw_rate or 0, int(RBR_RAW_RATE)),
        'rbr-grid': _("its force is not on DirectInput's steps (another program's force on the wheel?)"),
        'rbr-strength': _("waiting for its force to reach its top once (a big hit) to know its strength; set the "
                          "game's force feedback strength to 100 % and use the wheel's own"),
        'rbr-inverse': _("the game's DIRECTION is inverted (its force turns the wrong way): set it to normal in the "
                         "game's force feedback settings"),
        'beamng-no-mod': _("no rack force from the Countersteer BeamNG mod: install it from the FFB tab"),
        'beamng-port': _("the port for the Countersteer BeamNG mod (5320) is in use by another program"),
        'beamng-paused': _("paused"),
        'csb-rate': _("the mod sends {} a second of game time, needs {}").format(rate, int(CSB_RATE)),
        'no-rack': _("the mod finds no steering rack on this vehicle"),
        'fh-rate': _("{} packets/s, needs {}").format(rate, int(FH_RATE)),
        'fh-app-gain': _("the driver's app_gain is on: it would scale the game's force but not Countersteer's, "
                         "turn app_gain off"),
        'forza-wait': _("waiting for the car to move"),
        'forza-mismatch': _("the game's own force runs against the car's motion: the model is not trusted here"),
        'fh-no-slip': _("sends no front tyre slip"),
        'fh-two-effects': _("it drives the wheel with two constant forces at once"),
        'fh-no-constant': _("it drives the wheel through its spring and damper, not a constant force: nothing "
                            "to lighten"),
        'fh-raw-rate': _("its constant force updates {} a second, needs {}").format(status.raw_rate or 0,
                                                                                   int(FH_RAW_RATE)),
        'fh-flat': _("its constant force does not change: it steers the wheel through its spring and damper, "
                     "nothing to lighten"),
        'fh-not-steering': _("its constant force does not follow the steering: nothing to lighten"),
        'fh-raw-stale': _("its constant force stopped updating"),
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
