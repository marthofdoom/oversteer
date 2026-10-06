# Changelog

## 0.15.2 — 2026-10-06

### Coaching and telemetry pages
- The splits row, its table, the ribbon and the live view's split are the game's
  own sectors (S1, S2, S3 …) wherever a stage has real sector lines (every
  Assetto Corsa Rally stage), timed on the game's clock and coloured LiveSplit's
  way against your PB; the sum of best is the sum of your best sectors. The
  coach's corner sections stay in the Run view's "where the time went" and in
  the top 3. Stages without sector lines (other games, Livigno) keep corner
  sections, labelled as sections.
- Comparison labels say what the comparison is: viewing your PB compares with
  "next best", the Live view names its reference "Run N" unless it is the PB, and
  the stage map's START label sits away from the route.
- Absolute times are written one way everywhere (coach sentences, split and sector tables,
  GTK and web): m:ss.s, 28.7 under a minute; differences stay signed seconds.
- The coach compares with your PB (the quickest run by stage time), not with the run that was quickest after the
  start: a slower run with a long standing start is no longer the reference.

## 0.15.1 — 2026-10-06

### Coaching
- The coach diagnoses each corner from the braking, the speeds and the grip
  together, measured on the same metres of road as your best run, and says one
  thing to do. Over-slowing is now called as such ("Brake less there, or not at
  all, as your best run did: carry 24 km/h more through the slowest point. About
  26 % of the grip was left there."), and so are a slow run-up ("Leave the 4 left
  at 1.7 km 15 km/h faster: you were 15 km/h slower before braking here"), a
  slide, a braking point that was too early, a braking after the slowest point, a
  late throttle, coasting and a gear. A later slowest point with grip to spare is
  not an overshoot.
- Every tip has one thing to do. A corner or a section the numbers cannot give a
  fix for says what it measured and leaves it there, as a note, not a tip. The
  top 3 places say the cause and the fix in one sentence ("the 2 right at 4.1 km:
  about 2.1 s is there, mostly into the bend. Brake 45 m later, where your quickest
  pass did"); a place with nothing to do that the run shows is skipped for the next,
  and a section tip inside a top 3 place is left to it, so the same fix is not said
  twice.
- The old braking advice is gone. "Brake 41 m earlier" came from measuring
  each run's braking back from its own slowest point, so a slowest point that
  moved read as a braking point that moved; the braking point is now where the
  brake went down on the road, and the Brake column of the Run view is measured
  the same way. The top 3 places say "brake N m later", "brake or lift less to
  carry N km/h more to the apex" or "full throttle sooner" only where your own
  braking, speed or throttle there agrees, and a gear shorter than your fastest
  pass is quoted only when it cost time on the exit of that place.
- A spin or a near stop says what was measured (the heading, the speeds in and at
  the slowest point) and one fix the numbers point at: arrive at the speed your
  quickest pass came in at, catch it with opposite lock sooner (where you steered
  against it for little of it), or a shorter handbrake pull (where the handbrake
  was measured). Nothing is said about the line, a flick or the handbrake unless it
  was measured.

### Assetto Corsa Rally
- **The stages' real start, split and finish lines**, read from the game's
  level data, for all 46 stages (`sector_lines_m` in the stage table). The
  sector times (S1..) now add up to the stage time on every clock: the first
  starts at the clock's start and the last ends at the result, with the time
  taken where the car's distance along the road crosses each line (on the game's
  clock, to within a few hundredths of a second). They no longer show the
  approximate mark. The live view's sector delta uses the lines too, placed from
  where the reference run began.
- Three cut stages (Hafren Forest, Zeli Reverse, Aghii Theodori Reverse) had
  their start line placed 51 to 274 m too early; it is now the game's.
- Afon Bidno's finish line is corrected (5294.1 m, was an estimate of 5287.4);
  runs timed at the old line are moved to it.
- A run that did not record where it began is timed to the flying finish from its
  own trace, not from where the stage's runs mostly begin (Steigenbach: 299.435 s,
  not 299.717 s). The first start of this version works every stage's finished
  runs over again on the new lines (a background job, as after an update), and
  tries once more the runs that could not be re-timed before; a stage is worked
  over again whenever its start line, finish or road length changes.

## 0.15.0 — 2026-10-06

### Read this first
- **The telemetry database moves to schema v3** the first time this version
  starts. The migration is automatic and keeps a backup next to the file
  (`telemetry.db.v2.bak`). Afterwards a one-time background job works through
  your old runs: it re-analyses them, re-times them, re-classes them (clean,
  learning, off, partial) and rebuilds the potentials. It runs a few runs per
  tick, off the hot path, so the Telemetry tab fills in over a few minutes.
