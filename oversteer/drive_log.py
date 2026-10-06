"""The drive log: everything telemetry teaches that goes to disk.

The listener thread must never wait (docs/telemetry-coaching.md, section
4), so it only does constant-time work per packet and hands the rest to
the drive-log thread through a queue: each event is a function and its
arguments, run there with the database's only write connection. Writes
are batched into a transaction committed at most every few seconds and
whenever an event asks for it (the end of a session or a run).

Without a thread (tests, replays) events run at once, in the caller's
thread, and are committed as they go.
"""

import bisect
import logging
import math
import queue
import threading
import time

from . import car_data, coach, coach_context, drive_detect, live_buffer, potential, stage_tables
from .telemetry_formats import plan_xy
from .shift_learner import DRIVEN, SURFACES, CarModel, drive_slip
from .telemetry_store import open_store, TRACE_CHANNELS

QUEUE_SIZE = 8192
COMMIT_EVERY = 5.0               # seconds a batch of writes may wait
TICK_EVERY = 1.0                 # seconds between tick() calls on the thread


class DriveLog:
    """Owns the writer (a telemetry_store.Store) and, when `threaded`, the
    thread that uses it. `post(fn, *args)` never blocks: when the queue is
    full the event is dropped and counted in `dropped`. An event function
    returns True to have its writes committed at once. `ticks` are called
    about once a second on the thread (publishing snapshots). `batch`
    counts the batches committed or rolled back: a row id handed out in a
    batch that is rolled back is handed out again, so `on_rollback`
    (called on the thread with that batch) lets the owners of row ids
    forget them."""

    def __init__(self, path, threaded=True):
        self.path = path
        self.store = open_store(path)
        self.threaded = threaded
        self.dropped = 0
        self.ticks = []
        self.batch = 0
        self.on_rollback = []
        self._after_commit = []                          # callbacks for after the next commit (the thread's own)
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread = None
        self._commit_at = time.monotonic()
        if threaded:
            self._thread = threading.Thread(target=self._run, name='drive-log', daemon=True)
            self._thread.start()
        # The shipped stage tables, so a stage has its name, location and
        # surface from its first run
        self.post(self.store.seed_stages, stage_tables.tables())

    def post(self, fn, *args):
        if self._thread is None:
            self._handle(fn, args)
            self.store.commit()
            self._run_after_commit()
            return True
        try:
            self._queue.put_nowait((fn, args))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def call(self, fn, *args, timeout=5.0):
        """Run `fn(*args)` on the drive-log thread and wait for its result
        (for the few writes the GUI asks for: a rename, a forget). None
        if it did not finish in time."""
        if self._thread is None:
            result = fn(*args)
            self.store.commit()
            self._run_after_commit()
            return result
        done = threading.Event()
        box = []

        def run():
            try:
                box.append(fn(*args))
            finally:
                done.set()                               # a failure must not keep the GTK thread waiting
            return True
        try:
            self._queue.put((run, ()), timeout=timeout)
        except queue.Full:
            return None
        done.wait(timeout)
        return box[0] if box else None

    def sync(self, timeout=5.0):
        """Wait until everything posted so far is written and committed."""
        if self._thread is None:
            self.store.commit()
            self._run_after_commit()
            return True
        return self.call(lambda: True, timeout=timeout) is True

    def close(self, timeout=5.0):
        if self._thread is not None:
            self.sync(timeout)
            try:
                self._queue.put((None, ()), timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout)
            alive, self._thread = self._thread.is_alive(), None
            if alive:
                # Still writing: the connection is the thread's; it commits
                # what it has when it reaches the end of the queue
                logging.warning("drive log: still writing at exit")
                return
        self.store.commit()
        self._run_after_commit()

    def _run_after_commit(self):
        done, self._after_commit = self._after_commit, []
        for fn in done:
            try:
                fn()
            except Exception:
                logging.exception("drive log after commit")

    def after_commit(self, fn):
        """Call `fn` once the writes so far are committed (from the drive-log thread: what a reader of the
        file is told about must be in it already). Dropped if the commit fails."""
        self._after_commit.append(fn)

    def _handle(self, fn, args):
        self.store.begin()
        try:
            return fn(*args)
        except Exception:
            logging.exception("drive log")
            return False

    def _run(self):
        tick_at = time.monotonic() + TICK_EVERY
        while True:
            try:
                fn, args = self._queue.get(timeout=min(TICK_EVERY, COMMIT_EVERY))
            except queue.Empty:
                fn = args = None
            now = time.monotonic()
            if fn is None and args is not None:
                break                                   # close()
            urgent = fn is not None and self._handle(fn, args)
            if urgent or now - self._commit_at >= COMMIT_EVERY:
                self._commit()
            if now >= tick_at:
                tick_at = now + TICK_EVERY
                for tick in self.ticks:
                    try:
                        tick()
                    except Exception:
                        logging.exception("drive log tick")
        self._commit()

    def _commit(self):
        try:
            self.store.commit()
            self._run_after_commit()
        except Exception as e:
            self._after_commit = []
            logging.warning("drive log: can't write the telemetry database: %s", e)
            try:
                self.store.rollback()
            except Exception:
                pass
            for forget in self.on_rollback:
                try:
                    forget(self.batch)
                except Exception:
                    logging.exception("drive log rollback")
        self.batch += 1
        self._commit_at = time.monotonic()


# -- runs, segments, corners (docs/telemetry-coaching.md, section 8.4) --

TELEPORT = 50.0                  # m between consecutive positions, beyond what the speed covers: a restart...
TELEPORT_SPEED = 100.0           # ...or m/s implied between two packets, and...
TELEPORT_FACTOR = 3.0            # ...this many times the car's own speed (Forza passes 100 m/s)
NO_POSITION_GAP = 10.0           # s of silence that end a run in a game that sends no position
RESTART_DROP = 1.0               # s the stage clock must go back by to be a restart
MOVING = 1.0                     # m/s
START_MOVING = 3.0               # m/s: the run starts when the car first goes this fast
STOP = 3.0                       # s standing still after the start: a stop
LAUNCH_THROTTLE = 0.5            # held while standing before the start: a launch
SEGMENT = 200.0                  # m per segment
SEGMENT_MIN = 50.0               # m: a shorter tail is not kept
SEGMENT_ROWS = 3600              # rows (a minute at 60 Hz): a segment closes then however short (a parked car)
TRACE_EVERY = 0.1                # s between trace rows
RUN_MIN = 100.0                  # m moving: a shorter run (menus, a car parked) is not kept
DISTANCE_BACK = 100.0            # m the distance along a stage goes back: the stage restarted
SENT_LENGTH_TOLERANCE = 0.5      # m: a length the game sends against its own in the table
FINISHED = 0.99                  # progress through the stage that counts as reaching the end
CLOCK_STOPPED = 1.0              # s moving with the stage clock standing still: past the finish
CLOCK_SETTLE = 0.25              # s of the run's own time the game's clock stands still, moving: it stopped (not a few frames repeated)
FINISH_CLOCK_PAST = 100.0        # m past the table's finish line the game's clock may still stop at its own
FINISH_CLOCK_BEFORE = 400.0      # m before the table's line (the last pace note, the stop control) the game's clock may have stopped
FINISH_AFTER = 500.0             # m: a clock standing still sooner is not the finish
# The game keeps sending after the finish (the results screen, the car rolling
# out or parked), so a finished run is ended by what follows rather than by
# the game: the car at rest, or this long / this far past the line
FINISH_STILL = 2.0               # s the car stands (under MOVING) after the finish: the run is over
FINISH_GRACE = 15.0              # s after the finish at most
FINISH_ROLLOUT = 400.0           # m driven past the finish at most (ACR: the stop control is ~230 m on)
FINISH_GRACE_PROGRESS = 60.0     # s: where the finish is progress >= FINISHED, the last of the stage is still to drive
# Their progress is the position around the lap, near 1 at the end of every
# lap: a circuit session is not ended there
LAP_SPLINE_GAMES = ('ac', 'acc', 'acpmf')

# One row per packet of the segment being driven, for its features
SEGMENT_CHANNELS = ('t', 'speed', 'a_long', 'a_lat', 'yaw_rate', 'throttle', 'gear', 'slip', 'susp_fl', 'susp_fr',
                   'susp_vel_fl', 'susp_vel_fr', 'slip_angle', 'puddle', 'rumble', 'steer')
S = {name: i for i, name in enumerate(SEGMENT_CHANNELS)}
T = {name: i for i, name in enumerate(TRACE_CHANNELS)}


def _nan(x):
    return float('nan') if x is None else x


