# Changelog

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
