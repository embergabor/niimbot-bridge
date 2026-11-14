#!/usr/bin/env python3
"""niimbot-proxy — use a Niimbot B1 as a normal macOS printer.

    Any app ─▶ macOS print dialog ─▶ CUPS queue "Niimbot_B1"
          ─▶ niimbot-filter (adds job options header)
          ─▶ socket://127.0.0.1:9109 ─▶ this daemon
          ─▶ PDF rasterised @203 dpi, 1-bit ─▶ Niimbot B1 protocol
          ─▶ USB (CDC serial)  or  Bluetooth LE   (auto-selected)

Protocol per niimbluelib / printers.niim.blue ("B1" print task).

CLI (from source, or the bundled app: /Applications/NiimbotProxy.app/Contents/MacOS/NiimbotProxy):
    niimbot_proxy.py serve   [--transport auto|usb|ble] [--address X] [--port 9109]
    niimbot_proxy.py print   FILE.pdf|png [--density 3] [--label-type 1] [--dither] [--copies N]
    niimbot_proxy.py render  FILE.pdf|png OUT.png   # preview exactly what would be printed
    niimbot_proxy.py scan    # list USB ports + BLE devices
    niimbot_proxy.py version # versions of the bundled libraries
The bundled app started without arguments (LaunchAgent / Finder) runs `serve`
and logs to ~/Library/Logs/NiimbotProxy.log.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import logging.handlers
import os
import shlex
import struct
import subprocess
import sys
import threading
from pathlib import Path

__version__ = "0.1.0"
log = logging.getLogger("niimbot")
FROZEN = getattr(sys, "frozen", False)          # running inside NiimbotProxy.app (PyInstaller)
LOG_FILE = Path.home() / "Library/Logs/NiimbotProxy.log"

HEAD_PX = 384              # B1 printhead: 48 mm @ 203 dpi
DPI = 203
USB_VID = 0x3513           # Niimbot USB vendor id (B1 = 0x3513:0x0002)
BLE_CHAR = "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"
DEFAULT_PORT = 9109
MAGIC = b"NIIMBOT/1 "
SUPPORT_DIR = Path.home() / "Library/Application Support/NiimbotProxy"


# ───────────────────────────── protocol ─────────────────────────────

class Cmd:
    PRINT_START = 0x01
    PAGE_START = 0x03
    SET_PAGE_SIZE = 0x13
    RFID_INFO = 0x1A
    SET_DENSITY = 0x21
    SET_LABEL_TYPE = 0x23
    EMPTY_ROW = 0x84
    BITMAP_ROW = 0x85
    PRINT_STATUS = 0xA3
    CONNECT = 0xC1
    PRINT_CANCEL = 0xDA        # per niimbluelib; unverified on B1 hardware
    HEARTBEAT = 0xDC
    PAGE_END = 0xE3
    PRINT_END = 0xF3


IN_PRINT_ERROR = 0xDB
RESPONSE = {
    Cmd.CONNECT: 0xC2, Cmd.SET_DENSITY: 0x31, Cmd.SET_LABEL_TYPE: 0x33,
    Cmd.PRINT_START: 0x02, Cmd.PAGE_START: 0x04, Cmd.SET_PAGE_SIZE: 0x14,
    Cmd.PAGE_END: 0xE4, Cmd.PRINT_STATUS: 0xB3, Cmd.PRINT_END: 0xF4, Cmd.PRINT_CANCEL: 0xD0,
    Cmd.RFID_INFO: 0x1B, Cmd.HEARTBEAT: (0xDD, 0xDE, 0xDF, 0xD9),     # heartbeat reply id varies by firmware
}
# Error codes per niimbluelib (unverified on B1 hardware); the hex code is always shown too.
ERROR_NAMES = {1: "cover open", 2: "out of paper", 3: "low battery", 4: "battery fault", 5: "cancelled",
               6: "data error", 7: "overheated", 8: "paper feed problem", 9: "printer busy"}
# → IPP printer-state-reasons, which the macOS print queue shows in its own words.
ERROR_REASONS = {1: "cover-open", 2: "media-empty-error"}
STATE_REASONS = "cover-open,media-empty-error,offline-report"       # everything we ever set
# Paper types the B1 accepts (SetLabelType). 0 = "Auto": take it from the roll's RFID tag.
LABEL_TYPES = {1: "gaps", 2: "black mark", 3: "continuous", 5: "transparent"}


class PrinterError(Exception):
    def __init__(self, msg: str = "", reason: str = ""):
        super().__init__(msg)
        self.reason = reason            # IPP printer-state-reason, if one fits


class PrinterTimeout(PrinterError):
    pass


def printer_error(data: bytes) -> PrinterError:
    if not data:
        return PrinterError("printer error")
    name = ERROR_NAMES.get(data[0])
    return PrinterError(f"printer error 0x{data[0]:02x}" + (f" ({name})" if name else ""),
                        ERROR_REASONS.get(data[0], ""))


def parse_rfid(d: bytes) -> dict | None:
    """RfidInfo reply → roll details, or None when no tagged roll is loaded.
    Layout per niimbluelib/niimprint: uuid[8], len+barcode, len+serial, total u16, used u16, type u8."""
    if len(d) <= 1 or not any(d[:8]):
        return None
    try:
        i = 8
        n = d[i]
        barcode = d[i + 1:i + 1 + n].decode("ascii", "replace")
        i += 1 + n
        n = d[i]
        serial = d[i + 1:i + 1 + n].decode("ascii", "replace")
        i += 1 + n
        total, used, kind = struct.unpack(">HHB", d[i:i + 5])
    except (IndexError, struct.error):
        log.debug("unparsed RFID reply: %s", d.hex())
        return None
    return {"uuid": d[:8].hex(), "barcode": barcode, "serial": serial,
            "total": total, "used": used, "left": max(0, total - used), "type": kind}


def _xor(cmd: int, data: bytes) -> int:
    c = cmd ^ len(data)
    for b in data:
        c ^= b
    return c


def encode_packet(cmd: int, data: bytes = b"") -> bytes:
    data = bytes(data)
    pkt = b"\x55\x55" + bytes([cmd, len(data)]) + data + bytes([_xor(cmd, data)]) + b"\xaa\xaa"
    return (b"\x03" + pkt) if cmd == Cmd.CONNECT else pkt


class PacketParser:
    """Reassembles 55 55 <cmd> <len> <data> <xor> AA AA frames from a byte stream."""

    def __init__(self):
        self.buf = bytearray()

    def _frame_at(self, i: int):
        """'ok' + (cmd, data, size) | 'short' | 'bad'"""
        b = self.buf
        if len(b) - i < 4:
            return "short", None
        n = b[i + 3]
        if len(b) - i < n + 7:
            return "short", None
        cmd, data = b[i + 2], bytes(b[i + 4:i + 4 + n])
        if b[i + 4 + n] != _xor(cmd, data) or b[i + 5 + n:i + 7 + n] != b"\xaa\xaa":
            return "bad", None
        return "ok", (cmd, data, n + 7)

    def feed(self, chunk: bytes) -> list[tuple[int, bytes]]:
        self.buf += chunk
        out = []
        while True:
            i = self.buf.find(b"\x55\x55")
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]
                break
            del self.buf[:i]
            state, fr = self._frame_at(0)
            if state == "short":
                # Might be a false header with a bogus length: if a complete valid
                # frame starts later in the buffer, skip to it; otherwise wait.
                j = self.buf.find(b"\x55\x55", 1)
                while j > 0 and self._frame_at(j)[0] != "ok":
                    j = self.buf.find(b"\x55\x55", j + 1)
                if j > 0:
                    del self.buf[:j]
                    continue
                break
            if state == "bad":
                del self.buf[:1]
                continue
            cmd, data, size = fr
            del self.buf[:size]
            out.append((cmd, data))
        return out


def black_counts(row: bytes) -> tuple[int, int, int]:
    """Split-mode black-pixel counts (3 equal chunks, each capped at 255)."""
    k = len(row) // 3
    parts = (row[:k], row[k:2 * k], row[2 * k:])
    return tuple(min(255, sum(bin(b).count("1") for b in p)) for p in parts)  # type: ignore


def encode_rows(rows: list[bytes]):
    """Row packets with run-length repeat for identical consecutive rows."""
    y, n = 0, len(rows)
    while y < n:
        row, rep = rows[y], 1
        while y + rep < n and rep < 255 and rows[y + rep] == row:
            rep += 1
        if any(row):
            yield encode_packet(Cmd.BITMAP_ROW, struct.pack(">H3BB", y, *black_counts(row), rep) + row)
        else:
            yield encode_packet(Cmd.EMPTY_ROW, struct.pack(">HB", y, rep))
        y += rep


# ───────────────────────────── raster ─────────────────────────────

_INVERT = bytes(255 - i for i in range(256))
# PPD "BlackLevel" → grey threshold: pixels darker than this print (threshold mode only).
# Yellow ≈ 226 and #ddd = 221 in greyscale, so only Darkest prints pale colours; #f0f0f0 backgrounds stay white.
BLACK_LEVELS = {"light": 80, "normal": 128, "dark": 176, "darkest": 232}


def _fit(w: float, h: float) -> tuple[bool, float]:
    """Page of w×h px at 203 dpi → (rotate?, scale factor ≤ 1) so that it fits across the head."""
    rotate = w > HEAD_PX + 16 and h <= HEAD_PX + 16     # landscape page that only fits turned
    return rotate, min(1.0, HEAD_PX / (h if rotate else w))


def _flatten(img):
    """Transparent pixels become white paper (their hidden RGB is usually black)."""
    from PIL import Image
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, "white")
        bg.alpha_composite(img)
        return bg
    return img


def image_to_rows(img, dither: bool = False, threshold: int = 128, fitted: bool = False) -> list[bytes]:
    """PIL image → list of 48-byte rows (bit=1 → black), centred on the 384 px head.
    fitted=True: already rotated/scaled for the head (PDF path); only trim a rounding pixel."""
    from PIL import Image

    img = _flatten(img).convert("L")
    w, h = img.size
    if fitted:
        if w > HEAD_PX:
            x = (w - HEAD_PX) // 2
            img = img.crop((x, 0, x + HEAD_PX, h))
            w = HEAD_PX
    else:
        rotate, f = _fit(w, h)
        if rotate:
            img = img.rotate(90, expand=True)
            w, h = img.size
        if f < 1:                                     # e.g. 50 mm label = 400 px on a 384 px head
            h = max(1, round(h * f))
            img = img.resize((HEAD_PX, h), Image.LANCZOS)
            w = HEAD_PX
    canvas = Image.new("L", (HEAD_PX, h), 255)
    canvas.paste(img, ((HEAD_PX - w) // 2, 0))
    if dither:
        bw = canvas.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    else:
        lut = [0 if v < threshold else 255 for v in range(256)]
        bw = canvas.point(lut).convert("1", dither=Image.Dither.NONE)
    raw = bw.tobytes().translate(_INVERT)             # PIL: 1=white → printer: 1=black
    rl = HEAD_PX // 8
    return [raw[i * rl:(i + 1) * rl] for i in range(h)]


def document_to_pages(data: bytes, dither: bool = False, threshold: int = 128) -> list[list[bytes]]:
    if data[:5] == b"%PDF-" or b"%PDF-" in data[:1024]:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(data)
        try:
            pages = []
            for page in doc:
                # Rasterise straight at the final size: resampling a finished bitmap softens text and barcodes.
                w_pt, h_pt = page.get_size()
                rotate, f = _fit(w_pt * DPI / 72, h_pt * DPI / 72)
                img = page.render(scale=DPI / 72 * f, rotation=270 if rotate else 0, grayscale=True).to_pil()
                pages.append(image_to_rows(img, dither, threshold, fitted=True))
            return pages
        finally:
            doc.close()
    import io
    from PIL import Image
    return [image_to_rows(Image.open(io.BytesIO(data)), dither, threshold)]


# Config "top_offset_mm" nudges the print down the label (up if negative). On a 50x30 roll the B1 loses
# about the first 0.4 mm at the top edge, but shifting by 0.5 mm overshoots, so the default stays 0.
TOP_OFFSET_MM = 0.0


def shift_rows(rows: list[bytes], mm) -> list[bytes]:
    """Move the image down the label by mm (up if negative), keeping the page length."""
    try:
        n = round(float(mm) * DPI / 25.4)
    except (TypeError, ValueError):
        n = 0
    n = max(-len(rows), min(len(rows), n))
    blank = [bytes(HEAD_PX // 8)] * abs(n)
    if n > 0:
        return blank + rows[:len(rows) - n]
    return rows[-n:] + blank if n < 0 else rows


def black_level(value: str | int | None) -> int:
    """'Darkest' / 'normal' / '200' → threshold 1..255."""
    v = str(value if value is not None else "normal").strip().lower()
    if v in BLACK_LEVELS:
        return BLACK_LEVELS[v]
    try:
        return max(1, min(255, int(v)))
    except ValueError:
        return BLACK_LEVELS["normal"]


# ───────────────────────────── transports ─────────────────────────────

def find_usb_port() -> str | None:
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.vid == USB_VID]
    ports.sort(key=lambda p: not p.device.startswith("/dev/cu."))   # prefer cu.* on macOS
    return ports[0].device if ports else None


class SerialTransport:
    pace = 0.0
    name = "usb"

    def __init__(self, port: str | None = None):
        self.port = port

    async def open(self, on_data):
        import serial
        port = self.port or find_usb_port()
        if not port:
            raise PrinterError("No Niimbot USB serial port found", "offline-report")
        self.ser = serial.Serial(port, 115200, timeout=0.1)
        loop = asyncio.get_running_loop()
        self._stop = threading.Event()

        def reader():
            while not self._stop.is_set():
                try:
                    d = self.ser.read(256)
                except Exception:
                    break
                if d:
                    loop.call_soon_threadsafe(on_data, d)

        self._thread = threading.Thread(target=reader, daemon=True)
        self._thread.start()
        log.info("USB connected: %s", port)

    async def write(self, data: bytes):
        await asyncio.to_thread(self.ser.write, data)

    async def close(self):
        if not hasattr(self, "ser"):
            return
        self._stop.set()
        await asyncio.to_thread(self._thread.join, 1)     # reader wakes within its 0.1 s read timeout
        await asyncio.to_thread(self.ser.close)


class BleTransport:
    pace = 0.01          # niimbluelib default packet interval
    name = "ble"

    def __init__(self, address: str | None = None, name_prefix: str = "B1"):
        self.address, self.prefix = address, name_prefix

    async def open(self, on_data):
        from bleak import BleakClient, BleakScanner
        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=10)
        else:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or ad.local_name or "").startswith(self.prefix), timeout=10)
        if not dev:
            raise PrinterError("No Niimbot BLE printer found (is it on and not connected to the phone?)", "offline-report")
        self.client = BleakClient(dev)
        await self.client.connect()
        await self.client.start_notify(BLE_CHAR, lambda _c, d: on_data(bytes(d)))
        # CoreBluetooth silently drops write-without-response data beyond this size, so split to it.
        char = self.client.services.get_characteristic(BLE_CHAR)
        self.chunk = int(getattr(char, "max_write_without_response_size", 0) or 0)
        log.info("BLE connected: %s (%s), max write %s bytes", dev.name, dev.address, self.chunk or "unknown")

    async def write(self, data: bytes):
        n = getattr(self, "chunk", 0) or len(data)
        for i in range(0, len(data), n):
            await self.client.write_gatt_char(BLE_CHAR, data[i:i + n], response=False)

    async def close(self):
        try:
            await self.client.disconnect()
        except Exception:           # includes: never connected
            pass


def make_transport(kind: str = "auto", address: str | None = None, ble_name: str = "B1"):
    if kind == "usb" or (kind == "auto" and (address or "").startswith("/dev/")):
        return SerialTransport(address)
    if kind == "auto" and not address and find_usb_port():
        return SerialTransport()
    return BleTransport(None if (address or "").startswith("/dev/") else address, ble_name)


# ───────────────────────────── printer client ─────────────────────────────

class B1Printer:
    timeout = 5.0            # per request; tests lower it

    def __init__(self, transport):
        self.t = transport
        self.parser = PacketParser()
        self.q: asyncio.Queue = asyncio.Queue()
        self.say = lambda line: None     # status lines for the print queue (set by Proxy)
        self.roll: dict | None = None

    def _on_data(self, chunk: bytes):
        for pkt in self.parser.feed(chunk):
            self.q.put_nowait(pkt)

    async def __aenter__(self):          # already opened by Proxy.connect
        return self

    async def __aexit__(self, *exc):
        await self.t.close()

    def _drain(self):
        """Drop stale packets, but never lose an error the printer reported in the meantime."""
        while not self.q.empty():
            rc, rd = self.q.get_nowait()
            if rc == IN_PRINT_ERROR:
                raise printer_error(rd)

    async def request(self, cmd: int, data: bytes = b"", timeout: float | None = None) -> bytes:
        self._drain()
        await self.t.write(encode_packet(cmd, data))
        want = RESPONSE[cmd]
        want = want if isinstance(want, tuple) else (want,)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.timeout if timeout is None else timeout)
        while True:
            try:
                rc, rd = await asyncio.wait_for(self.q.get(), max(0.001, deadline - loop.time()))
            except asyncio.TimeoutError:
                raise PrinterTimeout(f"printer did not answer (command 0x{cmd:02x})") from None
            if rc == IN_PRINT_ERROR:
                raise printer_error(rd)
            if rc in want:
                return rd
            log.debug("ignored packet 0x%02x %s", rc, rd.hex())

    async def hello(self):
        try:
            await self.request(Cmd.CONNECT, b"\x01", timeout=min(2, self.timeout))
        except PrinterTimeout:
            log.debug("no Connect reply (ok on some firmwares)")

    async def roll_info(self) -> dict | None:
        """Read the loaded roll's RFID tag. None: no tag (third-party roll) or the printer didn't say."""
        try:
            self.roll = parse_rfid(await self.request(Cmd.RFID_INFO, b"\x01", timeout=min(2, self.timeout)))
        except PrinterError as e:
            log.debug("no roll info: %s", e)
            self.roll = None
        return self.roll

    async def _ok(self, cmd: int, data: bytes, what: str):
        r = await self.request(cmd, data)
        if not r or r[0] == 0:
            raise PrinterError(f"{what} rejected")

    async def print_pages(self, pages: list[list[bytes]], density=3, label_type=0, copies=1):
        await self.hello()
        roll = await self.roll_info()
        if roll:
            log.info("roll: %d of %d labels left, type %s, barcode %s, serial %s", roll["left"], roll["total"],
                     LABEL_TYPES.get(roll["type"], roll["type"]), roll["barcode"], roll["serial"])
            if roll["total"] and not roll["left"]:
                log.warning("the roll's tag says it is used up; printing anyway")
                self.say("INFO: The label roll reports no labels left")
        if label_type not in LABEL_TYPES:                     # 0 = Auto
            label_type = roll["type"] if roll and roll["type"] in LABEL_TYPES else 1
            log.info("paper type: %s (%s)", LABEL_TYPES[label_type], "from the roll" if roll else "default, no roll tag")
        await self._ok(Cmd.SET_DENSITY, bytes([max(1, min(5, density))]), "density")
        await self._ok(Cmd.SET_LABEL_TYPE, bytes([label_type]), "label type")
        total = len(pages) * copies
        await self._ok(Cmd.PRINT_START, struct.pack(">H5B", total, 0, 0, 0, 0, 0), "print start")
        try:
            await self._print_started(pages, copies, total)
        except Exception:
            await self._abort()
            raise
        await self.request(Cmd.PRINT_END, b"\x01")

    async def _abort(self):
        """Best effort: don't leave the printer waiting for the rest of a failed job."""
        for cmd in (Cmd.PRINT_CANCEL, Cmd.PRINT_END):
            try:
                await self.request(cmd, b"\x01", timeout=min(1, self.timeout))
            except Exception as e:
                log.debug("abort 0x%02x: %s", cmd, e)

    async def _print_started(self, pages: list[list[bytes]], copies: int, total: int):
        done = 0
        for rows in pages:
            packets = list(encode_rows(rows))
            for _ in range(copies):
                await self._ok(Cmd.PAGE_START, b"\x01", "page start")
                await self._ok(Cmd.SET_PAGE_SIZE, struct.pack(">HHH", len(rows), HEAD_PX, 1), "page size")
                for i, pkt in enumerate(packets):
                    await self.t.write(pkt)
                    if self.t.pace:
                        await asyncio.sleep(self.t.pace)
                    if i % 16 == 15:
                        self._drain()                      # surface a lid-open / out-of-paper error now
                for _attempt in range(50):                 # printer may still be busy
                    r = await self.request(Cmd.PAGE_END, b"\x01")
                    if r and r[0]:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise PrinterError("page end rejected")
                done += 1
                await self._wait_printed(done)
                log.info("page %d/%d printed", done, total)
                self.say(f"INFO: Printed label {done} of {total}")

    async def _wait_printed(self, page: int, timeout: float = 30):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            r = await self.request(Cmd.PRINT_STATUS, b"\x01")
            if len(r) >= 2 and struct.unpack(">H", r[:2])[0] >= page:
                return
            await asyncio.sleep(0.2)
        raise PrinterError("timed out waiting for page to print")


