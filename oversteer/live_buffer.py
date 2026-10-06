"""The run going on, live: a ring of its last 30 s at 10 Hz and its delta to
the coach's reference run (docs/telemetry-ui-design.md, section 9.1 and
"Phase 3 live API").

Threads (tier A):

- Every write to a LiveBuffer is under the learner's lock (learner.lock):
  RunTracker calls start_run(), push() (once per 10 Hz trace row), finish(),
  mark_mid_stage() and end_run() from the listener thread. Each call is constant time but for a bisect
  into the reference (O(log n)); nothing here reads the database or blocks.
- The **drive-log thread** loads the reference (load_reference(), over the
  writer's connection) and hands it over with offer_reference(): one
  attribute store of an immutable tuple. The listener adopts it on its next
  call; nothing else is shared with that thread.
- **Readers** (web server threads, the GTK main thread) call read(): no
  lock. They see the state the listener last published, one immutable dict
  swapped in by a single attribute store, and the ring's slots, each an
  immutable (seq, row) tuple stored by one list item assignment. A slot
  whose seq is not the one looked for was overwritten after the state was
  read (the reader is a whole ring behind) and is skipped.

Attribute and list item stores of a reference are atomic in CPython, with
or without the GIL (free-threaded builds lock per object), so no reader
ever sees a half-written state or row.
"""

import bisect
import functools
import logging
import math
import time

from . import coach_context, stage_tables
from .telemetry_formats import plan_xyz
from .telemetry_store import TRACE_CHANNELS

RATE = 10.0                      # rows per second (drive_log.TRACE_EVERY)
CAPACITY = 300                   # rows kept: 30 s
STALE = 2.0                      # s without a row while a run is on: 'stale' (a pause, a loading screen, a lost link)
MID_STAGE_SLACK = 50.0           # m: a run whose first row is further than this past the reference's start began mid-stage
EPOCH = '{:x}'.format(int(time.time() * 1000))     # per process: a page that saw another one starts over
ROUND = 3                        # decimals of a row's values on the wire

# The channels of a row, in order: the trace's (telemetry_store.TRACE_CHANNELS)
# but slip_drive and susp_rms, then the delta to the reference there
CHANNELS = ('t', 'distance', 'speed', 'rpm', 'gear', 'throttle', 'brake', 'clutch', 'handbrake', 'steer', 'a_long',
            'a_lat', 'yaw_rate', 'x', 'y', 'z', 'delta')
_PICK = tuple(TRACE_CHANNELS.index(c) for c in CHANNELS[:-1])
_T, _D = TRACE_CHANNELS.index('t'), TRACE_CHANNELS.index('distance')
_X = CHANNELS.index('x')                          # x, y, z follow each other in CHANNELS


ERROR_EVERY = 60.0               # s between two logs of a failing writer


def _guarded(method):
    """A writer called from RunTracker: a fault here must never break the
    run's tracking (it would lose the run), so it is logged (once a minute
    at most) and the live view goes without."""
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            now = time.monotonic()
            if self._error_at is None or now - self._error_at >= ERROR_EVERY:
                self._error_at = now
                logging.exception("live view")
            return None
    return call


