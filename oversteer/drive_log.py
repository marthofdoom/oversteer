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

import logging
import math
import queue
import threading
import time

from .shift_learner import drive_slip
from .telemetry_store import open_store, TRACE_CHANNELS

QUEUE_SIZE = 8192
COMMIT_EVERY = 5.0               # seconds a batch of writes may wait
TICK_EVERY = 1.0                 # seconds between tick() calls on the thread


class DriveLog:
    """Owns the writer (a telemetry_store.Store) and, when `threaded`, the
    thread that uses it. `post(fn, *args)` never blocks: when the queue is
    full the event is dropped and counted in `dropped`. An event function
    returns True to have its writes committed at once. `ticks` are called
    about once a second on the thread (publishing snapshots)."""

    def __init__(self, path, threaded=True):
        self.path = path
        self.store = open_store(path)
        self.threaded = threaded
        self.dropped = 0
        self.ticks = []
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread = None
        self._commit_at = time.monotonic()
        if threaded:
            self._thread = threading.Thread(target=self._run, name='drive-log', daemon=True)
            self._thread.start()

    def post(self, fn, *args):
        if self._thread is None:
            self._handle(fn, args)
            self.store.commit()
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
            return result
        done = threading.Event()
        box = []

        def run():
            box.append(fn(*args))
            done.set()
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
            self._thread = None
        self.store.commit()

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
        except Exception as e:
            logging.warning("drive log: can't write the telemetry database: %s", e)
            try:
                self.store.rollback()
            except Exception:
                pass
        self._commit_at = time.monotonic()


# -- runs, segments, corners (docs/telemetry-coaching.md, section 8.4) --

TELEPORT = 50.0                  # m between consecutive positions: a restart or a new stage
TELEPORT_SPEED = 100.0           # m/s implied between two packets: the same
NO_POSITION_GAP = 10.0           # s of silence that end a run in a game that sends no position
RESTART_DROP = 1.0               # s the stage clock must go back by to be a restart
MOVING = 1.0                     # m/s
START_MOVING = 3.0               # m/s: the run starts when the car first goes this fast
STOP = 3.0                       # s standing still after the start: a stop
LAUNCH_THROTTLE = 0.5            # held while standing before the start: a launch
SEGMENT = 200.0                  # m per segment
SEGMENT_MIN = 50.0               # m: a shorter tail is not kept
TRACE_EVERY = 0.1                # s between trace rows
RUN_MIN = 100.0                  # m moving: a shorter run (menus, a car parked) is not kept
FINISHED = 0.99                  # progress through the stage that counts as reaching the end

# One row per packet of the segment being driven, for its features
SEGMENT_CHANNELS = ('t', 'speed', 'a_long', 'a_lat', 'yaw_rate', 'throttle', 'gear', 'slip', 'susp_fl', 'susp_fr',
                   'susp_vel_fl', 'susp_vel_fr', 'slip_angle', 'puddle', 'rumble', 'steer')
S = {name: i for i, name in enumerate(SEGMENT_CHANNELS)}
T = {name: i for i, name in enumerate(TRACE_CHANNELS)}