- **Assetto Corsa Rally: the shared-memory bridge is now v4** and needs this
  version of Oversteer. Relaunch ACR through `oversteer-run` to get it; an
  older bridge keeps working, without the game's own stage clock.
- **Pedal response needs new-lg4ff 0.8.0 or later** and the udev rule changed:
  re-run the permission prompt (or reinstall the rules) so the new attributes
  are writable.

### Coaching
- The coach is technique-aware. Tips come by place and cause ("the left-left
  at 2.1 km: ...") instead of by metric, name the reference (your best clean
  run) and say where the gain is. Good driving is praised: a section quicker
  than your next best, a clean launch, a well-timed exit. Offs and resets are
  stated as facts; there is no labelling of the driver, and a technique line
  is shown neutral.
- Runs are classed (clean, learning, off, partial) before they are judged. Only
  clean runs are a reference; a run that did not finish keeps its slow tail as
  an off and leaves the last section out; a stage's tips appear as soon as it
  is finished, not at the start of the next one.
- Fewer false alarms: a launch is a bog only against the car's own launches on
  a loose surface, a corner's radius needs more than 5 m/s, a drop of speed
  across one row is no hit in a game whose speed channel is not checked, and
  praise or advice is given only against a reference section without an off.
- Potential time: for each stage and car, a quasi-steady-state simulation
  gives three layers: you (the sum of your best sections), grip (what the
  tyres allow on this road) and car (what the engine and mass allow). The top
  three sections to work on are shown after every run, quoting the splits'
  sections (name, bounds, time), and the splits sheet has an Avail column.
  The car's mass is the shipped one plus crew and fuel; tight corners are read
  through a short window, so a 5.5 m hairpin is no longer read as 8.8 m.
- Splits and sum of best: every grid section is timed by distance for any run
  that covered it, in the LiveSplit table and colours (gold best ever, green
  or red against the PB by cumulative time, dark or light by the section).
  Assetto Corsa Rally's own sectors are the splits S1 to Sn; where a time is
  estimated it is marked with a ≈. A run that began mid-stage is left off the
  reference.

### Telemetry pages
- Coaching | Telemetry sub-tabs on the web page (phone and desktop) and in GTK.
- **Live**: the delta to your PB (big ±0.00 and a ±2 s bar), the splits row,
  pedal and steering strips, a g-g plot and a minimap.
- **Run**: traces against the PB, the previous run or any run; the stage map
  (START and FINISH labels flip at the map's edge); where the time went, each
  section with the coach's advice. A run picker and compare chips.
- New JSON endpoints behind them: `/api/v1/live?since=` (the live run's ring
  and its delta) and `/api/v1/runs`, `/api/v1/runs/<id>`,
  `/api/v1/runs/<id>/trace`.
- Maps are drawn from one plan per game: ACR was drawn mirrored and WRC
  Generations rotated. **Corner left/right from positions changed** for games
  with no yaw-rate channel (DiRT, EA SPORTS WRC, OutGauge): they now use that
  same plan view, so old corner records of these games may read mirrored
  against new ones.
- The shift points table shows the coach's lights band (6900-7100 rpm
  (lights)) instead of the limiter the engine data ends at.

### Assetto Corsa Rally
- **Flying finish**: Oversteer learns each stage's finish line from the game's
  own clock (where it stopped, per run), so result times stop at the line
  instead of at the stop control. At the first start, old runs are re-timed;
  runs that cannot be are classed partial and stay out of best, reference and
  sum of best. A run that began mid-stage is not recorded finished.
- **Results on the game's own stage clock** (bridge v4): the time of a run is
  the game's, not an estimate from packets; a clock that restarts on a looped
  stage is a lap, not a restart.
- The car's position is read again (map, elevation), and the maps are no
  longer mirrored.
- Rev lights light to the car's limiter from the game's files, as ACR sends no
  max rpm.
- Tyre radius is learnt so slip is filled in; a stage the track name does not
  give is no longer guessed.

### Other games
- Every channel the games send is decoded: ACR per-tyre loads, forces, slip,
  radius, ride height, grip and angular velocity; Forza torque, tyre
  temperatures and wear, fuel and laps; EA SPORTS WRC and OutGauge extras.
- WRC Generations: forward direction (was backwards), steering sign, suspension
  travel in metres, the two fake g channels dropped, brake temperatures read.
