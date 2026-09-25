# Telemetry and coaching: design

Branch `launch-limiter`. This document is the design for the telemetry,
learning and coaching system, and the recovery point for the work on it: if
a run is cut off, the checklist at the end says what is done and the notes
next to each item say what was deferred and why. Keep it current with every
commit that touches the system.

Starting point (commit 8bcd348): `oversteer/telemetry.py` (UDP listener, rev
LEDs, launch limiter, `decode_sample()`), `oversteer/shift_learner.py`
(`CarModel`, `ShiftLearner`, SQLite schema v1), the Telemetry tab in
`gui.py`/`gtk_ui.py`, `data/telemetry/oversteer-shm-bridge.c` (OVST v2) and
35 passing tests.

## 1. What it has to do

| # | Requirement | Where in this design |
|---|---|---|
| 1 | Learn each car's best upshift per gear, per Oversteer profile, and keep learning; show it first in the Telemetry tab | §8.1, §12 |
| 2 | Long-term coaching: growth over time, habits, aware of discipline and of the shifter used (sequential, paddles, H-pattern); needs a database | §7, §9 |
| 3 | Detect discipline and surface from evidence, measurably, with honest confidence; unknown is a valid answer | §8.4–8.6 |
| 4 | Detect tuning (gear lengths, ride height, ...) per car, surface and stage; tuning advice fitted to the driver | §8.3, §10 |
| 5 | Read-only web page for a phone or laptop on the LAN, off by default, TCP 5301 | §11 |
| 6 | Launch limiter as an always-on tickbox | done (e1aa004) |

Non-goals for now: writing anything from the web page, cloud sync, sharing
data between users, AC EVO (`acevo_pmf_*`), LFS beyond what OutGauge already
gives.

## 2. Principles

- **Evidence or unknown.** Every detected fact (discipline, surface, wet,
  tune change, shifter) is a `Verdict` with a value, a confidence label and
  the evidence as sentences. "Unknown, because this game sends no position"
  is a complete answer. No invented probabilities: confidence is tied to
  which kind of evidence decided (§8.5).
- **Calibrate per game.** Units, signs and scales differ per game; no
  numeric threshold is shared across sources unless it is physical (g,
  m/s). Thresholds the research could not fix are marked **calibrate** and
  start as conservative defaults that answer unknown more often.
- **Priors are not evidence.** "Usually gravel here" (from a stage table or
  past runs) is shown next to what was measured, never merged into it.
- **The listener never waits.** The UDP thread does constant-time work per
  packet and never touches SQLite (§4).
- **Keep the raw data.** Raw captures and a compact per-run trace let every
  detector be re-run when its calibration improves.
- **Standard library only.** sqlite3, http.server, json, zlib, gzip,
  struct, threading, queue, statistics, array.

## 3. Decisions to confirm with the user

These are decided here so the work can go on; each is cheap to reverse.