class RunClock:
    """The run's clock, which the trace's `t` and the segments' times are
    on (trace version 2): the game's stage clock where the game sends one
    that runs, else the run's own time (the packets' gaps capped, as
    `duration` adds them up). Ticked only for the packets that count (not
    paused, not frozen), so a pause is in neither.

    `t` follows the game's clock as `stage_time + offset`. `offset` is 0
    when the game's clock was running at the run's first packet (a standing
    start: the trace's t is then the game's own time, and t at the finish
    is the result) and `exact` stays True while it stays 0. A clock that
    starts after the run did (a run-up to a timing line), one that went
    back without the run ending (a lap's clock) or one that was missing
    sets `offset` where t takes it up, so t never goes back. Where the
    game's clock stands still for over CLOCK_STOPPED (past its finish) t
    goes on by the run's own time until it moves again; a clock standing
    still from the first packet, not yet seen to move, is not one that
    stopped (the last run's finish, not reset): t goes on by the run's own
    time until it does."""

    __slots__ = ('t', 'offset', 'exact', 'last', 'still', 'moved', 'proven', 'fresh')

    def __init__(self, stage_time):
        running = stage_time is not None and stage_time > 0.0
        self.t = stage_time if running else 0.0
        self.offset = 0.0 if running else None       # None: on the run's own time
        self.exact = running
        self.last = stage_time                       # the game's clock at the last packet
        self.still = 0.0                             # s of the run's own time the game's clock has stood still
        self.moved = running                         # it moved at the last packet
        self.fresh = True                            # the first tick is the packet the clock was made from
        self.proven = False                          # it has been seen to move: until then a clock standing still is not 'stopped'

    def tick(self, stage_time, dt):
        last, self.last = self.last, stage_time
        fresh, self.fresh = self.fresh, False
        self.moved = False
        if stage_time is None:
            self.t += dt
            self.still = 0.0
            return
        forward = last is not None and stage_time > last
        if self.offset is None:
            if forward:
                # The game's clock has started: t takes it up from here
                self.t += stage_time - last
                self.offset = self.t - stage_time
                self.moved = True
                self.proven = True
            else:
                self.t += dt
            return
        if last is not None and stage_time == last:
            if fresh:
                return
            if not self.proven:
                self.t += dt                         # frozen from the first packet (a finish not yet reset): not a stop
                return
            self.still += dt
            if self.still > CLOCK_STOPPED:
                self.t += dt                         # stopped (past the finish): the run's own time goes on
            return
        self.moved = True
        self.proven = True
        self.still = 0.0
        candidate = stage_time + self.offset
        if candidate >= self.t and (forward or last is None):
            self.t = candidate
        else:
            # It went back (a lap's clock), or the run's own time ran ahead of it while it stood
            self.t += stage_time - last if forward else dt
            self.offset = self.t - stage_time
            self.exact = False


