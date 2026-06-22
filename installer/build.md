# Building `RT950-Setup.exe`

The installer is an [Inno Setup](https://jrsoftware.org/isinfo.php) script,
[`RT950-Setup.iss`](RT950-Setup.iss). It needs **Inno Setup 6.1 or newer** (the
built-in download support used for fetching Python/com0com landed in 6.1).

## One-time: get the compiler

```powershell
winget install --id JRSoftware.InnoSetup -e
```

## Compile

From this `installer/` folder:

```powershell
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" RT950-Setup.iss
```

`OutputDir=..` in the script puts the result one level up, at
`release/RT950-Setup.exe`.

## What the script bundles vs downloads

* **Bundled** (pulled from `release/` at compile time): the two drivers
  (`radtel_rt950pro_BLE_int.py`, `radtel_rt950pro_BL.py`), the bridge
  (`ble_bridge.py`), the GUI (`bridge_gui.py`), `icon.ico` / `icon.png`,
  `README.md`, `LICENSE`.
* **Downloaded at install time** (so the repo stays small; needs internet during
  install):
  * Python 3.10.11 amd64 — `python.org`
  * com0com 3.0.0.0 signed zip — SourceForge
  * `bleak` + `pyserial` — via `pip`

If a download URL ever 404s, bump the `PyVer` / `Com0comUrl` `#define`s at the top
of the `.iss` and recompile.

## Shipping it

`RT950-Setup.exe` is a build artifact, not source. Don't commit the binary; attach
it to a **GitHub Release** of the public repo instead, and point the README's
download link at that release asset.

## Notes / gotchas

* The installer requires **admin** (com0com installs a kernel driver; Python is
  all-users).
* com0com is only installed if it isn't already present, and the COM10↔COM11 pair
  is created only on that fresh install — re-running the installer won't stack
  duplicate pairs. A user who already has com0com keeps their existing pairs; they
  just pick the right port in the GUI.
* The Bridge GUI shortcut launches via `pyw.exe -3.10`, so the GUI (and the
  `ble_bridge.py` child it spawns with `sys.executable`) always runs on the 3.10
  that has `bleak`/`pyserial`.
