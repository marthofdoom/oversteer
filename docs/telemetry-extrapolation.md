# Telemetry extrapolation: what more the data can tell

Research of 2026-10-05 (branch `launch-limiter`). The question: how much
can be worked out from what the games already send, beyond what the coach
measures today, preferably in ways that work in more games than ACR, with
ACR first. Everything here was checked on marth's own captures. The
prototypes are in `research/extrapolation/`. They read the captures
read-only through the app's own `read_capture` and `decode_sample`, write
only derived numbers and small plots to `research/extrapolation/out/`, and
cache decoded samples outside the repository (`$EXTRAP_CACHE`, default
`~/.cache/oversteer-extrapolation`). No application code was changed.

The companion documents are `docs/coaching-derivations.md` (the
derivation database: channels, games, aspects), `docs/coach-techniques.md`
(what a coach needs, §5 "not measured yet") and
`docs/telemetry-ui-design.md` (phases 2–4).

## 0. Summary and ranking

Ranked by value to the owner times how ready it is. "Validated" means
against a ground truth on real data. The truths used are the game's own
files, the game's pace notes, positions, the tyre loads and the game
clock.

| # | Derivation | Validation on marth's data | Games |
|---|---|---|---|
| 1 | **Potential time** per stage and per section, in three layers (user < grip < car), with the leaderboard target mode | Afon Bidno, i20N: PB 187.7 s, sum of best 183.1 s, grip potential 171.4 s, car potential 162.7 s. The leaderboard (about 173 s) falls between the grip and car layers. The potential is faster than the best pass in 22 of 22 sections. Hindcast: built only from the slow sessions (PB 196.2 s), it gave 172.8 s, and its per-section "time available" correlates with the time the later PB actually found (r 0.72, Spearman 0.57) | any game with speed + yaw rate or positions |
| 2 | **Grip used per tyre and per axle** from ACR's tyre forces, with the generic g-g estimate checked against it | Per-tyre peak μ: 1.5–1.7 on gravel, 0.9–1.5 on tarmac by car. The g-g estimate tracks the forces at r 0.69 over time and r 0.54 per corner | forces: ACR, ACC; g-g: all |
| 3 | **Stage geometry**: map, curvature, corners with grade and apex, from yaw (no positions) or positions | Corner notes matched: 88–100 % of grades 1–4 and hairpins. Apexes 30–45 m after the call. Radius ranks with the note grade (Spearman 0.47–0.79). The yaw-built curvature agrees with the positions (r 0.998 Sommet, 0.86 Aghii; potential differs by 0.3 s and 1.3 s). Dead reckoning drifts 1.1–1.6 m/km on Sommet | all; positions: WRCG, DiRT, Forza, EA WRC, ACR from v4 |
| 4 | **Offs and crashes**: detection, cause, advice, frequency | Detection precision 0.86 (v1) to 1.00 (v2). Cause accuracy 0.59 in sample but 0.20 on blind held-out offs (0.33 counting the second-choice cause). The detector works; the cause classifier does **not** generalise yet (§5) | all; best with slip angle |
| 5 | **Drivetrain identification**: gear ratios, which gear set the setup uses, rolling radius, torque shape | Ratios within 0.06–0.19 % of the game files. The gear set was named correctly on every run (i20N: 6 runs on Set0, 13 on Set1). Torque from the forces: shape r 0.97–0.99, level 13–21 % under the files. Torque from acceleration alone: shape r 0.96 on the i20N, unusable on the Fabia and 208 | ratios: all; forces: ACR, ACC |
| 6 | **Braking zones**: onset, peak, release, trail share, lock-ups, consistency | 1658 ACR zones. Onset spread per place 7–9 m inter-quartile on Afon Bidno. Lock-ups confirmed by ACR's own wheel slip (0.74 against 0.25). The generic peak deceleration agrees with the forces at r 0.88 | all with brake + speed; lock-ups: wheel speeds |
| 7 | **Jumps and landings** | Free fall from a_z: recall 0.78, precision 0.87, against the tyre loads (46 flights). Landing load (median 2.1 g, up to 4.0 g) is **not** recoverable from a_z (r 0.03) | a_z: ACR, Forza, EA WRC |
| 8 | **Corner critique**: wrong gear, revs under the band, losing the rear, each with its cost | i20N on Afon Bidno, 183 passes: a gear shorter than the fastest pass costs 0.17 s (CI 0.04–0.29), revs under the band on the exit 0.19 s (0.07–0.32), and counter-steer 0.13 s per s (−0.15–0.35), with apex speed held as a control | all |
| 9 | **Game clock and the exact finish** (bridge v4) | The receive clock agrees with the game clock within 0.4 s per run. The clock stops 19–28 m after the "Finish" pace note and 193–319 m before the table's `pacenote_last_m` | ACR v4, WRCG, DiRT, EA WRC, Forza |
| 10 | **Elevation**: from positions, or from the tyre forces without them | From positions: climb, descent, minimum and maximum within 1 m of the WRCG game files on 3 of 4 stages. ACR start heights within 0.2 m of the stage table. Force grade: shape r 0.5–0.75 against the height, absolute climb not usable (errors of 87–228 m over a stage) | positions: most; forces: ACR |
| 11 | Line and road use from positions (track limits) | ACR spline distance is an exact road coordinate (along-track misfit p95 0.04–0.10 m). marth's line repeats to 0.24–0.30 m median spread; the road-used envelope is 0.6 m median (2–3 runs) | positions |

**What needs marth to drive** (§7): more ACR runs with positions on one
stage (5+, for track limits and line coaching), a finished Afon Bidno on
bridge v4 (to anchor potential time to the game clock and the true
finish), and a few runs with a known setup change (to validate setup
inference on the forces).

## 1. Inventory: what each game actually sends

From `research/extrapolation/inventory.py` (`out/inventory.txt`), over
every capture: 18 ACR captures (295 min, 1.08 M packets, 9 stages, 4
cars) and 3 WRC Generations captures (24 min, 4 stages). **There are no
captures of AC, ACC, DiRT, EA WRC, Forza, BeamNG or LFS**; for those the
decoder's channel list (`docs/coaching-derivations.md`) is all there is.

### ACR (OVST v3, and v4 from tonight's captures)

- **Packets**: 61.7 a second (median interval 16.2 ms, p95 16.7 ms). 111
  gaps over 0.1 s in 295 min. 1.3 % of consecutive moving packets repeat
  the rpm. There is **no packet clock before v4**, and the receive clock
  jitters, so d(speed)/dt on it is noisy (r 0.77 against the forces), while
  the car-frame acceleration is clean (r 0.98). Derive accelerations from
  `accel`, not from the speed.
- **Present and varying**: rpm, max_rpm, gear (−1..5), speed, throttle,
  brake, clutch (1 % of moving time not 0), steer, yaw_rate, lap_distance
  (spline distance), stage_length, vel (3), accel (3, kinematic; spikes to
  −4090 m/s² at impacts, so clip at about 25–40), wheel_rot (4), susp (4,
  m: −0.024..0.30).
- **Present in the packet, decoded since 2026-10-05** (`Sample.tyre_load`,
  `tyre_fx`, `tyre_fy`, `tyre_slip`, `brake_bias`, `ang_vel`; not yet in the
  trace, so not in a run's stored data): grip per tyre is now decodable
  (peak μ 1.5–1.7 on gravel, §4.4):
  - `wheel_load` (N, 4): Σ/g = 1250–1460 kg by car at speed.
  - `fx` and `fy` (N, 4). Σfx = m·a_x with r 0.97–0.99 and Σfy = m·a_y with
    r 0.98–0.995, in the car frame: fx forward, fy positive left.
  - `wheel_slip` (4, unnormalised; p95 0.2–1.8).
  - `brake_bias` (0.5–0.6).

  Load transfer has the right signs: braking loads the front, a left turn
  loads the right wheels.
- **Zero or empty**: ride height, tyre radius, suspension max travel,
  surface grip, session. ACR fills none of them.
- **pos (x, y, z)**: in the v4 captures of 2026-10-05 20:27 onward, on
  every packet. y is the height: the start heights 708.0 m (Aghii) and
  883 m (Sommet) match the stage table's `elevation_start_m` (707.8 and
  881.9). The plan view is **(x, −z)**: the world is left-handed, and
  taking (x, z) mirrors the map and flips every curvature's sign. The
  older v3 captures have no position.