# ───────────────────────────── job / server ─────────────────────────────

def parse_job_options(text: str) -> dict:
    """'copies=2 Density=4 LabelType=1 Dither=FloydSteinberg ...' → normalised dict. Later keys win."""
    opts = {}
    try:
        tokens = shlex.split(text)
    except ValueError:                  # unbalanced quote in some option: don't lose the job over it
        tokens = text.split()
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            opts[k.lower()] = v
        elif tok.startswith("no"):
            opts[tok[2:].lower()] = "false"
    def num(key, default):
        try:
            return int(opts.get(key, default))
        except ValueError:
            return default
    return {
        "copies": max(1, num("copies", 1)),
        "density": num("density", 3),
        "label_type": num("labeltype", 0),           # "Auto" or absent → 0 → from the roll
        "dither": opts.get("dither", "Threshold").lower() in ("floydsteinberg", "true", "1", "yes"),
        "threshold": black_level(opts.get("blacklevel")),
    }


class Proxy:
    def __init__(self, cfg: dict, reload=None):
        self.cfg = cfg
        self.reload = reload                # serve: re-read config.json for every job, so edits need no restart
        self.lock = asyncio.Lock()

    async def run_job(self, data: bytes, opts: dict, say=lambda line: None):
        """say(line) sends a CUPS status line ("INFO: …", "STATE: ±reason") to the print queue, if there is one."""
        threshold = opts.get("threshold", 128)
        pages = await asyncio.to_thread(document_to_pages, data, opts["dither"], threshold)
        if not pages:
            raise PrinterError("document has no pages")
        pages = [shift_rows(rows, self.cfg.get("top_offset_mm", TOP_OFFSET_MM)) for rows in pages]
        log.info("job: %d page(s) × %d, density=%d, label_type=%s, dither=%s, threshold=%d",
                 len(pages), opts["copies"], opts["density"], opts["label_type"] or "auto", opts["dither"], threshold)
        async with self.lock:
            async with await self.connect(say) as p:
                p.say = say
                say("STATE: -offline-report")
                say("INFO: Printing")
                await p.print_pages(pages, opts["density"], opts["label_type"], opts["copies"])
                say(f"STATE: -{STATE_REASONS}")
                if p.roll and p.roll["total"]:
                    say(f"INFO: Printed. About {max(0, p.roll['left'] - len(pages) * opts['copies'])} labels left on the roll")

    async def connect(self, say=lambda line: None) -> "B1Printer":
        """Open the printer, retrying for cfg['wait'] seconds if it is off or unplugged."""
        loop = asyncio.get_running_loop()
        end = loop.time() + float(self.cfg.get("wait", 0))
        told = False
        while True:
            p = B1Printer(make_transport(self.cfg.get("transport", "auto"), self.cfg.get("address"),
                                         self.cfg.get("ble_name", "B1")))
            try:
                await p.t.open(p._on_data)
                return p
            except Exception as e:
                await p.t.close()
                left = end - loop.time()
                if left <= 0:
                    raise e if isinstance(e, PrinterError) else PrinterError(f"cannot open printer: {e}", "offline-report")
                if not told:
                    told = True
                    log.warning("printer not available (%s); waiting up to %d s", e, left)
                    say("STATE: +offline-report")
                    say("INFO: Waiting for the printer - switch it on or plug it in")
                    await asyncio.to_thread(notify, "Niimbot B1", "Waiting for the printer — switch it on or plug it in.")
                await asyncio.sleep(2)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self.reload:
            self.cfg = self.reload()
        from_cups = False
        last = ["INFO: Processing"]

        def say(line: str):
            """Status back to niimbot-filter, which relays it to CUPS. Raw clients (nc) get nothing."""
            if not from_cups:
                return
            line = " ".join(str(line).split())
            if line.startswith("INFO:"):
                last[0] = line
            try:
                writer.write(line.encode("utf-8", "replace") + b"\n")
            except Exception:
                pass

        async def keepalive():              # the CUPS socket backend gives up after 90 s of silence
            while True:
                await asyncio.sleep(10)
                say(last[0])

        ticker = asyncio.ensure_future(keepalive())
        try:
            first = await reader.readline()
            if first.startswith(MAGIC):
                from_cups = True
                opts = parse_job_options(first[len(MAGIC):].decode("utf-8", "replace"))
                data = await reader.read()
            else:                                    # raw PDF/PNG (e.g. `nc localhost 9109 < x.pdf`)
                opts = parse_job_options("")
                data = first + await reader.read()
            await self.run_job(data, opts, say)
            say("NIIMBOT-DONE ok")
        except Exception as e:
            msg = str(e) or type(e).__name__
            if isinstance(e, PrinterError):
                log.error("job failed: %s", msg)
            else:
                log.exception("job failed")
            if getattr(e, "reason", ""):
                say(f"STATE: +{e.reason}")
            say(f"ERROR: {msg}")
            # "fail" makes the filter exit non-zero, so CUPS keeps the job instead of calling it printed.
            say("NIIMBOT-DONE fail" if self.cfg.get("report_failures", True) else "NIIMBOT-DONE ok")
            await asyncio.to_thread(notify, "Niimbot B1", f"Print failed: {msg}")
        finally:
            ticker.cancel()
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()          # CUPS socket backend waits for this → job completes after printing

    async def serve(self, port: int):
        told = False
        while True:
            try:
                server = await asyncio.start_server(self.handle, "127.0.0.1", port)
                break
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
                if not told:              # e.g. another logged-in user's proxy; take over when it goes away
                    told = True
                    log.warning("port %d is in use (another Niimbot Proxy?); retrying every 30 s", port)
                await asyncio.sleep(30)
        log.info("listening on 127.0.0.1:%d (transport=%s)", port, self.cfg.get("transport", "auto"))
        async with server:
            await server.serve_forever()


