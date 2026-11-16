# Niimbot Bridge

Use a Niimbot B1 label printer as a normal printer on macOS, over USB or Bluetooth. After installing, **Niimbot B1** appears in the print dialog of every app.

Needs macOS 15 or later. Works on Apple Silicon and Intel Macs.

## Install

1. Download `NiimbotBridge-<version>.pkg` from the [latest release](https://github.com/embergabor/niimbot-bridge/releases/latest).
2. Double-click the file. macOS will refuse to open it, because the installer isn't signed by Apple.
3. Open **System Settings → Privacy & Security**, scroll down to the message about NiimbotBridge, and click **Open Anyway**.
4. Follow the installer and enter your Mac password when asked.
5. Switch the printer on. For USB, plug it in. For Bluetooth, leave the cable unplugged and make sure the Niimbot phone app isn't connected to the printer.

That's all. There is nothing else to install and nothing to configure. The printer does not need to be paired in Bluetooth settings.

The first time you print over Bluetooth, macOS may ask whether *NiimbotProxy* may use Bluetooth. Click **Allow**.

## Print a label

1. In any app, choose **File → Print** and pick **Niimbot B1** as the printer.
2. Set **Paper Size** to the size of your labels, for example 50 × 30 mm. If your size isn't listed, choose *Manage Custom Sizes…*.
3. Click **Print**.

Tips:

- Make the page in your document the same size as the label.
- Keep text and graphics about 1 mm away from the edges.
- If the printer is plugged in, it prints over USB. Otherwise it uses Bluetooth.
- To avoid picking the size every time, choose **Presets → Save Current Settings as Preset…** in the print dialog.

## Options

In the print dialog, under **Printer Features** (you may need to click *Show Details*):

| Option | What it does |
|---|---|
| **Print Density** | How dark the print is, 1 to 5. The default is 3. |
| **Paper Type** | *Automatic* reads the type from genuine Niimbot rolls. For other rolls, choose it yourself: with gaps, black mark, continuous or transparent. |
| **Black Level** | Which colours print as black. *Normal* prints only dark colours. *Dark* adds mid-tones. *Darkest* also prints pale colours such as yellow. *Light* prints only the strongest darks. |
| **Halftoning** | *Threshold* is right for text, barcodes and logos. *Dither* is for photos, but photos come out rough on this printer. |

## If something goes wrong

- **The job waits and nothing prints.** The printer is off, asleep or out of range. Switch it on or plug it in; the job waits up to a minute for it.
- **The job failed.** It stays in the print queue with the reason, such as an open cover or an empty roll. Fix the problem, then open the print queue and retry the job.
- **Nothing prints over Bluetooth.** Disconnect the Niimbot phone app from the printer, and check that NiimbotProxy is allowed under **System Settings → Privacy & Security → Bluetooth**.
- **The print is blank or shifted.** Choose the correct Paper Type by hand, or raise the Print Density.
- **You need more detail.** The log is at `~/Library/Logs/NiimbotProxy.log`.

## Uninstall

Open Terminal and run:

```bash
sudo /Applications/NiimbotProxy.app/Contents/Resources/uninstall.sh
```

## Credits

The printer protocol comes from the work of these projects:

- [niimbluelib](https://github.com/MultiMote/niimbluelib) by MultiMote, and its documentation at [printers.niim.blue](https://printers.niim.blue)
- [niimprint](https://github.com/AndBondStyle/niimprint) by AndBondStyle

The app bundles these libraries: [bleak](https://github.com/hbldh/bleak) (Bluetooth), [pyserial](https://github.com/pyserial/pyserial) (USB), [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) with PDFium (PDF rendering), [Pillow](https://github.com/python-pillow/Pillow) (images) and [PyObjC](https://github.com/ronaldoussoren/pyobjc). It is packaged with [PyInstaller](https://github.com/pyinstaller/pyinstaller).

Niimbot is a trademark of its owner. This project is not affiliated with Niimbot.

## Licence

MIT, see [LICENSE](LICENSE).