- **stage_time** (v4): the game's own clock. It stops at the flying
  finish (§4.9).
- **lap_distance** is the projection on the game's road spline. Two runs
  at the same distance are at the same point along the road to within
  0.04–0.10 m (p95), so the lateral offset between runs is a clean
  measure. The driven path is 0.992–0.997 of the spline distance.

### WRC Generations (Codemasters layout, 280 bytes = 70 floats)

- **Packets**: 64 a second with a game clock (no gaps).
- **Present**: rpm, max/idle rpm (unit decided by `codemasters_unit`),
  gear, gears, speed, throttle, brake, clutch, steer, lap_distance,
  stage_time, game_time, pos (z is the height here), the two direction
  vectors, wheel_speed (4; their median is 0.92–0.97 of the speed),
  susp (4) and susp_vel (4).
- **Problems found** (all fixed in the decoder, 2026-10-05: forward negated, steer positive left,
  susp in metres and negated for compression, floats 34/35 dropped, so `accel` is None; the 2D
  maps use `telemetry_formats.plan_xy`):
  1. The decoder's `forward` vector points **backwards**: its median
     cosine with the direction of motion is −0.99, so `vel` comes out with
     x ≈ −speed. Heading has to come from the positions.
  2. Floats 34 and 35 (decoded as lateral and longitudinal g) do not
     match the motion. 35 correlates with d(speed)/dt at r −0.45, at its
     best with a 10-sample (~0.16 s) lag; 34 correlates with neither.
     Nothing here confirms their meaning.
  3. `steer` correlates **negatively** with the heading rate from the
     positions (r −0.37). That is the counter-steer anomaly already noted
     in `coaching-derivations.md`.
  4. `susp` reads 0.00028–0.00056 m because the decoder divides by 1000
     a value that looks like it is already in metres (0.28–0.56).
  5. Floats 66–69 are constant (0, 0, −1, −1) and 38–50 constant. 62 is
     constant −9327 in-stage.
- **Derived instead**: heading, curvature, yaw rate, body slip and
  vertical acceleration all come from the positions, which are good
  (|dpos/dt| / speed = 1.006–1.012).

### Games without captures

Forza (sled + dash), DiRT Rally 2.0, EA WRC, AC/ACC (OVST v3) and
OutGauge are decoded from their documentation only. Every method below
says which channels it needs, and §6 says what each game would get.

## 2. Potential time: where the time is (the top item)

**Why.** The owner was 15 s off the top leaderboard time and the coach
"couldn't tell me where to gain time". The coach compares only against the
driver's own runs, so a driver who is consistently slow gets nothing. The
potential-time model gives every section a time the car could do, from
the driver's own data, so the gap exists even when every run is equally
slow.

**Prototype**: `research/extrapolation/lapsim.py`.

### 2.1 Method: a quasi-steady-state lap simulation

1. **The road.** Build the course curvature k(d) on a 2 m grid of the
   spline distance. k = (yaw rate + d(body slip)/dt) / v, taking the
   median over every run of the stage (any car) and smoothing over 10 m.
   With positions, use the path's own curvature, dθ/ds of the (x, −z)
   plan view, instead.
2. **The grip.** Take the driver's g-g envelope over every run of the car
   on the stage's surface. In 2.5 m/s speed bins, it is the P-th
   percentile of |a_lat|, of −a_long while braking, and of a_long at full
   throttle. The drive envelope is made non-increasing with speed, as it
   is power-limited. The three combine as a friction ellipse:
   (a_x/a_x,max)² + (a_y/a_y,max)² ≤ 1.
3. **Corner limit.** v_lim(d) = √(a_lat,max(v)/|k|), solved by fixed
   point. It is never below the fastest speed any of the car's runs reached
   at d ("the car has done it there"). Without that floor, 2 sections came
   out slower than the best pass, because the median curvature is a little
   tighter than the fastest line.
4. **Passes.** A forward pass at the drive envelope and a backward pass at
   the braking envelope, each limited by the lateral grip the curvature
   already uses. The potential time is ∫ ds/v.
5. **Sections.** Cut between apexes, at the straightest point, merging
   any section under 120 m. These are cut on the road geometry from every
   run, so a hindcast keeps the same sections.
6. **Per section**:
   - the time in each layer and the PB's time;
   - the time available (PB minus potential);
   - the grip used at the apex: the PB's |a_lat| at its slowest point over
     a_lat,max at that speed, and, with ACR forces, the force-based usage;
   - the loss split into entry (start to 10 m before the apex), apex
     (±10 m) and exit, as ∫(1/v_pb − 1/v_pot) ds over each;
   - the cause: the largest of the three.

### 2.2 Three layers (owner's request)

| Layer | Meaning | How |
|---|---|---|
| **user** | What the driver has already done | The sum of the best pass through every section (stitched, all runs) |
| **grip** | What the driver's own grip allows | Lap sim at P98 of the driver's g-g on the surface |
| **car** | What the car can do | Lap sim with the highest grip any run of the car reached on the surface (P99.5 of lateral and braking g, held flat under 8 m/s, where the extreme is spins and knocks) and the drive from the game's car data, capped by the best traction seen and never below what any run showed. The drive is the torque curve through the gear set the runs used (identified per run, §4.5), the measured rolling radius (0.326 m) and the measured mass (Σ wheel load / g = 1400 kg for the i20N), less the drag measured against positions: g · (0.0238 + 9.3·10⁻⁵ v²). |

The ordering user ≥ grip ≥ car holds in every section of every stage
tried (the check is in the script).

### 2.3 Results: Afon Bidno - Severn, Hyundai i20N Rally2 (gravel)

The six finished runs, timed from the start line (238.5 m along the
spline) to the "Finish" pace note (5277 m): 196.2, 198.0, 198.1, 199.6,
188.4 and 187.7 s. The game clock stops about 25 m after the note (§4.9),
so the PB is about 188.3 s on the game clock, which matches the owner's
"≈188 s". The leaderboard top is about 173 s.

| Layer | Time |
|---|---|
| PB (captured) | 187.7 s |
| user (sum of best) | 183.1 s |
| **grip (P98)** | **171.4 s** |
| car | 162.7 s |
| leaderboard top (owner, approximate) | ≈ 173 s |

Sensitivity to the grip percentile (with the floor): P95 gives 176.1 s
(1 section not faster than the best pass), P98 171.4 s (0), P99 167.3 s
(0). P98 is the lowest percentile at which the potential beats every
section, and it lands within 2 s of the leaderboard. That is the
calibration: the fastest drivers use about the grip marth reaches 2 % of
the time, all the time. Without the floor, P98 gives 173.7 s with 2
sections slower than the best pass.

**Hindcast** (`--before 20261003-193057`): built only from the 4 slower
runs and the sessions before them (PB 196.2 s), the model gives:
- user 187.1 s, which predicts the 187.7 s PB of the next session;
- grip 172.8 s (against 171.4 s with everything), so the grip layer does
  not depend on how fast the driver currently is;
- car 164.5 s.

Its per-section "time available" against the time the next session
actually found per section: Pearson 0.72, Spearman 0.57. The 5 sections
it ranked highest held 39 % of the distance and 4.2 of the 8.6 s gained.
This is the evidence that "time available" points at the right places.

Per section, the P98 run with the floor (`out/lapsim_bidno_i20n_p98.*`).
km are from the start line. The *gap* column is the target mode for
173 s (§2.4):