- Assetto Corsa and ACC: a lap of a circuit is no finish and its wrapping lap
  distance is not the run's distance.
- DiRT: a frozen packet counts as a stop past the finish only once progress is
  1 or the stage clock ran past the result, so a pause is no finish.
- EA SPORTS WRC: progress is clamped to 0..1.
- Codemasters games: the rpm unit is the one that makes the maximum roundest,
  and acceleration the format lacks is unknown, not 0.
- A bridge newer than the decoder is logged once.

### Wheel and devices
- Hotkeys: hold a wheel button (or the keyboard shortcut) to repeat a step:
  the first repeat after 0.4 s, then every 0.12 s, for the strength, spring,
  damper and similar steps, the shift point and the rotation range. It stops
  when you let go, at the control's limit, when you start setting a button, or
  when you switch device. Toggles and profile switches fire once.
- Pedal response: a "Response..." button under each pedal's Invert box in the
  Controls tab sets the deadzone at the released end (0 to 45 %), where the
  pedal reaches full (55 to 100 %) and a curve (50 linear, lower softer at the
  start of the travel, higher sharper), in pedal travel whichever way Invert
  has the axis. It applies live, is kept in the profile and is ignored by the
  driver while the pedals are combined (the buttons grey out then). Presets:
  Linear, and Spring brake for stock spring-and-rubber brakes. Needs new-lg4ff
  0.8.0 or later with `pedal_response`.
- Devices tab: a "Steam launch options for wheels under Proton" row with a
  Copy button: `SDL_JOYSTICK_HIDAPI=0 %command%` keeps SDL (in Proton) from
  driving a Logitech wheel itself, bypassing the kernel driver and Oversteer's
  settings. The line under it reminds you to set the game's Steam Input to
  Disabled and shows the combined form with the shared-memory bridge.
- Applying the combined device, the handbrake direction or starting the proxy
  service while a Wine/Proton game runs now asks first: re-creating the virtual
  wheel under Assetto Corsa Rally crashes the game.
- A library of known device profiles for the proxy (identity and capability
  set per device, with a validating loader), starting with the G29's axis
  ranges and effects as read from a real rig.

### Under the hood
- The proxy tests skip themselves while a Wine game runs, for the same reason.
- A car's shipped drivetrain replaces a learnt one at the next start; a stage
  the shipped table knows takes its discipline from it.
- A run whose backfill fails keeps its old rows and class and is tried again at
  the next start; the trace of each car's best finished run on a stage
  survives the cap.
- The drive log's change stamp moves only after the run is committed, so a
  reader that sees it move finds the run in the file.
- Design and research notes: the technique-aware coach, the telemetry UI,
  anti-cheat policy for proxy identities and telemetry extrapolation under
  `docs/` and `research/`.

## 0.14.2 — 2026-09-28

### Added
- Assetto Corsa Rally: Oversteer ships each of the game's 18 cars' engine
  and gearing, derived from the game's own files (torque curve, limiter,
  gear sets, final drive, tyre radius, mass, drivetrain, the game's own
  shift-light rpms). The best upshift per gear is then exact from the
  first drive: for the Skoda Fabia RS Rally2, the limiter in every gear.
  The learnt gear ratios pick the gear set in use and stay as a check:
  where they are more than 4 % off every gear set the game gives the car
  (and wheelspin does not explain it), the game's data is set aside and
  the best upshifts are learnt, which the Telemetry tab says.
- Best upshifts per surface: a gear whose measured drive at full throttle
  (wheelspin included, over several pulls) stays well below what the
  engine gives in it (1st on gravel, say) is grip-limited there, and its
  best upshift is lowered to where the next gear reaches the same grip;
  from there up to the engine's best, changing up costs nothing, and all
  of that range is on target. The rev lights in Auto use the best for the
  surface of the stage being driven (from the stage tables), the
  Telemetry tab's shift table says which surface it shows and marks each
  best as from the game's data or lowered for grip, with its range.

### Changed
- Coaching measures each change up against the best for the run's
  surface and that gear, and says whether that best comes from the game's
  engine data or was learnt. Early changes on a loose surface (gravel,
  snow, ice, a mixed stage) are coached only once that gear's grip there
  is measured over a few full-throttle pulls: against the game's engine
  data or the learnt best where it does not limit, against the best
  lowered for grip where it does. Until then the coach says once that it
  is waiting for that.
