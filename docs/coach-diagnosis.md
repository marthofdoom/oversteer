# Corner diagnosis: from "what differed" to "what to do"

Status: implemented (`oversteer/coach_diagnosis.py`, wired in `coach._stage_place` and `coach._potential_lead`); validated on marth's runs.
The prototype is `research/diagnosis/`; its validation on the app's module gives the same distribution as section 6.
Branch point: master at v0.15.0 (a48ff6f).

## 1. The bug, and what it shows

The coach told marth, on Afon Bidno - Severn in the i20N:

> you braked 41 m later, were 14 km/h slower at the slowest point and left 16 km/h slower.
> Brake 41 m earlier, where your best run did.

That is run 57 (199.7 s) against run 51 (198.1 s), the left-left-right at 2.2 km. The replay of the captures
reproduces it word for word. What the traces actually show:

- None of those three numbers is about the same corner. The "41 m later" compares the lead corners' braking,
  each measured back from **its own** slowest point (2048 m in run 57, 2066 m in run 51). The "14 km/h slower"
  compares each run's slowest corner of the complex: run 57's is the third corner (60 km/h at 2.15 km), run 51's
  the first (74 km/h at 2.07 km). The "left 16 km/h slower" is a third pair, each run's last corner, read 2 s
  after the slowest point (a time, not a place).
- Measured on the same metres, run 57 braked for the first corner 24 m later and was 3 km/h faster at its slowest
  point (it lost 0.3 s there, arriving 45 km/h slower after a moment in the corner before). The rest of the time
  (1.45 of the 1.74 s) went at the 4 right at 2.13 km: run 57 braked hard there (95 % pedal, 24 km/h off) where
  run 51 did not brake at all, was 24 km/h slower at the slowest point (67 against 90) using 74 % of the grip, and
  38 km/h slower 50 m later.
- The fix "brake 41 m earlier" would have made it worse. marth's reading was right: braked later, then braked
  too much. The next quicker pass (run 59, 1.1 s quicker there) did not brake there at all.

The new diagnosis of that section:

> On the left-left-right at 2.2 km (the 4 right at 2.13 km), 1.7 s behind your best run: you braked where your
> best run did not (24 km/h off), were 24 km/h slower at the slowest point (67 against 90) and used 74 % of the
> grip there (your best run 81 %). Brake less there, or not at all, as your best run did: carry 24 km/h more
> through the slowest point. About 26 % of the grip was left there.

(The section before it, the 2 left at 1.9 km, also matters: run 57 had a moment there, 53 % of the corner
steered against the yaw, and arrived at the complex 45 km/h slower. The new table names that section SLIDE.)

## 2. Audit: every rule that turns one measure into advice

Each row is a rule in the shipped code that reads one number (or two) and gives advice without checking the
measures that would contradict it. "Evidence" is from the validation in section 6 (128 losing sections).

| # | Where | Rule | What it does not check | Evidence |
|---|---|---|---|---|
| A1 | `coach_context.brake_application` | The braking point is the onset of the application with the **highest pedal peak** | Which application took the speed off. A turn-in stab or a second application can be harder than the braking | Part of A2's numbers |
| A2 | `describe_corners` → `brake_d`; `compare_section['brake']` | Braking point = metres before the corner's **own** slowest point; the difference is "earlier/later" | Where the slowest point is. An apex 20 m later reads as "braked 20 m earlier" with the same pedal | Of 197 sections with both measures, 82 differ from the absolute onset by more than 20 m, 24 point the other way (old "earlier", trace "later" or back) |
| A3 | `drive_log._corner` | `entry_speed`/`exit_speed` are read 2 s before/after the slowest point | A slower run covers fewer metres in 2 s: the two runs' "exit" are different places | Built in |
| A4 | `compare_section` | Speed from each run's slowest corner of the section, braking from the lead corners, exit from the last ones | That in a complex the two runs' slowest corners can be different corners | 25 of 67 losing complexes: the "slowest point" speed is off by more than 5 km/h from the corner that lost the time |
| A5 | `section_pattern` `overdriven` → `_section_action` "Brake N m earlier" / "Enter N km/h slower" | Braked later (A2) or entered faster, and a slower exit | The minimum speed, the speed shed, the grip used | 18 such sections: 13 had a lower minimum, 12 of them with at least 15 % of the grip unused. The quicker next pass braked earlier in 33 % (base rate 30 %): no better than chance |
| A6 | `over-slowing` → "Brake about N m later" | Braked earlier (A2) and a lower minimum | Whether it also took more speed off; A2 | The quicker next pass braked later in 15 % (base rate 16 %): no better than chance |
| A7 | `under-committed` → "Brake at the same place and carry more speed in" | Same braking point, lower minimum, slower exit | How to carry it (less pedal, earlier release), the grip used | Right direction, no how |
| A8 | `slower` → "Carry more speed through it" | A lower minimum alone | The grip used (at the limit, more speed is not there), the run-up (arrived slower) | 7 of 27 were a slow run-up (SLOW-ARRIVAL), 1 at the grip limit |
| A9 | `late-throttle` → "Throttle sooner" | Throttle past 20 % 0.2 s later than the reference (time from each one's own slowest point) | The exit speed, where the time went | - |
| A10 | `over-rotated` → "a smaller flick, a shorter handbrake pull" | A lower minimum with more counter-steer | The handbrake and the flick are not measured: an unmeasured cause | 4 of 18 were an overshoot (in too fast), 2 a slow run-up |
| A11 | `_section_action` fallbacks | "look at where you braked and **the line you took**" | The line is not measured (positions are not used) | 26 of the 33 `unclear` sections get a measured diagnosis |
| A12 | `coach._best_of` / `corner.entry` praise | Quotes `_how` (A2, A4) for the gain | Same as A2/A4 | - |
| A13 | `coach._patterns` `throttle.late`, `coast.entry` | One measure over several corners | The minimum and exit speed, the braking | - |
| P1 | `potential.analyse_run` `cause` | The largest of entry/apex/exit loss against the grip layer; "entry" runs from the section's start | The run-up: a slow exit of the corner before is "entry" | 16 of the 54 "entry" places were SLOW-ARRIVAL |
| P2 | `potential.call` `'entry'` → "Brake later and carry the speed to the turn-in" (bend: "brake later") | The run's braking point, how much it took off | Of 54 losing places it was said for, the diagnosis gives another fix in 41: OVER-SLOWED 10, SLOW-ARRIVAL 16, SLIDE 9, OVERSHOT 5, EXIT-BRAKE 1 |
| P3 | `potential.call` `'exit'` → "Get to full throttle sooner after the apex" | Whether the throttle was already full (then the gear or the speed carried is the time) | - |
| P4 | `potential.critique` gear term | "a gear shorter than that costs about 0.2 s" at every pass in a shorter gear | Whether this pass lost time on the exit; the cost is the i20N's average, not this pass's | - |

The fix for all of them is one rule: **no advice from one measure**. The diagnosis reads the braking point
(absolute, same metres), the speed taken off, the minimum and where it is, the grip used there, the exit, the
throttle, the gear and the rotation together, corner by corner, and names one fix only where they agree.

## 3. The measures

Per section of the reference's grid (`sections_of(ref corners)`, bounds as `section_loss` times them: from the
reference's braking for it to the next one's, clipped to `exit_end`), and **per corner of the reference inside
it** (a complex is split at the reference's braking for each next corner, or at its fastest point between two
corners where it did not brake). Both runs on a common 1 m grid of the stage distance
(`potential.resample`/`elapsed` of `potential.arrays(stage_rows(trace))`). X is this run, R the reference; every
delta is X less R; distances positive = further down the road (later), speeds km/h.

