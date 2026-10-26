#!/bin/bash
# Screenshot the app in the iPhone simulator on the Mac, light and dark, against a running `vision serve`.
#   VISION_TOKEN=<contents of ~/.config/vision/remote_token> VISION_URL=http://<machine>:8765 ./simshot.sh <model|chats|sessions|none> <name>
# Writes <name>-light.png and <name>-dark.png next to this script. Requires Xcode + xcodegen on PATH (or ~/.local/xcodegen).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/VisionRemote"
export PATH="$PATH:$HOME/.local/xcodegen/xcodegen/bin"
: "${VISION_URL:=http://127.0.0.1:8765}"
APP="$(sed -n 's/^BUNDLE_ID_PREFIX *= *//p' Local.xcconfig Signing.xcconfig 2>/dev/null | head -1).VisionRemote"
xcodegen generate >/dev/null
xcodebuild -project VisionRemote.xcodeproj -scheme VisionRemote -destination "platform=iOS Simulator,name=iPhone 17,OS=27.0" -configuration Debug -derivedDataPath simbuild CODE_SIGNING_ALLOWED=NO build 2>&1 | grep -E "error:|BUILD (SUCCEEDED|FAILED)" | sort -u
SIM=$(xcrun simctl list devices available -j | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(x["udid"] for l in d["devices"].values() for x in l if x["name"]=="iPhone 17"))')
xcrun simctl boot $SIM 2>/dev/null || true
xcrun simctl bootstatus $SIM -b >/dev/null
xcrun simctl install $SIM simbuild/Build/Products/Debug-iphonesimulator/VisionRemote.app
xcrun simctl spawn $SIM defaults write $APP serverURL "$VISION_URL"
for mode in light dark; do
  xcrun simctl ui $SIM appearance $mode
  xcrun simctl terminate $SIM $APP 2>/dev/null || true
  SIMCTL_CHILD_VISION_TOKEN=$VISION_TOKEN xcrun simctl launch $SIM $APP --open $1 >/dev/null
  sleep 5
  xcrun simctl io $SIM screenshot "$HERE/$2-$mode.png" >/dev/null 2>&1
done
echo done
