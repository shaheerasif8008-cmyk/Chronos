#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT/Assets/AppIcon-1024.png"
ICONSET="$ROOT/.build/AppIcon.iconset"
OUTPUT="${1:-$ROOT/.build/AppIcon.icns}"

test -f "$SOURCE"
rm -rf "$ICONSET"
mkdir -p "$ICONSET" "$(dirname "$OUTPUT")"

render() {
  local pixels="$1"
  local filename="$2"
  sips -z "$pixels" "$pixels" "$SOURCE" --out "$ICONSET/$filename" >/dev/null
}

render 16 icon_16x16.png
render 32 icon_16x16@2x.png
render 32 icon_32x32.png
render 64 icon_32x32@2x.png
render 128 icon_128x128.png
render 256 icon_128x128@2x.png
render 256 icon_256x256.png
render 512 icon_256x256@2x.png
render 512 icon_512x512.png
render 1024 icon_512x512@2x.png
iconutil -c icns "$ICONSET" -o "$OUTPUT"
