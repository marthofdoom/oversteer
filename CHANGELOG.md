# Changelog

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
- The proxy service runs as root, so the installer refuses to start it
  from a user-writable location (install Oversteer system-wide; developers
  can pass `--unsafe-dev-tree`). The unit is sandboxed (NoNewPrivileges,
  ProtectSystem=strict, only input devices and /dev/uinput allowed), files
  it writes are root-owned 0644, udev rule text is sanitised.

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
