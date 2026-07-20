#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Chronos Desktop"
APP="$ROOT/dist/$APP_NAME.app"
CONTENTS="$APP/Contents"
VERSION="${CHRONOS_DESKTOP_VERSION:-1.0.0}"
BUILD_NUMBER="${CHRONOS_DESKTOP_BUILD:-1}"
IDENTITY="${CHRONOS_CODESIGN_IDENTITY:--}"

cd "$ROOT"
swift build -c release
BIN_DIR="$(swift build -c release --show-bin-path)"

rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
cp "$BIN_DIR/ChronosDesktop" "$CONTENTS/MacOS/ChronosDesktop"
cp Packaging/Info.plist "$CONTENTS/Info.plist"
scripts/generate-icon.sh "$CONTENTS/Resources/AppIcon.icns"

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$CONTENTS/Info.plist"

if [[ "$IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - --entitlements Packaging/ChronosDesktop.entitlements "$APP"
  echo "Built ad-hoc signed app at $APP"
else
  codesign \
    --force \
    --deep \
    --options runtime \
    --timestamp \
    --sign "$IDENTITY" \
    --entitlements Packaging/ChronosDesktop.entitlements \
    "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
  echo "Built Developer ID signed app at $APP"
fi
