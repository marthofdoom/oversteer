# Anti-cheat and virtual input identities

Research for ClickUp 86e3bk2va (2026-10-05). It decides what identity the
proxy (`oversteer/proxy/`) may give its uinput devices before any
"emulate identity" default ships. The rule throughout: **unknown is treated
as forbidden.**

Terms used below:

- **Passthrough identity**: the virtual device carries the identity
  (name/VID/PID/version) of a physical device that is plugged in and grabbed
  by the proxy, e.g. the combined wheel as the owner's own G29 `046d:c24f`.
  The game sees one G29, as it would without the proxy.
- **Generic identity**: an identity no game knows, e.g.
  `GENERIC_IDENTITY` (`1209:0ec5`, "Oversteer Combined Wheel", pid.codes
  vendor id). It claims nothing about hardware.
- **Emulated identity**: the identity of hardware that is *not* present,
  e.g. `HANDBRAKE_IDENTITY` (Fanatec ClubSport Handbrake `0eb7:00e5`) on
  top of an ANNX handbrake.

## Summary

- Of the 16 titles, five ship a **kernel anti-cheat on Windows** that is
  disclosed on their Steam page: EA SPORTS WRC, F1 24 and F1 25 (EA Javelin)
  and Le Mans Ultimate (Easy Anti-Cheat). iRacing also uses EAC/EOS
  (no Linux client). The EA titles do not run under Proton at all; LMU and
  iRacing are marked broken/unsupported on Linux by the trackers.
- No title in scope has a published rule about virtual HID devices or
  spoofed VID/PID. Neither EAC nor BattlEye publishes one either. The only
  explicit vendor statement found is Codemasters/EA (2024-09): tools "that
  can spoof peripherals to run as other devices, like JoyToKey" are
  **blocked** by F1 24's anti-cheat.
- The enforcement precedents found (EA Javelin blocking reWASD and DS4Windows
  at launch; Call of Duty Ricochet and Ubisoft Mousetrap targeting
  Cronus/XIM) are all about **gamepad/mouse aim advantage** or about
  **Windows remapping drivers**. **No ban tied to racing-peripheral
  emulation was found**, in either direction. Absence of evidence is not
  permission.
- Under Proton, a game (and any user-mode anti-cheat inside it) never sees a
  USB device. Winebus turns every Linux device, real or uinput, into a
  synthetic HID device whose descriptor Wine builds itself. A uinput clone
  of a G29 and a real G29 look almost identical from the Windows side; they
  differ mainly in serial and container ID, and on the Linux side (sysfs).
- **Policy (section 5):** passthrough or generic identity by default;
  emulated identity only as a per-title opt-in for titles with no client
  anti-cheat confirmed by more than one source (today: Forza Horizon 5/6,
  BeamNG.drive, Wreckfest); never in a title whose anti-cheat runs under
  Proton or that has a kernel anti-cheat on Windows; the proxy is off for
  those titles unless the owner turns it on.

## 1. Per-title table

Retrieved 2026-10-05. "Steam disclosure" is the store page's anti-cheat
field. Valve has required it for **kernel-mode** anti-cheat since
2024-10-30 [S1]. A missing disclosure therefore rules out a kernel AC but not
a user-mode or server-side one. "Deck" is Valve's Steam Deck category
(3 Verified, 2 Playable, 1 Unsupported, 0 Unknown) from the store's
compatibility endpoint on the same date.

