# Forza Horizon 6: telemetry, car and event recognition

Research and plan, 2026-10-26 (Oversteer master v0.15.4, FH6 Steam build of 2026-10-05).
No FH6 capture exists yet: everything marked **verify** waits for one (section 7).
Scripts and outputs: `research/fh6/`.

## 1. Data Out: what the game sends

**Format.** One fixed, 324-byte, little-endian UDP datagram per rendered frame (about
128-140 a second at a high frame rate), one way, no selectable format. It is the FH4/FH5
"Dash" layout; the FH6 documentation only names the 12 Horizon-specific bytes at 232-243
that FH4/FH5 never documented. Oversteer already reads it as `forza-fh` (324) in
`oversteer/telemetry_formats.py::_forza`.

| Offset | Field | Note |
|---|---|---|
| 0 | S32 IsRaceOn | **1 in free roam too**; 0 only in menus/pause. Not "in an event". |
| 4 | U32 TimestampMS | wraps |
| 8-19 | F32 EngineMaxRpm, EngineIdleRpm, CurrentEngineRpm | |
| 20-43 | F32 Acceleration XYZ, Velocity XYZ | car-local, x right, y up, z forward |
| 44-67 | F32 AngularVelocity XYZ; Yaw, Pitch, Roll | yaw is world-space: the car moves along (sin yaw, cos yaw) in world x/z |
| 68-211 | wheel arrays x4 (FL, FR, RL, RR) | NormalizedSuspensionTravel, TireSlipRatio, WheelRotationSpeed, WheelOnRumbleStrip (S32), WheelInPuddle (S32 0/1 per the FH6 doc; **verify**), SurfaceRumble, TireSlipAngle, TireCombinedSlip, SuspensionTravelMeters |
| 212 | S32 CarOrdinal | the car model id (section 2) |
| 216 | S32 CarClass | 0 D, 1 C, 2 B, 3 A, 4 S1, 5 S2, **6 R (new in FH6, PI 901-998)**, 7 X (999) |
| 220 | S32 CarPerformanceIndex | 100-999 (the tuned car's PI, not stock) |
| 224 | S32 DrivetrainType | 0 FWD, 1 RWD, 2 AWD |
| 228 | S32 NumCylinders | |
| 232 | **U32 CarGroup** | FH6-named; enum not published (Oversteer appends it as `group:N`) |
| 236 | **F32 SmashableVelDiff** | speed lost to the last smashable hit (m/s) |
| 240 | **F32 SmashableMass** | mass of the last smashable hit (kg) |
| 244-255 | F32 Position XYZ | world metres, y up; **same frame as the game's route files** (section 3.2) |
| 256-267 | F32 Speed (m/s), Power (W), Torque (Nm) | |
| 268-283 | F32 TireTemp x4 | Fahrenheit, whatever the in-game units |
| 284-295 | F32 Boost (psi), Fuel (0..1), DistanceTraveled | DistanceTraveled is **not metres**: a per-route progress value (below) |
| 296-311 | F32 BestLap, LastLap, CurrentLap, CurrentRaceTime | seconds |
| 312 | U16 LapNumber | 0-based; does not count the final lap |
| 314 | U8 RacePosition | >0 from the first countdown frame of a gridded race; 0 in free roam |
| 315-320 | U8 Accel, Brake, Clutch, HandBrake, Gear; S8 Steer | Gear 0 = reverse, 11 = neutral (Oversteer's mapping); Steer -127 full left |
| 321-322 | S8 NormalizedDrivingLine, NormalizedAIBrakeDifference | driving line saturates at +-127 far off the course |
| 323 | padding | |

**Versus FM2023 (331).** FH6 has CarGroup/SmashableVelDiff/SmashableMass; it has no
TireWear and no TrackOrdinal. So FH6 never names the track or event.

**Enable it.** In game: Settings > HUD and Gameplay > Data Out: On, Data Out IP Address
`127.0.0.1`, Data Out IP Port `5310`. Ports 5200-5300 must be avoided: the game binds its
own socket there (Oversteer already warns about this; its default is 5310). The Steam build
needs no loopback exemption (that is a Microsoft Store/Windows thing).

**Proton/Linux.** No public report either way; nothing special is expected: Wine sockets are
host sockets and the pressure-vessel container shares the host network (Oversteer's ACR
bridge already sends 127.0.0.1:5310 from inside it). First capture confirms (section 7).

**Quirks the community verified on real captures (LapScope, 2026-07):**
- `DistanceTraveled` reads about 5950 at the end of **every** completed course whatever
  its real length, and resets to 0 at a point-to-point/WTA launch and at a sprint finish
  hand-back (also on fast travel). Use it as route progress, never as metres; integrate
  `Speed` for distance.
- Event finishes: circuit = `LastLap` changes while `LapNumber` stands still, or the stream
  stops at the line; sprints/touge/cross-country: the stream just stops at the line, or the
  game hands back control parked at the line with the odometer reset; World Time Attack
  sends no lap fields at all.
- Rewinds: the lap clock drops below its high-water mark.
- Data Out stops in menus, pause, photo mode, replays and post-race.
- A title update may change the packet: log size + hex rather than crash.

### Things in Oversteer's decoder to fix for FH6 (phase 1)
1. `sample.distance` takes DistanceTraveled as metres. For `forza-fh` store it as
   `progress` (raw) and give `distance` from integrated speed.
2. `car_class` is `class:N pi:P group:G`; map N to the letters above (with R).
3. `sample.lap` is 0-based; check what RunTracker expects.
4. Puddle: decoder reads S32 0/1 for `forza-fh` (matches the FH6 doc); verify.
5. Car name: `Forza car <ordinal>` becomes the table name (section 2).
6. Signs flagged "verify" in `_forza` (yaw rate, angular velocity, steer, accel kind):
   check on the first capture (a left turn, a hill at constant speed).

## 2. Car recognition (CarOrdinal)

**The game's own database is not readable.** `media/Stripped/gamedbRC.slt` (16 MB) is
encrypted (entropy 8.00 bit/byte, no SQLite header); the entity XMLs in
`media/Stripped/EntityModel.zip` use a non-standard compression method (22) and are also
opaque. Decrypting would be reverse engineering of the executable; not worth it for names
(a Fable escalation if ever wanted).

**What is readable.** `media/Cars/*.zip` (682 cars, plain deflate): file names are the
game's media names `<MAKE>_<Model>_<YY>` (e.g. `ALF_4C_14`), with each car's
`physicsdefinition.bin`, `Manifest.xml`, `carbin`. No ordinal inside.

**Best ordinal source.** HDR's gist "Forza Horizon 6 Car Ordinals"
(gist.github.com/HDR/0659d1717bc61504bf83750628963f4f, "as of 13.10.26", updated
2026-10-22): **682 entries, exactly the install's 682 cars**, `{"YYYY Make Model": "ordinal"}`.
LapScope (github.com/darcane/LapScope, MIT) ships a 639-entry copy of it
(`app/car_ordinals.json`, refreshed daily from its repo); the two agree on all shared
ordinals except 10 renamed entries. Ordinals are unique across Forza titles (FM ordinals
stay with a car). No licence on the gist: ship LapScope's MIT copy as the base with
attribution, and fetch the gist as the refresh (or ask HDR for a licence).

**Media-name crosswalk** (`research/fh6/fh6_cars.py`): matching the ordinal names to the
install's media names by year and model similarity names 641 of 682; checked against a
third-party crosswalk (used only to measure, not copied): 588 agree, 10 disagree, 40
unmatched. Good enough to link a car to its game files later (engine/gearing like
`data/telemetry/cars/acr.json`), not needed for names.

**Proposal: `data/telemetry/cars/fh6-ordinals.json`**
`{"game": "forza-fh", "source": ..., "licence": ..., "cars": [{"ordinal", "year", "make",
"model", "name", "media"}]}`. Make/model split: the longest known make prefix (a short
make list in the generator). Class, PI, drivetrain and cylinders stay per packet (they
change with upgrades). Car key stays `forza-fh/<ordinal>` (no DB migration); only
`car_name` changes, and stored runs pick up names on the next backfill
(`telemetry_store` already treats `Forza car N` as a placeholder). Car identity for
coaching should be `ordinal + PI + drivetrain` (a tune changes all three; the shift learner
already keys per car).

**Refresh.** `scripts/fh6-cars.py --fetch` downloads the gist (and/or LapScope's list),
rebuilds the table (media names from the local install when present), prints added/renamed
ordinals; run by hand after a car pack. Unknown ordinal at runtime: show `Forza car N
(PI cls)` and let the user name it once (stored override, wins over the table).

## 3. Race/event recognition

### 3.1 What a "run" is in FH6
| Kind | Start | Finish | Notes |
|---|---|---|---|
| Circuit race | RacePosition>0, race clock from ~0 | LastLap changes with LapNumber unchanged, or stream stops within metres of the start line | laps from LapNumber |
| Sprint (road/dirt/street) | RacePosition>0, DistanceTraveled reset at launch | stream stops at the line, or odometer-reset hand-back parked at the line | |
| Touge, cross-country | as sprint | stream stops at the line | |
| Rivals | laps, no grid (RacePosition may be 0) | user leaves | each lap is a run lap |
| World Time Attack | DistanceTraveled starts growing after a hold at 0 | odometer hard reset while the clock runs | no lap fields: laps by returning to the launch point |
| Speed trap / zone / camera, drift zone, danger sign, Trailblazer | free roam (RacePosition 0) | none | **not runs**; possible later as "challenges" if route files are found (their entity XMLs are encrypted) |
| Free roam | IsRaceOn 1, RacePosition 0, no lap fields | - | not a run; still feeds shift learning |

Restart: `CurrentRaceTime` drops back to ~0 with the car back at the start -> end the run
as `restart` and start a new one (same as ACR). Rewind: lap clock below its high-water
mark -> trim the rewound stretch, mark the run `rewound`. Quit mid-run at speed looks like a
stream-cut finish: accept a cut-off finish only when the car is within 60 m of the matched
route's end (section 3.2) or, unmatched, flag the run `cutoff`.

### 3.2 Naming events from the game's route files (found in the install)
`media/OpenWorld/Brio/AITracks/Route<N>.owt` (164 files, plus `.nav`) are readable: magic
`FTWO`, point count at byte 36, 56-byte records from byte 96, each a world position
(x, y up, z), a lateral half-width vector (~5 m) and an up normal, one every ~2.0 m
(`research/fh6/fh6_routes.py`, summary in `research/fh6/routes.csv`). 37 are closed
loops; lengths 0.17-85 km (median 5.2 km; Route5555 is The Goliath).

**They are in the Data Out position frame.** Matched against LapScope's catalogue of named
courses (fingerprinted from real telemetry: start x/z and bounding box), 27 route files
match 20 named courses with the bounding box within 5 % and the start within 400 m: e.g.
The Goliath 13 767 x 17 352 m vs 13 763 x 17 351 m, Sekibe Scramble 414 x 446 vs
410 x 438 (start 13 m off). So a run's positions can be projected onto a route file with
no capture-made template, the same way ACR runs use the road spline.

**Naming.** Route files carry ids, not names (names live in the encrypted gamedb).
Proposal: ship `data/telemetry/stages/fh6.json` with one entry per route file (id, closed,
length, start, end, bbox, a decimated polyline at 10 m) and a `name` where a public
catalogue gives one (LapScope's 77-course `track_catalog.json`, MIT, matched by bbox +
start; more can be named by marth driving them, never by hand labelling: the name is shown
on the in-game result screen but not sent, so unmatched routes stay `FH6 route <N>`, with
`kind` and length, until a source names them). Duplicate geometry (e.g. Route301 and
Route30003) is one stage with `alt_codes`.

**Matching at run time** (phase 2): when a run starts (gridded, or launch), take the start
position + heading; candidates = routes whose first point is within 300 m and heading within
60 deg (or, for a circuit, any point within 30 m: grid slots are behind the line); then keep
projecting positions onto each candidate (distance to polyline, x/z grid index, y to tell
over/under passes apart) and lock when one candidate stays within ~25 m for 600 m while the
others drift away (shared openings, as LapScope found on Legend Island/Goliath, need up to
~1.8 km). Unlocked runs fall back to today's `start_cell` key plus bounding-box/length
fingerprint, and are re-matched by the backfill when the table grows.

## 4. Mapping to Oversteer features
- **Live / rev lights / shift learning:** work today (EngineMaxRpm, rpm, gear, power).
  Shift point from the game's power figure; the R class needs the letter map only.
- **Runs / run view:** needs 3.1 boundaries; free roam must not create runs (today
  `running` = IsRaceOn, which is 1 in free roam).
- **Splits (FH has no sectors):** route-derived. For a matched route: split lines at
  equal thirds of its length (S1-S3 as for the other games), placed on the route polyline;
  better, at the three longest straights' ends or the corners found by `find_corners` on
  the route geometry so splits fall where time is decided. For a circuit, also per lap.
  Times from crossings of the projected along-route distance (as ACR does with
  `lap_distance`). Unmatched: thirds of the run's own integrated distance, re-placed once
  matched.
- **Coach / potential / corner diagnosis:** need along-route distance (from projection),
  surface (route kind from catalogue, or `drive_detect` roughness: dirt shakes the
  suspension, cross-country jumps), and car identity incl. PI. The driving line value
  (+-127 = off the course) is a free "off track" signal for track-limit coaching. Grip
  level per car+surface as for the other games.
- **Contact/landing:** SmashableVelDiff/Mass mark smashable hits (FH6-only, nice for
  "you lost 4 km/h to a fence"); wall contact by acceleration jumps with the jump-landing
  exclusion (all four wheels at full droop).
- **PBs:** key = route + car (ordinal + PI class); PB rules unchanged.

## 5. Phased plan (Sonnet-ready specs)
**Phase 0 - first capture (marth, 20 min).** Section 7. Gate for everything below.

**Phase 1 - decoder + car names (sonnet-coder; tier C; Opus review).**
- `telemetry_formats._forza`: for `forza-fh`, `sample.progress = DistanceTraveled`;
  `sample.distance = None` (RunTracker integrates speed when distance is None; check it does
  for OutGauge already); car class letters `D C B A S1 S2 R X`; `sample.car_name` from the
  table; `sample.smash = (vel_diff, mass)`; `sample.race_position` already.
- New `data/telemetry/cars/fh6-ordinals.json` (from LapScope MIT copy + gist refresh) and
  `scripts/fh6-cars.py` (based on `research/fh6/fh6_cars.py`, `--fetch`, `--install DIR`).
  `car_data.py` gains `ordinal_name(game, ordinal)`.
- Tests: a synthetic 324-byte packet per class/drivetrain; unknown ordinal fallback; a
  name rename keeps the car key.
- After the capture: fix every `verify` sign in `_forza` against it; add a cut of the
  capture as a test fixture (`telemetry-replay.py --cut`).

**Phase 2 - runs and routes (sonnet-coder; RunTracker is a mechanism: Opus spec + review).**
- `drive_log.RunTracker`: FH6 run start = RacePosition>0 or a DistanceTraveled launch from
  0, not IsRaceOn; finish signals per 3.1 (LastLap change with LapNumber still; stream gap
  > 2 s within 60 m of the matched route end; odometer-reset hand-back with the car still);
  restart = race clock back to ~0; rewind trimming; WTA laps by return to launch point.
- `scripts/stage-tables.py fh6`: reads AITracks read-only, writes `stages/fh6.json` (entry
  per route as 3.2, decimated polyline, catalogue names + licence/sources).
- `oversteer/route_match.py`: candidates + projection + lock as 3.2; unit-tested with the
  shipped polylines and a synthetic drive along them (offset 0-20 m, noise, shared start).
- Backfill re-matches stored FH6 runs after a table update (like `requeue_moved_stages`).

**Phase 3 - splits and coaching (sonnet-coder; Opus review).**
- Split lines per route (thirds, then corner-aware), crossings from projected distance,
  S1-S3 in live/run views and the web page; PB compare per route + car class.
- Coach: off-line (driving line) and smashable losses as findings; surface from route kind
  or roughness; potential per route as for stages.

**Phase 4 (optional).** Name more routes (marth's runs never need labels: a name only
comes from a public catalogue or the game); car engine data from `physicsdefinition.bin`
(Fable RE only if wanted); challenges (speed traps/zones) if their positions are found.

## 6. Risks
- A title update can move fields or route files: decoder checks size; generators are
  re-runnable on a new install.
- DistanceTraveled semantics, finish signatures and signs come from another project's
  captures: re-verify on marth's.
- Third-party data licences: LapScope MIT (ship with attribution); HDR gist unlicensed
  (fetch at refresh, or ask); the other crosswalk (no licence) is never shipped.

## 7. First capture: instructions for marth
1. Oversteer (Flatpak): Telemetry tab: UDP port **5310**, "Learn from game telemetry" on,
   **"Record raw telemetry" on**. Save the profile you use for FH6 (port is per profile).
2. FH6: Settings > HUD and Gameplay: **Data Out: On**, **Data Out IP Address: 127.0.0.1**,
   **Data Out IP Port: 5310**. (Never 5200-5300.)
3. Drive, in this order, without closing Oversteer:
   - 30 s of free roam, including one clear left turn and one constant-speed hill;
   - one road **circuit** race (2+ laps), finish it; then restart it once from the pause
     menu after ~30 s and finish again;
   - one **sprint** (road or dirt), finish it; use one rewind mid-way;
   - one **cross-country** or touge if handy; one World Time Attack lap if handy.
   Note roughly which events you drove (the name on screen) - only to check matching, not as
   labels.
4. The captures are in `~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures/`
   (`*.ovcap.gz`); the live line on the Telemetry tab should show `forza-fh` 324-byte packets
   while driving. If nothing arrives, check the port in the saved profile.

## Sources
- Forza Support, "Forza Horizon 6 Data Out Documentation",
  https://support.forza.net/hc/en-us/articles/51744149102611 (2026; not fetchable by the
  research agent, content via the mirrors below).
- Ritze03/ForzaTelemetryV3, `docs/protocol/forza-fh6-packet-format.md` (transcription of the
  official doc; read 2026-10-26).
- darcane/LapScope (MIT, pushed 2026-09-13): `docs/wiki/FH6-Data-Out-Packet.md`,
  `docs/wiki/Event-Detection.md`, `app/car_ordinals.json`, `app/track_catalog.json`.
- HDR, "Forza Horizon 6 Car Ordinals" gist 0659d1717bc61504bf83750628963f4f (as of
  2026-10-13, updated 2026-10-22).
- RacingDSX guide (github.com/barnacules/racingdsx): Steam build needs no loopback exemption.
- SHWotever/SimHub issue 2267: ~128-140 packets/s while driving.
- GamingOnLinux, 2026-05: FH6 on Proton (Proton Hotfix default).
- The FH6 install, read-only (`media/Cars`, `media/OpenWorld/Brio/AITracks`,
  `media/Stripped`), 2026-10-26.
