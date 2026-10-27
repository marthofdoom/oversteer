# Richard Burns Rally (RallySimFans): telemetry, stages, cars and a plan

Research of 2026-10-26 against master (v0.15.4). **No RBR capture exists yet**: marth is installing the
RallySimFans (RSF) build under Wine. Everything marked **verify** waits for the first capture (section 7).

**Status (branch `rbr-telemetry`, Phase 1 built without a capture): section 8 lists what is built and what
waits for the capture.** Marth's install (RSF, `[NGP] udpTelemetry=1`, `udpTelemetryEndpoints=127.0.0.1:5310`)
already sends to 5310, which Oversteer's Auto port listens on; 6776 is a preset as well.
Scripts in `research/rbr/` (read-only, no game files touched):

| script | what it does |
|---|---|
| `ngp_packet.py` | the 664-byte layout as code; `--layout` prints the offset table, `FILE.bin` decodes one packet, `--capture X.ovcap.gz` lists the lifecycle (stage/car changes, steps going back, the clock starting and stopping, gaps) and every field's range |
| `rsf_install.py ROOT` | reads an RSF install: `--telemetry` (the `[NGP]` UDP keys), `--stages` (id, name, length, surface), `--cars`, `--car-now` (the car RSF put in the slot), `--markers ID` (start / split / finish distances from the stage's `.dls`, the driveline from its `.trk`) |

Confidence: **high** (two or more independent sources agree, or a real packet shows it), **medium** (one good
source, or several that agree but copy each other), **low** (inferred, or sources disagree). Times are m:ss.s.

## 1. How RSF sends telemetry

| finding | confidence | sources |
|---|---|---|
| The sender is the **NGP plugin** (Next Generation Physics, by WorkerBee), not the RSF plugin; vanilla RBR sends nothing. The RSF launcher only writes NGP's keys. | high | Simucube docs, Departed Reality docs, mika-n (RSF developer) README |
| In the latest RBR + NGP the packets come from a **separate x64 process**, `Plugins/NGP/bin/x64/NgpTelemetryDaemon.exe`, which needs network access of its own. | medium (one source, but it is RSF's own developer, 2025-26) | mika-n/RBRUDPTelemetryLogger README |
| Launcher: **RSF Launcher > (Advanced options on) > Telemetry > "UDP Telemetry"** checkbox, then an IP and a port, "Apply changes". | high | Departed Reality, Moza, SliProSuperPro README, mika-n |
| **Several destinations are allowed**: "add another address (e.g. `127.0.0.1:6777`)". In `RichardBurnsRally.ini` this is `[NGP] udpTelemetryEndpoints=` a comma-separated `ip:port` list (ztmz writes and parses it that way). Older NGP 6 setups used `udpTelemetryAddress=` + `udpTelemetryPort=`. | high for "several" (SliProSuperPro + ztmz), medium for the key names | SliProSuperPro README; ztmz `RBRGamePrerequisiteCheck.cs` (2026-10); Simucube docs (old keys) |
| **No single default port.** 6776 is what nearly every guide and tool uses (Simucube, Moza, SliProSuperPro, mika-n's logger, Race-Element, pmfrlyn RBRTools, aaronfang, groybe); Departed Reality says 6767, SpaceMonkey 6775, ztmz its own 30001, an NGP 6 forum post 10001. What the launcher pre-fills on a fresh install is not documented anywhere found. | high that 6776 is the convention; unknown launcher default | as listed |
| Packets flow **only while on a stage** ("ensure you are driving a rally stage"); none in menus. | medium | Departed Reality; pmfrlyn treats "no data" as pause |
| **Send rate: not published.** `totalSteps` counts NGP physics steps; NGP sets the physics step to 1/720 s (ReRBR's reverse engineering), so the counter likely runs at ~720/s. Whether a packet goes out every step, every frame, or at a fixed rate is unknown. Race-Element polls at 300 Hz and drops "same step" packets; SpaceMonkey processes only packets with a new `totalSteps`. | low (rate unknown; 720 steps/s medium) | ReRBR `notes/ngp.md` "Stepping"; Race-Element `RBRDataProvider.cs`; SpaceMonkey `RBRTelemetryProvider.cs` |
| Versions: the header is the one in the NGP plugin zip, authored 2019-12 (NGP 6, Guenter Schlupf). The same header is used unchanged for "the latest RBR+NGP" in mika-n's logger (2025-26); ReRBR targets NGP 7.4.778. So the layout has been stable from NGP 6 to NGP 7. | medium-high | SliProSuperPro `External/PhysicsNG/…TelemetryData.h` = mika-n's copy byte for byte |

NGP also writes a per-run **TSV telemetry file** after a stage (a different, longer channel list: `travelDistance`,
`utcSystemTime`, `vecAvgLinearAccelerationCar`, per-wheel `deflectionVelocity`, `wear[0..7]`; SimBridger's
`NgpTelemetry.cs` lists them) and ships an `sdk` folder with three `.h` files in the game folder (SimHub forum).
Not needed for live work; the `sdk` header is worth comparing with ours once installed (section 7).

## 2. The packet: 664 bytes, little-endian, packed, no header

`rbr::telemetry::data::TelemetryData` (`#pragma pack 1`). Wheel order **LF, RF, LB, RB** (front left, front right,
rear left, rear right). Four blocks: `totalSteps`, `Stage` (20 bytes), `Control` (32), `Car` (608 = 96 + 4 × 128).

| off | type | field | meaning, unit, sign | conf. |
|---|---|---|---|---|
| 0 | u32 | totalSteps | physics step counter; **goes back when a stage is (re)loaded**; repeats while paused | high (reset: mika-n, pmfrlyn), rate low |
| 4 | s32 | stage.index | RBR map id (`Maps/Tracks.ini` `[MapNN]`, RSF `stages_data.json` id). **Always 41 for BTB stages** | high (mika-n, NGPCarMenu, sample = 71) |
| 8 | f32 | stage.progress | "position on the driveline"; sample: 0.0234 at the start of a 2.2 km stage; unit **verify** (fraction of the driveline? vAzhure uses it as a distance) | low |
| 12 | f32 | stage.raceTime | the stage clock, s; 0 during the countdown (sample) | high |
| 16 | f32 | stage.driveLineLocation | sample 1.475 at the start; meaning **verify** (distance along the driveline in m is the hypothesis) | low |
| 20 | f32 | stage.distanceToEnd | m to the end; sample 2261.7 on Rally School Stage (table 2200, rounded) | medium |
| 24 | f32 | control.steering | −1..1; **positive right, equal to the G29's axis** (verified on marth's session, section 8) | high (marth's captures) |
| 28–40 | f32 ×4 | throttle, brake, handbrake, clutch | 0..1 | high |
| 44 | s32 | control.gear | **0 reverse, 1 neutral, 2 first …** (sample: 1, car at the start line) | high (mika-n, Race-Element, groybe; simapi says −1/0/1: wrong) |
| 48, 52 | f32 | footbrakePressure, handbrakePressure | unit unknown (0 in the sample) | low |
| 56 | s32 | car.index | the **slot** 0..7, not the car: **RSF always loads the chosen car into slot 5** (sample: 5) | high (ReRBR, sample) |
| 60 | f32 | car.speed | **km/h or mph, following the game's unit setting**; Race-Element's comment says m/s | medium (mika-n, pmfrlyn, SliProSuperPro `speedKph`; Race-Element disagrees) |
| 64–72 | f32 ×3 | positionX, Y, Z | world m; sample (7.5, 45.1, 4.1); **z up** expected (RBR's driveline is z-up), handedness **verify** | medium |
| 76–84 | f32 ×3 | roll, pitch, yaw | **degrees** (sample yaw 91.5); SpaceMonkey and Race-Element convert deg → rad | medium |
| 88–108 | f32 ×6 | velocities: surge, sway, heave, roll, pitch, yaw | car frame, m/s and deg/s (SpaceMonkey); **sway positive RIGHT, rates deg/s, signs below (section 8)** | high (marth's captures) |
| 112–132 | f32 ×6 | accelerations: surge, sway, heave, roll, pitch, yaw | m/s² (simapi divides by 9.81; mika-n calls sway "G"): **verify**. All 0 at rest in the sample: kinematic, not accelerometer (heave would be +9.8) | medium |
| 136 | f32 | engine.rpm | rpm (~1000 at idle in the sample) | high |
| 140–148 | f32 ×3 | radiatorCoolant, engineCoolant, engine temperature | **kelvin** (sample 362 K = 89 °C) | high (groybe, pmfrlyn, sample) |
| 152 + 128·w | | **per wheel w** (LF, RF, LB, RB): | | |
| +0 | f32 | springDeflection | m; 0.195 at rest (so not compression from full droop: **verify** zero and sign) | medium |
| +4..+16 | f32 ×4 | rollbarForce, springForce, damperForce, strutForce | N; at rest strut ≈ −(spring + rollbar + damper), ~2540 N a front corner | medium |
| +20 | s32 | helperSpringIsActive | 0/1 | high |
| +24 | f32 | damper.damage | 0 = none (scale **verify**) | medium |
| +28 | f32 | damper.pistonVelocity | m/s (sign **verify**) | medium |
| +32..+40 | f32 ×3 | brakeDisk layerTemperature, temperature, wear | K, K, 0..? | high (K) |
| +44 | f32 | tire.pressure | **Pa** (220000 = 2.2 bar) | high |
| +48..+56 | f32 ×3 | tire temperature, carcassTemperature, treadTemperature | K (343 K = 70 °C: RSF pre-warms) | high |
| +60 | u32 | tire.currentSegment | 0..7: which of 8 circumferential segments touches the road | medium |
| +64 | f32 ×16 | segment1..8 {temperature K, wear} | per segment around the tyre | medium |

Run `research/rbr/ngp_packet.py --layout` for every offset. Sample packet: the one in pmfrlyn/RBRTools'
`tests/test_telemetry.py` (a real RSF packet: Rally School Stage, slot 5, pre-start), decoded with
`ngp_packet.py`.

**Readers cross-checked** (all agree on every offset and the 664-byte size): NGP header via
fixfactory/SliProSuperPro and mika-n/RBRUDPTelemetryLogger (identical files), PHARTGAMES/SpaceMonkey `RBRAPI.cs`
(decompiled, likely from SimTools' plugin), Spacefreak18/simapi `rbrdata.h` (Linux), strawhatboy/ztmz_pacenote
`RBRUdpData.cs`, RiddleTime/Race-Element, vazhure/vAzhureRacingHub, pmfrlyn/RBRTools `format.py`
(`MESSAGE_LENGTH = 664`), groybe/rbr-udp-telem (decimal offsets). **Disagreements:** gear encoding (simapi),
speed unit (Race-Element says m/s; RSF's developer says km/h or mph by setting), `stage.index` signed or unsigned
(irrelevant). S4nderDevelopment/SimBridger's `NgpTelemetry` is **not** the UDP layout (it is the TSV file's
channel list, marked TODO there).

**What identifies the stage and car.** Stage: `stage.index` exactly (except BTB = 41). Car: nothing in the packet
but the slot (always 5 under RSF); the car is `Cars/Cars.ini [Car05] RSFCarID` at the time the stage loads
(section 3). No max rpm, gear count or idle rpm either.

**What signals start, finish, restart, retire, pause** (from ReRBR's reverse engineering of the game flow, mika-n,
pmfrlyn; all **verify** on the capture):

| event | in the packet |
|---|---|
| stage load | `totalSteps` restarts low (settle steps first; the sample is at step 1839, clock 0) |
| countdown | 7 s, `raceTime` 0, car held; go = `raceTime` starts. A jump start adds the countdown's rest + 10 s to the clock at once |
| driving | `raceTime` runs; outside-assistance penalties are added to the clock while it runs (jumps forward) |
| splits | **not sent**. The game stores the clock at each split key of the stage's `.dls` (section 3) |
| finish | the clock **stops at the finish key** while the car drives on for **20 s** (physics and packets go on), then the result page (no physics: packets stop or repeat). `distanceToEnd` should reach ~0 at the line (**verify**) |
| official stage time | the frozen `raceTime` after the finish (it includes the clock penalties) |
| restart | `totalSteps` goes back and `raceTime` returns to 0 (pmfrlyn's "Stage Restart!" test is exactly steps back after a gap) |
| retire | a message box pauses the game (no physics), accepting it ends the stage: an unfinished run, the clock never stops while moving |
| pause | no physics: packets stop (pmfrlyn) or repeat the same `totalSteps` (Race-Element) |
| replay | the game re-steps physics in replays; whether NGP sends then is **unknown** (must not create runs) |

## 3. Stages and cars on disk (read later, read-only)

`rsf_install.py` reads all of this; run it on the install once it is done (section 7). Paths are under the game
folder (`…/drive_c/Richard Burns Rally/` in the Wine prefix), case-insensitive.

| file | what | conf. |
|---|---|---|
| `rsfdata/cache/stages_data.json` | **RSF's stage list**: `id`/`stage_id`, `name`, `length` (m, mostly rounded to 100 m), `surface_id` (**1 tarmac, 2 gravel, 3 snow**), `tarmac/gravel/snow` shares (%), `short_country`, `author`, `deftime`. 516 stages in a 2025 sample; 74 lengths are shared by several ids, so the id, not the length, names a stage | high (race-engineer sample, NGPCarMenu `RaceStatDB.cpp`, ReRBR) |
| `Maps/Tracks.ini` | `[MapNN]`: `TrackName` (`Maps\track-NN` or `Maps\<id>-<name>\track-<id>`), `StageName`, on newer stages `Length` (**km**, 0.1), `Surface` (**0 tarmac, 1 gravel, 2 snow**), `CountryCode`, `Author` | high (pmfrlyn sample: 426 sections) |
| `Maps/<id>-<name>/Tracks<id>.ini` | the same keys for an RSF-installed stage | medium (ReRBR) |
| `Maps/…/track-<id>_<L>.dls` (L = M N E O S, time of day) | **start, split and finish lines**: keys of the `Driveline` animation set, `{u32 tag, u32 flags, f32 distance m}`; tags `0x15` start, `0x16` finish, `0x17` split (up to 16), `0x18` end of stage; the rest are pace notes | medium (ReRBR, unverified here) |
| `Maps/…/track-<id>_<L>.trk` | the **driveline**: chunk (3, 0x14), points `{xyz (z up), tangent, distance m}`; the domain of the `.dls` distances | medium (ReRBR) |
| `Cars/Cars.ini` | `[Car00]..[Car07]`; RSF rewrites the chosen car's slot (`[Car05]`) with `CarName`, `RSFCarID`, `RSFCarPhysics` | high (ztmz, ReRBR) |
| `rsfdata/cache/cars.json` | `id`, `name`, `path` (physics pack), `carmodel_id`, `base_group_id`, `ngp` | high |
| `rsfdata/cache/cars_data.json` | per car: `power` ("55bhp / 6500rpm"), `torque`, `drive_train` (RWD/FWD/AWD), `transmission` ("4 gears"), `weight`, `wdf`, `steering_wheel` (deg), `year` | high (98 cars in the sample) |
| `rsfdata/cache/cargroups.json`, `carmodels.json` | group (class) names, model folders | high |
| `Physics/s_i2003/common.lsp` (slot 5; ztmz reads `rsfdata\Physics\…`) | the loaded car's physics in plain text: `RPMLimit`, `NumberOfGears`, `Gear<N>Upshift/Downshift`, `MaxSteeringLock`, `SteeringRackRatio`, `InverseMass`. Some packs may be encrypted | medium |
| `RichardBurnsRally.ini [NGP]` | `udpTelemetry`, `udpTelemetryEndpoints` (or `udpTelemetryAddress`, `udpTelemetryPort`) | medium |

**Decision (marth, 2026-10-26): users set up nothing** (no environment variable, no file, no Flatpak override). The
stage names are shipped as facts only (`data/telemetry/stages/rbr.json`: id, name, length, surface and shares, country),
read once from marth's install by `research/rbr/stage_table.py`; a player's own install is an optional extra that
Oversteer finds by itself (section 8).

## 4. What the coach gets ("decode everything")

| channel | RBR | Sample slot |
|---|---|---|
| per-wheel suspension | deflection (m), piston velocity, spring / damper / rollbar / strut forces, helper spring, damper damage | `susp`, `susp_vel`; `tyre_load` ≈ −strutForce (verify); forces new |
| tyre temperatures | surface, carcass, tread, 8 circumferential segments | `tyre_temp` (+ new: carcass, tread, segments) |
| tyre pressure | Pa | `tyre_pressure` (kPa) |
| tyre wear | per segment | `tyre_wear` (mean; verify scale) |
| brakes | disc temperature, layer temperature, wear; brake and handbrake pressure | `brake_temp`; new: brake wear, pressures |
| handbrake, clutch, brake, throttle, steering | yes | as named |
| engine | rpm, two coolant temperatures, engine temperature | `rpm`, `engine_temp`; new: radiator |
| attitude, rates, accelerations | yaw/pitch/roll, angular velocities and accelerations, linear velocity and acceleration (car frame) | `attitude`, `ang_vel`, `yaw_rate`, `vel`, `accel` |
| **not sent** | wheel speeds or rotation, slip ratio / angle, max rpm, idle, gear count, ratios, fuel, turbo, surface, wetness, splits, car id | – |

The missing wheel speeds hurt slip-based work (power-band learning's slip gate, launch slip). Two partial
remedies: the car's ratios from `common.lsp` with rpm give the driven wheel speed (slip vs. ground speed), and
`currentSegment` stepping through 0..7 gives each wheel's rotation if packets come fast enough (8 changes per
turn; at 720 Hz up to ~90 turns/s is countable). Both **verify**.

## 5. Linux

| item | finding | conf. |
|---|---|---|
| UDP from Wine to 127.0.0.1 | Wine's sockets are host sockets: 127.0.0.1 in the prefix is the host's loopback. Lutris/Bottles/umu run in the host network namespace (Bottles' Flatpak and Oversteer's Flatpak both have `--share=network`). Oversteer's own ACR bridge already sends 127.0.0.1:5310 from inside Proton | high |
| `NgpTelemetryDaemon.exe` is x64 | the prefix must be **64-bit** (Wine's and Proton's default). A `WINEARCH=win32` prefix, sometimes suggested for old 32-bit games like RBR, could not start the daemon: no packets | medium (inference) |
| known RSF-on-Linux telemetry problems | none found (searched forums, GitHub, SimHub, Reddit mirrors); RSF is reported to run with GE-Proton 10-22 | low (absence of reports) |
| ports | 6776 and 5310 are both free of known conflicts; FH6's 5200–5300 rule does not apply | high |

## 6. Phased plan

**Packet-size collision check.** 664 bytes collides with nothing Oversteer detects: Forza 232/311/324/331,
OutGauge 92/96, EA WRC 237/252, OVST (magic `OVST`, ≥ 328 with the magic only), Codemasters 256–512 (any
multiple of 4). 664 is above `CODEMASTERS_MAX` (512): today the packet is dropped and logged once as "unknown
664-byte packet", **and the raw capture keeps it** (the recorder writes before decoding), so a capture can be made
before any code exists. Two notes: if `CODEMASTERS_MAX` is ever raised past 664, test RBR before the Codemasters
catch-all; and the `OVST` / EA 4CC checks look at bytes 0–3, which here is `totalSteps` (only ~1.4 × 10⁹ steps,
weeks of driving, could spell them).

### Phase 0: first capture (marth, section 7). Gate for all below.

### Phase 1: decoder and run lifecycle (sonnet-coder; Opus review; tier C) - BUILT, see section 8

**P1.1 `_rbr` in `oversteer/telemetry_formats.py`** (`decode_sample`: `n == 664` before the Codemasters range;
`game = 'rbr'`; docstring entry). Mapping (the **verify** items get fixed from the capture first):

| Sample | from |
|---|---|
| rpm | engine.rpm |
| max_rpm, idle_rpm, gears | None (learnt ceiling as for OutGauge; P2.3 gives the limiter) |
| gear | control.gear − 1 (0 R → −1, 1 N → 0) |
| speed | `hypot(vel surge, sway, heave)` if those are m/s on the capture, else car.speed / 3.6 (km/h) or / 2.237 (mph); decide per packet from their ratio while moving |
| throttle, brake, clutch, handbrake | control.* |
| steer | ±control.steering, positive left (sign from a left turn) |
| car | `rbr/slot<index>` (P1.4 replaces it) |
| stage | `rbr:<stage.index>` (none for 41 = BTB) |
| stage_time | raceTime |
| game_time | totalSteps / steps-per-second (720 if the capture agrees): freezes in a pause, goes back on a restart |
| lap_distance, progress, stage_length | from driveLineLocation / progress / distanceToEnd, whichever the capture shows is metres along the stage; stage_length = start-of-run distanceToEnd + distance or the table's |
| pos | (X, Y, Z) world; `plan_xyz('rbr')` = (x, z, y) or (x, z, −y) by handedness (heading rate vs yaw rate, as ACR was settled) |
| vel, accel, accel_kind | (surge, sway, heave) car frame, signs to ISO (x fwd, y left, z up); 'kinematic' |
| ang_vel, yaw_rate, attitude | angular velocities and angles, deg → rad, signs to ISO |
| susp, susp_vel | springDeflection, pistonVelocity (FL, FR, RL, RR; zero and sign verify) |
| tyre_temp, tyre_pressure, brake_temp, tyre_wear | K − 273.15; Pa / 1000; K − 273.15; mean segment wear |
| engine_temp | engineCoolantTemperature − 273.15 (radiator and engine temperature in new slots) |
| new slots | per-wheel forces, carcass / tread / segment temperatures, brake wear, brake pressures, damper damage, `steps` |

Tests: the capture's packets cut with `telemetry-replay.py --cut` (pre-start, driving, finish, restart, pause).

**P1.2 Run lifecycle in `oversteer/drive_log.py`** (Opus spec, RunTracker is a mechanism):
- start: as today (moving ≥ 3 m/s), plus a new run whenever `game_time` (steps) goes back by more than a second's
  worth (`_boundary`, 'restart'), even while the clock is still 0;
- finish: the clock standing still while moving is `_clock`'s existing rule (1 s, ≥ 500 m): it fits RBR's 20 s
  roll-on. Better, when the capture confirms `distanceToEnd` → 0 at the line: finish at that packet with
  `result_time` = the frozen raceTime (`_result_clock = 'game'`);
- pause: `frozen` for `rbr` = `game_time` unchanged (not "clock equal and speed < 0.1": a pause at speed would pile
  rows); silence in a pause must not end the run before `NO_POSITION_GAP`-like grace (check what a long pause does);
- retire: ends unfinished on the silence / the next stage's steps going back;
- replay: if the capture shows packets in a replay, ignore packets whose position track repeats a finished run's
  start (or whatever flag the capture shows).
Acceptance on the replayed capture: one run per stage attempt, the official times equal to the result screen's
to 0.01 s, no runs from the results page or a replay.

**P1.3 Stage recognition** (sonnet-coder; `scripts/stage-tables.py rbr --install ROOT` from `rsf_install.py`):
`stages/rbr.json` (local) with id, name, length, surface (parts from the json shares), country, `sector_lines_m`
from the `.dls` start / split / finish keys and the driveline for the map; key `rbr:<id>`; `_write_start`
matches by the sent id. BTB (41): `start_cell` fingerprint as for unmatched FH6 routes.

**P1.4 Car identity** (sonnet-coder): at each stage load (steps back) read `Cars/Cars.ini [Car<index>]`
`RSFCarID` (one small file; cached) when an RBR folder is configured; car key `rbr/<RSFCarID>`, name and
drivetrain / power / weight / gear count from `cars.json` + `cars_data.json`, limiter and shift points from the
slot's `common.lsp` (`car_data` gets a `limiter_rpm`). Without the folder: `rbr/slot5`, named "RBR car" (the
learner then mixes cars: say so in the UI). A setting "RBR folder" (auto-found in Lutris/Bottles/umu prefixes).

**P1.5 Port preset** (branch `port-presets`, `PORT_PRESETS` in `oversteer/telemetry.py`):
`(6776, "Richard Burns Rally (RSF / NGP) usual port")`, with a comment citing this doc. The recommended setup is
still to add `127.0.0.1:5310` to RSF's list (no profile change, other tools keep 6776).

### Phase 2: sectors, potential, coach

- **P2.1 Real sectors**: the `.dls` lines (P1.3) crossed by the run's along-stage distance give S1..SN with the
  game's clock, as ACR's `sector_lines_m` do (`stage_tables.sector_lines`). Acceptance: the sum equals the official
  time; the split times equal the in-game split display.
  **Built (branch `rbr-sectors`).** `research/rbr/stage_table.py` reads every `Maps/**/track-<id>_<L>.dls` and
  ships `sector_lines_m` [start, split..., finish] (+ `sector_lines_source`, `sector_lines_confidence`) for the
  480 of 661 stages with a finish and at least one split (the other `.dls` files, 100-500 m SSS and service-area
  maps, have none and keep none). The five time-of-day variants of all 506 stages agree exactly (785 files). 14 files
  have no start marker (Falstone, Rally School, Akagi 2, Sweet Lamb 2...): the start is 0 m, confidence `medium`;
  the others are `high`. Nothing is gated to a game: `stage_tables.sector_lines` (data present) switches on the
  sectors, the finish line (`finish_m` = last line, beats a learnt one), the splits, sector bests and sum of best
  for RBR as for ACR. Changes beyond the table: a local install's table (built from RSF's lists, which hold no
  lines) keeps the shipped lines (`stage_tables._add_table`); RBR runs record their start (`run_start`, the
  driveline where the run began), as ACR's do; on the game's clock the last sector ends at the result, because the
  trace's last row (clock stopped, car rolling on) is further along than the clock's stop and stretches the
  interpolated crossing short (23 ms on the real run).
  **Verified** on marth's capture 20261026-191852 (Monster HC 309, 2:25.6): lines 2.1 / 633.5 / 1850.9 / 2503.5,
  S1 31.553 s, S2 74.702 s, S3 39.380 s, sum 145.636 s = the game's result 145.636 s. The in-game split display
  has not been compared.
- **P2.2 Map and potential**: the `.trk` driveline as the stage spline (z up) for projection, curvature and the
  potential layers; car layer from `common.lsp` (power curve, ratios) where readable.
- **P2.3 Slip without wheel speeds**: driven-wheel speed from rpm and the `common.lsp` ratios, or `currentSegment`
  rates (section 4); per-car slip gate as DR2's P3.1.
- **P2.4 Engineer channels**: tyre carcass/tread/segment temperatures, pressures, brake temps and wear, damper
  damage into the Run view's engineer page; tyre temperature build over a long stage is what RSF players watch.

### Phase 3: polish

README section (setup: RSF Launcher > Telemetry > add `127.0.0.1:5310`), demo, screenshots per the
"screenshots follow UI" rule. NGP's per-run TSV file as an optional import (more channels) only if the live packet
misses something the coach needs.

## 7. First capture: instructions for marth

1. **RSF Launcher** (with *Advanced options* on): *Plugins*: NGP on. *Telemetry*: tick **UDP Telemetry**, add the
   address **`127.0.0.1:5310`** (keep any existing one, e.g. `127.0.0.1:6776`, as a second entry if another tool
   uses it). *Apply changes*. Optional, for a later port check: note what the launcher showed before you changed
   it, and send `RichardBurnsRally.ini`'s `[NGP]` lines (`research/rbr/rsf_install.py ROOT --telemetry`).
   Set the game's speed unit to **km/h**.
2. **Oversteer**: Telemetry tab, port **5310** (the default), **"Record raw telemetry" on**, learning on.
   While on a stage the log should say `telemetry: unknown 664-byte packet from 127.0.0.1` once: that is RBR
   arriving (nothing is decoded yet; the capture keeps every packet).
3. Drive, in one Oversteer session, with a car you will keep for a while:
   - **Rally School Stage** (id 71, 2.2 km, gravel): at the start, sit through the countdown with the car still
     for 3 s, then drive it to the finish and let the car roll on until the result page; wait 10 s there.
   - The same stage again: **restart** from the pause menu after ~30 s; then drive it to the finish.
   - One stage with **splits** (any 5–10 km stage): drive it through, note the split times the game shows.
   - Mid-stage: **pause** for ~20 s while rolling at speed, resume, drive on; later **retire** (pause menu) and
     accept.
   - On a straight: one clear **left** turn, one **right**, one hard **brake**, one constant-speed **hill**.
   - If handy: watch the **replay** of a finished stage for 30 s; drive one stage in a **second car** (car identity).
   Note the stage names and the official times from the result screens (only to check the decoder, not as labels).
4. Then run, read-only, on the install: `research/rbr/rsf_install.py ROOT --stages --cars --car-now` and
   `--markers 71`, and on the capture: `research/rbr/ngp_packet.py --capture <file>.ovcap.gz`. Captures are in
   `~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures/`. If nothing arrives: check the prefix is
   64-bit and that `NgpTelemetryDaemon.exe` runs (`ps` shows it under wine), then the address list.

## 8. Phase 1 as built (no capture yet)

Branch `rbr-telemetry`. Every number below that is not marked otherwise is taken from this doc and **verify on
capture** where section 2 says so; each such place carries a "verify on capture" comment in the code.

### Built

| item | where | notes |
|---|---|---|
| Decoder `_rbr` | `oversteer/telemetry_formats.py` | `n == 664` before the Codemasters range, after the `OVST` magic check; `game = 'rbr'`. Whole packet decoded, per wheel FL, FR, RL, RR (= LF, RF, LB, RB). K to C, Pa to kPa, degrees to radians, gear − 1. |
| New Sample slots | same | `steps`, `dist_to_end`, `drive_progress`, `radiator_temp`, `block_temp`, `pedal_pressure`, `susp_forces`, `helper_spring`, `damper_damage`, `brake_layer_temp`, `brake_wear`, `tyre_carcass_temp`, `tyre_tread_temp`, `tyre_seg_temp`, `tyre_seg_wear`, `tyre_contact`, `ang_accel`. Not stored in the drive log yet (it keeps what it keeps for every game); they are there for the engineer pages (P2.4). |
| Speed | same | the length of the car-frame velocity vector (m/s); `car.speed` (km/h, see "Confirmed") only where the vector is not finite. |
| Plan axes | `plan_xyz('rbr')` | (x, z, y), z up, north's sign unchecked. |
| Port preset | `PORT_PRESETS` in `oversteer/telemetry.py` | `(6776, "Richard Burns Rally (RSF / NGP)")`; Auto listens on it too. A launcher that also sends to `127.0.0.1:5310` (marth's does) needs nothing else. |
| Run lifecycle | `oversteer/drive_log.py` (`RunTracker`: `_rbr_loaded`, `_rbr_finish`, the repeated-step return in `feed`) | start: car moving **and the stage clock above 0** (a car rolling or spinning before the clock starts is no run). Restart: steps back by more than 720 (or another `stage.index`) ends the run unfinished, and the clock going back does as before. Finish: the clock stopped (`RunClock.settle`) with the car moving and `distanceToEnd` ≤ 10 m; or the clock stopped for 3 s well into the stage whatever `distanceToEnd` says. `result_time` = the frozen `raceTime` (game clock, `run_clock = 'game'`). Pause / results page: a packet with the same `totalSteps` as the last is ignored altogether; silence is no run boundary (a position is sent). After a finish the guard blocks the 20 s roll-on and is released by the steps going back, the clock reset, a stage change or 10 s of silence. |
| Stage names | `data/telemetry/stages/rbr.json`, `oversteer/rbr_install.py`, `stage_tables.load()` | stage key `rbr:<map id>`; the shipped table (built by `research/rbr/stage_table.py`) names every RSF stage; the install's own list, where one is found, is merged over it. Without either: "RBR stage <id>", length = driveLineLocation + distanceToEnd at the start. BTB (`stage.index` 41) names no stage (the start-cell fingerprint takes over). |
| Car names | `rbr_install.car_in_slot()` | from a found install only: `Cars/Cars.ini [Car<slot>] RSFCarID` -> `cars.json` name, key `rbr/<RSFCarID>`, `cars_data.json` drivetrain and gear count; checked every 2 s, read again when it changed. Otherwise `rbr/unknown`, "RBR car": a shared key, nothing learnt under it (no packet fingerprint tells the cars apart, see "Names without any setup"). |
| Coach | `coach.LAUNCH_OUT`, `ATTITUDE_OUT`, `coach_context.UNCHECKED_GAMES` | `rbr` is in all three: no launch, spin or stall coaching and no speed-drop reads (the speed channel is not checked). The steering sign and the other signs of section 8 (sway, yaw rate, roll, pitch, heading) were verified on marth's session against the G29 axis and the positions. |
| Tests | `tests/test_rbr.py` | the layout against `research/rbr/ngp_packet.py`, the doc's sample values, wheel order, units, a fake install (stage table, cache, cars, no write under the install), and synthetic packet sequences: finish, restart, pause (repeated steps and silence), retire, results page, no phantom runs. |

Note on `research/rbr/ngp_packet.py`: it holds the layout but **no sample packet** (the doc's "sample" is
pmfrlyn/RBRTools' `tests/test_telemetry.py`, which is not in this repo); the test builds a packet with the doc's
sample values on top of that layout. `research/rbr/rsf_install.py` stops on marth's `Maps/Tracks.ini` (line 201,
a stray `***`); `rbr_install.read_ini` skips such lines.

### Confirmed by marth's first captures (2026-10-26, stage 81, pedals inverted in them: pedal values not used)

- 720 packets a second, `totalSteps` +1 per packet (so 720 steps/s is the packet rate too); arrival timestamps come
  in bursts of about 3 that share one time (about 236 distinct times a second), so the steps are the packet clock.
- `driveLineLocation` is metres along the stage and `driveLineLocation + distanceToEnd` is its length (394.31 m
  on stage 81, constant), so `stage_length` and `lap_distance` are right. `stage.progress` is still unknown (3.38 at
  89 m, 6.16 at 182 m; not linear).
- `car.speed` is km/h (3.5 to 3.7 times the velocity vector's length at speed) but reads the wheels: 62 km/h with
  the car held and the wheels spinning, where the vector says 5 m/s. The speed is the vector.
- Kelvin, Pa and the gear mapping (1 = neutral, 2 = first) are as the doc says. `strutForce` is minus the spring
  force (-3238 against +3232 N at a front corner at rest), so `tyre_load = -strutForce` holds at rest.
- The clock starts when it starts, not at the go: on this map the car rolled for 8 s with the clock at 0 and the
  driveline position moving, and the clock started at 0.004 s as it crossed `driveLineLocation` 92.9 m. The
  tracker starts a run at the clock, not at the first movement.
- A pause menu stops the packets (21 s of silence on the capture) and a restart sets `totalSteps` to 1 with
  `raceTime` 0 and the car back at the start: the run ends as a restart.
- Stage 81 is not in `rsfdata/cache/stages_data.json` nor `Maps/Tracks.ini` (a stock map): "RBR stage 81", length from
  the packet.

### Names without any setup

- **Stages**: `data/telemetry/stages/rbr.json` (661 stages, read once from an RSF install on 2026-10-26; stage 41 /
  Beyond the Box left out) names every stage RSF lists. An id it lacks (a custom stage added later) is "RBR stage <id>"
  with the length from the packet. A stored fallback name (and only that exact form) is replaced once a table knows the
  stage: `Store.seed_stages` does it for every table at each start, so a database from before the shipped table is
  repaired on its next start.
- **Cars**: the packets cannot name the car. `car.index` is the slot (5 or 6 on the captures), and nothing else in the
  packet identifies it that a table could carry: the two cars captured (Fiesta WRC 2019, i20 Coupe WRC 2021) have the same
  mass (1390 kg, measured 1361-1394 kg at rest, so +-15 kg of noise), balance (49%), idle (1214 rpm), redline (about 8530
  rpm) and gear count (6); over RSF's 103 cars only half are unique even by published mass, balance and gears. So without
  the install the key is the shared `rbr/unknown` ("RBR car"): runs and PBs are kept, nothing is learnt per car.
  Where Oversteer finds an install by itself, `Cars.ini [Car05/06] RSFCarID` gives the exact car (`rbr/<id>`, name,
  drivetrain, gears).
  The best further step, not built: identify the car from its gear ratios once shifted (the ratios between gears do not
  depend on the tyre radius; the shift learner already learns them per gear), against ratios from the physics packs'
  `common.lsp`. Only the 9 packs installed locally have them, not RSF's 103 cars.
- **The car table** (`data/telemetry/cars/rbr.json`, built by `research/rbr/car_table.py`): a name for every RSF car id
  (`names`, key `rbr/<RSFCarID>`, 102 cars, with path, year, drivetrain and gear count; `car_data.display_name`, which the
  decoder uses when the install gave the key but no name), and for the cars whose NGP physics the install has held (today
  the Fiesta WRC 2019 = 11, i20 Coupe WRC 2021 = 104, Skoda Fabia RS Rally2 = 106, Toyota GR Yaris Rally2 = 107) a
  `car_data` entry like ACR's: the limiter, the torque curve, the gear sets and final drive. With the install visible the
  key is `rbr/<id>`, so the rev lights use the shipped limiter (RBR sends no max rpm) and the shift learner the shipped
  gearing and torque (exact best upshifts from the first drive). Rebuild after loading more cars in RSF:
  `python3 research/rbr/car_table.py /path/to/RBR` (writes the file; a car an earlier run read keeps its entry).
  What it reads: `rsfdata/cars/<path>/` or `<path>.zip` (RSF's download of each car), and a slot folder of
  `rsfdata/Physics/` only where a file named after the RSF car marks it (`s_i2003` = Fiesta, `t_coroll` = i20; `c_xsara`,
  `h_accent`, `mg_zr`, `m_lancer`, `p_206`, `s_i2000` carry no marker and are stock RBR cars, not shipped). From
  `common.lsp`: `Tq_NN_RPM_NGP` with `Tq_NN_NGP` (normalised 0..1) times the peak torque of `cars_data.json` (450 Nm on the
  Fiesta, the curve peaks at its listed 5500 rpm), `RPMLimit`, `GearNUpshift` (the AI's gearbox), `InverseMass`,
  `EffWheelRadius`; from `r_<surface>.lsp` the `GearListNN` Gear/Pinion teeth; from `setups/d_<surface>.lsp` the default
  setup's `GearId2..` (GearId0 reverse, 1 neutral, 2.. first gear), `FinalDriveId` and `DropGearId`. A gear set per default
  setup where they differ (the Fabia's tarmac set is shorter), gravel the default.
  **Verified against the captures of 2026-10-26.** The captures carry no `rbr_car`; the packet's slot tells the car:
  slot 5 = Cars.ini [Car05] = Fiesta WRC 2019 (captures 18:54, 18:57, 19:02, 19:04; gears 1-3 only), slot 6 = [Car06] = i20
  Coupe WRC 2021 (19:06, 19:14, 19:18; all six gears). The two cars share one gear list and drop gear (the i20's FinalDriveId
  pair is 60 / 60, the Fiesta's 62 / 63), so the table below, from the packet's wheel speed (`car.speed` / 3.6, clutch up, the
  mode over 100 000 packets; the ground-speed vector is not usable: wheelspin and slides move it by 4-9 %), is the i20's in
  every gear and the Fiesta's in gears 1-3 (398 / 316 / 256 on slot 5 as on slot 6), which is what tells that the Fiesta's
  62 / 63 does not multiply in. The i20's physics are in `rsfdata/Physics/t_coroll` (marked) and
  `rsfdata/cars/Hyundai_i20_Coupe_WRC_2021_ngp7`; the Fiesta's in `s_i2003` and its cars folder. Gears 4-6 of the Fiesta
  itself are not measured (it is the same list).

  | gear | rpm per m/s measured | shipped (ratio x drop 4.375 x 60 / 2 pi r, r 0.315) | diff |
  |---|---|---|---|
  | 1 | 398 | 397.9 | +0.03 % |
  | 2 | 316 | 316.3 | -0.08 % |
  | 3 | 256 | 255.8 | +0.08 % |
  | 4 | 209 | 209.4 | -0.19 % |
  | 5 | 175 | 174.8 | +0.10 % |
  | 6 | 149 | 148.5 | +0.31 % |

  The limiter: RPMLimit 8500, the highest rpm seen 8518-8544 on the Fiesta (slot 5) and 8526-8552 on the i20 (the game overshoots by up to 0.6 %). Surprises:
  the **drop gear multiplies in and its pair is stored the other way** (`Pinion 35, Gear 8` is 35 / 8 = 4.375, not 8 / 35),
  whereas `FinalDriveId` (62 / 63 on the Fiesta, 60 / 60 on the i20) does **not** (with it every gear reads 1.6 % off, without
  it within 0.3 %): it is kept as `transfer_ratio` and left out of the overall ratio. The capture of 19:18 (slot 6) reads 0.912
  of these in every gear = drop gear option 12 (32 / 8 = 4.0, 0.914): RSF's own setup differs from the default, so the
  overall ratio differs with the setup (the steps between gears, which the best upshift needs, do not; the learner's
  `gearing_aside` and `match_set` take it from the learnt steps). A non-default drop gear scales every gear (e.g. 0.914), so `slip_rpm` turns off (ZERO_PLAUSIBLE) and the car envelope uses the default drop gear for that run. Wheel speed under the ground-speed vector the decoder uses
  for `speed` is what reads the driveline; the vector reads 5-9 % off in the low gears from wheelspin.
- **The optional install**: `rbr_install.find_root()` looks, with no setting, in the usual Lutris (including the game path
  in Lutris' game files), Bottles, Heroic, Steam (Proton prefixes, and the exe/StartDir of non-Steam shortcuts in `userdata/*/config/shortcuts.vdf`, read-only, at most 20 files of 1 MB) and Wine places. The search runs once on a thread of its own and again, the same way, only when an RBR run starts and the last one found nothing (30 s apart at most); the packet path never searches. When found it adds the exact car and
  the install's own stage list over the shipped one (custom stages), cached in `<data>/oversteer/rbr/stages.json` and read
  again when the install's stage files change (checked at each run's start, at most every 30 s). A sandbox that cannot see
  the install (the Flatpak) just finds none and uses the shipped names. `OVERSTEER_RBR_DIR` points the finder at an
  install for development; it is not documented to users and not a setting.

### Per-packet cost at 720 packets a second

The tracker and the shift learner use a smooth clock made from `totalSteps` (the arrival times come in bursts) and keep
one packet in 12 steps (60 Hz) as segment rows and for the shift learner's windows; the lights, the live view, the finish
and the lifecycle see every packet. The clock repeating on most packets is no stop (RBR is exempt from the "frozen clock"
rule: its pauses are repeated steps or silence). A run starts only while the clock is moving, and after a finish only the
stage loading again (steps back) or another stage releases the guard, however long the pause.

### Capture size

The listener, the learner and the coach get every packet (720 a second). **RBR captures are saved at 60 Hz**: the
recorder (`oversteer/telemetry_capture.py`, `THIN`: packet length -> maximum saved rate, here 664 -> 60) keeps one
packet in every 12 steps and always the packets where a lifecycle field changes (steps going back, a new
`stage.index`, the clock starting, stopping or jumping by over 0.5 s, `distanceToEnd` jumping by over 5 m) and the
first and last of a file. The file format is unchanged; other formats are not thinned. The header line also gets `rbr_car` (`key`, `id`,
`name` of the car RSF had in the packet's slot when the file was opened), since the packets do not carry the car; the
replays (`telemetry_capture.replay(..., recorded_car=meta.get('rbr_car'))`, used by the data repair's retime and by
`scripts/telemetry-replay.py`) name the car from it, so an old capture keeps the car it was driven with. A capture
without the key (before this) is named from the install found today, or `rbr/unknown`. A replay of a thinned capture
gives the same run starts, finishes, result times and restarts as the full-rate one (`tests/test_rbr.py`; the
2026-10-26 run capture gave the same run both ways).

### Confirmed by marth's session of 2026-10-26 (five files, pedals right)

| file | stage | what is in it |
|---|---|---|
| 190243 | 83 (not in the table: "RBR stage 83", 195 m), car slot 5 | two attempts of a short map; the clock starts as the car crosses the start line (the car rolls 5 s with the clock at 0 first), max clock 9.2 s, a restart (steps back to 1 after 17.7 s of silence), second attempt ends with the capture |
| 190435 | 83 | the same, max clock 9.6 s, restart after 14.3 s |
| 190639 | 517 Bayanbulak-Pegasus 2 (18 070 m), car slot 6 | a first leg to 1:01.3 ended by a restart at 83 km/h after 21 s of silence (pause menu); a second leg of 5:24 clock: a +35.003 s jump of the clock at driveline 2328 m with the car at rest, a 10.4 s silence (pause) at 2:26.9 while rolling, and the capture ends with the car at rest (a retire, no finish) |
| 191420 | 517 | one leg to 3:54.6, the same +35.003 s jump at 2330 m, ends at rest (retire) |
| 191852 | 309 Monster HC (2501 m), slot 6 | **the one finish**: the clock stops at 2:25.6 (145.636 s) with `distanceToEnd` -0.02 m and `driveLineLocation` 2503.5 (the `.dls` finish is 2503.52), the car at 118 km/h rolling on (braking to 74 km/h); the capture ends 2.7 s later, so the 20 s roll-on and the results page are not on any capture |

- The run finishes 0.25 s after the clock stops (`RunClock.settle`), at the first stopped packet's clock: `result_time`
  2:25.6 against the clock stop 2:25.6 on the full-rate and the 60 Hz files alike. `distanceToEnd` is negative past the
  line, so the rule is `distanceToEnd <= 10 m` (was 30), and it no longer needs 500 m of run (a run restarted late in the
  stage finishes too; the 3 s fallback still does).
- `driveLineLocation` is the `.dls` distance domain (finish 2503.5 m on 309; splits 633.5 and 1850.9 m): **splits are
  not sent, but the sector times can be read as the clock at those distances (P2.1), no extra data needed.**
  Nothing in the packets marks a split. The +35.003 s clock jump (twice, at 2328 and 2330 m on 517, at rest, not near its
  splits at 2500 and 15500 m) is unexplained: a penalty the game adds, which stays in the clock and the result.
- Signs, from the positions' heading rate (the plan (x, z, y) as east, north: not mirrored) against the packet and
  the G29 axis (steering equals the wheel, high to the right; 95% of strong corners agree): `control.steering` positive
  right, so `steer` is its negative; sway velocity and acceleration are positive to the **right** (the doc guessed left),
  so the car-frame y is their negative; the yaw rate is positive to the left; roll positive with the right side down
  (a left turn rolls it +); pitch positive nose up (so the ISO pitch rate is its negative); `car.yaw` is a compass angle
  (it grows turning right); the angular rates are deg/s and the angular accelerations deg/s2; the **accelerations are in
  g** (`acc.sway` = v x heading rate / 9.81, braking -0.8, driving +0.5, 0 at rest) and converted to m/s2.
- `springDeflection` is the extension, not the compression: 0.12 to 0.41 m, smaller under braking and on the loaded
  side of a corner, spring force -0.99 against it, `pistonVelocity` its rate (0.97). `susp` and `susp_vel` are their
  negatives; the zero is not known. `tyre_load = -strutForce` (0 to 42 kN at the extremes).
- Pedals are 0..1 at rest 0 / 0 / 0 with the clutch 1.0 only when pressed (1 while standing at the start, 1 for a
  shift, 0 driving), foot and hand brake pressures in Pa (up to 6.4e6, brake pressure tracks the pedal at r 1.0), gears
  1..7 (neutral to 6th), rpm 1220 idle to 8550 and a few negative values (clamped). The car slot is 5 or 6: it is the
  slot of `Cars.ini` that RSF used, read from the packet.
- Not checked: heave (velocity does not follow the position's z), the vertical axis (z is up by its range), `progress`.

### Pending, verify on capture

- `car.speed` as km/h or mph by the game's setting (the captures were set to km/h).
- `stage.progress` (only stored as `drive_progress`).
- Heave, the suspension's zero, the wear and damage scales (brake wear 0.10 and tyre segment wear 0.015 after a
  long stage; damper damage 0).
- What a replay sends (a replay must not create runs; nothing is done about it yet), and the 20 s roll-on and the results
  page after a finish (the one finish capture ends 2.7 s after the line). A retire is the packets stopping with the car at rest.
- The outside-assistance penalties and jump starts add to the clock while it runs: the result is the frozen clock,
  so they are in it; the trace's `t` jumps with them.
- Stage ids against the install (e.g. 71 = Rally School Stage), the real `Cars.ini` after a stage load (marth's has
  no `RSFCarID` in `[Car05]` yet: RSF writes it at launch).

### Not built (P2 and later)

Real sectors from the `.dls` lines, the `.trk` driveline for maps and potential, slip without wheel speeds,
the engineer page for the new channels, `common.lsp` limiter / shift points, the "RBR folder" setting, README
and screenshots, and the NGP TSV import.

## Sources

- NGP UDP header `rbr.telemetry.data.TelemetryData.h` (Guenter Schlupf, 2019-12-28; from the NGP plugin zip):
  github.com/fixfactory/SliProSuperPro `External/PhysicsNG/` (pushed 2025-12) and
  github.com/mika-n/RBRUDPTelemetryLogger (Unlicense, 2026-08; README: NgpTelemetryDaemon, port 6776, launcher
  pages; `RBRUDPTelemetryLogger.cpp`: gear 0 R / 1 N, stage id 41 for BTB, slot 0..7, speed unit, steps back on a
  reload). Read 2026-10-26.
- SliProSuperPro README "Richard Burns Rally Configuration" (Advanced Settings > Telemetry, several addresses).
- strawhatboy/ztmz_pacenote (AGPL-3.0, 2026-10-20): `RBRGamePrerequisiteCheck.cs` (`udpTelemetryEndpoints`),
  `RBRGameDataReader.cs` (Cars.ini `RSFCarID`, `rsfdata/cache/cars.json`, `common.lsp`), `RBRUdpData.cs`.
- PHARTGAMES/SpaceMonkey (MIT) `RBRAPI.cs`, `RBRTelemetryProvider.cs` (degrees, sign flips, new-step filter).
- RiddleTime/Race-Element (GPL-3.0, 2026-10-25) `RBRDataProvider.cs`, `RBRTelemetryData.cs`.
- Spacefreak18/simapi `include/rbrdata.h`, `simapi/mapping/rbrmapper.c` (Linux; gear mapping disagrees).
- pmfrlyn/RBRTools `rbr_tools/telemetry/format.py` (664 bytes, K units), `tests/test_telemetry.py` (a real packet),
  `samples/Tracks.ini`, `rbr_influx_telemetry_sender.py` (pause and restart by steps).
- groybe/rbr-udp-telem (GPL-3.0): decimal offsets, gear − 1, kelvin.
- vazhure/vAzhureRacingHub `RichardBurnsRally/RBRGame.cs`.
- S4nderDevelopment/SimBridger `NgpTelemetry.cs` (NGP TSV channel names, not the UDP layout).
- JodusNodus/rerbr (GPL-3.0, 2026-10-26): `notes/gameplay.md` (race flow, clock, finish 20 s, restart, retire,
  pause), `notes/ngp.md` (1/720 s step), `notes/formats.md` and `crates/rbr-formats/src/{dls,markers}.rs` (.dls
  Driveline keys, .trk driveline), `src/rsf/{cars,stages}.rs` (slot 5, RSF files). Formats read, no code copied.
- mika-n/NGPCarMenu `src/RaceStatDB.cpp`: surface codes of stages_data.json and Tracks.ini, length sources, BTB 41.
- durandom/race-engineer `docs/data-model-rbr.md` and `data/rbr/*.json` (RSF cache samples, 2025).
- Simucube docs "Richard Burns Rally" (`[NGP] udpTelemetry`, `udpTelemetryAddress`, `udpTelemetryPort=6776`).
- Departed Reality docs, DR Sim Manager "Richard Burns Rally" (launcher steps, port 6767, "driving a rally stage").
- Moza support "Richard Burns Rally (RBR)" (Telemetry page, 127.0.0.1:6776).
- SHWotever/SimHub issue 490 (NGP 6 UDP, 2020-21); simhubdash forum "RBR ngp6 new telemetry data" (TSV file, sdk
  folder).
- SimXperience forum "does SC4 use Richard Burns Rally telemetry data" (NGP 6, port 10001).
- feddit.it post on RSF with GE-Proton 10-22 (unverified).
