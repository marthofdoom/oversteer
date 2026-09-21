# Changelog

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
