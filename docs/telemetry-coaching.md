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

Revised after an independent review (decisions in §3.2): the build plan
(§14) is ordered as vertical slices, so each step ships something the user
can see even if the run stops there, and the parts that need calibration
data nobody has yet (measured surface, dynamics-based discipline) come
last.

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
- **Priors are not evidence; the game's own word is.** A route the game
  identifies on a single-surface location (EA WRC Sweden, DiRT's Wales) is
  game evidence, like an EA session packet is for discipline. "Usually
  gravel here" learnt from past runs, or a table entry for a mixed
  location, is shown next to what was measured, never merged into it.
- **The listener never waits.** The UDP thread does constant-time work per
  packet and never touches SQLite (§4).
- **Keep the raw data.** Raw captures and a compact per-run trace let every
  detector be re-run when its calibration improves.
- **Standard library only.** sqlite3, http.server, json, zlib, gzip,
  struct, threading, queue, statistics, array.

## 3. Decisions

### 3.1 To confirm with the user

These are decided here so the work can go on; each is cheap to reverse.

- **D1. UDP default port 5300 → 5310.** The FH6 Data Out documentation says
  to avoid 5200–5300 because the game binds its own outgoing socket in that
  range; FH6 runs on the same host, so 5300 can collide. New profiles and
  profiles that never set a port get 5310; a profile that stored 5300 keeps
  it, and the "port in use" status then suggests 5310. `oversteer-run` and
  the bridge default follow (`OVERSTEER_TELEMETRY_PORT`, `--port`). Games
  already set up for 5300 would go quiet without a hint, so when a profile
  on 5310 has received nothing for 10 s the listener binds 5300 for one
  second (only if it is free); if packets arrive there the status says
  "your game sends to 5300: set it to 5310", otherwise "waiting for
  telemetry on 5310 (games set up for 5300 need the new port)". It does not
  keep listening on 5300: that is the socket FH6 may need. *As built:* the
  probe is symmetric (`Telemetry.other_port`): a profile that stored 5300
  probes 5310, because `oversteer-run` now sends there; it runs 10 s after
  start and then every 60 s until anything is heard. The bridge's own
  compiled-in default stays 5300 until the .exe is rebuilt (Step D):
  `oversteer-run` always passes `--port`, so only a bridge run by hand
  sees it. The web
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

### 3.2 Design decisions

Responses to the independent review of 2026-09-25 (numbers are the review's
findings). Accepted unless marked; the section named carries the change.

| # | Finding | Decision |
|---|---|---|
| 1 | Ratios learnt under wheelspin are wrong forever and later fire a false re-tune | Accepted. Ratios come from part-throttle, no brake, clutch-out samples, or from rpm ÷ driven-wheel speed where sent; full-throttle samples feed power only (§8.3). Test "spin first on gravel". |
| 2 | Neutral-between-gears misidentifies the shifter; `shift_press` is read but never written | Accepted. Method from the evdev code of the last press (gear button, sequential plate, paddle) via the combined spec; neutral only as fallback (§8.2). Moved to Step A. |
| 3 | Forget cascades into runs and labels | Accepted. Forget resets the model and tunes and keeps sessions, runs and labels (§7.4, §12). |
| 4 | Counter-steer inverted under the stated sign conventions | Accepted. `steer` is positive left like `yaw_rate` (ISO 8855); counter-steer = opposite signs, tested on a simulated left-hand corner (§5.2, §8.4). |
| 5 | Surface unknown until 6+ labelled stages per game; game-named routes demoted to priors | Accepted. Surface tier 1 is `game` from a route table for single-surface locations; the measured classifier waits for the §6.3 captures, segment features are stored from the start (§8.6). |
| 6 | Discipline dynamics tier (β percentiles) cannot be calibrated and is not needed | Accepted. Tier dropped until captures exist; the profile name is shown as an evidence line and breaks ties at `low` (§8.5). |
| 7 | Codemasters migration tests the launch limiter, not the game's max; decoder lacks a true-rpm branch | Accepted. Migration parses the max from the key; decoder gets a third branch (§5.3, §7.3). |
| 8 | EA WRC setup cannot read the game prefix from the Flatpak | Accepted: copy buttons with target paths and a shipped id → name table (§5.3). Manifest filesystem access rejected: a narrow sandbox is worth more than automatic names. |
| 9 | Bridge v3 cannot be built here; `OVST3_SIZE` missing from the size check | Accepted. Build with `ziglang` from a scratch venv (a build tool, not shipped); v3 moves to Step D and stays marked unbuilt if that fails (§5.3). |
| 10 | Plan front-loads decoding fidelity; nothing visible if cut off after Step A | Accepted. The four steps are re-cut as vertical slices (§14); `tests/sim.py` limited to what tests use. |
| 11 | Trace blobs in `runs`; `stages` FK order; metrics slicing; tunes keyed two ways | Accepted. `traces` table, `start_run` upserts the stage first, `metrics` carries discipline/surface/method with a `(session, name)` index, tunes are one ratio set per car (§7.2, §8.3). |
| 12 | Rounded DiRT stage keys split a stage at a boundary | Accepted. Tolerance match against known stages first, rounded key only for new ones (§5.4). |
| 13 | Two sources at once thrash the learner | Accepted. The listener locks to the first (address, format) until idle (§4). |
| 14 | Coaching that nags or misleads | Accepted: "if the setup allows" with a driving alternative, top-gear limiter gated per stage, growth weighted by count with ≥ 20 events a side, at most 3 "still:" lines (§9.2, §10). |
| 15 | Threading and performance details | Accepted: new dict then assign; history queried on events, not every second; snapshot cached by `updated`; bootstrap caches band medians; web server daemon threads and a connection per request; `EOFError` on truncated captures (§4, §6.1, §8.1, §11). Moving SQLite writes off the listener lock is the first commit of Step C. |
| 16 | Web LAN exposure hardening | Accepted: list every bound address, security headers, no source IP in status, IPv6 Host literals, "plain HTTP" note (§11). |
| 17 | Port move silently breaks configured games | Accepted with a one-second probe of 5300 and a hint (§3.1 D1). Listening on both ports rejected: 5300 is the port FH6 may need for itself. |
| 18 | Smaller gaps | Accepted: "not sent by this game" cells, bottoming depends on unverified units, the learner's 40-sample memory stated and `top_seen` reset on a re-tune, README fixed in Step A, tests for OutGauge reverse and Forza `IsRaceOn = 0` (§8.1, §10, §12, §13). |

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
- **Source lock.** The listener takes the first (address, format) it sees
  and ignores other sources until `IDLE_TIMEOUT` passes without a packet
  from it, logging each ignored source once. A stale bridge next to a game,
  or a replay sent while a game runs, would otherwise alternate car keys
  per packet and make the learner end and start a session on each.
- **Drive-log thread** (new, `oversteer/drive_log.py`). Consumes events:
  shift, launch, run start/end, segment closed (≈ every 200 m), sample at
  10 Hz for the trace, raw packet for the capture. Computes segment features,
  corners, run metrics, verdicts, coaching; writes SQLite in batches (commit
  at most every 5 s and at every run end). Publishes snapshots by building
  a new dict and then assigning it to an attribute (atomic in CPython); a
  published dict is never mutated, so readers take no lock.
  Expensive work (bootstrap confidence bands, calibration) runs here, never
  on the listener.
- **Readers.** The GTK timer (1 s) and the web handlers read the published
  snapshots and no longer call `snapshot()`/`advice()` under the learner
  lock (6 ms a call today). History queries use read-only connections
  (`sqlite3.connect('file:...?mode=ro', uri=True)`), which WAL lets run
  alongside the writer, and run only on session end, car change or when
  the tab is shown, never on the 1 s timer. Until the drive-log thread
  exists (Step C), the tab caches `load_snapshot()` of a saved car by its
  `updated` time instead of re-parsing the model every second.
- **Locks.** `ShiftLearner.lock` guards only the in-memory `CarModel` (feed
  vs. snapshot); it is never held across I/O. Today's per-shift INSERT and
  the 20 s save under that lock (which can stall LED updates on a busy
  disk) move to the drive-log thread; that is the first commit of Step C.
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
  `yaw_rate` positive turning left, and `steer` **positive left** (ISO
  8855: positive steer is counter-clockwise). In a steady left turn both
  are positive; counter-steer is steer and yaw rate of opposite sign. Each
  decoder converts its game's sign, **verified by capture** (a left turn
  gives steer > 0 and yaw rate > 0).
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
| Inputs | `brake, handbrake, steer` (+1 full left .. -1 full right, normalised; lock unknown) |
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
  WRCG's unit is unverified [3], the decoder decides per car from the raw
  max `m = floats[63]`, in this order: if `m × 30/π` lies within 1 rpm of a
  multiple of 50, rad/s; else if `m` itself is ≥ 3000, below `RPM_LIMIT`
  and within 1 of a multiple of 50, true rpm (without this branch a WRCG
  car in real rpm would be scaled × 9.55, fail `_plausible` and lose every
  packet); else if `m × 10` is a round figure, rpm/10; else rad/s. The
  decision is cached per (game, raw max) and logged once.
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
  The Flatpak cannot read the game's Proton prefix (the manifest grants no
  home access, and this design does not add it), so names come from
  **`data/telemetry/eawrc/ids.json`**, a small id → name table for vehicle
  classes, locations and routes built from the game's
  `readme/ids.json` (names only, with attribution; check the readme's
  terms before shipping it, and ship numbers if they forbid it). Vehicle
  names are left to the user's rename; unknown ids show as numbers.