| Title | AC vendor | Proton status | Passthrough | Emulate identity | Sources |
|---|---|---|---|---|---|
| Forza Horizon 6 | None found. No Steam disclosure; press: "no shenanigans with anti-cheat" (2026-03-25). Online play and an Xbox account are required. | Runs (Deck 3, Proton Hotfix default); online works (2026-05-19) | OK | **Opt-in OK** (no client AC; server-side unknown) | S2, S3, store |
| Forza Horizon 5 | None found. No Steam disclosure; "no EAC or BattlEye". | Runs (Deck 2); some report social features broken | OK | **Opt-in OK** (same basis as FH6, single press source) | S4, store |
| EA SPORTS WRC | EA Javelin, kernel (added June 2024, required offline too) | **Broken**: Javelin blocks Linux (GOL "Broken", AWACY "Denied") | n/a (does not run) | **Forbidden** | S5, S6, S7, store |
| WRC Generations | None disclosed; no positive source either way | Runs (Deck 2) | OK | **Unknown → forbidden** | store, S8 |
| F1 24 | EA Javelin, kernel | **Broken / Denied** | n/a | **Forbidden** (EA: peripheral-spoofing tools blocked) | S5, S7, S9, store |
| F1 25 | EA Javelin, kernel | **Broken / Denied** | n/a | **Forbidden** | S5, S7, S10, store |
| Assetto Corsa Competizione | None disclosed; not on GOL/AWACY lists; no positive source | Runs (Deck 2) | OK | **Unknown → forbidden** | store, S11 |
| Assetto Corsa | None disclosed; not on lists | Runs via Proton (Deck 1, "SteamOS not supported" label) | OK | **Unknown → forbidden** | store |
| Assetto Corsa Rally (EA) | None disclosed; no source. Private lobbies only (16 players, 0.6) | Runs (Deck 2, ProtonDB Gold) | OK | **Unknown → forbidden** | store, S12, S13 |
| Assetto Corsa EVO (EA) | None disclosed. Kunos said multiplayer "will feature an anti-cheat system", not named (unverified) | Runs (Deck 2) | OK | **Unknown → forbidden** | store, S14 |
| BeamNG.drive | None (native Linux build; BeamMP is a community mod) | Native Linux + Proton (Deck 2) | OK | **Opt-in OK** | S15, store |
| DiRT Rally 2.0 | VAC (user mode; "Running") | Runs (AWACY "Running"); Deck 1 | OK | **Unknown → forbidden** (no VAC stance on devices) | S7, store |
| Wreckfest | None found (community answer only) | Runs (Deck 3) | OK | **Opt-in OK** (weak: community source + Verified) | S16, store |
| Automobilista 2 | None disclosed. Forum: "AFAIK there isn't one" (2025-03-19, non-dev); one report mentions "cheat protected" servers (unverified) | Runs incl. online (Reiza forum) | OK | **Unknown → forbidden** | S17, S18, store |
| Le Mans Ultimate | Easy Anti-Cheat, kernel on Windows (added 2025) | **Unsupported / Broken** per GOL (2025-12-12); one 2026-04 report says online worked | Proxy off by default | **Forbidden** | S19, S20, S21, store |
| iRacing | EAC / EOS anti-cheat; **no Linux client**, online prohibited under Proton | **Broken** (AWACY, GOL 2026-08-18) | n/a | **Forbidden** | S7, S22, S23 |
| RaceRoom | None disclosed; not on lists; online works under Proton | Runs (Deck 2) | OK | **Unknown → forbidden** | S24, store |

"Passthrough OK" means: under Proton a passthrough device reaches the game
through the same SDL path, with the same synthetic descriptor, as the
physical wheel (section 2). It adds nothing an anti-cheat could see that the
real wheel does not already show, beyond the serial and container ID. It
does not mean a developer has approved it.

## 2. How an anti-cheat sees input devices under Proton

Read from the fetched Proton Wine source, `dlls/winebus.sys` (`main.c`,
`bus_sdl.c`, `bus_udev.c`; exact Proton version of the copy not recorded).

**Path.** Winebus enumerates Linux devices through up to three backends:
SDL (on by default, `Enable SDL`), udev hidraw, and udev evdev. Each device
becomes a Windows PnP device on a Wine bus, and `hidclass` exposes it to
DirectInput, Raw Input, WinMM and XInput. The game, and any anti-cheat in
its process, only ever see these synthetic devices.

