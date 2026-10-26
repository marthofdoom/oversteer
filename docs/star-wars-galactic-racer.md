# STAR WARS: Galactic Racer: G29 as the game's controller

Research and setup, 2026-10-26. Game: Steam app 4078430, build 26133805, Proton Experimental
11.0-100 (`experimental-11.0-20261014`), Steam Linux Runtime 3.0 (sniper) 3.0.20261021.
**Untested in the game so far.** Nothing here was run against the game; section 7 is the first test.

The game has no wheel support and its controls cannot be rebound. It does read a controller,
with this default set ([Prima Games][prima], via search summary; the page refuses direct fetches):

| Pad | Action |
|---|---|
| Left stick | Steer (X), pitch (Y) |
| Right stick | Camera |
| RT / LT | Accelerate / brake |
| A | Boost (menu: confirm) |
| R3 | Toggle boost mode |
| X | Advanced Maneuver |
| B | Critical Malfunction Recovery (menu: back) |
| LB | Activate Ability |
| RB | Change camera |
| D-pad | Select ability 1-4 |
| Menu / View | Pause / View Build |

No source lists Y or a dedicated drift button. One guide describes drifting as feathering
the brake with the throttle held ([playnews][drift]).

So the plan is to make Proton present the G29 to the game as an XInput controller, using the
G29's own identity, with no new device.

## 1. Launch options (paste into Steam: Properties > General > Launch Options)

```
SDL_JOYSTICK_HIDAPI=0 SDL_JOYSTICK_WHEEL_DEVICES_EXCLUDED=0x046d/0xc24f SDL_GAMECONTROLLERCONFIG="030000006d0400004fc2000011010000,Logitech G29 Driving Force Racing Wheel,platform:Linux,a:b0,b:b2,x:b1,y:b3,back:b8,start:b9,leftshoulder:b5,rightshoulder:b4,leftstick:b11,rightstick:b10,dpup:h0.1,dpright:h0.2,dpdown:h0.4,dpleft:h0.8,leftx:a0,righttrigger:a2,lefttrigger:a3," %command%
```

**Steam Input: Disabled** for this game (Properties > Controller > "Disable Steam Input"),
as Oversteer already advises for wheel games. With it disabled Steam has no reason to put
the pad or the wheel on its `SDL_GAMECONTROLLER_IGNORE_DEVICES` list. The launch-options
prefix also replaces any `SDL_GAMECONTROLLERCONFIG` that Steam sets: `steamui.so` does set
one, and the prefix wins because it is applied last.

**Oversteer profile:** load "Star Wars Galactic Racer" before starting the game. The trigger
mapping assumes `invert_pedals = 7` (section 4).

What each part does:

- `SDL_JOYSTICK_HIDAPI=0`: SDL 3.4 has a HIDAPI Logitech-wheel driver (`SDL_JOYSTICK_HIDAPI_LG4FF`).
  It must not claim the G29 and so hide its evdev device. All hidraw nodes are root-only on
  this rig anyway. This is the same switch as Oversteer's `WHEEL_OPTIONS`.
- `SDL_JOYSTICK_WHEEL_DEVICES_EXCLUDED=0x046d/0xc24f`: **required**. Without it the mapping
  is ignored (section 2).
- `SDL_GAMECONTROLLERCONFIG=...`: the mapping (section 3).

## 2. Why it works: the source trail

Proton's winebus (ValveSoftware/wine `experimental_11.0` and `proton_11.0`,
`dlls/winebus.sys/bus_sdl.c`):

1. `sdl_bus_init` dlopens `libSDL2-2.0.so.0`. The installed `winebus.so` names exactly that
   soname. In sniper that library is **sdl2-compat 2.32.74 on SDL3 3.4.18**
   (`manifest.dpkg`: `libsdl2-2.0-0 2.32.74+ds-1~steamrt3.1`, `libsdl3-0 3.4.18`). The
   classic SDL2 2.32.10 is present only as `sdl2-classic/`.