class RunTracker:
    """Cuts the telemetry of a session into runs (one stage attempt, one
    lap session, one stretch of free driving) and each run into ~200 m
    segments, keeps a 10 Hz trace of it, and hands each piece to the
    drive-log thread as it closes. Fed by the shift learner under its
    lock, per packet, in constant time; everything slow (features,
    corners, verdicts, SQLite) runs on the drive-log thread."""

    def __init__(self, learner):
        self.learner = learner
        self.run = None                  # the run going on: a number, its row id is the drive log's
        self._runs = 0
        self.run_rows = {}               # run number -> runs.id; drive-log thread only
        self.run_batch = {}              # run number -> the drive log's batch its row was written in
        self._live_loaded = set()        # run numbers whose live reference was looked up; drive-log thread only
        self._n_in_session = {}          # session number -> runs started in it
        self._reset()
        self._waiting()

    def _reset(self):
        self.run = None
        self._from_line = True                       # ACR: the run began at the stage's start line, not mid-stage
        self._finish_line = None                     # ACR: the flying finish (else the last pace note) along the spline
        self._start_d = self._acr_start = None
        self._last_t = None
        self._last_pos = None
        self._last_stage_time = None
        self._last_lap = None
        self._last_speed = None
        self._last_lap_distance = None
        self._last_track = None

    def _waiting(self):
        """Not driving yet: how long the car has stood, and whether with the
        revs held (a launch)."""
        self._stood = 0.0
        self._launch = 0.0
        self._launch_rpm = 0.0
        self._clutch = None              # the most the clutch was pressed while standing (the pedal, as the game has it)
        self._rolling = None             # when the car began to move, before the run started
        self._after_end = False
        self._guard = None               # after a run's finish: where the stage was, until a new one begins

    # -- the listener side --

    def feed(self, now, sample, throttle, session, profile):
        """One packet of the session numbered `session`."""
        if self.run is not None:
            reason = self._boundary(now, sample, session)
            if reason is not None:
                self.end(now, reason)
        if sample.packet == 'start':
            self._after_end = False
        dt = min(0.2, max(0.0, now - self._last_t)) if self._last_t is not None else 0.0
        speed = sample.speed or 0.0
        if self.run is None:
            if self._guard is not None and self._guard_released(now, sample, session):
                self._guard = None
            if self._guard is not None:
                self._guard_note(sample)
                self._remember(now, sample)
                return                               # still driving on past the finish of the run just ended
            if self._after_end and sample.packet != 'start':
                self._remember(now, sample)
                return                               # EA sent the end of the stage: wait for the next start
            if speed < START_MOVING:
                if speed < MOVING:
                    self._stood += dt
                    self._rolling = None
                    if throttle is not None and throttle >= LAUNCH_THROTTLE:
                        self._launch += dt
                        self._launch_rpm = max(self._launch_rpm, sample.rpm or 0.0)
                        if sample.clutch is not None:
                            self._clutch = max(self._clutch or 0.0, sample.clutch)
                    else:
                        self._launch = self._launch_rpm = 0.0
                        self._clutch = None
                elif self._rolling is None:
                    self._rolling = now
                self._remember(now, sample)
                return
            self._start(now, sample, session, profile)
        self._track(now, sample, throttle, dt, speed)
        self._remember(now, sample)
        if self.run is not None and self._finished == 1 and sample.game not in LAP_SPLINE_GAMES \
                and not (sample.laps and sample.laps > 1) and self._post_over(now):
            self.end(now, 'finish')
            return
        if sample.packet == 'end':
            self.end(now, 'end')
            self._after_end = True

    def _post_over(self, now):
        """Whether a finished run has gone on long enough past the line to
        be ended: the car has stood, or the grace (time or road) is used up."""
        if self._finish_t is None:
            self._finish_t = now
            return False
        if self._finish_counts:
            return self._still >= FINISH_STILL or now - self._finish_t >= FINISH_GRACE_PROGRESS
        return (self._post_still >= FINISH_STILL or now - self._finish_t >= FINISH_GRACE
                or self._post_d >= FINISH_ROLLOUT)

    def _guard_note(self, sample):
        guard = self._guard
        if sample.lap_distance is not None:
            guard['ld'] = max(guard['ld'], sample.lap_distance) if guard['ld'] is not None else sample.lap_distance
        if sample.stage_time is not None:
            guard['st'] = max(guard['st'], sample.stage_time) if guard['st'] is not None else sample.stage_time

    def _guard_released(self, now, sample, session):
        """A finished run ended while the game went on sending the same
        stage's results screen and roll-out: a new run may begin once the
        game shows something new (the stage restarted, another stage, a
        teleport, a silence, a new session or an EA start packet)."""
        guard = self._guard
        if session != guard['session'] or sample.packet == 'start':
            return True
        if self._last_t is not None and now - self._last_t > NO_POSITION_GAP:
            return True
        if sample.track and guard['track'] and sample.track != guard['track']:
            return True
        if sample.lap_distance is not None and guard['ld'] is not None \
                and sample.lap_distance < guard['ld'] - DISTANCE_BACK:
            return True
        if sample.stage_time is not None and guard['st'] is not None and sample.stage_time < guard['st'] - RESTART_DROP:
            return True
        if sample.pos is not None and self._last_pos is not None:
            jump = math.sqrt(sum((a - b) ** 2 for a, b in zip(sample.pos, self._last_pos)))
            if jump > TELEPORT + max(sample.speed or 0.0, self._last_speed or 0.0) * max(0.0, now - (self._last_t or now)):
                return True
        return False

    def _remember(self, now, sample):
        self._last_t = now
        if sample.pos is not None:
            self._last_pos = sample.pos
        if sample.stage_time is not None:
            self._last_stage_time = sample.stage_time
        if sample.lap_distance is not None:
            self._last_lap_distance = sample.lap_distance
        if sample.track:
            self._last_track = sample.track
        self._last_speed = sample.speed

    def _boundary(self, now, sample, session):
        """Why this packet starts a new run, or None."""
        if session != self._session:
            return 'session'
        if sample.packet == 'start':
            return 'restart'
        pos, last = sample.pos, self._last_pos
        gap = now - self._last_t if self._last_t is not None else 0.0
        if pos is not None and last is not None:
            # Measured against the car's own speed: at 110 m/s every packet
            # implies 110 m/s, and a hitch of half a second covers 55 m
            jump = math.sqrt(sum((a - b) ** 2 for a, b in zip(pos, last)))
            speed = max(sample.speed or 0.0, self._last_speed or 0.0)
            implied = max(TELEPORT_SPEED, TELEPORT_FACTOR * speed)
            # At least TELEPORT metres either way: packets arriving in a
            # burst have near-zero gaps, and half a metre over half a
            # millisecond is not 1000 m/s; nor is a reset after a crash
            # (a few metres) the end of the stage
            if jump > TELEPORT and (jump > TELEPORT + speed * gap or (gap > 0 and jump / gap > implied)):
                return 'teleport'
        elif pos is None and gap > NO_POSITION_GAP:
            return 'silence'
        # Another stage, or the stage started again: in Assetto Corsa Rally,
        # which sends no position or stage clock the checks above could use,
        # the distance along the stage jumps back to the start line
        # (ACR only: AC and ACC lap distances wrap at the line with laps 0)
        if sample.game == 'acr':
            if sample.track and self._last_track and sample.track != self._last_track:
                return 'restart'
            distance, last_distance = sample.lap_distance, self._last_lap_distance
            if distance is not None and last_distance is not None and distance < last_distance - DISTANCE_BACK:
                return 'restart'
        stage_time, last_time = sample.stage_time, self._last_stage_time
        if stage_time is not None and last_time is not None and stage_time < last_time - RESTART_DROP:
            laps, lap = sample.laps, sample.lap
            if not (laps and laps > 1 and lap is not None and self._last_lap is not None and lap > self._last_lap) \
                    and not self._acr_lap(sample, stage_time):
                return 'restart'                     # the clock went back without a lap done
        return None

    def _acr_lap(self, sample, stage_time):
        """ACR's clock back to 0 as a looped stage's car crosses the line at speed, the distance along the spline
        running on: a lap of the same run. A restart puts the car back at rest, or far back along the road."""
        distance, last = sample.lap_distance, self._last_lap_distance
        return (sample.game == 'acr' and stage_time < RESTART_DROP and (sample.speed or 0.0) > START_MOVING
                and (self._last_speed or 0.0) > START_MOVING and distance is not None and last is not None
                and abs(distance - last) < TELEPORT)

    def _start(self, now, sample, session, profile):
        learner = self.learner
        self._runs += 1
        self.run = self._runs
        for number in list(learner.run_surface):
            if number < self.run - 1:
                learner.run_surface.pop(number, None)
        self._session = session
        n = self._n_in_session.get(session, 0) + 1
        self._n_in_session = {session: n}
        self._t0 = now
        self._wall0 = learner.wall(now)
        self._start_pos = sample.pos
        self._distance = 0.0                         # m, integrated
        self._d0_game = None
        self._moving = self._duration = 0.0
        self._stops = 0
        self._still = 0.0
        self._clock_run = RunClock(sample.stage_time)
        self._clock_moved_d = None                   # d at the last packet the game's clock moved
        self._clock_moved_ld = None                  # and the lap distance (the place on the spline)
        self._line_clock = None                      # (d, the game's clock) where the run crossed the table's finish line
        self._seg_d0, self._seg_t0 = 0.0, self._clock_run.t
        self._rows = []
        self._trace = []
        self._trace_at = None
        self._susp_sq = 0.0
        self._susp_n = 0
        self._last_lap = sample.lap
        self._lap_t0, self._lap_d0 = now, 0.0
        self._laps_done = 0
        self._progress = sample.progress
        self._finished = None
        self._result_time = None
        self._result_clock = None                    # 'game': the game's own clock stopped at its line; 'line': read at ours
        self._finish_d = None                        # where the run crossed the finish, as far as it can tell
        self._finish_counts = False                  # progress said finished (before the line): the rest is driven in the run
        self._finish_t = None                        # when the run's end was first seen to be due (after the finish)
        self._post_still = 0.0                       # s the car has stood since the finish
        self._post_d = 0.0                           # m driven since the finish
        self._clock_d = None                         # d at the last packet the stage clock moved
        self._clock_still = 0.0                      # s moving since it last did
        self._puddles = self._samples = 0
        self._paused = sample.packet == 'pause'
        self._packets = {sample.packet} if sample.packet else set()
        self._summary = {
            'game': sample.game, 'profile': profile, 'stage': sample.stage, 'stage_length': sample.stage_length,
            'laps': sample.laps, 'has_pos': sample.pos is not None, 'standing': self._stood,
            'launch': self._launch, 'launch_rpm': self._launch_rpm or None, 'launch_clutch': self._clutch,
            'track': sample.track,
            'car': sample.car, 'gears': sample.gears,
            # s from moving off to the run's start (at START_MOVING), for the launch time
            'release': now - self._rolling if self._rolling is not None else 0.0}
        self._stood = self._launch = self._launch_rpm = 0.0
        self._clutch = None
        self._rolling = None
        # Assetto Corsa Rally sends no stage clock or progress: the run has
        # finished once it crosses the stage's flying finish, else its last pace note (looked up in
        # _track, as the track's name may arrive after the first packet).
        # The start tells two stages of one name apart only from standing:
        # a run split mid-stage starts anywhere.
        self._finish_line = None
        self._from_line = True
        self._start_d = sample.lap_distance
        self._acr_start = sample.lap_distance if self._summary['standing'] > 0.5 else None
        learner.live_run.start_run(self.run, now, sample.game, sample.stage, sample.track, sample.stage_length,
                                   sample.lap_distance)
        learner.log.post(self._write_start, self.run, session, n, self._wall0, sample.stage, sample.game,
                         sample.stage_length, list(sample.pos) if sample.pos is not None else None, sample.track,
                         self._acr_start, sample.lap_distance)
        # The live delta's reference, read once off the listener (posted after
        # the run's row, which names the stage)
        learner.log.post(self._live_reference, self.run, session, sample.game, sample.track, sample.stage_length,
                         self._acr_start)

    def _clock(self, now, sample, d, dt, speed):
        """The finish of a stage in a game that sends no progress: the
        stage clock stops at the line while the car rolls on."""
        if self._finish_d is not None or sample.stage_time is None or sample.game == 'acr':
            return                                   # (ACR's is told with the table's line: _line_finish, _clock_finish)
        if sample.stage_time != self._last_stage_time or self._clock_d is None:
            self._clock_d, self._clock_still = d, 0.0
        elif speed > START_MOVING and sample.stage_time > 0:
            self._clock_still += dt
            if self._clock_still >= CLOCK_STOPPED and self._clock_d >= FINISH_AFTER:
                self._finish_d = self._clock_d
                self.learner.live_run.finish(self.run, now, sample.stage_time, self._clock_d)

    def _d(self, sample):
        """Where along the run the car is: the game's stage distance where
        it sends one (the same stretch of road gets the same d in every
        run), else the distance driven (a circuit's lap distance wraps at
        the line)."""
        game = sample.distance if sample.distance is not None else (
            sample.lap_distance if not (sample.laps and sample.laps > 1) and sample.game not in LAP_SPLINE_GAMES
            else None)
        if game is None:
            return self._distance
        if self._d0_game is None:
            self._d0_game = game - self._distance
        return game - self._d0_game if sample.distance is None else game

    def _track(self, now, sample, throttle, dt, speed):
        summary = self._summary
        if sample.packet:
            self._packets.add(sample.packet)
            if sample.packet == 'pause':
                self._paused = True
            elif sample.packet in ('resume', 'update', 'start'):
                self._paused = False
        # DiRT Rally repeats its last packet while paused: only the clock of
        # the game moves, the stage clock and the car do not
        frozen = (sample.stage_time is not None and sample.stage_time == self._last_stage_time and speed < 0.1)
        if self._finish_t is not None and not self._finish_counts:
            # Past the finish: only watch for the car at rest. Nothing driven
            # here counts toward the run (its rows, distance, time)
            if speed < MOVING:
                self._post_still += dt
            else:
                self._post_still = 0.0
                self._post_d += speed * dt
            return
        if not self._paused and not frozen:
            self._clock_run.tick(sample.stage_time, dt)
            self._duration += dt
            self._distance += speed * dt
            if speed > MOVING:
                self._moving += dt
                self._still = 0.0
            else:
                self._still += dt
                if self._still - dt < STOP <= self._still:
                    self._stops += 1
        elif frozen and not self._paused and sample.game == 'acr':
            # ACR sends nothing in a pause, so a packet repeating the clock with the car stopped is the car at
            # rest: time, rest and a stop like any other (the run's clock t, which only the clock moves, is not
            # ticked). DiRT repeats its last packet in a pause: there it is not counted
            self._duration += dt
            self._still += dt
            if self._still - dt < STOP <= self._still:
                self._stops += 1
        elif frozen and self._finish_counts and (
                (sample.progress or 0.0) >= 1.0 or (sample.stage_time or 0.0) > (self._result_time or 0.0)):
            # Past the finish the game repeats its last packet (DiRT): the car is at rest all the same. Not in a
            # pause just after progress says finished, which is before the line (progress under 1, the clock
            # where it was at the progress finish)
            self._still += dt
        if sample.laps is not None:
            summary['laps'] = max(summary['laps'] or 0, sample.laps)
        if sample.stage_length and not summary['stage_length']:
            summary['stage_length'] = sample.stage_length
        if sample.gears and not summary['gears']:
            summary['gears'] = sample.gears
        if self._paused or frozen:
            # Nothing moves: no rows, which would pile up at packet rate for
            # as long as the pause lasts and swamp the next segment's features
            return
        d = self._d(sample)
        if self._clock_run.moved:
            self._clock_moved_d = d
            self._clock_moved_ld = sample.lap_distance
        lap = sample.lap
        if lap is not None and self._last_lap is not None and lap > self._last_lap:
            self._laps_done += 1
            self.learner.log.post(self._write_lap, self.run, self._laps_done, now - self._lap_t0, d - self._lap_d0)
            self._lap_t0, self._lap_d0 = now, d
        if lap is not None:
            self._last_lap = lap
        if sample.progress is not None:
            # (a circuit's progress is the lap's: near 1 at the end of every lap, not the end of the run)
            if self._finished is None and sample.progress >= FINISHED and (self._progress or 0.0) < FINISHED \
                    and sample.game not in LAP_SPLINE_GAMES:
                self._finished, self._result_time = 1, sample.stage_time
                self._finish_d = d
                self._finish_counts = True
                self.learner.live_run.finish(self.run, now, self._result_time, d)
            self._progress = sample.progress
        self._clock(now, sample, d, dt, speed)
        if self._finish_line is None and sample.game == 'acr' and sample.track and self._finished is None:
            stage = stage_tables.acr_stage(sample.track, self._acr_start, sample.stage_length)
            # The flying finish where known, else the last pace note (the stop control, past it)
            self._finish_line = (stage or {}).get('finish_m') or (stage or {}).get('pacenote_last_m') or False
            line = stage_tables.start_line(stage)
            # A run that began mid-stage (a restart after a silence, the second half of a run split by one) that
            # crosses the finish did not run the stage: its time is not the stage's
            self._from_line = line is None or self._start_d is None or self._start_d <= line + stage_tables.START_LINE_PAST
            if not self._from_line:
                self.learner.live_run.mark_mid_stage(self.run)       # no live delta against a run from the line
            # The track's name came after the run's first packet: the live
            # reference may have had no stage to look up (a no-op once it had)
            self.learner.log.post(self._live_reference, self.run, self._session, sample.game, sample.track,
                                  sample.stage_length, self._acr_start)
            surface = stage_tables.surface_of(stage)[0] if stage else None
            if surface is not None and self.learner.run_surface.get(self.run) is None:
                self.learner.run_surface[self.run] = surface       # the learner's best points are per surface
        if self._finish_line and self._finished is None and sample.lap_distance is not None \
                and sample.lap_distance >= self._finish_line \
                and self._from_line and (self._start_d is None or self._start_d < self._finish_line):
            self._line_finish(now, sample, d, speed)
        elif self._finish_line and self._finished is None and sample.lap_distance is not None \
                and sample.lap_distance >= self._finish_line - FINISH_CLOCK_BEFORE and self._from_line \
                and (self._start_d is None or self._start_d < self._finish_line):
            self._clock_finish(now, sample, speed)
        if sample.puddle is not None:
            self._samples += 1
            if any(p > 0 for p in sample.puddle):
                self._puddles += 1
        # Acceleration in the car frame: the game's, or the change of speed
        # and speed x yaw rate
        accel = sample.accel
        if accel is not None:
            a_long, a_lat = accel[0], accel[1]
        else:
            a_long = (speed - self._last_speed) / dt if dt > 0 and self._last_speed is not None else None
            a_lat = speed * sample.yaw_rate if sample.yaw_rate is not None else None
        car = self.learner.car
        slip = drive_slip(sample, car.drivetrain if car else None, car.radii if car else None)
        slip = slip[0] if slip is not None and slip[1] == 'raw' else None
        susp, susp_vel = sample.susp, sample.susp_vel
        slip_angle = sample.slip_angle
        self._rows.append((
            now, speed, a_long, a_lat, sample.yaw_rate, throttle, sample.gear, slip,
            susp[0] if susp else None, susp[1] if susp else None,
            susp_vel[0] if susp_vel else None, susp_vel[1] if susp_vel else None,
            (abs(slip_angle[0]) + abs(slip_angle[1])) / 2 if slip_angle else None,
            max(sample.puddle) if sample.puddle is not None else None,
            sum(sample.surface_rumble) / 4 if sample.surface_rumble is not None else None,
            sample.steer))
        if susp_vel is not None:
            self._susp_sq += (susp_vel[0] ** 2 + susp_vel[1] ** 2) / 2
            self._susp_n += 1
        if self._trace_at is None or now - self._trace_at >= TRACE_EVERY - 0.005:     # 60 Hz packets: 6 per row
            self._trace_at = now
            pos = sample.pos or (None, None, None)
            rms = math.sqrt(self._susp_sq / self._susp_n) if self._susp_n else None
            self._susp_sq, self._susp_n = 0.0, 0
            row = tuple(_nan(v) for v in (
                self._clock_run.t, d, speed, sample.rpm, sample.gear, throttle, sample.brake, sample.clutch,
                sample.handbrake, sample.steer, a_long, a_lat, sample.yaw_rate, slip, rms, pos[0], pos[1], pos[2],
                now - self._t0))
            self._trace.append(row)
            self.learner.live_run.push(self.run, now, row)
        if d - self._seg_d0 >= SEGMENT or len(self._rows) >= SEGMENT_ROWS:
            self._close_segment(d)

    def _line_finish(self, now, sample, d, speed):
        """ACR: the run is at or past the table's finish line (the flying
        finish, else the last pace note). With the game's clock (bridge
        version 4) the result is the game's own time: where its clock stops
        at its own line (CLOCK_SETTLE standing still with the car moving),
        which may be a little before or after the table's; failing that
        within FINISH_CLOCK_PAST m, the clock interpolated at the table's
        line. Until then the run goes on (its rows are kept: the course cuts
        them). Without it, the run's own clock at the line."""
        clock = self._clock_run
        if self._line_clock is None:
            at = sample.stage_time
            last_t, last_ld = self._last_stage_time, self._last_lap_distance
            if at is not None and last_t is not None and last_ld is not None and last_t <= at \
                    and last_ld < self._finish_line < sample.lap_distance:
                at = last_t + (at - last_t) * (self._finish_line - last_ld) / (sample.lap_distance - last_ld)
            self._line_clock = (d, at)
        if sample.stage_time is None or clock.offset is None:
            self._finish_at(now, d, self._duration, None)    # no clock from the game: the run's own
        elif clock.still >= CLOCK_SETTLE and speed > MOVING and self._clock_stop_near_line():
            self._finish_at(now, self._clock_moved_d if self._clock_moved_d is not None else d,
                            sample.stage_time + clock.offset, 'game')
        elif sample.lap_distance > self._finish_line + FINISH_CLOCK_PAST:
            line_d, at = self._line_clock
            self._finish_at(now, line_d, (at if at is not None else sample.stage_time) + clock.offset, 'line')

    def _clock_stop_near_line(self):
        """The place the game's clock last moved is near the table's line (FINISH_CLOCK_BEFORE before it, up to
        FINISH_CLOCK_PAST past it): a clock that stopped in the middle of the stage is not the finish."""
        at = self._clock_moved_ld
        return at is not None and self._finish_line - FINISH_CLOCK_BEFORE <= at <= self._finish_line + FINISH_CLOCK_PAST

    def _clock_finish(self, now, sample, speed):
        """ACR with the game's clock, before the table's line (but within FINISH_CLOCK_BEFORE of it): the clock
        standing still for CLOCK_STOPPED with the car moving, past FINISH_AFTER, is the finish. The game stops its
        clock 194-320 m before the last pace note on the stages with no flying finish in the table; the run
        finished there, whether the car goes on to the note or stops at the stop control short of it."""
        clock = self._clock_run
        if sample.stage_time is None or clock.offset is None or not clock.proven:
            return
        moved_d, at = self._clock_moved_d, self._clock_moved_ld
        if clock.still >= CLOCK_STOPPED and speed > MOVING and moved_d is not None and moved_d >= FINISH_AFTER \
                and at is not None and at >= self._finish_line - FINISH_CLOCK_BEFORE:
            self._finish_at(now, moved_d, sample.stage_time + clock.offset, 'game')

    def _finish_at(self, now, d, result, clock):
        self._finished, self._result_time, self._result_clock = 1, result, clock
        self._finish_d = d
        self.learner.live_run.finish(self.run, now, self._result_time, d)

    def _close_segment(self, d):
        rows, self._rows = self._rows, []
        t = self._clock_run.t
        if d - self._seg_d0 >= SEGMENT_MIN and rows:
            self.learner.log.post(self._write_segment, self.run, self._seg_d0, d, self._seg_t0, t, rows)
        self._seg_d0, self._seg_t0 = d, t

    def end(self, now, reason):
        """The run is over (`reason`: 'session', 'restart', 'teleport',
        'silence', 'end')."""
        if self.run is None:
            return
        # The run's changes of gear are written before it ends, so its
        # metrics see them all (a double tap at the very end goes unflagged)
        if self.learner.car is not None:
            self.learner._flush_shifts_locked(self.learner.car)
        last_d = self._trace[-1][T['distance']] if self._trace else 0.0
        self._close_segment(last_d)
        summary = dict(self._summary)
        finished = self._finished
        if reason == 'end':
            finished = 1 if (self._progress or 0.0) >= FINISHED - 0.01 else 0
        elif finished is None and reason in ('restart', 'teleport'):
            finished = 0
        summary.update(distance=self._distance, duration=self._duration, moving_time=self._moving,
                       stops=self._stops, finished=finished, result_time=self._result_time, laps_done=self._laps_done,
                       clock=self._result_clock, finish_line=self._finish_line or None,
                       clock_stop=self._clock_moved_ld if self._result_clock == 'game' else None,
                       packets=sorted(self._packets), end=reason,
                       puddles=self._puddles / self._samples if self._samples else None,
                       progress=self._progress,
                       course=self._finish_d if self._finish_d is not None else last_d)
        self.learner.log.post(self._write_end, self.run, self.learner.wall(self._last_t or now), summary, self._trace)
        self.learner.live_run.end_run(self.run, now, reason)
        guard = None
        if reason == 'finish':
            guard = {'session': self._session, 'track': self._last_track, 'ld': self._last_lap_distance,
                     'st': self._last_stage_time}
        self._reset()
        self._waiting()
        self._guard = guard

    # -- the drive-log thread --

    def _write_start(self, number, session, n, started, stage, game, stage_length, start_pos, track=None,
                     start_d=None, spline=None):
        learner = self.learner
        row = learner._session_rows.get(session)
        if row is None:
            return
        store = learner.log.store
        if stage is None and game in ('acr', 'acc', 'acpmf') and track:
            # Assetto Corsa Rally names the stage in its shared memory (the
            # bridge before version 3 does not say which AC game it is)
            stage = store.match_track(track, stage_length, start=start_d)
            if stage is not None:
                game = 'acr'
        if stage is None and game == 'wrcg' and stage_length:
            # WRC Generations sends the length of its route to the float
            # (the table has it from the game's files): it names the
            # stage, reverse or not, which a 1 % distance window can't
            stage = store.match_distance(game, stage_length, start_pos, within=SENT_LENGTH_TOLERANCE)[0]
        if stage is None and game in ('dirt', 'wrcg') and stage_length and start_pos is not None:
            # DiRT names no stage: its length and where it starts do
            stage = store.match_stage(game, stage_length, start_pos[2])
        self.run_rows[number] = store.start_run(row[0], n, started, stage, game, stage_length, start_pos)
        if spline is not None and game == 'acr':             # only ACR's lap_distance is a place on the road
            store.set_run_start(self.run_rows[number], spline)
        self.run_batch[number] = learner.log.batch
        surface = stage_surface(store, game, stage)
        if surface is not None and learner.run_surface.get(number) is None:
            learner.run_surface[number] = surface

    def _live_reference(self, number, session, game, track, stage_length, start_d):
        """Drive-log thread: hand the live buffer run `number`'s reference
        (live_buffer.load_reference) once a stage is known for it; posted at
        the run's start and again when ACR's track name arrives."""
        if number in self._live_loaded:
            return
        run = self.run_rows.get(number)
        store = self.learner.log.store
        live = self.learner.live_run
        stage = store.run_stage(run) if run is not None else None
        if stage is None and game == 'acr' and track:
            stage = store.match_track(track, stage_length, start=start_d)
        if stage is None and game == 'acr' and not track:
            return                                       # its track's name is still to come
        self._live_loaded.add(number)
        while len(self._live_loaded) > 8:
            self._live_loaded.discard(min(self._live_loaded))
        if stage is None:
            live.offer_reference(number, None, None, run)
            return
        row = self.learner._session_rows.get(session)
        try:
            reference = live_buffer.load_reference(store, stage, row[1] if row is not None else None, exclude=run)
        except Exception:
            logging.exception("live reference")
            reference = None
        known = store.stage(stage) or {}
        live.offer_reference(number, reference, {'key': stage, 'name': known.get('name') or track,
                                                 'length': known.get('length') or stage_length}, run)

    def _write_lap(self, number, n, lap_time, distance):
        run = self.run_rows.get(number)
        if run is not None:
            self.learner.log.store.add_lap(run, n, lap_time, distance)

    def _write_segment(self, number, d0, d1, t0, t1, rows):
        run = self.run_rows.get(number)
        if run is None:
            return
        features = segment_features(rows)
        self.learner.log.store.add_segment(run, d0, d1, t0, t1, features, pushed=pushed(features))

    def _write_end(self, number, ended, summary, trace):
        run = self.run_rows.pop(number, None)          # nothing is posted for a run after its end
        self.run_batch.pop(number, None)
        if run is None:
            return
        store = self.learner.log.store
        if summary['distance'] < RUN_MIN:
            store.drop_run(run)                          # menus, a car parked: not a run
            return True
        fields = {'ended': ended, 'distance': summary['distance'], 'duration': summary['duration'],
                  'moving_time': summary['moving_time'], 'finished': summary['finished'],
                  'result_time': summary['result_time']}
        stage = store.run_stage(run)
        game = summary['game']
        location = None
        # (ACR names its stage by its track: the road driven is no evidence, and its sent length is the spline's)
        if stage is None and game != 'acr' and game in stage_tables.tables() \
                and (summary['finished'] == 1 or not summary['progress']):
            # A game that names no stage and sends no length (WRC
            # Generations): the distance to the finish, against the
            # published lengths; the start tells apart two of one length
            start = (trace[0][T['x']], trace[0][T['y']], trace[0][T['z']]) if trace and summary['has_pos'] else None
            if start is not None and any(math.isnan(v) for v in start):
                start = None
            stage, candidates = store.match_distance(game, summary['course'], start)
            if stage is not None:
                fields['stage'], fields['stage_game'] = stage, game
            elif candidates:
                # Which stage is open (a stage and its reverse), but maybe
                # not where, nor on what
                summary['stage_candidates'] = candidates
                if len({c.get('location') for c in candidates}) == 1:
                    location = candidates[0].get('location')
        if stage is None and summary['has_pos'] and game:
            cell = start_cell(trace)
            if cell is not None:
                stage = fields['stage'] = store.match_cell(game, cell, summary['distance'])
                fields['stage_game'] = game
                if location is not None:
                    store.upsert_stage(stage, game, location=location)
        summary['stage'] = stage
        verdicts = drive_detect.detect_run(store, run, summary, trace, TRACE_CHANNELS)
        fields.update(verdicts)
        fields['course'] = summary['course']
        store.end_run(run, **fields)
        flying = (stage_tables.entry(fields.get('stage') or stage) or {}).get('finish_m')
        if summary['finished'] == 1 and game == 'acr' and flying:
            store.set_run_finish(run, flying)            # timed at the flying finish: never re-timed
        if summary['finished'] == 1 and summary.get('clock') == 'game':
            store.set_run_clock(run, 'game')             # the game's own time: no finish line of ours moves it
            self._learn_finish(store, run, stage, summary.get('clock_stop'))
        elif summary['finished'] == 1 and summary.get('clock') == 'line' and summary.get('finish_line'):
            store.set_run_finish(run, summary['finish_line'])    # the game's clock read at our line: moves with it
        store.add_trace(run, trace)
        self._work_over(run, summary, trace, fields)
        # The coach's tips for this run are there to read: said once they are committed, as a reader that
        # looks at the stamp reads the file
        self.learner.log.after_commit(self._history_changed)
        return True

    def _learn_finish(self, store, run, stage, stop):
        """The place the game stopped its clock is the stage's flying finish: kept per run, the stage's line learnt
        from them (Store.learn_finishes), and where that moved the line the runs timed to the old one are timed
        again (retime_finishes)."""
        if stop is None or not stage:
            return
        before = (stage_tables.entry(stage) or {}).get('finish_m')
        store.set_run_stop(run, stop)
        store.learn_finishes()
        after = (stage_tables.entry(stage) or {}).get('finish_m')
        if after is not None and (before is None or abs(after - before) >= FINISH_MOVED):
            retime_finishes(store)

    def _history_changed(self):
        self.learner.history_changed += 1

    def _work_over(self, run, summary, trace, verdicts, started=None, backfill=False):
        """What the run says about the driver (coach_context, coach.py): its
        corners with their phases, sections and losses, its events and
        class, the changes of gear classified and the metrics, tagged with
        the run's discipline and surface. Written for a run just ended, and
        again by the backfill for one from before."""
        learner, store = self.learner, self.learner.log.store
        row = store.run(run)
        if row is None:
            return
        started = row['started'] if row['started'] is not None else started
        stage = row['stage']
        course = summary.get('course')
        rows = coach_context.stage_rows(trace, course, row['finished'] == 1, row['result_time'])
        corners = find_corners(rows, summary.get('game'))
        car = _car_model(learner, store, row['car'], summary.get('car'))
        surface = verdicts.get('surface')
        context = coach.car_context(car, surface)
        shifts = store.run_shifts(run)
        profile = learner.profile
        reference = history = None
        others, last_started = [], None
        if stage:
            before = [r for r in store.stage_runs(stage, exclude=run, limit=60, car=row['car'], ranked=True)
                      if started is None or r['started'] <= started]
            last_started = before[0]['started'] if before else None
            ref = coach_context.reference_run(
                [r for r in before if r['run_class'] in ('clean', 'learning')], wet=verdicts.get('wet'))
            if ref is not None:
                ref_trace = store.trace(ref['id'])
                ref_corners = store.corners(ref['id'])
                if ref_trace and ref_corners:
                    reference = {'trace': coach_context.stage_rows(ref_trace, ref['course'], True, ref['result_time']),
                                 'corners': ref_corners, 'course': ref['course'], 'run': ref['id']}
            for r in before:
                if len(others) >= coach_context.SPREAD_RUNS - 1:
                    break
                if r['finished'] == 1 and r['run_class'] in ('clean', 'learning', 'off'):
                    found = store.corners(r['id'])
                    if found:
                        others.append((r, found))
        if surface is not None:
            history = [m['value'] for m in store.metric_series(profile, 'launch.t50', car=row['car'], surface=surface,
                                                               limit=coach_context.LAUNCH_HISTORY + 1)
                       if m['run'] != run][:coach_context.LAUNCH_HISTORY]
        stage_row = store.stage(stage) if stage else None
        stage_length, road = (stage_row or {}).get('length'), None
        if stage and stage.startswith('acr:'):
            # ACR sends the length of its spline, which is longer than the road a run drives
            road = stage_tables.road_length(stage_tables.entry(stage))
            stage_length = road or (stage_tables.entry(stage) or {}).get('length_m')
        analysis = coach_context.analyse(summary, rows, corners, shifts, context, started=started,
                                         reference=reference, history=history or (), others=others,
                                         last_started=last_started,
                                         stage_length=stage_length, road=bool(road))
        if not backfill:
            with learner.lock:
                learner.session_held_time += coach_context.held_seconds(analysis['episodes'])
        store.add_corners(run, corners)
        store.add_events(run, analysis['events'])
        klass = analysis['run_class']
        if klass in ('clean', 'learning') and store.finish_unknown(run):
            klass = 'partial'                            # timed to the old line and not re-timable (retime_finishes)
        store.update_run(run, course=course, run_class=klass)
        for s in analysis['shifts']:
            store.update_shift(s['id'], s['flags'], s['d'], s.get('band'))
        metrics = coach.run_metrics(summary, rows, TRACE_CHANNELS, analysis['shifts'], corners, context, analysis)
        metrics += coach.stage_metrics(store, run, stage, rows, TRACE_CHANNELS, corners, row['finished'], analysis,
                                       car=row['car'])
        # A value the trace could not give (NaN) is no measurement
        metrics = [m for m in metrics if m['value'] is not None and math.isfinite(m['value'])]
        for m in metrics:
            m['discipline'], m['surface'] = verdicts.get('discipline'), verdicts.get('surface')
        session = store.run_session(run)
        if session is not None and metrics:
            store.add_metrics(session, run, metrics)