def _nan(x):
    return float('nan') if x is None else x


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
        self._n_in_session = {}          # session number -> runs started in it
        self._reset()
        self._waiting()

    def _reset(self):
        self.run = None
        self._last_t = None
        self._last_pos = None
        self._last_stage_time = None
        self._last_lap = None
        self._last_speed = None

    def _waiting(self):
        """Not driving yet: how long the car has stood, and whether with the
        revs held (a launch)."""
        self._stood = 0.0
        self._launch = 0.0
        self._after_end = False

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
            if self._after_end and sample.packet != 'start':
                self._remember(now, sample)
                return                               # EA sent the end of the stage: wait for the next start
            if speed < START_MOVING:
                if speed < MOVING:
                    self._stood += dt
                    if throttle is not None and throttle >= LAUNCH_THROTTLE:
                        self._launch += dt
                    else:
                        self._launch = 0.0
                self._remember(now, sample)
                return
            self._start(now, sample, session, profile)
        self._track(now, sample, throttle, dt, speed)
        self._remember(now, sample)
        if sample.packet == 'end':
            self.end(now, 'end')
            self._after_end = True

    def _remember(self, now, sample):
        self._last_t = now
        if sample.pos is not None:
            self._last_pos = sample.pos
        if sample.stage_time is not None:
            self._last_stage_time = sample.stage_time
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
            jump = math.sqrt(sum((a - b) ** 2 for a, b in zip(pos, last)))
            if jump > TELEPORT or (gap > 0 and jump / gap > TELEPORT_SPEED):
                return 'teleport'
        elif pos is None and gap > NO_POSITION_GAP:
            return 'silence'
        stage_time, last_time = sample.stage_time, self._last_stage_time
        if stage_time is not None and last_time is not None and stage_time < last_time - RESTART_DROP:
            laps, lap = sample.laps, sample.lap
            if not (laps and laps > 1 and lap is not None and self._last_lap is not None and lap > self._last_lap):
                return 'restart'                     # the clock went back without a lap done
        return None

    def _start(self, now, sample, session, profile):
        learner = self.learner
        self._runs += 1
        self.run = self._runs
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
        self._seg_d0, self._seg_t0 = 0.0, now
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
        self._puddles = self._samples = 0
        self._paused = sample.packet == 'pause'
        self._packets = {sample.packet} if sample.packet else set()
        self._summary = {
            'game': sample.game, 'profile': profile, 'stage': sample.stage, 'stage_length': sample.stage_length,
            'laps': sample.laps, 'has_pos': sample.pos is not None, 'standing': self._stood,
            'launch': self._launch, 'track': sample.track}
        self._stood = self._launch = 0.0
        learner.log.post(self._write_start, self.run, session, n, self._wall0, sample.stage, sample.game,
                         sample.stage_length, list(sample.pos) if sample.pos is not None else None)

    def _d(self, sample):
        """Where along the run the car is: the game's stage distance where
        it sends one (the same stretch of road gets the same d in every
        run), else the distance driven."""
        game = sample.distance if sample.distance is not None else (
            sample.lap_distance if not (sample.laps and sample.laps > 1) else None)
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
        if not self._paused and not frozen:
            self._duration += dt
            self._distance += speed * dt
            if speed > MOVING:
                self._moving += dt
                self._still = 0.0
            else:
                self._still += dt
                if self._still - dt < STOP <= self._still:
                    self._stops += 1
        if sample.laps is not None:
            summary['laps'] = max(summary['laps'] or 0, sample.laps)
        if sample.stage_length and not summary['stage_length']:
            summary['stage_length'] = sample.stage_length
        d = self._d(sample)
        lap = sample.lap
        if lap is not None and self._last_lap is not None and lap > self._last_lap:
            self._laps_done += 1
            self.learner.log.post(self._write_lap, self.run, self._laps_done, now - self._lap_t0, d - self._lap_d0)
            self._lap_t0, self._lap_d0 = now, d
        if lap is not None:
            self._last_lap = lap
        if sample.progress is not None:
            if self._finished is None and sample.progress >= FINISHED and (self._progress or 0.0) < FINISHED:
                self._finished, self._result_time = 1, sample.stage_time
            self._progress = sample.progress
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
        slip = drive_slip(sample, self.learner.car.drivetrain if self.learner.car else None)
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
            self._trace.append(tuple(_nan(v) for v in (
                now - self._t0, d, speed, sample.rpm, sample.gear, throttle, sample.brake, sample.clutch,
                sample.handbrake, sample.steer, a_long, a_lat, sample.yaw_rate, slip, rms, pos[0], pos[1], pos[2])))
        if d - self._seg_d0 >= SEGMENT:
            self._close_segment(now, d)

    def _close_segment(self, now, d):
        rows, self._rows = self._rows, []
        if d - self._seg_d0 >= SEGMENT_MIN and rows:
            self.learner.log.post(self._write_segment, self.run, self._seg_d0, d, self._seg_t0 - self._t0,
                                  now - self._t0, rows)
        self._seg_d0, self._seg_t0 = d, now

    def end(self, now, reason):
        """The run is over (`reason`: 'session', 'restart', 'teleport',
        'silence', 'end')."""
        if self.run is None:
            return
        self._close_segment(self._last_t or now, self._trace[-1][T['distance']] if self._trace else 0.0)
        summary = dict(self._summary)
        finished = self._finished
        if reason == 'end':
            finished = 1 if (self._progress or 0.0) >= FINISHED - 0.01 else 0
        elif finished is None and reason in ('restart', 'teleport'):
            finished = 0
        summary.update(distance=self._distance, duration=self._duration, moving_time=self._moving,
                       stops=self._stops, finished=finished, result_time=self._result_time, laps_done=self._laps_done,
                       packets=sorted(self._packets), end=reason,
                       puddles=self._puddles / self._samples if self._samples else None)
        self.learner.log.post(self._write_end, self.run, self.learner.wall(self._last_t or now), summary, self._trace)
        self._reset()
        self._waiting()

    # -- the drive-log thread --

    def _write_start(self, number, session, n, started, stage, game, stage_length, start_pos):
        learner = self.learner
        row = learner._session_rows.get(session)
        if row is None:
            return
        store = learner.log.store
        if stage is None and game in ('dirt', 'wrcg') and stage_length and start_pos is not None:
            # DiRT names no stage: its length and where it starts do
            stage = store.match_stage(game, stage_length, start_pos[2])
        self.run_rows[number] = store.start_run(row[0], n, started, stage, game, stage_length, start_pos)

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
        run = self.run_rows.get(number)
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
        if stage is None and summary['has_pos'] and summary['game']:
            cell = start_cell(trace)
            if cell is not None:
                stage = fields['stage'] = store.match_cell(summary['game'], cell, summary['distance'])
                fields['stage_game'] = summary['game']
        summary['stage'] = stage
        verdicts = self.learner.detect(summary, trace, store)
        fields.update(verdicts)
        store.end_run(run, **fields)
        store.add_trace(run, trace)
        store.add_corners(run, find_corners(trace))
        return True


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


