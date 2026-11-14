#!/usr/bin/env python3
"""Generates NiimbotB1.ppd. Sizes are W×H mm, W = across the head (≤ 50), H = feed length."""
import sys
from pathlib import Path

SIZES = [(50, 15), (50, 20), (50, 25), (50, 30), (50, 40), (50, 50), (50, 70), (50, 80),
         (40, 20), (40, 30), (40, 40), (40, 60),
         (30, 15), (30, 20), (30, 30),
         (25, 15), (25, 25), (20, 10)]
# Black level = grey threshold (0–255): pixels darker than this print. Must match BLACK_LEVELS in niimbot_proxy.py.
BLACK_LEVELS = [("Light", "Light (only strong darks)"), ("Normal", "Normal"),
                ("Dark", "Dark (mid-tones print)"), ("Darkest", "Darkest (pale colours print)")]
DEFAULT = (50, 30)
FILTER = "/Library/Printers/NiimbotB1/niimbot-filter"


def name(w: int, h: int) -> str:
    """Adobe/CUPS standard size name (short x long, "Rotated" when wider than long; zero margins → Fullbleed).
    Don't use wNNNhNNN: CUPS reads those numbers as points, not mm."""
    return (f"{w}x{h}mm" if w <= h else f"{h}x{w}mmRotated") + ".Fullbleed"


def pt(mm: float) -> str:
    return f"{mm * 72 / 25.4:.2f}"


def main():
    o = []
    a = o.append
    a('*PPD-Adobe: "4.3"')
    for line in ('*FormatVersion: "4.3"', '*FileVersion: "1.0"', '*LanguageVersion: English',
                 '*LanguageEncoding: ISOLatin1', '*PCFileName: "NIIMBB1.PPD"', '*Manufacturer: "Niimbot"',
                 '*Product: "(Niimbot B1)"', '*ModelName: "Niimbot B1 niimbot-proxy"',
                 '*ShortNickName: "Niimbot B1 proxy"', '*NickName: "Niimbot B1 via niimbot-proxy"',
                 '*PSVersion: "(3010.000) 0"', '*LanguageLevel: "3"', '*ColorDevice: False',
                 '*DefaultColorSpace: Gray', '*FileSystem: False', '*Throughput: "1"',
                 '*LandscapeOrientation: Plus90', '*TTRasterizer: Type42', '*cupsVersion: 2.3',
                 '*cupsManualCopies: False',
                 f'*cupsFilter2: "application/vnd.cups-pdf application/vnd.niimbot-job 0 {FILTER}"'):
        a(line)

    a('')
    a('*OpenUI *PageSize/Label Size: PickOne')
    a('*OrderDependency: 10 AnySetup *PageSize')
    a(f'*DefaultPageSize: {name(*DEFAULT)}')
    for w, h in SIZES:
        a(f'*PageSize {name(w, h)}/{w} x {h} mm: "<</PageSize[{pt(w)} {pt(h)}]/ImagingBBox null>>setpagedevice"')
    a('*CloseUI: *PageSize')
    a('')
    a('*OpenUI *PageRegion/Label Size: PickOne')
    a('*OrderDependency: 10 AnySetup *PageRegion')
    a(f'*DefaultPageRegion: {name(*DEFAULT)}')
    for w, h in SIZES:
        a(f'*PageRegion {name(w, h)}/{w} x {h} mm: "<</PageSize[{pt(w)} {pt(h)}]/ImagingBBox null>>setpagedevice"')
    a('*CloseUI: *PageRegion')
    a('')
    a(f'*DefaultImageableArea: {name(*DEFAULT)}')
    for w, h in SIZES:
        a(f'*ImageableArea {name(w, h)}/{w} x {h} mm: "0 0 {pt(w)} {pt(h)}"')
    a(f'*DefaultPaperDimension: {name(*DEFAULT)}')
    for w, h in SIZES:
        a(f'*PaperDimension {name(w, h)}/{w} x {h} mm: "{pt(w)} {pt(h)}"')
    a('')
    a('*VariablePaperSize: True')
    a(f'*MaxMediaWidth: "{pt(50)}"')
    a(f'*MaxMediaHeight: "{pt(200)}"')
    a('*HWMargins: 0 0 0 0')
    a('*CustomPageSize True: "pop pop pop <</PageSize[5 -2 roll]/ImagingBBox null>>setpagedevice"')
    a(f'*ParamCustomPageSize Width: 1 points {pt(15)} {pt(50)}')
    a(f'*ParamCustomPageSize Height: 2 points {pt(10)} {pt(200)}')
    a('*ParamCustomPageSize WidthOffset: 3 points 0 0')
    a('*ParamCustomPageSize HeightOffset: 4 points 0 0')
    a('*ParamCustomPageSize Orientation: 5 int 0 0')
    a('')
    a('*OpenUI *Density/Print Density: PickOne')
    a('*OrderDependency: 20 AnySetup *Density')
    a('*DefaultDensity: 3')
    for d, label in ((1, "1 (Light)"), (2, "2"), (3, "3 (Normal)"), (4, "4"), (5, "5 (Dark)")):
        a(f'*Density {d}/{label}: ""')
    a('*CloseUI: *Density')
    a('')
    a('*OpenUI *LabelType/Paper Type: PickOne')
    a('*OrderDependency: 20 AnySetup *LabelType')
    a('*DefaultLabelType: Auto')
    a('*LabelType Auto/Automatic (from the roll): ""')
    a('*LabelType 1/Labels with gaps: ""')
    a('*LabelType 2/Black mark: ""')
    a('*LabelType 3/Continuous: ""')
    a('*LabelType 5/Transparent: ""')
    a('*CloseUI: *LabelType')
    a('')
    a('*OpenUI *Dither/Halftoning: PickOne')
    a('*OrderDependency: 20 AnySetup *Dither')
    a('*DefaultDither: Threshold')
    a('*Dither Threshold/Threshold (text, barcodes): ""')
    a('*Dither FloydSteinberg/Dither (photos): ""')
    a('*CloseUI: *Dither')
    a('')
    a('*OpenUI *BlackLevel/Black Level: PickOne')
    a('*OrderDependency: 20 AnySetup *BlackLevel')
    a('*DefaultBlackLevel: Normal')
    for key, label in BLACK_LEVELS:
        a(f'*BlackLevel {key}/{label}: ""')
    a('*CloseUI: *BlackLevel')
    a('')
    a('*DefaultFont: Courier')
    a('*Font Courier: Standard "(002.004S)" Standard ROM')
    a('')

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("NiimbotB1.ppd")
    out.write_text("\n".join(o), encoding="latin-1")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
