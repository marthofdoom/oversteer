# Telemetry and coaching UI: design

Status: design, 2026-10-05. Nothing in it is built yet. The splits chip
(7016bb5) is the starting point for §7.1.

The owner's brief:

- "Format the coaching, and especially the new telemetry tab, exactly how race
  engineers and coaches see live telemetry. It should feel like professional
  racing software."
- "Mobile should get a ton of extra polish too."
- Speedrun-style splits: each split's best and the sum of best, readable at a
  glance in an upper row of the mobile view, using little extra vertical space.
- Two sub-tabs on every surface. **Telemetry** is the professional readout:
  the live dash, distance-aligned traces against the best run, the delta trace,
  g-g and the stage map. **Coaching** is about getting faster: times and
  splits, the debrief, shift points and setup, with as little live data as
  possible on mobile.
- Responsive web comes first (phone, then desktop browser). The GTK tab is the
  counterpart and follows the web design.
- Personal use; Assetto Corsa Rally (ACR) first.

Reference mockup (the style anchor): `docs/design/telemetry-ui/mobile-telemetry.html`.
It covers the phone's Telemetry sub-tab (Live and Run), the splits row, the
bottom sub-tab bar, the stale, offline and finished states, day and night
colours, and portrait and landscape. It is self-contained, so open it in a
browser at 360–412 px wide. Its numbers come from a seeded model of an ACR
Afon Bidno - Severn run in a Hyundai i20N Rally2. The model uses the real
gearing, 7500 rpm limiter and 7000 rpm shift light from
`data/telemetry/cars/acr.json`, and the 5.05 km start-to-flying-finish from
`data/telemetry/stages/acr.json`. No capture went into it. Every other screen
is written below as a spec (§7) that a coding agent can build from, using the
mockup's tokens and components.

---

## 1. What exists today

### 1.1 Surfaces

| Surface | Files | What it shows |
|---|---|---|
| Web page (phone at the rig, a Pixel 4 at 411×869 CSS px, kept awake; desktop browser) | `data/telemetry/web/index.html` (one file, inline CSS and JS), `oversteer/telemetry_web.py` | Top bar with connection, splits and screen chips. Live panel with 15 lights, gear, km/h, rpm, "change up at", stage and progress. Cards for coaching, shift points, setup and recent sessions. Dark only. |
| GTK Telemetry tab | `oversteer/gtk_ui.py` (`_telemetry_overview`, `_telemetry_settings_view`), `oversteer/telemetry_view.py` (pure row and string builders) | Stack switcher with "Car and coaching" and "Settings". The first view has the car bar, a live line, the shift table, coaching rows, sessions and setup. Splits are in an expander (7016bb5). |

Web constraints that every design choice here keeps:

- **The CSP.** It is `default-src 'self'`, with inline `<script>` and `<style>`
  allowed only by sha256 hash. The page therefore has no CDN charting library
  and no webfont: charts are hand-drawn canvas and fonts are system fonts.
- **Read-only.** The server answers GET and HEAD only.
- **One request per connection, at most 8 in flight.** Live polling has to stay
  cheap: today it is 250 ms while data arrives and 1 s otherwise.

### 1.2 Data: live versus after a run

| Data | Live (`/api/v1/live`, every 250 ms) | After the run (database) |
|---|---|---|
| gear, rpm, speed, shift target (learnt or profile), limiter | yes | in the trace |
| throttle, brake | yes | in the trace |
| clutch, handbrake, steer, a_long, a_lat, yaw_rate | **no** (the Sample has them; `live_dict` leaves them out) | in the trace (10 Hz, `TRACE_CHANNELS`) |
| distance along the stage, stage length, progress | yes | in the trace |
| x, y, z (map) | no | in the trace. ACR `pos` is confirmed from 2026-09-27 (bridge fix); older ACR runs have none. |
| time since the start of the run | no (the run clock lives in `RunTracker`) | `t`; `result_time` per run |
| delta to the reference | **no** | derivable from both traces' t(d) |
| corners: entry, min and exit speed, gear_min, brake_d, brake_peak, throttle_on_t, coast, overlap, tightness call, radius, `loss_entry` / `loss_exit` vs the reference, `off` | — | `corners` table |
| sections (linked corners), their times and loss | — | `coach_context.sections_of`, `section_report` |
| splits: per-section last, best, gold, delta; best, possible (sum of best), gain | — | `coach.splits()` (in `/api/v1/coach`, 7016bb5) |
| shifts with rpm, best band, flags (missed, over-rev, cut, launch) | — | `shifts` table |
| events: limiter, off, stop, stall, spin, hit, launch, spread | — | `events` table |
| coaching | — | `Tip.to_dict()`: `id`, `kind` (focus, tip, praise, still, note, technique; `setup` and `driving` come from tuning), `text`, `evidence`, `value` |

Section tips and other place-tied tips carry their place in the tip id only:
`corner.section:<stage>:<d m>`, `limiter.held:<stage>:<d0>`,
`corner.spin:<stage>:<d0>`. `corner.off` and `technique:held` ids hold a
bucket, not metres. §9 adds structured fields so the UI does not have to parse
ids.

### 1.3 Channel availability per game (what the UI may show)

ACR is the priority, and every channel shown for it must already be in the
data. This is from `coaching_matrix.game_channels('acr')`:

