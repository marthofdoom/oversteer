# Issue hunt, October 2026 (v0.15.4, master b30bbe2)

All 33 of marth's captures were replayed through the current code into a scratch store. The path is the live one:
`Telemetry.handle` → `ShiftLearner` / `RunTracker` → drive log → `coach_context` → coach. The coach's tips and
splits were read after every run end, as the GTK tab reads them, and marked seen, so repeats behave as they would
on screen. Everything below comes from that telemetry and from the code. Nothing was labelled by hand.

Reproduce (writes only under the folder you give, reads the captures read-only):

```
python3 research/issue-hunt/replay_hunt.py ~/.cache/oversteer-hunt/full     # ~3 min; hunt.db, runs.jsonl, log.txt
python3 research/issue-hunt/sweep.py ~/.cache/oversteer-hunt/full           # the automated anomaly sweep
python3 research/issue-hunt/steer_kinematics.py ~/.cache/oversteer-hunt/steer.json
python3 research/issue-hunt/counter_lag.py ~/.cache/oversteer-hunt/full/hunt.db
python3 research/issue-hunt/sector_check.py ~/.cache/oversteer-hunt/full/hunt.db
python3 research/issue-hunt/clock_cadence.py
python3 research/issue-hunt/dirt_signs.py <DiRT capture> ...
```

The replay gave 200 runs: 151 ACR, 19 DiRT Rally 2.0 (10-23), 6 WRCG and 24 others. The live Flatpak database,
read from a sqlite backup copy, has the same wrong values for the same runs (live run 170 = replay 179, live runs
173-191 = replay 182-200).

**(e) Logs.** There were no exceptions and no warnings from the app's loggers in any replay. The only noise:

- numpy `RuntimeWarning: All-NaN slice` at `coach_diagnosis.py:269`, on DiRT only (no yaw channel; see I-3).
- INFO `unknown 264-byte packet` on DiRT. These are menu packets that `_codemasters` rejects (max rpm 0), which is
  harmless.

---

## Ranked findings

Impact order: wrong numbers or text the user sees first, then wrong coaching, then polish. Evidence uses replay
run ids (`hunt.db`) unless marked "live".

### I-1. Coaching arrives about 15 s after every ACR finish (the timing complaint)

- **Where:** `drive_log.py:214-216` (`FINISH_STILL`, `FINISH_GRACE`, `FINISH_ROLLOUT`), `drive_log.py:392-395`,
  `_post_over` at `drive_log.py:400-409`.
- **Evidence:** across all 43 ACR finishes, the finish is detected 0.25-0.27 s after the game's clock stops (good).
  The run then ends a median 15.0 s later (min 8.8 s), and 22 of the 43 end only when `FINISH_GRACE` runs out.
  The car rolls 200-330 m to the stop control at 10-40 km/h and seldom stands still for 2 s. The drive log takes
  0.15 s (max 0.38) and tips plus splits take 0.06 s (max 0.24), so nearly all the wait is the grace. The live
  card shows the time at once. The tips, splits, Run view and "closer to the potential" lines only appear about
  15 s later, often after the restart menu.
- **Why it is safe to end early:** nothing driven past the finish counts. `_track` returns early once `_finish_t`
  is set and `_finish_counts` is false (`drive_log.py:614-622`), and the guard (`drive_log.py:830-836`, released at
  `418-439`) already stops a new run from starting on the roll-out.
- **Fix:** where the finish is not a progress one (`not self._finish_counts`, i.e. ACR `'game'` / `'line'`
  finishes and `_clock` finishes), end the run as soon as the finish is set. Either call `self.end(now, 'finish')`
  on the packet after `_finish_at` / the `_clock` finish, or make `_post_over` return True at once in that branch.
  Keep the guard exactly as it is. Keep `FINISH_GRACE_PROGRESS` for progress finishes (I-4 changes their result).
  Tests: an ACR finish ends the run within one packet. The next packet past the line does not start a run. A
  teleport (restart) after the finish releases the guard.

### I-2. A coarse game clock fakes the finish: run 179 / live 170 (Cwmbiga, 10-22) timed 8:06.7 instead of about 8:30.8

- **Where:** `_clock_finish` at `drive_log.py:778-789` (`CLOCK_STOPPED = 1.0` at `:206`), `RunClock.tick` at
  `drive_log.py:285-294`.