- Some learnt shift points are learnt again after the update: a car's
  pooled power curve learnt on hills without the slope taken out is
  dropped (the per-gear curves stay), and the drive samples that measure
  grip per surface are measured again.

### Fixed
- The learnt best upshift of a car driven where the game sends no slope
  (the Fabia's 5100–5200 rpm, 800 rpm below where it should be): power is
  no longer pooled over the gears unless the slope is taken out, bands
  need more samples while hills are left in, and the slope now also comes
  from how the car's position climbs (Assetto Corsa Rally sends it now).
- Gear ratios are no longer learnt while coasting or braking.
- The rev lights in Auto no longer freeze for a moment every 2 s: working
  out a best upshift took up to a second with many samples learnt; it is
  now kept until what it depends on changes and worked out off the
  telemetry thread.
- A car whose slope comes from its position (Assetto Corsa and ACC over
  the bridge) no longer flickers between two ways of learning its power
  below about 24 km/h.

## 0.14.1 — 2026-09-26

### Fixed
- Assetto Corsa Rally: the distance along the stage is read (it was
  always 0), and steering, clutch, acceleration, speed and yaw are
  decoded with signs confirmed on real stages, so counter-steer and the
  car's balance are measured. The first packets of a stage, before the
  game names the car, no longer start a session for an unknown car.
- When a game sends to the other telemetry port (5300 or 5310), the
  Telemetry tab's live line says so and how to fix it; it was only
  shown under Settings, and noticed rarely.

### Added
- The web page accepts this computer's Tailscale name, so
  `tailscale serve` can give it real HTTPS, which a phone's browser
  needs to keep the screen on.

## 0.14.0 — 2026-09-25

### Added
- Rev lights: **Learn the limiter at each launch**. A rally stage starts
  with clutch in, handbrake up and the throttle floored, which holds the
  engine on its limiter; Oversteer learns that RPM at every standing
  launch and the % shift point applies to it, so each car on each stage
  gets its own shift light even when the game reports no maximum or the
  wrong one. It is raised if the car is later held on a higher limiter
  flat out (a launch control capping the revs at the line), never by a
  moment past it, and forgotten when the telemetry stops between stages.
  With no handbrake fitted, clutch in and throttle floored is the launch.
- Rev lights: **Shift lights at the learnt best upshift for each gear**:
  once Oversteer knows the car's power curve and gearing, the lights
  complete where the next gear starts pulling harder; a gear that pulls
  to the limiter still flashes as it gets there.
- Telemetry tab: the shift table has a column per way of changing gear
  (H-pattern, sequential, paddles) once you have used it. How each change
  was made comes from the control you pressed: a shifter gear, the
  sequential plate or a paddle.
- EA SPORTS WRC telemetry: the game's default structure, or Oversteer's own
  (car and stage ids, session start/end/pause), set up with two Copy
  buttons under the Telemetry tab's Settings.
- `scripts/telemetry-capture.py --write` records raw telemetry to a file;
  `scripts/telemetry-replay.py` plays it back through the shift learner,
  re-sends it over UDP, or cuts a piece out of it.
- Telemetry history: every drive is kept as runs (a stage attempt, a lap
  session, a stretch of free driving) with their corners, a 10 Hz trace
  and every change of gear up or down, flagged when a gate was missed,
  a gear skipped, the engine over-revved or a paddle double-tapped. Each
  run is judged a discipline (rally stage, hillclimb, circuit,
  rallycross, time attack, free roam) with the evidence that decided it,
  or left unknown when there is none. Each gearing a car was driven with
  is kept as a tune.
- Coaching from that history in the Telemetry tab: what the last session
  was and why Oversteer thinks so, the habit to work on first, up to
  three tips with their numbers (changing up early or late per gear and
  way of changing, the limiter, missed gates and double taps, bogged
  launches, both pedals at once on tarmac, coasting and corners against
  your best run of a stage) and praise when a habit improves. A tip
  shown on two occasions (hours apart) goes quiet until it gets worse. Changing up early waits
  until the surface is known, since short-shifting on gravel can be
  right.
- Tuning advice from the runs on the car's current setup: a final drive
  too short or too long for a stage, a gear too long out of corners, and
  oversteer or understeer fitted to how you drive, always "if the setup
  allows" and with the driving alternative first.
- "Label last session…" in the Telemetry tab: say what a session was
  (discipline, surface, wet, shifter), for Oversteer to learn surfaces
  from.
- A read-only web page for a phone or another computer (off by default,
  TCP 5301, under the Telemetry tab's Settings): the live gear and revs
  against the shift point, the shift tables, coaching, setup advice and
  recent sessions with their evidence. Nothing can be changed from it;
  anyone on the same network can read it, so it can be limited to this
  computer. Clients with a public address are refused.
- "Learn from game telemetry": learn shift points and keep the history
  with the rev lights off, or with a wheel that has none.
- "Record raw telemetry" under the Telemetry tab's Settings (off by
  default): what the game sends is kept as it arrived, a file per drive,
  in Oversteer's data folder, up to a size you choose (1 GB by default;
  the oldest go first). Labelling a session also labels its captures and
  keeps them.
