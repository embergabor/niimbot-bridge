#!/bin/bash
# CI only: puts the Developer ID certificates into a throwaway keychain so codesign/productbuild find them.
# Does nothing when no certificate is configured, so unsigned builds keep working.
#   MACOS_CERT_P12       base64 of a .p12 holding "Developer ID Application" and "Developer ID Installer"
#   MACOS_CERT_PASSWORD  its password
set -euo pipefail
[ -n "${MACOS_CERT_P12:-}" ] || { echo "No signing certificate configured: building unsigned."; exit 0; }
KC="$RUNNER_TEMP/niimbot-build.keychain-db"
PW=$(uuidgen)
security create-keychain -p "$PW" "$KC"
security set-keychain-settings -lut 21600 "$KC"
security unlock-keychain -p "$PW" "$KC"
echo "$MACOS_CERT_P12" | base64 --decode > "$RUNNER_TEMP/cert.p12"
security import "$RUNNER_TEMP/cert.p12" -P "$MACOS_CERT_PASSWORD" -A -f pkcs12 -k "$KC"
rm -f "$RUNNER_TEMP/cert.p12"
security set-key-partition-list -S apple-tool:,apple: -k "$PW" "$KC" >/dev/null
security list-keychains -d user -s "$KC" login.keychain-db
echo "Signing certificate imported."