- **Evidence:** `clock_cadence.py` shows that in capture `20261022-165210` ACR's stage clock stepped once a second
  for the whole run (468 steps, median 1.035 s). Every other ACR capture steps at 0.016 s. Within
  `FINISH_CLOCK_BEFORE` (400 m) of the line, a normal 1 s gap between steps reads as "the clock stopped". The
  finish was declared at lap distance 11614 (the line is 11769), 14.5 s before the clock last moved, with result
  486.69 s. The car then hit something at 11225 m and crawled to the line. The clock really stopped at 510.83 s at
  ld ≈ 11775.
- **Consequences:**
  - That run's result is 24.1 s too quick.
  - Its last sector is untimed (`sector_check.py`: S4 `----`).
  - Its trace `t` is a staircase. `RunClock` holds `t` flat until a step, so time-based features are wrong
    (corner durations, counter-steer seconds, a_long from speed).
  - The Cwmbiga potential was built from it: user 486.1, grip 340.6. That gives top-3 lines of "about 7.1 s is
    there" in a single corner (see I-9).
- **Fix:**
  - (a) In `RunClock`, record the longest step seen while the car moves (`max_step`, ignoring the first and any
    standing still). Add a property `stopped_after = max(CLOCK_STOPPED, 1.5 * max_step + 0.25)`. Use it in place
    of `CLOCK_STOPPED` in `tick`, `_clock_finish` and `_clock`, and use `max(CLOCK_SETTLE, 1.5 * max_step)` in
    place of `CLOCK_SETTLE` in `_line_finish`.
  - (b) While `proven` and the clock stands still for less than `stopped_after`, advance `t` by `dt`, so the
    trace is not a staircase. On the next step take `t = max(t, stage_time + offset)` and keep `exact` False if
    `t` ran ahead. The clock must never go back.
  - (c) Belt and braces: if a `'game'` finish was declared but the clock moves again before the run ends, clear
    the finish (`_finished = None`, `_finish_t = None`, `live_run` un-finish or a new `finish` later) and carry on.
  - Tests: a synthetic run with a 1 Hz clock finishes at the line, not 155 m before it. A 60 Hz clock behaves as
    today.
- **Data repair:** the live DB has one affected run (live 170). After the fix, re-time it through the backfill:
  clear `run_class`, set the result from the trace if the clock stop is in it, else mark it `partial`. Or simply
  note it for marth.

### I-3. DiRT Rally 2.0 maps are mirrored, so every corner direction, counter-steer and DiRT potential is wrong

- **Where:** `telemetry_formats.py:280-296` (`plan_xyz` returns `(x, y, z)` for DiRT, "unchecked");
  `potential.py:880` (`plan = 'acr' if acr else None`).
- **Evidence:** `dirt_signs.py` uses the wheel labels as the absolute reference. Suspension dive under braking
  fixes the compression sign, then body roll in corners gives the direction of turn.
  - The decoded steer agrees with the roll (r +0.29/+0.36), and so does the lateral g (r +0.95/+0.94). Both signs
    are right.
  - The heading rate of the `(x, z)` plan is the other way (r -0.86/-0.83), so the plan is mirrored. The a_lat
    scale is right: slope -0.96 against v × heading rate, and a_long against dv/dt gives 0.92.
- **Consequences:**
  - The DiRT map is drawn mirrored.
  - `drive_log._yaw_rates` takes yaw from positions (DiRT has no yaw channel), so it is negated. Corner
    directions in coach text are swapped (a "3 left" is a right).
  - `counter_steer` averages 0.787 on DiRT corners against 0.113 on ACR. Every DiRT corner reads as
    counter-steered (steer_kinematics: 79-89 % of cornering rows "against").
  - `potential.py:880` gives DiRT `plan=None`. Curvature then comes from `yaw_rate`, which is NaN, then
    `nan_to_num`, so the road is a straight. The grip layer is 46-53 % under the user's time (Jagodno user 214.9,
    grip 115.3) and the top-3 says "about 15.9 s is there" in one hairpin (run 189).
