# DiRT Rally 2.0: feature parity with Assetto Corsa Rally

Audit of 2026-10-26 against master (v0.15.4, `b30bbe2`). Evidence: marth's seven DiRT Rally 2.0 captures of
2026-10-23 (11 finished stage runs, 3 cars, Spain / Poland / Argentina, about 1 h of driving), replayed through
the current code into a scratch database, and a read-only copy of the live database, whose DiRT rows are
identical to the replay's (runs 173–191). Scripts in `research/dr2/` (all read-only):

| script | what it shows |
|---|---|
| `inventory.py` | min / max / distinct count of each of the 66 floats, per capture, and a per-run summary |
| `sectors.py` | every change of the sector, lap, laps-done, progress, last-lap, length and car fields, with the clock |
| `axes.py` | the world's axes, vector directions, signs and units against each other and against motion |
| `replay.py OUT.db` | the DiRT captures through `Telemetry.handle` into a scratch store (as `research/diagnosis`) |
| `outputs.py DB` | what the coach, the splits, the potential and the Run view make of the replayed runs |

Times below are m:ss.s; differences signed seconds.

## 1. What DiRT Rally 2.0 sends (extradata 3, 264 bytes, 66 floats)

The game sends 44–60 packets a second (`hardware_settings_config.xml`: `udp extradata="3"`, port 20777, delay 1).
Status: **dec** decoded correctly, **bug** decoded wrong, **no** not decoded, **–** the game sends nothing there.

| # | field (verified meaning on the captures) | today |
|---|---|---|
| 0 | total time, s (session clock, resets after the results screen) | dec `game_time` |
| 1 | stage clock, s: 0 until the start, runs, and drops to 0 in the packet the car crosses the finish | dec `stage_time` |
| 2 | distance along the stage, m: −20 at the pre-start spot, −2 at the line when the clock starts, = length at the finish | dec `lap_distance` |
| 3 | progress 0..1 (`lap_distance / length`); **2.0 from the finish packet on** (results screen) | dec `progress`, but 2.0 is not understood |
| 4–6 | world position, m. **y is up**; the world is left-handed (x right, z forward), like ACR's | dec `pos`; **bug: plan view mirrored** (see 2) |
| 7 | speed, m/s | dec |
| 8–10 | world velocity, m/s (r 1.000 with d(pos)/dt) | dec `vel` (car frame) |
| 11–13 | "roll" vector: the car's **left** (unit) | dec, used as left: correct |
| 14–16 | "pitch" vector: the car's forward (cos 0.99+ with the velocity) | dec `forward` |
| 17–20 | suspension position RL RR FL FR, mm, compression positive (fronts +19 to +63 mm under braking) | dec `susp` (m) |
| 21–24 | suspension velocity, mm/s | dec `susp_vel` |
| 25–28 | wheel speeds RL RR FL FR, m/s at the tread (free wheels 0.98–1.00 × speed; driven 1.10–1.20 flat out) | dec `wheel_speed` |
| 29–32 | throttle, steer (−1..1, negative left), brake, clutch | dec (steer negated: positive left, verified) |
| 33 | gear (0 neutral, 1..n; 10 reverse) | dec |
| 34 | lateral g, **positive left** (r +0.65..0.82 with speed × heading rate) | dec `accel[1]` (sign now verified) |
| 35 | longitudinal g, positive forward (r 0.83–0.95 with d(speed)/dt; the slope term is inconsistent, treat as kinematic) | dec `accel[0]` |
| 36 | lap: 0 in the stage, **1 from the finish packet** | dec `lap` (not used for the finish) |
| 37 | engine rate, rad/s | dec `rpm` |
| 38 | SLI Pro native support (0/1) | – |
| 39 | car position (0/1 in a stage) | no (`race_position`: worthless in time trial) |
| 40–44 | KERS, KERS max, DRS, TC, ABS | – (always 0) |
| 45, 46 | fuel, capacity | – (always 0; decoder already gives None) |
| 47 | in pits | – |
| 48 | **current split**: 0 → 1 → 2 → 3 at progress 0.25 / 0.50 / 0.75 on stages up to 9.3 km; 0 → 7 at eighths on the 13.2 and 16.6 km stages | **no** |
| 49 | **split 1 time**, s, exact (set the packet the split is crossed, e.g. 0:40.934) | **no** |
| 50 | **split 2 time**, s, exact | **no** |
| 51–54 | brake temperatures RL RR FL FR, °C (40–376 in a run; −273.15 = 0 K outside runs) | dec, **bug: −273.15 kept** |
| 55–58 | tyre pressures | –, **bug: decoded as 0 kPa** (the game sends 0) |
| 59 | laps completed: 0 → 1 at the finish | **no** |
| 60 | total laps: 1 | dec `laps` |
| 61 | stage length, m (exact float, names the stage with the start) | dec `stage_length` |
| 62 | **last lap time = the official stage time**, s, from the finish packet on (e.g. 3:42.784; the clock showed 3:42.78) | **no** |
| 63, 64 | max rpm, idle rpm, rad/s | dec |
| 65 | gear count | dec |

