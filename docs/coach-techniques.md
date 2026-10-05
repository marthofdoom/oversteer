# Coach techniques: what a professional coach knows that the rules do not

The coach (`oversteer/coach.py`, design §9) turns metrics into sentences.
A metric is a rule over the trace: seconds on the limiter per km, the
share of time on both pedals, the fraction of a corner spent steering
against the yaw. Each rule was written with a mistake in mind, and each
one also fires on a technique that looks the same in a channel or two and
is right in context. The owner's complaint ([ClickUp 86e3jeaye]) names
one: the coach complains about revs on the limiter, while on gravel being
under-geared through corners is common. This document is the catalogue
behind that complaint: the techniques and contexts a professional coach
recognises, what in the telemetry tells a technique from a mistake, what
the coach does with it today, and what the coach should say instead. It
ends with the rule changes it implies and with what professional coaching
looks for that Oversteer does not measure yet.

It is a catalogue, not code. Numbers given as thresholds are starting
values to **calibrate**, like every other threshold in the design. Rally
comes first (gravel, tarmac, snow and ice, mixed), then hillclimb,
rallycross, circuit and drift, because the owner drives Assetto Corsa
Rally first, then WRC Generations and DiRT Rally 2.0, on a G29 with a
T500 RS shifter (H-pattern and sequential plates) and a handbrake.