- **Fix:**
  - `plan_xyz('dirt', (x, y, z))` returns `(x, y, -z)` (as ACR). Update the docstring with this evidence.
  - In `potential.after_run`, pass `plan = run's game` for any game whose runs have positions (`has_positions`),
    not only ACR.
  - In `potential.stage`, return None when more than 20 % of `k` is NaN before `road()`, so an unknown road is no
    straight.
  - Tests: `test_formats` plan for DiRT; a potential with no yaw and no positions returns None.
- **Data repair (needed, the stored rows are wrong):** a one-shot in `repair_shipped` or a store migration step,
  for runs of `dirt` cars:
  - set `run_class = NULL` so the backfill redoes corners, events and metrics;
  - `DELETE FROM stage_potential WHERE stage LIKE 'dirt:%'`;
  - leave traces untouched (raw positions are stored).

  Gate it on a `stage_lines`-style fingerprint or a new `repairs` key so it runs once.
- **WRCG (lower priority):** WRCG shows the same symptom at a lesser degree. Counter-steer averages 0.392, steer
  against k is r 0.09-0.45, and the best lag sits at the search edge. Run `dirt_signs.py`'s method on WRCG before
  trusting its corners. The roll sign is already known there (`telemetry_formats.py` WRCG susp comment).

### I-4. DiRT and WRCG results are 1-7 s too quick: the time is read at 99 % of the distance

- **Where:** `drive_log.py:671-676`. At `progress >= FINISHED (0.99)`, `_result_time = sample.stage_time`, and it
  is never updated afterwards (`_clock` returns once `_finish_d` is set, `drive_log.py:579`).
- **Evidence:** every DiRT finish (sweep "timing") is short of the game clock's stop by 1.08-6.70 s:
  - Zagorze 597.33 against 604.03;
  - Jezioro Lukie 223.51 against 228.64;
  - Zienki 433.58 against 438.47.

  WRCG is short by 0.54-4.32 s (Monti di Ala 632.46 against 636.78). These are the PBs, splits and potential
  user layers marth sees for DiRT.
- **Fix:**
  - While `_finish_counts` and the clock is still moving (`sample.stage_time > _result_time`) and
    `sample.progress < 1.0` or the clock has not stood for `CLOCK_STOPPED`, keep
    `_result_time = sample.stage_time` and `_finish_d = d`.
  - Freeze both once the clock stands still (moving) for `CLOCK_STOPPED`, or at `progress >= 1.0`.
  - Call `live_run.finish` again with the final time; it must accept an update.
  - The run still ends via `_post_over` as today.
  - Test with a cut of `20261023-222056` (Zagorze): the result is 604.0 ± 0.1.
- **Data repair:** re-time the stored DiRT and WRCG finished runs from their traces (`t` at the last row where
  the clock moved). This can be folded into the I-3 backfill.

### I-5. Tips about other stages fill the coaching while you drive a new one

- **Where:** `coach.Coach.tips` / `_place_tips` (`coach.py:935-968`, `1309-1340`, `STAGES_COACHED = 3` at
  `coach.py:544`), `select()` at `coach.py:2284`.
- **Evidence:** (sweep "tips")
  - 30 runs show non-quiet tips about a stage other than the one just driven.
  - On Forêt de Munster, 10-14 runs 105-124, every coaching view is Elatia's corner tips and Elatia praise. On
    Cwmbiga, 10-22 (run 179, eight days later), five of the twelve lines are about Elatia.
  - On Petit Ballon with the i20N (run 156), the tips are about Sommet de Munster and Obersteigen from 5 Oct.
  - The same five Elatia lines were shown verbatim after 22 runs. They never go quiet, because a "showing" within
    `SHOWING` counts once, and after 7 days they are fresh again.
  - While a new stage has only restarts (`run_class = 'restart'`), it is skipped (`coach.py:1324-1329`) and the
    previous stage is coached as if it were current.
- **Fix:**
  - (a) In `tips()`, find the stage of the car's newest run of any class, restarts included. Call it `current`.
  - (b) Section, spread, incident and praise tips of other stages get `kind = 'still'` (with the "Still:"
    prefix), or go in a separate `other` group the views fold. They never take the `limit` budget or the focus
    from `current`.
  - (c) If `current` has no classified run yet, show no stage-place tips for other stages above the fold. Add one
    note instead: "First full run of <stage> gives the coach a reference."
  - (d) `_potential_lead` only for `current`.
  - Tests: a car with Elatia runs then Forêt restarts gives no non-still Elatia tip.

