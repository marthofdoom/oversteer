# Releasing Oversteer

Tag at good points: whenever a coherent set of features has been verified
against the wheel and the matching new-lg4ff release exists.

1. Merge the feature branches into `master` (`git merge --no-ff`).
2. Run from source: `meson setup build -Dprefix=$HOME/.local -Dudev_rules_dir=packed`,
   `ninja -C build`, then `MESON_BUILD_ROOT=$PWD/build MESON_SOURCE_ROOT=$PWD python3 build/bin/oversteer --gui`.
   Check every new control against the driver (sysfs values change) and
   that the CLI flags round-trip (`--list`, then set/restore a value).
3. If the udev rule changed, install it and confirm the new attributes are
   writable as the user.
4. **Opus 5.5 diff review**: have Opus 5.5 (`claude-opus-5-5`) review
   `git diff <previous tag>..master` for correctness (profile round-trip,
   model/UI re-entrancy, old-driver fallbacks), GTK/Glade handler wiring,
   CLI parsing and udev quoting. Fix or consciously waive every finding.
5. Bump `version` in `meson.build`, add a `<release>` entry to
   `data/io.github.berarma.Oversteer.appdata.xml.in`, add a section to
   `CHANGELOG.md`.
6. Commit, tag `vX.Y.Z` (annotated), push `master` and the tag.
7. `gh release create vX.Y.Z --notes-file <notes>`; state which new-lg4ff
   version the release expects.

Versioning: 0.x while Windows parity is incomplete; 1.0.0 when
`docs/g29-windows-parity.md` has no red rows for the G29.