Channels this document can use are the ones Oversteer has (design §5.2,
`docs/coaching-derivations.md`): speed, rpm, gear, throttle, brake,
clutch, handbrake (from the rig; ACR sends none), steer, yaw rate,
longitudinal and lateral g, body slip angle from the car-frame velocity,
wheel speeds (where sent; ACR derives them through the learnt tyre
radius, so the trace's `slip_drive` is thin there), distance along the
stage, the stage's surface from the table, the first and last pace-note
distances (the full note lists are still in the game files,
[ClickUp 86e3faftn]), and position (ACR only on captures after the
bridge fix). The 10 Hz trace carries `t, distance, speed, rpm, gear,
throttle, brake, clutch, handbrake, steer, a_long, a_lat, yaw_rate,
slip_drive, susp_rms, x, y, z`, so anything per corner is worked out at
10 Hz; anything faster (a clutch kick, a Scandinavian flick's first
steer input) is seen at that resolution or not at all.

## 1. What marth's own captures say

Before the catalogue, the evidence. The fifteen ACR and WRC Generations
captures of 2026-09-25 to 2026-10-03 were copied and replayed through the
live path (`ShiftLearner` + `Telemetry.handle`, one scratch database,
profile `ACR`): 60 runs (55 ACR, 5 WRCG), 1170 corners, 1429 gear changes
(780 up, 649 down), 133 km of gravel, 36 km of tarmac and 2 km of snow
(Livigno), in the Skoda Fabia RS Rally2, the Hyundai i20N Rally2 and the
Peugeot 208 Rally4. What the coach says about them, with "show all":

- **"Hold it to about 7500: the best from the game's engine data"**, for
  every gear of both Rally2 cars, on gravel and on tarmac, as the focus
  habit ("more than 200 rpm early in 5 of your last 5 sessions"). 7500 is
  the limiter. The game's shift lights for the same cars say `shift` at
  6900 (Fabia) and 7000 (i20N), `late` at 7100 and 7250, and the game's
  own automatic gearbox changes up at 7450. The driver changes at
  6400-7300 on gravel. The torque-curve crossover (`CarModel._scan`)
  lands on the ceiling for a flat-torque turbo engine, so "the best" is
  the cut itself, and the drive lost by changing at the shift light is
  3-6 %, which the sentence quotes. What it does not count: the shift
  itself (a sequential cut of 50-100 ms, during which the engine makes
  nothing), the boost the next gear lands with, and that holding to 7500
  means touching the limiter every time. This is the owner's complaint,
  stated the other way round: the coach does not complain about the
  limiter, it prescribes it.
- **"4 of your last 5 launches bogged down"** (Fabia, i20N, 3 of 5) on
  launches that reach 50 km/h in 1.4-1.8 s. Every AWD gravel launch in
  the captures drops the revs to 55-75 % of the held revs as the clutch
  bites and the tyres hook up, which is what a good AWD launch does. The
  60 % rule reads a hooked-up launch as a bog. The 208 Rally4 (FWD) on
  tarmac takes 2.8-2.9 s with the same rev drop; that is the car, not a
  bog either.
- **`limiter.per_km`**: 19.5 s in total over 170 km, below the tip's
  threshold everywhere but one run. Of those seconds, 12 are run 21, a
  174 m attempt in which the car sat in a ditch with the wheels spinning
  in 2nd at 7500 rpm (6 s of it at under 3 m/s). Of the 61 episodes
  (median 0.19 s), 54 end in a change up within 2 s (the driver touching
  the cut at the shift point); 6 of those are followed by a change back
  down within 4 s (an up-then-down between corners); 7 are held with no
  change up, 3 of them run 21's, 1 into braking. Nothing in the captures
  is "under-geared through a corner on the limiter"; the limiter time is
  the shift point and a stuck car. (A rally launch in these games is made
  with the throttle floored against the clutch, so the car stands on the
  cut before the start; the trace begins at 3 m/s, so little of that
  counts, but it would with a slower start.)
- **Pedal overlap** (throttle and brake both past 20 %): 9.5 % of moving
  time on gravel, 13.9 % on tarmac, 0.4 % on the Livigno ice. Three
  quarters of it is before the apex (entry) on both surfaces: left-foot
  braking into corners in a turbo car. The tarmac tip (`pedal.overlap`,
  fired when the stage's last run also lost 0.5 s in its worst corners,
  which the last run of a session nearly always has) would say "come off
  the brake before the throttle goes back on" to a Rally2 driver on
  Alsace doing what every Rally2 driver does.
- **Counter-steer** rises with the corner on gravel: 0.09 of the time in
  corners under 30° of heading change, 0.25 at 60-90°, 0.48 in hairpins
  (61 % of hairpins over 0.3); on tarmac 0.01 in fast corners and 0.38 in
  hairpins. The metric is a per-run average today; on marth's driving it
  is a description of hairpin technique, not a balance problem.
- **Coasting**: 54 s in corners and 55 s on straights over 133 km of
  gravel. The tip ("stay on one pedal or the other") fired against the
  stage's best run for both Rally2 cars. Half the coasting is the
  throttle-off rotation phase of a loose-surface corner.
- **Downshifts**: 649, mean engage 4400 rpm, 13 over-revs (2 %): clean.
  114 taken with the throttle past 50 % (a lower gear taken while still
  on the gas, for the exit): technique on a sequential rally car.
- **Exit revs below the power band** (`exit.low`): 25-43 % of gravel
  exits per gear, 69 % of 2nd-gear exits on tarmac in the 208. Below the
  50 % tuning threshold on gravel; the tarmac figure is a Rally4 leaving
  hairpins in 2nd, which §3.4 discusses.

Two things found on the way, outside this document's remit but worth a
ticket: every ACR run's discipline is `unknown` ("needs the car's
position, which Assetto Corsa Rally does not send"), the captures of
2026-10-02/03 included, so no discipline-gated coaching fires for ACR,
although the stage table knows which keys are rally stages and which is
the Livigno circuit; and the Fabia RS Rally2's car row carries
`drivetrain = fwd` (the shipped data says `awd`), which would put the
driven-wheel slip on the wrong axle.

## 2. Principles of a technique-aware coach

1. **A behaviour is never the fault; its consequence is.** The limiter,
   both pedals, counter-steer, coasting and a short-shift are all things
   fast drivers do. The coach has a fault when the behaviour costs time
   *against the same driver's own faster runs of the same corners* and
   the alternative is known to be available (the gear above would have
   pulled; the tyres had grip to spare; the corner was faster without
   the slide). A metric with no consequence attached is a description,
   and the coach may describe ("you left-foot brake into 70 % of the
   corners on gravel") but not correct.
2. **Context gates, in this order**: discipline, surface, car (drivetrain,
   turbo, gearbox, ABS), then the corner (how tight, what follows it, how
   far the next one is), then the phase (entry, rotation, exit). A rule
   that cannot see the context it depends on is silent and says so once,
   as the shift gates already do.
3. **The driver's own best is the reference**, never an absolute
   (design §2). A professional coach has the reference run; Oversteer
   has the stage's best run and the stage's best corners across runs.
4. **Per corner, not per km.** A pro says "the hairpin at 3.2 km", not
   "0.6 s per km". Everything the coach can tie to a corner it should,
   and the aggregate is for ranking only.
5. **The game's word beats the model's.** Where the game ships shift
   lights, an automatic change-up point or a limiter, the game's own
   driving aids define the band the coach should expect, and the
   torque-curve crossover is the explanation, not the target.
6. **Mechanical sympathy is coached in every discipline** (over-revs,
   stalls, landing on the brakes, bottoming) because no technique needs
   them. Everything else is conditional.
7. **Learning runs are not coached against the best.** A first run of a
   stage, a run after a long gap, and a run that ended in a ditch are
   different conversations (see §3.6).

## 3. The catalogue

Each entry: what it is; where it is right and where it is a mistake; the
telemetry signature that tells them apart in Oversteer's channels; what
the coach does today; what a professional coach says, in the coach's
sentence style (observation, number, consequence, one action).

### 3.1 Gearing and engine

#### Holding a gear on the limiter through a short straight

*What.* Between two corners close together, the driver stays in the gear
the first corner needed, touches or sits on the cut for the length of the
straight and brakes for the next corner in the same gear, instead of
changing up and back down.

*Right when.* Rally on any surface when the straight is short: a
sequential change up and down costs two cuts (~0.1-0.2 s of no drive)
plus the risk of arriving at the next braking point in the wrong gear;
the lower gear gives engine braking and an instant exit. On gravel more
so, because the exit of the next corner is traction-limited and the
lower gear's torque is used for rotation, not speed. In rallycross
between joker and main line. On a hillclimb up a steep pitch where the
next gear would fall off boost.

*Mistake when.* The straight is long enough that the gear above would
have carried the car faster to the braking point: as a rule of thumb,
more than ~2 s or ~100 m on the cut before braking, and the driver does
change up eventually (an up-then-down that was too late) or is on the cut
so long that the car is visibly slower than the best run over the same
stretch.

*Signature.* An episode of `rpm ≥ 0.985 × limiter`, `throttle ≥ 0.95`,
gear below top, speed above ~8 m/s (standing or crawling wheelspin is
not this; see "stuck" in §3.6). Its length in seconds and in metres, the
distance to the next corner's apex (`corners.d`) and what follows within
2 s: a change up (then the hold was the shift point, not a technique), a
change up followed by a change down within ~4 s (an up-then-down, the
alternative the driver chose), braking (a held gear into the next
corner). Compared with the best run of the stage over the same stretch
(`corner_loss` window extended to the straight between two apexes): was
it slower?

*Today.* `limiter.per_km` sums all of it per km with no corner context
and no speed floor, and the tip says "the engine makes nothing there.
Change up when the lights flash". In marth's captures that sum is mostly
a stuck car and the shift point itself (§1).

*A pro says.* On a short straight, nothing, or praise: "Between the two
rights at 2.1 km you hold 3rd on the cut for 40 m: right, the change
would cost more than it gives." When the straight is long: "After the
hairpin at 3.2 km you hold 2nd on the cut for 3.1 s along a 180 m
straight; 3rd would have pulled there (the next gear reaches the same
grip limit), and you lost 0.4 s to your best run before the next braking
point. Take 3rd as the car straightens, and 2nd again for the left."

#### Under-gearing through corners on loose surfaces

*What.* Taking and holding a gear lower than the corner's speed asks for:
2nd in a corner the speed would allow in 3rd, revs high through the
corner, often touching the cut at the exit before the change.

*Right when.* Gravel, snow, ice and wet, in any car, more so in a turbo
car: the lower gear keeps the engine on boost, gives throttle response to
steer the car with the rear (RWD) or with all four (AWD), and lets the
driver break traction on purpose to rotate the car. On tarmac in a
hairpin or a tight junction for the same reasons. In rallycross always.

*Mistake when.* The corner is open and fast, the car is traction-limited
by nothing, the lower gear ends on the cut mid-corner with the car
pointing straight (the drive is wasted), or on tarmac in a flowing
corner, where the slide the low gear invites scrubs speed.

*Signature.* `gear_min` of the corner against the gear the corner's
minimum speed would sit in the power band in (from the ratios:
`rpm = speed × ratio`), i.e. the "gear reserve" `rpm_at_min_speed /
limiter`. Right: on a loose surface, with counter-steer or body slip
through the exit (the throttle was used to steer) and an exit speed at
or above the stage best's. Mistake: the cut reached while `|yaw_rate| >
0.15` with little slip (the car is pointing straight and the engine is
cut) and an exit speed below the best run's, or on tarmac with
counter-steer where the best run had none.

*Today.* Not measured as such; `exit.low` measures the opposite (a gear
too long on exit); `limiter.per_km` catches the cut at the exit.

*A pro says.* "You take the long left at 4.0 km in 2nd and reach the cut
before the car is straight; your best run took it in 3rd and was 0.3 s
quicker over it. 2nd for the rotation, 3rd as soon as the nose points at
the exit." On gravel with a good exit: nothing.

#### Short-shifting on loose surfaces

*What.* Changing up well before the engine's best, typically 500-1500
rpm early, in the gears whose torque the surface cannot take.

*Right when.* Gravel, snow and ice in the lower gears of a torquey car
(Rally2, Rally1, Group B, any turbo RWD): the lower gear's drive exceeds
the grip, so the next gear, with less torque at the wheels, gives the
same acceleration with less wheelspin, less heat and a car that stays
straight. The learner measures exactly this (`CarModel.grip`,
`best_for` lowered per surface) and `shift.error` is gated on it. Also
right in the wet on tarmac, on a slippery start, and in 1st to 2nd in
nearly every rally car on loose surfaces.

*Mistake when.* The gear is not grip-limited (measured: low slip through
full-throttle pulls in it), the surface is dry tarmac, or the driver is
early by habit in every gear including the top ones where no car spins
its wheels.

*Signature.* `shift.error < -SHIFT_OFF` with `shift.slip` low (traction
was not the limit) on a surface where the gear's grip is measured and
not limiting. The gate in `_shift_tips` does this; what it still lacks
is the band discussed under the next heading.

*Today.* Correctly gated: early changes on a loose surface wait until
the gear's grip there is measured, and the coach says why once.

*A pro says.* On gravel in a grip-limited gear: nothing, or "2→3 on
gravel from 5500 up: right, 2nd is spinning anyway." Where the gear has
the grip: "3→4 on gravel: you change at 6000; 3rd has the grip there
(your pulls in it spun 3 %), and 4th gives 8 % less drive. Take it to the
light."

#### The target band is the shift light, not the cut

*What.* For an engine whose torque holds to the limiter (every modern
turbo rally engine), the torque-crossover "best" is the limiter itself,
and the coach prescribes it. The driver cannot change at 7500 without
touching a 7500 cut; the shift takes time; the next gear lands on boost
or off it.

*Right when.* Always, in every discipline: the change-up target is a
band ending short of the cut. Where the game ships shift lights
(`shift_lights_rpm`: prepare, shift, late, over_limit) or an automatic
change-up point (`auto_upshift_rpm`), that is the band the game's own
drivers use. Where it ships neither, the band is from the crossover
(where the next gear starts to pull as hard) up to `limiter − margin`
(margin: a sequential car's cut time at the engine's rate of climb in
that gear, roughly 150-250 rpm; an H-pattern's 300-400).

*Mistake when.* The driver is below the band (an early change: the
previous heading) or at the cut (a late change: the limiter heading).

*Signature.* `shift.rpm` against `[max(crossover, shift_lights.shift),
min(shift_lights.late, limiter − margin)]`; `shift.in_band` on that
band.

*Today.* `best_for` returns the crossover (7500 on both Rally2 cars) and
the tip says "Hold it to about 7500: the best from the game's engine
data"; `shift.in_band` uses ±100 rpm of a range or ±200 of the best.
The game's shift lights are in `data/telemetry/cars/acr.json` and unused
by the coach.

*A pro says.* "3→4: you change at 6500; the lights say 6900-7100 and 4th
gives 4 % less drive at 6500. Change on the lights, never on the cut."
And, when the driver is on the cut: "You touch the cut on 1 in 3 changes
in 2nd: the engine makes nothing there and the change lands late. Change
as the lights flash."

#### A higher gear on exit to limit wheelspin

*What.* Leaving a slow corner one gear higher than the revs would
suggest, below the power band, so the torque at the wheels does not
exceed the grip; the opposite of under-gearing.

*Right when.* Loose and wet surfaces in a torquey car with an open or
mild differential, on exits where the car is pointing straight (no
rotation needed) and the drive would otherwise spin away; FWD cars on
gravel in particular (a Rally4 in 3rd out of a hairpin); on ice always.
On tarmac in the wet.

*Mistake when.* The gear is so long the turbo is off boost and the car
bogs (`a_long` low with full throttle and no slip), on dry tarmac where
the lower gear's drive would stick, or where the rotation needed the
lower gear (the car runs wide instead).

*Signature.* `exit.low` (revs below the power band 1 s after the throttle
goes back on) with `exit_spin` (driven-wheel slip after the apex, where
wheel speeds exist): low spin and an exit speed at the best's means the
gear was right for the grip; `exit.low` with full throttle and `a_long`
well below the car's best exits in the lower gear means a bog.

*Today.* `exit.low` goes to tuning ("gear n is long; shorten it or use
n−1 there") when over 50 % of a gear's exits are below the band, without
the slip or the acceleration check.

*A pro says.* "Out of the hairpins on Afon Bidno you take 3rd below
4500: fine on gravel if 2nd spins, and your exits match your best. On
tarmac the same habit costs 0.2 s an exit: 2nd there." The tuning note
stays for the case where every exit in the gear is a bog.

#### Rev-matching, heel-and-toe and the H-pattern

*What.* On an H-pattern with a clutch, a change down while braking:
clutch in, a blip of throttle to bring the revs to where the lower gear
needs them, the gear in, clutch out, all while the right foot keeps brake
pressure (heel-and-toe), or with the left foot braking and the right
blipping. Double-declutching on a non-synchromesh box.

*Right when.* Any H-pattern car, every discipline: the blip is the
technique, so throttle and brake overlapping for 0.1-0.4 s at the moment
of a change down is correct and not left-foot braking. Skipping a gear on
the way down (5→3 for a hairpin) is correct on an H-pattern and on a
sequential alike. On a sequential rally box no clutch is needed once
rolling: clutching the changes costs time.

*Mistake when.* The engage revs land past 95 % of the limiter (an
over-rev, mechanical), the blip comes without the clutch (a lurch), the
lever goes through neutral slowly with the foot down (a missed gate), the
driver clutches every sequential change, or a change down is taken with
the throttle flat while the car is straight (meant to go up: the `skip`
flag on changes down).

*Signature.* On a change down with `method = h-pattern`: a throttle
pulse (rig throttle, `pedal_rate`) inside the clutch-down window with the
brake held; `engage_rpm` close to the matched revs (speed × new ratio)
is a good match, far below is a drop-in (the car jerks), far above is an
over-rev. On `method = sequential`: `clutch > 0.5` during changes at
speed. `neutral_time`, `missed`, `skip` as today.

*Today.* `downshift.over_rev`, `hpattern.neutral`, `hpattern.missed`,
`hpattern.skip` (a change up that skips a gear, or a change down taken
flat out near the cut, which was meant to go up; a block change down is
not flagged, as it should not be), `seq.double_tap`. No rev-matching
quality, no clutch use on a sequential.

*A pro says.* H-pattern: "Your changes down into the hairpins land 900
rpm under the matched revs: the car lurches on the clutch. A bigger blip
before the lever goes in." Sequential: "You clutch 60 % of your changes
in the Fabia; the box does not need it once rolling, and each costs a
tenth." Over-rev, any car: "3 in 100 changes down over-rev the engine:
brake a moment longer before going down" (as today).

#### Turbo: boost, lag and anti-lag

*What.* A turbo engine makes its torque only on boost; off boost it is
a small engine. Drivers keep it spooled: throttle kept partly open under
braking (the overlap of §3.2), a gear low enough to stay in the band,
a change up that lands above the boost threshold. Rally1 and some Rally2
cars run anti-lag, which keeps the turbo spinning off throttle (the
revs hang, the exhaust pops); the driver then lifts freely.

*Right when.* Any turbo car, any discipline: the overlap on entry, the
low gear in slow corners and the "late" change up are boost management.

*Mistake when.* Lifts mid-straight (boost lost for nothing), a change up
that lands below the boost threshold on exit (the car dies), full
throttle before the car can take it (boost arrives as a lump: wheelspin
or a snap).

*Signature.* `boost` where a game sends it (Forza; not ACR, WRCG or
DiRT through what Oversteer reads); otherwise `a_long` at full throttle
against the car's known drive at that rpm and gear (the learnt curve is
the steady full-boost curve, so a pull well below it at the same rpm is
lag). The car data's `turbo` flag names the cars this applies to.

*Today.* Nothing turbo-aware. The power curve is learnt from steady
pulls, so lag is noise to it.

*A pro says.* "Out of the hairpin at 6.1 km you go from 1st to 3rd at
5000: 3rd lands at 3200, off boost, and the car takes 1.5 s to come
alive. 2nd to the light, then 3rd." And the overlap entry (§3.2) is
praised, not corrected, in a turbo car.

#### Limiter in top gear

*What.* The cut in the top gear on a long straight.

*Right when.* Never a driving fault: the gearing is short for the stage,
or the stage has one straight longer than the gearing was chosen for
(a Rally2 on one long tarmac straight is normal).

*Signature.* `limiter.top`, `top.share`, `top.peak`. Already sent to
tuning, not driving, with "lift before the limiter in top" first where
the gearing is fixed.

*Today.* Right as it is.

#### Launch technique by surface and drivetrain

*What.* A rally start: revs held against the clutch (in these games
usually the throttle floored, the engine on its cut, which is how
Oversteer learns the limiter), handbrake up, clutch out as the lights
change, throttle fed to the grip.

*Right when and how.* AWD on gravel: high revs, a fast clutch, some
wheelspin is wanted (the gravel wants to be dug), the revs fall to
50-75 % of the held revs as the car hooks up and are back in the band in
under a second; 0-50 km/h in 1.5-2 s in a Rally2. AWD on tarmac: fewer
revs, a fast clutch, less slip. FWD (Rally4, Rally5, historic FWD):
fewer revs and a progressive clutch on tarmac (wheelspin kills a FWD
launch), more slip accepted on gravel; 2.5-3 s to 50. RWD: throttle
modulation after the clutch, the tail stepping out a little on gravel is
normal. Snow and ice: minimal revs, the clutch fed, almost no slip. The
held revs are whatever the driver chose (`launch_rpm` in the run
summary), often the cut itself; the rev drop after release is measured
from there, which is one reason the 60 % rule reads so many launches as
bogs.

*Mistake when.* The revs fall and the car does not go (a true bog: the
engine below its torque, `a_long` low for the first half second), the
engine stops (a stall), the wheels spin without the car moving (slip
high, `a_long` low: too many revs or too fast a clutch for the surface),
or the clutch is slipped so long the time to 50 is slow with no wheelspin
(too gentle).

*Signature.* Judge the launch by its outcome and its slip, not by the
rev drop: `launch.t50` against the driver's own median on that surface
in that car (and, with several cars, per drivetrain); `launch.slip`
where wheel speeds exist; `a_long` over the first 0.5 s after release
(an AWD Rally2 pulls ~0.7-0.9 g off the line on gravel; a bog is under
~0.3 g with the revs down); `launch.stall` as today. A rev drop is a bog
only with the acceleration slump.

*Today.* `launch.bog` = revs below 60 % of the held revs in the first
2 s: it flags most good AWD launches in the captures (§1). `launch.t50`
is a praise-only metric (quicker off the line).

*A pro says.* "Your launches on gravel in the i20N take 1.6 s to 50,
steady within a tenth: good." When slow: "Your last three launches took
2.4 s to 50 against your usual 1.6, with the revs down to 2800 for a
second: the clutch came out too slowly, or the revs were too low for
gravel. Hold 6500 and let it go." On a bog with spin: "The wheels spun
for the first second and the car went nowhere: too many revs for tarmac;
try 4500."

### 3.2 Pedals

#### Left-foot braking (throttle and brake at once)

*What.* The brake applied with the left foot while the right stays on
the throttle: on entry to settle the nose and start rotation without
losing boost, mid-corner to tighten the line (the brake on a loaded
front axle turns the car), and as a stab to shift weight before a flick.
In a FWD car it is the way to rotate the car at all.

*Right when.* Rally on loose surfaces: nearly always technique. Rally on
tarmac in a turbo car: on entry, as boost management and for stability,
normal in Rally2/Rally1. FWD in every discipline for rotation.
Rallycross throughout. H-pattern heel-and-toe blips (§3.1) are overlap
too, and must be excluded first.

*Mistake when.* Circuit racing in a non-turbo car, where the overlap
means the feet are fighting (except trail-braking to the apex, which has
the throttle at zero); on tarmac in any car when the overlap is long on
the straight (dragging the brake), or when the exits are slower than the
driver's own best with the overlap and faster without it; a brake held
while accelerating out of the corner (the only phase where overlap has
no technique behind it, FWD lift-off correction aside).

*Signature.* Overlap by phase: before the apex (entry; technique),
through the apex with `|yaw_rate|` high (rotation; technique on loose,
likely technique on tarmac in FWD), after the apex with the throttle
rising (exit; the suspect phase), and on the straight (`|yaw_rate| <
0.1`, no corner within 100 m; dragging). Exclude the 0.4 s around every
H-pattern change down. On marth's captures 76 % of the overlap is entry.

*Today.* `pedal.overlap` is a share of moving time; the tip fires on
circuit or tarmac when the stage's last run also lost 0.5 s in its worst
corners, which almost always holds for the last run of a session.

*A pro says.* On loose: "You left-foot brake into 70 % of the corners:
good, that is how the car rotates." Tarmac, exit-phase overlap: "Out of
the hairpins on Steigenbach you keep 20 % brake for a second after the
throttle goes down, and your exits there are 0.2 s slower than your
best: off the brake as the throttle goes on." Dragging: "You carry
brake for 80 m on the straight after 2.4 km."

#### Trail braking

*What.* Braking carried past turn-in and released progressively as the
steering goes on, keeping the front loaded to the apex.

*Right when.* Tarmac rally and circuit in every car: the technique of
corner entry. On gravel it exists but is shaped differently: the braking
is earlier and straighter (below), and what is carried into the corner is
left-foot braking against the throttle, not a trailed right foot.

*Mistake when.* On tarmac, not doing it: the brake released fully before
the steering goes on and the car coasts to the apex (the dead time
below). On gravel, braking deep into the turn with the throttle off and a
heavy brake, which locks the inside front and understeers (on a non-ABS
car) or runs out of rotation (on an ABS car).

*Signature.* Per corner entry: the overlap in time of `brake > 0.05`
with `|yaw_rate| > 0.15` or `|steer|` rising, and the shape: brake
falling as `|a_lat|` rises (a trail) against brake stepping to zero
before `a_lat` starts (an early release). `brake.trail` in the
derivations (planned, [ClickUp 86e3ev2dy]).

*Today.* Not measured.

*A pro says.* Tarmac: "Into the fast right at 5.2 km you are off the
brake 30 m before the steering goes on and coast to the apex; your best
run carried 20 % brake to the turn-in and was 0.3 s quicker: hold a
little brake into the corner." Gravel: nothing about trailing; the
entry is judged by the straight braking below.

#### Braking early and straight on loose surfaces

*What.* The braking done in a straight line, finished before the car is
turned, with the entry speed set low enough that the rotation (flick,
lift, handbrake) does the rest.

*Right when.* Gravel, snow and ice, any car: a braking point earlier
than tarmac would need, by 20-50 m at speed, is correct, and a brake
release before turn-in is correct. Later than the best run with a slower
minimum speed is still right if the exit is as fast.

*Mistake when.* The braking point is earlier *and* the minimum speed is
lower *and* the exit slower than the driver's best in the same corner
(over-slowing: the usual mistake of a careful driver on gravel), or so
late that the car enters sideways and scrubs (entry speed above the
best's with a lower exit).

*Signature.* `brake.point` (the distance before the apex of the last
brake onset), minimum speed, exit speed, each against the best run of
the stage per corner; the loss window of `corner_loss` split into entry
(to the slowest point) and exit (from it).

*Today.* `corner.loss` sums the three worst corners' total loss with no
phase and no cause.

*A pro says.* "The left at 1.8 km: you brake 35 m earlier than your best
run and are 9 km/h slower at the slowest point, and the exit is the
same: you are over-slowing it. Brake at the 100 board and trust the
note, the exit will still be there." Or praise: "Braked earlier than
your best, same minimum, faster exit: that is the right entry for
gravel."

#### Coasting

*What.* Neither pedal: throttle under 5 %, brake under 5 %, above
10 m/s.

*Right when.* Loose surfaces, the rotation phase: after the brake is
released and before the throttle goes on, the car is turning on its own
slip for 0.3-1 s and the driver is waiting for the nose to point at the
exit. A lift before a crest or a jump (§3.4). A lift to tuck the nose in
(lift-off oversteer, §3.3). A brief neutral throttle in a long loose
corner holding a steady slide. On ice nearly everything is partial
throttle, little of it zero.

*Mistake when.* On tarmac between brake release and throttle (dead
time: the trail-braking fault above); on any surface after the slowest
point when the car is straight (a late throttle); on straights.

*Signature.* Coasting by phase: in a corner before the slowest point
(rotation, technique on loose; dead time on tarmac), after it (late
throttle, a fault on every surface), on a straight with a crest or a
jump near (a lift, §3.4), on a straight with nothing near (hesitation).
On marth's gravel captures half the coasting is in corners.

*Today.* `pedal.coast` per km against the stage's best run; the tip
says "stay on one pedal or the other", which is wrong advice for the
rotation phase.

*A pro says.* Gravel: "You coast for 0.8 s through the rotation of the
hairpins; your best run gets on the throttle 0.3 s earlier, as the nose
comes round. Throttle as soon as the car points." Tarmac: "Between the
brake and the throttle you have 0.4 s of nothing in the fast corners:
carry the brake in, or the throttle on, there is no time in between."

#### Throttle application on exit

*What.* How the throttle goes back on after the slowest point: when,
how fast to full, and whether it is lifted again before the exit.

*Right when.* On gravel in an AWD car, the throttle goes on *before* the
slowest point (the drive helps rotation and the differentials pull the
car straight), in steps or in one ramp; a brief lift on exit to catch a
slide is correction, not a fault, if the exit speed holds. On tarmac it
goes on at the slowest point and ramps to full as the steering unwinds.
RWD on loose: progressive, steering with it; stabs are intended.

*Mistake when.* Full throttle before the car can take it on tarmac
(wheelspin in FWD/RWD, understeer in AWD), a lift mid-exit with the car
not sliding (hesitation), several throttle drops on the way out (a
stab-stab-stab exit), or the throttle so late that `a_long` on exit is
below the best run's.

*Signature.* Per corner from the slowest point: `t(throttle ≥ 0.95) −
t(min speed)` (negative on gravel is normal), the count of throttle drops
over 0.3 before the exit, the exit speed against the best; `exit_spin`
where wheel speeds exist. `pedal.throttle_application` in the
derivations (planned, [ClickUp 86e3ev2dy]).

*Today.* Not measured.

*A pro says.* "Out of the 90-right at 2.7 km you lift twice on the way
out and the exit is 6 km/h down on your best: one throttle, held, as the
steering comes off." Gravel praise: "Throttle on 0.4 s before the
slowest point in the hairpins: that is what pulls the i20N straight."

#### Handbrake turns

*What.* The handbrake pulled at turn-in to lock the rears and rotate the
car, on hairpins and junctions; also as the launch's hold.

*Right when.* Rally hairpins on every surface (tarmac too: a FWD rally
car almost always), rallycross, drift initiation, junctions in a gymkhana.
The pull is short (0.3-0.8 s), at or just before turn-in, with the clutch
in on a car whose handbrake would stall the drivetrain (AWD without a
handbrake disconnect; the game may not model this), and the throttle
back on as the car rotates.

*Mistake when.* Circuit; a pull in a corner the car would have rotated
in anyway (the best run has none there and is quicker); a pull too long
(the car over-rotates, counter-steer and a slow exit); a pull too late
(mid-corner, the car was already turning).

*Signature.* `rig_handbrake` (ACR sends none from the game) rising edge
past 0.5 per corner, its duration, its time relative to the yaw onset,
`corners.handbrake`; against the same corner in the best run.

*Today.* `handbrake.per_km` is stored (ACR: not written, no game
channel; with the rig axis it could be) and not coached.

*A pro says.* "You pull the handbrake in 9 of 10 hairpins on Afon
Bidno; in the two tight lefts at 1.3 and 3.8 km your best run did not,
and was 0.3 s quicker through each: the car rotates without it there."
Or, for a long pull: "Your pulls last 1.2 s; the car over-rotates and
you wait for it. Half that."

#### Threshold and cadence braking, ABS and no ABS

*What.* Without ABS the driver finds the lock point and stays just under
it (threshold), or pumps the pedal when a wheel locks (cadence). With
ABS the pedal is held hard and the system modulates.

*Right when.* Cadence and a stepped brake trace are right in a non-ABS
car (historics, R-GT, some Rally2 setups; ACR's Alfa GTA, every DiRT
historic); with ABS a long hard brake application is right. On gravel a
locked inside front for a moment is used to rotate on purpose by some
drivers (non-ABS).

*Mistake when.* Pumping an ABS car (time lost: `brake` oscillating with
no wheel locked); a long lock on a non-ABS car (a flat spot, a car that
does not turn: wheel speed far below road speed for over 0.2 s with the
steering on and the yaw not following).

*Signature.* `brake` pedal oscillation count per braking zone; wheel
speed below 0.8 × road speed under `brake > 0.3` for over 0.2 s (needs
wheel speeds; ACR derives them roughly); whether the car has ABS from
car data (not in `acr.json` today).

*Today.* Not measured (`brake.lockup` planned).

*A pro says.* Non-ABS: "You lock the inside front into the hairpin at
4.4 km for half a second and the car does not turn until you release:
a little less pedal at the end of the braking." ABS: nothing about the
shape of the pedal.

### 3.3 Attitude and steering

#### Counter-steer and oversteer as the intended attitude

*What.* The car yawing more than the steering asks, the wheel turned
against the turn to hold it. On loose surfaces the normal way round a
corner: the car is set sideways on entry and driven out on the throttle
with the wheel pointing at the exit.

*Right when.* Gravel, snow, ice: in hairpins and tight corners nearly
always; in medium corners often; in fast corners a little. Tarmac rally
hairpins (the handbrake or a flick). Drift always. The share rises with
the corner's tightness: on marth's gravel captures 0.09 under 30°, 0.48
in hairpins, and on tarmac 0.01 in fast corners, 0.38 in hairpins.

*Mistake when.* Tarmac in fast and medium corners (a slide scrubs
speed, and the best run has less); on any surface when the counter-steer
comes late and large (a snap caught, not a slide set: `steer_rate` high
and `yaw_rate` peaking after the steer reverses); when the exit speed is
below the best run's in the same corner with more counter-steer; when
the share in a corner class is far above the driver's own quicker runs
of the same class.

*Signature.* `corners.counter_steer` per corner, binned by heading
change and surface, against the driver's own best run in each corner;
body slip angle (`atan2(vel.y, vel.x)`) peak and its timing (before the
slowest point: set on entry, intended; after it with the throttle on:
power oversteer, intended on loose; a late spike with a steer reversal:
a catch). `balance.gradient` is the car's balance (understeer sign),
separate from the driver's attitude.

*Today.* `counter_steer` is a per-run weighted mean, stored and used by
tuning (`COUNTER_STEER = 0.3` on tarmac hints at setup) but not coached.

*A pro says.* Gravel: nothing, or "You run the hairpins sideways and
your exits match your best: good." Tarmac: "Through the fast lefts on
Steigenbach you counter-steer in a third of them and lose 0.2 s in each
to your best run, which kept the car straight: on tarmac the slide is
the time. Less entry speed, more throttle on the way out." A catch: "The
right at 3.0 km: a snap at the exit, caught; the note is tightening,
your best run lifted earlier."

#### Scandinavian flick and pendulum turn

*What.* Before a tight corner, the car is steered briefly the wrong way
(away from the corner), often with a lift or a brake stab, so the weight
swings and the rear steps out towards the outside; the steering then
goes into the corner and the car swings through it already rotating.

*Right when.* Gravel, snow and ice, hairpins and tight corners, any
drivetrain; tarmac rally hairpins in some cars; never on a circuit.

*Mistake when.* The flick is so big the car is sideways before the
corner and scrubs (minimum speed and exit below the best run with the
flick larger than the best run's), or it is done in a corner where the
best run rotates the car without it.

*Signature.* Just before the corner: `steer` and `yaw_rate` going the
opposite way to the corner's direction for 0.3-1 s, then reversing; a
brake stab or lift in the same window; body slip rising before the
slowest point. At 10 Hz the flick is 3-10 rows: visible, not fine.

*Today.* Not measured; the corner finder would read the counter-yaw as
the tail of the previous corner or as noise under `CORNER_YAW`.

*A pro says.* "You flick into the hairpins at 1.3 and 4.6 km; in the
second the car is sideways 40 m before the apex and 8 km/h down on your
best. A smaller flick, later." Praise where it works.

#### Lift-off oversteer

*What.* A throttle lift (sometimes with a brake touch) at turn-in to
move weight forward and let the rear step out: the FWD driver's rotation
tool, used in every drivetrain on loose surfaces.

*Right when.* FWD everywhere; any car on loose surfaces at turn-in; a
tightening corner where the car needs to rotate more mid-corner.

*Mistake when.* A lift mid-corner on tarmac in a fast corner (the rear
lets go at speed: a fright, a catch, time lost); a lift on the straight
(hesitation); a lift at the exit (the throttle fault above).

*Signature.* A throttle drop of over 0.5 within 0.3 s at the onset of
`yaw_rate`, followed by the yaw rate rising faster than the steer asks
and body slip growing; in the entry phase; against the best run's lift
in the same corner.

*Today.* Counted in `pedal.coast` when it lasts.

*A pro says.* FWD on gravel: "You lift into every corner and the 208
rotates on it: right." Tarmac fast corner: "The lift at the apex of the
fast right at 5.2 km snapped the rear; your best run stayed on 30 %
throttle through it."

#### Power-on drift and four-wheel drift (AWD)

*What.* The car driven through and out of the corner on the throttle
with all four wheels sliding, the steering near straight or against,
the nose pointing at the exit before the car is there.

*Right when.* AWD on gravel and snow: the normal exit; RWD on gravel
with the tail hanging. Drift discipline always.

*Mistake when.* Tarmac (except hairpins): a four-wheel drift on tarmac
is slower than a grip exit; on any surface when the slide carries past
the exit and the car has to be gathered (counter-steer after the exit
point, a lift at full throttle).

*Signature.* Body slip 5-15° with `throttle > 0.8` after the slowest
point; `exit_spin` where wheel speeds exist; exit speed against the
best; counter-steer continuing past the exit.

*Today.* Not measured as such.

*A pro says.* Gravel: nothing. Tarmac: "Out of the medium rights on
Alsace you drive out with 8° of slide and lose 0.2 s to your best, which
kept the car hooked up."

#### Sawing at the wheel and steering smoothness

*What.* Fast, repeated corrections at the wheel.

*Right when.* Loose surfaces: small fast corrections are how a sliding
car is held; on ice continuously. A single big correction catching a
snap is right whatever the surface (the alternative was an off).

*Mistake when.* Tarmac in fast and medium corners: steering rate high
with the car not sliding (`body_slip` low) means the driver is
over-driving the front or searching for the line; on any surface,
corrections growing in amplitude (a tank-slapper).

*Signature.* RMS of `steer_rate` (10 Hz: coarse) over cornering time,
per surface, against the driver's own quicker runs; the number of steer
sign reversals per corner; `profile.smoothness` (planned).

*Today.* Not measured.

*A pro says.* Tarmac: "Your hands are busier through Steigenbach's fast
section than in your best run, with the car not sliding: pick the line
and hold the wheel still." Gravel: nothing unless the car is clearly
being caught corner after corner.

#### Understeer: pushing on tarmac, washing out on gravel

*What.* The front sliding more than the rear: the steering turned more
than the yaw follows.

*Right when.* Never desired; but on gravel a little understeer on the
throttle in an AWD car is the setup pulling the car straight on exit,
and on ice everything understeers at the limit.

*Mistake when.* Entry understeer on tarmac (too fast in, the brake
released too early); mid-corner push with the throttle on in AWD (the
throttle came too early for the rotation); the driver adding lock as the
car runs wide (lock up, yaw not following).

*Signature.* `balance.gradient` (lock per g, the car); per corner, the
peak `|steer|` relative to the curvature the yaw gives (`yaw_rate /
speed`) and whether lock rises while yaw rate falls (the driver asking
for more and getting less); exit speed against the best.

*Today.* `balance.gradient` is a car-and-tune figure for tuning, never a
per-corner driving tip.

*A pro says.* "Into the 90-left at 2.2 km you add lock for a second and
the car keeps running wide; your best run came in 5 km/h slower and was
0.3 s quicker through it: slower in, earlier on the throttle." Setup
version, when the gradient has moved between tunes, stays with tuning.

#### Rotation timing: slowest point before the apex on gravel

*What.* Where in the corner the car is slowest and where it is rotated.
On gravel the car is rotated early and the slowest point comes before
the geometric apex, the exit is straight and long; on tarmac the slowest
point is at or after the apex (a late apex), the entry is long and the
exit short.

*Right when.* Each on its own surface; a hairpin on tarmac is driven
like gravel.

*Mistake when.* The gravel shape on tarmac (an early rotation that
needs the slide to finish, scrubbing); the tarmac shape on gravel (deep
braking into the corner, the rotation late, the exit compromised: an
"understeer, then oversteer" corner).

*Signature.* `corners.d` (the slowest point) relative to the yaw-rate
peak and to the midpoint of the yaw window; `yaw_rate` peak time
relative to the slowest point; against the best run.

*Today.* The slowest point is found; its position is not used.

*A pro says.* Gravel: "In the 90-rights your slowest point is past the
apex, with the rotation after it: the car is turned too late. Brake
earlier, rotate it before the apex, and it will leave straighter."

#### Drift as a discipline

Every rule above inverts: counter-steer near 100 % of the corner, the
throttle high, body slip 20-40°, clutch kicks and handbrake initiations
intended. The coach's driving tips are all wrong there; what it can
coach is consistency (the same angle, the same line, run after run),
mechanical sympathy (over-revs, stalls) and transitions (the time the
car spends straight between corners). The discipline tier decides this
(`drift` from the profile name or, one day, from the dynamics), and
every family in §3.1-3.3 should be silent on a `drift` run.

### 3.4 Road, stage and discipline

#### Flat-out crests and jumps

*What.* A crest taken flat where the note says so; a jump taken with a
lift before the crest so the car lands nose-first-not-nose-up, the
steering straight in the air, no brake in the air, the throttle back on
at touchdown.

*Right when.* Rally, every surface: a lift of 0.3-0.8 s approaching a
jump is technique, not hesitation; landing straight is technique; a
throttle touch in the air to level the car is technique in some cars.

*Mistake when.* Braking in the air (the nose drops, the car lands on
the front: `a_vert` spike with `brake > 0.1` during airborne time);
steering in the air (a landing sideways); a lift so long the car is
slow for the next 200 m when the note said flat; a jump taken flat the
note said to lift for (a bottoming landing, `susp_rms` or `susp_norm`
saturation after an airborne phase).

*Signature.* Airborne: `a_vert` near −g (specific) or the wheels
unloaded for over 0.15 s; the landing: the `a_vert` peak and suspension
compression; in the air: brake, steer, throttle; before: the lift's
timing and length; after: speed at 100 m against the best run. The
derivations have `vehicle.jumps` and `vehicle.bottoming` (planned); ACR's
`susp` is decoded but its sign and zero unverified.

*Today.* Not measured.

*A pro says.* "The jump at 3.3 km: you brake in the air and land on the
nose, then lose 0.4 s to the next corner. Lift before the crest, feet
still in the air, throttle as it lands." Praise: "Flat over the crest at
1.9 km every run."

#### Pace-note driving: committing blind

*What.* Driving to the co-driver's call, braking and turning for a
corner that cannot be seen yet, carrying speed over a blind crest the
note says is flat.

*Right when.* Every rally stage; the technique is trust, and it shows as
consistency: the same speed for the same call, the same braking point
relative to the note's distance, run after run.

*Mistake when.* Speed through a given grade of note varying widely
between runs with the same car (not trusting the note); braking late
for a call that was clear (a mis-heard or mis-timed call: the pro asks
about the notes before the driving); over-slowing for a grade the
driver's own faster runs took quicker.

*Signature.* Needs the note list placed along the spline
([ClickUp 86e3faftn]): per note, the minimum speed and the braking
point relative to the note's distance, per grade (1-6 or the game's
scale), and their spread across runs. Without the notes, the corner's
heading change and the best run stand in (`corner.pace_note` in the
derivations, planned).

*Today.* Not measured; the table has only the first and last note
distances.

*A pro says.* "Your speed through 4-grade corners varies by 12 km/h
between runs on Afon Bidno; through 3s and 5s by 4. You are not sure
what a 4 is: pick the speed from your quickest clean run and commit to
it." Or: "Every blind crest you are 8 km/h down on your best run; the
notes say flat and you were right every time you trusted them."

#### Surface changes within a stage and cuts

*What.* A stage that goes from gravel to tarmac and back (ACR Greece
Elatia, 85/15; Monte-Carlo), or a tarmac stage where cuts drag gravel
onto the road.

*Right when.* Technique changes at the join: braking earlier on the way
on to gravel, the first corner on tarmac after gravel driven carefully
(tyres, mud). A corner after a known cut driven with margin.

*Mistake when.* Treating the whole run as one surface, which the coach
does by run today.

*Signature.* Per-segment surface (`segments.surface`, measured; the
table's `surface_parts` for the prior); every per-corner metric keyed by
the segment's surface, not the run's.

*Today.* Metrics are keyed by the run's surface; a `mixed:` run is
treated as loose by `loose()`, which is the safe side.

*A pro says.* "Elatia's tarmac section: your corners there are driven
gravel-style, sideways, and lose 0.9 s to your best. Tarmac rules for
the 2 km from 4.1 km."

#### Snow and ice

*What.* Studded tyres on snow and ice (Sweden, Livigno): grip comes
from the studs biting; everything is earlier, gentler and longer: the
braking, the rotation, the throttle. Snow banks are used as a cushion on
the outside of corners (the "Scandinavian bank", intended by good
drivers, a crash by others).

*Right when.* Counter-steer most of the corner, partial throttle most
of the time, a long coast in the rotation, the slowest point well before
the apex, a higher gear everywhere: all technique on ice.

*Mistake when.* Hitting the bank hard (an `a_lat` spike with a speed
loss), locking (no ABS), a snap on the throttle.

*Signature.* The surface class decides; the thresholds that are
absolute today (`OVERLAP_SHARE`, `COAST_MORE`, `LIMITER_PER_KM`) should
be per surface, and on `snow`/`ice` the driving families go to
description and consistency.

*Today.* `snow` and `ice` are in `LOOSE`; the metrics' absolute
thresholds are not per surface.

#### Hillclimb

*What.* A point-to-point stage uphill, usually tarmac, often with
hairpins and short straights.

*Right when.* Under-gearing is right: gravity loads the engine and the
next gear falls off boost on a steep pitch; touching the cut on a short
uphill straight is right; a lower gear on a downhill section for engine
braking is right. The launch is standing, on tarmac, often with a
rolling start zone.

*Mistake when.* A change up on a steep pitch that lands below the band
(the car dies: `a_long` well below the car's drive at that rpm, `grade`
known from `vel`); a long gear into a hairpin.

*Signature.* `grade` from the velocity vector (the learner already
removes slope from power); the shift decision per gear per grade band.

*Today.* The discipline tier detects hillclimb from elevation; nothing
grade-aware in the coach.

#### Rallycross

*What.* Short mixed-surface laps from a standing grid start, a joker
lap, contact.

*Right when.* The launch is everything (reaction and t50); the
tarmac-to-gravel join inside the lap changes the
braking every time; a higher gear through the gravel section to limit
wheelspin; handbrake in the joker hairpin; the car driven sideways on
the gravel, hooked up on the tarmac.

*Mistake when.* As for rally per segment surface, with contact
(`a_lat`/`a_long` spikes with no corner) separated out.

*Signature.* Discipline `rallycross` (DiRT laps under 1.5 km on loose);
per-segment surface; the launch metrics; lap consistency.

*Today.* The discipline is detected; the metrics run as for rally, per
run surface, which is `mixed` on a rallycross lap.

#### Circuit (where rally techniques become mistakes)

On a circuit in a non-turbo car: the overlap is a fault (except the
heel-and-toe blip), counter-steer is a fault, coasting is a fault,
under-gearing is a fault (the straights are long), the handbrake does
not exist, braking is trailed to the apex, the slowest point is at the
apex or after. The coach's rules were mostly written for this, and the
tarmac gates in `coach.py` are the right default for `circuit`. In a
turbo circuit car the entry overlap returns as boost management. On the
Livigno ice circuit (ACR, detected `circuit` by the topology tier where
position exists, surface `snow`) the rally rules apply: surface before
discipline there.

### 3.5 Car-specific

- **FWD** (Rally4, Rally5, historic Golf/205/Kadett): rotation comes
  from the lift, the brake (left foot) and the handbrake; the throttle
  pulls the car straight; wheelspin on exit is the limit on every
  surface; a higher exit gear is often right. Counter-steer is rare and
  short (the nose, not the tail, is steered). Launches: progressive
  clutch, fewer revs.
- **RWD** (historic Escort, Alfa GTA, 911, R-GT): power oversteer is the
  steering on loose surfaces, throttle modulation is the technique,
  counter-steer high on every surface in slow corners; the launch is a
  throttle exercise; H-pattern nearly always, so heel-and-toe applies.
- **AWD** (Rally2, Rally1, Group A/B, WRC): the throttle before the
  slowest point, four-wheel drift exits on loose, the car pulled
  straight by the differentials; understeer on throttle with the wrong
  diff setup; launches hook up with a rev drop (§3.1); the handbrake may
  need the clutch.
- **Turbo vs naturally aspirated**: the overlap, the low gear and the
  "late" change are boost management in a turbo car; in an NA car the
  band is wider and the lift costs nothing but time.
- **Sequential vs H-pattern**: the sequential needs no clutch rolling
  and shifts under full throttle (a cut); the H-pattern needs the
  clutch, a lift, and rev-matching down; `method` already separates
  them and the coach compares the same driver across methods.
- **ABS, traction control, launch control**: where the car has them the
  pedal shape, the exit spin and the launch slip are the system's, not
  the driver's; the car data needs the flags (not in `acr.json`).
- **Hybrid (Rally1)**: boost on demand changes the exit; only EA WRC
  models it and sends nothing about it.

### 3.6 Session context the coach must know

- **A first run of a stage** is a learning run: no comparison with "your
  best" (there is none), no corner-loss tip; the coach may describe.
- **A run after a long gap** (days): the first run is warm-up; coach
  from the second.
- **A run with an off, a stuck car or a crash** (speed collapsing to
  near zero with no brake; reverse gear; `a_lat`/`a_long` spikes;
  wheelspin at a standstill as in run 21): the run's aggregates are
  poisoned (limiter time, coasting, launch); the coach says "you went
  off at 1.7 km" and coaches the corners before it, not the recovery.
- **A restarted run** (`finished = 0`, short): the driver restarted on
  purpose; nothing to coach but what happened before the restart, and
  the coach should not count it as a launch attempt for "bogged" tallies
  unless the launch itself is what went wrong.
- **Setup changes** (`tunes`): the shift points relearn (done); the
  balance figures reset (done, tuning).

## 4. Rule changes this implies, in priority order

Each is phrased as the rule it changes in `coach.py` and the gates it
needs; thresholds to **calibrate**.

1. **Change-up target = the game's band, not the cut.** `best_for` keeps
   the crossover as the floor; the target band for `shift.error` and
   `shift.in_band` becomes `[max(crossover, shift_lights.shift),
   min(shift_lights.late, limiter − margin)]`, with `margin` 150-250
   rpm on a sequential and 300-400 on an H-pattern where the car has no
   lights data. "Early" is below the band's floor; "late" is at or past
   the cut. The sentence quotes the lights ("the lights say 6900-7100")
   and never says "hold it to <the limiter>". Where the game ships no
   lights, the band is crossover to `limiter − margin`. This alone
   removes the owner's complaint on both Rally2 cars.
2. **Limiter time becomes limiter episodes with context.** A speed floor
   (≥ 8 m/s; the launch hold and a stuck car are not limiter time); per
   episode the length in m and s, the distance to the next apex and
   what followed. Only episodes longer than ~1.5 s or ~80 m that are
   not followed by a change up, or that are up-then-down, are candidate
   faults, and only when the best run of the stage was quicker over the
   same stretch. The tip names the place. Short touches at the shift
   point move to the change-up band (item 1). The stuck-car run is
   recognised (§3.6) and its aggregates dropped.
3. **Launch bog by outcome.** `launch.bog` = revs below 60 % *and*
   `a_long` under ~0.3 g over the first 0.5 s after release, or
   `launch.t50` more than ~0.5 s over the driver's median for the car
   and surface. Praise steady fast launches. Per drivetrain where the
   profile has several cars.
4. **Pedal overlap by phase and car.** Split entry, rotation, exit,
   straight; exclude the window around H-pattern changes down. Tip only
   on exit-phase and straight overlap, on tarmac or circuit, and only
   when the exits with the overlap are slower than the driver's own
   best in those corners (not when the run as a whole lost 0.5 s). Entry
   overlap in a turbo car is described or praised.
5. **Coasting by phase.** Rotation-phase coasting on loose surfaces is
   not a fault; the tip targets post-slowest-point coasting on every
   surface and dead time between brake and throttle on tarmac, each per
   corner against the best run. Rewrite the sentence.
6. **Counter-steer per corner class and surface**, against the driver's
   own best run of the stage, never absolute: a tip only on tarmac and
   circuit in corners under ~90° where the best run had less and was
   quicker; description elsewhere. Add the catch signature (a late
   steer reversal with a yaw spike) as its own count.
7. **Corner loss by phase and cause.** Split the `corner_loss` window
   at the slowest point (entry loss, exit loss); attach the entry's
   braking point and minimum speed and the exit's throttle-on time and
   exit speed against the best run; name the corner by distance (and
   by note once the notes exist). The "three worst corners" tip then
   says what was lost where and why.
8. **Exit gear with slip.** `exit.low` to tuning only where the exits
   below the band are also bogs (`a_long` well below the car's drive at
   that rpm and gear) or where `exit_spin` shows the lower gear would
   have stuck; on loose surfaces a higher exit gear is otherwise
   technique.
9. **Per-surface thresholds and the discipline switch.** `OVERLAP_SHARE`,
   `COAST_MORE`, `LIMITER_PER_KM` per surface; every driving family
   silent on `drift`; rally rules on a `circuit` run whose surface is
   snow or ice. Key per-corner metrics by the segment's surface on
   `mixed` runs.
10. **Session context gates** (§3.6): the first run of a stage, a run
    with an off or a stuck car, and a restart are classified before the
    metrics are written, and the coach reads the flag.
11. **ACR discipline from the stage table**: a known rally-stage key is
    `rally-stage` with `game` confidence, the Livigno keys `circuit`;
    the position-based tiers stay for unknown keys. (Found in §1; small
    and worth doing first because item 9 depends on the discipline.)

## 5. What professional coaching looks for that is not measured yet

Ranked by value for the owner's driving (ACR rally first), with the
channels each needs and whether ACR has them.

| # | What a pro looks at | Why it matters | Needs | ACR |
|---|---|---|---|---|
| 1 | **Corner phases against the reference run**: braking point, minimum speed and where it is, throttle-on time, exit speed, time lost in each phase, per corner | The basic tool of every coaching session; turns "you lost 1.5 s in three corners" into "you brake 30 m early into the left at 1.8 km" | trace, corners, best run | yes |
| 2 | **Pace-note context**: speed per note grade and its spread; braking relative to the called corner | Commitment and trust in the notes are what separate rally drivers; a speed-per-grade table is what a co-driver and coach work from | the note list along the spline ([ClickUp 86e3faftn]) | in game files |
| 3 | **Rotation timing and slip angle**: yaw peak and slowest point relative to the apex; body slip peak and when it comes (entry set, power slide, catch) | The surface-specific shape of a corner; tells a set slide from a caught one | yaw_rate, vel (body slip), corners | yes |
| 4 | **Throttle application**: time to full after the slowest point, drops before the exit, throttle before the slowest point on gravel | Exit speed is where rally time is made; planned ([ClickUp 86e3ev2dy]) | throttle, corners | yes |
| 5 | **Launch judged by outcome**: t50 against own median per car and surface, slip, first-0.5 s g | Every stage starts with one; the current bog rule misreads AWD launches | speed, a_long, rpm, wheel speeds | yes (slip thin) |
| 6 | **Jumps and crests**: lift before, feet in the air, landing attitude, speed after | Rally-specific, high cost when wrong, invisible to every current metric | a_vert, susp, throttle, brake, steer | a_vert yes; susp unverified |
| 7 | **Braking shape**: onset vs best run and vs note, peak decel, trail into the turn, lock-ups | Entry is half the corner; planned in the derivations | brake, a_long, yaw_rate, wheel speeds | lock-ups thin |
| 8 | **Steering smoothness and corrections** per surface | Over-driving on tarmac shows in the hands first | steer at 10 Hz | yes (coarse) |
| 9 | **Hesitation**: lifts and brake taps on straights with no corner, speed at blind crests against the best | Confidence and commitment, which a pro reads from these before anything else | throttle, brake, corners, best run | yes |
| 10 | **Clutch and gearbox handling**: clutch use on a sequential, rev-match quality on an H-pattern | Mechanical sympathy and tenths per change; the rig gives the inputs | clutch, rig throttle, engage rpm | yes |
| 11 | **Offs, hits and stuck cars** | A run with an off is a different conversation; the recovery poisons every aggregate | speed, a_lat/a_long, gear (reverse), distance | yes |
| 12 | **Pace within the run**: first vs last third against the best; split SD within a run | Concentration, tyre and brake management on long stages | distance, t | yes |
| 13 | **Setup vs driving separation** (planned `profile.setup_lean`) | A pro changes the driver or the car, not both | balance per tune, times | yes |
| 14 | **Line and road use**: cutting, using the width, ditch-hooking | What a pro sees from the roadside; needs road edges nobody has | pos, road geometry | pos only on new captures |
| 15 | **Tyre and brake temperatures, wear** | Management on long stages and over a rally | not sent through the bridge (verify: AC's physics page has tyre core temperatures) | verify |

## 6. Open and to verify

- The game's `shift_lights_rpm` as the band (item 1 of §4): confirm on
  the Fabia and i20N that the lights' `shift` matches where the game's
  own AI changes (its `auto_upshift_rpm` is 7450, oddly near the cut:
  which one the designers mean as "the" point needs a look in the game).
- The ACR `susp` sign and zero, before any jump or bottoming rule.
- ACR wheel speeds through the learnt tyre radius: good enough for
  `exit_spin` and lock-ups, or not. The trace's `slip_drive` is NaN on
  most ACR rows today.
- Whether ACR models the handbrake stalling an AWD car without the
  clutch (affects the handbrake entry's "right" case).
- The WRC Generations counter-steer and steer sign (0.55 of cornering
  time on the 2026-09-28 replay; `docs/coaching-derivations.md`): until
  verified, every attitude rule stays silent on `wrcg`.
- The Fabia's `drivetrain = fwd` in the replayed database, against the
  shipped `awd`: where the learnt vote overrides the data.
- ACR discipline `unknown` on every run, the newer captures included:
  whether the bridge on marth's rig sends position yet.

[ClickUp 86e3jeaye]: https://app.clickup.com/t/86e3jeaye
[ClickUp 86e3faftn]: https://app.clickup.com/t/86e3faftn
[ClickUp 86e3ev2dy]: https://app.clickup.com/t/86e3ev2dy

## Owner's requirements (marth, 2026-10-05)

These apply to every later stage (audit, design, oversight, build):

1. **Say what was good, more often.** Praise is coaching too: name good
   technique when the data shows it (a clean launch, a well-held gear
   through a short straight, consistent braking points, a tidy flick,
   shifts on the best point), not only improvement over time. Keep it
   specific and earned (a number or a place), never filler, and balance it
   against tips so a session doesn't read as a list of faults.
2. **Advice on specific turns and places.** Beyond per-gear and per-session
   averages, tie observations to where they happened: the corner (by the
   stage's pace notes where known, e.g. "the hairpin left after the
   bridge", else by distance along the stage and the corner's direction and
   tightness), with what was done there and what to try, compared with the
   driver's own best run through that corner. ACR first (its stage tables
   carry pace-note distances; full pace-note lists are in the game files,
   ClickUp 86e3faftn).
3. **Technique-aware, as the owner's original note says** (ClickUp
   86e3jeaye): e.g. the limiter between corners on gravel is often right,
   not a fault.

## 7. Design: a technique-aware coach (2026-10-05)

This section turns the catalogue (§2-§5) and the audit of 2026-10-05 into
a design. The audit replayed the fifteen captures through the current code
and also ran the coach on a copy of marth's live database. Of the 22
distinct things the coach says about the three ACR cars, 1 was fair, 5
were right in direction but had the wrong target, 9 were wrong and 7 could
not be judged. About half of the wrong ones misread technique (the
limiter, launches, coasting, left-foot braking). The other half come from
errors in the numbers before any rule reads them: corner windows that
overlap, a "best run" that is a 300 m restart or a different car, a stage
timed to the moment the car was parked after the finish. The design
therefore puts a **context layer** between the trace and the rules. The
layer classifies every event before a rule judges it, and stores the
class with the event. Each rule then reads the class and the gates
(surface, discipline, car) and does one of four things: correct, praise,
describe, or stay silent.

Every number marked **calibrate** is a starting value. Where it comes
from marth's captures, the evidence is given.

### 7.1 What the data supports (ACR first)

What can be built now on Assetto Corsa Rally, and what cannot:

- **Have:** the 10 Hz trace with speed, rpm, gear, throttle, brake, steer,
  a_long, a_lat, yaw rate and distance; corners from yaw; shift rows; the
  stage from the table, with its surface and `surface_parts`; finished
  and result time; the game's car data (limiter, `shift_lights_rpm`,
  `auto_upshift_rpm`, gear sets, drivetrain, turbo).
- **Do not have:**
  - Wheel slip: `slip_drive` is NaN on nearly every ACR row.
  - The game's handbrake, and the rig's handbrake in captures.
  - The shifter method in replays. Live data has it; marth's live
    database is all "sequential".
  - Position in older captures.
  - Pace-note lists. The table has only the first and last note, and
    those are in the track spline's coordinates ([ClickUp 86e3faftn]).
  - Per-segment surface: `segments.surface` is filled only where a
    calibration is deployed.
  - The suspension sign and zero, which are unverified.
- **So:**
  - Everything below runs on the channels in "Have".
  - Rules that need slip, the handbrake, the method or a per-segment
    surface are written with their gate closed. They stay silent and say
    once why.
  - Corners are named by distance, direction and tightness until the note
    lists exist.
  - WRC Generations is excluded from launch and attitude rules until its
    speed and steer signals are checked (§6).

### 7.2 The context layer: a new module `oversteer/coach_context.py`

The new module holds pure functions over (summary, trace, corners,
shifts, car data). Its results are written at run end on the drive-log
thread, next to the run's metrics. `coach.py` keeps only the rules.
Nothing in the module reads the database except the reference lookup,
which takes the store as an argument, the way `stage_metrics` does.

#### 7.2.1 Run class and reference

- **Finish cut.** Store the finish distance on the run, `runs.course`
  (the tracker's `_finish_d` from the summary's `course`). Every
  per-stage comparison uses `stage_rows(trace, course)`: the rows up to
  the first row at or past `course`, and for a finished run no row past
  `result_time` + 1 s. This fixes the 68.6 s split of run 45, which
  carried 1377 s of standing after the finish.
- **Incidents.** An incident is a stretch where the speed is under 3 m/s
  for at least 1 s. It must start past the first 100 m and end before
  the last 50 m (**calibrate**). It is an **off** when one of these holds:
  - reverse gear is engaged in it;
  - the speed fell from above 15 m/s within the 3 s before it;
  - the speed drops more than 40 % in 1 s with the brake under 0.3.

  Otherwise it is a **stop**. In the replay, 34 such stretches turn up in
  15 runs, most of them with reverse or from more than 20 m/s, including
  run 7 at 2.1 km. Each incident goes in `events`, kind `off` or `stop`.
  The corners within ±100 m of an incident are marked `corners.off = 1`.
- **Run class** (`runs.run_class`):
  - `restart`: not finished and under 50 % of the stage.
  - `partial`: not finished, 50 % or more.
  - `off`: finished with at least one off.
  - `learning`: the first run of this car on this stage, or the first
    after 7 days away (**calibrate**).
  - `clean`: everything else.

  The class decides what a run counts towards:

  | Run class | Reference run | Section best | Coaching tips | Launch | Coasting/overlap per km | Stuck time in limiter |
  |---|---|---|---|---|---|---|
  | clean | yes | yes | yes | yes | yes | n/a |
  | learning | yes, once it is the best | yes | describe only | yes | yes | n/a |
  | off | no | sections away from the off | sections before the off, plus "off at 2.1 km" | yes | excluding ±100 m of the off | dropped |
  | partial / restart | no | sections it covers cleanly | the sections it covers | yes, unless restarted within 10 s of the start | no | dropped |

- **Reference run.** The reference is the fastest finished run of the
  same car (the same `cars.id`) on the same stage, with run class `clean`
  or `learning`. `Store.stage_runs` gains `car` and `run_class` filters.
  This removes the Peugeot being compared with the Fabia's run 7, and the
  "best" of 0 s/km coasting that came from a 220 m restart. Tips name the
  reference: "your best run here in the Fabia (227.6 s on 3 Oct)".

#### 7.2.2 Corner sections, complexes and names

- **Sections.** `find_corners` already finds each corner's yaw window
  (rows i..j). It also stores the window's ends, `corners.d0` and
  `corners.d1`. The stage is cut into non-overlapping **sections**: a
  section runs from the midpoint between the previous corner's `d1` and
  this corner's `d0`, to the midpoint before the next corner. Section
  times add up to the run's time, so nothing is counted twice. This
  replaces the ±50 m windows that counted run 7's off three times. On
  gravel the median gap between corners is 41 m, so the old windows
  overlapped nearly everywhere, offs or not.
- **Complexes.** Corners less than 30 m apart (**calibrate**) are joined
  into one complex and one section. On the replay this turns 1170 corners
  into 732 sections. With 20 m it gives 801; with 50 m, 585. The complex
  keeps the corners' directions for its name ("the left-right").
- **Names.** Tightness comes from the heading change:
  - **hairpin**: 135° or more;
  - **square**: 75° to 135°;
  - **medium**: 40° to 75°;
  - **fast**: under 40° with a minimum speed above 20 m/s.

  Thresholds are **calibrate**. A corner is named "the square left at
  1.8 km". A complex is named "the left-right at 2.1 km". When the
  pace-note lists arrive, the call replaces tightness: "the 3 left after
  the jump".
- **Phases per section**, from this run's trace:
  - **approach**: from the section start to the brake onset, or to `d0`;
  - **entry**: from the brake onset or `d0` to the slowest point;
  - **exit**: from the slowest point until the throttle is above 0.95
    with |yaw| under 0.1, or the section end.

  Stored per corner as new `corners` columns:
  - `brake_d`: metres before the slowest point where the last brake
    onset above 0.1 began;
  - `throttle_t`: seconds from the slowest point to throttle at or above
    0.95 (negative means before it);
  - `relifts`: throttle drops of more than 0.3 in the exit;
  - `coast_entry` and `coast_exit`: seconds with both pedals under 0.05
    above 10 m/s;
  - `overlap_entry` and `overlap_exit`: seconds with both pedals above
    0.2;
  - `section_t`: seconds through the section.

  `min_speed` and `exit_speed` are already stored. The trace allows all
  of this at 10 Hz.
- **Loss per section** is this run's `section_t` minus the reference's
  time over the same distances. It is split at this run's slowest point
  into `loss_entry` and `loss_exit`. The **section best** is the quickest
  time through the section by any run of the car that covers it with no
  off within ±100 m, partial runs included. The sum of the section bests
  is the "possible" time for the stage. These are worked out at run end
  and stored as `corners.loss_entry`, `corners.loss_exit` and
  `corners.best_t` (the section best at that time). The cost is one
  reference trace and a lookup of the section bests.

#### 7.2.3 Events

There is a new `events` table: (`run`, `kind`, `class`, `d0`, `d1`, `t0`,
`t1`, `gear`, `value`, `detail` JSON). Kinds are `limiter`, `off`, `stop`
and `launch`. Shift rows gain `d`, the distance at the change, and
`flags` gains `cut` and `launch` (below). One table holds every located
event, so the coach and the web page can say where something happened
without reading a trace.

### 7.3 Rules: classifier, gates, what is said

#### R1. Change-up target: the game's lights band, judged as a distribution

**Classifier.** Each flat-out change up by one is one of these:

- `early`: below the band's low end minus 100 rpm.
- `on`: inside the band, ±100 rpm.
- `late`: above the band but under the cut.
- `cut`: the peak in the last 0.3 s is at or above 0.985 × limiter, or a
  limiter episode ended in this change. Flag `cut`.
- `launch`: the 1→2 within 3 s of a launch. This change belongs to R3,
  not here. Flag `launch`.

**The band.**

- **Low end:** the game's `shift_lights_rpm.shift`. If the gear is
  grip-limited on the surface, the low end is the grip-lowered best,
  under two conditions (**calibrate**):
  - the grip comes from 6 or more pulls;
  - the measured share is plausible: 1.0 or less, and the lowering is
    never applied to 3rd or higher on tarmac.
- **High end:** the smaller of `shift_lights_rpm.late` and the limiter
  minus a margin. The margin is 150 rpm on a sequential and 300 on an
  H-pattern (**calibrate**).
- **Without lights:** where the game ships no lights, the band runs from
  the crossover (`best_for`) to the limiter minus the margin.

The torque crossover stays as the explanation ("4th pulls 6 % less at
6470") and is never the target. The lights do not depend on the gearing,
so the 208, whose gearing is set aside, gets a band of 6000 to 6150.
The Fabia's band is 6900 to 7100 and the i20N's is 7000 to 7250.

**Stored as:**

- `shifts.best_low` and `shifts.best_high` hold the band. They already
  mean "the target range"; this is documented.
- `shifts.best` keeps the crossover for the drive lost.
- New metrics per gear and method: `shift.early_share` and
  `shift.cut_share`. `shift.in_band` keeps its name and moves to the
  band. `shift.error`, the mean, goes.

**Gates and decisions** (per car, surface, gear and method; 5 or more
changes):

| Pattern (**calibrate**) | Tarmac | Gravel, snow, ice, mixed |
|---|---|---|
| cut share at least 25 % | tip | tip |
| early share at least 60 % | tip, with the drive lost | 1st and 2nd: silent (short-shifting is technique). 3rd and up: tip only when the gear is measured *not* grip-limited by the plausible-grip rule above; otherwise a note, once |
| early at least 30 % and cut at least 25 % ("two places") | tip | tip |
| on share at least 60 % and cut share at most 10 % | praise | praise |

What the audit's cases become:

- **Fabia gravel 3→4** (80 % early): tip, target 6900-7100, provided
  the gear is measured not grip-limited. Otherwise a once-only note.
- **Fabia 2→3** (50 % early, 28 % cut): the two-places tip.
- **Fabia 1→2** (68 % cut): mostly launch changes, so R3 takes them.
  The non-launch rest is judged on its own.
- **208 tarmac 1→2** (100 % cut) and **2→3** (36 %): cut tips. Today
  they are missed.

`advice()` in `shift_learner.py` takes the same band and shares. "Spot
on" needs the praise row of the table. The live line "N s on the limiter
this session" uses R2's `held-straight` seconds only.

#### R2. Limiter episodes classified by what follows

**Episode.** Rows with rpm at or above 0.985 × limiter, throttle at or
above 0.95, the same gear, and the gear below top. When the game sends no
gear count, the top gear comes from the shipped gear set; this also lets
the final-drive notes run on ACR.

**Limiter figure.** Keep the game's figure when a launch measured within
2 % of it, because a launch overshoots the cut. This removes the WRCG
"7460 threshold" and the "hold it to 7577" in the live database.

**Classes:**

| Class | Rule (**calibrate**) | Replay count (episodes at or above 8 m/s: 50) | Coach |
|---|---|---|---|
| `crawl` | mean speed under 8 m/s | 28 episodes, 8.5 s | never; it feeds the off/stuck test |
| `shift` | ended by a change up within 1.0 s | 31; touch length p90 0.2 s | R1 (`cut` flag) |
| `up-down` | change up, then down within 4 s | 8 | describe only (it is the alternative to holding) |
| `held-corner` | ended by brake above 0.2 or a change down, or inside a corner, or the next section's `d0` within 100 m or 3 s | 11, the longest 1.8 s / 30 m on gravel with the next corner 29 m on | silent on loose surfaces and in hairpin complexes on tarmac; when it recurs at the same place in at least 3 of 5 runs and exceeds 1 s, a gearing description: "3rd is short for the run from 2.0 to 2.1 km; holding it there is right" |
| `held-straight` | 1.5 s or more, or 80 m or more on the cut, with no change up within 1 s and the next corner more than 100 m away | 0 | tip, only when the reference run was at least 0.1 s quicker from the episode's start to the next brake onset, or was in a higher gear there |

**Stored as:** `events` (kind `limiter`). The metric
`limiter.per_km` is renamed `limiter.held`: seconds of `held-straight`
per km, a different quantity under a new name. The current
`LIMITER_PER_KM` threshold applies to it.

On marth's captures this rule says nothing, which is the right answer.
The owner's complaint about the limiter between corners becomes, by
construction, the silent `held-corner` class.

#### R3. Launch judged by outcome

**Classifier.**

- **Revs** are read only while in 1st, from the release to the first
  change.
- **`launch.g`** (new metric) is the mean a_long over the first 0.5 s of
  the trace in 1st. ACR's trace starts at 3 m/s, so this is the first
  half second it holds.
- **Bog:** `launch.g` under 0.3 g, or `launch.t50` more than 0.5 s over
  the median of this car's last 10 launches on this surface. The rev drop
  alone is never a bog (**calibrate**).
- **Stall:** as today.
- **Game-controlled:** when the SD of `launch.t50` over 5 or more
  launches of a car on a surface is under 0.05 s, the game is taken to
  run the launch (the 208's 3.36-3.39 s). Neither tips nor praise.
- **Restart within 10 s of the start:** the launch is dropped; the
  driver aborted it.

**Stored as:** `events` (kind `launch`), with `detail` holding release
rpm, the 1st-gear minimum and t50. The launch 1→2 is flagged `launch`
in `shifts`.

**Gates.** Every surface and drivetrain. WRCG is excluded until its speed
channel is checked.

**Praise:** the last 5 launches within 0.1 s of each other and within
0.1 s of the car's best on the surface. For example, all 25 i20N launches
took 1.83 to 1.94 s. A cut held in 1st after the release for more than
0.3 s (**calibrate**) is a tip: "change to 2nd as the cut comes in".

#### R4. Pedal overlap by phase

**Classifier.** The `overlap_entry` and `overlap_exit` corner columns
from §7.2.2. A **straight overlap** is overlap more than 100 m from any
section's corner. The 0.4 s around every H-pattern change down is left
out (the blip).

**Metrics:** `pedal.overlap_entry` and `pedal.overlap_exit` (share of the
phase's time) and `pedal.drag` (seconds per km on straights).
`pedal.overlap` goes.

**Gates:**

- On loose surfaces, entry overlap is technique, and so is entry overlap
  on tarmac in a turbo or FWD car. It earns a description or praise once
  per car and surface: "You left-foot brake into 7 in 10 corners on
  gravel: that is how a Rally2 is turned."
- A tip needs one of two things, on tarmac or circuit only:
  - **Exit overlap:** at least 3 corners in the run with overlap_exit
    above 0.3 s whose `loss_exit` against the reference exceeds 0.1 s.
    The tip names those corners.
  - **Drag:** more than 0.5 s per km (**calibrate**).
- The "last run lost 0.5 s" gate goes.
- On the replay, exit overlap is 2-7 % of all overlap, so the tip will be
  rare, and correct when it fires.

#### R5. Coasting by phase

**Classifier.** `coast_entry` and `coast_exit` per corner. Coasting on a
straight more than 100 m from any corner counts as `coast_straight`.

**Gates and coaching:**

- **Entry coasting:**
  - on loose surfaces it is the rotation, and is never a fault;
  - on tarmac it is dead time between brake and throttle, coached per
    corner against the reference when that corner's `loss_entry`
    exceeds 0.1 s.
- **Exit coasting and throttle-on time (`throttle_t`):** coached on every
  surface when `throttle_t` is at least 0.3 s later than the reference's
  in the same corner and `loss_exit` exceeds 0.1 s. Two or more such
  corners are needed in the run.
- **Straight coasting:** described only, until crests and jumps can be
  told apart.

**Metrics:** `pedal.coast_entry`, `pedal.coast_exit` and
`pedal.coast_straight` (s/km). `pedal.coast` goes, and so do the
"minimum over earlier runs" reference and the sentence "stay on one
pedal or the other".

#### R6. Corner loss named by place, phase and cause

**Classifier.** Per section: `loss_entry` and `loss_exit` against the
reference, plus causes:

- `brake_d` against the reference: braked earlier or later;
- `min_speed` against the reference;
- `throttle_t` against the reference;
- `exit_speed` against the reference.

**Pattern names** (**calibrate**: 10 m, 3 km/h, 0.2 s):

- **over-slowing:** braked earlier, lower minimum, slower exit;
- **overdriven entry:** braked later or entered faster, slower exit;
- **late throttle:** same minimum, `throttle_t` later, slower exit;
- **the right gravel entry:** braked earlier, same minimum, faster exit.
  This one is praise on loose surfaces.

**Metric:** `corner.loss` is kept: the sum of the three worst sections,
now non-overlapping, offs left out, same-car reference.

**Gates:**

- stage known;
- a reference exists;
- the run is not `learning`;
- sections within ±100 m of an off are skipped and replaced by one line:
  "off at 2.1 km".

**Tip** (the costliest three sections):

> "Afon Bidno, 4.4 s behind your best here in the Fabia (227.6 s). Most
> of it in two places. The square left at 1.8 km, 0.6 s: you braked 25 m
> earlier and were 7 km/h slower at the slowest point for the same exit.
> Brake later and trust the note. The hairpin right at 3.2 km, 0.4 s: the
> throttle came 0.4 s later than in your best run. Throttle as soon as the
> nose points out."

**Praise:**

- a section where the run set the section best: "Your best yet through
  the hairpin at 0.9 km";
- the "possible" time against the best run, as motivation, once per
  stage and car.

#### R7. Consistency

The split SD is worked out:

- on `stage_rows` (cut at `course`);
- over the same car's `clean` finished runs only;
- with tenths of `course`, not of the trace's last distance.

The praise sentence names the runs it compares.

#### R8. Exit gear (tuning `_long_gears`)

**Classifier.**

- The gear and revs are read at throttle-on. The revs 1 s later are kept
  only if the gear is unchanged.
- A "low" exit is a **bog** only when a_long over that second is under
  80 % of the median of this car's in-band exits on the same surface,
  ±5 m/s of the same speed (**calibrate**).

**Metric:** `exit.low` now counts bog exits only; the name stays and the
meaning narrows.

**Gates.**

- On loose surfaces, and on FWD cars on any surface, the note needs 70 %
  bog exits over 15 exits (**calibrate**).
- Otherwise it needs 50 % over 10.
- "Use n−1 there" is never said for 1st (wheelspin; ACR gives no slip to
  check).

#### R9. Balance and counter-steer (tuning)

**Balance:**

- The balance gradient is pooled per surface; this fixes the pooling
  across surfaces at `tuning.py` line 153.
- The absolute "the car understeers" note goes. Only `balance.change`
  between tunes on the same surface remains, as the docstring of
  `balance_gradient` always said.

**Counter-steer:** the tuning rule reads only corners under 90° of
heading change on tarmac, so hairpin technique (0.38) no longer counts
as a balance problem.

#### R10. Gearbox events and the method comparison

- **H-pattern skip:** a change down is flagged `skip` only with the brake
  under 0.1. With the brake on, it is a left-foot-braked block change
  down, which is correct.
- **Method comparison tip:** compares the same car, surface and gear,
  with 10 or more changes each, against the R1 band rather than the mean
  error.
- **Clutch use on a sequential and rev-match quality:** later (§7.5);
  the captures do not hold the rig.

#### R11. Discipline, drivetrain and surface inputs

- **Discipline:** a known ACR stage key becomes `rally-stage` with `game`
  confidence. The two Livigno keys become `circuit`, through a new
  `discipline` field in their `acr.json` entries. Surface comes before
  discipline: Livigno is snow, so the loose rules apply.
- **Drivetrain:** where the car data is shipped, the shipped drivetrain
  wins over the learnt vote. This fixes the Fabia's stored `fwd`.
- **Mixed stages:** coached as loose (today's `loose()`, the safe side)
  until per-segment surface exists.

### 7.4 Gates at a glance

T: tip. P: praise. D: describe once. —: silent. "vs ref" means only where
the reference run was quicker there.

| Rule | Tarmac rally | Gravel, mixed rally | Snow, ice | Circuit (not snow) | Drift | Unknown discipline |
|---|---|---|---|---|---|---|
| R1 band: cut, two places | T | T | T | T | — | T |
| R1 early | T | 3rd and up, with measured grip; else D | D | T | — | as surface |
| R1 on the lights | P | P | P | P | — | P |
| R2 held-corner | — (D when recurring) | — (D when recurring) | — | D | — | as surface |
| R2 held-straight | T vs ref | T vs ref | T vs ref | T | — | T vs ref |
| R3 launch (bog, cut in 1st, steady) | T/P | T/P | T/P | — | — | T/P |
| R4 entry overlap | D/P (turbo or FWD), else — | D/P | D/P | T only on exit, non-turbo | — | as surface |
| R4 exit overlap, drag | T vs ref | — | — | T vs ref | — | as surface |
| R5 entry coast | T vs ref | — | — | T vs ref | — | as surface |
| R5 exit coast, throttle-on | T vs ref | T vs ref | D | T vs ref | — | T vs ref |
| R6 corner loss and causes | T/P | T/P (early braking may be P) | T/P | T/P | — | T/P |
| Over-rev, stall, double tap (mechanical) | T | T | T | T | T | T |

Car gates in every column:

- **FWD:** entry overlap is technique; the exit-gear note uses the 70 %
  threshold.
- **Turbo:** entry overlap is technique on tarmac.
- **Shipped car data:** the band comes from the lights.
- **A drift profile** silences every family except the mechanical ones
  (§3.3).

### 7.5 Phrasing

Every tip has four parts, in this order:

1. **The observation, with its place:** "The square left at 1.8 km".
2. **The number against the reference:** "you braked 25 m earlier than
   in your best run here, 7 km/h slower at the slowest point".
3. **Why it matters,** in time or drive: "0.6 s, for the same exit".
4. **One action:** "brake at the same board and trust the note".

Praise follows the same pattern: what was done, where, the number, and
why it is right ("That is how a Rally2 is rotated on gravel"). It is
earned: a number or a place, never filler.

**Selection** (`select()`):

- Up to 2 praise lines per view, where today there is 1.
- When 2 or more tips show, at least one praise shows if any exists, so
  that a view does not read as a list of faults (the owner's first
  requirement).
- A new kind, `technique`, holds descriptions of correct technique.
  These are shown once, like notes, and come back only when the share
  changes by 20 % or more.

**Words that go:**

- "Hold it to about <the limiter>";
- "the best from the game's engine data" used as a target;
- "stay on one pedal or the other";
- an absolute "the car understeers";
- "your best run" without naming which one;
- per-km figures in the sentence. Per-km stays in the evidence line and
  is used for ranking only.

**Costs for ranking:** seconds lost against the reference, where a
section or episode is known. The rough constants (`COST_*`) stay only
for rules that have no reference.

Tip ids change with the rules: `shift.band:*`, `limiter.held:*`,
`corner.section:<stage>:<d>`. The old `coach_state` rows simply stop
matching; nothing needs migrating.

### 7.6 Missing aspects: now and later

**Now (step 2), because ACR data supports them:**

| Aspect (derivations id) | What |
|---|---|
| `corner.phases` (new; absorbs `brake.point` and `corner.speeds`) | brake point, minimum and exit speed, throttle-on time against the reference, per section |
| `pedal.throttle_application` (planned → implemented, partial) | `throttle_t` and `relifts` per corner |
| `context.incidents` (new) | offs, stops, restarts, run class |
| `corner.loss` (reworked) | sections, complexes, names, section best, the "possible" time |
| `engine.limiter` (reworked) | episodes and classes, `limiter.held` |
| `launch.quality` (reworked) | `launch.g`, outcome-based bog, game-controlled gate |
| `shift.upshift` (reworked) | the lights band, early and cut shares |

**Later, because the data does not support them yet:**

- **Pace-note context:** speed per grade, braking against the call. Needs
  the note lists ([ClickUp 86e3faftn]).
- **Jumps and crests:** needs the ACR suspension sign and a_vert checked.
- **Lock-ups:** needs believable ACR wheel speeds.
- **Handbrake per corner:** the rig handbrake is in live data only; it
  can be built, but replays cannot test it.
- **Clutch on a sequential and rev-match quality:** rig channels, live
  only.
- **Rotation timing and body slip:** needs the car-frame velocity
  checked on ACR.
- **Steering smoothness:** 10 Hz is coarse.
- **Scandinavian flick and lift-off detection.**
- **Turbo lag:** needs a per-car boost model.
- **Per-segment surface on Elatia and Zeli:** needs a deployed ACR
  surface calibration, or the table's split points.
- **WRCG attitude rules:** need the steer sign verified.

### 7.7 Build plan

Each step follows the global model roles. The diff is written by the
`sonnet-coder` agent from this section, except the tier-A part named
below, which Opus writes. An Opus review comes before each commit.
`python3 -m pytest -q tests` stays green throughout.

**Step 1: classification, gates and rule fixes.**

1. **Schema v3** (`telemetry_store.py`), additive. This part is tier A,
   since it changes a persisted format, so Opus writes it. It adds:
   - `runs.course` and `runs.run_class`;
   - `corners.d0`, `d1`, `complex`, `tightness`, `brake_d`,
     `throttle_t`, `relifts`, `coast_entry`, `coast_exit`,
     `overlap_entry`, `overlap_exit`, `section_t`, `loss_entry`,
     `loss_exit`, `best_t` and `off`;
   - `shifts.d`;
   - the `events` table and its index;
   - `stage_runs(car=, run_class=)`.

   The v2 → v3 migration is `ALTER TABLE ADD COLUMN` plus `CREATE
   TABLE`, in one transaction, after the file is copied aside as v1 → v2
   does. A backfill runs once on the drive-log thread. It goes over runs
   with a stored trace and `run_class IS NULL`, oldest first, in batches,
   and recomputes corners, events and metrics. Older metric rows of
   renamed metrics are deleted for those runs.
2. **`coach_context.py`:**
   - `stage_rows`;
   - `incidents`;
   - `run_class`;
   - `reference_run`;
   - `sections`, which also builds complexes, names and phases;
   - `section_loss`;
   - `limiter_episodes`;
   - `launch_outcome`;
   - `shift_band` and `classify_shift`.

   Unit tests use synthetic traces, one per class boundary.
3. **Wiring.** `drive_log._write_end` and `_write_metrics` call the
   context layer. `run_metrics` and `stage_metrics` get the new and
   renamed metrics; the old definitions go. The data fixes are:
   - the finish cut;
   - the same-car reference;
   - the fastest finished clean run as the reference;
   - non-overlapping sections;
   - incidents excluded from launches, coasting and loss;
   - launch revs in 1st only;
   - the game's limiter within 2 % of a launch figure;
   - the top gear from the shipped gear set;
   - the shipped drivetrain winning over the vote;
   - ACR discipline from the stage table;
   - balance per surface;
   - the H-pattern skip needing the brake off.
4. **Rule gates** in `coach.py`, `shift_learner.advice()` and
   `tuning.py`, as in §7.3 and §7.4. Wrong sentences are removed and
   their replacements are minimal in this step: the band numbers, no
   limiter target. The new metrics go into `derivations.json` in the same
   commit, because `test_every_metric_the_coach_writes_maps_to_an_aspect`
   fails otherwise.
5. **Quick replay** of the 15 captures into a scratch database, as a
   sanity check: no crash, run classes plausible, sections counted.

**Step 2: phrasing, new aspects, docs, replay check.**

1. **Sentences.** The four-part templates of §7.5, place names, named
   references, the `technique` kind, and the praise rules in `select()`.
2. **New tips:** corner loss by place and cause (R6), late throttle (R5),
   exit overlap and drag on tarmac (R4), `held-straight` (R2), launch
   praise and the cut in 1st (R3), band praise and the two-places tip
   (R1), section bests and the "possible" time.
3. **Data and docs:**
   - `derivations.json`: the aspects of §7.6 marked implemented or
     partial, new metrics, `acr.coverage`;
   - `docs/coaching-derivations.md` tables;
   - `docs/telemetry-coaching.md` §9.1 (the metric table), §9.2
     (phrasing, praise, selection) and §10 (the exit-gear and balance
     rows);
   - this document's §4 items marked done.
4. **Replay check.** Replay all 15 captures with the scratch-database
   pattern of the audit (`replay_audit.py`), then run the coach on the
   copy of the live database. Pass criteria:
   - None of the audit's nine wrong tips appears.
   - The Fabia's gravel 3→4 tip targets 6900-7100, or is a once-only
     grip note.
   - The 208's tarmac 1→2 and 2→3 get cut tips.
   - The i20N launches get praise; no Fabia launch is "bogged".
   - Run 7 is class `off`, with "off at 2.1 km", and is not a reference.
   - No coasting tip uses a restart as its reference.
   - `limiter.held` is 0 on every ACR run.
   - At least one praise line per car.

   The result is recorded as a short appendix here. The captures are
   private, so this is a script and not a test.

### 7.8 Thresholds to calibrate

Every value below was set from marth's ACR captures unless it says
otherwise.

| Constant | Start | Evidence |
|---|---|---|
| Limiter episode speed floor | 8 m/s | 28 crawl episodes (8.5 s) under it, all stuck or at the start |
| `shift` class: change up within | 1.0 s | p90 of the touch before a change is 0.2 s, the maximum 0.39 s |
| `held-corner`: next corner within | 100 m / 3 s | gravel corner gaps: median 41 m, 74 % under 100 m; tarmac median 92 m |
| `held-straight`: on the cut for | 1.5 s / 80 m | none in the captures; the catalogue's rule of thumb |
| Complex: corners closer than | 30 m | 1170 corners give 732 sections (801 at 20 m, 585 at 50 m) |
| Incident: speed under / for | 3 m/s / 1 s | 34 mid-run stops in 15 runs, the offs with reverse or from more than 20 m/s |
| Restart / partial split | 50 % of the stage | marth's Afon Bidno restarts end at 220-470 m or 0.9-3.0 km of 5.3 km; one at 4.7 km is a partial |
| R1 cut share / early share / two places | 25 % / 60 % / 30 % + 25 % | Fabia 2→3: 50 % early, 28 % cut; 1→2: 68 % cut |
| R1 sequential / H-pattern margin | 150 / 300 rpm | the catalogue; the lights give the band where shipped |
| Grip lowering: pulls / plausible share | 6 / at most 1.0 | the audit's gravel shares over 1.0, and 4th on tarmac from one stage |
| Launch bog g / t50 over median | 0.3 g / 0.5 s | AWD launches 0.6-0.84 g, 1.6-2.1 s |
| Game-controlled launch SD | 0.05 s | 208: 3.36-3.39 s |
| Exit bog: a_long share / exits (loose or FWD) | 80 % / 70 % of 15 | the audit: "low" 5th-gear exits pulled 0.27 g against 0.20 g in band |
| Tightness bands | 135 / 75 / 40° | the catalogue's counter-steer bins |
| Pattern deltas | 10 m, 3 km/h, 0.2 s | to set from the first reference comparisons |