**hidraw is opt-in per device.** `is_hidraw_enabled()` passes a
joystick/gamepad usage through hidraw only for a hard-coded list or an
override (`PROTON_ENABLE_HIDRAW`, registry `EnableHidraw`). The hard-coded
list covers DS4, DualSense, some Thrustmaster flight gear, Simucube, a
dozen Fanatec bases and pedals (not `00e5`), VKB and VPC. Logitech `046d`
is not on it, so a real G29 already reaches games via SDL/evdev. A uinput
device has no hidraw node at all; that would need uhid.

**What the Windows side sees** for an SDL or evdev device:

| Field | Source (SDL backend) | Source (evdev backend) |
|---|---|---|
| VID/PID/version | `SDL_JoystickGetVendor/Product/ProductVersion`, i.e. the evdev `input_id` | `EVIOCGID`, or `PRODUCT=` from the udev `input` parent |
| Product string | SDL joystick name, i.e. the evdev name | `EVIOCGNAME` / `HID_NAME` |
| Manufacturer | the literal `"SDL"` | the USB parent's `manufacturer`, else the literal `"evdev"` |
| Serial | SDL serial (evdev `uniq`), else `"0000"` | USB parent `serial`, else `uniq`, else `"0000"` |
| HID report descriptor | **built by Wine** from the axes, buttons, hats and FF effects | same |
| Device ID | `WINEBUS\VID_xxxx&PID_xxxx` (bus type unknown) | `USB\VID_…` if `id/bustype` is USB, `BTHENUM\…` if Bluetooth |
| Hardware ID | always `WINEBUS\VID_xxxx&PID_xxxx` | same |
| Container ID | made up from VID/PID, an index and a tick count | for USB: from the USB parent's BUSNUM/DEVNUM/`USEC_INITIALIZED` |

Consequences:

- **The original USB descriptors are never visible** under the default
  path: no real HID descriptor, no USB string descriptors, no configuration
  descriptor. A real G29 and a uinput device with the G29's
  `input_id`, name and axis/button/FF set produce the same synthetic
  descriptor and the same IDs. They differ in serial (only if the real one
  has a `uniq`) and container ID.
- **A uinput device has no USB parent in sysfs.** It lives under
  `/sys/devices/virtual/input/`. If its `bustype` is USB, the evdev backend
  marks it `USB\…` and then fails to build a USB container ID (logs
  `ERR "Failed to get parent device"`). That is the only Wine-visible
  difference, and only on the evdev path. Under SDL the device is
  `WINEBUS\…` either way.