| Measure | Definition |
|---|---|
| slowest point `min_x`, `min_r`, `min_dv`, `min_dd` | R's minimum in the corner's yaw window ±30 m; X's minimum within 40 m of R's. Speed delta and position delta (positive: later apex) |
| braking zone | Brake > 0.1 applications merged across gaps < 8 m, each shedding ≥ 3 km/h. R's zone is the one that **shed the most speed** before its slowest point (not the hardest pedal); X's the one overlapping it most, else the nearest onset within 80 m. If one run braked in the window and the other did not, the other's is looked for up to 80 m before (never into the previous corner's) |
| `onset_dd` | X's braking onset less R's, absolute metres (A2 fixed) |
| `release_dd` | X's last metre of brake in the zone less R's (positive: held the brake longer) |
| `shed_x`, `shed_r`, `shed_dv` | Speed taken off in the zone: speed at its onset less the lowest before the next zone or the slowest point |
| `peak_x/r`, `integral_x/r` | Pedal peak and pedal-metres in the zone |
| `pre_dv` | Speed 5 m before the first of the two onsets: slower before either braked = the run-up |
| `arrive_dv` | Speed at R's braking onset |
| `turnin_dv` | Speed at R's yaw-window start of the corner |
| `drop_x`, `drop_r` | Speed lost from the first onset to each one's slowest point (same metres; used where one run did not brake) |
| `exit_dv` | Speed 50 m past R's slowest point (a place, not a time; A3 fixed); `end_dv` at the stretch's end |
| `pickup_dd`, `full_dd` | Throttle past 20 %, and to 95 %, after each one's slowest point: metres delta |
| `coast_ds` | Seconds with both pedals under 5 % from the end of braking to the throttle pickup: delta |
| `exit_brake_x/r` | Speed shed by braking after the slowest point, to the stretch's end |
| `gear_min_x/r`, `gear_exit_x/r` | Mode of the gear ±10 m around the slowest point and the exit point |
| `grip_x`, `grip_r` | Median \|lateral g\| ±4 m around each one's slowest point over the envelope's lateral limit at that speed (the stored potential's `env_bins`/`env_lat`, P98 of the driver's own g-g) |
| `counter_x/r`, `counter_d` | Share of the corner's window (R's yaw window) steered against the yaw (steer·yaw < 0, \|steer\| > 0.02) |
| `yaw_d` | Peak \|yaw rate\| delta in the window |
| `line_turnin`, `line_apex` | With positions (ACR v4 captures): X's signed lateral offset from R's path at R's turn-in and slowest point (positive: inside) |
| `t_approach`, `t_brake`, `t_exit`, `loss` | Time delta over R's distances: stretch start → R's onset → R's slowest point → stretch end |

The section's diagnosis is the diagnosis of the corner (stretch) that lost the most; its sentence names that
corner when the section has several ("the left-left-right at 2.2 km (the 4 right at 2.13 km)").

## 4. The decision table

Tried in order; the first row that holds wins. `loss` is the stretch's time against the reference (the
section's loss is said; the stretch's decides). Thresholds in section 5.

