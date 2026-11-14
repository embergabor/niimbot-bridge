"""Offline tests: simulated B1 on the wire, real filter script, real socket path."""
import asyncio
import io
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import niimbot_proxy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


class FakeB1:
    """Parses host packets, answers like a B1, reconstructs printed pages."""

    def __init__(self):
        self.parser = np.PacketParser()
        self.pages, self.cur, self.printed, self.cmds = [], None, 0, []
        self.settings = {}
        # fault injection
        self.error_after_rows = None     # send 0xDB (cover open) after this many row packets
        self.mute = set()                # commands that get no reply
        self.page_end_zeros = 0          # answer PageEnd with 0 this many times first
        self.rows_seen = 0
        self.rfid = b"\x00"               # RfidInfo reply; default: no tagged roll

    def reply(self, cmd, data):
        return np.encode_packet(cmd, data)

    def handle(self, cmd, d):
        self.cmds.append(cmd)
        R = np.RESPONSE
        if cmd in self.mute:
            return b""
        if cmd in (np.Cmd.EMPTY_ROW, np.Cmd.BITMAP_ROW):
            self.rows_seen += 1
            if self.rows_seen == self.error_after_rows:
                return self.reply(np.IN_PRINT_ERROR, b"\x01")
        if cmd == np.Cmd.PRINT_CANCEL:
            return self.reply(R[cmd], b"\x01")
        if cmd == np.Cmd.RFID_INFO:
            return self.reply(R[cmd], self.rfid)
        if cmd == np.Cmd.CONNECT:
            return self.reply(R[cmd], b"\x03")
        if cmd in (np.Cmd.SET_DENSITY, np.Cmd.SET_LABEL_TYPE):
            self.settings[cmd] = d[0]
            return self.reply(R[cmd], b"\x01")
        if cmd == np.Cmd.PRINT_START:
            assert len(d) == 7
            self.total = struct.unpack(">H", d[:2])[0]
            return self.reply(R[cmd], b"\x01")
        if cmd == np.Cmd.PAGE_START:
            return self.reply(R[cmd], b"\x01")
        if cmd == np.Cmd.SET_PAGE_SIZE:
            rows, cols, copies = struct.unpack(">HHH", d)
            self.cur = {"rows": rows, "cols": cols, "data": [None] * rows}
            return self.reply(R[cmd], b"\x01")
        if cmd == np.Cmd.EMPTY_ROW:
            y, rep = struct.unpack(">HB", d)
            for i in range(rep):
                self.cur["data"][y + i] = bytes(self.cur["cols"] // 8)
            return b""
        if cmd == np.Cmd.BITMAP_ROW:
            y, c1, c2, c3, rep = struct.unpack(">H3BB", d[:6])
            row = d[6:]
            assert (c1, c2, c3) == np.black_counts(row)
            for i in range(rep):
                self.cur["data"][y + i] = row
            return b""
        if cmd == np.Cmd.PAGE_END and self.page_end_zeros > 0:
            self.page_end_zeros -= 1
            return self.reply(R[cmd], b"\x00")
        if cmd == np.Cmd.PAGE_END:
            assert all(r is not None for r in self.cur["data"]), "missing rows"
            self.pages.append(self.cur)
            self.printed += 1
            return self.reply(R[cmd], b"\x01")
        if cmd == np.Cmd.PRINT_STATUS:
            return self.reply(R[cmd], struct.pack(">HBB", self.printed, 100, 100))
        if cmd == np.Cmd.PRINT_END:
            return self.reply(R[cmd], b"\x01")
        raise AssertionError(hex(cmd))


class FakeTransport:
    pace = 0.0
    name = "fake"

    def __init__(self, dev):
        self.dev = dev

    async def open(self, on_data):
        self.on_data = on_data

    async def write(self, data):
        if data[0] == 0x03:
            data = data[1:]
        for cmd, d in self.dev.parser.feed(data):
            out = self.dev.handle(cmd, d)
            if out:
                # deliver in 2 fragments to exercise reassembly
                asyncio.get_running_loop().call_soon(self.on_data, out[:3])
                asyncio.get_running_loop().call_soon(self.on_data, out[3:])
        await asyncio.sleep(0)          # real transports yield on every write

    async def close(self):
        pass


def make_pdf(pages=2) -> bytes:
    imgs = []
    for i in range(pages):
        im = Image.new("L", (400, 240), 255)          # 50x30 mm @ 203 dpi
        dr = ImageDraw.Draw(im)
        dr.rectangle([10, 10, 389, 229], outline=0, width=4)
        dr.text((40, 100), f"LABEL {i + 1}", fill=0)
        imgs.append(im.convert("RGB"))
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", resolution=203, save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def decode(page) -> Image.Image:
    raw = b"".join(page["data"])
    return Image.frombytes("1", (page["cols"], page["rows"]), raw.translate(np._INVERT))


def test_packet_roundtrip():
    p = np.encode_packet(0x21, b"\x03")
    assert p == bytes.fromhex("5555210103 23aaaa".replace(" ", ""))
    assert np.encode_packet(np.Cmd.CONNECT, b"\x01") == bytes.fromhex("03 5555 c1 01 01 c1 aaaa".replace(" ", ""))
    parser = np.PacketParser()
    junk = b"\x00\x55" + p + b"\x55\x55\x99" + p
    assert parser.feed(junk[:5]) == []
    got = parser.feed(junk[5:])
    assert got == [(0x21, b"\x03"), (0x21, b"\x03")], got


def test_options():
    o = np.parse_job_options("copies=2 Density=3 LabelType=1 Dither=Threshold Density=5 Dither=FloydSteinberg "
                             "BlackLevel=Normal BlackLevel=Darkest media=w50h30")
    assert o == {"copies": 2, "density": 5, "label_type": 1, "dither": True, "threshold": 232}, o
    assert np.parse_job_options("")["threshold"] == 128
    assert np.black_level("bogus") == 128 and np.black_level("200") == 200 and np.black_level("999") == 255


def test_black_levels():
    # Stripes of grey 60 / 150 / 200 / 226 (≈ yellow) / 245 (off-white), 40 px wide each.
    greys = [60, 150, 200, 226, 245]
    im = Image.new("L", (384, 10), 255)
    for i, g in enumerate(greys):
        im.paste(g, (i * 40, 0, i * 40 + 40, 10))

    def printed(level):
        row = np.image_to_rows(im, threshold=np.black_level(level))[0]
        bits = "".join(f"{b:08b}" for b in row)
        return [bits[i * 40 + 20] == "1" for i in range(len(greys))]   # sample stripe centres

    assert printed("light") == [True, False, False, False, False]
    assert printed("normal") == [True, False, False, False, False]
    assert printed("dark") == [True, True, False, False, False]
    assert printed("darkest") == [True, True, True, True, False]
    assert printed("100") == [True, False, False, False, False]


def test_end_to_end_via_filter_and_socket():
    tmp_path = Path(tempfile.mkdtemp(prefix="niimtest-"))
    subprocess.run([sys.executable, str(ROOT / "cups/make_ppd.py"), str(tmp_path / "base.ppd")],
                   check=True, capture_output=True)
    ppd = tmp_path / "queue.ppd"
    ppd.write_text((tmp_path / "base.ppd").read_text(encoding="latin-1")
                   .replace("*DefaultDensity: 3", "*DefaultDensity: 4")
                   .replace("*DefaultBlackLevel: Normal", "*DefaultBlackLevel: Dark"), encoding="latin-1")
    pdf = tmp_path / "job.pdf"
    pdf.write_bytes(make_pdf(2))
    filt = subprocess.run(["sh", str(ROOT / "cups/niimbot-filter"), "1", "gabor", "t", "2",
                           "LabelType=2 PageSize=30x50mmRotated.Fullbleed", str(pdf)],
                          env={**os.environ, "PPD": str(ppd)}, capture_output=True, check=True)
    header = filt.stdout.split(b"\n", 1)[0]
    assert b"Density=4" in header and b"LabelType=2" in header and b"BlackLevel=Dark" in header, header
    assert np.parse_job_options(header[len(np.MAGIC):].decode())["threshold"] == 176

    dev = FakeB1()
    real_make_transport = np.make_transport
    np.make_transport = lambda *a, **k: FakeTransport(dev)

    async def run():
        proxy = np.Proxy({"transport": "auto"})
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(filt.stdout)
        await w.drain()
        w.write_eof()
        back = await r.read()                 # daemon closes only after printing
        assert back.endswith(b"NIIMBOT-DONE ok\n") and b"INFO: Printed label 4 of 4" in back, back
        server.close()

    asyncio.run(run())
    np.make_transport = real_make_transport
    assert len(dev.pages) == 4                        # 2 pages × 2 copies
    assert dev.settings == {np.Cmd.SET_DENSITY: 4, np.Cmd.SET_LABEL_TYPE: 2}
    assert dev.total == 4
    pg = dev.pages[0]
    assert pg["cols"] == 384 and abs(pg["rows"] - 230) <= 1, pg["rows"]   # 400 px → scaled to head width
    img = decode(pg)
    img.save(tmp_path / "printed_page1.png")
    black = img.histogram()[0]
    assert 2500 < black < 10000, black
    assert dev.cmds[-1] == np.Cmd.PRINT_END



def noisy_rows(n=64):
    """Rows that are all different, so none are run-length merged."""
    return [bytes([(y * 7 + x) % 251 or 1 for x in range(48)]) for y in range(n)]


def run_print(dev, pages, timeout=0.3, **kw):
    async def go():
        p = np.B1Printer(FakeTransport(dev))
        p.timeout = timeout
        await p.t.open(p._on_data)
        await p.print_pages(pages, **kw)
    try:
        asyncio.run(go())
        return None
    except Exception as e:
        return e


def test_error_mid_page_is_reported_and_job_aborted():
    dev = FakeB1()
    dev.error_after_rows = 20
    err = run_print(dev, [noisy_rows()])
    assert isinstance(err, np.PrinterError) and "0x01" in str(err) and "cover open" in str(err), repr(err)
    assert dev.rows_seen < 64, "should stop streaming soon after the error"
    assert dev.cmds[-2:] == [np.Cmd.PRINT_CANCEL, np.Cmd.PRINT_END], [hex(c) for c in dev.cmds[-3:]]


def test_silent_printer_gives_a_clear_timeout():
    dev = FakeB1()
    dev.mute = {np.Cmd.PAGE_START}
    err = run_print(dev, [noisy_rows(4)])
    assert isinstance(err, np.PrinterTimeout) and "did not answer" in str(err) and "0x03" in str(err), repr(err)
    assert np.Cmd.PRINT_CANCEL in dev.cmds and dev.cmds[-1] == np.Cmd.PRINT_END

    dev = FakeB1()                       # a missing Connect reply alone is tolerated
    dev.mute = {np.Cmd.CONNECT}
    assert run_print(dev, [noisy_rows(4)]) is None and len(dev.pages) == 1


def test_page_end_retries_while_busy():
    dev = FakeB1()
    dev.page_end_zeros = 3
    assert run_print(dev, [noisy_rows(4)]) is None
    assert dev.cmds.count(np.Cmd.PAGE_END) == 4 and len(dev.pages) == 1


def test_transparent_png_prints_blank_not_black():
    im = Image.new("RGBA", (200, 50), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([10, 10, 30, 30], fill=(0, 0, 0, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    rows = np.document_to_pages(buf.getvalue())[0]
    black = sum(bin(b).count("1") for r in rows for b in r)
    assert black == 21 * 21, black


def test_pdf_renders_straight_to_head_width():
    rows = np.document_to_pages(make_pdf(1))[0]              # 50×30 mm page: 400 px on a 384 px head
    assert all(len(r) == 48 for r in rows) and 230 <= len(rows) <= 231, len(rows)
    mid = rows[len(rows) // 2]
    assert mid[1] and mid[-2] and not any(mid[4:-4]), mid.hex()   # frame edges kept (scaled, not cropped)


def test_landscape_pdf_rotates_like_an_image():
    im = Image.new("L", (600, 300), 255)                     # too wide for the head, fits when turned
    ImageDraw.Draw(im).rectangle([0, 0, 99, 49], fill=0)     # marker in the top-left corner
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PDF", resolution=203)
    pdf_rows = np.document_to_pages(buf.getvalue())[0]
    img_rows = np.image_to_rows(im)
    assert abs(len(pdf_rows) - 600) <= 1 and len(img_rows) == 600
    for rows in (pdf_rows, img_rows):                        # marker lands bottom-left in both paths
        assert rows[-20][6] == 0xFF and rows[20][6] == 0x00 and rows[-20][-7] == 0x00


def test_bad_input_does_not_kill_the_job_or_daemon():
    assert np.parse_job_options("Density=4 title=Bob's label")["density"] == 4
    old = np.SUPPORT_DIR
    np.SUPPORT_DIR = Path(tempfile.mkdtemp(prefix="niimtest-"))
    try:
        (np.SUPPORT_DIR / "config.json").write_text("{ not json")

        class Args:
            cmd = "serve"
        assert np.load_config(Args()) == {"wait": 60}
    finally:
        np.SUPPORT_DIR = old



def rfid_reply(total=230, used=30, kind=2):
    return (bytes.fromhex("881d8a6f12345678") + bytes([8]) + b"10262830" + bytes([10]) + b"PZ1G311305"
            + struct.pack(">HHB", total, used, kind))


def test_rfid_parsing():
    assert np.parse_rfid(b"\x00") is None and np.parse_rfid(bytes(20)) is None
    assert np.parse_rfid(rfid_reply()[:15]) is None                       # truncated reply: no crash
    r = np.parse_rfid(rfid_reply())
    assert (r["barcode"], r["serial"], r["total"], r["used"], r["left"], r["type"]) == \
        ("10262830", "PZ1G311305", 230, 30, 200, 2), r


def test_paper_type_comes_from_the_roll_when_auto():
    dev = FakeB1()
    dev.rfid = rfid_reply(kind=2)
    assert run_print(dev, [noisy_rows(4)]) is None
    assert dev.settings[np.Cmd.SET_LABEL_TYPE] == 2

    dev = FakeB1()                                    # an explicit choice beats the roll
    dev.rfid = rfid_reply(kind=2)
    assert run_print(dev, [noisy_rows(4)], label_type=5) is None
    assert dev.settings[np.Cmd.SET_LABEL_TYPE] == 5

    for dev_rfid in (b"\x00", rfid_reply(kind=99)):   # no tag, or a type the B1 doesn't take → gaps
        dev = FakeB1()
        dev.rfid = dev_rfid
        assert run_print(dev, [noisy_rows(4)]) is None
        assert dev.settings[np.Cmd.SET_LABEL_TYPE] == 1

    dev = FakeB1()                                    # a printer that never answers the RFID query still prints
    dev.mute = {np.Cmd.RFID_INFO}
    assert run_print(dev, [noisy_rows(4)]) is None and len(dev.pages) == 1


def through_cups(transport_factory, cfg=None):
    """Run a job the way CUPS does: filter → socket → proxy, with the proxy's replies fed to the
    filter's back-channel (fd 3) like the socket backend does. Returns (filter exit code, stderr)."""
    import socket
    import threading
    tmp = Path(tempfile.mkdtemp(prefix="niimtest-"))
    pdf = tmp / "job.pdf"
    pdf.write_bytes(make_pdf(1))
    real = np.make_transport
    np.make_transport = transport_factory
    real_notify, np.notify = np.notify, lambda *a: None
    result = {}

    def backend(port):
        sock = socket.create_connection(("127.0.0.1", port))
        r, w = os.pipe()
        filt = subprocess.Popen(["bash", "-c", f'exec "$0" "$@" 3<&{r}', str(ROOT / "cups/niimbot-filter"),
                                 "1", "gabor", "t", "1", "", str(pdf)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(r,))
        os.close(r)
        for chunk in iter(lambda: filt.stdout.read(65536), b""):
            sock.sendall(chunk)
        sock.shutdown(socket.SHUT_WR)
        for chunk in iter(lambda: sock.recv(4096), b""):
            os.write(w, chunk)
        os.close(w)
        sock.close()
        result["err"] = filt.stderr.read().decode()
        result["code"] = filt.wait(timeout=10)

    async def run():
        server = await asyncio.start_server(np.Proxy(cfg or {}).handle, "127.0.0.1", 0)
        await asyncio.to_thread(backend, server.sockets[0].getsockname()[1])
        server.close()

    try:
        asyncio.run(run())
    finally:
        np.make_transport, np.notify = real, real_notify
    return result["code"], result["err"]


def test_print_queue_hears_success():
    dev = FakeB1()
    dev.rfid = rfid_reply(total=230, used=30)
    code, err = through_cups(lambda *a, **k: FakeTransport(dev))
    assert code == 0 and len(dev.pages) == 1, (code, err)
    assert "INFO: Printed label 1 of 1" in err and "199 labels left" in err, err
    assert "STATE: -cover-open,media-empty-error,offline-report" in err, err


def test_print_queue_hears_failure():
    class Absent(FakeTransport):
        async def open(self, on_data):
            raise np.PrinterError("No Niimbot USB serial port found", "offline-report")
    code, err = through_cups(lambda *a, **k: Absent(None))
    assert code == 1, (code, err)
    assert "ERROR: No Niimbot USB serial port found" in err and "STATE: +offline-report" in err, err

    dev = FakeB1()                                    # lid opened mid-print → reason the queue can show
    dev.error_after_rows = 3                          # this label is only a handful of row packets
    np.B1Printer.timeout, old = 0.3, np.B1Printer.timeout
    try:
        code, err = through_cups(lambda *a, **k: FakeTransport(dev))
    finally:
        np.B1Printer.timeout = old
    assert code == 1 and "STATE: +cover-open" in err and "ERROR: printer error 0x01" in err, (code, err)

    code, err = through_cups(lambda *a, **k: Absent(None), {"report_failures": False})
    assert code == 0 and "ERROR:" in err, (code, err)  # opt-out: job counts as done, message still shown


def test_top_offset_moves_the_image_and_keeps_the_length():
    rows = [bytes([i]) * 48 for i in range(1, 21)]
    down = np.shift_rows(rows, 0.5)                    # 0.5 mm = 4 rows at 203 dpi
    assert len(down) == 20 and down[:4] == [bytes(48)] * 4 and down[4:] == rows[:16]
    up = np.shift_rows(rows, -0.5)
    assert len(up) == 20 and up[:16] == rows[4:] and up[16:] == [bytes(48)] * 4
    assert np.shift_rows(rows, 0) == rows and np.shift_rows(rows, "junk") == rows
    assert np.shift_rows(rows, 99) == [bytes(48)] * 20


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
