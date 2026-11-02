# Contributing

Countersteer is a fork of [berarma/oversteer](https://github.com/berarma/oversteer); `origin` is
marthofdoom/oversteer, `upstream` is berarma's.

## Run from source
`scripts/run-dev.sh` sets up a meson build directory on first run and starts the app from this tree
(dependencies: see `docs/upstream-readme.md`). By hand: `meson setup build -Dprefix=$HOME/.local -Dudev_rules_dir=packed`,
`ninja -C build`, then `MESON_BUILD_ROOT=$PWD/build MESON_SOURCE_ROOT=$PWD python3 build/bin/oversteer --gui`.

## Tests
`python3 -m pytest -q --ignore=tests/test_proxy.py`

`tests/test_proxy.py` creates virtual uinput devices. It skips itself without `/dev/uinput` access or while a
Wine/Proton game is running; never force it (`OVERSTEER_TEST_UINPUT`) or restart the proxy while a game uses the
proxied wheel, as the device churn can crash the game.

## Branches and releases
Work on a branch, merge it into `master` with `git merge --no-ff`, tag releases as annotated `vX.Y.Z`. The
release steps are in [RELEASING.md](RELEASING.md); user-visible changes get a [CHANGELOG.md](CHANGELOG.md) entry
at release time.

## Licence
GPLv3 (see `LICENSE`); contributions are under the same licence.
