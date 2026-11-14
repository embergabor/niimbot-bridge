#!/bin/bash
# Wraps one or more builds of NiimbotProxy.app (one per CPU type) into a single installer.
#   ./packaging/make_pkg.sh OUT.pkg path/to/NiimbotProxy.app [path/to/other-arch/NiimbotProxy.app]
# The installer carries every given build; its postinstall keeps the one matching the Mac.
# Optional, for distribution:
#   INSTALLER_ID="Developer ID Installer: Name (TEAMID)"   sign the pkg
#   NOTARY_KEY=/path/AuthKey.p8 NOTARY_KEY_ID=… NOTARY_ISSUER=…   notarize and staple (needs a signed pkg)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1"; shift
[ $# -ge 1 ] || { echo "usage: $0 OUT.pkg APP [APP…]"; exit 1; }
B="$HERE/build"
ID="local.niimbot-proxy"
R="$B/root"
STAGE="Library/Application Support/NiimbotProxy/apps"
plist() { /usr/libexec/PlistBuddy -c "Print :$2" "$1/Contents/Info.plist"; }

echo "==> Package"
rm -rf "$R" && mkdir -p "$R/Library/Printers/NiimbotB1" "$R/Library/LaunchAgents"
ARCHS="" VERSION="" MIN_MACOS="0"
for APP in "$@"; do
  arch=$(lipo -archs "$APP/Contents/MacOS/NiimbotProxy")
  case "$arch" in arm64|x86_64) ;; *) echo "unexpected architecture '$arch' in $APP"; exit 1 ;; esac
  [ ! -e "$R/$STAGE/$arch" ] || { echo "two builds for $arch given"; exit 1; }
  v=$(plist "$APP" CFBundleShortVersionString)
  [ -z "$VERSION" ] || [ "$VERSION" = "$v" ] || { echo "version mismatch: $VERSION vs $v"; exit 1; }
  VERSION="$v"
  m=$(plist "$APP" LSMinimumSystemVersion)
  MIN_MACOS=$(printf '%s\n%s\n' "$MIN_MACOS" "$m" | sort -t. -k1,1n -k2,2n | tail -1)
  mkdir -p "$R/$STAGE/$arch"
  ditto "$APP" "$R/$STAGE/$arch/NiimbotProxy.app"
  ARCHS="${ARCHS:+$ARCHS,}$arch"
  echo "    $arch: version $v, macOS ≥ $m"
done

python3 "$HERE/cups/make_ppd.py" "$R/Library/Printers/NiimbotB1/NiimbotB1.ppd" >/dev/null
chmod 644 "$R/Library/Printers/NiimbotB1/NiimbotB1.ppd"
install -m 755 "$HERE/cups/niimbot-filter"  "$R/Library/Printers/NiimbotB1/niimbot-filter"
install -m 644 "$HERE/packaging/$ID.plist"  "$R/Library/LaunchAgents/$ID.plist"
chmod 755 "$HERE/packaging/scripts/"*
# Strip quarantine/provenance xattrs, or they ship as ._ entries and land on the installed files.
xattr -cr "$R" 2>/dev/null || true
export COPYFILE_DISABLE=1

# Don't let Installer "relocate" the app to some other copy it finds on disk.
pkgbuild --analyze --root "$R" "$B/component.plist" >/dev/null
python3 - "$B/component.plist" <<'EOF'
import plistlib, sys
p = sys.argv[1]
items = plistlib.load(open(p, "rb"))
for it in items:
    it["BundleIsRelocatable"] = False
    it["BundleIsVersionChecked"] = False
plistlib.dump(items, open(p, "wb"))
EOF

pkgbuild --quiet --root "$R" --component-plist "$B/component.plist" --scripts "$HERE/packaging/scripts" \
  --identifier "$ID" --version "$VERSION" --install-location / --ownership recommended "$B/NiimbotProxy.pkg"

cat > "$B/distribution.xml" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>Niimbot Bridge $VERSION</title>
  <options customize="never" require-scripts="true" hostArchitectures="$ARCHS"/>
  <domains enable_anywhere="false" enable_currentUserHome="false" enable_localSystem="true"/>
  <volume-check><allowed-os-versions><os-version min="$MIN_MACOS"/></allowed-os-versions></volume-check>
  <choices-outline><line choice="default"><line choice="$ID"/></line></choices-outline>
  <choice id="default"/>
  <choice id="$ID" visible="false"><pkg-ref id="$ID"/></choice>
  <pkg-ref id="$ID" version="$VERSION" onConclusion="none">NiimbotProxy.pkg</pkg-ref>
</installer-gui-script>
EOF
mkdir -p "$(dirname "$OUT")"
if [ -n "${INSTALLER_ID:-}" ]; then
  productbuild --quiet --sign "$INSTALLER_ID" --timestamp --distribution "$B/distribution.xml" --package-path "$B" "$OUT"
else
  productbuild --quiet --distribution "$B/distribution.xml" --package-path "$B" "$OUT"
fi

if [ -n "${NOTARY_KEY:-}" ]; then
  [ -n "${INSTALLER_ID:-}" ] || { echo "notarization needs a signed pkg (set INSTALLER_ID)"; exit 1; }
  echo "==> Notarize"
  xcrun notarytool submit "$OUT" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER" --wait
  xcrun stapler staple "$OUT"
fi
echo
echo "Built $OUT ($(du -h "$OUT" | cut -f1)) for $ARCHS, macOS ≥ $MIN_MACOS${INSTALLER_ID:+, signed}${NOTARY_KEY:+, notarized}"
