# niimbot-proxy — handover

## Goal
Make a Niimbot B1 label printer work as a normal macOS printer, over **USB or Bluetooth**.
Any app should be able to print to it through the standard print dialog.
Keep the solution pragmatic: no GUI or label designer, just a "proxy" that sits behind CUPS.
**The end user installs nothing else.** They get one `.pkg`, and the app bundles Python and every library.

## Architecture
```
App → print dialog → CUPS queue "Niimbot_B1"
    → cups/niimbot-filter   (bash; emits a "NIIMBOT/1 copies=N <opts>\n" header, then the PDF unchanged,
                             then relays the proxy's status lines from the back-channel to CUPS)
    → socket://127.0.0.1:9109   (built-in CUPS socket backend; waits for the peer to close)
    → NiimbotProxy.app = frozen niimbot_proxy.py serve  (asyncio; one job at a time; closes the socket only after the label has printed)
    → pypdfium2 render @203 dpi → 1-bit, centred on the 384 px head
    → B1 print task → SerialTransport (pyserial, VID 0x3513) | BleTransport (bleak, char bef8d6c9-…)
```
- **Why not a custom backend?** `/usr/libexec/cups/backend` is SIP-protected. Filters in `/Library/Printers/...` are allowed via an absolute path in `*cupsFilter2`.
- **Packaging.** PyInstaller freezes the app in onedir mode (`packaging/NiimbotProxy.spec`), so Python runs inside the app's own process. The app's Info.plist carries `NSBluetoothAlwaysUsageDescription` and `LSUIElement`, and the app is ad-hoc signed. `packaging/build.sh` wraps it with `pkgbuild` and `productbuild` into `dist/NiimbotBridge-<ver>-<arch>.pkg`. The bundle is marked non-relocatable, and quarantine xattrs are stripped from the payload.
- **Install layout.** `/Applications/NiimbotProxy.app`, `/Library/Printers/NiimbotB1/{niimbot-filter,NiimbotB1.ppd}`, and `/Library/LaunchAgents/local.niimbot-proxy.plist` (for all users). The pkg's `postinstall` runs `lpadmin` and bootstraps the agent for every logged-in GUI user. `preinstall` stops a running instance first. The uninstaller is at `NiimbotProxy.app/Contents/Resources/uninstall.sh`.
- **Why `open -W -g -a` in the LaunchAgent?** Launching through LaunchServices makes the app the TCC-responsible process for Bluetooth. Started with no arguments, the frozen app defaults to `serve` and logs to `~/Library/Logs/NiimbotProxy.log`.
- **Arch / macOS floor.** The app targets the build host's arch and the build Python's minimum macOS (`sysconfig.get_platform()`, floor 11.0). The current build Mac is Intel with Homebrew Python 3.15, so the build is **x86_64, macOS ≥ 15**. Apple Silicon needs a build on an arm64 Mac. Universal2 would need python.org's universal2 Python plus fused wheels (pillow and pypdfium2 ship per-arch wheels).
- **Option handling.** Defaults set on the queue live in the PPD, and the filter greps `*Default…` lines from `$PPD`. Explicit job options come later on the header line and take precedence.
- **Black level.** The `BlackLevel` option (Light/Normal/Dark/Darkest) maps to a grey threshold of 80/128/176/232 (`BLACK_LEVELS` in the daemon) and applies in threshold mode only. Darkest is chosen so that yellow (≈226) prints and #f0f0f0 backgrounds don't.
- **Rendering.** PDFs are rasterised by pdfium directly at the final size (`_fit` decides rotation and scale from the page size), in grayscale, with no resampling. Only image files go through the PIL rotate and resize path. Transparent pixels are flattened onto white.
- **Failure handling.** `Proxy.connect` retries opening the printer for `wait` seconds (serve 60, print 0). `request` raises `PrinterTimeout` or `PrinterError`. A `0xDB` packet is never dropped: `_drain` raises on it, and row streaming checks every 16 packets. After PrintStart, any failure runs `_abort`. If port 9109 is taken, `serve` retries every 30 s instead of exiting.
- **Status back to CUPS.** For jobs that start with the `NIIMBOT/1` header, the proxy writes lines back on the socket: `INFO: …`, `STATE: ±reason`, `ERROR: …`, and finally `NIIMBOT-DONE ok|fail`. The socket backend forwards them to the back-channel (fd 3), and the filter, which stays alive after sending the PDF, echoes them to stderr for cupsd and exits 1 on `fail`, so the job is not marked printed. The proxy repeats its last INFO every 10 s (the backend gives up after 90 s of silence, the filter after 45 s). `report_failures: false` in the config turns `fail` into `ok`. **Unverified against real cupsd**: that fd 3 reaches the filter, that it sees EOF when the backend exits, and what CUPS does with the job (expected: job stopped, resumable).
- **Roll info.** After Connect the proxy asks for RfidInfo (0x1A → 0x1B; layout per niimbluelib/niimprint, parsed in `parse_rfid`). `LabelType=Auto` (0, the PPD default) takes the paper type from the roll when it is one of `LABEL_TYPES`, else gaps. Labels-left goes to the log and the queue. `status` prints the parsed roll plus the raw RFID and heartbeat replies.
- **Copies.** `*cupsManualCopies: False`, so the daemon prints copies itself by looping pages; `PrintStart.totalPages = pages × copies`.