| km (start line) | Corner | R m | car s | grip s | user s | PB s | **available s** | apex km/h PB / pot | grip used at apex | where | gap share s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0–206 | 4 right | 76 | 8.51 | 8.87 | 9.19 | 9.42 | **0.55** | 75 / 96 | 8 % | apex | 0.50 |
| 206–334 | 3 left | 26 | 6.20 | 6.67 | 7.55 | 7.55 | **0.88** | 41 / 53 | 83 % | apex | 0.79 |
| 334–540 | 5 right | 82 | 6.60 | 6.96 | 7.40 | 7.82 | **0.86** | 83 / 102 | 58 % | entry | 0.78 |
| 540–730 | 1 left | 12 | 7.45 | 8.16 | 8.73 | 9.04 | **0.88** | 30 / 39 | 68 % | entry | 0.79 |
| 730–928 | 4 left | 54 | 6.96 | 7.29 | 7.91 | 7.91 | **0.62** | 73 / 88 | 83 % | entry | 0.56 |
| 928–1076 | 4 right | 57 | 5.34 | 5.64 | 5.85 | 5.85 | **0.21** | 79 / 81 | 85 % | exit | 0.19 |
| 1076–1202 | 3 right | 40 | 4.87 | 5.19 | 5.60 | 5.88 | **0.69** | 68 / 70 | 76 % | entry | 0.62 |
| 1202–1534 | 3 right | 45 | 10.24 | 10.73 | 11.63 | 12.40 | **1.67** | 56 / 75 | 49 % | exit | 1.51 |
| 1534–1738 | 3 right | 43 | 7.68 | 8.05 | 8.83 | 8.83 | **0.78** | 70 / 77 | 77 % | exit | 0.70 |
| 1738–1876 | 4 left | 61 | 4.48 | 4.74 | 5.23 | 5.47 | **0.74** | 61 / 86 | 77 % | apex | 0.67 |
| 1876–2138 | 4 left | 60 | 8.35 | 8.78 | 9.53 | 10.36 | **1.58** | 60 / 85 | 79 % | entry | 1.43 |
| 2138–2450 | 5 left | 81 | 8.75 | 8.99 | 9.40 | 9.63 | **0.64** | 99 / 103 | 138 % | exit | 0.58 |
| 2450–2586 | 1 right | 9 | 6.96 | 7.60 | 8.17 | 8.51 | **0.92** | 19 / 31 | 74 % | entry | 0.83 |
| 2586–2718 | 1 left | 8 | 8.69 | 9.67 | 10.03 | 10.03 | **0.36** | 26 / 31 | 71 % | exit | 0.32 |
| 2718–2886 | 4 right | 46 | 6.08 | 6.48 | 6.95 | 6.95 | **0.47** | 64 / 72 | 10 % | exit | 0.42 |
| 2886–3018 | 3 left | 38 | 5.75 | 6.17 | 6.46 | 6.64 | **0.46** | 56 / 67 | 88 % | entry | 0.42 |
| 3018–3202 | 5 right | 86 | 5.60 | 5.88 | 6.24 | 6.24 | **0.36** | 83 / 88 | 18 % | exit | 0.32 |
| 3202–3352 | 5 left | 121 | 4.02 | 4.13 | 4.36 | 4.36 | **0.23** | 117 / 131 | 50 % | exit | 0.21 |
| 3352–3528 | 5 right | 81 | 4.92 | 5.00 | 5.05 | 5.07 | **0.07** | 108 / 110 | 36 % | entry | 0.06 |
| 3528–3878 | 5 left | 124 | 8.50 | 8.93 | 9.43 | 9.96 | **1.03** | 97 / 115 | 52 % | exit | 0.93 |
| 3878–4162 | 4 right | 60 | 7.03 | 7.26 | 7.86 | 8.04 | **0.78** | 87 / 98 | 59 % | entry | 0.70 |
| 4162–5038 | 6 left (to the finish) | 145 | 19.68 | 20.22 | 21.71 | 21.71 | **1.49** | 104 / 112 | 2 % | exit | 1.34 |

The km in the first column are distances from the start line along the
spline. The grade is the coach's radius scale (`coach_context.RADIUS_GRADES`).
§4.3 shows those radii against the game's notes.

Where the grip used at the apex is low (8 %, 2 %, 10 %), the "apex" is a
kink in a fast section. There the number means nothing and the call
leaves it out. 138 % means the PB ran past its own P98 envelope there.

**Other stages, with curvature from the positions** (v4 captures):

- **Sommet de Munster** (i20N, tarmac, 3 runs on the game clock: 171.6,
  169.8 and 162.0 s): user 160.9, grip 148.3, car 143.7 s. From the yaw
  rate instead of the positions: 148.6 s. The curvature correlates at
  r 0.998 and the median section differs by 0.008 s.
- **Aghii Theodori - Loutraki** (Polo GTI R5, gravel, 2 runs with offs:
  435.9 and 426.6 s): user 410.5, grip 379.5, car 355.2 s. From the yaw
  rate: 380.8 s (r 0.86, median section difference 0.02 s). 3 of 55
  sections were not faster than the best pass, all at the offs, where the
  floor and the median curvature disagree.

**So the yaw-rate reconstruction is good enough for potential time** in
every game that sends yaw rate or positions. Positions improve it only
slightly.

### 2.4 Leaderboard target mode

Given a target T, the gap G = PB − T is shared over the sections in
proportion to the time available in each (negative availability counts as
0). Afon Bidno, T = 173 s: G = 14.7 s against 16.3 s available, so the
target asks for 90 % of the available time everywhere. That is plausible:
it sits between the grip and car layers. For each section, the speed
needed is the PB's speed profile scaled by f = t_pb / (t_pb − G_i). The
call then gives the extra apex speed, v_apex·(f − 1), and the grip that
speed would use, (grip used)·f². When that comes out over 100 %, the
section's share is not reachable by carrying speed and the call has to
say "earlier on the throttle" or "brake later".

The calls the prototype writes (top 6 by share), in the coach's style:

> The 3 right at 1.3 km: you use 49 % of the grip; about 1.5 s is there. Get to full throttle sooner after the apex.
> The 4 left at 2.1 km: you use 79 % of the grip; about 1.4 s is there. Brake later and carry the speed to the turn-in.
> The 6 left at 4.2 km: about 1.3 s is there, mostly on the exit: full throttle sooner and longer.
> The 5 left at 3.6 km: about 0.9 s is there, mostly on the exit: full throttle sooner and longer.
> The 1 right at 2.5 km: you use 74 % of the grip; about 0.8 s is there. Brake later and carry the speed to the turn-in.
> The 3 left at 0.2 km: you use 83 % of the grip; about 0.8 s is there. Carry 5 km/h more through the apex.

On Sommet (T = 150 s) the top call is "The 1 right at 1.8 km: you use all
of the grip; about 2.4 s is there. Get to full throttle sooner after the
apex."

The corner names should come from the game's pace notes where the
coach has them, which is ClickUp 86e3faftn. §4.3 shows those notes line
up with the corners found here.

### 2.5 Limits

- The envelope mixes uphill and downhill. Drive and braking are not
  corrected for grade (the force grade is too biased; positions would
  allow it, §4.10). Gear-change time is not modelled (optimistic by
  roughly 0.1 s per change). The line is taken as the median driven line,
  not optimised.
- "Cause" is the phase where the potential is faster. It says *where* in
  the corner the time is, not *why* (that is §4.8 and §5).
- The car layer's mass and drag are measured, but its drive is 13–21 %
  above what the forces say the engine delivered (§4.5). It is a ceiling,
  not a target.

## 3. Catalogue of derivations

Each entry gives what it tells the driver or coach, its inputs, the
method, the games it works in (and how it degrades), its validation,
confidence and cost. "Confidence" is about the method as validated here.
"Cost" is the work to add it to the app: S is under a day, M a few days,
L more.