| # | Code | Condition (all) | Fix (one) | Confidence |
|---|---|---|---|---|
| 0 | SAME | \|loss\| < 0.1 s | none | - |
| 0 | GAIN | loss ≤ -0.1 s | praise, the measures that were better: carried N more, braked N later (with no lower minimum), left N faster, throttle N sooner | high if any |
| 1 | SLOW-ARRIVAL | `pre_dv` ≤ -5 and ≥ 60 % of the loss before the slowest point | "The time here was lost before the braking: look at the exit of the corner before." | medium |
| 2 | OVERSHOT | in faster (`turnin_dv` ≥ +3, or braked ≥ 10 m later and `arrive_dv` ≥ +3) and (lower minimum, lower exit or apex ≥ 15 m later) and (grip ≥ 0.92, or counter-steer +0.15, or apex ≥ 15 m later) | braked later: "Brake N m earlier, where your best run did, and turn in at its speed." else "Get the speed off before the turn-in: arrive N km/h slower, as your best run did." | high with ≥ 3 conditions and loss ≥ 0.2 s; medium 2; low 1 |
| 3 | SLIDE | counter-steer +0.15 over R and (lower minimum or lower exit) | "Keep the car straighter through it, as your best run did." (fact: the shares) | medium (low under 0.2 s) |
| 4 | LOST-SPEED | lower minimum, no braking, grip < 0.30 at the slowest point | none (a lift, a bump, a moment or a hit: the trace cannot tell them apart) | low |
| 5 | **OVER-SLOWED** | lower minimum (≤ -3), braking point not earlier (> -10 m), grip < 0.85 (or no envelope and more taken off), and more speed taken off (`shed_dv` ≥ +3 or `drop` ≥ +3) or no braking at all | how: no brake → "Lift less"; R did not brake → "Brake less there, or not at all, as your best run did"; released ≥ 10 m later → "Keep that braking point and come off the brake N m sooner"; peak ≥ 10 points harder → "…and brake less hard (X % pedal, your best run Y %)"; else "…and brake less". Then ": carry N km/h more through the slowest point. About P % of the grip was left there." (P only when grip ≥ 0.30) | as OVERSHOT |
| 6 | EARLY-BRAKE | braked ≥ 10 m earlier, ≥ 0.05 s lost before the slowest point, minimum not higher (< +3) | "Brake N m later, where your best run did." With a lower minimum: "…: carry M km/h more through the slowest point"; with more taken off too: "Brake N m later and less, as your best run did: carry M km/h more…" | as OVERSHOT (+1 when the minimum is not lower) |
| 7 | EXIT-BRAKE | braked again after the slowest point, ≥ 6 km/h more than R, and ≥ 50 % of the loss after the slowest point | "Keep the speed up after the slowest point: no brake on the way out, as your best run did." | medium |
| 8 | LATE-THROTTLE | minimum not lower, throttle ≥ 10 m later, exit lower or exit time lost | "Throttle N m sooner after the slowest point, where your best run did." | as OVERSHOT |
| 9 | COASTING | ≥ 0.3 s more with neither pedal between brake and throttle | "Go from the brake to the throttle without the gap." | medium |
| 10 | GEAR | (exit lower, end lower or ≥ 50 % of the loss on the exit) and another gear on the exit | "Use Nth out of it, as your best run did." | medium with a lower exit speed, else low |
| 11 | EARLY-APEX | slowest point ≥ 15 m earlier, minimum not lower, exit lower | "Take the slowest point N m later, where your best run did." | low |
| 12 | AT-LIMIT | lower minimum, grip ≥ 0.92 | positions: "Your best run was N m outside/inside your line at its slowest point, on a wider radius."; else none | low |
| 13 | EARLY-BRAKE (weak) | lower minimum, braked ≥ 10 m earlier, no time before the slowest point | as row 6 | low |
| 14 | UNCLEAR | none of the above | none: the facts only | low |

Owner rules kept: telemetry only, no labels; offs and moments as facts (LOST-SPEED, SLIDE say what was measured,
not why); nothing about the line or the turn-in point without positions (AT-LIMIT and the line fact need them).

### Facts each diagnosis states

The sentence states only the facts that support the diagnosis, in the order a driver meets them, then one fix.