### I-6. "Your best clean run" is misnamed, and the first finish of a stage is never a PB

- **Where:**
  - `coach.py:2125-2129` `_prior_best` (clean only);
  - `coach.py:1366-1372` (`ref_name`);
  - `coach_context.py:437-441` `stage_pb_id` and `coach.py:2087-2089` `stitch` (PB = clean only);
  - `coach_context.py:430-433` (`learning` = first run of the car on the stage, or the first after `AWAY` = 7
    days).
- **Evidence:**
  - New Loutraki run 181 (202.44, the quickest ever) is told "0.1 s behind **your best clean run** here (Skoda
    Fabia RS Rally2, 3:26.4 on 22 Oct)". That reference is run 180, a *learning* run 4 s slower. `_prior_best`
    ignores learning runs, so `beat` is False and the wording says "best".
  - Six car/stage pairs have finished runs and no PB at all, because their only finishes are `learning`:
    - Petit Ballon: i20N 2:53.3, Subaru 3:02.7, Alpine 3:21.0;
    - Forêt: Alpine 4:31.3, Delta 3:53.4;
    - New Loutraki: after run 180.

    The splits row shows no best (`splits()` returns None, run 180 "NO SPLITS").
  - Afon Bidno run 7 (3:34.9, class `off`) shows "Best 4:00.3". The potential user layer (240.3) is 25 s slower
    than the run just driven.
- **Fix:**
  - (a) `_prior_best` uses the same set as `reference_run` (clean and learning).
  - (b) `ref_name`: say "your previous best run" / "your best run" when the reference is `learning`, and keep
    "clean" only when it is clean.
  - (c) The PB (`stage_pb_id`, `stitch`'s `pb`, `potential.stage`'s `pick`) counts `clean` and `learning`. The
    `AWAY` re-learning only marks the run, and a faster learning run is a PB. This respects "PBs never deleted":
    nothing is deleted, the set only grows.
  - (d) **Decision for marth:** should a finished run with an `off` be a PB? Rally times count whatever happened.
    Recommendation: show it as "Fastest 3:34.9 (with an off)" beside the clean PB, and never let the potential
    user layer be slower than the fastest finished run.
  - Tests in `test_coach.py` for (a)-(c).

### I-7. Counter-steer is over-counted on ACR because the steer leads the yaw by 0.15-0.3 s

- **Where:** `drive_log.py:1645-1661` (`_corner`: steer at row k against the smoothed yaw at row k).
  `coach_diagnosis.py:149` and `:264` use the same zero-lag comparison.
- **Evidence:** `steer_kinematics.py`, at 60 Hz over every ACR capture:
  - The best correlation of steer with path curvature is with the yaw **0.15-0.31 s later** (Fabia 0.27-0.31 s,
    i20N 0.15-0.28 s, 208 0.15-0.21 s).
  - At lag 0, 10-15 % of cornering rows are "against". At the best lag it is 2-5 %, so about two thirds of what
    the counter-steer share counts at packet rate is the wheel unwinding before the car stops turning.
  - In the stored 10 Hz corners (`counter_lag.py`, 1522 ACR bends), corners at ≥ 20 % counter-steer fall from 302
    at lag 0 to 214 at 0.2 s and 161 at 0.3 s. The counter-steer seconds fall 249 → 193.
- **What the user sees:** "steered against the slide for 33 % of the corner (your best run 4 %)", the
  diagnosis's SLIDE row, "2.7 s of counter-steer in it, about 0.4 s" in the top-3, the tuning's counter-steer
  pool, and `COUNTER_DELTA` gates.
- **Fix:**
  - Compare `steer[k]` with `yaw[k + LAG]`, with `LAG = 2` rows at 10 Hz. Make `STEER_LAG_S = 0.2` per game
    (ACR measured; DiRT is re-measured after I-3).
  - Additionally, only count rows where `|yaw| > 0.1 rad/s`, so a straight's wobble is not "against".
  - Apply the same in `coach_diagnosis` (resample the steer 0.2 s earlier, or the yaw later).
  - Run `counter_lag.py` before and after. Expect corners ≥ 20 % to fall by about half.
  - Re-tune `COUNTER_DELTA` / `COUNTER_MORE` (0.15) only after the backfill has rewritten the corners. Clear
    `run_class` for all runs to rebuild.