def _car_model(learner, store, car_id, key):
    """The car's model as a copy: the learner's, else the stored one."""
    with learner.lock:
        car = learner._models.get(key) if key else None
        car = car.copy() if car is not None else None
    if car is None and car_id is not None:
        found = store.car_row(car_id)
        data = store.car(*found) if found else None
        if data is not None and data['model'].get('key'):
            try:
                car = CarModel.from_dict(data['model'])
            except (ValueError, KeyError, TypeError, AttributeError):
                car = None
    return car


# The metrics the backfill cannot work out again (the launch is not in the trace)
BACKFILL_KEEP = ('launch.t50', 'launch.stall', 'launch.slip')
BACKFILL_FAILED = 'unclassified'


def repair_shipped(store):
    """What the game's shipped data says beats what an older run or version
    wrote: the discipline of every run on a stage the table knows (a profile
    word or a shape said 'rally stage' of the Livigno circuit) and the
    drivetrain of every car the shipped car data knows (a learnt vote said
    'fwd' of a Fabia), and the finish line of ACR runs timed to the old one
    (retime_finishes), once at start. Returns the number of rows changed."""
    changed = 0
    for stage in store.stage_keys():
        found = drive_detect.table_discipline(stage)
        if found:
            try:
                changed += store.set_stage_discipline(stage, found, [
                    'The stage table says {}: {}.'.format(found.replace('-', ' '), stage)])
            except Exception:
                logging.exception("drive log: stage table discipline of %s", stage)
    for car, key, column, model in store.car_drivetrains():
        shipped = (car_data.entry(key) or {}).get('drivetrain')
        if shipped in DRIVEN and (column != shipped or model != shipped):
            store.set_car_drivetrain(car, shipped)         # the game's files beat a learnt vote
            changed += 1
    changed += learn_missing_stops(store)
    changed += retime_finishes(store)
    if changed:
        store.commit()
    return changed


