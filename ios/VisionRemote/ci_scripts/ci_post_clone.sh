#!/bin/sh
# Xcode Cloud supplies these as secret workflow environment variables. Keep their values
# out of the repository and logs; Local.xcconfig is gitignored.
set -eu

: "${VISION_TEAM_ID:?Set VISION_TEAM_ID as a secret in the Xcode Cloud workflow environment}"
: "${VISION_BUNDLE_PREFIX:?Set VISION_BUNDLE_PREFIX as a secret in the Xcode Cloud workflow environment}"
: "${CI_PRIMARY_REPOSITORY_PATH:?Xcode Cloud did not provide the checkout path}"

cat > "$CI_PRIMARY_REPOSITORY_PATH/ios/VisionRemote/Local.xcconfig" <<EOF
DEVELOPMENT_TEAM = $VISION_TEAM_ID
BUNDLE_ID_PREFIX = $VISION_BUNDLE_PREFIX
EOF
