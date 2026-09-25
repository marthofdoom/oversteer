"""The telemetry database: cars, tunes, sessions, runs and what was learnt
from them (docs/telemetry-coaching.md, section 7).

One file, ~/.local/share/oversteer/telemetry.db, in WAL mode so the
drive-log thread can write while the GUI and the web page read. Only the
drive-log thread writes (Store); everyone else reads through their own
read-only connection (Reader).

Schema versions, in PRAGMA user_version: 0 with a `cars` table is the
first schema (v1), 1 is v1 with DiRT's rpm units fixed, 2 is this one.
Opening a Store migrates, after copying the file aside once.
"""

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import zlib
from array import array

VERSION = 2

SCHEMA = """
CREATE TABLE cars (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    game TEXT NOT NULL,
    key TEXT NOT NULL,              -- '<game>/<id>'
    name TEXT,
    user_named INTEGER DEFAULT 0,
    car_class TEXT, drivetrain TEXT,
    model TEXT NOT NULL,            -- CarModel.to_dict() as JSON
    updated REAL,
    UNIQUE (profile, key)
);
CREATE TABLE tunes (                -- a setup epoch: one gearing (and what else is measurable)
    id INTEGER PRIMARY KEY,
    car INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    ratios TEXT NOT NULL,           -- JSON {gear: rpm per m/s}
    change TEXT,                    -- 'first', 'final-drive', 'gears:3,4', 'user'
    tyre_radius REAL, ride_height_f REAL, ride_height_r REAL, brake_bias REAL,
    hub_rest TEXT,                  -- JSON per-wheel resting hub position (EA WRC)
    note TEXT
);
CREATE TABLE stages (
    key TEXT PRIMARY KEY,
    game TEXT NOT NULL,
    name TEXT, location TEXT,
    length REAL,
    surface_prior TEXT, surface_prior_source TEXT,       -- 'table', 'user', 'learnt'
    discipline_prior TEXT, discipline_prior_source TEXT,
    runs INTEGER DEFAULT 0
);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    car INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    tune INTEGER REFERENCES tunes(id) ON DELETE SET NULL,
    game TEXT,
    started REAL NOT NULL, ended REAL,
    track TEXT,                     -- as the game names it
    stage TEXT REFERENCES stages(key),
    game_mode TEXT,
    discipline TEXT, discipline_conf TEXT,
    surface TEXT, surface_conf TEXT, wet TEXT,
    shifter TEXT,                   -- 'h-pattern', 'sequential', 'paddles', 'auto', 'mixed', NULL
    distance REAL DEFAULT 0, moving_time REAL DEFAULT 0,
    limiter_time REAL DEFAULT 0
);
CREATE TABLE runs (                 -- one stage attempt, one lap session, one stretch of free driving
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    n INTEGER NOT NULL,
    started REAL NOT NULL, ended REAL,
    stage TEXT REFERENCES stages(key),
    start_pos TEXT,                 -- JSON [x, y, z]
    distance REAL, duration REAL, moving_time REAL,
    finished INTEGER,               -- NULL unknown, 0 restarted/abandoned, 1 reached the end
    result_time REAL,
    discipline TEXT, discipline_conf TEXT, discipline_evidence TEXT,   -- evidence: JSON list of sentences
    surface TEXT, surface_conf TEXT, surface_evidence TEXT,
    wet TEXT, wet_evidence TEXT,
    detector_version INTEGER        -- which calibration produced the verdicts
);
CREATE TABLE traces (               -- kept apart so list queries on runs never page through blobs
    run INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    data BLOB NOT NULL              -- zlib(array('f')) at 10 Hz, TRACE_CHANNELS per row
);
CREATE TABLE laps (
    id INTEGER PRIMARY KEY,
    run INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    n INTEGER NOT NULL, time REAL, distance REAL, valid INTEGER
);
CREATE TABLE segments (             -- ~200 m of a run
    id INTEGER PRIMARY KEY,
    run INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    d0 REAL NOT NULL, d1 REAL NOT NULL, t0 REAL, t1 REAL,
    features TEXT NOT NULL,         -- JSON
    pushed INTEGER,                 -- the grip limit was reached (votes on surface)
    surface TEXT, margin REAL
);
CREATE TABLE corners (
    id INTEGER PRIMARY KEY,
    run INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    d REAL NOT NULL,                -- distance at the slowest point
    direction INTEGER,              -- 1 left, -1 right
    entry_speed REAL, min_speed REAL, exit_speed REAL,
    gear_min INTEGER, heading_change REAL, duration REAL,
    counter_steer REAL,             -- fraction of the corner steering against the yaw
    handbrake INTEGER, exit_spin REAL
);
CREATE TABLE shifts (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    at REAL NOT NULL,
    gear INTEGER NOT NULL,          -- changed from
    gear_to INTEGER,
    direction TEXT DEFAULT 'up',
    rpm REAL NOT NULL,              -- peak over the last 0.3 s in the old gear
    best REAL, best_low REAL, best_high REAL,
    throttle REAL,                  -- max over the same window
    method TEXT,                    -- 'h-pattern', 'sequential', 'paddles', 'auto', NULL
    neutral_time REAL,              -- s between leaving and engaging
    engage_rpm REAL,
    flat_out INTEGER,               -- counts for shift-point coaching
    slip REAL,                      -- driven-wheel slip at the change, when known
    flags TEXT                      -- 'missed', 'skip', 'over-rev', 'double-tap'
);
CREATE TABLE metrics (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value REAL NOT NULL, count INTEGER NOT NULL,
    gear INTEGER, method TEXT,
    discipline TEXT, surface TEXT
);
CREATE TABLE labels (               -- what the user says a run was: calibration ground truth
    run INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    discipline TEXT, surface TEXT, wet TEXT, shifter TEXT, note TEXT,
    set_at REAL NOT NULL
);
CREATE TABLE captures (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    started REAL, ended REAL, bytes INTEGER, packets INTEGER, games TEXT,
    session INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    keep INTEGER DEFAULT 0
);
CREATE TABLE calibration (
    game TEXT NOT NULL, kind TEXT NOT NULL,   -- 'surface', 'discipline'
    version INTEGER NOT NULL,
    model TEXT NOT NULL,            -- JSON: classes, means, variances, thresholds
    trained REAL, runs INTEGER, segments INTEGER,
    holdout REAL,                   -- leave-one-run-out accuracy
    deployed INTEGER NOT NULL,
    PRIMARY KEY (game, kind)
);
CREATE TABLE coach_state (
    profile TEXT NOT NULL,
    car INTEGER NOT NULL DEFAULT 0, -- 0 = about the driver, not one car
    tip TEXT NOT NULL,
    first_shown REAL, last_shown REAL, times INTEGER DEFAULT 0,
    value REAL,
    quiet INTEGER DEFAULT 0,
    PRIMARY KEY (profile, car, tip)
);
CREATE INDEX sessions_car ON sessions (profile, car, started);
CREATE INDEX runs_session ON runs (session);
CREATE INDEX runs_stage ON runs (stage, started);
CREATE INDEX shifts_session ON shifts (session);
CREATE INDEX metrics_name ON metrics (name, discipline, surface, session);
CREATE INDEX metrics_session ON metrics (session, name);
CREATE INDEX segments_run ON segments (run);
CREATE INDEX corners_run ON corners (run);
"""

