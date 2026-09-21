#!/bin/sh
# Build the Flatpak from flatpak/io.github.berarma.Oversteer.yaml and export a
# single-file bundle (Oversteer-<version>.flatpak) next to this script's repo.
# Needs org.flatpak.Builder from Flathub: flatpak install flathub org.flatpak.Builder
set -e
cd "$(dirname "$0")/.."
VERSION=$(sed -n "s/^  version: '\(.*\)',/\1/p" meson.build)
flatpak run org.flatpak.Builder --user --install-deps-from=flathub --force-clean \
    --repo=build-flatpak/repo build-flatpak/build flatpak/io.github.berarma.Oversteer.yaml
flatpak build-bundle build-flatpak/repo "Oversteer-$VERSION.flatpak" io.github.berarma.Oversteer
echo "bundle: Oversteer-$VERSION.flatpak (install with: flatpak install Oversteer-$VERSION.flatpak)"