- Setup help (in the tab, Step A): **"Copy structure JSON"** and **"Copy
  config lines"** buttons, like the existing launch-options Copy, next to
  the exact target paths inside the prefix
  (`…/steamapps/compatdata/1849250/pfx/drive_c/users/steamuser/Documents/My
  Games/WRC/telemetry/udp/oversteer.json` and `…/telemetry/config.json`).
  The config lines are `structure: "oversteer"`, each `session_*` packet,
  `ip: "127.0.0.1"`, `port: 5310`, `frequencyHz: 60`, enabled. **Verify the
  enable key's exact name** in the generated file (EA's readme says
  `enabled`; community notes say `bEnabled`). Oversteer does not edit the
  game's files.

**OutGauge (LFS layout; BeamNG).**
- Unchanged decoding. Fix [9]: BeamNG always sends `"beam"`, so
  `game = 'beamng'`, `car = 'beamng/unknown'`, and car identity comes from
  the fingerprint in §5.4. LFS: `game = 'lfs'`, car from `Car[4]`.
- Optional (Step D, low priority): BeamNG MotionSim `BNG1` packets on the
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
  (NaN until then). The `game` byte comes from the executable name that
  `oversteer-run` passes to `--watch`, falling back to `smVersion`. Gear
  stays `gear - 1` [12]; verify reverse logs -1 once in ACR. ACR
  temperatures are Kelvin (not forwarded now).