What it does not send: car or stage id/name, yaw rate / angular velocity, tyre data, ride height, weather,
damage, penalties. Derivable from what it sends (owner rule: decode it): yaw/pitch/roll **attitude** and
**angular velocity** from successive forward/left vectors; heading rate from them is better than from positions.

After the finish the game sends ~6–15 s of "results" packets (clock 0, progress 2.0, lap 1, last-lap time set,
car still moving), then all-zero packets (max rpm 0: dropped by the decoder), then the next stage's pre-start.

## 2. Parity matrix

ACR column is the reference behaviour; "DR2 today" is what the replay and the live copy show.

| feature | ACR | DR2 today | gap | fix (phase) |
|---|---|---|---|---|
| Run detection / restart | clean | **every finish spawns a junk "restart" run** (9 of 19 DR2 runs in the live db: 107–124 m after the line, own stage keys like `dirt:4381:-2370`) | the finish packet's clock drop to 0 reads as a restart | finish on lap 0→1 / laps-done / progress ≥ 1.5; nothing after it starts a run (P1) |
| Finish + result time | game clock at the flying finish | **short by 1.3 s to 6.7 s on every run** (taken at progress 0.99): 3:41.5 vs official 3:42.8, 9:57.3 vs 10:04.0 | field 62 ignored | result = field 62; finish line = `length` (P1) |
| Stage recognition | table + spline | 11 of 12 runs named (181-stage table from dr2_logger); **El Rodeo missed** (run start z 188 vs table 206: the run starts at 3 m/s, after the car rolled from the −20 m spot) | start-z gate too tight for where runs begin | match on the exact length first, z only to split equal lengths (5 pairs) (P1) |
| Stage names / surfaces | yes | yes where matched (surface from the table: "game") | – | – |
| Real sectors / splits | level-data lines, game-timed | **none**: `sectors` null everywhere, no live sectors | fields 48–50 not decoded, no lines in `dirt.json` | decode split index + times; lines at k/N of length, N learnt from field 48 (P2) |
| Corner-section splits sheet | yes | works only on a repeated stage (Jagodno ×2), on the mirrored corners | mirror bug | P1 mirror fix |
| Map / elevation | yes | drawn **mirrored** (plan (x, z); heading rate of (x, z) correlates −0.24..−0.45 with steer, −0.65..−0.82 with lateral g) | `plan_xyz` treats DiRT as right-handed | `dirt` → (x, y, −z) like ACR (P1) |
| Corner detection / directions | yes | **174 of 180 corners have the wrong side** (yaw from mirrored positions): coach says "hairpin left" for a right | same | P1 mirror fix + backfill corners |
| Coach diagnosis | yes | runs, but on wrong sides and with YAW all-NaN (warning in `coach_diagnosis`); a "spin in the hairpin at 0.0 km" from the launch | no yaw channel | yaw rate from the vectors (P2) |
| Potential (user / grip / car) | 3 layers, per section | **one 6.6 km "straight" section, radius 10 000 m**; grip layer 1:55 vs runs of 3:37 (nonsense); car layer none | curvature from `yaw_rate` (None) since plan is ACR-only; no car data | plan for every game with positions + yaw from vectors (P2); car layer from the learnt model (P3) |
| Live view / delta | yes | delta works on a repeated stage (reference by stage key); map mirrored; no splits | sectors, mirror | P1/P2 |
| Run view | yes | traces, map (mirrored), sections; no sectors, wrong times | above | P1/P2 |
| Car recognition | name from the bridge + `cars/acr.json` | key `dirt/9000-1300-6`, name "9000 rpm, 6 gears" | no car table | ship `cars/dirt.json` from dr2_logger (MIT): 9000/1300/6 = Seat Ibiza Kit Car, 9000/1200/6 = VW Golf Kitcar, 7400/1700/5 = Škoda Fabia R5 **or** VW Polo GTI R5 (P1) |
| Car data (gearing, torque, limiter, lights) | from game files | limiter from the packet (9000); ratios learnt; no torque; no game shift lights | DR2 files are NeFS with an encrypted header (see 4) | learnt model (P3); NeFS spike (P4) |
| Power / shift-point learning | yes | **0 of 44 rev bands learnt** after 437 pulls; best shift never known (`shift.in_band` 0 everywhere); a spurious "gears 3, 4 re-tuned" tip | driven-wheel slip 10–20 % flat out on a kit car always trips `SLIP_POWER['raw']` 0.08; ratios drift → off-ratio | per-game/per-car slip gate from the run's own slip distribution (P3) |
| Rev lights | from car data + learning | limiter only (no best shift) | as above | follows P3 |
| Launch | yes | 7 launches measured (t50 2.6 s, slip, cut) | – | – (check after the mirror fix) |
| Surface detection | game table + calibration | game table where named; El Rodeo "needs a calibration" | stage match | P1 |
| Wet | bridge | "needs word of rain" (not sent) | game sends none | none possible from UDP |
| Drivetrain | car data | learnt: both kit cars fwd; the R5 none | AWD votes not reached | P3 check |
| Engineer channels | many | brake temps (with −273 junk), suspension, wheels | tyre pressure 0 kPa junk; attitude/ang_vel not derived; splits/last-lap/laps-done not decoded | P2 decoder |
| Demo / README | ACR | one mention | – | P4 |

