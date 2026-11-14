#!/bin/bash
# Builds the self-contained app for this Mac's CPU, then an installer from it.
#   ./packaging/build.sh                    # → dist/NiimbotBridge-<version>-<arch>.pkg
#   PYTHON=/path/to/python3 ./packaging/build.sh
#   APP_ONLY=1 ./packaging/build.sh         # stop after build/dist/NiimbotProxy.app (CI builds one per CPU type,
#                                           #   then make_pkg.sh combines them into one installer)
#   CODESIGN_ID="Developer ID Application: Name (TEAMID)"   sign for distribution (default: ad-hoc)
# The app targets the build machine's CPU and the Python's minimum macOS.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
B="$HERE/build"
V="$B/venv"

echo "==> Build env ($PYTHON)"
# Rebuild the venv if it was made by a different interpreter than the one asked for now.
WANT=$("$PYTHON" -c 'import sys; print(sys.version, sys.base_prefix)')
[ "$(cat "$V/.built-with" 2>/dev/null)" = "$WANT" ] || rm -rf "$V"
[ -x "$V/bin/python" ] || { "$PYTHON" -m venv "$V" && echo "$WANT" > "$V/.built-with"; }
"$V/bin/pip" install -q --disable-pip-version-check -c "$HERE/packaging/constraints.txt" \
  -r "$HERE/requirements.txt" pyinstaller

VERSION=$(sed -n -E 's/^__version__ = "([^"]+)"/\1/p' "$HERE/niimbot_proxy.py")
ARCH=$(uname -m)
MIN_MACOS=$("$V/bin/python" - <<'EOF'
import sysconfig
v = sysconfig.get_platform().split("-")[1]                       # macosx-15.0-x86_64 → 15.0
print(max(v, "11.0", key=lambda s: tuple(map(int, s.split(".")))))
EOF
)
echo "    version $VERSION, $ARCH, macOS ≥ $MIN_MACOS"

echo "==> Tests"
"$V/bin/python" "$HERE/tests/test_proxy.py"

echo "==> App"
SIGN="${CODESIGN_ID:--}"                                           # "-" = ad-hoc
NIIMBOT_VERSION="$VERSION" NIIMBOT_MIN_MACOS="$MIN_MACOS" NIIMBOT_CODESIGN_ID="${CODESIGN_ID:-}" \
  "$V/bin/pyinstaller" --noconfirm --clean --log-level WARN \
  --distpath "$B/dist" --workpath "$B/work" "$HERE/packaging/NiimbotProxy.spec"
APP="$B/dist/NiimbotProxy.app"
install -m 755 "$HERE/packaging/uninstall.sh" "$APP/Contents/Resources/uninstall.sh"
if [ "$SIGN" = "-" ]; then
  codesign --force -s - "$APP"
else   # hardened runtime + timestamp are required for notarization
  codesign --force --options runtime --timestamp --entitlements "$HERE/packaging/entitlements.plist" -s "$SIGN" "$APP"
fi
codesign --verify --deep --strict "$APP"
"$APP/Contents/MacOS/NiimbotProxy" version

echo "==> Smoke test of the frozen app"
SMOKE="$B/smoke" && rm -rf "$SMOKE" && mkdir -p "$SMOKE"
"$V/bin/python" - "$SMOKE" <<'EOF'
import sys
from PIL import Image, ImageDraw
d = sys.argv[1]
im = Image.new("RGB", (400, 240), "white")
ImageDraw.Draw(im).rectangle([10, 10, 389, 229], outline="black", width=4)
im.save(f"{d}/label.pdf", resolution=203)
im.convert("RGBA").save(f"{d}/label.png")
im.save(f"{d}/label.jpg")
EOF
for f in label.pdf label.png label.jpg; do
  "$APP/Contents/MacOS/NiimbotProxy" render "$SMOKE/$f" "$SMOKE/$f.out.png" --dither >/dev/null
  [ -s "$SMOKE/$f.out.png" ] || { echo "smoke test failed: $f"; exit 1; }
done
echo "    render ok: pdf, png, jpg"

[ -z "${APP_ONLY:-}" ] || { echo; echo "Built $APP"; exit 0; }
"$HERE/packaging/make_pkg.sh" "$HERE/dist/NiimbotBridge-$VERSION-$ARCH.pkg" "$APP"