## Files
| File | Contents |
|---|---|
| `niimbot_proxy.py` | Everything: protocol, packet parser, raster, transports, B1Printer, server, CLI (`serve`/`print`/`render`/`scan`/`version`), `__version__` |
| `cups/niimbot-filter` | CUPS filter (bash 3.2 as shipped with macOS, BSD sed) |
| `cups/make_ppd.py` → `cups/NiimbotB1.ppd` | Generated PPD: 18 label sizes, custom size, Density, LabelType, Dither, BlackLevel. Size names use the Adobe standard form `30x50mmRotated.Fullbleed`, never `wNNNhNNN` (CUPS reads those as points). Passes `cupstestppd`, apart from the missing filter on a machine where it isn't installed. |
| `packaging/build.sh` | Build venv `build/venv` → tests → PyInstaller → smoke test → calls `make_pkg.sh`. `APP_ONLY=1` stops after the app. `CODESIGN_ID` signs with hardened runtime. |
| `packaging/make_pkg.sh` | One or more per-arch apps → one pkg. Payload stages them under `/Library/Application Support/NiimbotProxy/apps/<arch>/`; `postinstall` moves the matching one to `/Applications` and deletes the staging. Optional `INSTALLER_ID`, `NOTARY_*`. |
| `.github/workflows/build.yml`, `packaging/ci_keychain.sh`, `entitlements.plist` | CI: arm64 + Intel app builds, combined pkg, release on `v*` tags; signing only when secrets exist |
| `packaging/NiimbotProxy.spec`, `local.niimbot-proxy.plist`, `scripts/{pre,post}install`, `uninstall.sh` | App spec, LaunchAgent, pkg scripts, uninstaller |
| `tests/test_proxy.py` | Offline end-to-end test against a simulated B1 (`build/venv/bin/python tests/test_proxy.py`) |

## Protocol (B1 print task, from niimbluelib / printers.niim.blue)
Packets are `55 55 cmd len data xor AA AA`, where xor = cmd ^ len ^ data…. Connect (0xC1) carries an extra `03` prefix.

Job sequence:
1. Connect `[1]`
2. SetDensity 0x21 `[d]`
3. SetLabelType 0x23 `[t]`
4. PrintStart 0x01 `>H5B (total,0,0,0,0,color)`
5. Per page:
   1. PageStart 0x03 `[1]`
   2. SetPageSize 0x13 `>HHH (rows, cols=384, 1)`
   3. Rows: 0x85 `>H3BB` (row, c1, c2, c3, repeat) followed by 48 bytes, or 0x84 `>HB` for blank runs
   4. PageEnd 0xE3 `[1]`, retried while it answers 0
   5. Poll PrintStatus 0xA3 `[1]` until the page index (u16) ≥ pages done
6. PrintEnd 0xF3 `[1]`

Response ids are in the `RESPONSE` dict. 0xDB means a printer error.