## 3. The plan

Order is what marth sees first: wrong times and junk runs, then mirrored maps and wrong corner sides, then
names, then sectors, then the learning. Every diff gets the usual Opus review; **A** marks tier-A work (Opus
authors: store schema / migrations of live data, which are this project's co-save analogue).

### Phase 1: correct runs, times, maps, names (one release)

**P1.1 Decoder: the stage's end and the split fields** (sonnet-coder; `oversteer/telemetry_formats.py`,
`_codemasters`, `game == 'dirt'` only — WRCG keeps its own branches)
- `sample.last_lap = floats[62]` when > 0; `sample.laps_done = int(floats[59])` (new slot) — or reuse `lap` (36),
  which already flips 0→1 at the same packet; keep both decoded.
- `sample.sector = int(floats[48])` (new slot `sector`, the split being driven, 0-based) and
  `sample.split_times = (floats[49], floats[50])` with 0 → None (new slot).
- `sample.running = False` and `sample.packet = 'end'` when `floats[3] >= 1.5` (progress 2.0 = results screen).
- `tyre_pressure`: None when every value is 0 (`_positive`); `brake_temp`: None per wheel at or below −273 °C.
- `up = _cross(left, forward)` for `dirt` (the world is left-handed: today's `up` points down, so `vel[2]` has the
  wrong sign); document "y up, left-handed" in the docstring.
- Tests: the fixture packet bytes from `research/dr2/sectors.py` output (a mid-stage packet, the finish packet with
  clock 0 / progress 2.0 / last lap 222.784, a results packet, an all-zero packet).

**P1.2 Run lifecycle: finish on the game's word, no post-finish runs** (sonnet-coder; `oversteer/drive_log.py`)
- In `_track`, for `sample.game == 'dirt'`: the finish is the first packet with `lap` > the run's first `lap`, or
  `progress >= 1.5`, or `last_lap` newly set: `_finished = 1`, `_result_time = sample.last_lap` (fallback: the last
  stage clock), `_finish_d = d` at that packet, `_result_clock = 'game'`; call `live_run.finish` with it. Do not
  finish at `FINISHED` (0.99) for `dirt` any more.
