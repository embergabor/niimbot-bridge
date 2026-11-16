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
- **Status back to CUPS.** For jobs that start with the `NIIMBOT/1` header, the proxy writes lines back on the socket: `INFO: …`, `STATE: ±reason`, `ERROR: …`, and finally `NIIMBOT-DONE ok|fail`. The socket backend forwards them to the back-channel (fd 3), and the filter, which stays alive after sending the PDF, echoes them to stderr for cupsd and exits 1 on `fail`, so the job is not marked printed. The proxy repeats its last INFO every 10 s (the backend gives up after 90 s of silence, the filter after 45 s). `report_failures: false` in the config turns `fail` into `ok`. Verified on real cupsd, see Status.
- **Roll info.** After Connect the proxy asks for RfidInfo (0x1A → 0x1B; layout per niimbluelib/niimprint, parsed in `parse_rfid`). `LabelType=Auto` (0, the PPD default) takes the paper type from the roll when it is one of `LABEL_TYPES`, else gaps. Labels-left goes to the log and the queue. `status` prints the parsed roll plus the raw RFID and heartbeat replies.
- **Top offset.** `top_offset_mm` in the config shifts every page down (or up) by whole rows in `run_job`, keeping the page length. Default 0.
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
- ✅ Roll awareness, status reporting to the print queue, and the two-arch installer pipeline are implemented. 15 offline tests, including a simulated CUPS back-channel. Public repo: github.com/embergabor/niimbot-bridge (branch `main`; commits use the GitHub no-reply address, set in the repo's local git config). MIT licence.
- ✅ The GitHub workflow ran green on the first push (2026-11-14): `macos-15` and `macos-15-intel` runners, Python 3.15 and the pinned wheels all work. Its `installer` artifact is a 28 MB unsigned pkg holding both the arm64 and the x86_64 app. Tag `v0.1.0` published the first release (unsigned, 28 MB, both CPU types) the same day; signing is still untried, and the arm64 app has never run on an Apple Silicon Mac.
- ✅ `dist/NiimbotBridge-0.1.0-x86_64.pkg` builds (13 MB; the app is 29 MB). The frozen app passed its smoke tests: `version` loads every library, `render` works, and `serve` plus the real filter carries a job through to the transport ("No Niimbot USB serial port found", with the socket closed cleanly).
- ✅ **USB works on a real B1 (2026-11-14, run from source, 50×30 mm gap roll).** `status` and `print` both work: Connect, RfidInfo (the `parse_rfid` layout is right; the labels-left counter drops by one per print), heartbeat, the whole print sequence, PageEnd and PrintStatus. Orientation, centring and sharpness are correct with the current `black_counts`. The printer also sends unsolicited `0xD3` packets during a print, which are ignored. The first ~0.4 mm at the label's top edge doesn't print; a 0.5 mm down-shift overshot, so `top_offset_mm` (config, `shift_rows`) defaults to 0.
- ✅ **The pkg installs and printing through CUPS works (2026-11-14, USB).** Queue, LaunchAgent and proxy come up; `lp` jobs print; `-n 2` gives two labels (no doubling); `-o BlackLevel=… -o Density=…` arrive; PPD defaults arrive via the filter.
- ✅ **Status back to CUPS verified on real cupsd (2026-11-14).** cupsd gives filters a non-blocking fd 3, so the filter polls it (`sleep 0.2`, 45 s silence limit); the test's fake back-channel is non-blocking too. Observed: the queue shows the proxy's messages ("Waiting for the printer…", "Printing"); a failed job stays in the queue as stopped (`job-completed-with-errors`) with the error text, the printer itself stays idle and accepting; `lp -i <job> -H restart` reprints it (`-H resume` does nothing). The failure was simulated with a bad `address` in the config, not by unplugging.
- `launchctl kickstart -k` restarts only `open -W`, not the app; restart with `pkill -x NiimbotProxy` (KeepAlive brings it back). In `serve` the config is now re-read for every job.
- ✅ **Bluetooth works (2026-11-14, through the LaunchAgent and CUPS).** With USB unplugged, `auto` found `B1-…` by name and connected in ~1–4 s; max write 237 bytes; `pace = 0.01` was fine for a dense dithered two-page PDF (~4.5 s per label). Multi-page PDFs print one label per page.
- Filter quirk (bash 3.2): a `read` that fails on the non-blocking fd 3 leaves `$line` untouched, so the loop clears it first. A status line split across two writes would be dropped; the proxy writes whole lines, so this hasn't been seen.
- ⚠️ **Dither mode prints poorly (known, parked: photos are not a priority).** A Floyd–Steinberg gradient comes out too light in the mid-tones with horizontal streaks, identically over USB and BLE. Ruled out on hardware: BLE data loss, `black_counts` (split, all zeros and `[0, lo, hi]` total all look the same; split stays), and pacing rows by 8 ms. Untested theory: the head under-heats isolated dots. Next things to try if it ever matters: density 5, ordered (Bayer) dither, dither at half resolution with 2×2 dots. Threshold mode (the default) prints cleanly.
- ❌ Untested on hardware: cover-open/cancel mid-print (`0xDA`), a real unplug or power-off during a job, the print dialog in a GUI app. Not confirmed: whether macOS showed a Bluetooth permission prompt for NiimbotProxy.

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
6. GitHub: for the next release bump `__version__` in `niimbot_proxy.py`, then push a matching `v*` tag. Signing and notarization need the user's Apple Developer ID and the secrets listed in the workflow; hardened-runtime entitlements are a best guess until a signed build runs.

## Constraints and preferences
- `README.md` is a user guide only (install steps, how to print, options, troubleshooting, uninstall, credits), by the user's request. Technical, build, config-file (`transport`, `address`, `ble_name`, `wait`, `report_failures`, `top_offset_mm` in `~/Library/Application Support/NiimbotProxy/config.json`) and CLI details belong here, not there.
- Pragmatic and minimal. Single-file daemon; don't add frameworks.
- The end user needs no Python, no Homebrew and no terminal. Everything ships inside the pkg.
- Python ≥ 3.9 (the CLT python): use `from __future__ import annotations`, no `match`.
- Out of scope: B1 Pro two-colour printing, label designer UI.
- References: niimbluelib (MultiMote), printers.niim.blue, AndBondStyle/niimprint.