- Telemetry tab: the best upshift of each gear shows the range it is
  known to.
- Stage tables for WRC Generations (all 21 rallies, 165 stages), Assetto
  Corsa Rally (46 stages to update 0.6) and DiRT Rally 2.0 (every rally
  stage, rallycross track and DirtFish): a run on a known stage has its
  name, rally and surface from the first drive, without labelling. The
  WRC Generations and Assetto Corsa Rally tables come from the games'
  own data (exact lengths, names, surface shares); WRC Generations sends
  each stage's exact length, which identifies it, stage and reverse
  alike. DiRT Rally 2.0's data is encrypted: its table is the community
  one, checked against the game's stage counts.
- The Assetto Corsa family bridge (`oversteer-run`) sends more: the
  stage's length and the progress along it, wheel speeds, suspension
  travel and the game's current rev limit. `OVERSTEER_BRIDGE_VERBOSE=1`
  in a game's launch options logs the rest once a second, for checking a
  game's layout.
- Oversteer reopens the profile last used when it starts (unless
  `--profile` or a setting on the command line says otherwise): cars
  and history are kept per profile, and starting on none showed an
  empty Telemetry tab.

### Changed
- Profiles saved before this version get both new rev light switches
  (the launch limiter, the learnt upshifts) turned on, as new profiles
  do: the lights then follow the car rather than the fixed percentage.
  Untick them under the Telemetry tab's Settings for the old behaviour.
  A profile that never stored a shift point keeps 97 %; new ones start
  at 95 %.
- The shift learner compares each gear's own power curve where it knows
  both, takes the slope out of the acceleration where the game says
  which way is up, ignores turbo lag and wheelspin, and gives each best
  change up a range. Its database moves to a new layout (a copy of the
  old file is kept as `telemetry.db.v1.bak`), and writing it no longer
  happens on the telemetry thread.
- A driving session now ends after two minutes without telemetry, not
  two seconds, so pausing mid-stage no longer splits it. Forgetting a car
  starts its learning over but keeps its history.
- The telemetry port is now **UDP 5310** for new profiles and
  `oversteer-run`. Forza Horizon 6 binds its own socket in 5200–5300 and
  asks Data Out to stay clear of that range. Profiles that saved 5300 keep
  it. When nothing arrives, Oversteer listens on the other port for a
  second now and then and says so if a game is sending there.
- The listener takes telemetry from one source at a time (the first one
  heard, until it goes quiet), so a bridge left running next to a game
  no longer mixes two cars.
- The Telemetry tab is laid out like the other tabs, in two views.
  **Car and coaching**: the car, a one-line live status with a coloured
  dot, the shift points in a framed table (the best upshift in bold),
  coaching one row per tip with a Focus / Tip / Better tag, the last
  session with "Why Oversteer thinks so" and "Label…", recent sessions
  one row each, the setup. **Settings**: framed lists for receiving
  telemetry, the rev lights (no longer one crowded row), the web page
  and recording, each row a title with a short explanation or its status
  under it.
- The web page is a dark live dashboard for a phone or laptop next to
  the rig: shift lights, the gear huge, speed, rpm, where to change up in
  this gear and the stage by name with its progress, updated four times
  a second; then coaching, the shift points with the current gear
  highlighted, the setup and recent sessions. It fits portrait and
  landscape phones and puts the live panel beside the rest on a laptop.
  It keeps the screen on: with the browser's wake lock where allowed
  (HTTPS or the same computer), otherwise after a first tap with a tiny
  muted video made in the page. A chip at the top says whether the screen
  is being kept on.

### Fixed
- Telemetry packets arriving in a burst, or a reset after a crash, were
  taken for a teleport and split one stage into several runs.
- WRC Generations sends its stage's length where DiRT sends progress: it
  was read as progress, so no stage finished.