def _yaw_rates(trace):
    """Yaw rate per trace row: the game's, or the heading's rate of change
    from positions."""
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
        h1 = math.atan2(-(a[T['z']] - c[T['z']]), a[T['x']] - c[T['x']])
        h2 = math.atan2(-(b[T['z']] - a[T['z']]), b[T['x']] - a[T['x']])
        change = (h2 - h1 + math.pi) % (2 * math.pi) - math.pi
        out[i] = change / (b[T['t']] - a[T['t']] + (a[T['t']] - c[T['t']])) * 2
    return out


def find_corners(trace):
    """The run's corners from its trace: dicts for the corners table."""
    if len(trace) < 10:
        return []
    raw = _yaw_rates(trace)
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
        k = min(range(len(t)), key=lambda k: abs(t[k] - target))
        return speed[k]
    gears = [row[T['gear']] for row in trace[i:j + 1] if not math.isnan(row[T['gear']]) and row[T['gear']] >= 1]
    steer = [(row[T['steer']], yaw[i + k]) for k, row in enumerate(trace[i:j + 1]) if not math.isnan(row[T['steer']])]
    against = [s for s, y in steer if abs(s) > 0.02]
    handbrake = [row[T['handbrake']] for row in trace[i:j + 1] if not math.isnan(row[T['handbrake']])]
    spin = [row[T['slip_drive']] for row in trace if t[apex] <= row[T['t']] <= t[apex] + CORNER_SIDE
            and not math.isnan(row[T['slip_drive']])]
    return {
        'd': trace[apex][T['distance']], 'direction': sign,
        'entry_speed': at(-CORNER_SIDE), 'min_speed': speed[apex], 'exit_speed': at(CORNER_SIDE),
        'gear_min': int(min(gears)) if gears else None,
        'heading_change': math.degrees(heading), 'duration': duration,
        # Steering against the turn (steer and yaw rate both positive left)
        'counter_steer': (sum(1 for s, y in steer if abs(s) > 0.02 and s * y < 0) / len(against)) if against else None,
        'handbrake': int(max(handbrake) > 0.5) if handbrake else None,
        'exit_spin': max(spin) if spin else None,
    }