### I-8. Spins: hairpins and recoveries are called spins, with incoherent numbers

- **Where:** `coach_context.spins` (`coach_context.py:364-380`, `SPIN_HEADING = 200`), `coach._incident_says`
  (`coach.py:1589-1628`).
- **Evidence:** 70 spin events, including:
  - "the car turned 859 / 705 / 688 / 560 degrees" (runs 31, 132, 89, 178);
  - "you came in at 2 km/h and were down to 4 km/h" (run 129);
  - "came in at 11 km/h … 402 degrees" (run 93);
  - three spin notes at 4.6 km in one run, as a "hairpin left", a "hairpin right" and a "1 right" (run 93).

  Turning the car round after an off, or a 200°+ switchback (Petit Ballon, Sommet), counts as a spin. The fix
  offered, "Catch it with opposite lock … (you steered against it for 0 % of it)", follows from I-7's measure.
- **Fix:**
  - (a) A spin needs the body to rotate past the path. Over the corner window, compare the integrated yaw rate
    with the heading change of the path (`plan_xy` positions; yaw over speed where there are none):
    `|yaw_turn - path_turn| > 90°`.
  - (b) Entry speed ≥ 30 km/h and minimum speed ≥ 5 km/h before the peak rotation. A stretch under `SLOW` is a
    stall or off, never a spin.
  - (c) Merge spin events within `INCIDENT_REACH` into one.
  - (d) In `_incident_says`, drop the speed fact when `low > entry`. Offer the opposite-lock fix only for a spin
    from (a) at ≥ 40 km/h.
  - (e) The DiRT note "you spun in the hairpin left at 0.0 km." at the start comes from position noise at rest.
    It disappears with (b).

### I-9. Top-3 "time available" built on one bad run: "about 7.1 s is there" in a single corner

- **Where:** `potential.stage` (`potential.py:699-760`) accepts one finished run of the car (`full` ≥ 1), any of
  `RANKED_CLASSES` including `off`. `coach._potential_lead`.
- **Evidence:**
  - Cwmbiga run 179 (the I-2 run, `off`, one finished Fabia run): 7.1 s, 6.9 s and 5.5 s in single corners.
  - Afon Bidno run 57: 5.9 s at 1.8 km.
  - Petit Ballon run 159 (Alpine, first finish): 6.4 s in the 5 right.
  - Jagodno (DiRT, I-3): 15.9 s.
  - Grip layers 10-30 % under the user layer are normal on ACR (New Loutraki 202.4 / 176.7). Cwmbiga's 486.1 /
    340.6 (30 %) and Afon Bidno car 2's 240.3 / 179.8 (25 %) are the outliers.
- **Fix:**
  - The potential needs ≥ 2 finished runs of the car on the stage, or one `clean` run. A place is quotable only if
    the user's best pass through it comes from a run without an off within `OFF_REACH`.
  - Cap a place's `available` at 35 % of the user time through it. Above the cap, say "the grip layer and you
    differ a lot here: check the line" as a note, not a number.
  - Tests: one off-run gives no top-3.

### I-10. DiRT post-finish phantom runs create junk stages

- **Where:** the guard is only set for `reason == 'finish'` (`drive_log.py:830-836`). A DiRT run whose progress
  said finished ends by `'restart'` (the clock resets at the results screen), so no guard is set.
- **Evidence (sweep "phantom"):** after every DiRT finish, a 107-124 m run starts at the finish (ld = the stage
  length). These are runs 183, 185, 187, 190, 192, 195, 198 and 200. Each gets a new stage key at the finish's z:
  `dirt:6656:-3260`, `dirt:13180:4500`, `dirt:6438:4500` and so on. The live DB has the same 8 extra unnamed
  stages. On the next real run, a junk stage can be matched by `match_stage`, because its length is identical
  and only the start z differs.
- **Fix:**
  - Set the guard when the ended run had `finished == 1`, whatever the reason.
  - Also refuse `_start` while `sample.progress is not None and sample.progress >= FINISHED`, for non-lap games.
  - Data: delete runs with `finished IS NULL AND distance < 200 AND` a stage that has no other runs and is not in
    the table. Delete those stages.

