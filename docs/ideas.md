# Ideas (noted, not started)

Ideas from marth and from FFB work, kept here so they survive sessions. Nothing here is committed to or in progress
unless it says so.

## STAR WARS: Galactic Racer FFB (2026-11-04, marth)
**Parked** (marth, 2026-11-04): wait until the game has better device support. No Steam controller modifications.
Hardware (marth): probably needs an airplane-style thrust control; pedals aren't ideal. A thrust lever also gives the speed estimate a better input.
Not ground vehicles, so no tyres: turning input, speed and an approximation of how the vehicles steer are enough.
- The game has no wheel support and no known telemetry; see docs/star-wars-galactic-racer.md (controller setup, untested).
- Speed: estimate from the pedals first. Reading game memory only after checking for online play and anti-cheat.
  Reading the on-screen speedometer as a last resort.
- Order (marth, 2026-11-04: SpaceMonkey probably wanted after all): 1. UEVR + SpaceMonkey as below, no code on our side, to prove it runs
  under Proton and the data is good enough for forces. 2. Our own small UEVR plugin sending UDP directly, only if SpaceMonkey causes
  trouble. 3. The pedal estimate as the fallback. Per SpaceMonkey's page, UEVR doesn't send UDP itself: its SpaceMonkeyUEVR plugin feeds
  SpaceMonkey, which makes the UDP. UEVR can run without VR (rename openvr_api.dll and openxr_loader.dll, tick Stereo Emulation Mode).
  Unchecked: UEVR's plugin API, UEVR under Proton, anti-cheat/online risk.
- Telemetry route, from a Reddit post (2026-11-04,
  https://www.reddit.com/r/StarWarsGalacticRacer/comments/1x32txk/star_wars_galactic_racer_vr_motion_gseat_dual/): not from the game
  itself but a modded chain: UEVR > SpaceMonkey (https://github.com/PHARTGAMES/SpaceMonkey), output CodeMastersExtraData3 on UDP 20777,
  config default_MMF_UDP_CMED3; SimHub reads it with its Dirt 4 profile. Our app already decodes that format and port.
  Unknown: running UEVR and SpaceMonkey inside Proton, UEVR without a headset or VR runtime, whether the motion data is good enough for
  forces, and anti-cheat/online risk from the injection. First test: does SpaceMonkey's output appear in our log on Linux?
- Force: centring growing with about speed squared against steering, damping rising with speed, boost and engine
  vibration as texture. Tuned by slider, since arcade steering can't be derived from physics.

## Wheel strength from calibration, no weights (2026-11-03)
Calibration measures torque / inertia only. Inertia is fixed per wheel model, so measure it once per model with the
hanging-weight test, then peak torque = inertia x measured full-force acceleration for any unit, with short pushes on
a free wheel. G29: stored a_fs 17587 deg/s^2 with about 4 Nm gives about 0.013 kg m^2. Log against the figure in use;
change no force until marth says.

## Wheel force curve check (2026-11-04)
Stored calibration curve is flat from command 0.4 to 0.7 and steep from 0.7 to 1.0 (fit R2 0.92). Check on repeat runs,
or with several hanging weights, whether the flat spot is real or a fit artifact.

## Rust hot path (2026-11-03, marth: polish stage only)
After the feel is settled and the engine process's timing shows where the milliseconds go, port the per-sample core
(decode, force, mapping, write) to a pyo3 module. Learners, scorer and tests stay Python. Template:
~/Documents/Scripts/rallysandbox (aether_core).

## RBR car identity from Cars.ini (2026-11-03)
The gearing fingerprint named five 2017-21 WRC cars for a Ford Fiesta RS WRC 2014, which isn't in data/telemetry/cars/rbr.json.
RSF's Cars/Cars.ini names the car in each slot exactly; read that instead, and add the missing cars.

## Other drives through the shared chain (2026-11-03, marth: paused after FH6)
Order: DR2, BeamNG, ACC. RBR and FH6 are on the chain; FH6 awaits its first drive.

## FFB benchmark engine
Track several forces across similar cars and build benchmark forces to tune every game against: column torque, trail, slide
force, band energy, near-zero time and falls, normalised by car and binned by load, slip, speed and surface. Extends
`scripts/ffb-parity.py`. Interpolated or extrapolated targets are marked, with their spread. Skoda Fabia RS Rally2 is in both ACR and RBR.