TRACE_TAIL = 15.0               # m: a run's last 10 Hz row is up to a row's distance short of its finish (30 m/s: 3 m)


def _trace_t_at(trace, value, tail=0.0):
    """The trace's time at driven distance `value` (linear between rows), or None when the trace does not span
    it. With `tail`, a value up to that far past the last row is taken at the last row's speed."""
    previous = None
    for row in trace:
        d = row[T['distance']]
        if previous is not None and previous[T['distance']] <= value <= d:
            d0, t0 = previous[T['distance']], previous[T['t']]
            return t0 if d <= d0 else t0 + (row[T['t']] - t0) * (value - d0) / (d - d0)
        previous = row
    if tail and previous is not None and previous[T['distance']] < value <= previous[T['distance']] + tail:
        speed = previous[T['speed']]
        if speed is not None and speed > 1.0:
            return previous[T['t']] + (value - previous[T['distance']]) / speed
    return None


def _trace_speed_at(trace, value):
    """The speed of the last trace row at or before driven distance `value`."""
    speed = None
    for row in trace or ():
        if row[T['distance']] > value:
            break
        speed = row[T['speed']]
    return speed


# A run that ends this fast was already timed at the flying finish (marth's
# Afon Bidno runs: 120-160 km/h there, 20-40 km/h at the stop control)
AT_SPEED = 20.0                 # m/s
FINISH_MOVED = 0.5              # m: a finish line that moved less than this since a run was timed leaves it as it is


