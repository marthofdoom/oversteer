# Logitech G29 — Windows parity matrix

Goal for 1.0.0: a G29 under Oversteer + new-lg4ff is **equal or better than Windows**
(Logitech G HUB, and the older Logitech Gaming Software where G HUB dropped features).

Status legend: ✅ done · ⚠️ partial · ❌ missing · ➕ beyond Windows

Where a feature is implemented: **driver** = new-lg4ff sysfs/kernel, **app** = Oversteer.

## Wheel

| Feature | G HUB | LGS | Oversteer + new-lg4ff | Where | Notes |
|---|---|---|---|---|---|
| Operating range 40–900° | ✅ | ✅ | ✅ `range` | driver | |
| Steering sensitivity curve (0–100, 50 = linear) | ✅ | ✅ | ❌ | driver | apply in `lg4ff_adjust_input_event` on ABS_X; expose `sensitivity` sysfs |
| Centering spring on/off + strength | ✅ | ✅ | ✅ `autocenter` 0–100 | driver | 0 = off |
| Persistent centering spring (survives game `FF_AUTOCENTER 0`) | – | ✅ (registry `PersistentCenteringSpring`) | ❌ | driver | sysfs store and app share `set_autocenter`; add `autocenter_persistent` |
| Overall effects strength | – (dropped) | ✅ 0–100 | ✅ `gain` (master) | driver | |
| Overall effects strength > 100% | – | ✅ up to 150 in older LGS | ❌ | driver | issue new-lg4ff#29 |
| Spring effect strength | – | ✅ | ✅ `spring_level` | driver | |
| Damper effect strength | – | ✅ | ✅ `damper_level` | driver | |
| Friction effect strength | – | – | ➕ `friction_level` | driver | |
| Enable/disable force feedback | – | ✅ | ⚠️ gain 0 | app | add explicit toggle |
| Allow game to adjust settings (app `FF_GAIN` honoured) | – | ✅ | ❌ always honoured | driver | add `app_gain` toggle: ignore app gain when off |
| Wheel LEDs as FFB clipping meter | – | – | ➕ `ffb_leds` | driver | |
| Wheel LEDs driven by game telemetry | SDK only | SDK only | ❌ | app | LED class exists; needs telemetry source (SimHub-like) |
| Change range from wheel buttons | – | – | ➕ `use_buttons` | app | |
| Range / FFB overlay | – | – | ➕ | app | |
| PS4-mode wheel (046d:c260) | ✅ | ✅ | ❌ | driver | new-lg4ff#123 |

## Force feedback engine

| Feature | Windows driver | new-lg4ff | Notes |
|---|---|---|---|
| Constant / ramp / periodic (sine, square, triangle, saw) | ✅ | ✅ software-rendered into slot 0 | |
| Spring / damper / friction | ✅ | ✅ hardware slots 1–3 | |
| Inertia | ✅ | ❌ advertised, not rendered | new-lg4ff task |
| Several simultaneous condition effects of the same type | ✅ | ❌ one hardware slot per type; extra effects lost | dynamic slot allocation task |
| Effect envelopes (attack / fade) | ✅ | ✅ | |
| Effect count | 16+ | `LG4FF_MAX_EFFECTS` (16) | |
| Update rate | fixed | `timer_msecs` (default 2 ms), adaptive | ➕ tunable |
| Latency profiling | – | ➕ `profile=1` module param | |

## Pedals

| Feature | G HUB | LGS | Oversteer + new-lg4ff | Where |
|---|---|---|---|---|
| Combine pedals (brake + accelerator on one axis) | ✅ | ✅ | ✅ `combine_pedals` (also clutch+accel) ➕ | driver |
| Pedal sensitivity / response curve | ✅ | ✅ | ❌ | driver or app |
| Invert pedal axes | – | – | ❌ (PR new-lg4ff#120) | driver |
| Pedal deadzone / range of motion | – | – | ❌ (oversteer#273) | app |

## Software / workflow

| Feature | G HUB | Oversteer | Where |
|---|---|---|---|
| Per-game profiles, auto-applied when the game runs | ✅ | ⚠️ profiles + "run as companion"; PR #343 adds process detection | app |
| Assignments: wheel button → keyboard key / macro | ✅ | ❌ | app (uinput keyboard) |
| Live input test / visualiser | ✅ | ✅ | app |
| FFB latency / performance tests + charts | – | ➕ | app |
| Firmware update | ✅ | out of scope | – |
| Multi-device: shifter + handbrake appear as one wheel to the game | ✅ (shifter plugs into wheel) | ❌ (proxy framework, parked) | app |

## Order of work

1. **G HUB parity (app-visible features)** — sensitivity, persistent centering spring,
   allow-game-to-adjust, FFB on/off, pedal curves/invert, button assignments, per-game auto profiles.
2. **Driver parity** — inertia, dynamic slots, gain > 100 %, PS4 mode.
3. **Proxy devices** — combined virtual wheel, handbrake identity (FH6 Device-1 fix).