- The hardware ID always starts with `WINEBUS\`, so any anti-cheat that
  inspects PnP IDs already sees a Wine device whether or not the proxy is
  used.
- **Linux side (unverified for any shipping AC):** a Windows process under
  Wine can read the host file system through `Z:\`, including `/sys` and
  `/proc`. The Proton EAC and BattlEye runtimes are user-mode and are
  reported to scan `/proc` and memory [S25]. No source shows one reading
  `/sys/class/input`, but nothing prevents it. A uinput device is
  recognisable there (`virtual` path, no USB ancestors, `phys` such as
  `usb-fanatec-virt/input0`), as is the process that owns `/dev/uinput`.
- Only Steam Input's virtual pad `28de:11ff` is special-cased: SDL ignores
  it and it is handled by evdev with version 0. This is what Wine does with
  Valve's own uinput gamepad.

## 3. Published stances and precedents

**Anti-cheat vendors**

- **EAC**: no published policy on virtual HID, vJoy or remappers was found.
  On Linux/Proton it runs user mode only, and each game opts in per title
  (Epic, 2021-09) [S25, S26]. Older user reports tie EAC bans to x360ce,
  Xpadder or vJoy setups (For Honor, BF2042), all unconfirmed by EAC,
  which does not disclose ban reasons [S27].
- **BattlEye**: its FAQ says it "might decide to kick (not ban) you … for
  using a specific program (such as macro tools)". It blocks kernel drivers
  with known vulnerabilities. Proton support is per-game opt-in (2021-09)
  [S28, S29].
- **EA Javelin** (EA WRC, F1 24/25):
  - Codemasters, 2024-09-25: "Several third-party tools – such as those
    that can spoof peripherals to run as other devices, like JoyToKey – are
    blocked by F1 24's anti-cheat system" [S9].
  - EA blocked reWASD's virtual hardware and its driver, and the game will
    not start while reWASD is installed [S30].
  - EA FC 24 refused to start with DS4Windows running (2024-08-21; EA
    support: "not permitted while playing our games") [S31].
  - All of these are **launch blocks**, not documented account bans.
- **VAC** (DiRT Rally 2.0): no stance on input devices found.

**Spoofed-VID/PID hardware (Cronus Zen, XIM)**

- **Call of Duty / Ricochet**, 2026, BO7 Season 2: "These devices are not
  permitted … They are cheating tools, even if they masquerade as
  accessibility devices." Detection is behavioural (input timing and
  recoil patterns), not by device ID [S32, S33].
- **Rainbow Six Siege / Mousetrap**, console: XIM-style mouse-as-controller
  input is detected by input pattern and penalised with added latency, not
  bans [S34].
- Both target aim assistance from a mouse posing as a gamepad. No case was
  found of a racing wheel, pedal, shifter or handbrake identity spoof being
  detected or punished.

**Racing-peripheral emulation precedent (benign, commercial)**

- Collective Minds DriveHub's "FanaLogic mode emulates a G29 wheel" on
  PS4/PS5, so Fanatec bases work in games that only know a G29 [S35]. The
  Cronus Zen sold by the same company has a wheel mode [S36]. This is a VID/PID
  identity spoof sold openly for racing; no enforcement against it was found.
- Forza EmuWheel (vJoy feeder, Windows) exists to give Forza Horizon/Motorsport
  wheels, pedals, shifters and handbrakes it does not otherwise support
  [S37]. It uses a vJoy identity (generic), not a spoofed one.

## 4. Community practice

| Tool | What it presents | Known incidents |
|---|---|---|
| vJoy / Joystick Gremlin | generic vJoy device `1234:BEAD` | user-reported EAC bans with x360ce/vJoy (unconfirmed) [S27]; iRacing plugins exist [S38]; iRacing's accessibility page does not mention it [S39] |
| SimHub Control Mapper | combines wheels/pedals/handbrake into one vJoy (or Arduino) device [S40] | none found; one report of EA anticheat flagging a SimHub-adjacent USB device (unverified) |
| EmuWheel | vJoy for Forza [S37] | none found |
| Steam Input | virtual Xbox 360 pad on Windows; `28de:11ff` on Linux | EAC once blocked Steam Controller in Absolver (old, title-specific) [S41]; Wine itself handles `28de:11ff` |
| DS4Windows / ViGEmBus | virtual Xbox 360 / DualShock 4 (ViGEmBus retired) [S42] | EA FC 24 launch block 2024-08 [S31]; no BattlEye issue known [S43] |
| reWASD | virtual gamepad + driver | EA Javelin launch block; Ricochet ban policy [S30, S32] |
| JoyToKey | key/pad remap | blocked by F1 24 anti-cheat [S9] |
| input-remapper (Linux, uinput) | uinput clone per device | none found [S44] |

The pattern: vendors act against **Windows drivers and remappers with
macro/rapid-fire features** (reWASD, DS4Windows, JoyToKey under EA) and
against **aim-assist hardware**. Plain virtual joysticks for sim gear are
tolerated in sims without client anti-cheat, with no official approval.

## 5. Default identity policy for Oversteer

1. **Default: passthrough or generic.** The combined wheel keeps the
   wheel's own identity (`identity='wheel'`). Extra axis devices use a
   generic identity (`1209:xxxx`, honest name) unless a rule below allows
   more. The proxy never presents hardware that is not plugged in unless the
   owner has opted in for that title.
2. **Emulated identity: per-title opt-in only**, and only for titles marked
   *Opt-in OK* in section 1 (FH6, FH5, BeamNG.drive, Wreckfest). Those are
   titles with no client anti-cheat, backed by more than a missing Steam
   disclosure. The opt-in is the owner's explicit choice, per title, and
   remembered. The Fanatec ClubSport Handbrake identity for Forza falls
   under this.
3. **Never emulate where an anti-cheat is involved**: any title with a
   kernel anti-cheat on Windows (EA Javelin, EAC: EA WRC, F1 24/25, LMU,
   iRacing) or an anti-cheat that runs under Proton (EAC/BattlEye opted
   in). For these the proxy is **off** by default. The owner may enable
   passthrough, with a warning that no developer has approved it.
4. **Unknown → forbidden.** Every title not listed, and every *Unknown*
   row (ACR, ACC, AC, AC EVO, WRC Generations, DR2, AMS2, RaceRoom), gets
   passthrough/generic only. ACR is marth's main title; it needs no emulated
   identity, since its G29 support works with passthrough.
5. **Re-check on anti-cheat changes.** If a store page gains an anti-cheat
   disclosure, or a title is added to the GOL/AWACY lists, it drops to rule 3
   until reviewed. EA WRC gained Javelin a year after release.
6. **Honest Linux-side traces.** Keep a `phys`/name that identifies the
   device as virtual where the game does not need it hidden. Never hide the
   proxy from sysfs or `/proc`.

Mapping to `device_library.CLASSES`: a profile's `anti_cheat_class` says
whether that **identity** may be presented without the hardware.
`unknown` behaves as `passthrough-only`. A profile becomes `emulate-ok`
only for use under rule 2, so the per-title gate is still needed on top of
it. All current profiles stay `unknown`.

## 6. Open questions

- **ACR, AC EVO multiplayer anti-cheat**: Kunos has said EVO multiplayer
  will have one, unnamed. Check each Early Access update's notes and the
  store disclosure.
- **AMS2 "cheat protected" servers**: one report says EAC blocks them under
  Proton, a later forum answer says there is none. Unresolved.
- **LMU on Linux**: GOL marks it broken (2025-12), simracinginfo reports
  online sessions working (2026-04/08). Is EAC enabled for Proton or simply
  failing open?
- **Forza server-side checks**: do Playground's servers see controller
  identity (e.g. telemetry of device type) and treat an unexpected one as
  suspicious? There is no public information.
- **Whether any Proton AC runtime reads `/sys/class/input`** or flags
  `/dev/uinput` users. Not documented; would need a trace of the runtime
  (outside this task's limits: no running games or AC).
- **Exact Proton/Wine version** of the winebus copy used for section 2.
  Re-check `is_hidraw_enabled()` and the SDL path against the Proton
  version marth runs.

## Sources

- S1 Game Developer, "devs on Steam now need to disclose kernel mode anti-cheat" (2024-10-30) https://www.gamedeveloper.com/pc/heads-up-devs-on-steam-now-need-to-disclose-kernel-mode-anti-cheat-software ; PC Gamer https://www.pcgamer.com/games/steam-now-requires-developers-to-tell-people-when-their-games-have-kernel-mode-anticheat/
- store: Steam store pages and `ajaxgetdeckappcompatibilityreport`, fetched 2026-10-05 (app ids 2483190, 1551360, 1849250, 1953520, 2488620, 3059520, 805550, 244210, 3917090, 3058630, 284160, 690790, 228380, 1066890, 2399420, 266410, 211500)
- S2 GamingOnLinux, FH6 playable on Deck (2026-03-25) https://www.gamingonlinux.com/2026/03/forza-horizon-6-confirmed-to-be-playable-on-steam-deck-steamos/
- S3 GamingOnLinux, FH6 out, Proton Hotfix (2026-05-19) https://www.gamingonlinux.com/2026/05/forza-horizon-6-is-out-valve-update-proton-hotfix-for-linux-initial-thoughts/
- S4 caniplayonlinux FH5 https://caniplayonlinux.com/games/forza-horizon-5/ ; Forza support Steam Deck FAQ https://support.forza.net/hc/en-us/articles/13169879356947-Steam-Deck-and-SteamOS-FAQ
- S5 GamingOnLinux anti-cheat list, EA Javelin https://www.gamingonlinux.com/anticheat/vendor/ea-javelin-anticheat/ (fetched 2026-10-05)
- S6 EA, "EA SPORTS WRC - EA anticheat" https://www.ea.com/technology/news/ea-anticheat ; Destructoid https://www.destructoid.com/ea-wrc-is-unplayable-on-the-steam-deck-after-latest-update/
- S7 AreWeAntiCheatYet `games.json` https://github.com/AreWeAntiCheatYet/AreWeAntiCheatYet (fetched 2026-10-05)
- S8 ProtonDB WRC Generations https://www.protondb.com/app/1953520
- S9 Traxion, "F1 24: 'No advantage' from alleged exploit" (2024-09-25), quoting Codemasters https://traxion.gg/f1-24-no-advantage-from-alleged-exploit-speculation-condemned/
- S10 Proton issue #9673 F1 25 https://github.com/ValveSoftware/Proton/issues/9673
- S11 Steam ACC discussion "Works great on Linux" https://steamcommunity.com/app/805550/discussions/0/1741094390462271126/
- S12 GamingOnLinux, ACR Early Access (2025-11-14) https://www.gamingonlinux.com/2025/11/assetto-corsa-rally-has-arrived-in-early-access-should-work-well-on-linux-steam-deck/
- S13 Simulation Daily, ACR 0.6 private lobbies https://simulationdaily.com/news/assetto-corsa-rally-update-0-6-release/
- S14 GTPlanet, AC EVO "everything we know" (2024-12-17) https://www.gtplanet.net/assetto-corsa-evo-everything-we-know-20241217/ (anti-cheat claim via search summary, unverified)
- S15 BeamMP-Linux https://github.com/gamingdoom/BeamMP-Linux ; caniplayonlinux BeamNG https://caniplayonlinux.com/games/beamngdrive/
- S16 Steam Wreckfest discussion "Is there some sort of anti-cheat" https://steamcommunity.com/app/228380/discussions/0/1735462352506957817/
- S17 Reiza forum "Anti-Cheat Ams2 ?" (2025-03-19) https://forum.reizastudios.com/threads/anti-cheat-ams2.34962/
- S18 Reiza forum "AMS2 works great on Linux" https://forum.reizastudios.com/threads/automobilista-2-works-great-on-linux-with-steam-play-proton.9670/
- S19 GamingOnLinux anti-cheat list, EAC (LMU 2025-12-12, iRacing 2026-08-18) https://www.gamingonlinux.com/anticheat/vendor/easy-anti-cheat/
- S20 LMU Known Issues https://guide.lemansultimate.com/hc/en-gb/articles/13240843908623-Known-Issues-and-Advice
- S21 simracinginfo, "Le Mans Ultimate on Linux?" (2026-04-16, upd. 2026-08-23) https://www.simracinginfo.com/le-mans-ultimate-on-linux/
- S22 AreWeAntiCheatYet iRacing https://areweanticheatyet.com/game/iracing
- S23 iRacing 2024 S2 Patch 4 notes (EAC → EOS) https://support.iracing.com/support/solutions/articles/31000173098-2024-season-2-patch-4-release-notes-2024-05-01-02-
- S24 BoxThisLap, "SimRacing on Linux" https://boxthislap.org/simracing-on-linux-a-new-reality/
- S25 Epic Online Services, anti-cheat for Linux/Mac/Deck (2021-09) https://onlineservices.epicgames.com/news/epic-online-services-launches-anti-cheat-support-for-linux-mac-and-steam-deck ; Wikipedia EAC https://en.wikipedia.org/wiki/Easy_Anti-Cheat
- S26 GamingOnLinux, "EAC not as simple as expected for Proton" (2022-01) https://www.gamingonlinux.com/2022/01/easy-anti-cheat-not-as-simple-as-expected-for-proton-and-steam-deck/
- S27 Steam For Honor "EAC Banned" thread https://steamcommunity.com/app/304390/discussions/0/1471968797465840118/ ; EA BF2042 "banned?" https://forums.ea.com/discussions/battlefield-2042-general-discussion-en/re-banned/6933332
- S28 BattlEye FAQ https://www.battleye.com/support/faq/ (fetched 2026-10-05)
- S29 BattlEye on X, Proton opt-in (2021-09) https://x.com/TheBattlEye/status/1441477816311291906 ; GamingOnLinux (2021-11) https://www.gamingonlinux.com/2021/11/supporting-linux-proton-and-the-steam-deck-with-battleye-is-just-an-email-away/
- S30 EA forums "EA Javelin Anticheat & Recent Software Blocks" https://forums.ea.com/discussions/ea-forums-general-discussion-en/ea-javelin-anticheat--recent-software-blocks/12218073 (403 to fetch; content via search summary, unverified) ; reWASD forum https://forum.rewasd.com/forum/rewasd/technical-questions-aa/246126-ea-security-violation
- S31 Steam EA FC 24 "Anticheat - Virtual Controller" (2024-08-21) https://steamcommunity.com/app/2195250/discussions/0/4434443557913532365/
- S32 Call of Duty, RICOCHET update (2026-04) https://www.callofduty.com/blog/2026/04/call-of-duty-black-ops-7-ricochet-anti-cheat-season-03 ; Dot Esports https://dotesports.com/call-of-duty/news/call-of-duty-cronus-zen-xim-matrix-ban-ranked-play-season-2
- S33 Dexerto, Cronus/XIM crackdown https://www.dexerto.com/call-of-duty/cod-cracks-down-on-cronus-zen-xim-in-major-anti-cheat-update-for-black-ops-7-season-2-3313252/
- S34 Windows Central, R6 Mousetrap https://www.windowscentral.com/gaming/xbox/ubisoft-is-taking-a-stand-against-mouse-and-keyboard-cheaters-in-rainbow-six-siege-on-console
- S35 DriveHub manual, Fanatec/Logitech modes https://collectiveminds.gitbook.io/drivehub/manual/fanlogimodes
- S36 Cronus Zen PS5 wheel mode https://guide.cronuszen.com/wheel-mode/ps5setup
- S37 Fanatec forum, Forza EmuWheel Configurator https://forum.fanatec.com/topic/18823-updated-forza-emuwheel-configurator-app/
- S38 irwjplugin (Joystick Gremlin for iRacing) https://github.com/darkfibre/irwjplugin
- S39 iRacing, hardware/software for iRacers with disabilities (mod. 2025-09-09) https://support.iracing.com/support/solutions/articles/31000170166
- S40 SimHub Control Mapper https://oesimracing.com/simhub/control-mapper/
- S41 Absolver forum, EAC blocking Steam Controller https://absolvergame.com/forums/discussion/25354/pc-easy-anti-cheat-blocking-steam-controller
- S42 ViGEmBus (retired) https://github.com/nefarius/ViGEmBus
- S43 ds4win.com, "Can DS4Windows get you banned?" https://ds4win.com/ds4windows-get-you-banned/ (vendor-adjacent, weak)
- S44 input-remapper https://github.com/sezanzeb/input-remapper/discussions/227
- Wine: `dlls/winebus.sys/{main.c,bus_sdl.c,bus_udev.c}` from the Proton Wine tree, functions `is_hidraw_enabled`, `sdl_add_device`, `is_sdl_ignored_device`, `get_device_subsystem_info`, `udev_add_device`, `get_container_id_for_usb_udev_device`, `lnxev_device_create`, `get_device_id`, `get_hardware_ids`
