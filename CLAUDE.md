# Countersteer (Oversteer fork) — notes for coding agents

## Build, run, test
- Run from source: `scripts/run-dev.sh [--gui]` (meson build dir `build/`, then `python3 build/bin/oversteer`).
- Tests: `python3 -m pytest -q --ignore=tests/test_proxy.py` (about 2 minutes). CI adds `-k "not uinput"` and runs under xvfb.
- FFB parity: `scripts/ffb-parity.py [CAPTURE] [--ref CAPTURE] [--json PATH]` compares a recorded FFB drive with a reference drive and flags gaps over 25 %; log in `~/.cache/ffb-parity/latest.log`; run `tests/test_ffb_parity.py` after editing it.
- Release steps: `RELEASING.md`; the version lives in `meson.build`, `data/io.github.berarma.Oversteer.appdata.xml.in`,
  `CHANGELOG.md` and the oversteer module's `tag:` line in `flatpak/io.github.berarma.Oversteer.yaml`.

## Layout
- `oversteer/` Python package. GTK UI: `gtk_ui.py`, `gtk_handlers.py`, `gtk_live_view.py`, `gtk_run_view.py`,
  `main.ui`. Wheel/device: `device.py`, `device_manager.py`, `model.py`.
- Telemetry: `telemetry_formats.py` (game packet decoders), `telemetry_capture.py`, `telemetry_store.py`
  (SQLite, `~/.local/share/oversteer/telemetry.db`, WAL), `telemetry_web.py` + `web_controls.py` (local web UI),
  `telemetry_view.py`, `telemetry_plot.py`.
- Coaching/analysis: `coach*.py`, `coaching_matrix.py`, `potential.py`, `run_analysis.py`, `shift_learner.py`,
  `tuning.py`, `car_data.py`, `stage_tables.py`, `rbr_*.py` (Richard Burns Rally).
- `oversteer/proxy/` FFB/input proxy daemon (virtual uinput device in front of the wheel).
- `data/telemetry/` shipped tables; `data/telemetry/web/` front end; `data/telemetry/oversteer-shm-bridge.c` (Wine shared-memory bridge).
- `flatpak/` manifest, `scripts/` dev helpers, `docs/`, `research/`, `tests/` (pytest, `conftest.py` isolates
  RBR installs and learnt tables).

## Conventions
- Absolute times are `m:ss.s` everywhere including coach text (`oversteer/timefmt.py`); differences are signed
  seconds (`telemetry_view.signed`).
- PBs are never deleted: PB runs, traces and history survive pruning, retention, re-analysis, merge and forget.
- No user setup required for game support: no env vars, config files or Flatpak overrides; ship recognition
  tables built from real data. `OVERSTEER_RBR_DIR` is a development aid only.
- Work on branches, merge to `master`, annotated `vX.Y.Z` tags. Match surrounding style and comment density.

## Safety rules
- Never run `tests/test_proxy.py` or restart the proxy while a game is running; never set `OVERSTEER_TEST_UINPUT`
  (virtual-device churn has crashed games).
- Never open the live telemetry DB for writing in experiments: read-only backup copies only
  (`telemetry_store._backup(db, dest)` for the copy, `open_reader(path)` for a `mode=ro` connection).
- Do not `pkill`; kill only your own PIDs. No `git stash`/`reset` in a shared tree; use worktrees.