# The first schema, as Oversteer 0.13 created it: kept to migrate from (and
# for the tests that build such a file)
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS cars (
    profile TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT,
    model TEXT NOT NULL,
    updated REAL,
    PRIMARY KEY (profile, key)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    car TEXT NOT NULL,
    track TEXT,
    discipline TEXT,
    surface TEXT,
    started REAL NOT NULL,
    ended REAL,
    limiter_time REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    gear INTEGER NOT NULL,
    rpm REAL NOT NULL,
    best REAL,
    throttle REAL,
    method TEXT
);
CREATE INDEX IF NOT EXISTS sessions_car ON sessions (profile, car, started);
CREATE INDEX IF NOT EXISTS shifts_session ON shifts (session);
"""

# The 10 Hz trace of a run: one row of these per sample, NaN where the game
# does not send it. Position is in it (not in the design's first list)
# because topology and elevation re-run from it.
TRACE_CHANNELS = ('t', 'distance', 'speed', 'rpm', 'gear', 'throttle', 'brake', 'clutch', 'handbrake', 'steer',
                  'a_long', 'a_lat', 'yaw_rate', 'slip_drive', 'susp_rms', 'x', 'y', 'z')
TRACE_VERSION = 1
TRACES_CAP = 200 * 1024 * 1024       # bytes of traces kept; the oldest go first (their runs stay)

STAGE_LENGTH_TOLERANCE = {'dirt': 2.0, 'wrcg': 2.0, 'acr': 10.0}     # m, a measured length on a rounding boundary
STAGE_START_TOLERANCE = 15.0                                         # m of start z (DiRT)


def _split(script):
    return [s.strip() for s in script.split(';') if s.strip()]


# -- migrations --

CODEMASTERS_KEY = re.compile(r'^codemasters-(\d+)-(\d+)-(\d+)$')
CODEMASTERS_NAME = re.compile(r'^\d+ rpm, \d+ gears$')      # the decoder's own name, not the user's


def _near_multiple(x, step, tolerance):
    return abs(x - round(x / step) * step) <= tolerance


def rescale_model(model, factor):
    """A model dict (CarModel.to_dict()) with every rpm it holds times
    `factor`; power bands are re-binned by their middle."""
    model = dict(model)
    for field in ('limiter', 'top_seen'):
        if model.get(field):
            model[field] = float(model[field]) * factor
    for field in ('ratios', 'upshifts'):
        model[field] = {g: [float(x) * factor for x in v] for g, v in (model.get(field) or {}).items()}
    keep = 40

    def rebin(bands):
        out = {}
        for band in sorted(bands, key=int):
            out.setdefault(str(int((int(band) + 0.5) * factor)), []).extend(bands[band])
        return {b: v[-keep:] for b, v in out.items()}
    model['power'] = rebin(model.get('power') or {})
    if model.get('power_g'):
        # "gear:band" keys
        by_gear = {}
        for key, values in model['power_g'].items():
            gear, band = key.split(':')
            by_gear.setdefault(gear, {})[band] = values
        model['power_g'] = {'{}:{}'.format(g, b): v for g, bands in by_gear.items() for b, v in rebin(bands).items()}
    return model


def rescale_codemasters(db, path=None):
    """One-off, on the first schema: DiRT Rally sends engine rates in
    rad/s, which Oversteer read as rpm / 10 until the decoder was fixed,
    so every rpm it learnt about those cars is 30/pi too high. The unit is
    decided from the game's max in the key (floats[63] x 10, rounded), not
    the model's limiter, which is often a launch-learnt figure bouncing on
    the limiter and never round. A car whose max is not a round rpm in
    rad/s (a WRC Generations car read right) is left as it was. The file
    is copied to `<path>.v0.bak` first when anything changes. Sets
    user_version 1."""
    from . import telemetry_formats
    rows = db.execute("SELECT profile, key, name, model FROM cars WHERE key LIKE 'codemasters-%'").fetchall()
    todo = []
    for profile, key, name, model in rows:
        match = CODEMASTERS_KEY.match(key)
        if match is None:
            continue
        key_max, key_idle, gears = (int(x) for x in match.groups())
        if telemetry_formats.codemasters_unit(key_max / 10.0) != telemetry_formats.RAD_S:
            logging.info("telemetry store: %s kept as it is (its max is a round rpm as read)", key)
            continue
        todo.append((profile, key, name, model, key_max, key_idle, gears))
    if todo and path is not None and not os.path.exists(path + '.v0.bak'):
        try:
            shutil.copy2(path, path + '.v0.bak')
        except OSError as e:
            logging.warning("telemetry store: no backup before the DiRT rpm fix: %s", e)
    factor = 3.0 / math.pi               # stored = raw x 10; true = raw x 30/pi
    db.execute('BEGIN')
    try:
        for profile, key, name, model, key_max, key_idle, gears in todo:
            top = round(key_max * factor, -1)
            new_key = 'codemasters-{:.0f}-{:.0f}-{:.0f}'.format(top, round(key_idle * factor, -1), gears)
            if db.execute('SELECT 1 FROM cars WHERE profile = ? AND key = ?', (profile, new_key)).fetchone():
                logging.warning("telemetry store: %s not rescaled, %s exists already", key, new_key)
                continue
            try:
                data = rescale_model(json.loads(model), factor)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                logging.warning("telemetry store: can't read %s: %s", key, e)
                continue
            data['key'] = new_key
            if name is None or CODEMASTERS_NAME.match(name):
                name = '{:.0f} rpm, {} gears'.format(top, gears)
            data['name'] = name
            db.execute('UPDATE cars SET key = ?, name = ?, model = ? WHERE profile = ? AND key = ?',
                       (new_key, name, json.dumps(data), profile, key))
            db.execute('UPDATE shifts SET rpm = rpm * ?, best = best * ? WHERE session IN '
                       '(SELECT id FROM sessions WHERE profile = ? AND car = ?)', (factor, factor, profile, key))
            db.execute('UPDATE sessions SET car = ? WHERE profile = ? AND car = ?', (new_key, profile, key))
            logging.info("telemetry store: %s is %s (DiRT rpm were read x 10 instead of rad/s)", key, new_key)
        db.execute('PRAGMA user_version = 1')
        db.execute('COMMIT')
    except sqlite3.Error:
        db.execute('ROLLBACK')
        raise


def v1_key(key):
    """(game, key) in the second schema for a car key of the first. Games
    the old key cannot tell apart ('forza': Horizon or Motorsport,
    'codemasters': DiRT or WRC Generations) are adopted by the right game
    the first time it sends the car (legacy_keys())."""
    for prefix, game in (('forza-', 'forza'), ('codemasters-', 'codemasters'), ('eawrc-', 'eawrc'),
                         ('acpmf-', 'acpmf')):
        if key.startswith(prefix):
            return game, '{}/{}'.format(game, key[len(prefix):])
    if key == 'acpmf':
        return 'acpmf', 'acpmf/unknown'
    if key == 'outgauge-beam':
        return 'beamng', 'beamng/unknown'
    if key.startswith('outgauge-'):
        return 'lfs', 'lfs/' + key[len('outgauge-'):]
    return 'unknown', key


LEGACY_GAMES = {'forza-fh': 'forza', 'forza-fm': 'forza', 'dirt': 'codemasters', 'wrcg': 'codemasters'}


def legacy_keys(key):
    """Keys the same car may have been stored under before its game could
    be told apart."""
    game, _, rest = key.partition('/')
    old = LEGACY_GAMES.get(game)
    return ['{}/{}'.format(old, rest)] if old and rest else []


def _migrate_v1(db, path):
    """The first schema (user_version 1) to this one, in one transaction,
    after copying the file to `<path>.v1.bak` once."""
    if path is not None and not os.path.exists(path + '.v1.bak'):
        try:
            shutil.copy2(path, path + '.v1.bak')
        except OSError as e:
            logging.warning("telemetry store: no backup before the upgrade: %s", e)
    # Tables are renamed and rebuilt: references must not be checked or
    # rewritten half way (the pragma only works outside a transaction)
    db.execute('PRAGMA foreign_keys = OFF')
    db.execute('BEGIN')
    try:
        for table in ('cars', 'sessions', 'shifts'):
            db.execute('ALTER TABLE {0} RENAME TO {0}_v1'.format(table))
        for index in ('sessions_car', 'shifts_session'):
            db.execute('DROP INDEX IF EXISTS ' + index)
        for statement in _split(SCHEMA):
            db.execute(statement)
        ids, seen = {}, set()
        for profile, key, name, model, updated in db.execute(
                'SELECT profile, key, name, model, updated FROM cars_v1').fetchall():
            game, new_key = v1_key(key)
            try:
                data = json.loads(model)
                data['key'] = new_key
                model = json.dumps(data)
            except (ValueError, TypeError):
                pass
            if (profile, new_key) in seen:
                continue                              # 'acpmf' and 'acpmf-unknown': keep the first
            seen.add((profile, new_key))
            user_named = int(bool(name) and name != key and not CODEMASTERS_NAME.match(name)
                             and not name.startswith('Forza car ') and not name.startswith('EA WRC car '))
            ids[(profile, key)] = db.execute(
                'INSERT INTO cars (profile, game, key, name, user_named, model, updated) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (profile, game, new_key, name, user_named, model, updated)).lastrowid
        sessions = {}
        for sid, profile, car, track, started, ended, limiter_time in db.execute(
                'SELECT id, profile, car, track, started, ended, limiter_time FROM sessions_v1').fetchall():
            car_id = ids.get((profile, car))
            if car_id is None:
                continue                              # its car is gone: nothing to read it against
            sessions[sid] = car_id
            db.execute('INSERT INTO sessions (id, profile, car, game, started, ended, track, limiter_time) '
                       'VALUES (?, ?, ?, (SELECT game FROM cars WHERE id = ?), ?, ?, ?, ?)',
                       (sid, profile, car_id, car_id, started, ended, track, limiter_time or 0.0))
        for row in db.execute('SELECT id, session, at, gear, rpm, best, throttle, method FROM shifts_v1').fetchall():
            if row[1] not in sessions:
                continue
            throttle = row[6]
            db.execute('INSERT INTO shifts (id, session, at, gear, gear_to, direction, rpm, best, throttle, method, '
                       'flat_out) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       row[:4] + (row[3] + 1, 'up') + row[4:] + (int(throttle is None or throttle >= 0.8),))
        for table in ('shifts', 'sessions', 'cars'):
            db.execute('DROP TABLE {}_v1'.format(table))
        db.execute('PRAGMA user_version = {}'.format(VERSION))
        db.execute('COMMIT')
    except sqlite3.Error:
        db.execute('ROLLBACK')
        raise
    finally:
        db.execute('PRAGMA foreign_keys = ON')
    logging.info("telemetry store: upgraded to version %d (%d cars, %d sessions)", VERSION, len(ids), len(sessions))


def _connect(path):
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10.0)
    db.execute('PRAGMA journal_mode = WAL')
    db.execute('PRAGMA synchronous = NORMAL')
    db.execute('PRAGMA foreign_keys = ON')
    return db


def open_store(path):
    """The writer (a Store), after bringing the file to this schema."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    db = _connect(path)
    version = db.execute('PRAGMA user_version').fetchone()[0]
    has_cars = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cars'").fetchone()
    if not has_cars:
        db.execute('BEGIN')
        for statement in _split(SCHEMA):
            db.execute(statement)
        db.execute('PRAGMA user_version = {}'.format(VERSION))
        db.execute('COMMIT')
    else:
        if version == 0:
            rescale_codemasters(db, path)
            version = 1
        if version == 1:
            _migrate_v1(db, path)
        elif version > VERSION:
            db.close()
            raise sqlite3.DatabaseError("{} is from a newer Oversteer (version {})".format(path, version))
    return Store(db, path)


