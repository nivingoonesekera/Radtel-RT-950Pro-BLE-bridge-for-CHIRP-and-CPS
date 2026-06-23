# Building `RT950-Setup.exe`

The installer is an [Inno Setup](https://jrsoftware.org/isinfo.php) script,
[`RT950-Setup.iss`](RT950-Setup.iss). It needs **Inno Setup 6.1 or newer** (the
built-in download support used for fetching Python landed in 6.1).

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
  `README.md`, `NEXT-STEPS.txt`, `LICENSE`.
* **Downloaded at install time** (so the repo stays small; needs internet during
  install):
  * Python 3.10.11 amd64 — `python.org`
  * `bleak` + `pyserial` — via `pip`

**com0com is no longer bundled or downloaded.** v3 is unreliable on Windows 11, so
the installer leaves it to the user (NEXT-STEPS.txt points them at the working
v2.2.2). This also removed the old `CreateProcess failed; code 2 … com0com\setup.exe`
failure (a silent unzip miss left no `setup.exe`).

If a download URL ever 404s, bump the `PyVer` `#define` at the top of the `.iss`
and recompile.

## Python: user chooses how

The wizard shows a **"Python setup"** page (a `CreateInputOptionPage`):

* **Use my system Python 3.10** (default) — installs 3.10 only if `py -3.10` is
  missing, then `pip install bleak pyserial`. PATH untouched. GUI shortcut runs
  via `pyw -3.10`.
* **Install a private Python 3.10 inside the app** — a self-contained
  `python.org` install into `{app}\python310` (`TargetDir=… Include_tcltk=1
  Include_pip=1 InstallAllUsers=0`, so Tk works and nothing global is touched),
  then pip into it. GUI shortcut runs that interpreter's `pythonw.exe`, and a
  `RT950_BLEAK_SITE` env var points the integrated CHIRP driver at its
  site-packages. Removed on uninstall.

Use the **full** python.org installer (not the embeddable zip) — the embeddable
distro has no Tkinter (GUI wouldn't run) and no pip.

## Shipping it

`RT950-Setup.exe` is a build artifact, not source. Don't commit the binary; attach
it to a **GitHub Release** of the public repo instead, and point the README's
download link at that release asset.

## Notes / gotchas

* The installer requires **admin** — for the Program Files install dir and the
  all-users system-Python option. (It no longer installs a kernel driver; com0com
  is the user's job now.)
* **Non-intrusive to existing Python** (system mode). It installs Python 3.10
  **only if a working 3.10 isn't already present** (checked via `py -3.10`), uses
  **no `PrependPath`** (your PATH / default `python` is untouched), and pip-installs
  `bleak`/`pyserial` **without `--upgrade`** (already-present copies keep their
  version). Other Python versions are in separate folders and are never touched.
* **com0com is not installed** by the setup — by design (v3 is unreliable on Win11).
  `NEXT-STEPS.txt` (shown on the final page and bundled into `{app}`) walks the user
  through installing v2.2.2 and making a port pair.
* The Bridge GUI shortcut launches via `pyw.exe -3.10` (system mode) or the bundled
  `{app}\python310\pythonw.exe` (private mode), so the GUI — and the `ble_bridge.py`
  child it spawns with `sys.executable` — always runs on the Python that has
  `bleak`/`pyserial`.