- **D1. UDP default port 5300 → 5310.** The FH6 Data Out documentation says
  to avoid 5200–5300 because the game binds its own outgoing socket in that
  range; FH6 runs on the same host, so 5300 can collide. New profiles and
  profiles that never set a port get 5310; a profile that stored 5300 keeps
  it, and the "port in use" status then suggests 5310. `oversteer-run` and
  the bridge default follow (`OVERSTEER_TELEMETRY_PORT`, `--port`). The web
  page stays on **TCP 5301** as asked (TCP and UDP ports are separate; 5301
  TCP is not in FH6's way).
- **D2. Codemasters RPM units.** DiRT Rally 1/2 send engine rates in rad/s,
  not rpm/10 (every max/idle value in the car tables is an exact multiple of
  π/30 of a round rpm). Stored cars are migrated (§7.3) rather than
  forgotten.
- **D3. Car keys gain the game** (`<game>/<id>`); old keys are adopted on
  first sight (§5.4).
- **D4. The listener can run without rev lights** ("Learn from game
  telemetry", app-wide). Off by default: today the listener only runs with
  the rev lights on, and that stays the default. Learning should not depend
  on having LEDs.

## 4. Architecture, threads and performance

```
UDP :5310 ──► telemetry thread (Telemetry._run → Telemetry.handle)
               decode_sample() ─► RevLeds / launch limiter        (as now)
                               ─► ShiftLearner.feed()   in-memory, O(1)
                               ─► RunTracker.feed()     in-memory, O(1) accumulators
                               ─► capture queue (raw bytes)       optional
                               └► events ──► queue.Queue(maxsize=8192)
                                                  │
                         drive-log thread (DriveLog) ◄┘
                           owns the only write connection (WAL)
                           runs/segments/corners/shifts/metrics/tunes
                           detectors, coach, snapshots every 1 s
                           capture file writer (gzip)
                                  │ publishes immutable dicts
             ┌────────────────────┴───────────────────┐
     GTK main loop (1 s refresh)                web threads (GET only)
     reads snapshots; history via               read snapshots; history via
     its own read-only connection               per-thread read-only connections
```

- **Telemetry thread** (existing). Per packet: decode, LEDs, launch,
  `ShiftLearner.feed` (in-memory model only), `RunTracker.feed` (running
  sums, a 0.3 s ring buffer, the current 200 m segment buffer), and
  `put_nowait` of events. Budget: **mean ≤ 0.3 ms, p99 ≤ 2 ms** per packet
  at 120 packets/s on the user's machine (8.3 ms between packets). If the
  queue is full the event is dropped and counted (`DriveLog.dropped`), never
  waited on.
- **Drive-log thread** (new, `oversteer/drive_log.py`). Consumes events:
  shift, launch, run start/end, segment closed (≈ every 200 m), sample at
  10 Hz for the trace, raw packet for the capture. Computes segment features,
  corners, run metrics, verdicts, coaching; writes SQLite in batches (commit
  at most every 5 s and at every run end). Publishes `snapshot` dicts by
  plain attribute assignment (atomic in CPython), so readers take no lock.
  Expensive work (bootstrap confidence bands, calibration) runs here, never
  on the listener.
- **Readers.** The GTK timer (1 s) and the web handlers read the published
  snapshots; history queries use their own read-only connections
  (`sqlite3.connect('file:...?mode=ro', uri=True)`), which WAL lets run
  alongside the writer.
- **Locks.** `ShiftLearner.lock` guards only the in-memory `CarModel` (feed
  vs. snapshot); it is never held across I/O. Today's per-shift INSERT and
  the 20 s save under that lock move to the drive-log thread.
- **Memory.** Per run: a 10 Hz trace (≈ 12 float32 channels: 480 B/s,
  ≈ 290 KB for 10 minutes before zlib); the current segment's 60 Hz buffer
  (≈ 10 s) is discarded once its features are computed.
- **Measured by** `tests/bench_telemetry.py` (like `bench_latency.py`):
  replays a synthetic 10-minute stage through `Telemetry.handle` with a null
  LED object and reports mean/p99 per packet. Not part of pytest (timing is
  machine-dependent); a loose sanity test (< 2 ms mean) is.

## 5. Decoding layer

### 5.1 Module split

`oversteer/telemetry.py` keeps the listener, `RevLeds` and the launch
logic. Decoding moves to **`oversteer/telemetry_formats.py`**: `Sample`,
`decode_sample(data)`, one private function per format, the EA WRC
structure loader and the unit helpers. `telemetry.py` re-exports `Sample`,
`decode_sample` and `decode` so existing imports and tests keep working.
The socket loop body becomes `Telemetry.handle(now, data, addr)` so replay
and tests drive exactly the live path.

### 5.2 `Sample` v2

`__slots__`, every field None when the format does not carry it. Conventions
are fixed here and every decoder converts to them:

- Car frame **x forward, y left, z up** (ISO 8855); m, m/s, m/s², rad/s.
  `yaw_rate` positive turning left.
- `accel` is **specific force** (what an accelerometer reads: excludes
  gravity) in the car frame. Where a game sends g, multiply by 9.80665.
  Where the game's figure is derived from velocity (includes gravity on a
  hill), the decoder says so with `accel_kind = 'kinematic'`; otherwise
  `'specific'`. Which one each game sends is verified by capture (§6.3).
- Wheel tuples are ordered **FL, FR, RL, RR** (DiRT sends rear first).
- `gear`: -1 reverse, 0 neutral, 1.. forward (as today).
- `susp`: displacement in metres, compression positive; `susp_norm` 0..1 of
  travel where the game knows the travel.

| Group | Fields |
|---|---|
| As today | `rpm, max_rpm, shift, gear, speed, car, car_name, throttle, clutch, power, track` |
| Identity | `game` (§5.4), `idle_rpm, gears` (forward gear count), `car_class, drivetrain` ('fwd'/'rwd'/'awd'), `location, stage` (ids or names), `stage_length` (m), `game_mode` |
| Timing | `game_time` (the game's own clock, s), `running` (False in menus/pause where the game says so), `packet` ('update', 'start', 'end', 'pause', 'resume' for EA WRC) |
| Inputs | `brake, handbrake, steer` (-1 left .. 1 right, normalised; lock unknown) |
| Motion | `pos` (world x, y up, z; m), `vel` (car frame), `accel`, `accel_kind`, `yaw_rate`, `forward`, `up` (world unit vectors) |
| Wheels | `wheel_speed` (m/s at the tread), `slip_ratio` (game's own figure, `slip_kind` says 'raw' or 'normalised'), `slip_angle`, `susp, susp_vel, susp_norm, wheel_load, tyre_radius`, `ride_height` (front, rear; m), `fx, fy` (N, ACC/ACR) |
| Surface hints | `puddle` (per wheel), `rumble` (per wheel, kerb), `surface_rumble` (Forza), `grip` (AC surfaceGrip level), `rain` (ACC) |
| Structure | `lap, laps, lap_distance, distance, progress` (0..1), `stage_time`, `race_position` |
| Game's own | `game_shift_rpm` (EA shiftlights_rpm_end), `boost`, `brake_bias`, `auto_shift` (AC autoShifterOn) |

Initialising ≈ 60 slots costs a few µs per packet; measured by the bench.

### 5.3 Per format: fixes and additions

Numbers in brackets refer to the format research findings listed in the
appendix (§17).

**Forza (232/311/324/331).**
- Fix [4]: gear byte `0` → -1 (reverse), `1..10` forward, **`11` → 0
  (neutral)**. H-pattern shifts through neutral are then seen in FH6.
- Add from the sled: acceleration 20–28 (car frame X right, Y up, Z forward
  → convert to x forward, y left, z up), local velocity 32–40, angular
  velocity 44–52 (Y is yaw; its sign is **verified by capture**: a left
  turn must give a positive `yaw_rate`), yaw/pitch/roll 56–64, normalised suspension 68–80,
  slip ratio 84–96 (normalised: `slip_kind = 'normalised'`), wheel rotation
  100–112 (rad/s), rumble 116–128, puddle 132–144 (FH6 S32 0/1, FM F32
  depth: branch on game), surface rumble 148–160, slip angle 164–176,
  suspension metres 196–208.
- Car: class 216, PI 220, drivetrain 224 (0/1/2 → fwd/rwd/awd). FH: car
  group U32 at 232 (kept raw in `car_class` as `group:<n>`; its values are
  undocumented and only ever a hint).
- Dash: position B+0..8, boost B+40, distance B+48, lap B+68, race position
  B+70, brake B+72, handbrake B+74, steer B+76 (s8 / 127), current race time
  B+60. FM2023: track ordinal 327 → `stage = 'fm:<n>'`.
- `game`: 324 → `forza-fh`, 311/331 → `forza-fm`, 232 → `forza`.
- Tyre radius per wheel = speed / undriven-wheel rad/s, learnt (not
  decoded) in §8.3.

**Codemasters extradata 3 (DiRT Rally 1/2, DiRT 4; WRC Generations copies
the layout).**
- Fix [2]: engine rate 37, max 63, idle 64 are **rad/s → × 30/π**. Because
  WRCG's unit is unverified [3], the decoder decides per car from the
  numbers: if `floats[63] × 30/π` lies within 1 rpm of a multiple of 50,
  rad/s; else if `floats[63] × 10` does, rpm/10; else rad/s. The decision is
  cached per (game, raw max) and logged once.
- Fix [5]: gear `10` (or any negative) → -1; `0` neutral; `1..9` forward.
- Fix [6, 15]: key and name use the corrected rpm, rounded to 10 rpm (stable
  across the migration in §7.3).
- Add: total time 0 → `game_time`, lap time 1 → `stage_time`, lap distance
  2, progress 3, position 4–6, world velocity 8–10 (projected on the forward
  vector 14–16 and left vector 11–13 into the car frame), suspension
  position 17–20 and velocity 21–24 (**unit and sign verify**; research
  says mm, low confidence), wheel speed 25–28 (m/s, **sign verify**),
  steer 30, brake 31, lateral/longitudinal g 34/35 (×9.80665; DR2 sign
  **verify**), lap 36, laps 59/60, track length 61 → `stage_length`.
- `game`: 264 bytes → `dirt`; any other accepted length → `wrcg`
  (**provisional**: confirm WRCG's length from the "unknown size" log or a
  capture, then match it exactly). WRCG's g-forces are probably m/s² (/30
  in vAzhure's decoder) and reverse is 10; handled by per-game constants.
- Fix [8]: the catch-all only runs after the EA WRC checks below.
- Pause: DR2 repeats identical packets while paused (only the run time
  moves); the run tracker treats an unchanged `stage_time` with speed 0 for
  > 1 s as a pause, not driving.

**EA SPORTS WRC (new) [7].** EA's own JSON-configured UDP.
- Oversteer ships **`data/telemetry/eawrc/oversteer.json`**, a packet
  structure whose header is `["packet_4cc"]` and whose `session_update`
  lists: `packet_uid, game_total_time, game_delta_time, game_frame_count,
  shiftlights_fraction, shiftlights_rpm_start, shiftlights_rpm_end,
  shiftlights_rpm_valid, vehicle_gear_index, vehicle_gear_index_neutral,
  vehicle_gear_index_reverse, vehicle_gear_maximum, vehicle_speed,
  vehicle_transmission_speed, vehicle_position_{x,y,z},
  vehicle_velocity_{x,y,z}, vehicle_acceleration_{x,y,z},
  vehicle_left_direction_{x,y,z}, vehicle_forward_direction_{x,y,z},
  vehicle_up_direction_{x,y,z}, vehicle_hub_position_{bl,br,fl,fr},
  vehicle_hub_velocity_{bl,br,fl,fr}, vehicle_cp_forward_speed_{bl,br,fl,fr},
  vehicle_brake_temperature_{bl,br,fl,fr}, vehicle_engine_rpm_{max,idle,current},
  vehicle_throttle, vehicle_brake, vehicle_clutch, vehicle_steering,
  vehicle_handbrake, stage_current_time, stage_current_distance,
  stage_length, stage_shakedown, vehicle_id, vehicle_class_id,
  vehicle_manufacturer_id, location_id, route_id` (252 bytes by the
  channel types; computed, **verify on a capture**). `session_start`,
  `session_end`, `session_pause`, `session_resume` use the same list.
  Only channels from `channels.json` data version 2 are used; the v1.8
  channels (`game_mode`, `stage_result_*`, `vehicle_tyre_state_*`,
  `stage_progress`) go in an optional `oversteer_v18.json` once a capture
  confirms the game accepts them.
- The decoder builds its `struct` format from the shipped JSON and a
  channel → type table in Python (`EAWRC_TYPES`: fourcc `4s`, uint8 `B`,
  uint16 `H`, uint64 `Q`, float32 `f`, float64 `d`, **boolean 1 byte `B`**).
  Recognised by the 4CC (`SESS/SESU/SESE/SESP/SESR`) and the computed
  length; a length mismatch is logged once with both numbers.
- The game's default `wrc` structure (**237 bytes**, no header) is decoded
  too, by length, as a `session_update` without ids.
- Gear: compare with the sentinels sent in the packet (`neutral` → 0,
  `reverse` → -1). Motion axes are x **left**, y up, z forward: map to the
  car frame by projecting world vectors on the forward/left/up vectors.
  Per-wheel slip = contact-patch speed vs body speed. RPM is true rpm.
- Identity: `car = eawrc/<vehicle_id>`, `stage = eawrc:<location_id>:<route_id>`.
  Names come from `readme/ids.json` (UTF-16) when Oversteer can read it in
  the game's prefix (`…/compatdata/1849250/pfx/drive_c/users/steamuser/
  Documents/My Games/WRC/telemetry/readme/ids.json`); otherwise numbers,
  which the user can rename. The file is read, never copied into the repo.
- Setup help (Step D): the tab shows the lines to add to
  `Documents/My Games/WRC/telemetry/config.json` (`structure: "oversteer"`,
  each `session_*` packet, `ip: "127.0.0.1"`, `port: 5310`,
  `frequencyHz: 60`, enabled). **Verify the enable key's exact name** in the
  generated file (EA's readme says `enabled`; community notes say
  `bEnabled`). Oversteer does not edit the game's files in this plan.

**OutGauge (LFS layout; BeamNG).**
- Unchanged decoding. Fix [9]: BeamNG always sends `"beam"`, so
  `game = 'beamng'`, `car = 'beamng/unknown'`, and car identity comes from
  the fingerprint in §5.4. LFS: `game = 'lfs'`, car from `Car[4]`.
- Optional (Step A, low priority): BeamNG MotionSim `BNG1` packets on the
  same port for position, velocity, acceleration and angular velocity,
  merged into the next OutGauge sample by arrival time.

**OVST (bridge) v3 [10–14].** v1/v2 stay decodable. v3 keeps the v2 prefix
byte for byte and appends:

```c
/* version 3, after the 96-byte v2 prefix */
uint8_t game;            /* 1 AC, 2 ACC, 3 ACR (by executable, then smVersion) */
uint8_t flags2;          /* bit 0 autoShifterOn, bit 1 isInPit */
uint16_t reserved;
float clutch, steer;                 /* physics 364, 24 */
float accg[3];                       /* physics 44, as the game sends */
float local_vel[3];                  /* physics 568 */
float local_ang_vel[3];              /* physics 296 */
float wheel_slip[4];                 /* 56 */
float wheel_ang_speed[4];            /* 104 */
float susp_travel[4];                /* 184 */
float wheel_load[4];                 /* 72 */
float ride_height[2];                /* 268; NaN in ACR */
float tyre_radius[4];                /* static 436; NaN when 0 */
float susp_max_travel[4];            /* static 420 */
float fx[4], fy[4];                  /* 608, 624; ACC/ACR only, else NaN */
float current_max_rpm;               /* 588, int32 in memory; ACC/ACR only */
float track_length;                  /* static 520 (trackSplineLength) */
float spline_pos, distance;          /* graphics normalizedCarPosition, distanceTraveled */
float surface_grip, brake_bias;      /* graphics surfaceGrip, physics 564 */
int32_t laps, session_type;          /* graphics numberOfLaps, ACC sessionType */
float world_pos[3];                  /* graphics carCoordinates (player) */
```

  Physics view 580 B for AC1, 800 B for ACC/ACR; static 684/820; if
  `MapViewOfFile` fails with the larger size, retry smaller and send NaN for
  the missing fields [13]. `max_rpm` prefers `current_max_rpm` when > 0 in
  ACC/ACR [10]. Car key `ac|acc|acr/<carModel>`; ACR stage key
  `acr:<round(track_length)>` because `track` is often empty [11]. Graphics
  offsets differ between AC1 and ACC: the bridge branches on the game and
  **the graphics fields are sent only after one ACR capture confirms them**
  (NaN until then). Gear stays `gear - 1` [12]; verify reverse logs -1 once
  in ACR. ACR temperatures are Kelvin (not forwarded now). The `.exe` is
  rebuilt with `scripts/build-shm-bridge.sh`; if no toolchain is available
  the C change is committed and the rebuild is noted as deferred.

### 5.4 Identity: game, car, stage

- `Sample.game` ∈ `forza-fh, forza-fm, forza, dirt, wrcg, eawrc, ac, acc,
  acr, acpmf` (bridge v1/v2, game unknown), `beamng, lfs`.
- `Sample.car` = `<game>/<id>`: `forza-fh/<ordinal>`,
  `dirt/<max>-<idle>-<gears>`, `eawrc/<vehicle_id>`, `acr/<carModel>`,
  `beamng/unknown`.
- **Adoption of old keys** (D3): when `(profile, key)` is not in the
  database, look for its legacy form (`forza-<n>`, `codemasters-…` after the
  unit migration, `acpmf-<name>`, `outgauge-beam`) and rename that row (and
  its sessions) to the new key. One rename per car, logged.
- **Fingerprint split** for games whose key is not unique (DiRT collisions
  [6], BeamNG): when a session's learnt ratio set differs from the car's on
  ≥ 2 gears by > 3 % (**calibrate**), it is recorded as a new tune (§8.3),
  not silently merged. The tab offers "This is a different car" on such a
  tune, which splits it into `<key>#2` with its own model. Automatic
  splitting is deferred: a tune change and a different car look the same in
  these games.
- `stage` key: `eawrc:<loc>:<route>`, `dirt:<round(length)>:<round(start z,
  -1)>`, `fm:<track ordinal>`, `acr:<length>`, `ac:<track>:<config>`,
  `acc:<track>`; Forza Horizon and BeamNG have none (§8.4 builds a start-cell
  key for repeated routes).

## 6. Capture and replay

### 6.1 File format (`oversteer/telemetry_capture.py`)

```
<data>/oversteer/captures/YYYYmmdd-HHMMSS.ovcap.gz      gzip stream
  b'OVCAP1\n'
  one JSON line: {"started": <epoch>, "port": 5310, "oversteer": "<version>", "note": ""}
  records: struct '<dIH' (t since start as float64, source IPv4 as u32, length) + bytes
```

- `CaptureWriter(path)`: `.write(t, addr, data)`, `.close()`; driven by the
  drive-log thread from the raw-packet events (the listener only enqueues).
- `read_capture(path) -> (meta, iterator of (t, addr, data))` tolerates a
  truncated tail (a crash mid-write).
- A sidecar `<file>.json` holds the labels once set (§8.6), so a capture
  copied elsewhere (a test fixture, a bug report) describes itself.
- Size: ≈ 20 KB/s raw at 60 Hz, ≈ 5 KB/s gzipped. Cap 1 GB in total
  (preference); the oldest **unlabelled** captures go first. Recording is
  off by default, switched in the tab.

### 6.2 Tools

- `scripts/telemetry-capture.py [port] [--write FILE]`: as now, plus writing
  a capture file with the same format.
- `scripts/telemetry-replay.py FILE [--db PATH] [--profile NAME]
  [--send HOST:PORT [--realtime]]`: feeds a capture through
  `Telemetry.handle` with a null LED object and the capture's own clock into
  a database (default: a temporary one) and prints the learnt cars, runs and
  verdicts; or re-sends it over UDP to a running Oversteer (for trying the
  web page or the tab without the game).
- `scripts/telemetry-calibrate.py [--db PATH] [--game GAME]`: prints the
  per-class feature distributions of labelled segments and the held-out
  accuracy, and stores the model (§8.6).

### 6.3 Captures the user is asked for (they settle the "verify" items)

1. FH6: 2 minutes free roam, one road event, a hill at constant speed
   (accelerometer kind), a left turn (yaw sign), neutral with the H-pattern.
2. DiRT Rally 2.0: a stage start with a pause, a left turn, reverse.
3. WRC Generations: any stage (packet length, rpm units, g units, reverse).
4. EA SPORTS WRC with `oversteer.json` enabled: one stage start to finish,
   with a pause and a restart.
5. ACR with bridge v3 `--verbose`: a stage including reverse.
Each capture, once labelled, becomes a fixture in `tests/data/` if under
~1 MB (cut to the useful seconds by `telemetry-replay.py --cut A:B`).

## 7. Data model (SQLite, schema v2)

### 7.1 Conventions

- File `~/.local/share/oversteer/telemetry.db` (as now). `PRAGMA
  journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`.
- Version in `PRAGMA user_version`: 0 with a `cars` table = v1 (today), 2 =
  this design. Migrations run in one transaction at open; the old file is
  first copied to `telemetry.db.v1.bak` (once).
- Times are Unix epoch seconds (REAL). JSON columns hold things whose shape
  will change with calibration (feature vectors, evidence), never things
  that are queried.

### 7.2 Schema

```sql
CREATE TABLE cars (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    game TEXT NOT NULL,
    key TEXT NOT NULL,              -- '<game>/<id>'
    name TEXT,
    user_named INTEGER DEFAULT 0,
    car_class TEXT, drivetrain TEXT,
    model TEXT NOT NULL,            -- CarModel.to_dict() as JSON (version 2)
    updated REAL,
    UNIQUE (profile, key)
);
CREATE TABLE tunes (                -- a setup epoch: one gearing (and what else is measurable)
    id INTEGER PRIMARY KEY,
    car INTEGER NOT NULL REFERENCES cars(id) ON DELETE CASCADE,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    stage TEXT, surface TEXT,       -- where it was first used
    ratios TEXT NOT NULL,           -- JSON {gear: rpm per m/s}
    change TEXT,                    -- 'first', 'final-drive', 'gears:3,4', 'user'
    tyre_radius REAL, ride_height_f REAL, ride_height_r REAL, brake_bias REAL,
    hub_rest TEXT,                  -- JSON per-wheel resting hub position (EA WRC)
    note TEXT
);
CREATE TABLE stages (
    key TEXT PRIMARY KEY,           -- §5.4
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
    track TEXT,                     -- as the game names it (v1 column)
    stage TEXT REFERENCES stages(key),
    game_mode TEXT,
    discipline TEXT, discipline_conf TEXT,   -- majority of the runs
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
    detector_version INTEGER,       -- which calibration produced the verdicts
    trace BLOB,                     -- zlib(array('f')) at 10 Hz, see TRACE_CHANNELS
    trace_version INTEGER
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
    features TEXT NOT NULL,         -- JSON, §8.6
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
    gear_to INTEGER,                -- v1 rows: gear + 1
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
    name TEXT NOT NULL,             -- §9.1
    value REAL NOT NULL, count INTEGER NOT NULL,
    gear INTEGER, method TEXT
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
    deployed INTEGER NOT NULL,      -- 1 when holdout >= 0.90 (calibrate)
    PRIMARY KEY (game, kind)
);
CREATE TABLE coach_state (
    profile TEXT NOT NULL,
    car INTEGER NOT NULL DEFAULT 0, -- 0 = about the driver, not one car
    tip TEXT NOT NULL,              -- tip id, e.g. 'shift.early:h-pattern:gravel'
    first_shown REAL, last_shown REAL, times INTEGER DEFAULT 0,
    value REAL,                     -- the metric when last shown
    quiet INTEGER DEFAULT 0,
    PRIMARY KEY (profile, car, tip)
);
CREATE INDEX sessions_car ON sessions (profile, car, started);
CREATE INDEX runs_session ON runs (session);
CREATE INDEX runs_stage ON runs (stage, started);
CREATE INDEX shifts_session ON shifts (session);
CREATE INDEX metrics_name ON metrics (name, session);
CREATE INDEX segments_run ON segments (run);
CREATE INDEX corners_run ON corners (run);
```

`TRACE_CHANNELS = ('t', 'distance', 'speed', 'rpm', 'gear', 'throttle',
'brake', 'clutch', 'handbrake', 'steer', 'a_long', 'a_lat', 'yaw_rate',
'slip_drive', 'susp_rms')`, NaN where not sent. Traces are capped at 200 MB
in total (**preference**); the oldest traces are dropped first, their metrics
and verdicts stay.

### 7.3 Migration v1 → v2

In one transaction, after the backup copy:

1. `cars`: create the new table, copy rows: `game` from the old key prefix
   (`forza-` → `forza`, `codemasters-` → `codemasters`, `acpmf-` →
   `acpmf`, `outgauge-beam` → `beamng`, other `outgauge-` → `lfs`), new key
   `<game>/<rest>`; the adoption rule (§5.4) later moves `forza/…` to
   `forza-fh/…` and so on.
2. **Codemasters units (D2).** For each `codemasters` car, test the stored
   limiter: if `limiter / 10 × 30/π` is within 2 rpm of a multiple of 50,
   the car was recorded in rad/s × 10; rescale by `f = 3/π` (0.95493) the
   model's `limiter, top_seen`, every `upshifts` value, every `ratios`
   value, and re-bin `power` bands (`band × f`, merging lists); rescale that
   car's `shifts.rpm` and `shifts.best`; rebuild the key with the corrected
   rpm rounded to 10. Otherwise leave the car as it is and log it (a WRCG
   car whose unit was right).
3. `sessions`: add the new columns; `car` TEXT → the new car id by (profile,
   key); rows whose car no longer exists are dropped (they have no model).
4. `shifts`: add the new columns; `gear_to = gear + 1`, `direction = 'up'`,
   `flat_out = throttle IS NULL OR throttle >= 0.8`.
5. Create the new tables; `PRAGMA user_version = 2`.
6. `CarModel.from_dict` accepts model version 1 (pooled power only) and
   upgrades on save (§8.1).

Tested with a v1 database built from today's `SCHEMA` and rows in
`tests/test_store.py`.

### 7.4 Access (`oversteer/telemetry_store.py`)

- `open_store(path) -> sqlite3.Connection` (writer; migrates) and
  `open_reader(path)` (read-only URI).
- Writer API used by the drive-log thread only: `upsert_car`, `save_model`,
  `start_session`, `end_session`, `start_run`, `end_run`, `add_segment`,
  `add_corners`, `add_shift`, `add_metrics`, `add_tune`, `touch_tune`,
  `set_label`, `add_capture`, `save_calibration`, `coach_seen`.
- Reader API used by GTK and web: `cars(profile)`, `car(profile, key)`,
  `sessions(car_id, limit)`, `session(id)` (with runs, verdicts, evidence),
  `metric_series(profile, name, car=None, discipline=None, surface=None,
  method=None, limit=50)`, `stage(key)`, `tunes(car_id)`, `labels_for(game)`.

## 8. Learners and detectors

### 8.1 Shift points (`shift_learner.py`)

Keep the method (crossover of P(rpm) with P(rpm × r_{n+1}/r_n)); change:

1. **Power per (gear, band)** in `CarModel.power_g[(gear, band)]`, pooled
   `power[band]` kept for gears with no overlap. `best_shift(gear)` compares
   `P_gear(rpm)` with `P_{gear+1}(rpm × step)` when both are known (same
   road speed, so drag and slope cancel), else the pooled curve.
2. **Slope-free acceleration.** With `accel_kind == 'specific'`, use
   `accel[0]`; with only a velocity vector, `a = dv/dt + g·sin(grade)`,
   `grade = asin(v_up / |v|)`; else dv/dt as now. Power stays
   `(a + C0 + C2·v²)·v`.
3. **Gates** on power samples: driven-wheel slip ≤ 0.08 where wheel speeds
   exist (**calibrate** per game), brake < 0.05, throttle ≥ 0.95 **held**
   for `BOOST_HOLD` = 0.8 s (**calibrate**; later learnt per car as the rise
   time of acceleration after throttle-on).
4. **Band estimate**: P75 of the band when slope-corrected, median
   otherwise (downhill would inflate a high quantile).
5. **H-pattern window fix.** A 0.3 s ring buffer of (t, rpm, throttle): a
   change up records the **peak rpm and the maximum throttle** over the
   window before the old gear was left. Today's rule drops most H-pattern
   shifts because the driver has already lifted.
6. **Limiter precedence by source**: `feed(..., limiter, limiter_source)`
   with `'launch' > 'game' > 'seen'`; a launch-learnt figure replaces a
   game one even when lower (WRCG over-reports).
7. **Confidence band**: 20 bootstrap resamples of each band's samples →
   (P10, P90) of the crossover → `best_low`, `best_high` in the snapshot;
   computed on the drive-log thread at most every 10 s per car.
8. **Drag fit** (optional in Step B): fit C0, C2 per car by minimising the
   disagreement of `P_gear` between gears in their overlapping rpm range.
   Validated on Forza captures, where the game's power figure is truth.
9. `CarModel.to_dict()` gains `version: 2`, `power_g`, `limiter_source`,
   `boost_hold`, `drag`.

Advice sentences stay in `CarModel.advice` style but move to the coach
(§9), which knows method, surface and history.

### 8.2 Shifts and the shifter

- **Method** per shift: `h-pattern` when neutral was seen between gears
  (Forza now included), `sequential` / `paddles` from the last button press
  within 0.6 s (the source device: the T500 RS source in the combined
  device → sequential, the wheel's paddles → paddles), `auto` when the game
  says (AC `autoShifterOn`) or no press, no neutral and no clutch movement
  are seen, else NULL. The input side (setting `launch_inputs['shift_press']
  = (monotonic, 'shifter'|'wheel')` in `GtkController.process_events`, from
  the combined device's mapping sources) is wired in Step D; until then only
  h-pattern and auto are recognised.
- **Downshifts** recorded too, with `engage_rpm`: over-rev when
  `engage_rpm > 0.95 × limiter`.
- **H-pattern flags**: `neutral_time`; `missed` when neutral lasts > 0.5 s
  on an upshift under throttle; `skip` for n → n+2 on an upshift attempt
  (or n → n−1).
- **Sequential flags**: `double-tap` when two changes in the same direction
  come < 0.25 s apart and the second is reverted within 1 s
  (**calibrate**).
- Session `shifter` = the method of ≥ 80 % of the session's shifts, else
  `mixed`.

### 8.3 Gear ratios, tunes and tune detection

- Ratios as now (rpm per m/s per gear). **Re-tune rules**: a new ratio is
  accepted only in the first 60 s or 2 km of a session (setups change in
  menus, which end sessions in every supported game), with brake = 0, over
  samples spanning ≥ 2 speed bins 5 m/s wide; a shorter ratio (higher
  rpm/m/s, what spin also produces) needs twice the samples.
- **Tune classification**: all gears changed by the same factor ±1 % →
  `final-drive`; some gears only → `gears:<list>`. A new tune row opens a
  new setup epoch; its ratios become the car's current ones, and the shift
  points of the changed gears are relearnt (as today).
- **Measured per tune**: tyre radius (speed / undriven-wheel rad/s: Forza,
  AC static), ride height (AC1/ACC), brake bias (ACC/ACR), resting hub
  positions at rest on flat ground (EA WRC, as a ride-height *change* only,
  low confidence). The tune shows these; nothing is inferred beyond them.
- Tunes are keyed by (car, stage, surface) when those are known, so the tab
  can say "on this stage you ran the 3.9 final drive last time".

### 8.4 Runs, segments, corners (`oversteer/drive_log.py`)

`RunTracker.feed(now, sample)` (listener thread, O(1)) and
`RunTracker.close_*` (drive-log thread):

- **Session** = telemetry from one car without a silence longer than
  `SESSION_GAP` = 120 s. (The 2 s idle still resets the LEDs and the launch
  limiter; it no longer ends the session, so a Forza pause does not split a
  stage.)
- **Run boundaries**: a teleport (> 50 m between consecutive positions, or
  an implied speed > 100 m/s), the game's own signals (EA `SESS`/`SESE`,
  `stage_time` or `lap_distance` going back to 0, Forza race time reset), a
  silence > 2 s followed by a position jump. A pause (EA `SESP`, DiRT
  repeated packets, Forza silence without a jump) suspends the run.
- **Distance**: the game's stage/lap distance when sent, else speed
  integrated.
- **Segments** every 200 m (or 10 s if no distance): features in §8.6,
  computed on the drive-log thread from the segment's 60 Hz buffer.
- **Corners**: yaw rate (or heading rate from the forward vector) smoothed
  over 0.5 s; a corner is |yaw rate| > 0.15 rad/s for ≥ 1 s or a heading
  change ≥ 30° (**calibrate**); the slowest point is the apex; entry and
  exit speeds 2 s either side; counter-steer fraction = time with steer sign
  opposite to yaw rate.
- **Trace** at 10 Hz (§7.2).
- **Start-cell stage key** for games that name no stage (Forza Horizon,
  BeamNG, AC practice): `cell:<game>:<x/50>:<z/50>:<heading/45°>` of the
  run's start, completed by the run length rounded to 100 m once it ends.
  Used only to compare runs of the same route and to build priors; two
  routes from the same start stay apart by length.

### 8.5 Discipline (`oversteer/drive_detect.py`)

Classes: `rally-stage`, `hillclimb`, `circuit`, `rallycross`, `drift`,
`free-roam`, `time-attack`, `unknown`.

```python
class Verdict:
    value: str            # a class or 'unknown'
    confidence: str|None  # 'game', 'high', 'medium', 'low'; None with unknown
    evidence: list[str]   # sentences shown to the user
    missing: list[str]    # what would have been needed

def classify_discipline(run, history, calibration) -> Verdict
```

Tiers, in order; the first that fires with enough data decides:

1. **Game fields** (`game`): EA WRC session packets → rally stage
   (shakedown noted); ACC `session_type`; DiRT `laps > 1` → circuit family,
   and with lap length < 1.5 km on a loose surface → rallycross; FM
   `TrackOrdinal` with laps → circuit.
2. **Topology** (`high` with ≥ 2 closures, `medium` with one): positions
   hashed into 10 m cells; a loop closure is a revisit after ≥ 500 m of path
   with heading within ±45°; laps between closures within ±30 % (jokers).
   Zero closures over ≥ 2 km → point-to-point family.
3. **Elevation** (`medium`): hillclimb when net grade ≥ 4 % over ≥ 2 km and
   the descending fraction < 10 %.
4. **Dynamics** (`low`, **calibrate** every number): body slip angle β
   over moving samples (v > 8 m/s): drift when P50|β| > 10° with sustained
   counter-steer; rally when P50 < 6° and P90 > 12°; circuit when P90 < 6°;
   handbrake pulls per km; corners per km; stops per 10 min and reversing →
   free roam.
5. **Start signature**: a launch hold then one continuous 2–15 min run with
   no stops → stage; a rolling start into loops → time attack.

Needs ≥ 90 s and ≥ 2 km moving; below that, or when tiers disagree →
unknown with the reason ("BeamNG through OutGauge sends no position").
After a stage key has three runs classified alike, that becomes its prior
("usually a rally stage here"), shown separately; each run is still
classified.

### 8.6 Surface and wet

Classes `tarmac`, `gravel`, `snow`, `ice`; composite answers `mixed:<a>,<b>`
(second surface ≥ 25 % of voting segments) and `loose-low` (snow or wet
gravel, a pair that does not separate); `unknown`. Wet ∈ `dry`, `wet`,
`unknown`, separately.

**Segment features** (per ~200 m, JSON in `segments.features`):

| Feature | Definition | Sources |
|---|---|---|
| `mu_p95`, `mu_p50` | P95 and P50 of √(a_lat² + a_long²)/g in the 10–25 m/s bin | accelerometer; else v·yaw rate and slope-corrected dv/dt |
| `spin` | fraction of full-throttle time in gear ≥ 3 with drive slip > κ₀ (**calibrate** per game) | wheel speeds / slip ratio / contact-patch speed |
| `rough` | RMS of 5–25 Hz suspension velocity ÷ speed | DiRT 21–24, Forza travel differenced, AC travel differenced, EA hub velocity; Forza `surface_rumble` as its own feature |
| `lr_corr` | correlation of front-left and front-right suspension velocity | same |
| `post_peak` | slope of a_lat vs slip angle beyond the peak | Forza, ACC/ACR slip angle; else β from vectors |
| `mu_wheel` | P95 of √(fx²+fy²)/load at the slip peak | ACR/ACC via bridge v3 |
| `puddle`, `rain`, `grip` | direct hints | Forza, ACC, AC |
| `n`, `speed_mean` | sample count, mean speed | all |

**Limit gate**: a segment votes only if `mu_p95 ≥ 1.5 × mu_p50` and it has
≥ 120 samples above 10 m/s (**calibrate**); otherwise `pushed = 0` ("never
near the grip limit"). A run needs ≥ 5 voting segments, else unknown.

**Classifier** per game: diagonal Gaussian per class on standardised
features. Reject when the best class's Mahalanobis² exceeds the χ²(d)
99 % point (≈ 15.1 for 5 features): unlike anything calibrated → unknown.
When the two best log-likelihoods differ by < ln 4 (**calibrate**): the
pair (`loose-low` for snow/wet gravel, else unknown). Run answer from the
voting segments; confidence `high` when ≥ 80 % agree with a margin ≥ 2× the
ambiguity margin and ≥ 10 votes, `medium` when ≥ 60 %, else `low` (shown as
"maybe"). Wet: Forza puddles and ACC rain override; otherwise smooth +
low μ̂ = wet tarmac only when tarmac is the prior.

**Priors** (`data/telemetry/stages/*.json`, loaded into `stages`): EA WRC
location → surface (Sweden snow; Monte-Carlo and Central Europe mixed
tarmac/snow/wet; Croatia, Japan, Mediterraneo, Iberia tarmac; most others
gravel; **confirm per route**); DiRT Rally 2.0 stages by length and start
position (derived from dr2_logger's MIT-licensed tables, with its notice).
The tab shows "measured gravel (high) · usually gravel here"; a
disagreement is highlighted for the user to label.

**Calibration** (`calibrate(game) -> CalibrationResult`): from labelled
runs (`labels`) and their segments. Needs ≥ 3 labelled runs per class and
≥ 2 classes; leave-one-run-out segment accuracy ≥ 0.90 (**calibrate**) to
deploy. Undeployed or missing → every run of that game answers unknown with
"not calibrated for <game> yet: label a few stages of each surface" (or the
current holdout figure). Only calibrated classes can be answered; the reject
rule keeps an uncalibrated surface from being forced into a known one. Each
recalibration bumps `version`; runs keep their `detector_version`, and
"Re-check old runs" re-classifies from stored segments.

Labels come from the tab (Step D) and the web page never writes them.

## 9. Coaching engine (`oversteer/coach.py`)

### 9.1 Metrics (per run, stored in `metrics`)

| Name | Unit | Counted when | Weight / gate |
|---|---|---|---|
| `shift.error` (per gear, per method) | rpm, signed vs best | flat-out upshifts with a known best | full on tarmac/circuit; low on gravel/snow (traction-limited, short-shifting can be right); silent when surface unknown and the car is traction-limited (slip at the shift > κ₀) |
| `shift.in_band` | fraction within the confidence band ±100 rpm | same | same |
| `limiter.per_km` | s/km, gears below top | always | all disciplines |
| `limiter.top` | s per run in top gear | always | goes to tuning (§10), not driving |
| `launch.t50` | s from release to 50 km/h | launch detected | rally, rallycross, drag |
| `launch.slip` | peak drive slip in the first 2 s | wheel speeds sent | per surface |
| `launch.bog` / `launch.stall` | count | rpm < 60 % of launch rpm / 0 | all |
| `pedal.overlap` | fraction with throttle and brake > 20 % | always | reported only on circuit or tarmac and only with a slower exit than the driver's best there (left-foot braking is technique on loose surfaces) |
| `pedal.coast` | s/km with both < 5 % above 10 m/s | always | compared with the driver's own runs of the same stage, never absolute |
| `hpattern.neutral` | median s per shift | H-pattern shifts | per method |
| `hpattern.missed`, `hpattern.skip` | per 100 shifts | H-pattern | |
| `downshift.over_rev` | per 100 downshifts | engage rpm known | |
| `seq.double_tap` | per 100 shifts | sequential/paddles | |
| `handbrake.per_km` | pulls/km | handbrake known | rally, drift |
| `counter_steer` | fraction of cornering time | steer + yaw rate | technique on loose, habit or setup on tarmac |
| `consistency.split_sd` | s, SD of splits at every 10 % of the stage | ≥ 3 runs of the stage | per stage |
| `corner.loss` | s lost in the 3 worst corners vs own best run | ≥ 2 runs of the stage | per stage |

### 9.2 Over time

- **Series**: `metric_series(...)` sliced by car, discipline, surface and
  method. A metric is compared only within the same slice.
- **Growth**: the last N = 5 sessions against the previous 5, each side with
  ≥ 5 counted events; reported only past an effect size per metric
  (**calibrate**; starting values: shift error median moved > 150 rpm,
  limiter s/km halved, neutral time −20 %).
- **Habit**: a metric beyond its threshold in ≥ 70 % of ≥ 5 sessions in a
  slice. One **focus** habit at a time: the one with the largest estimated
  time cost (shift error × drive lost, limiter time), ties broken by count.
- **Rate limiting** (`coach_state`): at most 3 tips and 1 praise per view;
  a tip is re-shown when its value got worse by ≥ 20 %, after 14 days, or
  when the user asks ("Show all"); after two showings without change it
  becomes a quiet "still:" line at the bottom.
- **Phrasing**: observation + number + consequence + one action, in the
  sentence style of today's `CarModel.advice`. Praise carries a number too.
  Examples:
  - "2→3 with the H-pattern: you change up at 6400 rpm, 500 early; 3rd gives
    9 % less drive there. Hold it to about 6900."
  - "With the H-pattern you change up 400 rpm earlier than with the
    paddles." (shifter comparison, needs ≥ 10 shifts each)
  - "Better: 4 of your last 5 stages had your 2→3 within 100 rpm of the
    best; two weeks ago you were 500 early."
- **Gating**: coaching that depends on discipline or surface is silent when
  either is unknown, and says so once ("tips about short-shifting wait until
  the surface is known").

`Coach(reader).tips(profile, car_id=None, limit=3) -> list[Tip]` with
`Tip(id, kind ('focus', 'tip', 'praise', 'still'), text, evidence,
value)`; `Coach.seen(tips)` updates `coach_state` (drive-log thread).

## 10. Tuning advice (`oversteer/tuning.py`)

Keyed by (car, tune, surface, stage), falling back to the car when the
surface is unknown. Each rule returns an observation, the setup levers as a
family (never click counts) and whether it is a setup or a driving matter.

| Observation | Rule (**calibrate** all numbers) | Advice |
|---|---|---|
| Final drive too short | `limiter.top` > 1 s per run on this stage | lengthen final drive / top gear |
| Final drive too long | top gear used > 5 % of the time but never above 85 % of the limiter | shorten final drive |
| A gear too long for the stage | rpm 1 s after throttle reapplication after corners in gear n below the power band on > 50 % of exits | shorten gear n, or use n−1 there |
| Bottoming | travel saturating (Forza normalised 1.0, AC `suspensionMaxTravel`, DiRT/EA saturation) per km, split into landings (vertical accel spike) and compressions | ride height up / springs or bump stiffer |
| Spring/damper balance | travel histogram piled at one end; > 1.5 oscillations after a hit | softer/stiffer; more damping |
| Understeer/oversteer | sign and change between tunes of the steer-vs-a_lat gradient (arbitrary units: steering lock unknown); counter-steer fraction per surface | rear/front anti-roll bar, springs, differential |
| Open differential | inside-wheel spin on > 30 % of hairpin exits | lock the differential more |
| Brake balance | front vs rear locking under braking (wheel speeds) | brake bias |

Driver-fitted: a counter-steering driver in a car that measures oversteer on
tarmac gets the setup hint; a neutral car with a sliding driver gets the
driving hint. Tuning advice needs ≥ 3 runs on the tune, otherwise it says
what it is still collecting.

## 11. Web page (`oversteer/telemetry_web.py`)

- `TelemetryWeb(port=5301, bind='0.0.0.0', live=callable, reader_path=...,
  profile=callable)`: a `ThreadingHTTPServer` subclass on a daemon thread;
  `start()` returns False with the error when the port is taken; `stop()`
  shuts it down.
- **Off by default.** App preferences (`config.ini` DEFAULT section):
  `telemetry_web` (0/1), `telemetry_web_port` (5301), `telemetry_web_bind`
  (`lan` → 0.0.0.0, `local` → 127.0.0.1).
- **Read-only**: only GET and HEAD; everything else 405. No cookies, no
  CORS headers (so pages on other origins cannot read the JSON with fetch),
  at most 8 requests in flight (503 beyond), 10 s socket timeout, request
  logging at debug level only.
- **Host check** against DNS rebinding: requests whose `Host` is not an IP
  literal, `localhost`, the machine's hostname or `<hostname>.local` get 421.
- Endpoints (JSON, UTF-8, `Cache-Control: no-store`):
  - `GET /` → `index.html`, one self-contained file (inline CSS and JS, no
    external resources) from `data/telemetry/web/`, installed with the other
    telemetry data.
  - `GET /api/v1/status` → version, profile, listening port, source,
    game, car, session id.
  - `GET /api/v1/live` → the last sample (gear, rpm, speed, shift target,
    limiter, stage distance) or `null`; the page polls it once a second.
  - `GET /api/v1/cars` → cars in the current profile.
  - `GET /api/v1/cars/<id>` → the shift table (best, band, your average per
    method), current tune, tuning advice.
  - `GET /api/v1/cars/<id>/sessions?limit=20` → sessions with discipline,
    surface, confidence and evidence.
  - `GET /api/v1/sessions/<id>` → runs, metrics, verdicts, laps.
  - `GET /api/v1/coach?car=<id>` → focus, tips, praise, growth. The web page
    does not mark tips as seen (read-only); only the GTK tab does.
- The page: live strip (gear, rpm bar to the shift point, speed), the car's
  shift table, coaching, recent sessions with expandable evidence. Phone
  first (single column, 16 px gutters), light and dark by
  `prefers-color-scheme`.
- **What no authentication implies** (shown next to the switch): anyone on
  the same network can read the driving history, car and profile names,
  when you drive, and the live telemetry while you drive; nobody can change
  anything. On a public or shared network, use "this computer only" or leave
  it off. The Flatpak already has network access; a host firewall
  (firewalld, ufw) may need TCP 5301 opened, and the tab says so when the
  page is on but has never been reached from another address.
- The tab shows the URL(s): `http://<LAN address>:5301/`, found by
  connecting a UDP socket to a TEST-NET address (no packet is sent).

## 12. GTK Telemetry tab

What shows first is what requirement 1 asks for. Top to bottom:

1. **Car bar**: car picker (as now), rename, forget, "Label last session…".
2. **Context line**: discipline and surface with confidence and prior,
   stage/location, tune, shifter — e.g. "Rally stage (high: point to point,
   9.8 km) · gravel (medium) · usually gravel here · H-pattern". Clicking
   shows the evidence.
3. **Shift table**: Gear | Best (± band) | You | H-pattern | Sequential |
   Paddles | Data; method columns only when they have shifts.
4. **Coaching**: focus habit, up to 3 tips, praise, growth line; "Show all"
   expander with the quiet lines.
5. **Tuning**: current tune (ratios, change type, measured ride height /
   tyre radius / brake bias where sent), up to 2 tuning notes, "This is a
   different car" on a fingerprint split.
6. **Recent sessions** (10): date, stage, discipline/surface badges,
   shift error, limiter s/km.
7. **Live line** (as now).
8. **Settings** expander: the rev light switches (moved from the top),
   "Learn from game telemetry" (D4), UDP port, web page switch, port,
   bind, URL and the note of §11, "Record raw telemetry" with the capture
   folder and size, EA WRC setup lines.

The label dialog: discipline, surface, wet, shifter (preset from the
detected ones), note; saved to `labels` for every run of the session.

View code stays thin: `telemetry_view.py` (pure functions building rows and
strings from snapshots) is unit-tested; `gtk_ui.py` only places widgets.

## 13. Testing

- `pytest -q tests` stays green at every commit; no test touches /sys,
  /dev or the GUI.
- **Decoders** (`tests/test_formats.py`): packets built with `struct` per
  layout; regression tests for every research fix (rad/s, Forza gear 11,
  DiRT gear 10, EA 237 bytes not taken for DiRT, EA 4CC structure, OVST v3
  with NaN fields, v1/v2 still decode).
- **Simulator** (`tests/sim.py`): `SimCar` (ratios, power curve, mass,
  C0/C2, drivetrain, turbo lag), `SimStage` (length, grade profile, corners,
  surface segments with μ, roughness spectrum, spin), `SimDriver` (shift
  point, method with realistic lift and neutral time, left-foot braking,
  counter-steer) → `Sample`s at 60 Hz, and `encode(sample, fmt)` to raw
  packets in each format so tests run decode → pipeline → database end to
  end. The current simulated car in `test_shift_learner.py` moves there.
- **Learner corrections**, each with a test: slope (a hilly stage still
  finds the analytic shift within 100 rpm), slip gate, turbo lag, H-pattern
  window, limiter precedence, re-tune rules (a steady spin on snow is not a
  re-tune), confidence band contains the analytic answer.
- **Detectors**: honest-unknown tests (no calibration → unknown; cruising on
  ice → unknown "never near the limit"; OutGauge → unknown "no position");
  calibration round trip on simulated labelled runs reaching the holdout
  gate; the reject rule on an uncalibrated surface; topology on a simulated
  circuit vs stage.
- **Store**: v1 → v2 migration from a real v1 file built with today's
  schema, including the Codemasters rescale; WAL reader alongside the writer.
- **Coach**: synthetic metric histories → habits, growth, rate limiting,
  gating by discipline/surface/method, phrasing snapshots.
- **Web**: server on port 0, GET each endpoint, POST → 405, bad Host →
  421, 9th concurrent request → 503.
- **Captures**: write/read round trip, truncated tail, replay determinism
  (same capture → same database contents).
- **Recorded captures** (`tests/data/*.ovcap.gz`, when the user provides
  them): decode without unknown packets; the verdicts match the labels, or
  the test documents the known miss.
- `tests/bench_telemetry.py`: per-packet cost (outside pytest).

## 14. Build plan

Each step ends green, with small commits, and updates the checklist below.

**Step A: decoding, Sample fidelity, capture and replay.**
1. Split `telemetry_formats.py` out of `telemetry.py` (no behaviour change).
2. `Sample` v2 fields and conventions (§5.2).
3. Fixes: Codemasters rad/s with the per-car unit test, DiRT reverse, Forza
   neutral, car keys with the game and adoption helper (store side in B).
4. Forza sled/dash additions; Codemasters additions; OutGauge `game`.
5. EA SPORTS WRC: `data/telemetry/eawrc/oversteer.json`, `EAWRC_TYPES`,
   4CC decoder, default 237-byte decoder, placed before the catch-all.
6. Bridge v3 (C, per-game views, `current_max_rpm`, track length) and its
   decoder; rebuild the `.exe` if a toolchain is present.
7. `Telemetry.handle()` refactor; `telemetry_capture.py`; capture and
   replay scripts; `tests/sim.py` with encoders; tests.
8. D1 port default 5310 (model default, `oversteer-run`, bridge usage text,
   status hint), CHANGELOG entry.

**Step B: database v2, sessions and runs, learners.**
1. `telemetry_store.py`: schema v2, migration v1 → v2 (with backup and
   Codemasters rescale), writer and reader APIs; tests.
2. `drive_log.py`: queue, worker thread, sessions with `SESSION_GAP`, runs,
   segments, corners, trace, capture writing; `ShiftLearner` DB access
   moved here.
3. Shift learner changes 1–7 (§8.1), 8 if time allows; shifts with method,
   downshifts and flags (§8.2).
4. Tunes and re-tune rules (§8.3).
5. `drive_detect.py`: `Verdict`, discipline tiers 1–5, surface features,
   limit gate, classifier, reject rule, calibration and `calibrate` script;
   stage priors files. Every game starts uncalibrated (answers unknown).
6. Tests for all of it; bench.

**Step C: coaching, tuning advice, web.**
1. `coach.py`: metrics per run, series, habits, growth, coach_state, tips.
2. `tuning.py`: the rules of §10.
3. `telemetry_web.py` and `data/telemetry/web/index.html`; meson install.
4. Tests (coach, tuning, web).

**Step D: GTK tab, preferences, docs.**
1. `telemetry_view.py` and the tab layout of §12; label dialog; settings
   expander; web switch and URL; capture switch; EA WRC setup lines.
2. App preferences (`telemetry_listen`, `telemetry_web*`,
   `telemetry_capture*`), listener without LEDs (D4).
3. Shift-press wiring from the combined device (§8.2).
4. README section, CHANGELOG, this document; tests for the view functions.

## 15. Deferred and open

- Automatic splitting of colliding DiRT cars and BeamNG cars (§5.4): needs
  real data; the user splits by hand meanwhile.
- AC1 UDP remote telemetry (port 9996): the bridge covers AC1 already.
- EA WRC v1.8 channels: after a capture confirms them.
- Writing EA WRC's `config.json` for the user: the tab shows the lines
  instead.
- Learning `BOOST_HOLD` per car and fitting drag per car (§8.1 items 3, 8)
  if Step B runs short.
- A QR code for the web URL (no stdlib encoder; would need ~300 lines).
- Every **calibrate** threshold stays a default until labelled captures
  exist; the first calibration pass needs the captures of §6.3.

## 16. Progress

### Step A
- [ ] `telemetry_formats.py` split, `Telemetry.handle()`
- [ ] `Sample` v2 fields and conventions
- [ ] Codemasters rad/s (per-car unit decision), DiRT reverse, Forza neutral
- [ ] Car keys `<game>/<id>`, stage keys
- [ ] Forza sled/dash dynamics decoded
- [ ] Codemasters dynamics decoded
- [ ] EA SPORTS WRC structure file and decoder (4CC and default 237 B)
- [ ] OVST v3 bridge (C) and decoder; `.exe` rebuilt
- [ ] BeamNG MotionSim (optional)
- [ ] Capture format, writer, reader; capture and replay scripts
- [ ] `tests/sim.py` with per-format encoders; decoder tests
- [ ] UDP default port 5310 (D1)

### Step B
- [ ] Schema v2, WAL, reader/writer APIs
- [ ] Migration v1 → v2 with backup and Codemasters rescale
- [ ] Drive-log thread; sessions (`SESSION_GAP`), runs, segments, corners, trace
- [ ] Shift learner: per-gear power, slope-free accel, gates, P75, H-pattern window, limiter precedence, confidence band
- [ ] Drag fit per car (optional)
- [ ] Shifts: method, downshifts, flags
- [ ] Tunes and re-tune rules
- [ ] Discipline tiers with `Verdict` and evidence
- [ ] Surface features, limit gate, classifier, reject rule
- [ ] Labels, calibration, `telemetry-calibrate.py`; stage priors
- [ ] Bench: per-packet cost within budget

### Step C
- [ ] Metrics per run
- [ ] Habits, growth, rate limiting, phrasing
- [ ] Tuning advice rules
- [ ] Web server (read-only, Host check, limits) and page
- [ ] Tests for coach, tuning, web

### Step D
- [ ] Telemetry tab layout and view functions
- [ ] Label dialog
- [ ] Preferences: learn without LEDs, web, capture
- [ ] Shift-press wiring (sequential vs paddles)
- [ ] README, CHANGELOG, this document

## 17. Appendix: format research findings

Checked against `telemetry.py` and the bridge at 8bcd348. Confidence: H =
official documentation or proven from the numbers, M = several independent
community decoders, L = one source or inference.

| # | Finding | Conf. |
|---|---|---|
| 1 | FH6's doc says to avoid UDP 5200–5300 (its own outgoing socket); 5300 can collide | H |
| 2 | DiRT Rally 1/2 engine rate, max and idle (37, 63, 64) are rad/s, not rpm/10: ×10 overstates rpm by 4.72 % | H |
| 3 | WRC Generations copies the layout; g-forces probably m/s², reverse = 10, rpm unit unverified | M/L |
| 4 | Forza gear 11 = neutral (decoded as None today), 0 = reverse | M |
| 5 | Codemasters reverse = 10 (some titles negative), decoded as None today | M |
| 6 | DiRT car keys (max, idle, gears) collide: 82 cars, 66 keys | H |
| 7 | EA SPORTS WRC's default `wrc` packet is 237 bytes and not decoded | H |
| 8 | The Codemasters catch-all would take any 4-aligned 256–512-byte packet, EA custom ones included | M |
| 9 | BeamNG OutGauge always sends car `"beam"`: every BeamNG car shares one learnt car | H |
| 10 | Bridge `maxRpm` (static 412) may be missing in ACR; physics `currentMaxRpm` 588 (int32) exists in ACC/ACR | M |
| 11 | ACR's static `track` is often empty; `trackSplineLength` (520) identifies the stage | M/L |
| 12 | Bridge `gear - 1` is right for AC, ACC and ACR | H/M-H |
| 13 | Bridge views (64/416 B) must grow for new fields: physics 580 (AC1) / 800 (ACC/ACR), static 684 / 820 | M |
| 14 | The `acpmf` car key does not record which of AC, ACC, ACR it came from | M |
| 15 | The Codemasters car name's "rpm" carries the unit error of #2 | H |

Sources: FH6 and FM Data Out articles (support.forza.net), EA WRC's own
`readme.txt`/`channels.json`, the AC and ACC shared-memory PDFs, dr2_logger
(MIT), vAzhureRacingHub, pithsim, theRTB/ForzaShiftTone,
albertowd/live-telemetry-evo, nobonobo/ac-telemetry, the BeamNG protocols
documentation.
