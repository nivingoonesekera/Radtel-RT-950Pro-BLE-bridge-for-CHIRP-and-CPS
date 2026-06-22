<p align="center">
  <img src="assets/logo.png" alt="RT-950 Pro" width="320">
</p>

# Radtel RT-950 Pro CHIRP driver (Bluetooth)

A [CHIRP](https://chirpmyradio.com/) driver that reads and writes the Radtel
RT-950 Pro over Bluetooth, so you can program the radio without a USB cable.
It also ships a Bluetooth bridge (and a one-click GUI / installer) so the
**official Radtel BT-RT950PRO CPS** can program the radio over Bluetooth too.

I wrote the Bluetooth side of this. It builds on the USB-cable driver by Nathan
Barguss (2E0NBS) for the radio's memory format:
<https://github.com/NathanBarguss/Chirp_Radtel-RT-950-Pro>.

Heads up, this is experimental. Writing to a radio can mis-program it, so always
do a Download first and keep that `.img` as a backup before you upload anything.
No warranty, use at your own risk.

## Easy install (recommended)

Download **`RT950-Setup.exe`** from the
[latest release](https://github.com/nivingoonesekera/Chirp_BLE_Radtel-RT-950-Pro/releases)
and run it. It installs everything for you in one go:

* Python 3.10 (with the `py` launcher),
* the `bleak` + `pyserial` packages,
* com0com and a ready-made COM10 ↔ COM11 virtual pair (for the bridge path),
* the drivers, the bridge, and the GUI — with a Start-menu/desktop **RT-950 Pro
  Bridge** shortcut.

The **RT-950 Pro Bridge** GUI it installs works with **both CHIRP and the
official Radtel BT-RT950PRO CPS**: start the bridge, then point either program at
the bridge's COM port. (If you only ever use the CPS, this `.exe`/GUI is the path
to use — the direct CHIRP driver below is a CHIRP module the CPS can't load.)

It needs admin (com0com installs a driver) and an internet connection during
install. The only thing it does *not* install is CHIRP itself — grab that from
<https://chirpmyradio.com/> and turn on `Help > Enable Developer Functions`.
(The CPS is a separate download from Radtel.)

Prefer to set it up by hand, or not on Windows? Follow the manual steps below.

## Two ways to use it

Which one you can use depends on the software:

* **CHIRP** — either way below works.
* **Official Radtel CPS** — you must use the **bridge** (or the GUI / the
  `.exe`). The direct driver is a CHIRP module, so the CPS can't load it.

Most people on CHIRP should use the direct Bluetooth driver:

* **`radtel_rt950pro_BLE_int.py`** connects straight to the radio over Bluetooth
  and lets you pick it from a list. Needs Python 3.10 and `bleak`, nothing else.
  **CHIRP only.**

If you would rather use a bridge, want to watch the live Bluetooth traffic, or
want to run the **official Radtel CPS**:

* **`radtel_rt950pro_BL.py`** plus **`ble_bridge.py`** (or `bridge_gui.py`) needs
  Python 3.10, `bleak`, `pyserial` and com0com. The bridge makes BLE look like a
  USB cable, so **both CHIRP and the Radtel CPS** work through it.

Both speak the same protocol to the radio. They only differ in how the bytes get
there.

## Setup (once)

1. Turn the radio on, turn Bluetooth on, and make sure it is not connected to
   the Radtel phone app (it has to be free to advertise).
2. Install Python 3.10 from <https://www.python.org/downloads/> (leave the
   "py launcher" option ticked in the installer).
3. In a terminal, run `pip install bleak`.
4. Use a CHIRP with developer mode: the bundled `chirp-next` build, or run CHIRP
   with `--developer`.

It has to be Python 3.10, because the CHIRP build is Python 3.10 and `bleak`'s
compiled parts have to match it. If you run CHIRP from your own Python that
already has `bleak`, it just uses that.

## Direct Bluetooth (recommended)

1. In CHIRP, turn on `Help > Enable Developer Functions`.
2. `Radio > Load Module` and choose `radtel_rt950pro_BLE_int.py`.
3. `Radio > Download From Radio`.
   * Model: Radtel RT-950 Pro.
   * Port: pick any COM port in the list, it is ignored.
   * A window pops up listing nearby Bluetooth devices, with the RT-950 at the
     top. Pick it and click OK.
4. Make your changes, then `Radio > Upload To Radio` to write them back.

Tips:

* To skip the scan, set your radio's MAC first:
  `set RT950_BLE_ADDR=AA:BB:CC:DD:EE:FF`
* If `bleak` is not found automatically, point at it:
  `set RT950_BLEAK_SITE=C:\Path\To\Python310\Lib\site-packages`

## Bridge with com0com (CHIRP *and* the Radtel CPS)

Use this if the direct way will not run on your PC, you want the bridge's live
frame log, or you want to use the **official Radtel BT-RT950PRO CPS** software
over Bluetooth. The bridge makes BLE look like a plain USB cable, so any COM-port
app — CHIRP or the CPS — works through it unchanged.

One time:

1. Install [com0com](https://com0com.sourceforge.net/) and make a linked pair,
   for example COM10 and COM11.
2. `pip install bleak pyserial`.

The easy way — GUI:

1. Run `python bridge_gui.py`. It scans for your radio (with a signal meter),
   lists COM ports, and has Start/Stop buttons — no flags to type.
2. Pick your radio and the bridge's COM port (e.g. COM10), press **Start Bridge**.
3. Point CHIRP or the CPS at the *other* side of the pair (COM11), then
   Download/Upload as usual.

The command-line way:

1. Start the bridge and leave it running (use your radio's MAC):
   `python ble_bridge.py COM10 --addr AA:BB:CC:DD:EE:FF`
   Wait for `unlock sent; bridge live`.
2. In CHIRP (developer mode) `Load Module > radtel_rt950pro_BL.py`, or open the
   CPS.
3. Download or Upload, model Radtel RT-950 Pro, port COM11 (the other side of
   the pair).
4. Press Ctrl+C in the bridge window (or Stop in the GUI) when you are done.

Note: boot-logo upload is **not** supported over Bluetooth — use the USB cable
for that.

## If something goes wrong

* "Could not import bleak": install Python 3.10 and run `pip install bleak`,
  then reload the module. If it still cannot find it, set `RT950_BLEAK_SITE`
  (see above).
* Empty device list, or "No RT-950 Pro found": check the radio is on, Bluetooth
  is on, and it is not connected to the phone app. Toggle the radio's Bluetooth
  and try again.
* Turned the radio off and on and it will not reconnect: just retry the download
  or upload, the driver rescans and tries the unlock again.
* A clone fails halfway: just retry. Keep your backup `.img` handy in case a
  write goes wrong.

## How it works (short version)

The radio uses a HM-10 style Bluetooth service `ffe0`. `ffe1` is the data pipe
that carries the whole clone protocol, and `ff31` takes a one-time unlock at
connect (the radio stays silent until it gets it). The driver sends the unlock,
then reads and writes the 33,152-byte image in blocks. APRS uses read `0x54`
and write `0x58`.

## Credits and licence

* Bluetooth version by Nivin Goonesekera (VK3NWG).
* Original USB-cable driver by Nathan Barguss (2E0NBS):
  <https://github.com/NathanBarguss/Chirp_Radtel-RT-950-Pro>.
* Built on the open-source CHIRP project.

MIT licence, see [LICENSE](LICENSE).