def notify(title: str, msg: str):
    if sys.platform == "darwin":
        q = lambda s: json.dumps(s, ensure_ascii=False)       # AppleScript takes UTF-8, not \\uXXXX
        script = f'display notification {q(msg)} with title {q(title)}'
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def load_config(args) -> dict:
    cfg = {}
    f = SUPPORT_DIR / "config.json"
    if f.exists():
        try:
            loaded = json.loads(f.read_text())
            if not isinstance(loaded, dict):
                raise ValueError("top level must be an object")
            cfg.update(loaded)
        except (ValueError, OSError) as e:
            log.error("ignoring %s: %s", f, e)
    for k in ("transport", "address", "ble_name"):
        v = getattr(args, k, None)
        if v:
            cfg[k] = v
    if getattr(args, "wait", None) is not None:
        cfg["wait"] = args.wait
    cfg.setdefault("wait", 60 if getattr(args, "cmd", "") == "serve" else 0)
    return cfg


async def scan():
    from serial.tools import list_ports
    print("USB:")
    for p in list_ports.comports():
        tag = "  ← Niimbot" if p.vid == USB_VID else ""
        print(f"  {p.device}  {p.vid and hex(p.vid)}:{p.pid and hex(p.pid)}  {p.description}{tag}")
    print("BLE (10 s):")
    from bleak import BleakScanner
    for d, ad in (await BleakScanner.discover(timeout=10, return_adv=True)).values():
        name = d.name or ad.local_name
        if name:
            print(f"  {d.address}  {name}  rssi={ad.rssi}")