def _run_start_m(store, run, entry):
    """Where along the road a run's trace distance began: where the run was recorded to start, else the stage's
    start line, else None."""
    start = store.run_start(run)
    return start if start is not None else stage_tables.start_line(entry)


def learn_missing_stops(store):
    """A run on the game's clock recorded by a build that kept run_clock but not run_stop has no clock stop: the
    stop is where it began plus its course, taken when that is within the clock's range of the table's line
    (FINISH_CLOCK_BEFORE, FINISH_CLOCK_PAST). The stages' finish lines are learnt again (Store.learn_finishes) and the
    finished runs of a stage whose line was newly learnt or moved are queued for the backfill (run_class cleared), so
    their classes and potentials are worked out with the finish. Returns the number of stops added."""
    added, stages = 0, set()
    for run, stage, start, course in store.clock_runs_without_stop('acr'):
        entry = stage_tables.entry(stage) or {}
        line = entry.get('finish_m') or entry.get('pacenote_last_m')
        stop = start + course
        if line and line - FINISH_CLOCK_BEFORE <= stop <= line + FINISH_CLOCK_PAST:
            store.set_run_stop(run, stop)
            stages.add(stage)
            added += 1
    if not added:
        return 0
    before = {s: (stage_tables.entry(s) or {}).get('finish_m') for s in stages}
    store.learn_finishes()
    for stage in stages:
        after = (stage_tables.entry(stage) or {}).get('finish_m')
        if after is not None and (before[stage] is None or abs(after - before[stage]) >= FINISH_MOVED):
            store.queue_stage_runs(stage)
    return added