## Status
- ✅ Offline test passes: filter → socket → daemon → packets → reassembled image is correct. Also passes on the Mac (2026-11-14).
- ✅ The parser resyncs on junk and false `55 55` headers.
- ✅ Review pass implemented (see `REVIEW.md` status): error handling, wait-for-printer, direct-size PDF rendering, BLE write splitting, trimmed bundle, pinned versions. 11 offline tests, including simulated printer faults.
- ✅ Roll awareness, status reporting to the print queue, and the two-arch installer pipeline are implemented. 15 offline tests, including a simulated CUPS back-channel. Public repo: github.com/embergabor/niimbot-bridge (branch `main`; commits use the GitHub no-reply address, set in the repo's local git config). No LICENSE file yet.
- ✅ The GitHub workflow ran green on the first push (2026-11-14): `macos-15` and `macos-15-intel` runners, Python 3.15 and the pinned wheels all work. Its `installer` artifact is a 28 MB unsigned pkg holding both the arm64 and the x86_64 app. Releases on `v*` tags and signing are still untried.
- ✅ `dist/NiimbotBridge-0.1.0-x86_64.pkg` builds (13 MB; the app is 29 MB). The frozen app passed its smoke tests: `version` loads every library, `render` works, and `serve` plus the real filter carries a job through to the transport ("No Niimbot USB serial port found", with the socket closed cleanly).
- ❌ **The pkg has never been installed, and nothing has run on real hardware.** This is the next job.

## Open review items
`REVIEW.md` holds the 2026-11-14 code review with a status section at the top. Most of it is implemented. Open: CUPS failure reporting (A5 part 2), native copies (B2), BLE connection reuse (B3), MIME check (C7). Unverified guesses to check on hardware: cancel command `0xDA`, the error-code names, BLE write splitting.

## Next steps (in order)
1. Plug in the B1. Install `dist/NiimbotBridge-*.pkg`; it needs an admin password, so the user does it. Check that `lpstat -p Niimbot_B1` shows the queue and `~/Library/Logs/NiimbotProxy.log` shows "listening".
2. USB: `/Applications/NiimbotProxy.app/Contents/MacOS/NiimbotProxy -v print test.png --transport usb`. Use `render` to check the image itself.
3. Validate on hardware. If something is wrong, these are the likely suspects:
   - **Black-pixel counts** (`black_counts`, split mode). The spec contradicts itself here. If prints are blank or garbled, try all zeros, which niimprint uses.
   - **Connect reply**: the code tolerates a timeout.
   - **PageEnd / PrintStatus semantics** on the B1 firmware.
   - **Print orientation.** B1 direction is "top"; the code does no rotation except for wide landscape pages.
   - **BLE pacing**: `BleTransport.pace = 0.01`. The log shows the max write size on connect.
   - **Roll info**: run `NiimbotProxy status` first and compare the parsed roll with the raw RFID reply. If Auto picks the wrong paper type, set it by hand.
   - **Cancel on failure**: `_abort` sends `0xDA` then PrintEnd. Trigger it by opening the lid mid-print and check the log and the next job.
4. Print from Preview through CUPS. Watch the queue window: it should show progress, and a job sent with the printer off should wait, fail with a reason and stay in the queue. Check `/var/log/cups/error_log` if the job hangs for ~45 s after printing (back-channel not reaching the filter). Verify that page sizes show in the dialog with readable names, that `-o media=Custom.40x30mm` works, that options arrive (including BlackLevel) (see `INFO: niimbot-filter …` in `/var/log/cups/error_log` or the job log), and that copies aren't doubled. If copies are doubled, flip `cupsManualCopies`.
5. BLE: check that the TCC prompt appears for NiimbotProxy.app. `scan` run from a terminal attributes Bluetooth to the terminal app, not to NiimbotProxy, so test BLE through the LaunchAgent and CUPS path. If no prompt appears, try launchd running `Contents/MacOS/NiimbotProxy` directly.
6. GitHub: tag `v0.1.0` after the hardware test to publish a release (untried). Signing and notarization need the user's Apple Developer ID and the secrets listed in the workflow; hardened-runtime entitlements are a best guess until a signed build runs. Pick a licence.

## Constraints and preferences
- Pragmatic and minimal. Single-file daemon; don't add frameworks.
- The end user needs no Python, no Homebrew and no terminal. Everything ships inside the pkg.
- Python ≥ 3.9 (the CLT python): use `from __future__ import annotations`, no `match`.
- Out of scope: B1 Pro two-colour printing, label designer UI.
- References: niimbluelib (MultiMote), printers.niim.blue, AndBondStyle/niimprint.