- DiRT Rally 2.0 (and DiRT Rally, DiRT 4): engine rpm was read 4.7 % too
  high (the game sends rad/s, not rpm / 10), and with it the shift light
  and everything learnt. Cars already learnt are corrected once when the
  telemetry database opens (a backup is kept as `telemetry.db.v0.bak`).
- Reverse in DiRT Rally and WRC Generations, and neutral in Forza, were
  read as "unknown gear"; H-pattern changes through neutral are now seen
  in Forza Horizon.
- Gear ratios are learnt only at part throttle, where the tyres barely
  slip: a first run spinning up gravel no longer leaves a car with wrong
  ratios and a false "re-tuned" note.

## 0.13.1 — 2026-09-24

### Fixed
- Plasma opened System Settings on the Shortcuts page every time Oversteer
  started. Its portal does that whenever an app declares shortcuts, so on
  Plasma a start now only reads the keys already assigned (they still
  work), and declaring waits for "Set keyboard keys…". Other desktops keep
  their shortcuts per session and still declare at start, silently once
  they know them.

## 0.13.0 — 2026-09-23

### Added
- Hotkeys tab: change settings while you drive, from a wheel button or a
  keyboard key. Shift point up/down (1 % or 100 rpm a press), rev LEDs,
  force feedback on/off, overall strength, centering spring, spring,
  damper, friction, rumble, the FFB switches, rotation range (±10°/±90°),
  sensitivity and next/previous profile. Wheel buttons are saved with the
  profile; keyboard keys go through the desktop's shortcut portal (Plasma,
  GNOME), so they work over a full-screen game and nothing reads the
  keyboard. The rev LEDs show the new level for a moment. Next/previous
  profile bindings are app-wide, so a profile with other buttons can't
  strand you.

## 0.12.4 — 2026-09-22

### Changed
- The rotation range slider shows plain ticks instead of repeating the
  degrees the preset buttons underneath already give, and a preset the
  wheel can't reach is hidden rather than silently clamping.

## 0.12.3 — 2026-09-22

### Changed
- The Controls tab shows each axis as a game receives it rather than the
  pedal's position: with Invert off a Logitech pedal sits full when
  released, which is exactly what a game reads, and ticking Invert flips
  the bar where you can see it. Hiding that behind a flipped display is
  what made a wrongly-read pedal invisible in the first place.

## 0.12.2 — 2026-09-22

### Fixed
- A profile saved before the driver could invert pedals no longer greys
  the Invert boxes out.
- Axis readings are taken from the kernel rather than the snapshot made
  when the device was opened, which a bar redraw would otherwise show.
- An error while handling one input event no longer stops Oversteer
  reading the wheel for the rest of the session.

### Changed
- **Pedals are straightened out instead of being hidden.** Logitech pedals
  report their released position at the far end of the axis; Oversteer used
  to flip that for its own display only, so the Controls tab looked right
  while games still received a pedal that reads "fully pressed" when
  released (Assetto Corsa Rally shows this). On a wheel whose driver can
  invert pedals, Oversteer now inverts the ones that rest at the far end,
  once per device per session and only while the driver's setting is
  untouched, so games get 0 released / full pressed. A profile that
  carries the setting always wins, which is how to keep the raw
  direction, as does `--invert-pedals`.
- The **Invert** boxes moved to the Controls tab, one under each pedal,
  ticked when that pedal is inverted. Each box drives the axis the driver
  associates with that pedal, which is not always the one Oversteer shows
  it as (a G29 in DFP or DFGT emulation, a G920, the T150/TMX/T248 and the
  G PRO all report their pedals on other axes), and the bars are drawn
  from where each axis really rests instead of assuming the Logitech
  convention.
- The handbrake has an **Invert** box too, overriding the direction the
  combined device gives it (the proxy sets this automatically from where
  the lever rests; the daemon now reports what it decided so the box shows
  the truth). Changing it reinstalls the combined device, so it asks for
  the administrator password.
- Toggling any Invert box redraws the bars from where the axes are right
  then, instead of waiting for the next movement, and events arriving
  while the driver catches up are already read the new way.

## 0.12.1 — 2026-09-22

### Fixed
- The Controls tab went dead (no steering, no buttons) after the combined
  device was rebuilt: restarting a proxy destroys its virtual device and
  creates a new one under the same name, and Oversteer kept reading the
  deleted node. It now notices the node it holds is gone or replaced and
  re-opens, and a read error drops the device so the next read recovers.