- In `_boundary`: for `dirt`, a stage-clock drop on the finish packet (lap up, or progress ≥ 1.5, or last_lap set)
  is not a restart. While `progress >= 1.5` or the decoder says `running is False`, no packet starts a run
  (results screen); the next run starts on the next pre-start packet (progress < 1, length > 0).
- Acceptance (replay `research/dr2/replay.py`): exactly 11 runs, all finished, `result_time` equal to field 62 to
  1 ms (list in section 2's evidence: 3:42.784, 4:01.695, 2:55.261, 6:35.850, 3:39.272, 7:18.482, 10:04.041,
  3:48.658, 4:56.978, 3:31.210, 3:42.508), no `restart` rows; ACR replay unchanged (`research/diagnosis` scripts).

**P1.3 Plan axes** (sonnet-coder; `telemetry_formats.plan_xyz`): `dirt` → `(x, y, -z)` like ACR, with the
`axes.py` numbers in the docstring. Acceptance: `research/dr2/axes.py` style check that corner directions agree
with the sign of `a_lat` on ≥ 95 % of corners (today 6 of 180). DiRT 4 shares the key: note it as unverified there.

**P1.4 Stage match by exact length** (sonnet-coder; `telemetry_store.match_stage`): a sent length within 0.5 m of
exactly one table length names that stage whatever the start z; z (nearest, no 15 m gate) only chooses among
equal lengths (5 groups). Unit test with El Rodeo (2845.63 m, start z 188).

**P1.5 Car names** (sonnet-coder; new `data/telemetry/cars/dirt.json` + `car_data`/learner naming): the dr2_logger
car table (MIT, license text in the file as `stages/dirt.json` does) keyed `dirt/<max rpm>-<idle rpm>-<gears>`
rounded as `_codemasters` does; per key a list of candidate names with class and drivetrain where known. One
candidate: that name; several: "A / B" until P3.4 resolves it. `user_named` stays untouched. Keep the existing car
keys (no re-key: models and PB rows hang on them).

*Built (P1.5):* `data/telemetry/cars/dirt.json` (made by `research/dr2/car_table.py`). DiRT Rally and DiRT 4 share
`game = 'dirt'`, so a car of either that shares a key with a DR2 car gets the DR2 name. 11 keys are ambiguous (two or
more DR2 cars share the engine and gearbox) and read "A / B" until P3.4.

**P1.6 Repair the rows already written (A — Opus)**: a one-off, versioned store migration for `game = 'dirt'`:
delete the post-finish `restart` runs (unfinished, start progress past the end, < 150 m; never a PB, rule
"PBs never deleted" holds), re-key El Rodeo's runs to the table key, recompute `result_time` from the captures
where they exist (replay the window; else leave it and mark the row), re-run corners/segments/potential for DiRT
runs (the backfill path already exists for ACR: reuse it), drop stage rows nobody references. Backup first
(`_backup`), in one transaction, logged.

### Phase 2: sectors, yaw, potential (one release)

**P2.1 DiRT split lines** (sonnet-coder; `stage_tables`, `drive_log`, `dirt.json`): the game's split lines are at
k/N of the length (N = 4 up to 9.3 km, 8 on 13.2 / 16.6 km seen). Store per stage the `lap_distance` at each
change of `sample.sector` (learnt lines, the `stage_lines` mechanism ACR uses) and give `sector_bounds` lines at
0, L·k/N, L for DiRT stages (N from the stage's learnt count, else 4 below 10 km and 8 above, marked 'game' only
once seen). Origin: store the run's start `lap_distance` (`set_run_start`) for DiRT too, so `sector_times` places
the trace exactly. Times: the game's clock at the change (fields 49/50 exact for S1/S2; the rest from the clock at
the transition, the last from field 62). Then the splits sheet uses `unit = 'sectors'`, and the live view, Run
view and web show S1..SN as for ACR. Acceptance on the replay: Subida S1 0:40.934, S2 0:44.617, S3 ≈ 1:34.8,
S4 ≈ 0:42.4 (to the packet; sum 3:42.784).