def retime_finishes(store):
    """A finished ACR run timed to the old line (the last pace note, the stop
    control: its time holds the slow-down) on a stage that now has a flying
    finish (finish_m) is timed again to it, once: the driven distance between
    the lines is pacenote_last_m - finish_m, so the new finish is that much
    before the run's course, and the time shortens by what the trace took to
    drive it. The run is queued for the backfill (run_class cleared) to
    recompute its corners, sections, losses and metrics with the new course.
    A run timed by the new code, or handled here, has a run_finish row. Runs
    without a trace or whose trace does not span the new line are marked
    (finish NULL) and left as they were. A run timed by the game's own clock
    (run_clock) is never re-timed: the game stopped it at its own line,
    wherever the tables put theirs. Returns the number re-timed."""
    done = 0
    store.forget_unknown_finishes_without_course()       # an older build marked every run of a v2 database
    for run, stage, result, course in store.finished_untimed('acr'):
        entry = stage_tables.entry(stage) or {}
        flying, old = entry.get('finish_m'), entry.get('pacenote_last_m')
        if not flying or not old or flying >= old:
            continue                                     # no flying finish known: the run stays as timed
        try:
            trace = store.trace(run)
            if course is None and trace:
                course = trace[-1][T['distance']]        # a database migrated from version 2 has no course
            end_speed = _trace_speed_at(trace, course) if trace and course is not None else None
            if end_speed is not None and end_speed > AT_SPEED:
                store.set_run_finish(run, flying)        # it ends at speed: timed at the flying finish already
                continue
            # The trace's distance is measured from where the run began; the course is where the car finally
            # stopped, seconds after the stop control: the lines are anchored on the road, not on the course
            start = _run_start_m(store, run, entry)
            d_old = min(course, old - start) if start is not None and course is not None else None
            new = flying - start if d_old is not None else None
            t_old = _trace_t_at(trace, d_old, TRACE_TAIL) if trace and new is not None else None
            t_new = _trace_t_at(trace, new) if t_old is not None and new > 0 else None
            if t_new is None or result is None or t_old - t_new >= result:
                logging.warning("drive log: run %s cannot be re-timed to the flying finish (no trace or out of range)",
                                run)
                store.set_run_finish(run, None)
                # Its time holds the slow-down to the stop control, next to flying-finish times: not for
                # ranking (best, reference, sum of best) on a stage with a flying finish. Kept, not deleted
                if store.run(run)['run_class'] is not None:      # one still to be worked out is said by _work_over
                    store.update_run(run, run_class='partial')
                continue
            store.update_run(run, result_time=result - (t_old - t_new), course=new, run_class=None)
            store.set_run_finish(run, flying)
            done += 1
        except Exception:
            logging.exception("drive log: re-timing run %s", run)
    # A finish line refined since (the table's finish_m moved): a run timed at the old one is shifted by the
    # trace's time between the two lines. Out of the trace's range, it stays as it was
    for run, stage, result, course, recorded in store.finished_timed('acr'):
        flying = (stage_tables.entry(stage) or {}).get('finish_m')
        if not flying or recorded is None or abs(flying - recorded) < FINISH_MOVED or result is None \
                or course is None:
            continue
        try:
            trace = store.trace(run)
            start = _run_start_m(store, run, stage_tables.entry(stage) or {})
            d_old = min(course, recorded - start) if start is not None else None
            new = flying - start if d_old is not None else None
            t_old = _trace_t_at(trace, d_old, TRACE_TAIL) if trace and d_old is not None else None
            t_new = _trace_t_at(trace, new) if t_old is not None and new > 0 else None
            if t_new is None or result - (t_old - t_new) <= 0:
                logging.warning("drive log: run %s cannot be moved from finish %.1f to %.1f (trace out of range)",
                                run, recorded, flying)
                continue
            store.update_run(run, result_time=result - (t_old - t_new), course=new, run_class=None)
            store.set_run_finish(run, flying)
            done += 1
        except Exception:
            logging.exception("drive log: moving the finish of run %s", run)
    return done


def backfill_step(learner, limit=3):
    """Work the oldest runs from before the context layer over again (their
    corners, events, class and metrics), at most `limit` of them: runs with a
    trace and no class. The trace does not hold what the run tracker's
    summary did (the launch, the game's gear count): the launch metrics that
    were written stay, the rest is recomputed from what is stored. Returns
    the number of runs worked over, 0 when nothing is left. A run that fails is
    left as it was (the rewrite is rolled back) and is not tried again until
    the app next starts."""
    store = learner.log.store
    failed = learner.__dict__.setdefault('_backfill_failed', set())
    if not learner.__dict__.get('_repaired'):
        learner._repaired = True
        repair_shipped(store)
    done = 0
    for run in store.runs_to_backfill(limit + len(failed)):
        if run in failed:
            continue
        if done >= limit:
            break
        store.savepoint('backfill')
        try:
            _backfill_run(learner, store, run)
            store.release_savepoint('backfill')
        except Exception:
            logging.exception("drive log: backfill of run %s", run)
            store.rollback_savepoint('backfill')
            failed.add(run)
        done += 1
    if done < limit:
        done += _potentials_step(learner, store)
    return done


def _potentials_step(learner, store):
    """Where no run is left to work over: the potential of one stage and car that has finished ranked runs and none
    stored (a stage driven before the potential existed, or whose runs were re-classed since), the newest first. One
    per call, so a batch stays short; a pair that came to nothing (too few runs) is not tried again until the app
    next starts. Returns 1 when a potential was built, else 0."""
    tried = learner.__dict__.setdefault('_potential_tried', set())
    for stage, car, run in store.potentials_missing():
        if (stage, car) in tried:
            continue
        tried.add((stage, car))
        return 1 if potential.after_run(store, run) is not None else 0
    return 0


def _backfill_run(learner, store, run):
    row = store.run(run)
    trace = store.trace(run)
    if row is None or not trace:
        store.update_run(run, run_class=BACKFILL_FAILED)
        return
    car_row = store.car_row(row['car'])
    car_key = car_row[1] if car_row else None
    last = trace[-1][T['distance']] if trace else 0.0
    course = row['course'] if row['course'] is not None else last
    summary = {'game': (car_key or '').split('/')[0] or None, 'stage': row['stage'], 'distance': row['distance'] or 0.0,
               'duration': row['duration'], 'moving_time': row['moving_time'], 'finished': row['finished'],
               'result_time': row['result_time'], 'course': course, 'car': car_key, 'launch': 0.0}
    store.clear_derived(run, BACKFILL_KEEP)
    verdicts = {'surface': row['surface'], 'discipline': row['discipline'], 'wet': row['wet']}
    if row['stage'] and row['discipline'] in (None, 'unknown'):
        found = drive_detect.table_discipline(row['stage'])
        if found:
            store.update_run(run, discipline=found, discipline_conf='game')
            verdicts['discipline'] = found
    learner.runs._work_over(run, summary, trace, verdicts, row['started'], backfill=True)


def stage_surface(store, game, stage):
    """The one surface a stage is on, as far as known before driving it:
    the shipped table's word (a stage mostly on one surface counts as it,
    stage_tables.surface_of), else what its earlier runs taught (the
    stage's learnt prior). None for a mixed or unknown one."""
    if stage is None:
        return None
    row = store.stage(stage)
    surface, _prior = drive_detect.route_surface(game, row.get('location') if row else None, stage)
    if surface in SURFACES:
        return surface
    if row and row.get('surface_prior') in SURFACES and row.get('surface_prior_source') in ('learnt', 'user'):
        return row['surface_prior']
    return None


