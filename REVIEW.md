# Code review — recommendations (2026-11-14)

Review only; nothing here is implemented yet. Line numbers refer to `niimbot_proxy.py` as of this date.
"Verified" means reproduced on this Mac. "Suspected" means read from the code and still needs the B1 or a real CUPS queue to confirm.

Do group A before or alongside the first hardware session: those are the failures most likely to be mistaken for protocol problems.

## Status (updated 2026-11-14, after the implementation pass)

**Done and covered by tests or frozen-app checks:** A1, A2, A3, A4, A5 part 1 (wait for the printer, 60 s in `serve`), A7, A8, A9, A10, A11, B1, B5, C1 (app 41 → 29 MB, pkg 18 → 13 MB), C2, C3, C4, C6, and all of section D.

**Done but only checkable on hardware or a real install:**
- A6 (BLE writes split to the characteristic's maximum; the size is logged on connect).
- A4's cancel command `0xDA` and the error-code names in `ERROR_NAMES` come from niimbluelib from memory. If the B1 ignores or rejects `0xDA`, drop it and keep PrintEnd.
- C5 (queue defaults carried over on upgrade; needs a second install to see).

**Still open:**
- A5 part 2 is now implemented (status lines over the socket, relayed by the filter) and tested against a simulated back-channel, but not against real cupsd.
- B2: the printer's own copy count. Needs hardware.
- B3: keep the BLE connection between jobs. Needs hardware.
- C7: confirm the filter's MIME destination on first install.

**Dropped:** B4 (lazy rendering). Measured at about 7 ms per page, so there is nothing to gain.

## A. Bugs and robustness

| # | Issue | Where | Status | Suggested fix |
|---|---|---|---|---|
| A1 | **Transparent PNGs print solid black.** `convert("L")` drops alpha, and transparent pixels are usually RGB 0,0,0. | `image_to_rows` :171 | Verified | For images with alpha (RGBA, LA, P with transparency), composite onto white before converting. PDFs are unaffected (pdfium fills white). |
| A2 | **Timeouts show an empty error.** `asyncio.wait_for` raises a bare `TimeoutError`, so the log and the notification say "Print failed: " with nothing after it. The custom message at :330 is almost never reached. | `request` :331, `handle` :439 | Verified by reading | Catch the timeout in `request` and raise `PrinterError("printer did not answer command 0x..")`. |
| A3 | **Printer errors during a page are thrown away.** Nothing reads the queue while rows stream (:357–360), and the next `request` empties it (:321–322), including any 0xDB error packet. A lid-open or out-of-paper error then surfaces seconds later as a generic "page end rejected". | `request`, `print_pages` | Suspected | When draining, raise on 0xDB instead of discarding. Also check the queue every N rows while streaming. |
| A4 | **A failed job leaves the printer mid-print.** No PrintEnd or cancel is sent after an exception past PrintStart. The next job may be rejected until the printer is power-cycled. | `print_pages` :351–371 | Suspected | `try/finally`: on failure send the cancel command from niimbluelib (and PrintEnd), best effort, short timeout. |
| A5 | **CUPS thinks failed jobs succeeded.** `handle` swallows the error and closes the socket cleanly, so the job is marked complete and is gone. The only signal is a notification. A printer that is switched off or unplugged loses the job immediately. | `handle` :437–441 | Verified offline (job failed, `nc` exit 0) | Two parts. (1) Wait for the printer: retry `open` for ~60 s before failing, since CUPS shows the job as printing meanwhile. (2) Report failure: send a status line back on the socket before closing; the filter reads it from the CUPS back-channel (fd 3), prints `ERROR: …` and exits non-zero. Part 2 needs checking against real CUPS and its sandbox. |
| A6 | **BLE writes may exceed the negotiated MTU.** A row packet is 61 bytes, written without response in one call. If the link's limit is lower, CoreBluetooth drops or truncates silently, which looks like garbled prints. | `BleTransport.write` :284 | Suspected | Log `client.mtu_size` on connect; split writes to the characteristic's max write-without-response size. |
| A7 | **Notifications garble non-ASCII text.** `json.dumps` emits `ó`, which AppleScript prints literally. Matters on this Mac: system messages are Hungarian. | `notify` :452 | Verified | `json.dumps(..., ensure_ascii=False)`. Also run it via `asyncio.to_thread`; it currently blocks the event loop. |
| A8 | **A malformed `config.json` crash-loops the daemon** every 10 s with nothing visible to the user. | `load_config` :460 | Verified by reading | Catch the parse error, log it, fall back to defaults. |
| A9 | **One odd job option can kill a job.** `shlex.split` raises on an unbalanced quote. CUPS normally escapes quotes, so this is unlikely, but macOS passes document titles in `com.apple.print.*` options. | `parse_job_options` :389 | Suspected | Wrap in `try/except ValueError` and fall back to `text.split()`. |
| A10 | **The serial reader thread is never joined.** After `close`, it can call into a closed event loop at exit of `print`. Harmless today, noisy later. | `SerialTransport` :242–259 | Suspected | Join the thread in `close` (it wakes within the 0.1 s read timeout). |
| A11 | **The log file grows forever.** | `main` :543 | Verified by reading | `RotatingFileHandler`, e.g. 1 MB × 2. |

## B. Print quality and speed

| # | Recommendation | Why | Notes |
|---|---|---|---|
| B1 | **Render PDFs straight to the head width instead of rendering at 203 dpi and resampling.** | The default 50 mm label renders at 400 px and is then LANCZOS-scaled to 384 (:177–180) before thresholding. That softens text and can distort barcode bar widths. Rendering with `scale = min(203/72, 384 / page_width_pt)` lets pdfium rasterise vectors at the final resolution. Decide rotation from the page size before rendering, too. | Biggest quality win. Keep the resample path for PNG input only. Worth a hardware comparison of pdfium's smoothing on vs off for small text. |
| B2 | **Use the printer's own copy count.** | `SetPageSize` already carries a copies field (:356, currently always 1). Today each copy resends the whole bitmap, which over BLE is ~2–3 s per label of pure transfer. | Needs hardware: confirm how PrintStatus counts pages when copies > 1. Fall back to the current loop if the firmware ignores it. |
| B3 | **Keep the BLE connection for a short idle period, and remember the device.** | Every job scans (up to 10 s) and reconnects (:272–279). Printing several labels one by one pays that each time. | Hold the transport open ~30–60 s after a job; cache the discovered address in memory. USB reopen is cheap, so this is BLE-only. |
| B4 | **Render pages lazily.** | All pages are rasterised before the first one prints (:416). The page count for PrintStart is available from the PDF without rendering. | Matters for jobs with dozens of labels; trivial for one or two. |
| B5 | Request grayscale output from pdfium. | Skips an RGB bitmap and a conversion per page. | Small. pypdfium2 5.x takes render flags via keyword options; check the exact names. |

Not worth doing: micro-optimising `black_counts` or the packet parser (well under 50 ms on the longest label), and batching USB row writes (do not change flow behaviour before the protocol is validated).

## C. Packaging

| # | Recommendation | Detail |
|---|---|---|
| C1 | **Trim the app (41 MB → roughly 28 MB).** | Measured in the bundle: Pillow codec libraries the proxy never uses — libavif 4.5 MB, plus libjpeg, libwebp, libtiff, openjp2, lcms2 ≈ 2.9 MB — and OpenSSL (libcrypto 5.2 MB, libssl 1.0 MB), pulled in by `ssl`/`hashlib` that nothing here needs. Exclude the unused PIL plugins and `ssl`/`_ssl`/`_hashlib` in the spec, then rerun the smoke tests. Keep PNG and JPEG. |
| C2 | **Pin dependency versions for release builds.** | `requirements.txt` uses `>=`, so two builds a month apart can differ. Add a constraints file with the exact versions that passed (currently bleak 3.1.0, pillow 13.0.0, pypdfium2 5.15.2, pyserial 3.5, pyinstaller 6.22.3). |
| C3 | **Run the frozen app's smoke tests inside `build.sh`.** | Today only `version` runs. Add `render` on a generated PDF, so a broken freeze fails the build rather than the user's install. This matters most for the future arm64 CI build, which nobody can test by hand. |
| C4 | **Rebuild the venv when the interpreter changes.** | `build/venv` is reused even if `PYTHON=` points somewhere else, silently building with the old one. |
| C5 | **Preserve queue defaults on upgrade.** | `postinstall` always runs `lpadmin -P`, which resets a user's saved default size, density and so on. Only pass `-P` when the queue is new or the PPD changed. |
| C6 | **Handle a second logged-in user.** | The LaunchAgent runs per user, but only one process can bind port 9109. The second one restarts every 10 s forever. Exit quietly, or back off, when the port is taken. |
| C7 | **Confirm the filter's MIME destination on first install.** | The PPD's `cupsFilter2` targets `application/vnd.niimbot-job`, a type CUPS does not know. I expect CUPS to register it on the fly, but have not seen it run. If the queue rejects jobs with a filter or MIME error in `/var/log/cups/error_log`, fall back to the classic `*cupsFilter: "application/vnd.cups-pdf 0 <path>"`. |

## D. Tests

- **Fault injection in `FakeB1`.** The simulated printer only takes the happy path. Add cases for: a 0xDB error mid-page (A3), no reply (A2), PageEnd answering 0 several times, and a job failing after PrintStart (A4). These are the paths that cannot be exercised on the real printer on demand.
- **Cover the new input cases:** transparent PNG (A1), a 50 mm PDF page rendering at exactly 384 px wide (B1).
- **Stop writing outside the test's own directory.** The test regenerates `cups/NiimbotB1.ppd` in the source tree and uses a fixed `/tmp/niimtest`; use a temp directory.

## Suggested order

1. A1, A2, A7, A8, A9, A11 — small, safe, no hardware needed.
2. A3, A4 plus the fault-injection tests — before leaning on hardware results.
3. Hardware session; settle A6, B2, C7 with what it shows.
4. B1, then A5.
5. C1–C5 before the pkg goes to anyone else; B3, B4, C6 as needed.
