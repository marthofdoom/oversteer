"""Issue hunt 2026-10: replay every capture through the current live path into a scratch store, instrumented.

    python3 research/issue-hunt/replay_hunt.py OUT_DIR [--only SUBSTR]

Reads marth's captures (read-only, oldest first by file name) from ~/.local/share/oversteer/captures and the
Flatpak's captures folder; writes OUT_DIR/hunt.db (a scratch store, never a live one), OUT_DIR/runs.jsonl (one line
per run end: timing of the finish against the game's own clock, the drive-log cost, the coach's tips and splits
read right after it as the GUI would) and OUT_DIR/log.txt (every warning and exception logged).

The tips are read after each run end with Coach(reader, now=the run's wall time) and marked seen (coach.seen), as the
GTK tab does when it is visible, so repeats across runs show as they would on the screen.
"""

import glob
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from oversteer import coach, stage_tables  # noqa: E402
from oversteer.telemetry_capture import read_capture, replay  # noqa: E402
from oversteer.telemetry_formats import decode_sample  # noqa: E402
from oversteer.shift_learner import ShiftLearner  # noqa: E402
from oversteer import drive_log  # noqa: E402

FOLDERS = ('~/.local/share/oversteer/captures', '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')
PROFILES = {'acr': 'AC Rally', 'dirt': 'Dirt Rally', 'wrcg': 'WRCG', 'eawrc': 'EA WRC'}


class Collect(logging.Handler):
    def __init__(self, out):
        super().__init__(logging.INFO)
        self.out = out
        self.capture = None
        self.counts = {}

    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += '\n' + logging.Formatter().formatException(record.exc_info)
        key = (record.levelname, record.getMessage()[:80])
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] <= 3:
            self.out.write('[%s] %s %s: %s\n' % (self.capture, record.levelname, record.name, text))
            self.out.flush()


def game_of(path):
    _meta, records = read_capture(path)
    for i, (_t, _a, data) in enumerate(records):
        s = decode_sample(data)
        if s is not None and s.game:
            return s.game
        if i > 5000:
            break
    return None


class Probe:
    """Per-run raw clock watch, fed from RunTracker.feed: when the game's clock last moved and where."""

    def __init__(self):
        self.reset(None)

    def reset(self, run):
        self.run = run
        self.clock_last = None        # (t, stage_time, lap_distance, speed) at the last packet the clock moved
        self.last_st = None
        self.finish_line = None
        self.line_cross = None        # (t, stage_time, lap_distance) when lap_distance first passed the table's line
        self.max_ld = None
        self.start = None
        self.first = None
        self.stops = []               # every (t, st, ld) where the clock stood >= 0.5 s while moving

    def feed(self, now, sample, tracker):
        if tracker.run != self.run:
            self.reset(tracker.run)
            self.start = (now, sample.stage_time, sample.lap_distance, sample.track)
        if self.run is None:
            return
        st, ld = sample.stage_time, sample.lap_distance
        if st is not None and st != self.last_st:
            if self.clock_last is not None and now - self.clock_last[0] >= 0.5 and (sample.speed or 0) > 1:
                self.stops.append((round(self.clock_last[0], 2), self.clock_last[1], self.clock_last[2],
                                   round(now - self.clock_last[0], 2)))
            self.clock_last = (now, st, ld, sample.speed)
        self.last_st = st
        if ld is not None:
            self.max_ld = ld if self.max_ld is None else max(self.max_ld, ld)
        fl = tracker._finish_line
        if fl and self.line_cross is None and ld is not None and ld >= fl:
            self.line_cross = (now, st, ld)
        if fl:
            self.finish_line = fl