async def status(cfg: dict):
    """What the printer says about itself and the loaded roll (also dumps raw replies for debugging)."""
    async with await Proxy(cfg).connect() as p:
        print(f"connected over {p.t.name}")
        await p.hello()
        try:
            raw = await p.request(Cmd.RFID_INFO, b"\x01", timeout=2)
            roll = parse_rfid(raw)
            if roll:
                print(f"roll: {roll['left']} of {roll['total']} labels left, "
                      f"type {roll['type']} ({LABEL_TYPES.get(roll['type'], 'unknown')}), "
                      f"barcode {roll['barcode']}, serial {roll['serial']}")
            else:
                print("roll: no RFID tag read (third-party roll, or none loaded)")
            print(f"  raw rfid reply: {raw.hex()}")
        except PrinterError as e:
            print(f"roll: no answer ({e})")
        try:
            beat = await p.request(Cmd.HEARTBEAT, b"\x01", timeout=2)
            print(f"  raw heartbeat reply: {beat.hex()}")
        except PrinterError as e:
            print(f"  heartbeat: no answer ({e})")


def render_preview(src: str, out: str, dither: bool, threshold: int = 128):
    from PIL import Image
    pages = document_to_pages(Path(src).read_bytes(), dither, threshold)
    imgs = [Image.frombytes("1", (HEAD_PX, len(rows)), b"".join(rows).translate(_INVERT)) for rows in pages]
    h = sum(i.height for i in imgs) + 8 * (len(imgs) - 1)
    sheet = Image.new("L", (HEAD_PX, h), 160)            # grey gaps between pages
    y = 0
    for im in imgs:
        sheet.paste(im.convert("L"), (0, y))
        y += im.height + 8
    sheet.save(out)
    print(f"{len(imgs)} page(s), {HEAD_PX} px wide → {out}")


