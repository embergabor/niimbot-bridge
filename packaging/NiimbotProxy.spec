# PyInstaller spec → dist/NiimbotProxy.app (onedir: Python runs in the app's own
# process, so macOS attributes the Bluetooth permission to NiimbotProxy).
# Built by packaging/build.sh, which sets NIIMBOT_VERSION / NIIMBOT_MIN_MACOS / NIIMBOT_CODESIGN_ID.
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = os.environ.get("NIIMBOT_VERSION", "0.0.0")
BUNDLE_ID = "local.niimbot-proxy"

a = Analysis(
    [os.path.join(ROOT, "niimbot_proxy.py")],
    hiddenimports=collect_submodules("serial.tools") + collect_submodules("bleak.backends.corebluetooth"),
    # Unused weight: OpenSSL (nothing here does TLS or needs OpenSSL hashes) and Pillow codecs
    # beyond PNG/JPEG. Pillow skips plugins that fail to import.
    excludes=["tkinter", "_tkinter", "unittest", "pydoc", "test",
              "ssl", "_ssl", "_hashlib",
              "PIL.AvifImagePlugin", "PIL._avif", "PIL.WebPImagePlugin", "PIL._webp",
              "PIL.ImageCms", "PIL._imagingcms", "PIL.ImageTk", "PIL._imagingtk", "PIL.ImageQt"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NiimbotProxy",
    console=False,
    argv_emulation=False,
    # Ad-hoc by default; a Developer ID (set by build.sh) also turns on the hardened runtime.
    codesign_identity=os.environ.get("NIIMBOT_CODESIGN_ID") or None,
    entitlements_file=os.path.join(ROOT, "packaging", "entitlements.plist")
    if os.environ.get("NIIMBOT_CODESIGN_ID") else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NiimbotProxy")
app = BUNDLE(
    coll,
    name="NiimbotProxy.app",
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        "CFBundleName": "NiimbotProxy",
        "CFBundleDisplayName": "Niimbot Proxy",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSUIElement": True,                     # no Dock icon / menu bar
        "LSMinimumSystemVersion": os.environ.get("NIIMBOT_MIN_MACOS", "11.0"),
        "NSBluetoothAlwaysUsageDescription": "Connects to the Niimbot label printer over Bluetooth.",
    },
)
