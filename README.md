# niimbot-proxy

Lets a Niimbot B1 show up as a normal printer on macOS. Any app can print to it (Preview, Pages, a browser, `lp`), over USB or Bluetooth.

```
App → print dialog → CUPS queue "Niimbot_B1" → niimbot-filter (passes options through)
    → socket://127.0.0.1:9109 → NiimbotProxy.app → render PDF at 203 dpi, 1-bit
    → B1 protocol → USB (CDC serial, VID 0x3513) or Bluetooth LE
```

One code path handles both transports. Only the byte pipe differs: pyserial for USB, bleak for BLE.
In `auto` mode the proxy uses USB when the printer is plugged in, and Bluetooth otherwise.

## Install

Double-click `NiimbotBridge-<version>-<arch>.pkg` and enter your admin password. Nothing else needs installing: Python and every library are bundled inside the app.

It installs:

| Path | What |
|---|---|
| `/Applications/NiimbotProxy.app` | The proxy (background-only app, no Dock icon) |
| `/Library/Printers/NiimbotB1/` | CUPS filter and PPD |
| `/Library/LaunchAgents/local.niimbot-proxy.plist` | Starts the proxy at login and keeps it running |
| CUPS queue **Niimbot B1** | Created by the installer |

The package isn't signed with an Apple Developer ID. On a Mac that downloaded it, Gatekeeper blocks the first open: right-click → **Open**, or allow it under System Settings → Privacy & Security.

## Use

- **Print dialog:** pick **Niimbot B1**. Set Paper Size to the label size: 18 common B1 sizes from 20×10 to 50×80 mm, 50×30 by default, and custom sizes work too. Under Printer Features you can set:
  - *Print Density* (1–5): how dark the dots are
  - *Paper Type*: *Automatic* (the default) reads it from the RFID tag on a genuine Niimbot roll and falls back to gaps; or choose gaps, black mark, continuous or transparent yourself
  - *Halftoning*: threshold for text and barcodes, dither for photos
  - *Black Level* (threshold mode): which shades turn black. *Normal* prints only darker than mid-grey. *Dark* also prints mid-tones. *Darkest* also prints pale colours such as yellow and light grey. *Light* prints only strong darks.
- **Presets per label roll:** in the print dialog, choose Presets → *Save Current Settings as Preset…* (for example "40×30 barcodes").
- **CLI:** `lp -d Niimbot_B1 -o media=Custom.40x30mm -o Density=4 -o BlackLevel=Dark label.pdf`
- **The app's own CLI** (`P=/Applications/NiimbotProxy.app/Contents/MacOS/NiimbotProxy`):
  - `$P print label.png --transport usb` prints directly, without CUPS
  - `$P render label.pdf preview.png [--black-level darkest]` writes exactly what would be printed, as a PNG
  - `$P scan` lists USB ports and BLE devices
  - `$P status` shows what the printer reports about the loaded roll (labels left, paper type)
  - `$P version` shows the bundled library versions

Make the page the size of the label (width = across the printhead, max 50 mm). Pages wider than 48 mm are scaled to fit the 384 px head. A landscape page that only fits when rotated is rotated automatically.

## Config

Optional, per user: `~/Library/Application Support/NiimbotProxy/config.json`
```json
{ "transport": "auto", "ble_name": "B1", "address": null }
```
- `transport`: `auto` | `usb` | `ble`
- `address`: a `/dev/cu.usbmodem…` path, or the BLE identifier from `scan` (on macOS this is a UUID, not a MAC address)
- `wait`: seconds to wait for a printer that is off or unplugged before a job fails (default 60)
- `report_failures`: `true` (default) keeps failed jobs in the print queue; `false` marks them done

After editing it: `launchctl kickstart -k gui/$(id -u)/local.niimbot-proxy`

## Logs and troubleshooting

- `tail -f ~/Library/Logs/NiimbotProxy.log`
- If the printer is off or unplugged, the job waits up to a minute for it (you get a notification), then fails.
- The print queue shows progress ("Printed label 2 of 4") and, with a tagged roll, how many labels are left.
- If a print fails, the job stays in the print queue with the reason, so you can fix the problem and resume it. You also get a notification. Cover-open, out-of-paper and offline show as printer status.
  If a failed job ever blocks the queue in a way you don't want, set `"report_failures": false` in the config: failed jobs then count as done, as before.
- PNG and JPEG files print directly with `print`; transparent areas print as blank paper.
- The CUPS job stays "printing" until the label has physically come out.
- **Bluetooth:** the first BLE print triggers a Bluetooth permission prompt for *NiimbotProxy*. Allow it. If the phone app is connected to the printer, disconnect it first.
- **Blank or shifted output:** try the other Paper Type, or a higher density.

## Uninstall

```bash
sudo /Applications/NiimbotProxy.app/Contents/Resources/uninstall.sh
```
This leaves your config and log in `~/Library`.

## Build

```bash
./packaging/build.sh                         # → dist/NiimbotBridge-<version>-<arch>.pkg
PYTHON=/path/to/python3 ./packaging/build.sh # build with a specific Python
```
That builds for the Mac you run it on. **One installer for both Apple Silicon and Intel** comes from GitHub Actions (`.github/workflows/build.yml`): it builds the app on each kind of Mac, then `packaging/make_pkg.sh` puts both into one pkg, and the installer keeps the build that matches the Mac. Pushing a tag like `v0.1.0` publishes the pkg as a release.

**Signing and notarization** are off until you add an Apple Developer ID. The workflow file lists the repository secrets it needs; locally, set `CODESIGN_ID`, `INSTALLER_ID` and the `NOTARY_*` variables described at the top of `build.sh` and `make_pkg.sh`.

The build creates its own venv under `build/`, runs the tests, freezes the app with PyInstaller, smoke-tests the frozen app, and wraps it with `pkgbuild` and `productbuild`.
Library versions are pinned in `packaging/constraints.txt`; change them there on purpose, then rebuild.
The app targets the build machine's CPU, and the minimum macOS of the Python you build with. Homebrew Python only covers the current macOS. python.org Python goes back to 11.0+.

For development, run from source with the build venv: `build/venv/bin/python niimbot_proxy.py -v print …`.

## Notes

- Protocol: the "B1" print task from niimbluelib (MultiMote), documented at printers.niim.blue. It is also compatible with niimprint (AndBondStyle).
- Single colour only. B1 Pro red/black printing is not implemented.
- `tests/test_proxy.py` runs the whole path against a simulated printer: filter → socket → daemon → packets → decoded image.

## Licence

MIT, see `LICENSE`.
