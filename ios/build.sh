#!/bin/zsh
# Build Vision Remote and install it on the iPhone plugged into (or reachable from) this Mac; UDID=<device> picks
# another. Run in the GUI session (codesign needs the keychain). To sign under your own team, set up
# VisionRemote/Local.xcconfig first (see Local.xcconfig.example).
set -e
cd "${0:A:h}/VisionRemote"  # this checkout; DerivedData and the log go in ~/VisionRemote-ios
OUT=~/VisionRemote-ios
DERIVED=$OUT/DerivedData
LOG=$OUT/build.log
mkdir -p "$OUT"
if [ -z "$UDID" ]; then
  # The first paired physical iPhone devicectl can reach, a cabled one before one over Wi-Fi.
  DEVICES=$(mktemp)
  xcrun devicectl list devices --json-output "$DEVICES" >/dev/null 2>&1 || true
  UDID=$(python3 -I - "$DEVICES" <<'PY'
import json, sys
try:
    devices = json.load(open(sys.argv[1]))["result"]["devices"]
except Exception:
    devices = []
phones = [d for d in devices
          if d["hardwareProperties"].get("reality") == "physical"
          and d["hardwareProperties"].get("deviceType") == "iPhone"
          and d["connectionProperties"].get("pairingState") == "paired"
          and d["connectionProperties"].get("transportType")]
phones.sort(key=lambda d: d["connectionProperties"]["transportType"] != "wired")
if phones:
    print(phones[0]["hardwareProperties"]["udid"])
PY
)
  rm -f "$DEVICES"
  if [ -z "$UDID" ]; then
    echo "No iPhone found. Plug one in, unlock it and trust this Mac, or pass UDID=<device>." | tee "$LOG"
    echo BUILD_FAILED >> "$LOG"
    exit 1
  fi
fi
# Signing: with an App Store Connect key in ~/.appstoreconnect/key.env, Xcode can refresh the
# provisioning profile itself (the push capability needs one made for this app) without an account in its settings.
AUTH=(-allowProvisioningUpdates)
if [ -f ~/.appstoreconnect/key.env ]; then
  source ~/.appstoreconnect/key.env
  AUTH+=(-authenticationKeyPath ~/.appstoreconnect/AuthKey_$KEY_ID.p8 -authenticationKeyID $KEY_ID -authenticationKeyIssuerID $ISSUER_ID)
fi
(
  xcodebuild -project VisionRemote.xcodeproj -scheme VisionRemote -configuration Debug \
    -destination 'generic/platform=iOS' -derivedDataPath "$DERIVED" "${AUTH[@]}" build || exit 1
  APP="$DERIVED/Build/Products/Debug-iphoneos/VisionRemote.app"
  BUNDLE_ID=$(/usr/libexec/PlistBuddy -c "Print CFBundleIdentifier" "$APP/Info.plist")
  xcrun devicectl device install app --device "$UDID" "$APP" || exit 1
  xcrun devicectl device process launch --device "$UDID" --terminate-existing "$BUNDLE_ID" || true
  echo BUILD_DONE
) > "$LOG" 2>&1 || true
[ "$(tail -1 "$LOG")" = BUILD_DONE ] || echo BUILD_FAILED >> "$LOG"