2. After `SDL_Init`, winebus reads `getenv("SDL_GAMECONTROLLERCONFIG")` and passes it to
   `SDL_GameControllerAddMapping`. When the variable is set, the registry `Map` mappings are
   skipped. The string is in the shipped `winebus.so`.
3. `sdl_add_device` opens a device as a game controller only if
   `options->map_controllers && SDL_IsGameController(index) && joystick_type != SDL_JOYSTICK_TYPE_WHEEL && != FLIGHT_STICK`.
   `Map Controllers` defaults to on.
4. For a controller, `desc.is_gamepad = TRUE` with 6 axes. `main.c`'s `get_compatible_ids`
   then adds `WINEBUS\WINE_COMP_XINPUT`, so winexinput turns it into an **XInput gamepad**.
   VID/PID/version stay the device's own (`SDL_JoystickGetVendor/Product/ProductVersion`).
   The product string is `SDL_GameControllerName`, which is the name in the mapping.
5. SDL3's `SDL_GetJoystickType` returns `WHEEL` for any VID/PID in `wheel_devices`, and
   `046d:c24f` ("Logitech G29 (PS3)") is in that list (SDL `release-3.4.x`,
   `src/joystick/SDL_joystick.c`). That trips the check in step 3. The list honours
   `SDL_JOYSTICK_WHEEL_DEVICES_EXCLUDED` ("overrides ... the built in device list"; hints
   fall back to the environment). With the exclusion the type becomes `UNKNOWN`, and then
   `GAMEPAD` because a mapping exists. **This is why the exclusion is required.**
6. Rumble: winebus drives an XInput vibration as `SDL_JoystickRumble`. On Linux, SDL uploads
   that as an `FF_RUMBLE` evdev effect. new-lg4ff 0.8.0 (installed) declares `FF_RUMBLE` and
   renders it as a 30/60 Hz wheel vibration scaled by `rumble_level`
   (`hid-lg4ff.c: lg4ff_calculate_rumble`). The proxy forwards FF to the wheel.

