#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Chronos Desktop"
VERSION="${CHRONOS_DESKTOP_VERSION:-1.0.0}"
DMG="$ROOT/dist/Chronos-Desktop-$VERSION.dmg"
STAGING="$ROOT/.build/dmg-root"

"$ROOT/scripts/build-app.sh"
rm -rf "$STAGING" "$DMG"
mkdir -p "$STAGING"
cp -R "$ROOT/dist/$APP_NAME.app" "$STAGING/$APP_NAME.app"
ln -s /Applications "$STAGING/Applications"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  "$DMG" >/dev/null

if [[ -n "${CHRONOS_CODESIGN_IDENTITY:-}" && "${CHRONOS_CODESIGN_IDENTITY}" != "-" ]]; then
  codesign --force --timestamp --sign "$CHRONOS_CODESIGN_IDENTITY" "$DMG"
fi

shasum -a 256 "$DMG" > "$DMG.sha256"
echo "Packaged $DMG"