### I-11. Steering: what the number is, and how it is shown (ClickUp 86e3m0j5w)

There is no evdev ground truth in the captures, so this is the kinematic check (`steer_kinematics.py`, bicycle
model at 2.5 m wheelbase, low-slip rows |a_lat| < 3 m/s²).

- **Scale:** ACR's steer is the game's input as a fraction of **the car's own lock**. It clamps at ±1, and 0.3-3 %
  of moving time is at ≥ 0.9 (9 % for the 208 at Livigno). Curvature per unit steer gives about 12-17° of
  road-wheel angle at steer 1.0 at speed, and 17-26° at walking pace (Fabia 15/24°, i20N 12-17/15-20°,
  208 8-10/11-18°). The difference is understeer at speed, so the channel is close to linear in road-wheel angle.
- **Offset:** below 0.01 straight-ahead in every capture.
- **Sign:** positive left is confirmed for ACR (r +0.4…+0.87 with curvature) and for DiRT by the suspension roll
  (I-3).
- **Typical use:** median |steer| is 0.09 and p99 0.6-0.9. The ±1 strips (live `telemetry_plot.py:655-661`, web
  `index.html:1469`, Run view `telemetry_plot.py:305-313`) therefore show rally steering as a flat line with
  wiggles. The Run view readout (`gtk_run_view.py:414`) prints `steer 9` with no unit. This, more than a wrong
  decode, is the likely "I don't think it's accurate".
- **Alpine A110 10-17 13:06:** r is negative (-0.28). That capture is 10 restarts with crashes; its
  yaw-to-heading agreement is only 0.12, so it is not a decode fault.