| Code | Facts (keys of `_facts`) |
|---|---|
| OVER-SLOWED | braking point, speed taken off on the brake (against R's), slowest-point speed, grip used |
| OVERSHOT | braking point, turn-in speed, slowest-point speed, apex shift, counter-steer |
| EARLY-BRAKE | braking point, turn-in speed, speed taken off, slowest-point speed |
| SLOW-ARRIVAL | speed before the braking, slowest-point speed |
| SLIDE | counter-steer share against R's, slowest-point speed, exit speed |
| LOST-SPEED | braking (none), slowest-point speed, cornering load, exit speed |
| EXIT-BRAKE | slowest-point speed, braking on the way out (against R's), exit speed |
| LATE-THROTTLE | slowest-point speed, throttle pickup, exit speed |
| COASTING | brake release, the gap with neither pedal |
| GEAR | gear on the way out, exit speed |
| EARLY-APEX | apex shift, exit speed, line (positions) |
| AT-LIMIT | slowest-point speed, grip used, line (positions) |
| UNCLEAR | the first four facts that differ |

Sentence: `On {stage}, {section} ({corner} when the section has several), {loss} s behind {reference} here
({reference text}): you {facts joined}. {fix}` — the same frame as today's `corner.section` tip.

## 5. Thresholds

All marked "calibrate" in code, as the others in `coach_context`.

| Name | Value | Meaning |
|---|---|---|
| BRAKE_SAME | 10 m | braking points closer are the same point (= `coach_context.BRAKE_DELTA`) |
| SPEED_SAME | 3 km/h | speeds closer are the same (= `SPEED_DELTA`) |
| SHED_MORE | 3 km/h | more speed taken off than R: more braking |
| RELEASE_SOONER | 10 m | a release this much later: "come off the brake N m sooner" |
| PEAK_HARDER | 0.10 | pedal peak this much higher: "brake less hard" |
| GRIP_LEFT | 0.85 | grip used below: grip left (≥ 15 %) |
| GRIP_LIMIT | 0.92 | grip used at or above: at the limit |
| GRIP_QUOTE_MIN | 0.30 | `potential.GRIP_QUOTE_MIN`: under it there is no cornering to quote |
| COUNTER_MORE | 0.15 | counter-steer share over R: a slide (= `COUNTER_DELTA`) |
| APEX_SHIFT | 15 m | slowest point moved: earlier/later apex |
| APEX_REACH | 40 m | X's slowest point looked for within this of R's |
| THROTTLE_LATER | 10 m | throttle pickup later |
| COAST_LONGER | 0.3 s | more coasting (= `coach.COAST_LONGER`) |
| EXIT_AT | 50 m | exit speed read this far past R's slowest point |
| APPROACH | 120 m | braking looked for this far before the section's start |
| MERGE_GAP | 8 m | brake applications closer are one braking |
| ZONE_MIN | 3 km/h | a braking must take this off |
| PAIR_REACH | 80 m | X's braking nearest R's within this is the same braking |
| SLOW_ARRIVAL | 5 km/h | slower before either braked: the run-up |
| LOSS_MIN | 0.1 s | (= `SECTION_MIN`) |
| LINE_MIN | 1 m | lateral offset worth saying |

## 6. Validation on marth's runs

### Data and method

- All 21 captures (8 native, 13 Flatpak; read-only) replayed oldest first through the current code (v0.15.0)
  into a scratch store (`research/diagnosis/replay_captures.py`). The replay gives the same runs as the live
  databases (checked against sqlite-backup copies; the live files were never opened for writing). 78 runs, 1714
  corners. The stored potentials (envelope P98) come from the same replay.
- Stages: Afon Bidno - Severn (i20N Rally2: 6 finished clean runs, 187.8-199.7 s, plus 16 restarts and a
  partial) and Sommet de Munster (i20N: 3 finished, 162.0-171.5 s, plus a partial).
- Three comparison sets, deduplicated by (run, reference, section): `coach` (each judged run against the
  reference the coach picks, its stored loss: what the app says), `pairs` (each finished run against every
  earlier quicker one) and `pb` (every run with a trace, restarts included on the sections they covered without
  an off, against the car's quickest clean run). 288 sections: 128 losing ≥ 0.1 s, 117 gaining, 43 the same.
- `research/diagnosis/validate.py STORE out.json` then `research/diagnosis/report.py out`.
- Positions: only the Sommet de Munster runs (v4 captures, Oct 5) have them; the Afon Bidno captures do not, so
  no line fact there.

### Distribution

| Diagnosis | Afon Bidno | Sommet de Munster | All |
|---|---|---|---|
| GAIN | 98 | 21 | 119 |
| SAME | 28 | 6 | 34 |
| OVER-SLOWED | 26 | 10 | 36 |
| SLOW-ARRIVAL | 25 | 3 | 28 |
| SLIDE | 17 | 1 | 18 |
| EARLY-BRAKE | 11 | 3 | 14 |
| OVERSHOT | 12 | 0 | 12 |
| UNCLEAR | 10 | 0 | 10 |
| GEAR | 4 | 1 | 5 |
| EXIT-BRAKE | 4 | 0 | 4 |
| LOST-SPEED | 1 | 3 | 4 |
| AT-LIMIT | 1 | 1 | 2 |
| COASTING | 1 | 1 | 2 |

Over-slowing is marth's most common loss (36 of 128 losing sections, 28 %), then a slow run-up (22 %: the
time belongs to the corner before), then slides (14 %). Only 10 of 128 (8 %) stay UNCLEAR (facts, no fix),
against 33 `unclear` (26 %, "look at the line") and 27 `slower` ("carry more speed", no how) under the old rules.
(The table counts every section's diagnosis; it is made on the corner that lost the most, so 7 sections within
0.1 s overall hold a corner that lost time and carry its code.)

Old pattern → new diagnosis (losing sections): `overdriven` (brake earlier) 18 → OVER-SLOWED 7, OVERSHOT 3,
SLOW-ARRIVAL 3, SLIDE 2, AT-LIMIT 1, LOST-SPEED 1, UNCLEAR 1; `over-slowing` (brake later) 20 → SLOW-ARRIVAL 6,
EARLY-BRAKE 5, OVER-SLOWED 4, SLIDE 2, other 3; `slower` 27 → OVER-SLOWED 14, SLOW-ARRIVAL 7, other 6;
`under-committed` 12 → OVER-SLOWED 6, SLIDE 3, other 3; `over-rotated` 18 → SLIDE 7, OVERSHOT 4, other 7;
`unclear` 33 → 26 with a measured diagnosis.

Confidence (losing sections): high 36, medium 69, low 23.

### Examples: old advice against new

Eleven sections, the first the owner's. "Old" is what `_how` + `_section_action` say (the coach's text after
the place and the reference); "New" is the prototype's sentence (stage and reference text left out).

1. **Afon Bidno, the left-left-right at 2.2 km** (run 57, 199.7 s, against run 51, 198.1 s; 1.74 s lost). Old pattern `overdriven`, new **OVER-SLOWED** (high).
   - Old: "you braked 41 m later, were 14 km/h slower at the slowest point and left 16 km/h slower. Brake 41 m earlier, where your best run did."
   - Old top-3 call there: "The 4 left at 2.1 km: you use 50 % of the grip; about 1.8 s is there. You were in 3rd at the apex where your fastest pass was in 4th: a gear shorter than that costs about 0.2 s."
   - New: "On the left-left-right at 2.2 km (the 4 right at 2.13 km), 1.7 s behind your best run: you braked where your best run did not (24 km/h off), were 24 km/h slower at the slowest point (67 against 90) and used 74 % of the grip there (your best run 81 %). Brake less there, or not at all, as your best run did: carry 24 km/h more through the slowest point. About 26 % of the grip was left there."
   - The next quicker pass (run 59, 1.14 s quicker here) did the old fix: no; the new fix: yes.

2. **Afon Bidno, the 2 left at 1.9 km** (run 57, 199.7 s, against run 56, 198.2 s; 1.43 s lost). Old pattern `under-committed`, new **OVER-SLOWED** (high).
   - Old: "you were 18 km/h slower at the slowest point and left 24 km/h slower. Brake at the same place and carry more speed in: your best run was 18 km/h quicker through the middle."
   - Old top-3 call there: "The 3 left at 1.8 km: you use 53 % of the grip; about 0.9 s is there. You were in 3rd at the apex where your fastest pass was in 4th: a gear shorter than that costs about 0.2 s."
   - New: "On the 2 left at 1.9 km, 1.4 s behind your best run: you braked at the same place, took 78 km/h off on the brake where your best run took 60 km/h, were 18 km/h slower at the slowest point (47 against 65) and used 53 % of the grip there (your best run 74 %). Keep that braking point and brake less: carry 18 km/h more through the slowest point. About 47 % of the grip was left there."
   - The next quicker pass (run 59, 1.88 s quicker here) did the old fix: yes; the new fix: yes.

3. **Sommet de Munster, the 4 right at 0.3 km** (run 63, 171.5 s, against run 66, 162.0 s; 1.46 s lost). Old pattern `slower`, new **OVER-SLOWED** (high).
   - Old: "you were 54 km/h slower at the slowest point and left 39 km/h slower. Carry more speed through it: your best run was 54 km/h quicker at the slowest point."
   - Old top-3 call there: "The 6 right at 0.3 km: about 1.7 s is there, mostly into the bend: brake later."
   - New: "On the 4 right at 0.3 km, 1.5 s behind your best run: you braked where your best run did not (59 km/h off), were 58 km/h slower at the slowest point (63 against 121) and used 52 % of the grip there (your best run 58 %). Brake less there, or not at all, as your best run did: carry 58 km/h more through the slowest point. About 48 % of the grip was left there."
   - The next quicker pass (run 64, 0.90 s quicker here) did the old fix: yes; the new fix: yes.

4. **Sommet de Munster, the right-left at 1.8 km** (run 66, 162.0 s, against run 64, 169.8 s; 0.46 s lost). Old pattern `slower`, new **OVER-SLOWED** (high).
   - Old: "you were 14 km/h slower at the slowest point and left 27 km/h faster. Carry more speed through it: your best run was 14 km/h quicker at the slowest point."
   - Old top-3 call there: "The 1 right at 1.8 km: you use 39 % of the grip; about 2.2 s is there. You were in 1st at the apex where your fastest pass was in 2nd: a gear shorter than that costs about 0.2 s."
   - New: "On the right-left at 1.8 km, 0.5 s behind your best run: you braked 10 m later, took 84 km/h off on the brake where your best run took 70 km/h, were 11 km/h slower at the slowest point (13 against 23) and used 39 % of the grip there (your best run 90 %). Keep that braking point and brake less hard (100 % pedal, your best run 87 %): carry 11 km/h more through the slowest point. About 61 % of the grip was left there."

5. **Afon Bidno, the 2 left at 0.9 km** (run 45, 196.4 s, against run 60, 187.8 s; 1.26 s lost). Old pattern `over-rotated`, new **OVERSHOT** (high).
   - Old: "you were 27 km/h slower at the slowest point and left 19 km/h slower. Rotate the car less on the way in (a smaller flick, a shorter handbrake pull) and get the throttle on sooner."
   - Old top-3 call there: "The 3 right at 0.9 km: you use 70 % of the grip; about 1.8 s is there. You were in 2nd at the apex where your fastest pass was in 3rd: a gear shorter than that costs about 0.2 s."
   - New: "On the 2 left at 0.9 km (the 3 left at 0.86 km), 1.3 s behind your best run: you braked 11 m later, reached the turn-in 9 km/h faster, were 27 km/h slower at the slowest point (47 against 73), had the slowest point 23 m later and steered against the slide for 22 % of the corner (your best run 0 %). Brake 11 m earlier, where your best run did, and turn in at its speed."
   - The next quicker pass (run 46, 0.65 s quicker here) did the old fix: n/a; the new fix: yes.

6. **Afon Bidno, the 3 right at 1.7 km** (run 56, 198.2 s, against run 45, 196.4 s; 0.85 s lost). Old pattern `unclear`, new **EARLY-BRAKE** (medium).
   - Old: "you braked 34 m later, were 4 km/h faster at the slowest point and left 4 km/h faster. The time went before the slowest point: look at where you braked and the line you took in."
   - Old top-3 call there: "The 3 right at 1.6 km: you use 53 % of the grip; about 1.8 s is there. Brake later and carry the speed to the turn-in."
   - New: "On the 3 right at 1.7 km (the 2 right at 1.56 km), 0.9 s behind your best run: you braked 39 m earlier, reached the turn-in 24 km/h slower, took 93 km/h off on the brake where your best run took 65 km/h and were 18 km/h slower at the slowest point (46 against 64). Brake 39 m later and less, as your best run did: carry 18 km/h more through the slowest point."
   - The next quicker pass (run 57, 0.51 s quicker here) did the old fix: n/a; the new fix: yes.

7. **Afon Bidno, the left-left at 2.9 km** (run 45, 196.4 s, against run 60, 187.8 s; 1.04 s lost). Old pattern `overdriven`, new **SLOW-ARRIVAL** (medium).
   - Old: "you braked 70 m later, were 10 km/h slower at the slowest point and left 15 km/h slower. Brake 70 m earlier, where your best run did."
   - Old top-3 call there: "The 3 left at 2.9 km: you use 88 % of the grip; about 1.6 s is there. Get to full throttle sooner after the apex."
   - New: "On the left-left at 2.9 km (the 3 left at 2.96 km), 1.0 s behind your best run: you were 9 km/h slower before the braking and were 3 km/h slower at the slowest point (52 against 56). The time here was lost before the braking: look at the exit of the corner before."
   - The next quicker pass (run 51, 0.65 s quicker here) did the old fix: yes; the new fix: n/a.

8. **Afon Bidno, the 2 left at 1.9 km** (run 57, 199.7 s, against run 51, 198.1 s; 1.33 s lost). Old pattern `over-rotated`, new **SLIDE** (medium).
   - Old: "you were 47 km/h slower at the slowest point and left 37 km/h slower. Rotate the car less on the way in (a smaller flick, a shorter handbrake pull) and get the throttle on sooner."
   - Old top-3 call there: "The 5 left at 1.9 km: about 5.3 s is there. You were in 3rd at the apex where your fastest pass was in 4th: a gear shorter than that costs about 0.2 s."
   - New: "On the 2 left at 1.9 km, 1.3 s behind your best run: you steered against the slide for 53 % of the corner (your best run 0 %), were 27 km/h slower at the slowest point (47 against 73) and were 31 km/h slower 50 m after it. Keep the car straighter through it, as your best run did."
   - The next quicker pass (run 59, 1.24 s quicker here) did the old fix: n/a; the new fix: yes.

9. **Afon Bidno, the 1 right at 2.7 km** (run 56, 198.2 s, against run 60, 187.8 s; 0.90 s lost). Old pattern `unclear`, new **EXIT-BRAKE** (medium).
   - Old: "you left 38 km/h slower. The time went across the whole section: look at the line you took against your best run."
   - Old top-3 call there: "The 1 left at 2.6 km: you use 65 % of the grip; about 0.9 s is there. Brake later and carry the speed to the turn-in."
   - New: "On the 1 right at 2.7 km (the 4 left at 2.76 km), 0.9 s behind your best run: you took the same speed through the slowest point, braked again on the way out (6 km/h off, your best run 0 km/h) and were 9 km/h slower 50 m after it. Keep the speed up after the slowest point: no brake on the way out, as your best run did."
   - The next quicker pass (run 59, 0.71 s quicker here) did the old fix: n/a; the new fix: yes.

10. **Afon Bidno, the left-right-left at 3.4 km** (run 51, 198.1 s, against run 45, 196.4 s; 0.83 s lost). Old pattern `over-rotated`, new **GEAR** (low).
   - Old: "you braked 12 m earlier, were 45 km/h slower at the slowest point and left 16 km/h slower. Rotate the car less on the way in (a smaller flick, a shorter handbrake pull) and get the throttle on sooner."
   - Old top-3 call there: "The 5 left at 3.2 km: you use 41 % of the grip; about 0.8 s is there. You were in 3rd at the apex where your fastest pass was in 4th: a gear shorter than that costs about 0.2 s."
   - New: "On the left-right-left at 3.4 km, 0.8 s behind your best run: you were in 3rd on the way out (your best run 4th). Use 4th out of it, as your best run did."
   - The next quicker pass (run 56, 1.24 s quicker here) did the old fix: n/a; the new fix: yes.

11. **Sommet de Munster, the 1 right at 3.7 km** (run 63, 171.5 s, against run 66, 162.0 s; 1.08 s lost). Old pattern `overdriven`, new **LOST-SPEED** (low).
   - Old: "you braked 57 m later, were 14 km/h slower at the slowest point and left 29 km/h slower. Brake 57 m earlier, where your best run did."
   - Old top-3 call there: "The 2 right at 3.7 km: about 2.3 s is there. You were in 1st at the apex where your fastest pass was in 2nd: a gear shorter than that costs about 0.2 s."
   - New: "On the 1 right at 3.7 km (the 2 left at 3.75 km), 1.1 s behind your best run: you were 16 km/h slower at the slowest point (26 against 42), had hardly any cornering load there (23 % of the grip) and were 5 km/h slower 50 m after it."
   - The next quicker pass (run 64, 0.91 s quicker here) did the old fix: no; the new fix: n/a.

### Sanity checks

1. **Follow-through.** For each losing section, the next later run that was at least 0.1 s quicker through the
   same section (123 cases) was measured against the diagnosed run: did it do what the fix said?

   | | checkable fixes | the quicker pass did it |
   |---|---|---|
   | old advice | 73 | 38 (52 %) |
   | new diagnosis | 82 | 64 (78 %) |
   | both checkable (same 50 sections) | 50 | old 28, new 38 |

   By fix, against the base rate (how often any of the 122 quicker passes did that thing, whatever was said):

   | Fix | said by | followed / said | base rate |
   |---|---|---|---|
   | brake later | old `over-slowing` | 3 / 20 (15 %) | 16 % |
   | brake earlier | old `overdriven` | 6 / 18 (33 %) | 30 % |
   | carry more speed | old `slower`, `under-committed` | 29 / 35 (83 %) | 80 % |
   | brake less, same or later point | new OVER-SLOWED | 31 / 34 (91 %) | 69 % |
   | brake later | new EARLY-BRAKE | 10 / 13 (77 %) | 16 % |
   | brake earlier / arrive slower | new OVERSHOT | 8 / 10 (80 %) | 30 % |
   | straighter (less counter-steer) | new SLIDE | 10 / 17 (59 %) | 32 % |
   | no brake on the exit | new EXIT-BRAKE | 2 / 3 | 16 % |
   | gear | new GEAR | 2 / 3 | 29 % |
   | no coasting | new COASTING | 1 / 2 | 18 % |

   The old braking-point advice did no better than chance (its "earlier/later" came from A2); the new braking
   fixes are followed by the quicker pass 2.7 to 5 times the base rate. "Carry more speed" is always followed
   (a quicker pass usually is faster), which is why it says little.

2. **Contradictions removed.** Of the 18 old "brake earlier" sections, 13 had a lower minimum and 12 of those left
   at least 15 % of the grip at it. The new table never says "brake earlier" with a lower minimum unless the run
   came in faster and it showed (OVERSHOT).
3. **The braking point.** 82 of 197 sections differ by more than 20 m between the old apex-relative measure and
   the absolute onset; in 24 the old one has the wrong side.
4. **Complexes.** In 25 of 67 losing complexes the old "slowest point" speed was more than 5 km/h off the corner
   that lost the time (the owner's case: 14 km/h said, 24 km/h at the corner that lost it).
5. **The top-3 call.** `potential.call` said "brake later" (cause `entry`) at 54 losing places; the diagnosis
   gives another fix at 41 of them.
6. **Synthetic rows.** `research/diagnosis/synthetic.py` builds one run per row on the test suite's road builder
   (`tests.test_coach_context.stage`): SAME, OVER-SLOWED, OVERSHOT, EARLY-BRAKE, LATE-THROTTLE and SLOW-ARRIVAL
   all come out as the table says.

### Limits

- Two stages, one car, nine finished runs: the passes are not independent (the same runs appear in several
  pairs), and the thresholds are tuned on the same data they are checked on. The follow-through is the check
  that does not depend on the thresholds' fit, and it is still marth's own data.
- The envelope is the final one (all runs): the grip percentages are a little higher than the coach would have
  said at the time.
- COASTING, GEAR, EXIT-BRAKE, AT-LIMIT and EARLY-APEX have under five cases each: their thresholds are guesses.
- SLOW-ARRIVAL points at the corner before; the coach should then name that one (the spec does: the previous
  section's own diagnosis is said, not repeated).
- The Afon Bidno captures have no positions, so the line is never said there.

## 7. Implementation spec (for a Sonnet agent; Opus reviews)

Risk tier B: coach logic only; no schema change, no engine, no threading. Port the prototype; do not import from
`research/`.

### New module `oversteer/coach_diagnosis.py`

Port `research/diagnosis/diagnose.py` as is (constants, `_zones`, `_pair_zones`, `Run`, `_grip`, `_counter`,
`_lateral`, `measures`, `measures_section`, `diagnose_section`, `diagnose`, `_facts`, `FACTS`, `_gain`,
`sentence` pieces). Changes from the prototype:

1. `measures(run_arr, ref_arr, sec, env=None)` and `measures_section(...)` keep their signatures. `sec` =
   `{'a', 'b', 'lo', 'floor', 'corners'}` (`key` for one corner). `plan_xy` uses the game of the run
   (`'acr'` hard-coded in the prototype; pass `game` in `sec`, None = no positions).
2. `diagnose(m, loss)` returns `{'code', 'confidence', 'facts', 'fix', 'evidence'}`; `diagnose_section(parts,
   loss)` adds `corner`, `part_loss`, `m`, `parts`.
3. A helper `spans(reference)` in `coach_context` (next to `grid_of`): `[(g, a, b, lo, floor)]` for each
   grid section, the same bounds `section_loss` uses (`section_bounds`, then `b = min(b, exit_end(...))`,
   `lo = max(a - APPROACH, previous apex + 5, first row)`, `floor` = previous section's apex + 5). Make
   `section_loss` use it too so the two can never disagree.

### `oversteer/coach.py`

1. `Coach._stage_place`: after `report`, load the run's and the reference's arrays once
   (`potential.arrays(coach_context.stage_rows(reader.trace(id), course, finished, result_time))`; cache on the
   Coach instance by run id for the call) and the envelope from `potential.stored(reader, stage, car)`
   (`profile['env_bins']`, `['env_lat']`; None without a potential). For each `named` item with a matched grid
   section (`it['ref']` from `section_report`; its `id` is its index in the grid, so in `spans(...)`): `parts =
   measures_section(...)`, `d = diagnose_section(parts, it['loss'])`; store it on the item (`it['diag']`).
2. The `corner.section` tip text: `'On {}, {}, {:.1f} s behind {} here ({}): you {}. {}'.format(name, where,
   loss, ref_name, ref_text, _say(d['facts']), d['fix'] or '')` where `where` is `it['name']`, plus
   `' (' + corner_name(d['corner']) up to ' at' + ' at {:.2f} km)'` when the section has more than one corner.
   No fix: the sentence ends after the facts. Evidence: the existing entry/exit split, plus `'Measured on the
   same metres as your best run: ' + ', '.join(d['evidence'])`.
3. Selection: a SLOW-ARRIVAL section is not named as a tip of its own when the section before it is also in
   `lost` (it is the same time); otherwise it is said with its fix (look at the corner before). UNCLEAR, LOST-SPEED
   and AT-LIMIT without a fix are said only when no other section of the run has a fix (facts are still worth a line
   when nothing else is there), never in the top-3 ahead of a section with a fix.
4. `_patterns`: `throttle.late` counts only sections whose diagnosis is LATE-THROTTLE; `coast.entry` only
   COASTING; `claimed` maps them as today (`'late-throttle'`, plus `'COASTING'`), so a section tip does not repeat
   them.
5. Praise: `corner.entry` (right-entry) and `_best_of`'s "where you ..." use `d['facts']` of a GAIN diagnosis
   (`_gain`) instead of `_how`.
6. Fallback when either trace is missing (evicted by `TRACES_CAP`, or a test without one): keep
   `section_pattern`, with three fixes in `coach_context`:
   - `compare_section['brake']` from the absolute onset: `(ref_lead['d'] - ref_lead['brake_d']) - (lead['d'] -
     lead['brake_d'])` (positive: earlier), so an apex shift is no braking difference (A2);
   - `overdriven` only when the minimum is not lower (`speed > -SPEED_DELTA`); with a lower minimum it is `slower`;
   - `_section_action`: `over-rotated` → "Keep the car straighter through it, as your best run did."; the three
     fallbacks drop "the line you took" ("The time went before the slowest point." / "after" / "across the whole
     section.").
7. `_how` stays for the fallback only.

### `oversteer/potential.py`

1. `call(s, term=None, diag=None)`: when the coach has a diagnosis for the place (`diag`: the section's
   `diagnose_section` against the quickest pass through it, see 2), the how is `diag['fix']` (no fix: the apex
   sentence "The grip allows about N km/h at the apex, you took M." where both are known, else nothing after "about
   X s is there."). Without one:
   - `'entry'`: "Brake later and carry the speed to the turn-in." only when the run's braking onset is at least
     `BRAKE_SAME` before the grip layer's (the last grid point before the apex where `v_grip` stops rising,
     going backwards); when the run's apex speed is under the layer's by `SPEED_SAME` or more, "Brake less: the
     grip allows about N km/h at the apex, you took M."; else the apex sentence.
   - `'exit'`: "Get to full throttle sooner after the apex." only when `exit_throttle` < 0.95; else the apex
     sentence.
   - Bend: the same two checks for "brake later." / "full throttle sooner and longer."
2. `Coach._potential_lead`: for each top-3 row, diagnose the run against the quickest pass through that place
   (`found['best']['who'][split]` from the splits' stitch when `on_splits` named it, else the PB run), unless the
   run is that pass; pass `diag` to `call`. The span is the row's `d0`..`d1` (one stretch per stored corner of the
   quickest pass inside it, `measures_section` with `corners` = that run's stored corners there).
3. `critique` gear term: quote it only when this pass lost time after the slowest point to the quickest pass
   (`m['t_exit'] >= CRITIQUE_MIN`) and the gear differs on the exit, and say the measured exit-time loss rather
   than the constant.

### Data each section needs

The run's and the reference's `potential.arrays` (speed, brake, throttle, gear, rpm, steer, yaw_rate, a_lat, x/y/z
— all in the trace already), the reference's stored corners (windows `d0`/`d1`, `d`, `direction`), the section
bounds (`spans`), the envelope (optional), the section's loss (stored `loss_entry` + `loss_exit`). Nothing new is
stored; the diagnosis is computed when the tips are built (about 1 ms per section on the 1 m grid).

### Tests

New `tests/test_coach_diagnosis.py`, on `tests.test_coach_context.stage` traces (as
`research/diagnosis/synthetic.py`), a flat envelope of 10 m/s²:

1. Each row of the table from a minimal pair: SAME, GAIN (facts, no fix), SLOW-ARRIVAL, OVERSHOT (braked later →
   "Brake N m earlier"; same point, faster turn-in → "Get the speed off"), SLIDE (steer against yaw in the
   window), LOST-SPEED (no fix), OVER-SLOWED in its five hows (no brake → "Lift less"; reference without braking
   → "Brake less there, or not at all"; later release → "come off the brake N m sooner"; harder peak → "brake
   less hard"; else "brake less"), EARLY-BRAKE (three wordings), EXIT-BRAKE, LATE-THROTTLE, COASTING, GEAR,
   EARLY-APEX, AT-LIMIT (no fix without positions; with x/z positions the line sentence), UNCLEAR (no fix).
2. The owner's case as a regression: a three-corner complex where the run brakes 20 m later for the first corner
   and is faster through it, then brakes (95 %, 24 km/h off) for the third where the reference does not; the
   diagnosis is OVER-SLOWED on the third corner, the sentence contains "Brake less" and never "earlier".
3. The braking point is absolute: the same onset with the slowest point 20 m later → "braked at the same place".
4. Zone pairing: a light early braking (40 km/h off) followed by a harder short stab (5 km/h off) → the onset is
   the light braking's.
5. Exit speed is read at a place: two runs with different speeds give `exit_dv` at R's slowest point + 50 m.
6. A grip under 0.30 is never quoted as "of the grip was left".
7. No sentence ever contains "line" without positions, or "handbrake" or "flick".
8. `potential.call` without `diag`: `'entry'` with the run braking later than the layer and a slow apex → "Brake
   less…", not "Brake later"; `'exit'` at full throttle → the apex sentence.

Update `tests/test_coach_places.py` (`test_a_section_that_lost_time_is_named_with_its_place_numbers_and_action`,
`test_each_cause_has_its_own_action`): give both runs traces (`stage()`) for the trace path and assert the new
sentences; keep one test per fallback pattern without traces, with the corrected `overdriven` and actions. Update
`tests/test_potential.py`'s `call` expectations (entry/exit wording per 1). Run the full suite
(`python3 -m pytest -q`).

### Done when

- The full suite passes.
- `research/diagnosis/validate.py` on a fresh replay gives the same distribution with the app's module in place
  of the prototype (swap the import), and the owner's case says OVER-SLOWED.
- An Opus review of the diff.