def versions():
    import bleak.backends.corebluetooth.client  # noqa: F401  (pyobjc CoreBluetooth loads)
    import PIL
    import pypdfium2.version as pdfv
    import serial
    import serial.tools.list_ports  # noqa: F401
    print(f"niimbot-proxy {__version__}  python {sys.version.split()[0]}  frozen={bool(FROZEN)}")
    print(f"bleak ok  pyserial {serial.__version__}  "
          f"pillow {PIL.__version__}  pypdfium2 {pdfv.PYPDFIUM_INFO}  pdfium {pdfv.PDFIUM_INFO}")


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]   # Finder/`open` may pass -psn_…
    if FROZEN and not argv:
        argv = ["serve"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--log", help="log file (default for the app's serve: ~/Library/Logs/NiimbotProxy.log)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "print", "status"):
        s = sub.add_parser(name)
        s.add_argument("--transport", choices=["auto", "usb", "ble"])
        s.add_argument("--address", help="/dev/cu.usbmodem… or BLE address/UUID")
        s.add_argument("--ble-name", help="BLE name prefix (default B1)")
        s.add_argument("--wait", type=float, help="seconds to wait for an absent printer (serve: 60, print: 0)")
    sub.choices["serve"].add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.choices["print"]
    p.add_argument("file")
    p.add_argument("--density", type=int, default=3)
    p.add_argument("--label-type", type=int, default=0,
                   help="0=auto from the roll (default) 1=gaps 2=black mark 3=continuous 5=transparent")
    p.add_argument("--dither", action="store_true")
    p.add_argument("--copies", type=int, default=1)
    r = sub.add_parser("render")
    r.add_argument("file")
    r.add_argument("out")
    r.add_argument("--dither", action="store_true")
    for s in (p, r):
        s.add_argument("--black-level", default="normal",
                       help="light|normal|dark|darkest or a grey threshold 1-255 (threshold mode)")
    sub.add_parser("scan")
    sub.add_parser("version")
    args = ap.parse_args(argv)

    if FROZEN and args.cmd == "serve" and not args.log:
        args.log = str(LOG_FILE)
    handlers: list[logging.Handler] = []
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(args.log, maxBytes=1_000_000, backupCount=2))
    if not args.log or sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if args.cmd == "scan":
        asyncio.run(scan())
    elif args.cmd == "version":
        versions()
    elif args.cmd == "render":
        render_preview(args.file, args.out, args.dither, black_level(args.black_level))
    elif args.cmd == "serve":
        asyncio.run(Proxy(load_config(args), reload=lambda: load_config(args)).serve(args.port))
    elif args.cmd == "status":
        try:
            asyncio.run(status(load_config(args)))
        except (PrinterError, OSError) as e:
            sys.exit(f"error: {e}")
    else:
        opts = {"copies": args.copies, "density": args.density,
                "label_type": args.label_type, "dither": args.dither,
                "threshold": black_level(args.black_level)}
        try:
            asyncio.run(Proxy(load_config(args)).run_job(Path(args.file).read_bytes(), opts))
        except (PrinterError, OSError) as e:
            log.debug("print failed", exc_info=True)
            sys.exit(f"error: {e or type(e).__name__}  (-v for details)")


if __name__ == "__main__":
    main()