| Derivation | Tells | Inputs | Method | Games and degradation | Validation | Conf. | Cost |
|---|---|---|---|---|---|---|---|
| Potential time (3 layers) and target mode | Where the time is, even for a consistently slow driver | speed, yaw_rate or pos, a_long, a_lat, spline distance; car data for the car layer | §2 | All with yaw or positions. Without accelerations, a_lat = v·yaw and a_long = dv/dt on a game clock. Without car data, no car layer | §2.3 | high (grip/user), medium (car) | M |
| Stage map | The minimap; places by shape | pos, else yaw_rate + vel + speed | (x, −z) for ACR, (x, y) for WRCG; dead reckoning ψ = ∫r dt + β | All. Without positions, dead reckoning drifts 1.1–1.6 m/km on tarmac and 8–10 m/km on 10 km of gravel (slides) | §4.1 | high | S |
| Elevation profile, gradient, crests and dips | Blind crests, climbs, where to expect landings | pos height; else a_z/v² (vertical curvature) and ACR's force grade | Height on a 5 m grid, smoothed 15–30 m; grade = dh/ds; κ_v = d²h/ds² | Positions: high. Without: shape only (a_z/v² r 0.34–0.61 against the height; force grade r 0.4–0.75); the absolute level is unusable | §4.10 | high / low | S |
| Corner geometry | Radius, length, apex, tightens or opens, note-like grade | yaw or pos, spline distance | §4.3 | All | 0.88–1.00 recall on grades 1–4 and hairpins | high | S |
| Racing line, lateral offset, road-used envelope | Line consistency, apex offset, how much road is used | pos + spline distance (ACR), else nearest point on a reference line | Offset along the normal of the median line of all runs | Positions only | §4.11 | high (offset), pending (edges) | M |
| Drift / body slip angle | Set slide, power slide, a caught slide | vel (car frame), else heading from positions minus course | β = atan2(v_y, v_x) | ACR (vel). WRCG via positions + forward once its forward vector is fixed. Forza, EA WRC decoded | Used in §5 and §4.8 | medium | S |
| Tyre and wheel slip | Wheelspin, lock-ups | wheel_rot × radius (ACR, Forza) or wheel_speed (Codemasters, EA WRC); ACR `wheel_slip` | slip = ωr/v − 1, r from coasting rows (0.317–0.330 m) | All with wheels | Lock-ups agree with ACR's own wheel slip (0.74 / 0.25) | high | S |
| Grip usage per tyre and axle; balance by axle | % of grip per corner, understeer/oversteer at the axle | ACR, ACC: fx, fy, wheel load | μ = √(fx²+fy²)/fz over the tyre's P99 | Forces: ACR, ACC. Everyone else: g over the g-g envelope (r 0.69 over time, 0.54 per corner, against the forces) | §4.4 | high (forces), medium (g-g) | M |
| g-g envelope and traction circle | Grip level per surface; a_lat,max(v) | a_long, a_lat, speed | Percentiles per speed bin | All | Basis of §2 | high | S |
| Braking analysis | Onset, peak, build, release, trail, lock-ups, consistency | brake, accel, speed, yaw, wheels | §4.6 | All with brake | §4.6 | high | S |
| Throttle application | Pickup point, ramp, wheelspin on exit | throttle, slip, apex | Time from the slowest point to 95 %; slip while > 60 % | All | Part of the §2 loss split and §5 "power" | medium | S |
| Gear / shift timing vs power band | Wrong gear, exit revs under the band | gear, rpm, torque curve or learnt band | §4.8 | All | §4.8 | medium | S |
| Engine torque curve | The band, crossover shift points for games without car data | rpm, gear, a_long (or forces), mass | Least squares over full-throttle rows | Forces: shape r 0.97–0.99. Accel only: works on the i20N (2.6 % after scaling), fails on the Fabia and 208 | §4.5 | medium | M |
| Gear ratios, gear set, tyre radius | Setup inference (gearing), the drivetrain check | rpm, wheel spin or speed | rpm / driven-wheel spin per gear | All. Without wheel spin, rpm/v gives ratio over radius | 0.06–0.19 % against the files | high | S |
| Surface and grip level | Which surface, grip change within a stage | per-tyre μ peak; g-g; wheel slip vs force | The peak μ per window against the stage's | Forces: gravel 1.5–1.7, tarmac 0.9–1.5 by car. g-g P98 per run: gravel 0.92–1.08 g, tarmac 0.82–1.38 g, overlapping | Per surface (§4.4); within-stage change not yet tried | medium | M |
| Jumps and airtime | Flights, airtime, landings | a_z (free fall), susp at droop, ACR loads | §4.7 | a_z: recall 0.78. Suspension alone: 0.35. Landing load needs the loads | §4.7 | high (flight), low (landing) | S |
| Offs, crashes, resets, rolls | Incidents, why, how often | speed, gear, a, β, loads, positions | §5 | All | §5 | high (detect), low (cause) | M |
| Roll-over and car-on-side | A crash marker | ACR loads: all four under 50 N without free fall | §4.7 | ACR, ACC | 8 found, all at known crashes | high | S |
| Consistency by section | Spread per place; where the driver is unsure | section times, brake onsets | IQR across runs | All | Onset IQR 7–9 m (§4.6) | high | S |
| Time-loss attribution | Entry, apex or exit | potential or reference speed | ∫(1/v − 1/v_ref) ds per phase | All | §2.1 | medium | S (exists in part as `corner.loss`) |
| Predicted best (user layer) | The realistic next PB | section times | Sum of best | All | Hindcast 187.1 s predicted 187.7 s | high | S |
| Fatigue and pace within a stage | First third against last | section times against potential | Ratio of time to potential by third | All | Not prototyped (few long stages) | — | S |
| Setup inference | Gear set; springs and ride height | gear ratios; susp and load | §4.5; susp/load slope per wheel | Gear set: validated. Springs: susp is in m and loads in N on ACR, so k = ΔF/Δx per wheel is measurable; not tried | partial | medium | M |
| Weather and grip change | Wet, drying, snow | μ peak per km; puddle (Forza) | Drift of the per-km μ peak against the stage median | Not tried; ACR sends no weather | — | — | M |
| Mass and drag | Car layer, grade | Σ wheel load, Σfx − m·a against the height | §2.2 | ACR, ACC | Drag g·(0.024 + 9.3·10⁻⁵ v²) from positions | medium | S |
| Game clock and exact finish | Official-equivalent times, exact `finish_m` | stage_time (v4), spline distance | §4.9 | ACR v4, WRCG, DiRT, EA WRC, Forza | §4.9 | high | S |

## 4. Validation of the other prototypes

### 4.1 Map by dead reckoning (`map_elevation.py`, `acr_positions.py`)

- **ACR against positions** (v4): rigid-aligned RMS error 6.5–9.1 m on
  the 5.7 km Sommet runs (1.1–1.6 m/km) and 79–106 m on the 10.4 km Aghii
  gravel runs (7.7–10 m/km: big slides and offs). The median over every
  run, partials included, is 1.4 m/km. The 1.3 km loop of the Livigno
  circuit closes within 1.3 m.
- **Without positions**, 11 Afon Bidno runs dead-reckoned and aligned
  agree within 4–17 m RMS of their median (`out/map_acr_dead_reckoning.png`).
  One run is off by 218 m (an off with a recovery).
- **WRCG**: positions are good. The plan view is (x, y) with z up. The
  decoder's convention says y up; that does not hold for WRCG.

### 4.2 Elevation (`map_elevation.py`, `acr_positions.py`)