- `decode_sample` accepts `OVST3_SIZE` next to `OVST_SIZE` and
  `OVST2_SIZE` in its length check (it matches on the size set today), and
  checks the version byte against the length as it does for v2.
- **Build.** Neither `x86_64-w64-mingw32-gcc` nor `zig` is installed here.
  `scripts/build-shm-bridge.sh` is run with `zig` from `pip install ziglang`
  in a scratch venv (a build tool, never an app dependency). If that fails,
  the C change is not committed half-built: v3 stays unchecked in §16 with
  "bridge v3 unbuilt" and the reason. Because every ACR-specific feature
  (`mu_wheel`, brake bias, suspension travel) depends on v3, v3 sits in
  Step D next to the measured detectors that use it.

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
- `stage` key: `eawrc:<loc>:<route>`, `fm:<track ordinal>`,
  `ac:<track>:<config>`, `acc:<track>` are exact. Keys built from measured
  numbers are **matched with a tolerance first** so a value on a rounding
  boundary does not split a stage: DiRT's (length, start z) against the
  shipped stage table (±2 m, ±15 m), then against stages already in the
  database; ACR's track length against known `acr:` stages (±10 m); a
  start cell against its neighbouring cells (§8.4). Only a stage matching
  nothing gets a new rounded key (`dirt:<round(length)>:<round(start z,
  -1)>`, `acr:<round(length, -1)>`). Forza Horizon and BeamNG have no stage
  (§8.4 builds a start-cell key for repeated routes).

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
  truncated tail (a crash mid-write): `EOFError` and a short record end
  the iterator quietly.
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
- Version in `PRAGMA user_version`: 0 with a `cars` table = v1 (today), 1 =
  v1 with the Codemasters rescale done (Step A, §7.3 step 2), 2 = this
  design. Migrations run in one transaction at open; the old file is
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
    detector_version INTEGER        -- which calibration produced the verdicts
);
CREATE TABLE traces (               -- kept apart so list queries on runs never page through blobs
    run INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    data BLOB NOT NULL              -- zlib(array('f')) at 10 Hz, see TRACE_CHANNELS
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
    gear INTEGER, method TEXT,
    discipline TEXT, surface TEXT   -- copied from the run; rewritten by "Re-check old runs"
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
CREATE INDEX metrics_name ON metrics (name, discipline, surface, session);
CREATE INDEX metrics_session ON metrics (session, name);
CREATE INDEX segments_run ON segments (run);
CREATE INDEX corners_run ON corners (run);
```

`TRACE_CHANNELS = ('t', 'distance', 'speed', 'rpm', 'gear', 'throttle',
'brake', 'clutch', 'handbrake', 'steer', 'a_long', 'a_lat', 'yaw_rate',
'slip_drive', 'susp_rms')`, NaN where not sent. Traces are capped at 200 MB
in total (**preference**); the oldest `traces` rows are dropped first, their
runs, metrics and verdicts stay.

`runs.stage` and `sessions.stage` reference `stages(key)` with
`foreign_keys=ON`, so `start_run` upserts the stage row first, in the same
transaction, then inserts the run.

### 7.3 Migration v1 → v2

In one transaction, after the backup copy:

1. `cars`: create the new table, copy rows: `game` from the old key prefix
   (`forza-` → `forza`, `codemasters-` → `codemasters`, `acpmf-` →
   `acpmf`, `outgauge-beam` → `beamng`, other `outgauge-` → `lfs`), new key
   `<game>/<rest>`; the adoption rule (§5.4) later moves `forza/…` to
   `forza-fh/…` and so on.
2. **Codemasters units (D2).** For each `codemasters` car, take the game's
   max from the key (`codemasters-<max>-<idle>-<gears>`, where max was
   `floats[63] × 10` rounded), not the stored limiter: that one is often a
   launch-learnt rpm bouncing on the limiter (7215) and never round. If
   `key_max / 10 × 30/π` is within 2 rpm of a multiple of 50, the car was
   recorded in rad/s × 10; rescale by `f = 3/π` (0.95493) the
   model's `limiter, top_seen`, every `upshifts` value, every `ratios`
   value, and re-bin `power` bands (the band's middle × f, merging lists); rescale that
   car's `shifts.rpm` and `shifts.best`; rebuild the key with the corrected
   rpm rounded to 10. Otherwise leave the car as it is and log it (a WRCG
   car whose unit was right).
   Step A ships the decoder fix on schema v1, so this rescale runs there
   first as a one-off on the v1 tables (renaming the key in `cars` and in
   `sessions.car`, then `PRAGMA user_version = 1`) and is skipped here when
   already done.
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
  `forget_model` (resets `cars.model` and deletes the car's tunes; sessions,
  runs, labels and metrics stay, because labels are the only calibration
  ground truth and weeks of them should not go with one mis-learnt car),
  `start_session`, `end_session`, `start_run` (upserts the stage first),
  `end_run`, `add_segment`,
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
   computed on the drive-log thread at most every 10 s per car. Each
   resample computes its per-band estimates once and interpolates from
   them, rather than calling `power_at` (which re-medians 40 samples) for
   every rpm step of the crossover search.
8. **Drag fit** (optional, Step D): fit C0, C2 per car by minimising the
   disagreement of `P_gear` between gears in their overlapping rpm range.
   Validated on Forza captures, where the game's power figure is truth.
9. `CarModel.to_dict()` gains `version: 2`, `power_g`, `limiter_source`,
   `boost_hold`, `drag`.
10. **Memory, by design.** `SHIFTS_KEEP` and `POWER_KEEP` keep the 40
   most recent samples per band and gear, so the model follows the car as
   it is now and forgets older data; that is what lets a re-tune relearn.
   The tab says "learnt from your recent driving". A re-tune also resets
   `top_seen` for the changed gears.

Advice sentences stay in `CarModel.advice` style but move to the coach
(§9), which knows method, surface and history.

### 8.2 Shifts and the shifter

- **Method** per shift comes from the **physical input**, because whether
  telemetry shows neutral between gears depends on the game's transmission
  model, not on the shifter (a sequential car in DR2 driven on the H-gate
  mapped to gear buttons never shows neutral; an H-pattern car in AC
  driven on paddles does). In `GtkController.process_events`, every EV_KEY
  press is classified with the loaded combined spec
  (`_load_combined_spec()`): a code in the wheel's gear codes
  (`G29_GEAR_CODES`, where the shifter's gears land) → `'gear'`, the
  sequential plate's buttons (`KNOWN_SHIFTERS` indexes 8, 9) →
  `'sequential'`, the paddles (292/293) → `'paddle'`; it sets
  `launch_inputs['shift_press'] = (monotonic, kind)`, which `telemetry.py`
  already passes to the learner. A shift with a press within 0.6 s takes
  its method from it (gear → `h-pattern`, sequential → `sequential`,
  paddle → `paddles`); without one, `auto` when the game says (AC
  `autoShifterOn`), else `h-pattern` when neutral was seen between gears
  (fallback only), else NULL. About 30 lines; in Step A because every
  per-method metric depends on it.
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

- **Learning a ratio** (rpm per m/s per gear) uses only samples where
  slip is near zero: throttle < 0.5, brake = 0, clutch out, steady speed.
  Full-throttle samples feed the power curve only, and only when they sit
  on the learnt ratio. Today every full-throttle sample is taken while a
  gear has no ratio yet (`known is None` in `_feed_locked`): 400 samples
  of 2nd at a steady 6 % slip on gravel learn 349.8 instead of 330.0,
  clean samples are then rejected as off-ratio for good, and the first
  steady part-throttle samples fire `_check_retune` and wipe the gear. A
  6 % ratio error moves the crossover by ~400 rpm at 7000, more than the
  ±200 rpm "spot on" window. Where the game sends driven-wheel speed (DiRT
  25–28, EA `vehicle_transmission_speed` / contact-patch speed, Forza
  wheel rotation × tyre radius, ACR via bridge v3), the ratio is rpm ÷
  driven-wheel speed, which spin cannot distort.
- **Re-tune rules**: a new ratio is accepted only in the first 60 s or
  2 km of a session (setups change in menus, which end sessions in every
  supported game), from the same low-slip samples, brake = 0, spanning ≥ 2
  speed bins 5 m/s wide; a shorter ratio (higher rpm/m/s, what spin also
  produces) needs twice the samples.
- **Tune classification**: all gears changed by the same factor ±1 % →
  `final-drive`; some gears only → `gears:<list>`. A new tune row opens a
  new setup epoch; its ratios become the car's current ones, and the shift
  points of the changed gears are relearnt (as today).
- **Measured per tune**: tyre radius (speed / undriven-wheel rad/s: Forza,
  AC static), ride height (AC1/ACC), brake bias (ACC/ACR), resting hub
  positions at rest on flat ground (EA WRC, as a ride-height *change* only,
  low confidence). The tune shows these; nothing is inferred beyond them.
  None of the user's rally games sends ride height (ACR does not publish
  it; EA WRC, WRCG and DiRT have none): the tab says "not sent by this
  game" instead of an empty cell.
- A tune is one ratio set per car, nothing more. "On this stage you ran
  the 3.9 final drive last time" comes from `sessions.tune` of the last
  session on that stage.

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
  opposite to yaw rate (both positive left, §5.2; a test drives a simulated
  left-hand corner with and without a slide).
- **Trace** at 10 Hz (§7.2).
- **Start-cell stage key** for games that name no stage (Forza Horizon,
  BeamNG, AC practice): `cell:<game>:<x/50>:<z/50>:<heading/45°>` of the
  run's start, completed by the run length rounded to 100 m once it ends.
  Used only to compare runs of the same route and to build priors; two
  routes from the same start stay apart by length. A new start is matched
  against the neighbouring cells and headings first (§5.4).

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
4. **Dynamics**: dropped until labelled captures exist (§3.2 #6). Its
   body-slip thresholds could not be calibrated, β needs a car-frame
   velocity BeamNG does not send, and for the user's games tiers 1–3 and 5
   already decide: `game` settles EA WRC, WRCG, DiRT and ACR (DR2 laps > 1
   → rallycross), ACC and FM; topology and the start signature handle FH6.
5. **Start signature**: a launch hold then one continuous 2–15 min run with
   no stops → stage; a rolling start into loops → time attack.

The **Oversteer profile** is always an evidence line ("profile 'rally'"),
since the user keeps a rally profile and a circuit one. It is a name, not
a measurement: it decides only when no tier fired, at `low`, and only when
the name contains a class word (rally, circuit, drift, hillclimb).

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

Tiers, as for discipline:

1. **Game** (confidence `game`, Step C): the stage key names a route on a
   single-surface location in the shipped route table
   (`data/telemetry/stages/*.json`): EA WRC Sweden (snow), Croatia, Japan,
   Iberia, Mediterraneo (tarmac) and the gravel locations; DiRT Rally 2.0
   locations from dr2_logger's tables (MIT, with its notice). **Confirm per
   route** against the game where a location has both. Mixed locations
   (Monte-Carlo, Central Europe) fall through. Games that send the
   surface outright (ACC rain, AC `surfaceGrip`, Forza puddles for wet)
   decide here too.
2. **Measured** (Step D, after the §6.3 captures): the classifier below.
   Until it is deployed for a game, only the segment features are
   computed and stored (cheap), so it can be trained on past runs later.

A learnt prior ("usually gravel here", from past runs of a start cell or a
mixed location) stays separate from both and never decides.

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

**Route table and priors** (`data/telemetry/stages/*.json`, loaded into
`stages`): per route, the surface when the location has one (game
evidence, tier 1) or `mixed:<list>` (a prior only). The tab shows "gravel
(game: Wales)" or "measured gravel (high) · usually gravel here"; a
measured answer disagreeing with the table is highlighted for the user to
label.

**Calibration** (`calibrate(game) -> CalibrationResult`, Step D): from
labelled runs (`labels`) and their segments. Needs ≥ 3 labelled runs per class and
≥ 2 classes; leave-one-run-out segment accuracy ≥ 0.90 (**calibrate**) to
deploy. Undeployed or missing → the measured tier answers unknown with
"not calibrated for <game> yet: label a few stages of each surface" (or the
current holdout figure); the game tier still answers where it can. Only calibrated classes can be answered; the reject
rule keeps an uncalibrated surface from being forced into a known one. Each
recalibration bumps `version`; runs keep their `detector_version`, and
"Re-check old runs" re-classifies from stored segments.

Labels come from the tab (Step C) and the web page never writes them.

## 9. Coaching engine (`oversteer/coach.py`)

### 9.1 Metrics (per run, stored in `metrics`)

| Name | Unit | Counted when | Weight / gate |
|---|---|---|---|
| `shift.error` (per gear, per method) | rpm, signed vs best | flat-out upshifts with a known best | full on tarmac/circuit; low on gravel/snow (traction-limited, short-shifting can be right); silent when surface unknown and the car is traction-limited (slip at the shift > κ₀); with the game tier (§8.6) that is rare in the user's rally games |
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
- **Growth**: the most recent sessions against the ones before, each side
  pooled by `count` (a 3-minute stage and an hour of free roam weigh by
  their events, not as one session each) and holding ≥ 20 counted events;
  reported only past an effect size per metric
  (**calibrate**; starting values: shift error median moved > 150 rpm,
  limiter s/km halved, neutral time −20 %).
- **Habit**: a metric beyond its threshold in ≥ 70 % of ≥ 5 sessions in a
  slice. One **focus** habit at a time: the one with the largest estimated
  time cost (shift error × drive lost, limiter time), ties broken by count.
- **Rate limiting** (`coach_state`): at most 3 tips and 1 praise per view;
  a tip is re-shown when its value got worse by ≥ 20 %, after 14 days, or
  when the user asks ("Show all"); after two showings without change it
  becomes a quiet "still:" line at the bottom, at most 3 of those, oldest
  dropped. The same limits apply to praise and to "Still learning the
  engine"; today's per-gear "spot on" on every refresh goes.
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
| Final drive too short | `limiter.top` > 1 s per run on ≥ 3 runs of the same stage (a Rally2 on one long tarmac straight is normal) | lengthen final drive / top gear, if the setup allows |
| Final drive too long | top gear used > 5 % of the time but never above 85 % of the limiter, on ≥ 3 runs of the same stage | shorten final drive, if the setup allows |
| A gear too long for the stage | rpm 1 s after throttle reapplication after corners in gear n below the power band on > 50 % of exits | shorten gear n, or use n−1 there |
| Bottoming | travel saturating (Forza normalised 1.0, AC `suspensionMaxTravel`, DiRT/EA saturation) per km, split into landings (vertical accel spike) and compressions; for DiRT and WRCG only once their suspension units are verified by capture | ride height up / springs or bump stiffer |
| Spring/damper balance | travel histogram piled at one end; > 1.5 oscillations after a hit | softer/stiffer; more damping |
| Understeer/oversteer | sign and change between tunes of the steer-vs-a_lat gradient (arbitrary units: steering lock unknown); counter-steer fraction per surface | rear/front anti-roll bar, springs, differential |
| Open differential | inside-wheel spin on > 30 % of hairpin exits | lock the differential more |
| Brake balance | front vs rear locking under braking (wheel speeds) | brake bias |

Gearing is class-limited or fixed for many cars in EA WRC and WRCG, so
every setup lever is phrased "if the setup allows", and where a driving
alternative exists it comes first ("use n−1 there", "lift before the
limiter in top").

Driver-fitted: a counter-steering driver in a car that measures oversteer on
tarmac gets the setup hint; a neutral car with a sliding driver gets the
driving hint. Tuning advice needs ≥ 3 runs on the tune, otherwise it says
what it is still collecting.

## 11. Web page (`oversteer/telemetry_web.py`)

- `TelemetryWeb(port=5301, bind='0.0.0.0', live=callable, reader_path=...,
  profile=callable)`: a `ThreadingHTTPServer` subclass with
  `daemon_threads = True`, served from a daemon thread; `start()` returns
  False with the error when the port is taken; `stop()` shuts it down.
  Each request opens its own read-only connection and closes it (a
  per-thread cache would leak with one thread per request).
- **Step B** serves what exists today: the live sample, the learnt cars
  and shift tables (`ShiftLearner.snapshot`/`load_snapshot`) and the
  per-car history (`ShiftLearner.history`). Sessions, verdicts and coaching
  endpoints are added in Step C when the data behind them exists.
- **Off by default.** App preferences (`config.ini` DEFAULT section):
  `telemetry_web` (0/1), `telemetry_web_port` (5301), `telemetry_web_bind`
  (`lan` → 0.0.0.0, `local` → 127.0.0.1).
- **Read-only**: only GET and HEAD; everything else 405. No cookies, no
  CORS headers (so pages on other origins cannot read the JSON with fetch),
  at most 8 requests in flight (503 beyond), 10 s socket timeout, request
  logging at debug level only. Every response carries
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src
  'self'` (the page's inline CSS/JS gets a hash or `'unsafe-inline'` for
  style and script; nothing external either way) and `Referrer-Policy:
  no-referrer`.
- **Host check** against DNS rebinding: requests whose `Host` is not an IP
  literal (IPv4, or IPv6 in brackets like `[fe80::1]:5301`), `localhost`,
  the machine's hostname or `<hostname>.local` get 421.
- Endpoints (JSON, UTF-8, `Cache-Control: no-store`):
  - `GET /` → `index.html`, one self-contained file (inline CSS and JS, no
    external resources) from `data/telemetry/web/`, installed with the other
    telemetry data.
  - `GET /api/v1/status` → version, profile, listening port, game, car,
    session id. Not the telemetry source address: it would tell anyone on
    the network which machine runs the game.
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
- Binding `lan` means every interface, VPNs, Docker bridges and Tailscale
  included, so the tab lists **every address actually bound** as a URL
  (IPv4 addresses from `/proc/net/fib_trie` local entries, loopback left
  out; if that cannot be read, the address found by connecting a UDP
  socket to a TEST-NET address, no packet sent), and says the page is
  plain HTTP: anything on the path can read it.

## 12. GTK Telemetry tab

What shows first is what requirement 1 asks for. Top to bottom:

1. **Car bar**: car picker (as now), rename, forget, "Label last session…".
   Forget's confirmation says what it does: "Relearn this car from
   scratch. Its sessions and your labels are kept."
2. **Context line**: discipline and surface with confidence and prior,
   stage/location, tune, shifter — e.g. "Rally stage (high: point to point,
   9.8 km) · gravel (medium) · usually gravel here · H-pattern". Clicking
   shows the evidence.
3. **Shift table**: Gear | Best (± band) | You | H-pattern | Sequential |
   Paddles | Data; method columns only when they have shifts.
4. **Coaching**: focus habit, up to 3 tips, praise, growth line; "Show all"
   expander with the quiet lines.
5. **Tuning**: current tune (ratios, change type, measured ride height /
   tyre radius / brake bias; "not sent by this game" where the game has
   none), up to 2 tuning notes, "This is a different car" on a fingerprint
   split.
6. **Recent sessions** (10): date, stage, discipline/surface badges,
   shift error, limiter s/km.
7. **Live line** (as now).
8. **Settings** expander: the rev light switches (moved from the top),
   "Learn from game telemetry" (D4), UDP port, web page switch, port,
   bind, the bound URLs and the notes of §11, "Record raw telemetry" with
   the capture folder and size, EA WRC setup (copy buttons and target
   paths, §5.3).

The label dialog: discipline, surface, wet, shifter (preset from the
detected ones), note; saved to `labels` for every run of the session.

View code stays thin: `telemetry_view.py` (pure functions building rows and
strings from snapshots) is unit-tested; `gtk_ui.py` only places widgets.

## 13. Testing

- `pytest -q tests` stays green at every commit; no test touches /sys,
  /dev or the GUI.
- **Decoders** (`tests/test_formats.py`): packets built with `struct` per
  layout; regression tests for every research fix (rad/s, the true-rpm and
  rpm/10 branches, Forza gear 11, DiRT gear 10, EA 237 bytes not taken for
  DiRT, EA 4CC structure, OVST v3 with NaN fields and its size accepted,
  v1/v2 still decode), plus two behaviours that are right today but
  untested: OutGauge gear byte 0 decodes to -1, and Forza `IsRaceOn = 0`
  yields no car key (menus never open a session).
- **Simulator** (`tests/sim.py`, Step C): the simulated car now in
  `test_shift_learner.py` moves there and gains a grade profile, a steady
  drive-slip setting and a left-hand corner; `encode(sample, fmt)` exists
  only for formats a test actually drives end to end (added in Step D).
  Surfaces, roughness spectra and driver models wait until measured
  detectors need them.
- **Learner corrections**, each with a test: slope (a hilly stage still
  finds the analytic shift within 100 rpm), slip gate, turbo lag, H-pattern
  window, limiter precedence, re-tune rules (a steady spin on snow is not a
  re-tune), **spin first on gravel** (400 full-throttle samples of 2nd at
  6 % slip before any clean one: the ratio is still learnt within 1 % and
  no re-tune fires), confidence band contains the analytic answer.
- **Shifter**: press classification from a combined spec (gear code,
  sequential plate, paddle) and the method it gives a shift, with the
  neutral fallback only when no press was seen.
- **Listener**: source lock (a second address is ignored until idle), the
  5300 probe hint.
- **Detectors**: honest-unknown tests (no route and no calibration →
  unknown; OutGauge → unknown "no position"); surface game tier from the
  route table (Sweden → snow, Monte-Carlo → falls through); stage key
  tolerance (DiRT start z 104.9 and 105.1 are one stage); counter-steer on
  a simulated left-hand corner; topology on a simulated circuit vs stage.
  Step D adds: cruising on ice → unknown "never near the limit",
  calibration round trip reaching the holdout gate, the reject rule.
- **Store**: v1 → v2 migration from a real v1 file built with today's
  schema, including the Codemasters rescale decided from the key's max
  (with a launch-learnt limiter like 7215 in the model); forget keeps
  sessions and labels; `start_run` on a new stage; WAL reader alongside
  the writer.
- **Coach**: synthetic metric histories → habits, growth, rate limiting,
  gating by discipline/surface/method, phrasing snapshots.
- **Web**: server on port 0, GET each endpoint, POST → 405, bad Host →
  421 (an IPv6 literal Host passes), 9th concurrent request → 503, the
  security headers present, no source address in `/api/v1/status`.
- **Captures**: write/read round trip, truncated tail, replay determinism
  (same capture → same database contents).
- **Recorded captures** (`tests/data/*.ovcap.gz`, when the user provides
  them): decode without unknown packets; the verdicts match the labels, or
  the test documents the known miss.
- `tests/bench_telemetry.py`: per-packet cost (outside pytest).

## 14. Build plan

Four steps, each a vertical slice that ships on its own: if a run stops
after any step, the user has a working feature and this document says what
comes next. Each step ends green, with small commits, and updates the
checklist below.

**Step A: fixes and the shifter, on today's schema v1.**
1. Tab order: shift table first, the rev light switches in a Settings
   expander; README no longer says the rev lights are in the Tools tab.
2. Decoder fixes with regression tests: Forza gear 11 → neutral, DiRT
   reverse, Codemasters rpm units (three-branch rule, §5.3) with the one-off
   rescale of stored Codemasters cars decided from the key's max (§7.3
   step 2); tests for OutGauge reverse and Forza `IsRaceOn = 0`.
3. Ratio learning from low-slip samples only (§8.3); `top_seen` reset on a
   re-tune; the spin-first test.
4. Shift-press wiring in `process_events` from the combined spec; method
   from the press, neutral as fallback (§8.2); per-method columns in the
   shift table (v1 `shifts.method` already exists).
5. `Telemetry.handle(now, data, addr)` refactor (no behaviour change) and
   the source lock (§4).
6. D1: port 5310 with the 5300 probe and hint; `oversteer-run`, bridge
   usage text; CHANGELOG entry.
7. GTK thread: `load_snapshot()` cached by `updated`, history queried on
   car change, session end and tab shown only; praise and "still" lines
   limited (§9.2).
8. EA SPORTS WRC: `telemetry_formats.py` split out of `telemetry.py`
   first (no behaviour change), then the shipped structure file, the 4CC
   and default 237-byte decoders filling today's `Sample` fields plus
   `game` and `stage`, the id → name table, the copy buttons (§5.3).

**Step B: the read-only web page over today's data.**
1. `telemetry_web.py` (§11: limits, Host check, headers, a connection per
   request) serving status, live, cars, shift tables and history from
   today's `ShiftLearner`; `data/telemetry/web/index.html`; meson install.
2. App preferences `telemetry_web*`; the switch, port, bind, the bound
   URLs and the notes in the Settings expander.
3. Tests (§13 Web).

**Step C: database v2, sessions and runs, game-tier verdicts, coaching.**
1. Drive-log thread; the per-shift INSERT and 20 s save leave the
   listener lock (first commit).
2. `telemetry_store.py`: schema v2 (§7.2), migration with backup, writer
   and reader APIs, `forget_model`; car keys `<game>/<id>` with adoption;
   stage keys with tolerance matching.
3. `Sample` v2 fields the runs need (inputs, position, motion, wheel
   speeds) for Forza, Codemasters and EA WRC; sessions (`SESSION_GAP`),
   runs, segment features stored, corners, traces.
4. Shift learner changes 1–7 and 10 (§8.1); shifts with downshifts and
   flags (§8.2); tunes as one ratio set per car and the re-tune rules.
5. `drive_detect.py`: `Verdict`, discipline tiers 1–3 and 5 with the
   profile line; surface game tier from the route table and the game's own
   hints; wet from game hints; unknown otherwise.
6. `coach.py` (metrics with discipline/surface/method, series, habits,
   growth, `coach_state`, tips) and `tuning.py` (§10); the web endpoints
   for sessions and coaching; the tab's context line, coaching, tuning,
   recent sessions, label dialog; learn without LEDs (D4).
7. `tests/sim.py` (§13); tests for all of it; bench.

**Step D: capture and replay, bridge v3, measured detectors.**
1. `telemetry_capture.py`, capture and replay scripts, the capture switch
   and folder size in the tab; per-format encoders in `tests/sim.py` for
   the formats tested end to end.
2. Bridge v3 (C) built with `ziglang` from a scratch venv, and its
   decoder (`OVST3_SIZE`). If it cannot be built, stop and record why.
3. BeamNG MotionSim (optional).
4. Once the §6.3 captures exist: limit gate, measured surface classifier,
   reject rule, calibration and `telemetry-calibrate.py`, "Re-check old
   runs"; revisit the dropped dynamics tier with the same data.
5. Drag fit and `BOOST_HOLD` per car (optional).
6. README section, CHANGELOG, this document.

## 15. Deferred and open

- Automatic splitting of colliding DiRT cars and BeamNG cars (§5.4): needs
  real data; the user splits by hand meanwhile.
- AC1 UDP remote telemetry (port 9996): the bridge covers AC1 already.
- EA WRC v1.8 channels: after a capture confirms them.
- Writing EA WRC's `config.json` for the user: the tab shows the lines
  instead.
- Learning `BOOST_HOLD` per car and fitting drag per car (§8.1 items 3, 8):
  optional, in Step D.
- A QR code for the web URL (no stdlib encoder; would need ~300 lines).
- Every **calibrate** threshold stays a default until labelled captures
  exist; the first calibration pass needs the captures of §6.3.
- The measured surface classifier and the dynamics discipline tier (§8.5
  tier 4): until the captures exist to calibrate them (§3.2 #5, #6).
- Read access to the game's Proton prefix in the Flatpak manifest: not
  planned; copy buttons and a shipped id table instead (§3.2 #8).
- Listening on 5300 and 5310 at once: rejected, 5300 is the port FH6 may
  need for its own socket (§3.2 #17).

## 16. Progress

Design revised after the independent review (§3.2): done.

### Step A
- [ ] Tab order (shift table first, Settings expander); README fixed
- [x] Forza gear 11, DiRT reverse; OutGauge reverse and Forza `IsRaceOn = 0` tests (`tests/test_formats.py`)
- [x] Codemasters rpm units (three branches, `codemasters_unit()`) and rescale of stored cars from the key's max (`rescale_codemasters()`, `user_version` 1, `telemetry.db.v0.bak`)
- [x] Ratios from low-slip samples (throttle < 0.5, brake ≤ 0.02 or a little throttle when no brake is sent, |a| ≤ 2 m/s²); `top_seen` reset on re-tune; spin-first test. Driven-wheel speed as the ratio source waits for `Sample` v2 wheel speeds (Step C)
- [x] Shift-press wiring (`shift_button_kinds()` in `proxy/equipment.py`, `update_shift_buttons()` every 5 s with the handbrake check) and method from the press (`shift_method()`); per-method columns from `ShiftLearner.method_shifts()`, re-read on car change, session end (`sessions_ended`) and tab shown
- [x] `Telemetry.handle()` refactor; source lock (locked to (address, `Sample.game`); `Sample` gains `game`, `brake`, `stage` now)
- [x] UDP default port 5310 (D1) with the probe and hint (symmetric, see §3.1 D1); CHANGELOG entries for Step A. Bridge .c default left at 5300 until it is rebuilt
- [ ] GTK: snapshot cache, history on events only, praise/"still" limits
- [ ] `telemetry_formats.py` split (done, acb5ba0); EA SPORTS WRC decoder, id table, copy buttons

### Step B
- [ ] Web server (read-only, limits, Host check, headers) over today's data, and page
- [ ] Web preferences and Settings controls, bound URLs
- [ ] Web tests

### Step C
- [ ] Drive-log thread; SQLite writes off the listener lock
- [ ] Schema v2, migration with backup, reader/writer APIs, `forget_model`
- [ ] Car keys `<game>/<id>` with adoption; stage keys with tolerance
- [ ] `Sample` v2 fields for Forza, Codemasters, EA WRC
- [ ] Sessions, runs, segment features, corners, traces
- [ ] Shift learner §8.1 items 1–7, 10; shifts with downshifts and flags
- [ ] Tunes and re-tune rules
- [ ] Discipline tiers 1–3, 5 and the profile line; surface and wet game tier
- [ ] Coach metrics, habits, growth, rate limiting; tuning rules
- [ ] Web endpoints for sessions and coaching; tab sections, label dialog; learn without LEDs (D4)
- [ ] `tests/sim.py`; tests; bench within budget

### Step D
- [ ] Capture format, writer, reader; capture and replay scripts; encoders
- [ ] OVST v3 bridge (C) built, and decoder
- [ ] BeamNG MotionSim (optional)
- [ ] Measured surface classifier, calibration, `telemetry-calibrate.py` (needs §6.3 captures)
- [ ] Drag fit, `BOOST_HOLD` per car (optional)
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
