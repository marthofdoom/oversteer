# Changelog

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