- **WRCG from positions against the game's own stage table**:

  | Stage | min / max (m) | table | climb / descent (m) | table |
  |---|---|---|---|---|
  | Media Luna | 272 / 338 | 271 / 338 | 100 / 84 | 98 / 114 |
  | León | 102 / 106 | 102 / 106 | 3 / 2 | 6 / 6 |
  | Monti di Ala | 205 / 409 | 204 / 408 | 434 / 463 | 492 / 463 |
  | Lerno | 254 / 373 | 250 / 372 | 134 / 215 | 224 / 141 |

  Minimum and maximum agree within 1 m (Lerno within 4 m). On Lerno the
  climb and descent look swapped: the stage was probably driven in the
  table's reverse direction. Climb totals depend on the hysteresis (1 m
  here).
- **ACR start heights**: 708.0 m against the table's 707.8 m (Aghii), and
  about 883 m against 881.9 m (Sommet).
- **ACR without positions**, the grade from the tyre forces,
  sin θ = (Σfx − m·a_x)/(m g):
  - the shape repeats across 11 Afon Bidno runs (pairwise r 0.90 median,
    0.76 minimum);
  - against the height it correlates at r 0.41–0.75;
  - it carries the rolling and aero drag as a bias, fitted against
    positions as 0.024 + 9.3·10⁻⁵ v² (in units of g);
  - removing that bias with a model fitted on the other stage still leaves
    87–228 m of error in the net climb.

  **Verdict**: elevation needs positions. The force grade is only good
  for the relative shape.
- **Vertical curvature from a_z/v²** against the height's: r 0.34–0.63,
  slope 1.1–1.8 (it sees the suspension too).
- **Crests** against the notes' OverCrest and Jump calls: recall 0–0.5 on
  ACR from a_z (0.24 overall at R < 250 m, against 0.12 at random). Even
  from the true height, the recall is only 0.17 (Sommet) and 0.36
  (Aghii). The game's crest calls are about visibility and commitment, not
  sharp vertical curvature. For crests, **use the notes**; use a_z for
  airtime (§4.7).

### 4.3 Corners and pace-note grade (`corners.py`)

Corners are stretches tighter than a 400 m radius, of at least 15° or
under 100 m radius. They were matched to the game's corner notes
(Koenvh1/PacenotePal's MPL-2.0 extraction of DT_Pacenote, used as ground
truth only and not shipped), on 8 stages:

| Stage | runs | corner notes | recall (all) | recall (grade 1–4 and HP) | precision (R < 150 m) | apex after the call | grade within 1 | Spearman grade vs R |
|---|---|---|---|---|---|---|---|---|
| Afon Bidno | 26 | 54 | 0.63 | 0.93 | 0.97 | 30 m | 0.74 | 0.72 |
| Loutraki - Aghii Theodori | 3 | 104 | 0.88 | 0.96 | 0.92 | 33 m | 0.71 | 0.55 |
| Elatia | 2 | 42 | 0.71 | 0.96 | 0.85 | 33 m | 0.77 | 0.74 |
| Forêt de Munster | 10 | 58 | 0.72 | 0.88 | 0.82 | 45 m | 0.64 | 0.03 |
| Steigenbach | 1 | 66 | 0.77 | 1.00 | 0.85 | 41 m | 0.78 | 0.55 |
| Obersteigen | 2 | 34 | 0.74 | 1.00 | 0.80 | 35 m | 0.80 | 0.79 |
| Sommet de Munster | 4 | 37 | 0.60 | 0.90 | 0.91 | 45 m | 0.86 | 0.72 |
| Aghii Theodori - Loutraki | 11 | 107 | 0.82 | 0.95 | 0.96 | 31 m | 0.58 | 0.47 |

The missed notes are grades 5–6, which hardly bend the path. The median
driven radius per note grade is HP 8 m, 1: 13 m, 2: 30 m, 3: 44 m,
4: 82 m, 5: 95 m. That is close to `coach_context.RADIUS_GRADES` (12, 25,
45, 80, 140). Grades 5 and 6 overlap. The exact grade agrees 35 % of the
time and within one grade 70 %.

**Use**: name corners by the game's notes where they exist (ACR; the
apex is 30–45 m after the note's distance). Use the radius grade
elsewhere.

From positions, the curvature agrees with the yaw-based one: lap sim
r 0.998 and 0.857 (§2.3). In `acr_positions.py` the raw per-run
correlation is lower (0.44–0.88), because it is unsmoothed at 5 m.

Finding (fixed in the stage table and docs, 2026-10-05): the stage table's
`finish_source` said "the game's pace notes have no finish marker". The extraction does have a `Finish` note on 45
stages (Afon Bidno at 5277 m), and the game clock stops 19–28 m after it
(§4.9).

### 4.4 Grip (`grip.py`)

- **Peak μ per tyre** (P99 per run), by surface and car:

  | Surface / car | front | rear |
  |---|---|---|
  | gravel / Fabia | 1.49 | 1.57 |
  | gravel / i20N | 1.56 | 1.63 |
  | gravel / Polo | 1.67 | 1.72 |
  | gravel / 208 | 1.61 | 1.37 |
  | tarmac / 208 | 0.88 | 0.89 |
  | tarmac / i20N | 1.43 | 1.48 |

  Surface is easy to tell per car from the forces. Across cars it is not
  (the 208 on tarmac is 0.88).
- **g-g P98 per run**: gravel 0.92–1.08 g, tarmac 0.82–1.38 g. The g
  level alone does not tell the surface.
- **"Grip used"**: the generic g over the g-g envelope against the force
  truth (the axle nearer its limit) is r 0.69 median over time (0.16–0.82
  by run; the 208 on Alsace tarmac is worst) and 0.54 per corner.
  - Good enough for a per-corner "% of grip" readout in any game.
  - Not good enough to judge a single corner's balance.
  - For ACR and ACC use the forces.
- **Balance by axle** (front-minus-rear usage) against body slip per
  corner: r −0.3 to −0.6 for the 208 and near 0 for the AWD cars. On FWD it
  says something; on AWD the axles saturate together.

### 4.5 Powertrain (`powertrain.py`)

- **Overall ratios** from the engine speed over the driven-wheel spin:
  within 0.06–0.19 % of the files, per gear.
  - The **gear set** in use is named on every run. The i20N has two sets
    in its data, and marth's runs used both: Set0 on 6 runs (error 0.06 %)
    and Set1 on 13 (0.17 %).
  - Only rpm/speed is available in a game without wheel spin, which
    leaves ratio over radius. WRCG gives 139, 89, 66, 51, 39 rpm per km/h,
    steps 1.56, 1.35, 1.31, 1.31.
- **Free-rolling radius** from coasting wheels: 0.317–0.330 m, against
  the files' 0.3155–0.325 m.
- **Torque from the forces**, Σfx_driven · r / (G η): shape r 0.97–0.99
  against the files, level 0.79–0.87 of them. The gap could be the driven
  tyres' rolling loss, transmission loss beyond the gearbox efficiency,
  or the curve being the engine's flywheel torque before losses. Note it,
  do not "correct" it.
- **Torque from acceleration alone** (any game, engine-limited rows,
  drag fitted jointly):
  - i20N: r 0.96, 2.6 % after scaling, implied mass 1371 kg (measured
    1400);
  - Fabia: r 0.21, and 208: negative. Grade and traction contaminate it.

  WRCG's shape, made grade-corrected with its positions, peaks at
  4375–4625 rpm, with crossover upshifts at 6264, 5717, 5411 and
  5418 rpm. **Unvalidated**: no car data exists for WRCG. Use only where
  it can be checked.

### 4.6 Braking (`braking.py`)

On 1658 ACR zones and 217 WRCG zones:

- **Peak deceleration** from the car-frame accel agrees with the forces
  at r 0.88 (0.29 when taken from d(speed)/dt on the receive clock).
- **Shape**: median build 0.19 s, release 0.51 s; the brake is still on
  at the slowest point in 17 % of zones; trail share 0.46.
- **Lock-ups** (a wheel under 70 % of the road speed for 0.1 s) in 49 %
  of zones, and a hard lock (under 30 %) in 21 %. ACR's own `wheel_slip`
  peaks at 0.74 in zones flagged locked and 0.25 in the others, which
  confirms the flag.
