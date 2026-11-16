#!/bin/bash
# Build the hover-legend helper and wrap it as BlackholeLegend.app.
# A real bundle (not a bare binary) is what lets macOS ask, once, whether the
# helper may query Ghostty. Rebuilding changes the ad-hoc signature, so macOS
# will ask again after a rebuild.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release
APP=BlackholeLegend.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/BlackholeLegend "$APP/Contents/MacOS/BlackholeLegend"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.blackholeinstrument.legend</string>
  <key>CFBundleName</key><string>BlackholeLegend</string>
  <key>CFBundleExecutable</key><string>BlackholeLegend</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>BlackholeLegend asks Ghostty which terminal each window is showing, so the black hole legend can show the right session's numbers.</string>
</dict>
</plist>
EOF
codesign --force --sign - "$APP" >/dev/null 2>&1
echo "built $(pwd)/$APP"
