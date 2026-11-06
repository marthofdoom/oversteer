# FFB tools for adding a game

All read only: no wheel, proxy, game or telemetry database is touched. Each writes its full log to `~/.cache/<tool>/latest.log`.

- `scripts/new-game-check.py CAPTURE [--vs REFERENCE]`: the game a recording decoded as, the channels the force code reads that are present or missing, then `ffb-sim-match` against the newest ACR capture and `ffb-parity`; says so when the game has no rack yet.
- `scripts/ffb-sim-match.py A [A2 ...] --vs B [B2 ...] [--class-a NAME] [--class-b NAME] [--peak NM]`: today's rack over recorded physics in `ffb-match`'s situations (ACR, RBR, Forza racks; the power class from the game's profile).
- `scripts/ffb-force-curve.py A [A2 ...] --vs B [B2 ...] [--class-a NAME] [--class-b NAME] [--peak NM] [--set NAME=VALUE ...]`: today's rack's rim force in Nm by the front axle's lateral force and the normalised slip (the per-game definition is `THETA_TABLE` in `ffb_tools.py`), the cells where A and B differ, the effective trail by slip, and for ACR Newton's front force against the tyres'; `--set ffb_forza.TP_S_HALF=0.4` tries a constant for the replay only; log `~/.cache/ffb-force-curve/latest.log`.
- `scripts/ffb-stages.py A [A2 ...] --vs B [B2 ...] [--class-a NAME] [--class-b NAME] [--peak NM] [--set NAME=VALUE ...] [--mass-a/--mass-b KG] [--ratio-a/--ratio-b R]`: every stage of today's force chain (ay, speed, front slip, Fyf with its mass and b/L, kingpin torque with its caster and pneumatic parts and the effective trail, ratio, column, driver after the boost and its gain, rho, rim, share, Nm played) by |ay| and speed, time-weighted, A median, B median and A/B per cell and the A/B per stage over all cells, under |ay| 5 and from 5; `--mass-a/b` and `--ratio-a/b` replace the mass (not for ACR) and steering ratio the rack uses (RBR: `DERIVED_RATIO`, not the table ratio); for ACR also the true mass (four tyres' Fy over ay) and its front share; log `~/.cache/ffb-stages/latest.log`.
- `scripts/ffb-match.py A --vs B`: what two sets of recordings actually played in the same situations.
- `scripts/ffb-parity.py [CAPTURE] [--ref CAPTURE]`: a recorded drive against a reference, flags over 25 %.
- `scripts/ffb-wheel-model.py [--old RUN.json.gz] [--weight KG SHARE]`: what the wheel delivers for an asked torque, from the stored calibration, and the hanging-weight cross-check.
- `scripts/calib-check.py [RUN ...]`: a saved calibration run's outcome, pulses, run-ups, odd pushes, a refit with today's fit and accel against speed.

## Order for a new game

1. Record a drive of the game with the telemetry capture.
2. `new-game-check.py CAPTURE`: which channels the game gives; the math is derived from these (see `docs/new-game-checklist.md`).
3. Write the game's rack in `oversteer/ffb_engine.py`, add it to `RACKS` in `scripts/ffb_tools.py` (rack class, profile file, default power class).
4. `ffb-sim-match.py CAPTURE --vs <ACR capture>`: the situations where the new force differs from ACR's; fix the biggest gaps first.
5. `ffb-parity.py CAPTURE`: the measures against the reference, once the game's own recording has a force sidecar.
6. After a wheel calibration, `calib-check.py` for the run and `ffb-wheel-model.py` for what the wheel then delivers.
7. `ffb-components.py CAPTURE [CAPTURE ...]`: what the played force is made of (per band 0-1, 1-6, 6-30 Hz: Nm per cause from a joint fit, errors-in-variables check on yaw acceleration, settle-after-a-turn sign flips); log `~/.cache/ffb-components/latest.log`.