- **Onset consistency** (inter-quartile spread across full runs, zones
  seen in 3 or more runs):

  | Afon Bidno | median | p90 |
  |---|---|---|
  | i20N | 7.4 m | 22.8 m |
  | 208 | 6.8 m | 14.8 m |
  | Fabia | 9.2 m | 18.6 m |

  The least consistent places (the i20N at 646 m: 68 m of spread) are
  exactly where a "you brake 30 m early here" call is worth making.

### 4.7 Jumps (`jumps.py`)

Truth: all four tyre loads under 50 N, with free fall, for at least
0.1 s. There are 46 flights; the median airtime is 0.19 s and the longest
0.74 s.

| Detector | recall | precision | airtime error |
|---|---|---|---|
| a_z < −7.5 m/s² for 0.1 s | 0.78 | 0.87 | median +0.02 s |
| Suspension at droop | 0.35 | 1.00 | — |
| Both | 0.28 | 1.00 | — |

- The a_z threshold and duration were swept: −6.5 m/s² for 0.1 s gives
  recall 0.85 and precision 0.72; −8.5 m/s² for 0.15 s gives 0.50 and
  1.00.
- **Landing load** (median 2.1 g, up to 4.0 g of total load over m g) is
  not predicted by the peak a_z (r 0.03) or by the airtime (−0.15).
- Eight "flights" without free fall were the car on its roof or side.
  All 8 were at places where offs were detected: it is a crash marker.
- **WRCG**: free fall from the positions' height found 4–60 events per
  stage. The suspension never reads at droop with the decoder's scaling,
  which fits the susp unit problem (§1).

### 4.8 Corner critique (`corner_critique.py`)

Each run × section pass, against the section's fastest pass. The
regression is within section, with the apex speed deficit as a control.
Costs with 90 % bootstrap intervals over sections:

| Term | i20N (183 passes) | 208 (83) | Fabia (63) |
|---|---|---|---|
| Apex gear longer than the fastest pass | 0.11 s (−0.02–0.25) | 0.28 s (0.09–0.56) | 0.53 s (0.20–1.03) |
| Apex gear shorter | 0.17 s (0.04–0.29) | 0.19 s (−0.04–0.36) | 0.20 s (−0.03–0.40) |
| Exit revs under the band (torque ≥ 90 % of peak) | 0.19 s (0.07–0.32) | none seen | one case |
| Counter-steer (losing the rear), per second | 0.13 s (−0.15–0.35) | 0.27 s (0.09–0.46) | 0.41 s (0.09–0.80) |
| Body slip over the section's median, per degree | ≈ 0 | ≈ 0 | ≈ 0 |
| Apex speed under the best (control), per km/h | 0.038 s | 0.044 s | 0.036 s |

- Wrong gear and the rear stepping out each cost 0.1–0.5 s per instance.
  The intervals are wide with 3–6 runs per car and narrow with more.
- The control matters. Without it, "a gear shorter" took 0.40 s on the
  i20N, because a slower apex both costs time and calls for a lower gear.
- **The calls this supports**, per corner of the PB: for example, "the 1
  left at 2.6 km: the revs were under the band on the exit (2824 rpm),
  about 0.2 s", or "the 3 right at 1.2 km: 0.8 s of counter-steer, about
  0.2 s" (km from the start line).

### 4.9 Game clock and the exact finish (`clock_finish.py`)

On the v4 captures:

- **Receive clock**: within 0.04–0.39 s of the game's clock over full
  runs. One partial run differs by 2.6 s, a pause.
- **Distance timing to the "Finish" note** is 0.5–0.7 s short of the game
  clock: the clock stops 26.8–27.6 m after the note on Sommet and 19.1 m
  after it on Aghii.
- **Against `pacenote_last_m`**, the stop is 319 m before it on Sommet
  (10477 against 10796) and 193 m before it on Aghii (10463 against
  10656). That fits the 194–320 m found on 45 stages.

**Exact method**: finish_m = the spline distance where stage_time stops
increasing with the car still moving. The start is where it starts
(Sommet 5145.2 m, Aghii 298.0–298.1 m, on every run). Per stage, one
finished v4 run fixes it.

WRCG also stops its clock at the stage length (13205.6 against
13207.9 m).

### 4.10 Line and track limits (`acr_positions.py`)

Two or three full runs per stage so far:

| | Sommet | Aghii |
|---|---|---|
| Lateral spread across runs, median (p95) | 0.24 m (0.71 m) | 0.30 m (1.17 m) |
| Road-used envelope, max − min offset, median (p95) | 0.57 m (1.7 m) | 0.6 m (2.35 m) |
| Apex offset to the inside of the median line, spread between runs | 0.25 m | 0.51 m |

marth's line is very repeatable. That is good for coaching the line
against his own best, and it means the envelope of 2–3 runs says almost
nothing yet about where the road edge is. **Edges are pending**: they need
runs that use the width (5+ runs, including offs).

## 5. Offs and crashes: why, how to avoid it, how often (owner's request)

**Prototype**: `research/extrapolation/offs.py`. Labels are in
`offs_labels.json`; the review sheets (not committed, they show samples)
are made with `--review [--blind] [--only <capture prefix>]`.

### 5.1 Detection

An incident is one of:
- a stop: under 3 m/s for 1 s or more past the first 100 m, or reverse
  engaged, or the run abandoned after slowing;
- a roll: all four tyre loads under 50 N without free fall (ACR);
- a hit: over 3 g horizontal for 50 ms.

Pieces within 5 s are one incident. v2 also drops:
- anything past the finish, which is the stop at the marshals; the finish
  is the "Finish" note or the stage table's `finish_m`;
- an incident within 20 s and 150 m of the previous one (its aftermath).

The coach's own `coach_context.incidents()` does the same on the 10 Hz
trace; this version runs at 60 Hz with the loads.

### 5.2 Loss of control, cause, advice

Going back up to 6 s from the incident, the loss of control is the first
sample where the body slip passes 20°, the car leaves the ground, or a
hit. The cause comes from the 4 s before it, against the median of the
car's clean finished runs on the same 5 m grid. v2 checks the causes in
this order:

| Cause | Test | The coach's sentence |
|---|---|---|
| landing | In the air (0.08 s or more) within 1.5 s before | "Off after the crest at {place}: the car landed {heavily (2.7 g) \| with 20° of slide}. Lift or brake before the crest so it lands straight and on four wheels; full commitment comes after the landing." |
| power | Throttle over 60 % past the slowest point with the driven wheels over 20 % slip, as the slide grows | "Off at {place}: the throttle went to 96 % while the car was still turning, and the driven wheels spun (31 %). Wait for the car to point down the exit, then squeeze it on." |
| understeer | Lock over 35 % in the last 2 s with the body slip under 15° | "Off at {place}: the car ran wide with 80 % lock and little slide (understeer). Enter slower, or keep weight on the front (brake into the turn-in) so the nose bites." |
| lift / brake mid-corner | The slide starts with the brake (> 30 %) applied after the lock was on, or within 1 s of a brake or throttle release | "Off at {place}: the rear let go 0.3 s after the brake. Get the braking done before the turn-in, or release it progressively as you turn, so the load stays on the rear." |
| entry speed | More than 6 % over the clean runs in the 2 s before, in a corner; braking more than 15 m later than them is named | "Off at {place}: +18 km/h over your clean runs entering a left 1. You braked 24 m later than in them. Brake to your clean-run speed there; the time is made on the exit." |
| counter-steer late | Slide over 15°, steering against the yaw 0.4 s or more after it starts | "Off at {place}: the slide reached 46° and the counter-steer came 0.9 s after it started. Catch it earlier: steer into the slide as soon as the rear moves." |
| clipped | A hit with no slide, at the clean runs' speed | "Hit at {place} with the car straight and at your usual speed: the line touched something (a rock, a bank, the inside). Leave a little more room there." |
| unknown | — | Said as unknown; never guessed |