def _fin(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


class Reference:
    """The run the live run is measured against, read once (load_reference)
    and never changed after: shared by the drive-log and listener threads."""

    __slots__ = ('run', 'stage', 'result_time', 'course', 'trace', 'track', 'splits', 'sectors', 'sector_start', 'origin', 'view')

    def __init__(self, run, stage, result_time, course, trace, splits=(), sectors=(), sector_start=None, origin=None):
        self.run = run                              # runs.id
        self.stage = stage
        self.result_time = result_time              # s, the run's own clock to the finish
        self.course = course                        # m where it finished, along its trace
        self.trace = tuple(trace)                   # stage_rows of its trace
        self.track = tuple(coach_context.along(self.trace))
        self.splits = tuple(splits)                 # ((name, d0, d1), ...): the coach's grid
        self.sectors = tuple(sectors)               # ((name, d0, d1), ...): the game's sectors
        self.sector_start = sector_start            # m along the road spline where the sectors' d is 0 (where the reference began)
        self.origin = origin if origin is not None else sector_start      # m along the road spline where its distance 0 is
        self.view = {'run': self.run, 'time': self.result_time, 'course': self.course,       # never changed
                     'splits': [{'name': n, 'd0': a, 'd1': b} for n, a, b in self.splits],
                     'sectors': [{'name': n, 'd0': a, 'd1': b} for n, a, b in self.sectors]}

    def t_at(self, d):
        """When the reference first passed `d` m (interpolated, as the coach
        times a section), or None outside its trace."""
        if not self.track or d is None:
            return None
        return coach_context.time_at(self.trace, self.track, d)


def load_reference(store, stage, car, exclude=None):
    """The coach's reference for a run of `car` (cars.id) on `stage`, as a
    Reference, or None: coach_context.reference_run over the car's clean and
    learning runs of the stage, as coach.splits() and the drive log's own
    analysis pick it (the wetness of a run just started is not known: any
    run compares). Its trace is cut as the coach cuts it (stage_rows), its
    splits are the sections of its grid (coach_context.grid_of: the bounds
    coach.splits() times), its sectors the stage's own
    (stage_tables.sector_bounds, from where the reference began). Reads the database:
    the drive-log thread only."""
    if not stage or car is None:
        return None
    before = store.stage_runs(stage, exclude=exclude, limit=60, car=car, ranked=True)
    ref = coach_context.reference_run([r for r in before if r['run_class'] in ('clean', 'learning')])
    if ref is None:
        return None
    trace = store.trace(ref['id'])
    if not trace:
        return None
    rows = coach_context.stage_rows(trace, ref['course'], True, ref['result_time'])
    if not rows:
        return None
    splits = ()
    corners = store.corners(ref['id'])
    if corners:
        grid, bounds = coach_context.grid_of({'trace': rows, 'corners': corners, 'course': ref['course']})
        splits = tuple((coach_context.section_name(s), a, b) for s, (a, b) in zip(grid, bounds))
    sectors, sector_start = (), None
    entry = stage_tables.entry(stage)
    placed = stage_tables.sector_bounds(entry)
    origin = store.run_start(ref['id'])
    if origin is None:
        origin = stage_tables.run_origin(entry)
    if placed is not None:
        # from where the reference began (its trace's distance 0), as coach._sectors places a run's lines
        start = sector_start = placed['start_m'] if origin is None else origin
        sectors = tuple(('S{}'.format(i + 1), a - start, b - start) for i, (a, b) in enumerate(placed['bounds']))
    return Reference(ref['id'], stage, ref['result_time'], ref['course'], rows, splits, sectors, sector_start, origin)


JOIN_SLACK = 30.0                # m: a first row this far into split 0 or less is the run leaving the line


class _Splits:
    """Where the live run is among a list of (name, d0, d1) and what it has
    lost to the reference since the current one began. Listener thread."""

    __slots__ = ('bounds', 'starts', 'index', 'entry', 'prev')

    def __init__(self, bounds):
        self.bounds = bounds
        self.starts = [a for _, a, _ in bounds]
        self.index = None               # the split the car is in
        self.entry = None               # the delta where it entered it (None: not known)
        self.prev = None                # (index, s lost in it) of the split last left

    def feed(self, d, delta_at):
        """The car is at `d`; `delta_at(x)` gives the delta at x m (x up
        to `d`, between the last row and this one)."""
        if not self.bounds:
            return
        i = bisect.bisect_right(self.starts, d) - 1
        if i < 0 or d > self.bounds[-1][2]:
            i = None
        if i == self.index:
            return
        if self.index is not None and (i == self.index + 1 or (i is None and self.index == len(self.bounds) - 1)):
            # Left the split for the next (or past the last one's end): it is timed
            edge = self.bounds[self.index][2]
            at = delta_at(edge)
            if at is not None and self.entry is not None:
                self.prev = (self.index, at - self.entry)
            self.entry = at if i is not None else None
        elif i == 0 and self.index is None and d - self.starts[0] <= JOIN_SLACK:
            self.entry = delta_at(self.starts[0])
        else:
            # Joined part way (the reference arrived late, a reset up or down
            # the road): this split is not timed
            self.entry = None
        self.index = i

    def complete(self, at):
        """The run finished with the delta `at` while in the last split: it is timed."""
        if self.bounds and self.index == len(self.bounds) - 1:
            if at is not None and self.entry is not None:
                self.prev = (self.index, at - self.entry)
            self.index, self.entry = None, None

    def view(self, delta):
        if not self.bounds:
            return None
        i = self.index
        return {'index': i, 'n': len(self.bounds), 'name': self.bounds[i][0] if i is not None else None,
                'delta': delta - self.entry if i is not None and delta is not None and self.entry is not None
                else None,
                'prev': {'index': self.prev[0], 'name': self.bounds[self.prev[0]][0], 'delta': self.prev[1]}
                if self.prev is not None else None}


class LiveBuffer:
    """See the module's docstring for the threads. `seq` counts rows from 1
    for the life of the process; read(since) gives the rows after `since`."""

    def __init__(self, capacity=CAPACITY, clock=time.monotonic):
        self.capacity = int(capacity)
        self.clock = clock
        self._slots = [None] * self.capacity
        self._seq = 0
        self._offer = None              # (run number, Reference or None, stage dict or None, run id): drive-log thread
        self._run = None                # listener-thread state of the run, below
        self._error_at = None
        self.state = self._publish_idle()

    # -- the drive-log thread --

    @_guarded
    def offer_reference(self, number, reference, stage=None, run_id=None):
        """The reference (or None: there is none) of run `number`, with its
        stage ({key, name, length}) and runs.id as the database has them."""
        self._offer = (number, reference, stage, run_id)

    # -- the listener thread --

    @_guarded
    def start_run(self, number, now, game=None, stage=None, track=None, stage_length=None, start_d=None):
        """Run `number` (RunTracker's) has begun: the rows before it are no
        longer shown, and its delta waits for its reference. `start_d`: where
        along the game's road the run began (ACR's spline)."""
        self._run = {'n': number, 'id': None, 'game': game, 'first': self._seq + 1, 'at': now,
                     'stage': {'key': stage, 'name': track, 'length': stage_length} if (stage or track) else None,
                     'ref': None, 'ref_status': 'pending', 'adopted': False, 'last': None, 'delta': None, 'd0': None,
                     'mid': False, 'spline': start_d, 'off': 0.0,
                     'splits': _Splits(()), 'sectors': _Splits(()), 'final': None}
        self._adopt()
        self._publish(now)

    @_guarded
    def push(self, number, now, row):
        """One trace row of run `number` (drive_log: TRACE_CHANNELS, NaN for
        unknown), the 10 Hz row RunTracker keeps for the database."""
        run = self._run
        if run is None or run['n'] != number or run['final'] is not None:
            return
        self._adopt()
        t, d = row[_T], row[_D]
        if run['d0'] is None and _fin(d):
            run['d0'] = d
            self._check_mid(run)
        ref = run['ref']
        delta = None
        if ref is not None and not run['mid'] and _fin(t) and _fin(d):
            t_ref = ref.t_at(d + run['off'])
            if t_ref is not None:
                delta = t - t_ref
            self._feed(run, ref, t, d)
        if _fin(t) and _fin(d):
            run['last'] = (t, d)
        run['delta'] = delta
        self._seq += 1
        values = list(row[i] for i in _PICK)
        # The map's axes, not the game's (x, z across the plan, y the height): telemetry_formats.plan_xyz
        values[_X:_X + 3] = plan_xyz(run['game'], values[_X:_X + 3])
        values = tuple(values) + (delta,)
        self._slots[self._seq % self.capacity] = (self._seq, values)
        self._publish(now)

    def _feed(self, run, ref, t, d):
        """Move the split trackers to (t, d), the run's time and distance, from the run's last row."""
        last = run['last']

        def delta_at(x):
            # The run's own time at x m (between the last row and this
            # one), less the reference's there
            t_ref_x = ref.t_at(x + run['off'])           # the same road point on the reference
            if t_ref_x is None:
                return None
            if last is not None and last[1] < x < d:
                return last[0] + (t - last[0]) * (x - last[1]) / (d - last[1]) - t_ref_x
            return t - t_ref_x
        run['splits'].feed(d, delta_at)
        run['sectors'].feed(d, delta_at)

    def _check_mid(self, run):
        """A run whose first row is far past the reference's start began mid-stage (for ACR RunTracker says so)."""
        ref = run['ref']
        if ref is not None and ref.track and run['d0'] is not None and run['d0'] > ref.track[0] + MID_STAGE_SLACK:
            run['mid'] = True
            run['ref_status'] = 'mid_stage'

    @_guarded
    def mark_mid_stage(self, number):
        """RunTracker knows run `number` did not begin at the start line: no delta against the reference."""
        run = self._run
        if run is None or run['n'] != number:
            return
        run['mid'] = True
        if run['ref'] is not None:
            run['ref_status'] = 'mid_stage'
            run['delta'] = None

    @_guarded
    def finish(self, number, now, result_time, distance=None):
        """Run `number` crossed the finish with `result_time` s on its own
        clock (None where the game gives none)."""
        run = self._run
        if run is None or run['n'] != number or run['final'] is not None:
            return
        self._adopt()
        ref = run['ref'] if not run['mid'] else None
        if ref is not None and _fin(result_time) and _fin(distance) and (run['last'] is None or distance > run['last'][1]):
            self._feed(run, ref, result_time, distance)         # the last split is completed on the finish's clock
        delta = result_time - ref.result_time if ref is not None and result_time is not None \
            and ref.result_time is not None else None
        if ref is not None:
            run['splits'].complete(delta)
            run['sectors'].complete(delta)
        run['final'] = {'time': result_time, 'delta': delta}
        run['delta'] = delta if delta is not None else run['delta']
        self._publish(now)

    @_guarded
    def end_run(self, number, now, reason):
        """Run `number` is over. A finished run stays shown ('finished')
        until the next starts; any other end (a restart, a teleport, the
        session's end) goes back to 'idle'."""
        run = self._run
        if run is None or run['n'] != number:
            return
        if run['final'] is None:
            self._run = None
            self.state = self._publish_idle()
        else:
            self._adopt()
            self._publish(now)

    def _adopt(self):
        """Take the reference the drive-log thread offered for this run
        (once; `_offer` is only ever written by that thread)."""
        offer, run = self._offer, self._run
        if offer is None or run is None or offer[0] != run['n'] or run['adopted']:
            return
        run['adopted'] = True
        _, ref, stage, run_id = offer
        run['ref'] = ref
        run['ref_status'] = ('mid_stage' if run['mid'] else 'ready') if ref is not None else 'none'
        if stage is not None:
            run['stage'] = stage
        if run_id is not None:
            run['id'] = run_id
        if ref is not None:
            self._check_mid(run)
            # Both runs' distance 0 is where they began: the same road point is `off` m on in the reference's
            run['off'] = shift = run['spline'] - ref.origin if _fin(run['spline']) and ref.origin is not None else 0.0
            run['splits'] = _Splits(tuple((n, a - shift, b - shift) for n, a, b in ref.splits))
            run['sectors'] = _Splits(tuple((n, a - shift, b - shift) for n, a, b in ref.sectors))
            if not run['mid']:
                self._replay(run, ref)

    def _replay(self, run, ref):
        """The reference came late: the run's rows still in the ring go through the split trackers, so the
        split the run is in has its true entry, not the delta at the moment of arrival."""
        last = None
        for seq in range(max(run['first'], self._seq - self.capacity + 1), self._seq + 1):
            slot = self._slots[seq % self.capacity]
            if slot is None or slot[0] != seq:
                continue
            t, d = slot[1][0], slot[1][1]
            if _fin(t) and _fin(d):
                saved, run['last'] = run['last'], last
                self._feed(run, ref, t, d)
                run['last'] = saved
                last = (t, d)

    def _publish_idle(self):
        return {'seq': self._seq, 'first': self._seq + 1, 'run': None, 'game': None, 'stage': None, 'ref': None,
                'ref_status': None, 'delta': None, 'split': None, 'sector': None, 'predicted': None, 'final': None,
                't': None, 'distance': None, 'at': None}

    def _publish(self, now):
        run = self._run
        ref, delta = run['ref'], run['delta']
        last = run['last']
        final = run['final']
        mid = run['mid'] and run['ref'] is not None
        delta = None if mid else delta
        self.state = {
            'seq': self._seq, 'first': run['first'], 'run': {'n': run['n'], 'id': run['id']}, 'game': run['game'],
            'stage': run['stage'], 'ref': ref.view if ref is not None else None,
            'ref_status': run['ref_status'], 'delta': delta,
            'split': None if mid else run['splits'].view(delta), 'sector': None if mid else run['sectors'].view(delta),
            'predicted': ref.result_time + delta if ref is not None and delta is not None
            and ref.result_time is not None else None,
            'final': dict(final) if final is not None else None,
            't': last[0] if last else None, 'distance': last[1] if last else None, 'at': now}

    # -- any thread --

    @property
    def seq(self):
        return self._seq

    def read(self, since=0, now=None, limit=CAPACITY):
        """The live run for a reader, JSON-ready: `seq` (the last row's),
        `state` ('live', 'stale', 'finished', 'idle'), `run`, `stage`, the
        `samples` after `since` (at most `limit`, the newest; only the
        current run's) with their `channels`, `delta`, `split`, `sector`,
        `predicted`, `ref`, `final`. No lock, no database."""
        state = self.state                          # one snapshot: everything below agrees with it
        now = self.clock() if now is None else now
        seq = state['seq']
        try:
            since = int(since or 0)
        except (TypeError, ValueError):
            since = 0
        reset = since > seq                         # a seq from before this process: start over
        if reset or since < 0:
            since = 0
        samples = []
        if state['run'] is not None:
            limit = self.capacity if limit is None else max(0, int(limit))
            lo = max(since + 1, state['first'], seq - self.capacity + 1, seq - limit + 1)
            slots = self._slots
            for s in range(lo, seq + 1):
                slot = slots[s % self.capacity]
                if slot is not None and slot[0] == s:
                    samples.append([round(v, ROUND) if isinstance(v, float) and math.isfinite(v)
                                    else (None if isinstance(v, float) else v) for v in slot[1]])
        if state['run'] is None:
            phase = 'idle'
        elif state['final'] is not None:
            phase = 'finished'
        elif state['at'] is not None and now - state['at'] > STALE:
            phase = 'stale'
        else:
            phase = 'live'
        return {'epoch': EPOCH, 'seq': seq, 'first': state['first'], 'reset': reset, 'state': phase, 'run': state['run'],
                'game': state['game'], 'stage': state['stage'], 't': state['t'], 'distance': state['distance'],
                'delta': state['delta'], 'split': state['split'], 'sector': state['sector'],
                'predicted': state['predicted'], 'final': state['final'], 'ref': state['ref'],
                'ref_status': state['ref_status'], 'age': None if state['at'] is None else max(0.0, now - state['at']),
                'channels': list(CHANNELS), 'samples': samples}