- Combined device around a Logitech wheel: a shifter's buttons beyond its
  gear positions were dropped, so the T500 RS / TH8A sequential plate did
  nothing. Everything the shifter reports is carried now, and for the
  T500 RS the generated spec records which code each sequential position
  got (down -> BTN_TRIGGER_HAPPY11, up -> BTN_TRIGGER_HAPPY12, buttons 26
  and 27 on the Controls tab). Wheels without gear codes already carried
  every button.

### Added
- Controls tab: a **Handbrake** column next to the pedals, shown when the
  device has a handbrake. Which axis that is comes from the proxy's own
  spec when a combined device presents the wheel, so it stays right
  whatever else is folded in; a natively connected handbrake is probed
  instead, skipping axes the wheel uses for something else (the T150, TMX
  and T248 report their clutch on ABS_THROTTLE) and correcting one that
  rests at the top of its travel.
- `scripts/probe-device.py`: name the raw events of any input device,
  including one hidden or grabbed by the proxy (`--stop-proxy`), for
  mapping new controls.

## 0.12.0 — 2026-09-21

### Added
- **Shared-memory telemetry under Proton** (Assetto Corsa, Assetto Corsa
  Competizione, Assetto Corsa Rally): `oversteer-shm-bridge.exe`, a small
  Windows helper that runs inside the game's Proton prefix, reads the
  `Local\acpmf_*` shared memory and sends RPM / redline / gear / speed to
  the rev lights over UDP (24-byte `OVST` datagram). `oversteer-run
  %command%` in the game's Steam launch options starts it alongside the
  game through the same Steam Linux Runtime and Proton; the Tools tab
  shows the exact launch options string with a Copy button. Source in
  `data/telemetry/`, rebuild with `scripts/build-shm-bridge.sh`.

- Rev lights: a per-profile **shift point**, either as % of the game's
  maximum RPM (default 97) or as an RPM figure (dropdown; the value is
  converted when the game has reported its max RPM). All five LEDs are on
  at the shift point and flash above it; the first comes on at 72 % of it.
  Rally and turbo cars shift well below the limiter, so set it per profile
  to where you change up.

- Proxy status file: `ff_effect_types` (what the game has uploaded, by
  type) and `ff_playing` (types currently playing) per proxy, refreshed
  when the set changes, so a game's use of spring / damper / friction /
  constant / periodic effects can be read off `/run/oversteer/proxies.json`.

### Fixed (pre-release review)
- OutGauge: the learnt redline sagged per packet down to the current RPM,
  so a steady cruise lit the whole bar and flashed; it now decays per
  second and never below what keeps the current RPM at "all on".
- Changing the shift point updates the running listener instead of
  restarting it (no LED blink, learnt redline kept); profiles from before
  the shift point load with the old 97 %; values are clamped to their
  unit's range on load and on unit conversion.
- Proxy status: effect bookkeeping is lock-protected and a change inside
  the half-second refresh window is written when it ends instead of
  being dropped.
- WRC Generations telemetry was ignored: it sends the Codemasters
  extradata=3 layout natively but in a longer packet than DiRT Rally 2.0's
  264 bytes. Any 4-byte-aligned length from 256 to 512 bytes is accepted
  now, and unknown packet sizes are logged once.

## 0.11.0 — 2026-09-21

### Added
- **Rev lights from game telemetry**: the wheel's rev LEDs fill with engine
  RPM and flash at the limiter, fed by the game's UDP telemetry (Forza
  Horizon / Motorsport "Data Out", BeamNG / LFS OutGauge, DiRT Rally 2.0 /
  DiRT 4 extradata 3).
  Tools tab: switch, UDP port, Test LEDs. Takes the LEDs away from the FFB
  meter while active; LEDs go out 2 s after telemetry stops.
- "Try" buttons next to every force feedback control: play that effect on
  the wheel for two seconds to feel what it does.
- Rotation range presets (270/360/540/720/900) under the range slider.
- Driver status line under the device selector: driver name and version,
  a hint when the in-kernel hid-logitech is loaded, and whether the
  combined device is active.
- "Reset to defaults" on the Force Feedback tab.
- Devices tab: the status line refreshes itself, explains which service is
  involved when it isn't running (oversteer-proxy.service, with the
  journalctl hint) and offers a Start service button.

### Changed
- The main window is resizable; sliders grow with it.
- Force feedback tooltips explain what each force does to the wheel and
  what games use it for.
- Driver status reads the version of the module actually bound to the
  wheel, and recognises new-lg4ff before its udev permissions are applied.