**P2.2 Yaw rate and attitude from the vectors** (sonnet-coder; decoder is stateless, so in `Telemetry` or
`drive_log` before the trace row): heading rate = d(atan2 of the plan forward)/dt over ~0.1 s, written to
`sample.yaw_rate`; `ang_vel` (roll, pitch, yaw) from successive forward/left vectors; `attitude` from the vectors
directly (decoder). Acceptance: yaw rate × speed vs lateral g r ≥ 0.8 on the replay; `coach_diagnosis` YAW no
longer all-NaN.

**P2.3 Potential for DiRT** (sonnet-coder; `potential.recompute` / `curvature`): `plan = game` for any game whose
runs have positions (`has_positions`), not only ACR; DiRT's road is `lap_distance` from 0 to the length (no tail).
Acceptance: Jagodno gets ≥ 10 sections, grip layer within 15 % below the PB, no "radius 10000" section.

**P2.4 Launch-spin at 0 km is not a corner spin** (sonnet-coder; coach/events): a spin event before the first
corner while `launch` is running is the launch's slip, not "spun in the hairpin at 0.0 km". Check against the
replay's run 177.

### Phase 3: the car learning

**P3.1 Slip gate per car** (sonnet-coder; `shift_learner`): `SLIP_POWER['raw']` 0.08 rejects nearly every
full-throttle sample of a FWD kit car on gravel and tarmac (driven/free wheel 1.10–1.20). Gate on slip rising
(spin starting: slip above the car-surface's running P75 of full-throttle slip + 0.05, or d(slip)/dt) instead of an
absolute value, for games with wheel speeds; keep ACR's results unchanged (replay-compare its power bands).
Acceptance: ≥ 30 of 44 rev bands on the Ibiza after the replay; a best shift for gears 1–5.

**P3.2 Ratio drift / false re-tune** (sonnet-coder): investigate gear 1's samples (541 → 491) and the "gears 3, 4
re-tuned" tip on the replay with counters; ratios from rpm / driven-wheel speed must not drift with spin. Only if
marth did not re-gear between Jagodno runs (check the captures' rpm/speed per gear before and after).

**P3.3 Car layer from the learnt model** (sonnet-coder; `potential.car_envelope`): where there is no car data,
the drive from the learnt specific power (accel-based power is per kg already) through the learnt ratios and
the packet's limiter. Mark the layer "learnt" in the views.

**P3.4 Telling candidates apart** (sonnet-coder): for a key with several names, the learnt drivetrain (AWD vs FWD
vs RWD separates most groups: Delta S4 / 208 R2, Fiat 131 / Megane RX) and, where P4.1 yields them, gear ratios;
no question to marth.

### Phase 4: spikes and polish

**P4.1 Game files** (Opus spike, read-only on the game): DR2 keeps cars in `cars/*.nefs` (84 archives, 11 GB) and
stages in `locations/*.nefs`. Each starts `NeFS` with a header that is encrypted (entropy ~5 bits/byte over the
first 512 bytes; a hex digest follows the magic), the Ego-engine NeFS v2 layout. Open-source NeFS tools (Ego
NefsEdit) claim to read DiRT Rally 2.0's; whether the car physics inside (torque, ratios, mass) is readable is
unverified. Worth it only for the car layer and for exact name resolution; nothing above depends on it.

**P4.2 Custom UDP** (spike): the game also has `<custom_udp ... filename="packet_data.xml">` (off on marth's rig;
the strings `custom_udp`, `motec`, `nativeSupport` are in `dirtrally2.exe`). If it can send channels extradata 3
lacks (yaw rate, tyre slip, surface), Oversteer could ship a packet file as it does for EA WRC. Find the channel
list before promising anything.

**P4.3 README / demo**: a DiRT Rally 2.0 section (setup: `udp enabled="true" extradata="3"` port), and the parity
table's ✓s once phases 1–2 ship; screenshots follow the UI rule.

## 4. What marth needs to do

No labelling. Only driving with captures on (they are on), after Phase 1 ships:
1. One stage restarted from the pause menu mid-stage, and one paused for ~10 s (no capture has either: the
   restart and pause paths are unverified on DiRT).
2. The same stage twice more in the Ibiza (splits, PB, potential need repeats; Jagodno has two).
3. A few stages in an R5 (resolves Fabia / Polo by drivetrain and, later, ratios) and one in a RWD car.
4. Nothing to set in the game: extradata 3 at port 20777 is already right.