The place is the corner (radius grade and direction) and its km. The
game's note there is appended: "(The game's note there: Caution, Brake,
Right2.)".

**Frequency**: incidents are ordered by time. Each carries the number of
earlier incidents with the same cause, and with the same cause within
60 m (`same_cause_same_place_before`). That gives "3rd time in 10 runs
you've gone off braking late into the right 3 at 1.3 km". On marth's runs
the repeat places stand out: the right 2 (DontCut) at 1.52 km along the
spline of Afon Bidno (1.3 km from the start) has 6 incidents across two
cars (Fabia, i20N); the left 2 (Opens) at 1.15 km of Aghii has 3 in one
evening.

### 5.3 Accuracy, honestly

- **Labelling.** 90 detections were labelled by the research session from
  the traces and the notes: 72 in sample and 18 blind (§5.3, held out). The labeller also wrote the classifier, so the in-sample figures
  are an upper bound.
- **Detection.**
  - v1: precision 0.86 (10 of 72 were the finish stop or the aftermath
    of the incident before).
  - v2: precision 1.00 on the same captures (0 of 59), and 1.00 on the
    18 held-out.
  - Recall was not measured. There is no independent list of offs; the
    scratch database's `off` events (31) are the 10 Hz version of the same
    rule.
- **Cause, in sample** (45 incidents with a clear label; 17 unclear even
  to the eye):

  | | v1 | v2 (rules revised after seeing v1's labels) |
  |---|---|---|
  | Strict | 0.42 | 0.59 |
  | Counting the second-choice cause | 0.58 | 0.66 |

  By cause in v2:

  | Cause | recall | precision |
  |---|---|---|
  | entry speed | 0.75 | 1.00 |
  | power | 1.00 | 1.00 |
  | clipped | 0.75 | 1.00 |
  | lift | 0.56 | 0.56 |
  | understeer | 0.38 | 0.83 |
  | landing | 0.00 | — |

- **Cause, held out**: the 18 offs of the 2026-10-05 evening (Aghii,
  Polo; Sommet, i20N), labelled blind from sheets drawn without the
  prediction, after v2 was frozen:
  - strict 0.20, 0.33 counting the second choice (15 clear);
  - v1 scored 0.07 and 0.20;
  - 7 of the 11 "lift" offs were called "counter-steer late".

  The reason is visible in the traces. On gravel the body slip passes 15°
  in **normal** corner entries, so "the slide started" fires during the
  ordinary entry slide and the rule chain never reaches the brake release
  that actually began the loss.
- **What would fix it (v3, not done)**:
  - define the loss of control as the body slip *diverging* (|β| rate over
    a threshold and |β| over the car's P95 for that corner type on the
    surface), not a fixed 15° or 20°;
  - read the pedal events in the second before that point;
  - score again on new blind labels.

  Until the held-out accuracy passes about 0.7, the coach should **say the
  place, the frequency and the facts** ("+18 km/h over your clean runs",
  "the brake came off 0.3 s before the rear went") and **not name a single
  cause**.

## 6. Integration plan (ranked)

Phases refer to `docs/telemetry-ui-design.md` §10 (2: post-run analysis,
3: live, 4: coaching debrief). Traces are version 2 now: `t` is the run's
pause-free clock, the game's stage clock where it runs, and `wall` is the
receive clock. Everything below reads `t` for times and `distance` for
places, and never `wall`.

### 6.1 Potential time and the leaderboard target (phase 2 data, phase 4 calls)

1. **`oversteer/potential.py`** (new, pure). It holds `envelope(rows, p)`,
   `simulate(k, env, floor)`, `sections(grid, k)` and `layers(...)` as in
   the prototype.
   - Inputs come from the 10 Hz traces. a_long and a_lat are there;
     curvature is yaw_rate/speed; x, y, z come from trace v2 where
     present.
   - At 10 Hz the curvature smoothing must widen to about 20 m. Check it
     by replaying the Afon Bidno runs: potential within 1 s of the 60 Hz
     result.
2. **Storage**. Two tables:
   - `envelopes`: (car, surface, game), the P98 and P99.5 bins of lat,
     brake and drive by speed, the runs counted, and when it was built. It
     is rebuilt after each finished run.
   - `stage_potential`: (stage, car), the grid step, the user, grip and
     car totals, and the JSON per-section list (d0, d1, apex_d, radius,
     grade, user_s, grip_s, car_s, apex speeds, the loss split). It is
     recomputed when the envelope or the stage geometry changes.
3. **API**. `GET /api/v1/runs/<id>` gains `potential: {user, grip, car,
   target, sections: [...]}`, with each section's `available_s`,
   `grip_used`, `cause` and `target_gain_s`. The splits ribbon can then
   show the time available per split. `GET /api/v1/potential?stage=&car=`
   returns the stage view.
4. **Target**. The leaderboard time is entered per stage and car (a
   settings field; ACR's leaderboard is not in the telemetry). Store it in
   `stage_targets(stage, car, target_s, source)`. The coach apportions
   the gap as in §2.4.
5. **Coach**. A new aspect `potential.sections` in `derivations.json`,
   with outputs `potential.available` (s) and `potential.grip_used`
   (fraction) and gates (a finished run; at least 3 runs of the car on the
   surface for the envelope). The tip carries the structured fields from
   §9.4 of the UI design: `place` from the section, `cost` = target share
   or available time, `call` = the §2.4 sentence. Grip under 30 % at a
   kink is never quoted.
6. **UI**. Telemetry › Run gets a fourth trace on the speed strip (the
   potential speed) and an "available" column in "Where the time went".
   Coaching gets the target card: "PB 187.7 → target 173.0: 14.7 s, 90 %
   of what is available".

**Implemented (2026-10-05, `oversteer/potential.py`).** Pure numpy over the 10 Hz traces; the road from
the positions (ACR, plan view (x, -z)) or yaw/speed, smoothed over 20 m; the envelope from single 10 Hz rows
(no smoothing: 3 rows lands 4-5 s over the 60 Hz result). Tables `envelopes` and `stage_potential`
(`telemetry_store.POTENTIAL_DDL`), recomputed on the drive-log thread from `coach.stage_metrics` (the post-run
path outside drive_log) when the runs they come from change. The user layer is the splits' sum of best
(`coach.stitch`). Afon Bidno, i20N, replayed from marth's captures: PB 187.7, user 184.0 (183.1 on the
research's own sections), grip 169.3, car 162.0 s (research 171.4 and 162.7; Sommet grip 145.3 against 148.3).
The coach says the top 3 places after every finished run (`kind: top3`), plus what closed on the potential
since the last run; offs state place, how often and measured facts, no cause. Not built: `stage_targets` and the
leaderboard target mode.

### 6.2 After every finished run: top 3 (phase 4)

After the run is closed and classed (`run_class` clean or off), pick the 3
sections with the largest `available_s` on this run (PB − potential uses
this run's times). Each gets one fix: the §2.4 call, with the §4.8
corner-critique term where its cost is ≥ 0.15 s ("a gear shorter than
your fastest pass, about 0.2 s"). Then add "what improved": the section
whose time against the potential improved most since the previous run of
the stage and car, as praise with a number ("the 4 left at 2.1 km: 0.9 s
closer to the potential than last time"). Storage: `coach_state` already
carries tips. The structured tip gains `kind = 'top3'`, `rank`, and
`delta_prev`.

### 6.3 Stage geometry storage and track limits (phase 2)

1. **`stage_geometry`** (stage, game, version, step_m, source:
   `pos|yaw`, runs, updated) plus `stage_geometry_rows` (stage, d, x, y,
   z, k, kv, grade, width_l, width_r, surface_mu, rough), keyed (stage,
   d), every 2–5 m. With positions, x/y/z are the median of the aligned
   runs. Without, the dead-reckoned map (`source='yaw'`) and no height.
   Both carry k. A stage built from `yaw` is replaced by `pos` when 2
   position runs exist. Size: about 2000 rows for 5 km.
2. **Track limits** (after 5+ position runs of the stage, any car):
   - `width_l` and `width_r`: the envelope of the lateral offsets (P2 and
     P98 over runs, widened by the car's half-width of 0.9 m). Offs are
     excluded: an off's offsets mark *beyond* the edge, kept as
     `off_side` in `incidents`.
   - `surface_mu`: the per-5 m P95 of tyre μ (ACR/ACC) or of g over the
     envelope, against the stage's median. A drop over 20 % across runs is
     a grip change (mud, a wet patch, a surface change in a mixed stage).
   - `rough`: the per-5 m RMS of susp_vel or of a_z high-passed (ridges,
     ruts, cuts). Validate against `OverBumps` and `Bumps` notes before
     coaching it.
3. **API**: `GET /api/v1/stages/<key>/geometry?step=` for the minimap and
   the run view. It replaces `/runs/<id>/map` when present.
4. **Pending**: edges and roughness need more position runs (§7). The line
   offset is ready (§4.10).

### 6.4 Grip usage and balance from the forces (ACR, ACC; phase 2–3)

- **Decoder**: `_ovst3` takes `wheel_load`, `fx`, `fy` and `wheel_slip`
  into new `Sample` fields (`tyre_load`, `tyre_fx`, `tyre_fy`,
  `tyre_slip`). They need adding to `coaching_matrix` channels, with ACR
  `confirmed` citing these captures. This is a decoder change: tier B, an
  Opus review.
- **Trace**: add `grip_use` (the axle nearer its limit, 0..1.5) and
  `grip_balance` (front minus rear usage). That means `TRACE_VERSION`
  becomes 3.
- **Aspects**: `context.grip_level` gets a `forces` derivation, which
  makes its ACR status *validated*. `balance.slip_angle` gets its `body`
  derivation, with body slip confirmed on ACR. The live view (phase 3) can
  show a grip bar.

### 6.5 Offs and crashes (phase 4, after a v3 classifier)

1. Ship detection now. v2's finish and aftermath filtering should go
   into `coach_context.incidents()`: drop stops past `finish_m` and merge
   within 20 s and 150 m. Add the ACR roll detection from the loads once
   the loads are decoded (§6.4).
2. Store in `events`: `cause`, `cause_conf`, `place`, `facts` (JSON:
   dv_kmh, brake_late_m, beta_max, slip, lag). Add a per-driver view
   `incident_counts(car, stage, place_bin, cause)` for "3rd time in 10
   runs".
3. Coach: say the place, the count and the facts now. Name the cause
   only after v3 passes about 0.7 on blind held-out labels. marth's next
   sessions provide those labels: a "why did I go off?" picker in the run
   view would collect them cheaply.

### 6.6 Smaller items, by value

- **Exact finish from the game clock** (§4.9). In `stage_tables` /
  `drive_log`, derive `finish_m` per stage from the first finished v4 run:
  the clock-stop spline distance, with `finish_source: 'game clock'`. It
  replaces the median-of-slowdowns estimate. S.
- **Gear-set and setup inference** (§4.5). A `tunes.gear_set` column
  filled from the measured ratios. The coach's gearing advice and the car
  layer then use the right set. S.
- **Braking-point consistency** (`brake.point`, `brake.consistency`,
  planned in `derivations.json`) is validated here: implement with the
  §4.6 method. The lock-up flag (`brake.lockup`) needs `slip_drive` from
  wheel_rot × the learnt radius in the trace (§4.6 used 0.327 m). S.
- **Jumps** (`vehicle.jumps`): the a_z detector at −7.5 m/s² for 0.1 s,
  validated. Landing severity only from the loads (ACR, ACC). S.
- **Corner names from notes** (ClickUp 86e3faftn): the apex is 30–45 m
  after the note's distance (§4.3), which is the rule for matching. M.
- **Corner critique** (§4.8). Add the gear and exit-revs terms to
  `corner.phases`, with the cost from the within-section fit stored per
  car. Quote only when ≥ 0.15 s and the interval excludes 0. M.
- **Elevation and crests** from positions in `stage_geometry` (§6.3). The
  force grade only as a shape where there are no positions. Crests from
  the notes. S.
- **WRCG**: no attitude, crest or grip coaching until the decoder review
  fixes forward, accel and susp (§1). Positions-based derivations
  (map, elevation, curvature, potential) work now.

### 6.7 Derivation database changes (`derivations.json`)

- New channels:
  - `tyre_load`, `tyre_fx`, `tyre_fy`, `tyre_slip`: ACR confirmed;
    ACC decoded.
  - `stage_clock`: ACR v4 confirmed; WRCG confirmed.
- New derived channels:
  - `grip_use`: forces, or g over the envelope;
  - `course_curvature`: (yaw_rate, vel) or (pos);
  - `height`: (pos).
- New aspects:
  - `potential.sections`: validated, Afon Bidno;
  - `potential.target`;
  - `context.track_limits`: planned, pending positions;
  - `context.stage_geometry`: validated: yaw against pos, notes, WRCG
    table;
  - `vehicle.gear_set`: validated;
  - `incident.cause`: untested, with held-out accuracy 0.20.
- Status changes:
  - `vehicle.jumps` (accel_only): validated;
  - `brake.consistency` and `brake.lockup` (wheels): validated in
    research;
  - `context.grip_level`: a new `forces` derivation, validated.
- Gaps to update:
  - "No tyre loads or forces decoded" becomes "in the packet, confirmed,
    not yet decoded";
  - "Position only since the 2026-09-27 bridge fix" becomes "since
    40c8ca4 (2026-10-05), confirmed on v4 captures";
  - `finish_source` for ACR: the pace notes **do** have a finish marker.

## 7. What needs marth to drive

1. **Afon Bidno on bridge v4, i20N, 3+ finished runs.** It ties the
   potential to the game clock and the true finish, and gives the
   position-based curvature on the stage the potential was tuned on. It
   also gives a forward test: does the next PB come from the top-ranked
   sections?
2. **5+ runs of one stage with positions**, ideally one using the full
   road and one with an off. That is enough to learn road edges, `rough`
   and grip changes (§6.3).
3. **A run or two after changing one setup item** (gear set, springs or
   ride height). That validates gear-set inference live, and spring rate
   from load against suspension travel (not tried).
4. **Any ACC or AC session** (the forces and the loads come through the
   same bridge). Optional: a DiRT Rally 2.0 or Forza capture to confirm a
   second game's accelerations and slip channels.
5. **A "why I went off" label** for the next sessions' offs, for the v3
   classifier.

## 8. Reproducing

```sh
export EXTRAP_CACHE=~/.cache/oversteer-extrapolation     # decoded samples, outside the repo
cd research/extrapolation
python3 inventory.py > out/inventory.txt
python3 lapsim.py                                         # Afon Bidno, i20N, P98, target 173
python3 lapsim.py --before 20261003-193057                # hindcast
python3 lapsim.py --stage "Alsace Sommet" --car "Hyundai i20N Rally2" --d0 5145.2 --d1 10477 --target 150 --curv pos
python3 corners.py; python3 grip.py; python3 powertrain.py; python3 jumps.py; python3 braking.py
python3 map_elevation.py; python3 acr_positions.py; python3 clock_finish.py
python3 corner_critique.py --car "Hyundai i20N Rally2"
python3 offs.py [--v1] [--review --blind --only 20261005-2]
```

`corners.py`, `offs.py` and `acr_positions.py` read the pace notes from
`$PACENOTES` (default `~/.cache/oversteer-extrapolation/pacenotes`). Fetch
them once from Koenvh1/PacenotePal (MPL-2.0); they are not shipped.