def start_cell(trace):
    """(x / 50 m, z / 50 m, heading / 45°) of where the run starts, from
    the first rows that moved 20 m; None without positions."""
    first = None
    for row in trace:
        x, z = row[T['x']], row[T['z']]
        if math.isnan(x) or math.isnan(z):
            return None
        if first is None:
            first = (x, z)
        elif math.hypot(x - first[0], z - first[1]) >= 20.0:
            heading = math.degrees(math.atan2(z - first[1], x - first[0])) % 360.0
            return (int(first[0] // 50), int(first[1] // 50), int((heading + 22.5) // 45) % 8)
    return None


def _values(rows, name):
    i = S[name]
    return [r[i] for r in rows if r[i] is not None]


def _quantile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _correlation(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _high_pass(values, width):
    """Each value less the mean of the `width` around it: what shakes faster
    than the road's shape."""
    n = len(values)
    half = max(1, width // 2)
    out = []
    total = sum(values[:half])
    count = half
    for i in range(n):
        if i + half < n:
            total += values[i + half]
            count += 1
        if i - half - 1 >= 0:
            total -= values[i - half - 1]
            count -= 1
        out.append(values[i] - total / count)
    return out


G = 9.80665
SPIN_KAPPA = 0.08                # driven-wheel slip that counts as spin (calibrate per game)
LIMIT_RATIO = 1.5                # mu_p95 / mu_p50 at least this: the segment reached the grip limit (calibrate)
LIMIT_SAMPLES = 120              # samples above 10 m/s the segment needs to vote


def segment_features(rows):
    """What ~200 m of driving says about the surface (docs, section 8.6):
    a dict of numbers, None where the game sends too little."""
    features = {'n': len(rows), 'speed_mean': sum(r[S['speed']] for r in rows) / len(rows) if rows else None}
    mu = [math.hypot(r[S['a_long']], r[S['a_lat']]) / G for r in rows
          if 10.0 <= r[S['speed']] <= 25.0 and r[S['a_long']] is not None and r[S['a_lat']] is not None]
    features['mu_p95'] = _quantile(mu, 0.95) if len(mu) >= 20 else None
    features['mu_p50'] = _quantile(mu, 0.5) if len(mu) >= 20 else None
    features['fast'] = sum(1 for r in rows if r[S['speed']] > 10.0)
    driving = [r for r in rows if r[S['throttle']] is not None and r[S['throttle']] >= 0.95
               and r[S['gear']] is not None and r[S['gear']] >= 3 and r[S['slip']] is not None]
    features['spin'] = (sum(1 for r in driving if r[S['slip']] > SPIN_KAPPA) / len(driving)) if len(driving) >= 10 \
        else None
    # Suspension: the game's velocity, or its travel differenced
    times = [r[S['t']] for r in rows]
    rate = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else None
    left, right = _values(rows, 'susp_vel_fl'), _values(rows, 'susp_vel_fr')
    if len(left) != len(rows) or len(right) != len(rows):
        fl, fr = _values(rows, 'susp_fl'), _values(rows, 'susp_fr')
        if len(fl) == len(rows) and len(fr) == len(rows) and rate:
            left = [(b - a) * rate for a, b in zip(fl, fl[1:])]
            right = [(b - a) * rate for a, b in zip(fr, fr[1:])]
        else:
            left = right = []
    if left and rate and features['speed_mean']:
        width = int(rate / 5.0)                      # above ~5 Hz: the surface, not the road's shape
        shake = _high_pass([(a + b) / 2 for a, b in zip(left, right)], width)
        features['rough'] = math.sqrt(sum(v * v for v in shake) / len(shake)) / max(features['speed_mean'], 1.0)
        features['lr_corr'] = _correlation(left, right)
    else:
        features['rough'] = features['lr_corr'] = None
    features['post_peak'] = _post_peak(rows)
    puddles = _values(rows, 'puddle')
    features['puddle'] = sum(1 for p in puddles if p > 0) / len(puddles) if puddles else None
    rumble = _values(rows, 'rumble')
    features['rumble'] = sum(rumble) / len(rumble) if rumble else None
    return features


def _post_peak(rows):
    """How lateral grip falls off past its peak against the tyres' slip
    angle (a slope, g per rad), where the game sends slip angles."""
    bins = {}
    for r in rows:
        angle, lateral = r[S['slip_angle']], r[S['a_lat']]
        if angle is not None and lateral is not None:
            bins.setdefault(int(angle / 0.02), []).append(abs(lateral) / G)
    means = sorted((b * 0.02 + 0.01, sum(v) / len(v)) for b, v in bins.items() if len(v) >= 5)
    if len(means) < 4:
        return None
    peak = max(range(len(means)), key=lambda i: means[i][1])
    beyond = means[peak:]
    if len(beyond) < 3:
        return None
    mx = sum(x for x, _ in beyond) / len(beyond)
    my = sum(y for _, y in beyond) / len(beyond)
    sxx = sum((x - mx) ** 2 for x, _ in beyond)
    return sum((x - mx) * (y - my) for x, y in beyond) / sxx if sxx else None


def pushed(features):
    """1 when the segment reached the grip limit (it votes on the surface),
    0 when it never came near it, None when it cannot tell."""
    if features.get('mu_p95') is None:
        return None
    return int(features['mu_p95'] >= LIMIT_RATIO * features['mu_p50'] and features['fast'] >= LIMIT_SAMPLES)


CORNER_YAW = 0.15                # rad/s, smoothed over CORNER_SMOOTH
CORNER_SMOOTH = 0.5              # s
CORNER_TIME = 1.0                # s above CORNER_YAW...
CORNER_HEADING = 30.0            # ...or this many degrees of heading: a corner (calibrate)
CORNER_SIDE = 2.0                # s either side of the apex for entry and exit speeds
RADIUS_SPEED = 5.0               # m/s: slower than this the speed over the yaw rate (a crash row, a stop) is no radius
RADIUS_YAW = 0.1                 # rad/s: slower turning than this has no radius worth the name


def _yaw_rates(trace, game=None):
    """Yaw rate per trace row: the game's, or the heading's rate of change
    from positions (on the map's axes: plan_xy of the game)."""
    yaw = [row[T['yaw_rate']] for row in trace]
    if all(not math.isnan(v) for v in yaw):
        return yaw
    out = [float('nan')] * len(trace)
    for i in range(1, len(trace)):
        a, b = trace[i - 1], trace[i]
        dt = b[T['t']] - a[T['t']]
        if i < 2 or dt <= 0:
            continue
        c = trace[i - 2]
        pa, pb, pc = (plan_xy(game, (r[T['x']], r[T['y']], r[T['z']])) for r in (a, b, c))
        h1 = math.atan2(pa[1] - pc[1], pa[0] - pc[0])
        h2 = math.atan2(pb[1] - pa[1], pb[0] - pa[0])
        change = (h2 - h1 + math.pi) % (2 * math.pi) - math.pi
        out[i] = change / (b[T['t']] - a[T['t']] + (a[T['t']] - c[T['t']])) * 2
    return out


def find_corners(trace, game=None):
    """The run's corners from its trace: dicts for the corners table. `game` says the world's axes where the
    yaw rate is worked out from positions."""
    if len(trace) < 10:
        return []
    raw = _yaw_rates(trace, game)
    half = max(1, int(CORNER_SMOOTH / TRACE_EVERY / 2))
    smooth = []
    for i in range(len(raw)):
        window = [v for v in raw[max(0, i - half):i + half + 1] if not math.isnan(v)]
        smooth.append(sum(window) / len(window) if window else 0.0)
    corners = []
    i = 0
    n = len(trace)
    while i < n:
        if abs(smooth[i]) <= CORNER_YAW:
            i += 1
            continue
        sign = 1 if smooth[i] > 0 else -1
        j = i
        while j + 1 < n and smooth[j + 1] * sign > CORNER_YAW:
            j += 1
        corner = _corner(trace, smooth, i, j, sign)
        if corner is not None:
            corners.append(corner)
        i = j + 1
    return corners


def _corner(trace, yaw, i, j, sign):
    t = [row[T['t']] for row in trace]
    duration = t[j] - t[i]
    heading = abs(sum(yaw[k] * (t[k + 1] - t[k]) for k in range(i, j) if k + 1 < len(t)))
    if duration < CORNER_TIME and math.degrees(heading) < CORNER_HEADING:
        return None
    speed = [row[T['speed']] for row in trace]
    apex = min(range(i, j + 1), key=lambda k: speed[k])

    def at(seconds):
        target = t[apex] + seconds
        k = bisect.bisect_left(t, target)
        if k == len(t) or (k > 0 and target - t[k - 1] <= t[k] - target):
            k -= 1
        return speed[k]
    gears = [row[T['gear']] for row in trace[i:j + 1] if not math.isnan(row[T['gear']]) and row[T['gear']] >= 1]
    steer = [(row[T['steer']], yaw[i + k]) for k, row in enumerate(trace[i:j + 1]) if not math.isnan(row[T['steer']])]
    against = [s for s, y in steer if abs(s) > 0.02]
    handbrake = [row[T['handbrake']] for row in trace[i:j + 1] if not math.isnan(row[T['handbrake']])]
    spin = [row[T['slip_drive']] for row in trace if t[apex] <= row[T['t']] <= t[apex] + CORNER_SIDE
            and not math.isnan(row[T['slip_drive']])]
    speeds_in = [(speed[k] / abs(yaw[k]), k) for k in range(i, j + 1) if abs(yaw[k]) >= RADIUS_YAW and speed[k] > RADIUS_SPEED]
    radius = min(speeds_in)[0] if speeds_in else None
    heading_deg = math.degrees(heading)
    return {
        'd': trace[apex][T['distance']], 'direction': sign,
        'd0': trace[i][T['distance']], 'd1': trace[j][T['distance']], 'radius': radius,
        'tightness': coach_context.tightness(radius, heading_deg, speed[apex]),
        '_i0': i, '_i1': j, '_apex': apex,
        'entry_speed': at(-CORNER_SIDE), 'min_speed': speed[apex], 'exit_speed': at(CORNER_SIDE),
        'gear_min': int(min(gears)) if gears else None,
        'heading_change': math.degrees(heading), 'duration': duration,
        # Steering against the turn (steer and yaw rate both positive left)
        'counter_steer': (sum(1 for s, y in steer if abs(s) > 0.02 and s * y < 0) / len(against)) if against else None,
        'handbrake': int(max(handbrake) > 0.5) if handbrake else None,
        'exit_spin': max(spin) if spin else None,
    }