- Flatpak: the sandbox shares the network namespace so telemetry can reach
  the rev lights.

### Fixed (from the pre-release review)
- Loading a profile now starts or stops the rev lights as saved in it.
- The Devices tab's periodic refresh no longer discards the equipment ticks
  or interrupts an install in progress, and the Update button no longer
  stacks refresh timers.
- Start service runs pkexec off the GTK thread; cancelling it is not an error.
- Turning the FFB meter on turns the rev lights off (and vice versa) in both
  the UI and the profile.
- Try buttons are greyed while force feedback is off and report an effect
  that could not be played.

## 0.10.4 — 2026-09-21

### Fixed
- 'Combine into one device' installed nothing: the installer skipped the
  very candidate directory it was given (0.10.3 regression).

## 0.10.3 — 2026-09-21

### Fixed
- Flatpak: 'Combine into one device' installed nothing because the candidate
  spec was written to the sandbox's private /tmp, which the host-side
  installer can't see. It now lives under the config directory.

## 0.10.2 — 2026-09-21

### Fixed
- Flatpak: the Devices tab reads the installed proxy state through
  `host-etc` and a status heartbeat (Flatpak reserves /etc and hides the
  host's /proc).

## 0.10.1 — 2026-09-21

### Changed
- The proxy service runs from a root-owned copy of the daemon in
  `/usr/local/lib/oversteer-proxy` installed by the Devices tab, so it works
  the same from a source tree, a system install or the Flatpak (the host
  needs `python3-evdev`, `python3-pyudev` and polkit). `--unsafe-dev-tree`
  is gone.

### Added
- Flatpak: `flatpak/io.github.berarma.Oversteer.yaml` (derived from the
  Flathub manifest) and `scripts/build-flatpak.sh`; a single-file bundle is
  attached to each release.

## 0.10.0 — 2026-09-20

### Added
- **Devices tab**: every racing device on the computer, classified (wheel,
  shifter, pedals, handbrake, gamepad), and a single *Combine into one
  device* switch. Combining builds one virtual copy of the wheel carrying
  the ticked devices (shifter gears on the G29's own gear buttons,
  handbrake on a spare axis, force feedback passed through), hides the
  real devices from games and runs it as the `oversteer-proxy` system
  service. Fixes games that only talk to one device (Forza Horizon's
  "Device 1" force feedback). The combined device keeps the wheel's
  identity by default; a checkbox presents it as a generic "Oversteer
  Combined Wheel" instead — needed for Forza Horizon 6 under Proton, which
  drops force feedback for a recognised wheel in a custom profile but keeps
  it for a generic device (verified: full FFB, T500 RS shifter on the G29's
  gear buttons, analog handbrake; Forza won't navigate menus from an
  unknown device, so keep a gamepad or keyboard for that). Start the game
  after the device exists.
- Proxy devices under the hood: `--proxy-list`, `--proxy-run`,
  `--proxy-daemon`, `--proxy-install`, `--proxy-remove`; JSON specs in
  `~/.config/oversteer/proxies/`.
- Rumble vibration slider (`--rumble-level`) for new-lg4ff's rumble emulation; udev rule grants `rumble_level`.

### Security
- The proxy service runs as root from a root-owned copy of the daemon in
  /usr/local/lib/oversteer-proxy — never from files the user can edit,
  whether Oversteer runs from a source tree, a system install or a Flatpak.
  The unit is sandboxed (NoNewPrivileges, ProtectSystem=strict, only input
  devices and /dev/uinput allowed), files it writes are root-owned 0644,
  udev rule text is sanitised. Inside Flatpak the install goes through
  flatpak-spawn to the host.

## 0.9.0 — 2026-09-20

Logitech G29 Windows-parity features. Requires new-lg4ff 0.6.0 for the new
controls; they are greyed out on other drivers.

### Added
- Steering sensitivity slider (0–100, 50 = linear) and `--sensitivity`.
- Invert pedals: Clutch / Accelerator / Brakes toggles and `--invert-pedals`.
- Force feedback on/off switch (`--ffb` / `--no-ffb`); strength settings are
  kept while off.
- "Keep centering spring in games" (`--autocenter-persistent`).
- "Let games adjust FFB strength" (`--app-gain` / `--no-app-gain`).
- "True inertia effects" (`--inertia-mode 0|1`).
- Global feedback gain up to 150 % with the safe range marked.
- udev rule grants access to the new new-lg4ff attributes.
- `docs/g29-windows-parity.md`: the parity matrix that defines 1.0.0.