Fix spec:
- **(a) Units:** label the steer readouts "% lock" (Run view `steer 9 %` → `9 % lock`; strip label "STEER %
  lock").
- **(b) Strip scale:** use an odd-power scale, `y = sign(s) * |s|^0.5`, with gridlines at 10/25/50/100 %, so 5-30
  % steering is readable. Apply it in all three strips and keep the clamp.
- **(c) Degrees:** extract each ACR car's steering lock (hand-wheel degrees, and road-wheel max if present) from
  the vehicle data assets alongside the gear sets (the car_data pipeline; field `steer_lock_deg`). Then show
  `s * lock / 2` in degrees of the hand wheel when known.
- **(d) Counter-steer:** see I-7.
- **ClickUp evidence report:** the numbers above. The evdev comparison still needs a capture with the G29's ABS_X
  recorded beside the telemetry. Add an optional evdev column to the capture (that one is a capture-format
  change: **tier A**, a separate task).

### I-12. Number and text polish in coach lines

- **(a) Arithmetic:** in `potential.py:598`, "carry 29 km/h more to the apex (the grip allows 114, you took 86)"
  computes the difference before rounding. 10 of the 39 such lines are off by one (runs 20, 91, 93, 98, 101, 104,
  116, 156 ×2, 180). Fix: `a, b = round(pot), round(took)` and print `a - b`.
- **(b) Launch praise:** `coach.py:1228-1230` gives "from 1.7 7 days ago" and "over your last 1 session". Make it
  "Quicker off the line: 1.2 s to 50 km/h this session, down from 1.7 s 7 days ago." (`n == 1` → "this
  session").
- **(c) Other praise lines:** the same "over your last 1 session" / "from X.Y N days ago" pattern is in
  `coach.py:1069` (lights band: "Better: 3→4 is on the lights band … over your last 1 session"), `:1155` and
  `:1172`. Apply the same rule to all four.
- **(d) Raw keys:** `coach.py:753-757` `_stage_name` prints raw keys, e.g. "On stage dirt:2846:190, …" (run 186).
  Fallback: "an unnamed {:.1f} km stage", or the location when known.
- **(e) Car names:** "Alpine_A110" is the shipped `screen_name` (`data/telemetry/cars/acr.json`). Make it "Alpine
  A110". DiRT cars are named "9000 rpm, 6 gears" for two different keys (`dirt/9000-1200-6`,
  `dirt/9000-1300-6`). Append the idle ("9000 rpm, 6 gears, idle 1300"), or let marth rename them.
- **(f) Incomplete note:** the spin note without facts, "On Jezioro Lukie, you spun in the hairpin left at
  2.3 km." (DiRT: no corner matched), should be dropped or merged when there are no facts.

### I-13. DiRT's engine never learns

"Still learning the engine (0 of about 44 rev bands known)" was shown after all 19 DiRT runs: 6 full stages, about
35 minutes. Band learning gets nothing from DiRT.

- **Where to look:** `ShiftLearner._drive_sample` / `CarModel._estimate`. DiRT sends a kinematic a_long
  (`accel_kind='kinematic'`); check whether the band estimator requires `'specific'` or a gear ratio that is not
  yet known.
- **Spec:** reproduce with `replay_hunt.py --only 20261023-222056`. Print `model.known_bands()` after the run and
  the reject reason per pull. Fix the gate. A DiRT car with no shipped data should have ≥ 10 bands after a 10 min
  stage.
- Lower priority: marth's focus is ACR.

### I-14. Smaller checks that passed (no action)

- **ACR finish detection:** all 43 game-clock finishes are within 0.27 s of the clock stop and at the table's line
  (within 1.1 m), on every new stage driven on 10-17 and 10-22: Forêt, Petit Ballon, Obersteigen, Elatia - Zeli,
  Cwmbiga and New Loutraki. Stage recognition and cars were right on every ACR run.
- **Sectors:** for every finished ACR run, all game sectors are timed and sum to the result within 0.03 s
  (`sector_check.py`). The one exception is run 179 (I-2).
- **Formats and ranges:** time format (m:ss.s absolute, signed s differences) is correct in every coach line. Grip
  used is always 0-100 %. Potential layers are always ordered user ≥ grip ≥ car.
- **Restarts:** ACR restarts end runs at once (`teleport`). The post-finish guard never swallowed a real start.

---

## Fix batches (for sonnet-coder; every diff gets an Opus review)

There are no tier-A items in batches 1-7: no threading, ABI or file-format change. Batch 1 edits the
listener-side `RunTracker`, which runs per packet under the learner lock. Keep it constant-time and treat it as
the riskiest tier-B. The evdev capture column in I-11 is tier A and stays out of these batches.

1. **Finish and timing:** I-1, I-2, I-4, I-10 (guard part). `drive_log.py` RunTracker/RunClock only, with tests
   in `tests/test_drive_log.py` from synthetic packets plus a cut of `20261022-165210` (1 Hz clock) and
   `20261023-222056` (DiRT). Acceptance: rerun `replay_hunt.py` and `sweep.py`. The "timing" section is empty
   except WRCG, and the ACR run end is under 1 s after the finish.
2. **DiRT geometry and potential inputs:** I-3, potential `plan` for all games, NaN-road refusal, I-10 start
   refusal, plus the one-shot DiRT requeue/repair. Acceptance: DiRT counter-steer mean < 0.2, grip layer within
   10-35 % of the user layer, and `dirt_signs.py` heading r > 0.8.
3. **Coach scope:** I-5. Acceptance: sweep "runs whose tips (not still) are about another stage" = 0.
4. **PB and reference semantics:** I-6 (a)-(c), and (d) once marth decides. Acceptance: run 181 says "previous
   best run", and every car/stage pair with a finish has a PB.
5. **Steering:** I-7 (lag) and I-11 (a)(b) UI units and scale, then a full backfill. Acceptance: `counter_lag.py`
   shows corners ≥ 20 % roughly halved. The screenshots and demo are recaptured per the screenshots-follow-UI rule.
   Track I-11 (c) (car data `steer_lock_deg`) as a separate data task.
6. **Incidents:** I-8 spins and `_incident_says`, I-9 potential guards. Acceptance: no "turned > 360 degrees",
   no entry under 20 km/h, no top-3 over 35 % of its section.
7. **Text polish:** I-12 (a)-(f). This is haiku-coder material, except (d), which needs the stage length plumbed
   in (sonnet).
8. **DiRT engine learning:** I-13. This is investigation first, so give it to sonnet-coder with the repro above.

Order: 1 → 2 → 3 → 4 (user-visible wrong numbers and the timing complaint), then 5 → 6 → 7 → 8. Batches 2 and 5
rewrite stored corners through the backfill. Do them in one release, so the backfill runs once.