def open_reader(path):
    """A read-only connection, for any thread that only reads (WAL lets it
    run alongside the writer)."""
    db = sqlite3.connect('file:{}?mode=ro'.format(path), uri=True, check_same_thread=False, timeout=10.0)
    return Reader(db)


# -- traces --

def pack_trace(rows):
    """zlib(array('f')) of the rows (tuples of TRACE_CHANNELS)."""
    data = array('f')
    for row in rows:
        data.extend(float('nan') if v is None else v for v in row)
    return zlib.compress(data.tobytes(), 6)


def unpack_trace(blob):
    """The rows back, as tuples of floats (NaN where not sent)."""
    data = array('f')
    data.frombytes(zlib.decompress(blob))
    n = len(TRACE_CHANNELS)
    return [tuple(data[i:i + n]) for i in range(0, len(data) - n + 1, n)]


def _json(value):
    return None if value is None else json.dumps(value)


class Reader:
    """Queries for the GUI and the web page, on a read-only connection."""

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()

    def close(self):
        self.db.close()

    def _rows(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def cars(self, profile):
        """[(key, name)] of the profile's cars, by name."""
        return [(k, n or k) for k, n in self._rows(
            'SELECT key, name FROM cars WHERE profile = ? ORDER BY name', (profile,))]

    def car(self, profile, key):
        """The car's row as a dict (with its model parsed), or None. A key
        the car had before its game could be told apart is found too."""
        for candidate in [key] + legacy_keys(key):
            rows = self._rows('SELECT id, game, key, name, user_named, car_class, drivetrain, model, updated '
                              'FROM cars WHERE profile = ? AND key = ?', (profile, candidate))
            if rows:
                row = rows[0]
                try:
                    model = json.loads(row[7])
                except ValueError:
                    model = {}
                return {'id': row[0], 'game': row[1], 'key': row[2], 'name': row[3], 'user_named': bool(row[4]),
                        'car_class': row[5], 'drivetrain': row[6], 'model': model, 'updated': row[8]}
        return None

    def updated(self, profile, key):
        rows = self._rows('SELECT updated FROM cars WHERE profile = ? AND key = ?', (profile, key))
        return rows[0][0] if rows else None

    def history(self, profile, key, limit=10):
        """The car's recent sessions, newest first."""
        rows = self._rows(
            'SELECT s.id, s.started, s.track, s.limiter_time, COUNT(h.id), AVG(h.rpm - h.best), '
            '       GROUP_CONCAT(DISTINCT h.method), s.shifter, s.stage, s.discipline, s.surface, s.distance '
            'FROM sessions s JOIN cars c ON s.car = c.id '
            "LEFT JOIN shifts h ON h.session = s.id AND h.direction = 'up' AND h.flat_out = 1 "
            'WHERE c.profile = ? AND c.key = ? GROUP BY s.id ORDER BY s.started DESC LIMIT ?',
            (profile, key, limit))
        return [{'id': r[0], 'started': r[1], 'track': r[2], 'limiter_time': r[3] or 0.0, 'shifts': r[4],
                 'error': r[5], 'methods': sorted((r[6] or '').split(',')) if r[6] else [], 'shifter': r[7],
                 'stage': r[8], 'discipline': r[9], 'surface': r[10], 'distance': r[11] or 0.0} for r in rows]

    def method_shifts(self, profile, key, keep=40):
        """{gear: {method: (mean rpm, count)}} of the car's most recent
        flat-out changes up, per way of changing (the last `keep` each)."""
        rows = self._rows(
            'SELECT h.gear, h.method, h.rpm FROM shifts h JOIN sessions s ON h.session = s.id '
            'JOIN cars c ON s.car = c.id '
            "WHERE c.profile = ? AND c.key = ? AND h.method IS NOT NULL AND h.direction = 'up' "
            'AND h.flat_out = 1 ORDER BY h.at DESC LIMIT 5000', (profile, key))
        recent = {}
        for gear, method, rpm in rows:
            values = recent.setdefault(gear, {}).setdefault(method, [])
            if len(values) < keep:
                values.append(rpm)
        return {gear: {m: (sum(v) / len(v), len(v)) for m, v in methods.items()} for gear, methods in recent.items()}

    def sessions(self, car_id, limit=20):
        rows = self._rows('SELECT id, started, ended, stage, track, discipline, discipline_conf, surface, '
                          'surface_conf, wet, shifter, distance, moving_time, limiter_time, tune '
                          'FROM sessions WHERE car = ? ORDER BY started DESC LIMIT ?', (car_id, limit))
        names = ('id', 'started', 'ended', 'stage', 'track', 'discipline', 'discipline_conf', 'surface',
                 'surface_conf', 'wet', 'shifter', 'distance', 'moving_time', 'limiter_time', 'tune')
        return [dict(zip(names, r)) for r in rows]

    def session(self, session_id):
        """A session with its runs, their verdicts and evidence."""
        rows = self._rows('SELECT id, profile, car, tune, game, started, ended, track, stage, discipline, '
                          'discipline_conf, surface, surface_conf, wet, shifter, distance, moving_time, '
                          'limiter_time FROM sessions WHERE id = ?', (session_id,))
        if not rows:
            return None
        names = ('id', 'profile', 'car', 'tune', 'game', 'started', 'ended', 'track', 'stage', 'discipline',
                 'discipline_conf', 'surface', 'surface_conf', 'wet', 'shifter', 'distance', 'moving_time',
                 'limiter_time')
        session = dict(zip(names, rows[0]))
        session['runs'] = self.runs(session_id)
        return session

    def runs(self, session_id):
        names = ('id', 'n', 'started', 'ended', 'stage', 'start_pos', 'distance', 'duration', 'moving_time',
                 'finished', 'result_time', 'discipline', 'discipline_conf', 'discipline_evidence', 'surface',
                 'surface_conf', 'surface_evidence', 'wet', 'wet_evidence', 'detector_version')
        runs = []
        for row in self._rows('SELECT {} FROM runs WHERE session = ? ORDER BY n'.format(', '.join(names)),
                              (session_id,)):
            run = dict(zip(names, row))
            for field in ('start_pos', 'discipline_evidence', 'surface_evidence', 'wet_evidence'):
                run[field] = json.loads(run[field]) if run[field] else None
            runs.append(run)
        return runs

    def segments(self, run_id):
        return [{'d0': r[0], 'd1': r[1], 't0': r[2], 't1': r[3], 'features': json.loads(r[4]), 'pushed': r[5],
                 'surface': r[6], 'margin': r[7]}
                for r in self._rows('SELECT d0, d1, t0, t1, features, pushed, surface, margin FROM segments '
                                    'WHERE run = ? ORDER BY d0', (run_id,))]

    def corners(self, run_id):
        names = ('d', 'direction', 'entry_speed', 'min_speed', 'exit_speed', 'gear_min', 'heading_change',
                 'duration', 'counter_steer', 'handbrake', 'exit_spin')
        return [dict(zip(names, r)) for r in self._rows(
            'SELECT {} FROM corners WHERE run = ? ORDER BY d'.format(', '.join(names)), (run_id,))]

    def trace(self, run_id):
        rows = self._rows('SELECT version, data FROM traces WHERE run = ?', (run_id,))
        return unpack_trace(rows[0][1]) if rows and rows[0][0] == TRACE_VERSION else None

    def shifts(self, session_id):
        names = ('at', 'gear', 'gear_to', 'direction', 'rpm', 'best', 'best_low', 'best_high', 'throttle',
                 'method', 'neutral_time', 'engage_rpm', 'flat_out', 'slip', 'flags', 'run')
        return [dict(zip(names, r)) for r in self._rows(
            'SELECT {} FROM shifts WHERE session = ? ORDER BY at, id'.format(', '.join(names)), (session_id,))]

    def stage(self, key):
        rows = self._rows('SELECT key, game, name, location, length, surface_prior, surface_prior_source, '
                          'discipline_prior, discipline_prior_source, runs FROM stages WHERE key = ?', (key,))
        names = ('key', 'game', 'name', 'location', 'length', 'surface_prior', 'surface_prior_source',
                 'discipline_prior', 'discipline_prior_source', 'runs')
        return dict(zip(names, rows[0])) if rows else None

    def tunes(self, car_id):
        names = ('id', 'first_seen', 'last_seen', 'ratios', 'change', 'tyre_radius', 'ride_height_f',
                 'ride_height_r', 'brake_bias', 'note')
        tunes = []
        for row in self._rows('SELECT {} FROM tunes WHERE car = ? ORDER BY first_seen, id'.format(', '.join(names)),
                              (car_id,)):
            tune = dict(zip(names, row))
            tune['ratios'] = {int(g): r for g, r in json.loads(tune['ratios']).items()}
            tunes.append(tune)
        return tunes

    def labels_for(self, game):
        """[(run id, label dict)] of the game's labelled runs."""
        rows = self._rows('SELECT l.run, l.discipline, l.surface, l.wet, l.shifter, l.note FROM labels l '
                          'JOIN runs r ON l.run = r.id JOIN sessions s ON r.session = s.id WHERE s.game = ? '
                          'ORDER BY l.run', (game,))
        return [(r[0], {'discipline': r[1], 'surface': r[2], 'wet': r[3], 'shifter': r[4], 'note': r[5]})
                for r in rows]

    def calibration(self, game, kind):
        rows = self._rows('SELECT version, model, trained, runs, segments, holdout, deployed FROM calibration '
                          'WHERE game = ? AND kind = ?', (game, kind))
        if not rows:
            return None
        r = rows[0]
        return {'version': r[0], 'model': json.loads(r[1]), 'trained': r[2], 'runs': r[3], 'segments': r[4],
                'holdout': r[5], 'deployed': bool(r[6])}

    def stage_history(self, key, limit=20, exclude=None):
        """Discipline and surface verdicts of the stage's recent runs, for
        its priors."""
        return self._rows('SELECT discipline, discipline_conf, surface, surface_conf FROM runs WHERE stage = ? '
                          'AND id IS NOT ? AND ended IS NOT NULL ORDER BY started DESC LIMIT ?', (key, exclude, limit))


class Store(Reader):
    """The writer. Used by the drive-log thread only (one connection,
    transactions batched by the caller with begin() and commit())."""

    def __init__(self, db, path):
        super().__init__(db)
        self.path = path

    def begin(self):
        if not self.db.in_transaction:
            self.db.execute('BEGIN')

    def commit(self):
        if self.db.in_transaction:
            self.db.execute('COMMIT')

    def rollback(self):
        if self.db.in_transaction:
            self.db.execute('ROLLBACK')

    def _do(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # -- cars --

    def car_id(self, profile, key, game, name=None, model=None):
        """The car's id, creating its row if needed. A car stored under a
        key from before its game could be told apart (forza/…,
        codemasters/…) is adopted: renamed to the new key, history and
        all."""
        row = self._do('SELECT id FROM cars WHERE profile = ? AND key = ?', (profile, key)).fetchone()
        if row is not None:
            return row[0]
        for old in legacy_keys(key):
            row = self._do('SELECT id FROM cars WHERE profile = ? AND key = ?', (profile, old)).fetchone()
            if row is not None:
                self._do('UPDATE cars SET key = ?, game = ? WHERE id = ?', (key, game, row[0]))
                logging.info("telemetry store: %s is %s", old, key)
                return row[0]
        return self._do('INSERT INTO cars (profile, game, key, name, model, updated) VALUES (?, ?, ?, ?, ?, ?)',
                        (profile, game or 'unknown', key, name, json.dumps(model or {}), time.time())).lastrowid

    def save_model(self, profile, key, game, name, model, car_class=None, drivetrain=None, updated=None,
                   create=True):
        """Write the car's model (a dict); returns the car's id (None when
        the car has no row and `create` is False)."""
        if not create and self._do('SELECT 1 FROM cars WHERE profile = ? AND key IN ({})'.format(
                ', '.join('?' * (1 + len(legacy_keys(key))))), (profile, key, *legacy_keys(key))).fetchone() is None:
            return None
        car = self.car_id(profile, key, game, name, model)
        self._do('UPDATE cars SET model = ?, name = CASE WHEN user_named THEN name ELSE COALESCE(?, name) END, '
                 'car_class = COALESCE(?, car_class), drivetrain = COALESCE(?, drivetrain), updated = ? '
                 'WHERE id = ?', (json.dumps(model), name, car_class, drivetrain, updated or time.time(), car))
        return car

    def drop_car_if_empty(self, car):
        """A car driven for a moment that taught nothing (no session left,
        no gear learnt) is not worth listing."""
        row = self._do('SELECT model FROM cars WHERE id = ? AND NOT EXISTS (SELECT 1 FROM sessions WHERE car = ?)',
                       (car, car)).fetchone()
        if row is None:
            return False
        try:
            learnt = bool(json.loads(row[0]).get('ratios'))
        except (ValueError, AttributeError):
            learnt = True
        if not learnt:
            self._do('DELETE FROM cars WHERE id = ?', (car,))
        return not learnt

    def rename_car(self, profile, key, name):
        self._do('UPDATE cars SET name = ?, user_named = 1, updated = ? WHERE profile = ? AND key = ?',
                 (name, time.time(), profile, key))

    def forget_model(self, profile, key):
        """Start the car's learning over: its model and tunes go; sessions,
        runs, labels and metrics stay (labels are the only ground truth
        for calibration, and weeks of them should not go with one
        mis-learnt car)."""
        row = self._do('SELECT id, model FROM cars WHERE profile = ? AND key = ?', (profile, key)).fetchone()
        if row is None:
            return
        try:
            name = json.loads(row[1]).get('name')
        except (ValueError, AttributeError):
            name = None
        self._do('UPDATE cars SET model = ?, updated = ? WHERE id = ?',
                 (json.dumps({'key': key, 'name': name}), time.time(), row[0]))
        self._do('DELETE FROM tunes WHERE car = ?', (row[0],))

    # -- tunes --

    def add_tune(self, car, at, ratios, change, tyre_radius=None):
        return self._do('INSERT INTO tunes (car, first_seen, last_seen, ratios, change, tyre_radius) '
                        'VALUES (?, ?, ?, ?, ?, ?)',
                        (car, at, at, json.dumps({str(g): r for g, r in ratios.items()}), change,
                         tyre_radius)).lastrowid

    def touch_tune(self, tune, at, ratios=None, tyre_radius=None):
        """The tune was driven again; `ratios` fills in gears learnt since."""
        if ratios is not None:
            self._do('UPDATE tunes SET last_seen = ?, ratios = ?, tyre_radius = COALESCE(?, tyre_radius) '
                     'WHERE id = ?', (at, json.dumps({str(g): r for g, r in ratios.items()}), tyre_radius, tune))
        else:
            self._do('UPDATE tunes SET last_seen = ? WHERE id = ?', (at, tune))

    def current_tune(self, car):
        row = self._do('SELECT id, ratios FROM tunes WHERE car = ? ORDER BY first_seen DESC, id DESC LIMIT 1',
                       (car,)).fetchone()
        return (row[0], {int(g): r for g, r in json.loads(row[1]).items()}) if row else None

    # -- stages --

    def upsert_stage(self, key, game, length=None, name=None, location=None, surface_prior=None,
                     surface_prior_source=None):
        self._do('INSERT INTO stages (key, game, name, location, length, surface_prior, surface_prior_source) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET '
                 'length = COALESCE(stages.length, excluded.length), name = COALESCE(stages.name, excluded.name), '
                 'location = COALESCE(stages.location, excluded.location), '
                 'surface_prior = COALESCE(stages.surface_prior, excluded.surface_prior), '
                 'surface_prior_source = COALESCE(stages.surface_prior_source, excluded.surface_prior_source)',
                 (key, game, name, location, length, surface_prior, surface_prior_source))

    def match_stage(self, game, length, start_z=None):
        """The key of a stage already stored that a measured (length, start
        z) is, within tolerance, or a new rounded key: a value on a
        rounding boundary must not split one stage in two."""
        tolerance = STAGE_LENGTH_TOLERANCE.get(game, 5.0)
        best = None
        for (key,) in self._do('SELECT key FROM stages WHERE game = ? AND key LIKE ?', (game, game + ':%')):
            parts = key.split(':')
            try:
                known_length = float(parts[1])
                known_z = float(parts[2]) if len(parts) > 2 else None
            except (IndexError, ValueError):
                continue
            if abs(known_length - length) > tolerance:
                continue
            if start_z is not None and known_z is not None and abs(known_z - start_z) > STAGE_START_TOLERANCE:
                continue
            miss = abs(known_length - length)
            if best is None or miss < best[0]:
                best = (miss, key)
        if best is not None:
            return best[1]
        if start_z is None:
            return '{}:{:.0f}'.format(game, round(length, -1))
        return '{}:{:.0f}:{:.0f}'.format(game, round(length), round(start_z, -1))

    def match_cell(self, game, cell, length):
        """A start-cell stage key (cell:<game>:<x>:<z>:<heading>) for a run
        of `length` m from `cell` ((x, z, heading) in 50 m and 45° steps):
        a stored one from a neighbouring cell and heading with a length
        within 5 % (or 100 m), else a new one."""
        x, z, h = cell
        best = None
        for (key,) in self._do('SELECT key FROM stages WHERE key LIKE ?', ('cell:{}:%'.format(game),)):
            try:
                _, _, kx, kz, kh, kl = key.split(':')
                kx, kz, kh, kl = int(kx), int(kz), int(kh), float(kl)
            except ValueError:
                continue
            if abs(kx - x) > 1 or abs(kz - z) > 1 or min((kh - h) % 8, (h - kh) % 8) > 1:
                continue
            miss = abs(kl - length)
            if miss <= max(100.0, 0.05 * length) and (best is None or miss < best[0]):
                best = (miss, key)
        if best is not None:
            return best[1]
        return 'cell:{}:{}:{}:{}:{:.0f}'.format(game, x, z, h, round(length, -2))

    # -- sessions and runs --

    def start_session(self, profile, car, game, started, track=None, stage=None):
        if stage is not None:
            self.upsert_stage(stage, stage.split(':')[0])
        return self._do('INSERT INTO sessions (profile, car, game, started, track, stage) VALUES (?, ?, ?, ?, ?, ?)',
                        (profile, car, game, started, track, stage)).lastrowid

    def update_session(self, session, **fields):
        if fields:
            names = sorted(fields)
            self._do('UPDATE sessions SET {} WHERE id = ?'.format(', '.join(n + ' = ?' for n in names)),
                     tuple(fields[n] for n in names) + (session,))

    def drop_session_if_empty(self, session):
        """A session with no change of gear and no run taught nothing."""
        cursor = self._do('DELETE FROM sessions WHERE id = ? AND NOT EXISTS (SELECT 1 FROM shifts WHERE session = ?) '
                          'AND NOT EXISTS (SELECT 1 FROM runs WHERE session = ?)', (session, session, session))
        return cursor.rowcount > 0

    def session_shifter(self, session, share=0.8):
        """The way of changing gear used for at least `share` of the
        session's changes, 'mixed' when none was, None when none is known."""
        rows = self._do('SELECT method, COUNT(*) FROM shifts WHERE session = ? AND method IS NOT NULL '
                        'GROUP BY method', (session,)).fetchall()
        total = sum(n for _, n in rows)
        if not total:
            return None
        method, count = max(rows, key=lambda r: r[1])
        return method if count >= share * total else 'mixed'

    def start_run(self, session, n, started, stage=None, stage_game=None, stage_length=None, start_pos=None):
        """A new run; its stage row is written first (the run refers to it)."""
        if stage is not None:
            self.upsert_stage(stage, stage_game or stage.split(':')[0], stage_length)
        return self._do('INSERT INTO runs (session, n, started, stage, start_pos) VALUES (?, ?, ?, ?, ?)',
                        (session, n, started, stage, _json(start_pos))).lastrowid

    def end_run(self, run, **fields):
        stage = fields.get('stage')
        if stage is not None:
            self.upsert_stage(stage, fields.pop('stage_game', None) or stage.split(':')[0],
                              fields.pop('stage_length', None))
        fields.pop('stage_game', None)
        fields.pop('stage_length', None)
        for name in ('start_pos', 'discipline_evidence', 'surface_evidence', 'wet_evidence'):
            if name in fields and not isinstance(fields[name], (str, type(None))):
                fields[name] = json.dumps(fields[name])
        names = sorted(fields)
        if names:
            self._do('UPDATE runs SET {} WHERE id = ?'.format(', '.join(n + ' = ?' for n in names)),
                     tuple(fields[n] for n in names) + (run,))
        row = self._do('SELECT stage FROM runs WHERE id = ?', (run,)).fetchone()
        if row is not None and row[0] is not None:
            self._do('UPDATE stages SET runs = runs + 1 WHERE key = ?', (row[0],))

    def set_segment_surface(self, run, d0, surface, margin):
        self._do('UPDATE segments SET surface = ?, margin = ? WHERE run = ? AND d0 = ?', (surface, margin, run, d0))

    def set_stage_prior(self, key, kind, value, source):
        """A stage's prior ('surface' or 'discipline'): shown next to what a
        run measured, never merged into it."""
        column = {'surface': 'surface_prior', 'discipline': 'discipline_prior'}[kind]
        self._do('UPDATE stages SET {0} = ?, {0}_source = ? WHERE key = ?'.format(column), (value, source, key))

    def run_stage(self, run):
        row = self._do('SELECT stage FROM runs WHERE id = ?', (run,)).fetchone()
        return row[0] if row else None

    def summarise_session(self, session):
        """The session's distance and time on the move from its runs, and
        its discipline and surface: what most of its distance was, with
        the confidence of the runs that said so."""
        rows = self._do('SELECT distance, moving_time, discipline, discipline_conf, surface, surface_conf, wet '
                        'FROM runs WHERE session = ?', (session,)).fetchall()
        if not rows:
            return
        fields = {'distance': sum(r[0] or 0.0 for r in rows), 'moving_time': sum(r[1] or 0.0 for r in rows)}
        for name, value, conf in (('discipline', 2, 3), ('surface', 4, 5), ('wet', 6, None)):
            weight = {}
            for r in rows:
                if r[value] is not None and r[value] != 'unknown':
                    weight[r[value]] = weight.get(r[value], 0.0) + (r[0] or 0.0)
            if weight:
                best = max(weight, key=weight.get)
                fields[name] = best
                if conf is not None:
                    confs = [r[conf] for r in rows if r[value] == best and r[conf]]
                    order = ('low', 'medium', 'high', 'game')
                    fields[name + '_conf'] = min(confs, key=order.index) if confs else None
        self.update_session(session, **fields)

    def drop_run(self, run):
        self._do('DELETE FROM runs WHERE id = ?', (run,))

    def add_trace(self, run, rows, cap=TRACES_CAP):
        self._do('INSERT OR REPLACE INTO traces (run, version, data) VALUES (?, ?, ?)',
                 (run, TRACE_VERSION, pack_trace(rows)))
        self.prune_traces(cap)

    def prune_traces(self, cap=TRACES_CAP):
        """Drop the oldest traces beyond `cap` bytes in total; their runs,
        metrics and verdicts stay."""
        total = self._do('SELECT COALESCE(SUM(LENGTH(data)), 0) FROM traces').fetchone()[0]
        if total <= cap:
            return 0
        dropped = 0
        for run, size in self._do('SELECT run, LENGTH(data) FROM traces ORDER BY run').fetchall():
            if total <= cap:
                break
            self._do('DELETE FROM traces WHERE run = ?', (run,))
            total -= size
            dropped += 1
        return dropped

    def add_lap(self, run, n, lap_time, distance, valid=None):
        self._do('INSERT INTO laps (run, n, time, distance, valid) VALUES (?, ?, ?, ?, ?)',
                 (run, n, lap_time, distance, valid))

    def add_segment(self, run, d0, d1, t0, t1, features, pushed=None, surface=None, margin=None):
        return self._do('INSERT INTO segments (run, d0, d1, t0, t1, features, pushed, surface, margin) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (run, d0, d1, t0, t1, json.dumps(features), pushed, surface, margin)).lastrowid

    def add_corners(self, run, corners):
        names = ('d', 'direction', 'entry_speed', 'min_speed', 'exit_speed', 'gear_min', 'heading_change',
                 'duration', 'counter_steer', 'handbrake', 'exit_spin')
        for corner in corners:
            self._do('INSERT INTO corners (run, {}) VALUES (?, {})'.format(', '.join(names), ', '.join('?' * len(names))),
                     (run,) + tuple(corner.get(n) for n in names))

    def add_shift(self, session, run, shift):
        names = ('at', 'gear', 'gear_to', 'direction', 'rpm', 'best', 'best_low', 'best_high', 'throttle', 'method',
                 'neutral_time', 'engage_rpm', 'flat_out', 'slip', 'flags')
        return self._do('INSERT INTO shifts (session, run, {}) VALUES (?, ?, {})'.format(
            ', '.join(names), ', '.join('?' * len(names))), (session, run) + tuple(shift.get(n) for n in names)).lastrowid

    def add_metrics(self, session, run, metrics):
        for m in metrics:
            self._do('INSERT INTO metrics (session, run, name, value, count, gear, method, discipline, surface) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (session, run, m['name'], m['value'], m['count'], m.get('gear'), m.get('method'),
                      m.get('discipline'), m.get('surface')))

    # -- ground truth and calibration --

    def set_label(self, run, discipline=None, surface=None, wet=None, shifter=None, note=None, at=None):
        self._do('INSERT OR REPLACE INTO labels (run, discipline, surface, wet, shifter, note, set_at) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?)', (run, discipline, surface, wet, shifter, note, at or time.time()))

    def add_capture(self, path, started, ended=None, size=None, packets=None, games=None, session=None):
        self._do('INSERT OR REPLACE INTO captures (path, started, ended, bytes, packets, games, session) '
                 'VALUES (?, ?, ?, ?, ?, ?, ?)', (path, started, ended, size, packets, games, session))

    def save_calibration(self, game, kind, model, runs, segments, holdout, deployed, trained=None):
        """Store a calibration; its version is one more than the last."""
        row = self._do('SELECT version FROM calibration WHERE game = ? AND kind = ?', (game, kind)).fetchone()
        version = (row[0] if row else 0) + 1
        self._do('INSERT OR REPLACE INTO calibration (game, kind, version, model, trained, runs, segments, holdout, '
                 'deployed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 (game, kind, version, json.dumps(model), trained or time.time(), runs, segments, holdout,
                  int(bool(deployed))))
        return version

    def coach_seen(self, profile, car, tip, value, at=None):
        at = at or time.time()
        self._do('INSERT INTO coach_state (profile, car, tip, first_shown, last_shown, times, value) '
                 'VALUES (?, ?, ?, ?, ?, 1, ?) ON CONFLICT (profile, car, tip) DO UPDATE SET '
                 'last_shown = excluded.last_shown, times = coach_state.times + 1, value = excluded.value',
                 (profile, car or 0, tip, at, at, value))