**GUID.** SDL's Linux evdev GUID is `bus, crc16(name), vendor, 0, product, 0, version,
driver signature 0, driver data 0`, little-endian 16-bit fields:
`03 00 | crc | 6d 04 | 00 00 | 4f c2 | 00 00 | 11 01 | 00 | 00`. When matching, SDL
**zeroes the CRC** of the device GUID ("the mappings never include it in the GUID",
`SDL_PrivateMatchGamepadMappingForGUID`). A mapping with CRC 0 and no `crc:` field
therefore matches, and on a version mismatch SDL retries without the version. Hence
`030000006d0400004fc2000011010000`.

**Which device the game actually sees.** The real G29 (`event20`) is `root 0600`, hidden
and grabbed by `oversteer.proxy.daemon` (`oversteer-proxy.service`, spec `combined-wheel`).
Games see the proxy's combined wheel, `event257` (`/sys/devices/virtual/input/input40`,
phys `py-evdev-uinput`). That device has the G29's passthrough identity (name, `046d:c24f`,
version `0111`, USB), so it has the same GUID, and the mapping applies to whichever of the
two is visible. Its layout is the G29's plus extras, so every index used below means the
same on both:

- **Axes:** a0 `ABS_X` wheel, a1 `ABS_Y` clutch, a2 `ABS_Z` throttle, a3 `ABS_RZ` brake;
  on the combined device only, a4 `ABS_THROTTLE` handbrake.
- **Buttons:** b0-b15 are `BTN_TRIGGER`..`BTN_DEAD` (0x120-0x12f), then b16+ are
  `BTN_TRIGGER_HAPPY1`.. (SDL counts `BTN_JOYSTICK..KEY_MAX` first, then anything below).
- **Hat:** h0 is `ABS_HAT0X/Y`.

Source: sysfs `capabilities/{abs,key}` only.

**Trigger semantics** (`SDL_PrivateParseGamepadElement`, `HandleJoystickAxis`):

- A plain `aN` on a trigger maps the full input range -32768..32767 linearly onto 0..32767.
  The pedal's rest value must therefore sit at the axis minimum.
- `aN~` swaps the input ends (inverted).
- `+aN` / `-aN` take half an input axis. A `+`/`-` before the output name (`-lefty:a1`)
  drives half an output axis.
- One input drives one output: the first matching binding wins. Bindings are kept
  one-to-one.

## 3. Button table

| G29 control | evdev | SDL | Pad | Game action |
|---|---|---|---|---|
| Wheel | `ABS_X` | a0 | left stick X | steer |
| Throttle pedal | `ABS_Z` | a2 | RT | accelerate |
| Brake pedal | `ABS_RZ` | a3 | LT | brake / feather for drift |
| Cross (X) | `BTN_TRIGGER` | b0 | A | boost, menu confirm |
| Square | `BTN_THUMB` | b1 | X | Advanced Maneuver |
| Circle | `BTN_THUMB2` | b2 | B | Critical Malfunction Recovery, menu back |
| Triangle | `BTN_TOP` | b3 | Y | (none known; menus) |
| Left paddle | `BTN_PINKIE` | b5 | LB | Activate Ability |
| Right paddle | `BTN_TOP2` | b4 | RB | Change camera |
| Share | `BTN_BASE3` | b8 | View | View Build |
| Options | `BTN_BASE4` | b9 | Menu | Pause |
| R3 | `BTN_BASE5` | b10 | R3 | Toggle boost mode |
| L3 | `BTN_BASE6` | b11 | L3 | (none known) |
| D-pad | `ABS_HAT0X/Y` | h0 | D-pad | abilities 1-4, menus |

Paddle codes come from `oversteer/proxy/equipment.py`
(`LOGITECH_PADDLE_CODES = BTN_TOP2` right, `BTN_PINKIE` left).

Ability is on the left paddle because it is a racing action you fire with hands on the rim.
The right paddle carries the bumper the game uses for camera.

Left unmapped:

- R2/L2 buttons, +/-, the dial, Enter, PS, and the shifter gears.
- The clutch, the handbrake, pitch (left Y) and the right stick.

**Optional:** pitch on the clutch pedal. Add `-lefty:a1,` to the mapping (clutch = stick up).
Use `+lefty:a1,` if the nose goes the wrong way. Only try this once you know what pitch does
in a race.

## 4. Oversteer profile "Star Wars Galactic Racer"

File: `~/.var/app/io.github.berarma.Oversteer/config/oversteer/profiles/Star Wars Galactic Racer.ini`.
It has the same keys as marth's "Star Wars.ini", which is unchanged.

| Key | Value | Why |
|---|---|---|
| `range` | 270 | Full stick deflection at ±135°: full lock without hand-over-hand, finer than 180° at the game's speeds. In-game `SteeringSensitivity` (1.0 now) is the second knob. |
| `sensitivity` | 75 | Square-root mix at the centre that walks the wheel out of the game's stick dead zone (below). |
| `autocenter` | 20 | The game sends only rumble, so this spring is the only centring. A stick snaps back; 20 returns the wheel without fighting. |
| `autocenter_persistent` | 1 | `SDL_HapticOpen` writes `FF_AUTOCENTER 0` when winebus opens the device; this keeps the spring. |
| `app_gain` | 0 | SDL also sets gain 100 on open; Oversteer's `ff_gain` stays in charge. |
| `invert_pedals` | 7 | Pedals rest at the axis minimum, which is what a plain trigger mapping needs. With 0, use `righttrigger:a2~,lefttrigger:a3~` instead. |
| `accelerator_response` | 3,90,50 | 3% dead zone against creep; full throttle by 90% travel (arcade racer, throttle mostly pinned). |
| `brakes_response` | 5,80,50 | 5% dead zone so a resting foot doesn't brake; full braking by 80% on the stiff G29 brake; linear for feathering drifts. |
| `clutch_response` | 0,100,50 | Unmapped; neutral. |
| `rumble_level` | 25 | XInput motor rumble becomes wheel vibration. The game rumbles on boosts and hits; 25 is felt without drowning the centring. |
| `rev_leds` | 0 | The game has no telemetry output. |
| `ff_gain`, `spring_level`, `damper_level`, `friction_level`, `inertia_mode`, `ffb_leds`, rest | as "Star Wars.ini" | The game sends no condition effects, so these have nothing to scale. |

**Things we can't tune from here:**

- The game's own stick dead zone and response curve. Its saved settings expose only
  `SteeringSensitivity`, `PitchSensitivity`, `InvertedSteering` and `bControllerVibration`.
- Unreal's usual gamepad dead zone is about 0.2–0.25 (assumed, not verified for this game).
  At 270° with a linear wheel, 0.2 would swallow the first ±27°. With `sensitivity = 75` it
  is about ±13°, and past that the response is close to linear: about 22% at a quarter
  turn, 50% at half, 76% at three quarters.
- **How to tell if the dead zone hurts:** on a straight, make small corrections of 5-10°.
  If the craft doesn't react until the wheel is well off centre and then darts, the dead zone
  is too big.
  - First fix: raise `sensitivity` (85-90).
  - Second fix: lower `range` (240).
  - Do not drop to 180 before trying those.

## 5. Policy

docs/anti-cheat.md, section 5:

- **No new device, no proxy change, no uinput.** We create no device and change no proxy
  spec. The combined wheel is the existing passthrough identity (rule 1).
- **No emulated identity.**
  - The Windows side sees VID/PID `046d:c24f`, version `0111`, and the name
    "Logitech G29 Driving Force Racing Wheel" (the mapping's name is the wheel's own),
    as it does today.
  - What changes is the HID **usage**: Generic Gamepad + XInput-compatible instead of
    Automobile Simulation Device. Wine does this itself for every SDL-mapped controller.
  - No Xbox VID/PID is presented, and none of Proton's spoof switches
    (`PROTON_SPOOF_STEAMINPUT_VIDPID`) is used.
  - This is a class change of a device that is really plugged in, not hardware that isn't
    there.
- **No input manipulation.** No macros, turbo or timing changes: one analogue wheel and two
  pedals, mapped one to one.
- **Anti-cheat status: none found.**
  - The Steam store shows only "Denuvo Anti-Tamper" and a 5-machine activation limit. There
    is no kernel anti-cheat disclosure (fetched 2026-10-26).
  - The install has no EasyAntiCheat/BattlEye/`start_protected_game` files, only
    `EOSSDK-Win64-Shipping.dll` (crossplay is on: `bEnableCrossplay=True`).
  - The game is not on AreWeAntiCheatYet `games.json` (fetched 2026-10-26).
  - Server-side checks are unknown. Under rule 4 the title is *Unknown*, so passthrough or
    generic only, which this is.
  - If a store anti-cheat disclosure appears later, stop and re-review (rule 5).
- **Denuvo** counts activations per hardware/Proton fingerprint (5 per 24 h). Don't switch
  Proton versions while testing this. Environment variables are not known to count.

## 6. Both the wheel and the Xbox 360 pad connected

With the launch options set, both devices are XInput controllers. Their slot order follows
enumeration order, so the game may listen to either.

- **Wheel only:** unplug the pad before launching. Or add
  `SDL_GAMECONTROLLER_IGNORE_DEVICES=0x045e/0x028e` to the front of the launch options.
  winebus's `is_sdl_ignored_device` drops it.
- **Pad only (fallback):** delete the launch options, or just the
  `SDL_JOYSTICK_WHEEL_DEVICES_EXCLUDED=...` part. The G29 is then a wheel-type HID device
  again, not XInput, and the pad is the only controller.
  Or keep everything and add `SDL_GAMECONTROLLER_IGNORE_DEVICES=0x046d/0xc24f`.

## 7. First test (marth)

1. **Before launch:**
   - Load the Oversteer profile "Star Wars Galactic Racer" and check that invert pedals
     shows all three.
   - Steam Input Disabled; launch options pasted.
   - For the first run, add `PROTON_LOG=1 WINEDEBUG=+hid` to the front of the launch options.
2. **Log check** (`~/steam-4078430.log`):
   - `Setting environment mapping` should appear.
   - Look for a `controller id ...` line with `vid 046d, pid c24f, ... is_gamepad 1`.
   - `joystick id` with `is_gamepad 0` means the mapping didn't take: check the
     `WHEEL_DEVICES_EXCLUDED` part and the quoting.
   - Remove `PROTON_LOG`/`WINEDEBUG` afterwards.
3. **Menus:** the d-pad moves, Cross confirms, Circle goes back, Options pauses, Share opens
   View Build.
4. **Steering:** in a race, full lock left and right at about ±135°. Small corrections near
   centre respond (section 4's dead-zone check). Release the wheel and it centres.
5. **Triggers:** no creep with feet off. Throttle reaches full by about 90% travel. The brake
   slows the craft, and with throttle held the brake can be feathered into a drift.
6. **Boost and actions:** Cross boosts, R3 toggles boost mode, left paddle fires the ability,
   d-pad picks abilities 1-4, Square does the Advanced Maneuver, right paddle changes camera.
7. **Rumble:** a wall hit or a boost buzzes the rim, and the in-game vibration toggle stops
   it. Adjust `rumble_level` to taste. If there is no rumble at all, check the log for
   haptics warnings.

## 8. Possible follow-up in code (not done)

`oversteer/steam_options.py` holds one global string, `WHEEL_OPTIONS`. A small, natural
extension:

- a `GAMEPAD_MAPPINGS` dict keyed by Steam app id (`4078430: <mapping>`);
- a function `gamepad_options(vid, pid, mapping)` returning
  `SDL_JOYSTICK_HIDAPI=0 SDL_JOYSTICK_WHEEL_DEVICES_EXCLUDED=0x{vid}/0x{pid} SDL_GAMECONTROLLERCONFIG="<guid>,<name>,..." %command%`,
  building the GUID from the wheel's sysfs `id/*`.

The wheel launch-options row in `gtk_ui.py` would offer it as a second entry for games
without wheel support. That is about 20 lines plus a test. No game knowledge would live in
code beyond the mapping table.

## Sources

- Proton winebus: https://github.com/ValveSoftware/wine/blob/experimental_11.0/dlls/winebus.sys/bus_sdl.c ,
  `main.c` (same branch); installed `files/lib/wine/x86_64-unix/winebus.so` strings.
- SDL 3.4: https://github.com/libsdl-org/SDL/tree/release-3.4.x
  (`src/joystick/SDL_joystick.c`, `SDL_gamepad.c`, `linux/SDL_sysjoystick.c`,
  `hidapi/SDL_hidapijoystick.c`, `include/SDL3/SDL_hints.h`).
- Runtime: `SteamLinuxRuntime_sniper/sniper_platform_3.0.20261021.266025/files/manifest.dpkg`.
- new-lg4ff: `marthofdoom/new-lg4ff` fork at `9d09fd2`, `hid-lg4ff.c` (module 0.8.0 loaded).
- Steam store: https://store.steampowered.com/app/4078430/ ; AreWeAntiCheatYet `games.json`;
  [GamingOnLinux, release and Denuvo](https://gamingonlinux.com/2026/04/star-wars-galactic-racer-releases-october-6) ;
  [GamingOnLinux, Denuvo and Proton versions](https://www.gamingonlinux.com/2025/05/denuvo-will-lock-you-out-of-games-on-linux-steamos-steam-deck-if-you-keep-changing-proton-versions/).
- Handling: [GamingTrend review](https://gamingtrend.com/reviews/star-wars-galactic-racer-review/),
  [Checkpoint preview](https://checkpointgaming.net/features/2026/06/star-wars-galactic-racer-hands-on-preview-podracer-takedown/).
  Reviews call the handling tight and responsive, not floaty.

[prima]: https://primagames.com/tips/star-wars-galactic-racer-complete-controls
[drift]: https://www.playnews.gg/en/guides/star-wars-galactic-racer-drift-how-to-master-it-our-tips