def main(out_dir, only=None):
    os.makedirs(out_dir, exist_ok=True)
    db = os.path.join(out_dir, 'hunt.db')
    for f in (db, db + '-wal', db + '-shm'):
        if os.path.exists(f):
            os.remove(f)
    log_out = open(os.path.join(out_dir, 'log.txt'), 'w')
    handler = Collect(log_out)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    runs_out = open(os.path.join(out_dir, 'runs.jsonl'), 'w')
    files = sorted((p for d in FOLDERS for p in glob.glob(os.path.join(os.path.expanduser(d), '*.ovcap.gz'))),
                   key=os.path.basename)
    for path in files:
        name = os.path.basename(path)
        if only and only not in name:
            continue
        handler.capture = name
        game = game_of(path)
        profile = PROFILES.get(game, '_no_profile')
        meta, records = read_capture(path)
        started = meta.get('started')
        learner = ShiftLearner(db, profile=profile)
        tracker = learner.runs
        probe = Probe()
        state = {'finish_at': None}
        orig_feed, orig_end, orig_finish_at = tracker.feed, tracker.end, tracker._finish_at
        orig_live_finish = learner.live_run.finish
        orig_write_end = tracker._write_end

        def feed(now, sample, throttle, session, profile_, _p=probe):
            orig_feed(now, sample, throttle, session, profile_)
            _p.feed(now, sample, tracker)

        def live_finish(run, now, result, d, *a, **k):
            if state['finish_at'] is None:
                state['finish_at'] = (now, result, d, tracker._result_clock if hasattr(tracker, '_result_clock')
                                      else None)
            return orig_live_finish(run, now, result, d, *a, **k)

        timing = {}

        def write_end(number, ended, summary, trace):
            t0 = time.perf_counter()
            r = orig_write_end(number, ended, summary, trace)
            timing['write_end'] = time.perf_counter() - t0
            return r

        def end(now, reason, _p=probe):
            run_no = tracker.run
            if run_no is None:
                return orig_end(now, reason)
            row_id = tracker.run_rows.get(run_no)
            fin = state['finish_at']
            snap = {
                'capture': name, 'game': game, 'profile': profile, 'run_no': run_no, 'run_id': row_id,
                'reason': reason, 'end_t': round(now - 1000.0, 2),
                'start_t': round(_p.start[0] - 1000.0, 2) if _p.start else None,
                'start_st': _p.start[1] if _p.start else None, 'start_ld': _p.start[2] if _p.start else None,
                'track': _p.start[3] if _p.start else None,
                'finish_detect_t': round(fin[0] - 1000.0, 2) if fin else None,
                'finish_result': fin[1] if fin else None, 'finish_d': fin[2] if fin else None,
                'result_clock': tracker.__dict__.get('_result_clock'),
                'clock_last_t': round(_p.clock_last[0] - 1000.0, 2) if _p.clock_last else None,
                'clock_last_st': _p.clock_last[1] if _p.clock_last else None,
                'clock_last_ld': _p.clock_last[2] if _p.clock_last else None,
                'line': _p.finish_line, 'line_cross_t': round(_p.line_cross[0] - 1000.0, 2) if _p.line_cross else None,
                'line_cross_st': _p.line_cross[1] if _p.line_cross else None, 'max_ld': _p.max_ld,
                'clock_stops': _p.stops[-6:], 'from_line': tracker.__dict__.get('_from_line'),
                'duration': tracker.__dict__.get('_duration'), 'distance': tracker.__dict__.get('_distance'),
                'finished': tracker.__dict__.get('_finished'),
                'post_d': tracker.__dict__.get('_post_d'), 'post_still': tracker.__dict__.get('_post_still'),
            }
            timing.clear()
            r = orig_end(now, reason)
            state['finish_at'] = None
            snap['write_end_s'] = round(timing.get('write_end', 0.0), 3)
            reader = learner._reader()
            row = reader.run(row_id) if row_id is not None else None
            if row is not None:
                snap.update({k: row.get(k) for k in ('stage', 'finished', 'result_time', 'run_class', 'course',
                                                     'duration', 'distance', 'surface', 'discipline', 'car')})
                wall = learner.wall(now)
                t0 = time.perf_counter()
                try:
                    tips = coach.Coach(reader, now=wall).tips(profile, row['car'])
                    snap['tips'] = [{'id': t.id, 'kind': t.kind, 'text': t.text, 'cost': t.cost,
                                     'value': t.value} for t in tips]
                except Exception:
                    logging.exception('hunt: tips after run %s', row_id)
                    tips = []
                snap['tips_s'] = round(time.perf_counter() - t0, 3)
                t0 = time.perf_counter()
                try:
                    sp = coach.splits(reader, profile, row['car'])
                    snap['splits'] = json.loads(json.dumps(sp, default=str)) if sp is not None else None
                except Exception:
                    logging.exception('hunt: splits after run %s', row_id)
                snap['splits_s'] = round(time.perf_counter() - t0, 3)
                if tips:
                    coach.seen(learner.log.store, profile, row['car'], tips, at=wall)
                    learner.log.store.commit()
            else:
                snap['dropped'] = True
            runs_out.write(json.dumps(snap, default=str) + '\n')
            runs_out.flush()
            return r

        tracker.feed, tracker.end, tracker._write_end = feed, end, write_end
        learner.live_run.finish = live_finish
        t0 = time.time()
        replay(records, learner, started=started)
        learner.close()
        print(name, game, '%.0f s' % (time.time() - t0), flush=True)
    log_out.close()
    runs_out.close()


if __name__ == '__main__':
    args = sys.argv[1:]
    only = None
    if '--only' in args:
        i = args.index('--only')
        only = args[i + 1]
        del args[i:i + 2]
    main(os.path.expanduser(args[0]), only)