| Channel | ACR | Note for the UI |
|---|---|---|
| speed, rpm, gear, throttle, brake, clutch, steer | confirmed | Steer sign is +1 = left once Oversteer flips ACR's −1 = left. Clutch 1 = engaged in ACR is decoded as 1 − x. |
| a_long, a_lat (accG), yaw_rate | confirmed | g-g is fine. Accel is kinematic, so 0 at rest. |
| handbrake | **absent in ACR**; read from the rig (`rig_handbrake`) | Show it as "HB (rig)" and dim the strip when the rig has none. |
| position x, y, z | confirmed from the bridge fix only | Without position, the stage map falls back to a distance ribbon (§7.3). |
| stage time | derived (Oversteer's run clock, flying finish `finish_m`) | Label it "time" and never "official". The finish split also holds the slow-down to the stop where `finish_m` is unknown. |
| slip_drive, wheel slip | **absent** from the ACR trace (no tyre radius on the trace path) | Never draw a slip strip for ACR. Hide the channel and do not show it empty. |
| susp_rms | decoded, sign unchecked | Not shown in v1. |
| surface grip, puddles, tyre temperatures | absent | Not shown. |

The UI reads availability from data and is not hard-coded: a strip whose
channel is all NaN for the run is not drawn, and its row in the channel picker
says "not sent by <game>".

---

## 2. What pro tools do (research)

Sources were read on 2026-10-05. Items marked *(convention)* are common
practice that I could not pin to a primary page. They are used as defaults,
not cited as fact.

**Data analysis (MoTeC i2, AiM, Pi/Cosworth, Atlas, WinDarab)**

- Laps are compared **by distance, overlaid**, with one main lap and one
  overlay lap. MoTeC i2 switches the graph's x axis to metres (F9) so "you will
  see the comparison at the exact same place of the track".
  [MoTeC forum: time or distance](https://forum.motec.com.au/viewtopic.php?f=26&t=3664&p=19300)
- **Variance (delta time)** is the time difference at the same distance between
  the main lap and the overlay lap, as a running gain or loss through the lap.
  Its derivative (time gained or lost per metre) colours a "rainbow" track map
  of where time was made and lost.
  [MoTeC forum: variance channel](https://forum.motec.com.au/viewtopic.php?f=26&t=1231),
  [MoTeC forum: gain/loss track map](https://forum.motec.com.au/viewtopic.php?f=26&t=3697&start=10),
  [i2 1.1.4 feature guide](https://www.motec.com.au/hessian/uploads/i2_V1_1_4_Feature_Guide_0d03b00fa8.pdf)
- Worksheets stack channel strips on one x axis with **one shared cursor**, and
  the readout gives every channel's value at the cursor for both laps. Gear is
  a step trace, and the scatter (mixture) plot of lateral against longitudinal
  g is the g-g diagram. *(convention)*
- Positive variance means **slower** than the reference, the same as AiM.
  *(convention: a search summary, not confirmed on a primary page)*

**Sim coaching tools**

- Reference against current: the reference lap is drawn **washed out** behind a
  bright current trace, throttle is **green** and brake is **red**, plotted by
  track position. [irdashies lap-trace widget, PR #731](https://github.com/tariknz/irdashies/pull/731)
- Coach Dave Delta shows your lap and the reference in two fixed colours,
  coaching insights pinned to their place on the trace, and an overview of
  where the lap separates from the reference. Fixes are ordered so the
  **earliest mistake in a sequence comes first**, because mistakes compound.
  Its phrasing is concrete and comparative: "you are braking 30 m earlier than
  the reference lap"; "brake later, turn in earlier, tighten the exit".
  [Coach Dave Academy](https://coachdaveacademy.com/tutorials/save-seconds-in-lap-time-using-coach-dave-delta/)
- Corner views in Garage 61-style tools put each lap's brake point and full
  throttle point in metres around the apex.
  [garage61-session-analysis PR #43](https://github.com/botchkin2/garage61-session-analysis/pull/43)
- Track Titan splits time lost into corner **entry** and **exit**, and Trophi
  gives per-corner "brake a few metres earlier or later". Oversteer's
  `loss_entry` and `loss_exit` are the same split.
  [Startup Mag](https://www.startupmag.co.uk/funding/track-titan-2025-seed-funding/),
  [Sim Racing Manual](https://simracingmanual.com/resources/ai-driving-coaches/)
- A caution: "brake later into turn three" reads like a diagnosis when it is
  only a measured difference. A call must say what was measured.
  [GitGud Racing](https://gitgudracingacademy.com/guides/ai-sim-racing-coach/)

**Speedrun splits (LiveSplit)**

- The columns are split name, delta, split time and segment time. **Sum of Best
  Segments** is the perfect run, **Possible Time Save** is a segment against its
  best, and **Previous Segment** turns into "Live Segment" while the current
  segment runs past its best. [LiveSplit components](http://livesplit.org/components/)
- The colour rule, read from the source, is in
  [`LiveSplitStateHelper.GetSplitColor`](https://github.com/LiveSplit/LiveSplit/blob/master/src/LiveSplit.Core/Model/LiveSplitStateHelper.cs):
  - Gold for a best segment.
  - Otherwise green when ahead of the comparison and red when behind.
  - The darker shade when the split gained time against the comparison, the
    lighter shade when it lost time.
- Defaults from
  [`StandardLayoutSettingsFactory.cs`](https://github.com/LiveSplit/LiveSplit/blob/master/src/LiveSplit.Core/Options/SettingsFactories/StandardLayoutSettingsFactory.cs):
  ahead-gaining `#29CC54`, ahead-losing `#70CC89`, behind-gaining `#CC7870`,
  behind-losing `#CC3729`, best segment `#D8AF1F`, PB `#16A6FF`, not running
  `#7A7A7A`.

**Dashes and shift lights**

- Race dashes stage their lights green, then yellow, then red, and **flash** at
  the shift point because drivers react faster to flashing. A blue final stage
  is common. [Rennlist thread](https://rennlist.com/forums/data-acquisition-and-analysis-for-racing-and-de/1186658-input-from-the-group-shift-lights.html),
  [MoTeC C127 manual (16 RGB LEDs)](https://www.motec.com.au/hessian/uploads/C127_User_Manual_da85a94ff5.pdf).
  The page's 15 lights already do this, and the mockup keeps them.
- Engineer radio calls put the **number first**, are tied to a place, and give
  one fix: "Turn 4, a tenth on entry, brake later". Debriefs open with the
  biggest loss. *(convention)*

**Conventions adopted**

- The x axis is distance.
- One cursor runs through every strip.
- The reference is drawn grey and thin; this run in the channel colour, thicker.
- Delta: positive = slower = red.
- Throttle green, brake red, gear as a step trace.
- Tabular monospace numerals on a near-black background, with hairline grids.
- Splits are LiveSplit's columns and colours.
- Calls lead with the number and the place.

---

## 3. Information architecture

### 3.1 Sub-tabs

- **Coaching**: "how do I get faster". It covers times (the splits row, the
  last run, the trend), the debrief, shift points and setup. Live data shrinks
  to the connection chip, plus one "LIVE · 3rd · 2.4/5.1 km" line on desktop
  only.
- **Telemetry**: "what is the car doing". It has two views:
  - **Live**: the dash, the delta to PB, rolling pedal and steer strips, g-g
    and the position on the stage.
  - **Run**: distance-aligned strips against a comparison, the delta trace,
    the stage map by delta, where the time went, splits, and g-g.
- **Settings** (GTK only, as today). The web page stays read-only and has none.

The **splits row** sits above both sub-tabs on mobile. It is shared because it
answers "how am I doing on this stage" from either tab.

### 3.2 Navigation per surface

| Surface | Sub-tab control | Telemetry Live/Run | Notes |
|---|---|---|---|
| Phone portrait (< 700 px) | Bottom tab bar, 58 px plus the safe area, two items with icon and label | Segmented control at the top of the Telemetry tab | Thumb reach. A badge on Coaching counts new calls since it was last opened (stored per viewer in `localStorage`). |
| Phone landscape (height ≤ 520 px) | Left rail, 64 px | Compact segmented control | The dash and the strips sit side by side. |
| Tablet / desktop web (≥ 1000 px) | Tabs in the top bar, centred | Segmented control in the Telemetry header | Wide layouts are in §7.6 and §7.7. |
| GTK | The existing `Gtk.StackSwitcher`: **Coaching · Telemetry · Settings** (replaces "Car and coaching") | `Gtk.StackSwitcher` in the Telemetry header | §7.8 and §7.9 |

**Deep links** use the hash so a refresh or a shared link restores the view:
`#coaching`, `#telemetry/live`, `#telemetry/run/<run id>?d=<m>&cmp=pb|prev|<run id>`.

**Automatic switching.** On mobile, opening the page with telemetry arriving
lands on Telemetry › Live, otherwise on Coaching. When a run finishes, Live
shows the finished card (§7.2) and does not switch tabs under the user's
thumb. The user's last explicit choice wins for the rest of the session.

### 3.3 Cross-links

- A debrief call opens Telemetry › Run zoomed to that place
  (`#telemetry/run/<id>?d=<m>`).
- A corner-table row and a map tap zoom the strips to that section.
- A split row in the splits sheet opens the Run view at that split.
- The finished card on Live has "Open the run" and "Debrief" buttons.

---

## 4. Visual system

### 4.1 Tokens

These are the mockup's `:root` values. Dark is the default and the design
target. Light ("day") exists for a sunny room or a laptop. It is a manual
toggle kept in `localStorage`; it does not follow `prefers-color-scheme`,
because the phone sits beside a screen.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bg` | `#0a0c0f` | `#eef0f3` | page |
| `--panel` | `#11151a` | `#ffffff` | panels |
| `--raised` | `#171c22` | `#f5f6f8` | selected and pressed |
| `--line` | `#232a33` | `#d5dae1` | borders, strip baselines |
| `--grid` | `#1a2028` | `#e6e9ee` | gridlines, alternate section bands |
| `--text` / `--dim` / `--faint` | `#eef2f6` / `#8c96a3` / `#56606c` | `#0e1116` / `#566170` / `#8a94a1` | ink |
| `--accent` | `#4c9dff` | `#1f6fd6` | selection, active tab |
| `--speed` | `#4cc3ff` | `#0a74b8` | speed (this run) |
| `--thr` | `#2fd27a` | `#128a47` | throttle |
| `--brk` | `#ff4d4d` | `#d42a2a` | brake |
| `--steer` | `#b58cff` | `#7446d0` | steering |
| `--rpm` | `#ffb020` | `#b06d00` | rpm, shift band |
| `--gear` | `#e8edf2` | `#1b2129` | gear step |
| `--clutch` (spec) | `#5fc4c4` | `#167b7b` | clutch |
| `--hb` (spec) | `#ff8a3d` | `#c05a14` | handbrake (rig) |
| `--ref` | `#8c96a3` | `#8a94a1` | the comparison run, every channel |
| `--slower` / `--faster` | `#ff5a4e` / `#2fd27a` | `#d42a2a` / `#128a47` | delta, loss/gain |
| `--neutral` | `#3a424d` | `#c9cfd7` | the map's "no difference" |
| `--ls-*` | LiveSplit defaults (§2) | darkened for white | split colours |
| `--led-g/y/r/b` | `#2fd27a` / `#ffc21a` / `#ff4d4d` / `#4c9dff` | darker steps | shift lights |

**Validation.** The channel set (speed, throttle, brake, steer, rpm) was run
through the dataviz palette validator against `#11151a`:

- CVD separation passes; the worst adjacent pair is brake against throttle at
  ΔE 8.3 (deuteranopia).
- The normal-vision floor passes.
- Contrast is at least 3:1 everywhere.
- The lightness band "fails" on purpose. These are 1.5–1.75 px lines on near
  black, where pro tools use bright ink, and every strip is labelled with its
  channel name. The label is the secondary encoding, so colour is never the
  only cue.

Red against green for slower against faster is the domain's language and is
kept. Every delta is also **signed** (+ or the true minus "−") and placed above
or below a zero line, so it never depends on hue alone.

### 4.2 This run against the comparison

- **This run**: the channel's colour, 1.75 px.
- **Comparison**: `--ref` grey, 1–1.25 px, 80–90 % opacity, the same for every
  channel. The legend says "Run 5 · PB".
- A third run is not overlaid in v1, because more than two traces at phone
  width is noise. On desktop, a "+ add run" option allows up to three
  comparisons, each in grey with its own dash pattern. The dash patterns are
  only for data lines, never for gridlines.

### 4.3 Type

- **Numbers**: `ui-monospace, "JetBrains Mono", "SF Mono", "Cascadia Mono",
  "Roboto Mono", Menlo, Consolas, "DejaVu Sans Mono"` with
  `font-variant-numeric: tabular-nums slashed-zero`. No webfont, per the CSP.
- **Words**: `system-ui`.
- **Labels**: 10.5 px, uppercase, `letter-spacing: .09em`, weight 600, `--dim`.
  This is the "engineering label" look.
- **Scale (phone)**:

  | Element | Size |
  |---|---|
  | Gear | `clamp(104px, 31vw, 150px)`, weight 800 |
  | Speed | `clamp(46px, 14vw, 64px)` |
  | rpm | 26–34 px |
  | Live delta | 40 px |
  | Run time | 24 px |
  | Table numbers | 13 px |
  | Body | 15 px |
  | Smallest text anywhere | 11 px |

- **Formats**:

  | Quantity | Format |
  |---|---|
  | Times | `m:ss.s` (3:07.9). Tenths, because the coach's own precision is 0.1 s and ACR's clock is Oversteer's. |
  | Live delta | two decimals (+1.03), the delta-bar convention |
  | Split and loss deltas | one decimal |
  | Distance | `2.31 km` in readouts; `0.8 / 5.0 km` in progress |
  | Speed | km/h, integer |
  | rpm | integer to 10 rpm live; `6.1k` in compact readouts |
  | Brake-point differences | whole metres, + = earlier than the reference |
  | Steer | % of lock, + = left |
  | Minus sign | the true minus "−", never a hyphen |

### 4.4 Components (all in the mockup unless marked spec)

- **Top bar** (48 px plus the safe area). A connection chip:
  - green dot "ACR · live";
  - amber "paused 6 s";
  - red "Oversteer not answering";
  - grey "between stages".

  Then the title, a day/night icon button, and the keep-awake icon button
  (sun: green when kept on, accent when a tap is needed, struck through when
  impossible). Every icon button is 44×44.
- **Splits row** (40 px). Line 1: stage short name · `PB 3:06.0` · `SoB 3:04.0`
  · right-aligned now-value. Live the now-value is "split/total delta", e.g.
  "3/17 +1.03"; after a run it is "Last 3:07.9 +1.9". Line 2 is the **ribbon**:
  a 7 px bar with one cell per split, its width proportional to the split's
  length. Cells are coloured by the LiveSplit rule against the PB, the current
  cell pulses, and future cells are `--ls-none`. Tapping the row drops the
  **splits sheet**: a table with Split · Last · Δ PB (★ on gold) · Best · Save
  and a Stage total row. It replaces today's chip and dropdown and keeps their
  `localStorage` open state.
- **Panel**: radius 10, 1 px `--line`, 10 px padding. A panel header holds a
  label left and a meta label right.
- **Segmented control**: 40 px, with a selected pill.
- **Shift lights**: 15 LEDs. The rule is the existing one, the first at 60 % of
  the change-up point, green 7, yellow 5, red 3, all flashing `--led-b` past
  it.
- **Delta block**: big signed number, a ±2 s centre-zero bar, and a meta line
  with "vs PB 3:06.0 · split 3 +0.05" and "finish ≈ 3:07.0" (PB plus the
  current delta).
- **Rolling strip** (live): 12 s window, newest at the right edge, filled 18 %
  under the line.
- **Strip stack** (run): one canvas with a 16 px section-number lane, then the
  strips. Each strip has a gutter label and scale ticks in `--faint`, and a
  6 px gap between strips. Alternate sections get a `--grid` band. The cursor
  is a 1 px `--text` line through every strip.
- **Readout** (sticky above the strips): "2.31 km · split 8 · L-L" and the
  delta at the cursor. Below that, a 3×2 grid of this-run / comparison pairs:
  km/h, thr, brk, gear, rpm, steer.
- **Zoom bar**: ◀ · All · section name · ▶, with 44 px targets.
- **Stage map**: the road is a 5 px line coloured per section by the delta
  (diverging; opacity by size, saturating at 0.6 s; grey under 0.05 s). Labels
  appear only on sections losing at least 0.25 s, plus start, finish and the
  cursor dot.
- **Loss table**: rows of 40 px. Split (name, km) · Loss (a bar ∝ loss plus the
  number) · Min · Exit · Brake.
- **g-g**: rings at 0.5 g and 1 g, axes labelled ACC and BRK. Live it shows a
  3 s fading trail and a dot; for a run it shows a scatter of this run against
  the reference.
- **Banner**: an amber or red strip at the top of Live for stale or offline.
- **Finished card**: replaces the dash after a finish, with Open the run and
  Debrief buttons.
- **Call row** (spec, §8): number-first debrief line.
- **Trend sparkline** (spec, §7.1): the last N times on the stage, with the PB
  line.

---

## 5. Motion and refresh

- Live numbers **never animate**. They are set on each sample, because a
  tweened speed lies.
- The only animations allowed:
  - the view change, a 140 ms fade;
  - the current ribbon cell pulse, 1 s;
  - the shift-light flash, 5 Hz.

  Under `prefers-reduced-motion` all three are off.
- Canvases redraw on new data (10 Hz live) and on cursor movement (pointer
  events, coalesced to `requestAnimationFrame`). They are not redrawn while
  the tab is hidden (`document.hidden`), as polling already pauses today.
- Every canvas is sized by `devicePixelRatio`, capped at 3. Pixel 4 is 2.625.

---

## 6. Mobile polish checklist (applies to every phone screen)

1. **Touch**:
   - every target is at least 44×44 CSS px;
   - table rows are 40 px with the whole row tappable;
   - the strips take one-finger horizontal drag for the cursor (vertical
     scroll still passes through: `touch-action: pan-y`);
   - double-tap zooms to the section and double-tap again shows the whole
     stage;
   - pinch zoom on the strips is spec only (phase 2): a two-pointer distance
     ratio drives the x range, clamped to 100 m to the whole stage.
2. **Thumb reach**: the sub-tabs are at the bottom; Live/Run is at the top but
   also swipeable (spec: a horizontal swipe on the Live dash goes to Run).
3. **Safe areas**: `viewport-fit=cover`, and `env(safe-area-inset-*)` on the
   top bar, main, bottom bar and the landscape rail.
4. **Landscape** (height ≤ 520): the rail replaces the bottom bar, Live becomes
   two columns (dash | strips and g-g), and the gear is `clamp(80px, 30vh,
   120px)`.
5. **States**: live, stale (no sample for 2 s or more: amber chip "paused N s",
   banner, last values dimmed to 38 % and desaturated), offline (red chip,
   banner "retrying every 2 s · last data hh:mm:ss"), between stages (finished
   card), and empty: no runs yet, no PB, no position for the map (§7.3). Every
   empty state is one sentence that says what will fill it.
6. **Keep-awake**: the sun button replaces the text chip. Its states (on via
   wake lock, on via video, tap needed, not possible) and its title text stay
   as today. On Pixel 4 Chrome the wake lock works through the insecure-origin
   flag.
7. **Scrolling**: the top bar and splits row are sticky (88 px together). The
   Run readout is sticky beneath them, so the cursor values stay in view while
   the strips scroll. No horizontal page scroll at 360 px.
8. **Performance**: one canvas per strip stack. Traces are decimated to about
   1.5 samples per pixel for the whole-stage view and loaded at full resolution
   only when zoomed (§9). Live costs one request per 250 ms (§9.1), unchanged.
9. **Offline-friendly**: the last good `/api/v1/coach` and run payloads are
   kept in memory (not storage), so a dropped Wi-Fi keeps the debrief readable
   with the stale banner.

---

## 7. Screens

Each spec gives the layout top to bottom, the data source per element, and
the behaviour. Pixel sizes are CSS px at 411 wide unless stated.

### 7.1 Mobile · Coaching (at a glance) — spec

The goal is one screen that answers "how was that, and what do I change next
run", readable between stages without scrolling for the first answer.

1. **Top bar and splits row**, shared (§4.4).
2. **Last run panel** (about 120 px):
   - Line 1: `3:07.9` (24 px mono) on the left; `+1.9` vs PB on the right in
     `--slower`, or `−0.4 PB!` in `--ls-gold` when it is a new PB.
   - Line 2 (dim): "Run 7 · clean · 3 gold splits · Afon Bidno - Severn ·
     i20N Rally2".
   - **Trend sparkline**, 36 px tall, full width. One dot per finished run on
     this stage and car, last 12, oldest left. The y axis is time with lower
     at the top. A `--ls-gold` dashed line marks the PB and a dim dashed line
     the SoB. The newest dot is in `--text` with a 2 px ring. Tapping a dot
     opens that run in Telemetry › Run.
   - Data: the new `/api/v1/runs?car=&stage=&limit=12` (§9.3).
3. **Debrief panel** "Engineer's debrief · Run 7 vs PB", with the call rows of
   §8:
   - The focus first (one, if any), then up to **3** place calls ranked by
     `cost`.
   - Then "Good" (praise, up to 2), "Technique" (up to 2), and "Notes" behind a
     disclosure.
   - "Also noted (n)" collapses the still lines, as today.
4. **Shift points panel**, compact. Columns are Change · Best · You · Δ, one
   row per gear change at 40 px. "You" is red when it is more than 300 rpm
   early. There is no live gear highlight on mobile Coaching, because live data
   stays off this tab. A footer gives the limiter and the source ("from the
   game's engine data"). The full table, with per-method columns and bands,
   sits behind "Details".
5. **Setup panel**, collapsed to one line ("Gearing set 0 · brake bias 64 % ·
   first seen 3 Oct") with tuning notes behind a disclosure.
6. **Sessions**, collapsed: the last 5 as two-line rows (stage, date · class ·
   time), with evidence behind a disclosure. This is the existing content in
   less space.

The first viewport (869 px minus the bars, about 720 px) shows the splits row,
the last run with its trend, and at least the focus and the first two calls.

### 7.2 Mobile · Telemetry › Live — built in the mockup

Order by glance priority (the mockup is the reference):

1. Banner (stale or offline), only when needed.
2. **Dash panel**:
   - lights (16 px);
   - gear on the left and speed, rpm and "change up at 7000 learnt" on the
     right; the gear goes red at the change-up point;
   - **delta block**: delta to PB at this distance, the ±2 s bar, the current
     split's delta, and the predicted finish.
3. **Pedals · last 12 s** (64 px: throttle and brake), then **Steering** (44 px,
   L up). Spec, when the rig reads them: a clutch line (`--clutch`, thin) in
   the pedal strip, and a handbrake strip (24 px, `--hb`) only while
   `rig_handbrake` reads.
4. **g-g** and **Stage** side by side. The stage minimap shows the driven part
   in `--dim`, the rest in `--line`, the car as a dot, and "0.8 / 5.0 km".
5. After the finish, the **finished card** replaces the dash: time, Δ PB, "PB
   3:06.0 · SoB 3:04.0 · 3 gold splits · 3 calls in the debrief", and the
   buttons Open the run and Debrief.

Data: `/api/v1/live?since=<seq>`, which returns the sample plus the 10 Hz rows
since `seq`, the delta, and the split (§9.1). Without a PB on this stage and
car the delta block reads "No PB yet on this stage: finish a clean run",
dimmed, and the splits row shows only "Run time 1:12.4".

### 7.3 Mobile · Telemetry › Run — built in the mockup

1. **Run header**: time (24 px), Δ against the comparison; "Run 7 · clean · 3
   gold splits"; "vs Run 5 3:06.0". Comparison chips: **vs PB** (the coach's
   reference run, `coach_context.reference_run`, so the traces and the debrief
   agree), **vs previous**, and **vs sum of best** (spec, phase 2: a stitched
   ghost from `coach_context.stitched`; its traces jump at split boundaries,
   which the legend says). A run picker (spec) is a bottom sheet listing the
   stage's runs with time, class and date.
2. **Readout** (sticky).
3. **Strip stack**: Δ s (56) · km/h (84) · pedal (56, throttle and brake) ·
   steer (44) · gear (40) · rpm (56, shift band shaded 7000–7500, the 7.5k
   limiter label). That is 336 px plus the lane.
   - The view **opens zoomed to the biggest loss**, because that is where the
     debrief points. All is one tap.
   - Channel picker (spec, phase 2): a "Channels" chip opens a sheet of
     toggles. Defaults are the six above. Optional: clutch, handbrake (rig),
     yaw rate, a_lat, a_long. Channels the game does not send show "not sent
     by ACR" and are disabled.
4. **Stage map**, coloured by time per section against the comparison. Tapping
   the map moves the cursor to the nearest point (spec).
   - **No position**: older ACR runs and any run without x/y fall back to a
     **distance ribbon**, a 24 px horizontal bar from start to finish with the
     same section colours, labelled "No position for this run: the map
     needs runs after the 2026-09-27 bridge fix".
5. **Where the time went**: the top 8 splits by loss, with Min, Exit (km/h
   against the comparison) and Brake (m, + earlier). Tapping a row zooms the
   strips and scrolls the readout into view. "Show all" lists every split.
6. **g-g · whole run**: a scatter of this run against the comparison.
7. Spec, phase 2: an **Events** lane under the section lane, with small glyphs
   at their d. "L" is limiter-held (amber), "▲" an off or spin (red), "S" a
   stall. Shifts appear as ticks on the gear strip. A missed or over-rev shift
   (`flags`) gets a red tick.

### 7.4 Mobile states — built in the mockup (Live)

The MOCK button in the mockup toggles Live, Stale, Offline and Finished. Run
and Coaching follow the same states: stale and offline keep the last payload
and show the banner; empty states are one dim sentence in the panel.

### 7.5 Mobile landscape — built in the mockup

At ≤ 520 px tall, the rail is on the left and Live is two columns: the dash on
the left; the pedal and steer strips, g-g and the minimap on the right. Run in
landscape (spec) gives the strip stack the full width with strip heights × 0.8,
puts the readout in a 160 px column on the right, and puts the map and table
below.

### 7.6 Desktop web · Telemetry — spec (≥ 1000 px; max content width 1440)

- **Header bar** (56 px): Oversteer · tabs **Coaching | Telemetry** (centred) ·
  connection chip · splits summary (`PB 3:06.0 · SoB 3:04.0 · Last +1.9`) ·
  day/night · screen. The splits **ribbon** runs full width under the header,
  10 px tall, with split numbers on hover.
- **Telemetry › Live** (three columns, 320 | flexible | 340):
  - Left: the dash panel at phone size (lights, gear 140 px, speed, rpm, the
    delta block).
  - Centre: rolling strips over **30 s** (speed, pedal, steer, gear, rpm),
    stacked as in Run but with time on x. The current split's reference trace
    is drawn grey, aligned by distance and projected onto time.
  - Right: g-g (300 px), the minimap (300 px) and the current split table (the
    five splits around the current one).
- **Telemetry › Run** (two columns: strips flexible, side 400):
  - Left: run header and comparison chips; readout as a single row; the strip
    stack at **1.6×** phone heights, plus the events lane and the shift ticks.
  - Interaction:
    - the mouse moves the cursor;
    - the wheel zooms around the cursor;
    - a drag on the section lane pans;
    - keys: ← → move 10 m (Shift: 100 m); + − zoom; 0 shows All; [ and ]
      step to the previous and next split.
  - Right column: the stage map (400×320) with the cursor; Where the time went
    (all splits, sortable by loss, km or name); the splits table (LiveSplit
    columns); g-g.
  - The cursor, the map and the tables stay linked both ways.

### 7.7 Desktop web · Coaching — spec

Two columns (flexible | 420):

- Left: Last run panel with the trend chart at **120 px** tall (axes labelled,
  PB and SoB lines labelled); the **debrief** with every call expanded, each
  with a 120 px **micro-trace**. The micro-trace is a mini strip stack of
  speed plus pedals for that section, this run against the comparison, so the
  call is backed by its trace. Then praise, technique and notes.
- Right: the splits table (always open, not a sheet); shift points (the full
  table with per-method columns and bands); setup; sessions (last 10, with
  evidence).
- A **live line** at the top of the left column, only while live: "● LIVE ·
  3rd · 130 km/h · 2.4 / 5.0 km · +1.03". Clicking it opens Telemetry › Live.
  This is the "somewhat more" the owner allows on desktop Coaching.

### 7.8 GTK · Telemetry — spec

The GTK tab follows the desktop web layout using native widgets. Drawing is
plain **cairo on `Gtk.DrawingArea`**, not matplotlib: the app already ships
matplotlib (`combined_chart.py`), but redrawing a figure at 10 Hz with a shared
cursor is too slow, and cairo code can mirror the web canvas code one to one.

- Stack switcher: **Coaching · Telemetry · Settings**. The current "Car and
  coaching" content is split between the first two.
- **Telemetry header**: a car combo (as today) · a `Gtk.StackSwitcher` with
  Live | Run · a run combo with comparison radio buttons (PB / Previous) when
  on Run.
- **Live**: a `Gtk.Paned`.
  - The left pane is a `DrawingArea` dash: lights, gear, speed, rpm and the
    delta block. It uses the same tokens, read from `main.css` custom
    properties as GTK named colours.
  - The right pane is the rolling strips `DrawingArea`, with g-g and the
    minimap beneath.
  - Data comes in process from the listener (no HTTP): the same
    `live_dict(...)`, plus the ring buffer of §9.1.
- **Run**: a strip `DrawingArea` (keys as on desktop web), then a right column
  with the map `DrawingArea`, a `Gtk.TreeView` for Where the time went
  (sortable), and the splits `Gtk.TreeView` with LiveSplit colours as cell
  foregrounds.
- New module **`oversteer/telemetry_plot.py`**: pure cairo drawing functions
  `strips(cr, w, h, runs, view, cursor, sections)`, `stage_map(...)`,
  `gg(...)`, `rolling(...)`, `dash(...)`. They take plain data and are testable
  by rendering to an `ImageSurface` in pytest and checking pixels at known
  points. `telemetry_view.py` gains the data shaping (`run_strips`,
  `loss_rows`, `split_rows`).
- Theme: follow the GTK theme's dark preference. The channel colours are fixed
  per mode (§4.1).

### 7.9 GTK · Coaching — spec

This is today's "Car and coaching" reordered to the web Coaching order:

1. car bar;
2. live line (as today);
3. **last run** row with time, Δ PB and a 48 px trend `DrawingArea`;
4. the **splits** (the expander from 7016bb5, open by default, as a TreeView
   with LiveSplit colours);
5. the **debrief** list (§8 rows, each with a "Show in Telemetry" button that
   switches the stack and zooms);
6. shift points;
7. sessions;
8. setup.

---

## 8. Coaching as an engineer's debrief

### 8.1 Call grammar

Each place-tied tip becomes a **call row** (about 64 px on the phone):

```
+0.8  ·  0.51 km  ·  split 2  ·  R-R            ▸
Brake 14 m later, carry 6 km/h more through the slowest point.
```

- Line 1 is mono, 13 px. In order:
  - the **cost** (signed, `--slower`), first and boldest;
  - the place (`km`, split number, the section's call name);
  - a chevron that opens Telemetry › Run at `d`.
- Line 2 is 15 px words: **one imperative, measured fix**, made from the
  section comparison the coach already computes (`compare_section`: brake m,
  min and exit km/h, throttle s), at most two clauses.
- Tapping the row (not the chevron) expands the coach's full sentence (`text`)
  and its `evidence` ("0.6 s before the slowest point and 0.2 s after").
- Kinds and their tags (11 px uppercase, outlined):

  | Kind | Tag |
  |---|---|
  | focus | **FOCUS** (`--brk`) |
  | tip | **CALL** (`--accent`) |
  | praise | **GOOD** (`--thr`) |
  | technique | **TECHNIQUE** (`--steer`) |
  | note | **NOTE** (`--dim`) |
  | setup | **SETUP** (`--rpm`) |

  A praise row's line 1 shows the gain in `--faster`, with "−0.3" first.
- Calls that are not tied to a place (shift habits, launches) keep line 1 as
  the measure: "−900 rpm · 2→3 · 12 changes". Line 2 is again the fix.
- No call states a cause it did not measure. "You braked 14 m earlier" is
  measured. "You were scared of the crest" never appears, as GitGud cautions
  (§2).

### 8.2 Ranking

The rows are ordered as follows:

1. The focus, which is the habit (the coach's own choice).
2. Place calls by `cost`, highest first. Ties go to the **earlier** place,
   because an early mistake compounds (Coach Dave).
3. Praise.
4. Technique.
5. Notes.

The coach already ranks and caps them (`SECTION_TIPS`, `worth()`). The UI
never re-ranks beyond this order; it only groups.

### 8.3 Data needed

`to_dict()` gains structured fields (§9.4) so the UI does not parse ids or
sentences. Until then, the web UI can recover `d` from the id
(`corner.section:<stage>:<d>`) and show the full sentence as line 2.

---

## 9. Data and API additions (`oversteer/telemetry_web.py`)

All of these are GET, read-only, and JSON with `_clean` as today. The sizes
assume an ACR 5 km stage.

### 9.1 Live: `GET /api/v1/live?since=<seq>` (extends the current endpoint)

```json
{ "seq": 18342, "gear": 4, "rpm": 6190, "speed": 36.1, "shift_rpm": 7000, "learnt": true, "limiter": 7500,
  "throttle": 0.98, "brake": 0, "clutch": 1, "steer": 0.04, "a_lat": 0.21, "a_long": 0.12, "yaw_rate": 0.05,
  "handbrake": 0, "distance": 812.4, "stage_length": 5050, "progress": 0.16, "run_t": 38.2,
  "delta": 1.03, "ref_run": 4123,
  "split": {"i": 2, "n": 17, "cum": 1.03, "seg": 0.05},
  "rows": [[38.0, 806.0, 36.0, 6170, 4, 0.97, 0, 1, 0.04, 0.12, 0.20, 0.05], ...],
  "row_channels": ["t", "distance", "speed", "rpm", "gear", "throttle", "brake", "clutch", "steer", "a_long", "a_lat", "yaw_rate"],
  "stage": "acr:...", "track": "Wales Afon Bidno", "car": "acr/...", "game": "acr" }
```

- `rows` are the 10 Hz rows since `seq`, at most 3 s of them, from a **ring
  buffer of the last 30 s** that the listener fills as it already builds trace
  rows for `drive_log`.
- `delta` comes from the reference run's t(d), loaded once when the run is
  matched to a stage (`drive_log` already loads the reference trace for its
  coaching), and looked up with `bisect` per sample: O(log n) at 10 Hz.
- `split.i` comes from the split bounds (§9.2).
- The ring buffer and the reference load cross the listener thread and the
  web threads, which makes them **tier A (threading)**. They are authored by
  Opus with an immutable snapshot published per sample, as `telemetry.live`
  is today.

### 9.2 Splits: extend `coach.splits()`

- Add `d0` and `d1` (m) per split, from `best['bounds']`.
- Add `apex` per split.
- Add `ref_run` (the id).
- Add the per-run split times of the last 12 runs for the trend (or leave the
  trend to §9.3).

The live split index and the ribbon need the bounds.

### 9.3 Runs

- `GET /api/v1/runs?car=<id>&stage=<key>&limit=12` gives
  `[{id, n, started, result_time, finished, run_class, wet, golds}]` for the
  trend and the run picker.
- `GET /api/v1/runs/<id>` gives the run row, the `corners` (with
  `loss_entry`/`loss_exit`), the sections (`section_report` against the
  reference: name, d, loss, entry/exit split, the compare dict), the `events`,
  the `shifts` (d, gear, gear_to, rpm, flags), the splits for this run, and
  `ref_run`. This is about 20 KB.
- `GET /api/v1/runs/<id>/trace?ref=<id|pb|prev>&step=<m>&d0=&d1=&channels=`
  returns columns on a common **distance grid**. The trace is resampled from
  10 Hz to `step` metres; the default is 5 m for the whole stage, and 1 m when
  zoomed to under 600 m. The response holds `t` for both runs (the delta comes
  from it) and every requested channel for both runs, rounded to 3
  significant digits. A whole stage is about 1000 points × 8 channels × 2 runs,
  roughly 90 KB uncompressed. The server has no gzip today, so keep the 5 m
  default.
- `GET /api/v1/runs/<id>/map?step=10` gives the x/z polyline (or `null`
  without position).

### 9.4 Coaching

`Tip.to_dict()` adds the following fields, all optional:

- `cost` (s);
- `place: {stage, d, d0, d1, split, name}`;
- `call` (the short fix line, built where the sentence is built:
  `_section_action` and `_how` already hold the parts);
- `cause` (the pattern: `late-brake`, `early-brake`, `low-min`,
  `late-throttle`, `right-entry`, and so on);
- `run` and `ref_run`.

`/api/v1/coach` adds `last_run` ({id, n, result_time, delta_pb, golds,
run_class}).

### 9.5 Page size and CSP

The page grows from about 600 to about 2000 lines. While it is under about
150 KB it stays one file, which keeps the CSP hashing as it is. Past that,
serve `/app.js` and `/app.css` from the same origin (`'self'` already allows
them), still with no third parties.

---

## 10. Implementation plan

The owner's order is used. Every phase ends with tests green, an Opus review
of the diff, and a phone check on the Pixel 4. Roles follow the global rules:
Sonnet writes the code from these specs, Opus authors the tier-A parts
(marked) and reviews every diff.

### Phase 1: Splits row and the sub-tab shell (web)

- Files:
  - `data/telemetry/web/index.html`: tokens (§4.1); the bottom bar / rail /
    top tabs; Coaching and Telemetry sections, with the existing cards moved
    into Coaching and the live panel into Telemetry › Live as-is; the splits
    row with its ribbon and sheet, replacing the chip; hash routing.
  - `oversteer/coach.py`: bounds in `splits()` (§9.2).
  - `tests/test_telemetry_web.py`.
- API: §9.2 only.
- Risks: `localStorage` keys change (keep `splitsOpen`); the sticky stack
  height on small phones; the landscape rail and the existing landscape media
  query. Tests should cover splits being `null`, a single split, and every
  split gold.

### Phase 2: Post-run analysis (Telemetry › Run, web)

- Files: `index.html` (strip stack, readout, zoom, map or ribbon, loss table,
  g-g, run picker); `oversteer/telemetry_web.py` (§9.3);
  `oversteer/telemetry_store.py` (a reader helper to resample a trace to a
  distance grid, pure, testable); tests.
- API: §9.3.
- Risks:
  - **Alignment**: traces must be cut with `coach_context.stage_rows` (course,
    finish) exactly as the coach cuts them, or the delta and the coach's losses
    disagree. Test it: the trace delta at each split end equals `splits()`.
  - Payload size: keep the 5 m default.
  - Runs without position (fallback ribbon).
  - Runs re-timed by the flying-finish backfill (44d39c6): the trace endpoint
    must use the new `course`.

### Phase 3: Live view (web, then shared)

- Files:
  - `oversteer/telemetry.py` / `drive_log.py`: the ring buffer, the reference
    t(d), and the split index (**tier A: threading, Opus**).
  - `telemetry_web.py`: `live_dict` gains the fields and `?since=`.
  - `index.html`: dash and delta block, rolling strips, g-g, minimap, states,
    finished card.
- API: §9.1.
- Risks:
  - Thread safety: publish immutable snapshots only.
  - Polling cost: still one request per 250 ms.
  - The delta's meaning before the reference is known: hide it.
  - ACR's first packets have no car, track or max rpm; the delta stays hidden
    until the stage is matched.
  - The stale detector threshold (2 s), against game pauses.

### Phase 4: Coaching debrief (web)

- Files:
  - `oversteer/coach.py`: structured fields in `Tip` and `to_dict`; `call`
    built beside the sentence.
  - `index.html`: call rows, the trend sparkline, the compact shift table,
    collapsed setup and sessions.
  - `telemetry_web.py`: `/api/v1/runs` for the trend; `last_run` in coach.
- Risks:
  - The `call` and the full sentence must agree. Generate both from one
    `compare` dict, and test that the numbers in `call` appear in `text`.
  - The `coach_state` "still" logic must be unchanged; the web stays
    read-only.

### Phase 5: GTK counterpart

- Files:
  - `oversteer/telemetry_plot.py` (new, cairo);
  - `oversteer/telemetry_view.py` (data shaping);
  - `oversteer/gtk_ui.py`: stack becomes Coaching / Telemetry / Settings;
    Telemetry Live and Run; Coaching reorder;
  - `oversteer/main.css` (named colours);
  - `oversteer/gui.py` / the controller: a 10 Hz redraw timer only while the
    tab is visible.
  - Tests render to an `ImageSurface`.
- Risks:
  - GTK main-loop cost at 10 Hz: draw only the strips' damaged region and
    stop the timer when hidden.
  - Theme colours under light GTK themes.
  - `gtk_ui.py` size (about 2000 lines): put the new views in a new
    `oversteer/telemetry_tab.py` rather than growing it.

**Desktop web (§7.6, §7.7)** lands in phases 2–4 as the ≥ 1000 px layout of
each view. It is CSS grid over the same components, not a separate page.

---

## 11. Open questions for the owner

1. **Split granularity.** Splits today are the coach's sections (runs of linked
   corners): one to two dozen on a 5 km stage (17 in the mockup's model). Should the headline numbers also use
   the game's **3 sectors** (`sectors_km`, e.g. 1.8 / 1.7 / 1.6 km for Afon
   Bidno)? That would read like a WRC timing screen: S1, S2, S3 cells in the
   row and sections in the ribbon. It needs the sector lengths mapped onto
   spline distance, which is unverified.
2. **Comparison default.** Should the default be **PB** (the coach's reference,
   so the traces and the debrief agree) or **previous run** (what changed since
   the last attempt)? The design uses PB.
3. **Day mode.** Should there be a light theme at all (the current page is dark
   only), and should it follow the phone's system setting or stay a manual
   toggle as designed?
4. **Live delta on the phone during a stage.** It is a distraction risk: the
   phone is glanced at, not read. Keep the big ±0.00, or show only the bar
   colour while moving and the number when stopped?
5. **Sum-of-best ghost** as a comparison: is a stitched ghost with jumps at
   split boundaries useful, or confusing?
6. **Rig channels.** Should clutch and handbrake (from the wheel, not the
   game) appear in Telemetry for ACR by default, or only on request?
7. **Units.** km/h and metres throughout, or an mph option?
8. **GTK priority.** Is the GTK Telemetry view worth phase 5 at all for
   personal use, given the desktop web page does the same in a browser on the
   rig?

## Owner additions (2026-10-05)

- **Where the time went → coach advice.** Tapping a row in the "where the time went" table (and, with a mouse, hovering it) shows the coach's advice for that section next to the row: the place-tied tips (`corner.section:*`, `corner.entry:*`, `corner.best*`), praise and technique notes whose place falls in the section, in the debrief style (time · place · fix). The same on the stage map and on the strips: the section under the cursor shows its advice in the readout. Needs the structured tip fields from phase 4 (`place` with section/distance, `cost`, `call`) brought forward into phase 2, so the UI matches tips to sections without parsing tip ids. GTK: the same on row selection and pointer motion over the table/strips.
- Decisions: compare against PB by default; S1–S3 sectors shown alongside the corner sections; big ±0.00 live delta plus bar; GTK built together with web in every phase.

## Phase 3 live API (built; tier A backend, 2026-10-05)

The backend of §9.1 as built. The UI (web and GTK) is written on this and
nothing else; it never reads the database for the live view.

### Where it lives

- `oversteer/live_buffer.py`: `LiveBuffer` (the ring and the delta),
  `Reference`, `load_reference()`.
- One buffer per app: `ShiftLearner.live_run`. Fed by `drive_log.RunTracker`
  (start of a run, each 10 Hz trace row, the finish, the end); the reference
  is loaded by `RunTracker._live_reference` on the drive-log thread.
- Web: `GET /api/v1/live?since=<seq>[&limit=<rows>]`. **Without `since` the
  endpoint answers exactly as before** (the dash dict or `null`), so the
  current page keeps working until it moves over.
- GTK: `self.shift_learner.live_run.read(since)` from the main thread, in the
  existing once-a-second refresh or a faster `GLib.timeout_add` of its own
  (250 ms is fine). `read()` is lock-free and cheap; no `GLib.idle_add`
  hand-off is needed because nothing is pushed to GTK. Never call GTK from
  the listener.

### Response

```json
{ "seq": 18342, "first": 17001, "reset": false,
  "state": "live",
  "run": {"n": 12, "id": 4188},
  "game": "acr",
  "stage": {"key": "acr:greece:elatia", "name": "Elatia", "length": 4900.0},
  "t": 38.2, "distance": 812.4, "age": 0.04,
  "delta": 1.03, "predicted": 244.25,
  "split":  {"index": 2, "n": 21, "name": "the 1 right at 0.2 km", "delta": 0.05,
             "prev": {"index": 1, "name": "the 4 right at 0.0 km", "delta": 0.31}},
  "sector": {"index": 0, "n": 3, "name": "S1", "delta": 0.4, "prev": null},
  "final": null,
  "ref_status": "ready",
  "ref": {"run": 4123, "time": 243.22, "course": 6498.6,
          "splits": [{"name": "the 4 right at 0.0 km", "d0": 0.35, "d1": 102.2}, ...],
          "sectors": [{"name": "S1", "d0": 0.0, "d1": 1650.0}, ...]},
  "channels": ["t", "distance", "speed", "rpm", "gear", "throttle", "brake", "clutch", "handbrake", "steer",
               "a_long", "a_lat", "yaw_rate", "x", "y", "z", "delta"],
  "samples": [[38.1, 810.4, 36.0, 6170.0, 4, 0.97, 0.0, 1.0, null, 0.04, 0.12, 0.2, 0.05, null, null, null, 1.02], ...],
  "dash": { ...live_dict as before: gear, rpm, speed, shift_rpm, learnt, limiter, ... } }
```

Field by field:

| Field | Meaning |
|---|---|
| `seq` | The last row's sequence number. Counts from 1 for the life of the Oversteer process, across runs; never goes back. Poll with `since=<the seq you last got>`. |
| `first` | The first seq of the run shown. Rows before it belong to an earlier run and are never returned. |
| `reset` | `true` when `since` was greater than `seq` (Oversteer restarted under an open page): the client must drop what it holds; the response carries the rows from the start of the run. |
| `state` | `idle` (no run: menus, standing at the start, after a restart or a non-finish end), `live` (a run, a row within the last 2 s), `stale` (a run, no row for over 2 s: the game paused or stopped sending, a loading screen), `finished` (the run crossed the finish; kept until the next run starts, never stale). |
| `run` | `{n, id}`: `n` is the run's number in this process (changes on every new run: **clear the strips when it changes**), `id` the database `runs.id` once the drive-log thread wrote it (else `null`). `null` when idle. |
| `stage` | `{key, name, length}`; before the drive-log thread matched the stage: the game's own key/track name. `null` when idle or unknown. `length` is the stage table's published length, not the course along the trace. |
| `t`, `distance` | The run's clock (s from the start, as the trace's `t`) and distance along the stage (m from the start line, as the trace's `distance`) at the last row. |
| `age` | s since the last row (the stale timer); `null` when idle. |
| `delta` | s behind (+) or ahead (−) of the reference at the car's distance: `t_now − t_ref(d)`, `t_ref` interpolated on the reference's trace exactly as the coach times sections (`coach_context.time_at`). `null` with no reference, before the reference's first row, or past its finish. At `finished` it is the final delta. |
| `predicted` | `ref.time + delta`; `null` when `delta` is. |
| `split` | Where the car is among the reference's grid sections (the bounds `coach.splits()` times: `grid_of(reference)`; §9.2's `d0`/`d1` are the same numbers), or `null` with no reference or no corners on it. `index` is in **grid order** (0 = the launch section). Note `coach.splits()['splits']` leaves out the launch and off sections, so match the ribbon by distance (`ref.splits[i].d0/d1`) or by name, not by list position. `delta` is what this split has lost so far (s, + = slower); `null` for a split the car joined part way (the reference arrived late, a reset along the road). `prev` is the split last completed, with its loss: flash it on change. `index` is `null` before the first split and past the last. |
| `sector` | The same over the game's sectors (S1…), from `stage_tables.sector_bounds` (from the start line); `null` where the stage has none placed (e.g. Greece Elatia today). |
| `final` | `{time, delta}` once the run crossed the finish: the run's own clock to the line (ACR: the flying finish `finish_m`, else the last pace note; other games: progress ≥ 0.99 with the game's stage clock) and that less `ref.time`. `delta` is `null` with no reference. |
| `ref_status` | `pending` (looking it up, or ACR's track name not here yet), `ready`, `none` (no clean or learning finished run of this car on this stage, no trace, or no stage known). **Hide the delta unless `ready`.** |
| `ref` | `{run, time, course, splits, sectors}` of the reference (`runs.id`, its result time, where it finished along its trace, and the bounds). Constant for the run: cache it by `ref.run`. |
| `channels`, `samples` | Rows after `since` (at most `limit`, default and max 300 = the 30 s ring; the newest are kept), oldest first, of the current run only. One row per 10 Hz trace row RunTracker keeps (so none while the game is paused and the tracker drops rows). Values are rounded to 3 decimals; unknown is `null` (ACR: `handbrake`, and `x/y/z` before the bridge fix). The last column is that row's `delta` (the delta strip). |
| `dash` | `live_dict()` as the endpoint without `since` returns it (or `null`): the gear, rpm, shift point and lights for the dash at packet rate. |

### Client loop (web)

1. `since = 0`. Poll every 250 ms while `state` is `live`, 1 s otherwise.
2. On a response: if `reset` or `run.n` differs from the one held, clear the
   strips. Append `samples`; keep the last 300. `since = seq`.
3. Show the delta block only when `ref_status == 'ready'` and `delta` is not
   null. `finished`: show the finished card from `final`.
4. The first poll may return up to 300 rows (~37 KB); later ones 2–3 rows.

### Threads and guarantees (tier A)

- **One writer.** Only RunTracker writes the buffer, from the listener
  thread, under the learner's lock (the same lock that already serialises
  RunTracker). Each write is constant time plus a bisect into the reference
  trace (O(log n) at 10 Hz). Writers are guarded: a fault in the live view is
  logged once a minute and never breaks run tracking.
- **The reference is read off the hot path.** `RunTracker._start` posts
  `_live_reference` to the drive-log queue right after `_write_start` (FIFO,
  so the run's row and stage exist); for ACR it is posted again when the
  track name first arrives (a no-op once looked up). It reads the database
  once per run (`stage_runs`, `trace`, `corners`, `stage`), builds an
  immutable `Reference` and hands it over with a single attribute store
  (`offer_reference`). The listener adopts it on its next row; an offer for
  another run number is ignored. If the drive-log queue is full the offer
  never comes and the run stays `pending` (no delta): never a block.
- **Readers take no lock.** The listener publishes a fresh state dict per
  row with one attribute store and never mutates a published one; ring slots
  are immutable `(seq, row)` tuples stored with one list-item assignment.
  `read()` takes the state once, then the slots up to that state's `seq`,
  skipping any slot whose seq no longer matches (overwritten: the reader was
  a whole ring behind). So a reader never sees a torn row, rows out of order,
  rows of another run, or a state that disagrees with its rows. Both
  assignments are atomic in CPython with and without the GIL. Tested with
  one writer at full speed and four polling readers.
- **Per request:** no database, no lock, O(rows returned).
- **Selection** is the coach's: `coach_context.reference_run` over the car's
  `clean` and `learning` runs of the stage (excluding the run itself), so the
  live delta, `coach.splits()` and the drive log's section losses all use the
  same run. The run's wetness isn't known at its start, so any run compares
  (the post-run coach filters by wetness).
- **Known limits.** ACR sends no pause flag: in a pause that keeps sending,
  rows go on with speed 0 (`live`, delta growing) as RunTracker's clock does;
  only a pause that stops the packets reads `stale`. A run that starts part
  way along a stage has no meaningful delta (its distance is from where it
  started), as in the coach.
