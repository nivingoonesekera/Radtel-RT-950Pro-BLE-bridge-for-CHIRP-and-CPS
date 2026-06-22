"""Tkinter launcher for ble_bridge.py (works with both the Radtel CPS .exe and
CHIRP — the bridge underneath is a dumb byte pipe, so it doesn't care which app
drives the COM port).

This is a thin front-end, not a second implementation of the bridge. It only:
  1. continuously scans for BLE devices (bleak) so you can pick the radio and
     see its live signal strength (RSSI) before you connect,
  2. lists every local COM port (pyserial + the Windows registry, so com0com
     virtual ports show up too),
  3. exposes the ble_bridge.py flags as plain-language checkboxes / radios,
  4. spawns `python ble_bridge.py <port> --addr <mac> [flags]` as a child
     process and streams its stdout/stderr into the colored log pane.

The hardware-proven byte pipe stays entirely inside ble_bridge.py — this file
never touches BLE or serial *data* itself (the live RSSI scan is passive and is
paused while the bridge owns the radio). Stop simply terminates the child (same
as Ctrl+C); Ctrl+C in the launching terminal also cleanly closes the window.

Run:  python bridge_gui.py

Copyright (c) 2026 Nivin Goonesekera - VK3NWG. MIT License (see LICENSE).
Part of the Radtel BT-RT950PRO BLE bridge project.
"""

import asyncio
import os
import queue
import re
import subprocess
import sys
import threading
import time

import tkinter as tk
from tkinter import ttk

from serial.tools import list_ports

_HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(_HERE, "ble_bridge.py")
ICON_PNG = os.path.join(_HERE, "icon.png")
ICON_ICO = os.path.join(_HERE, "icon.ico")
DEFAULT_ADDR = "E4:66:E5:78:28:3C"  # the original hardcoded unit, as a fallback


def _set_app_id():
    """On Windows the taskbar groups by AppUserModelID; without an explicit one
    it shows python.exe's icon. Set our own so our window/taskbar icon is used."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "VK3NWG.RadtelBTRT950PRO.BLEBridge")
    except Exception:
        pass

# How stale an RSSI reading may be (seconds) before we show it as "searching".
# The radio advertises every second or two, so allow a few missed beacons.
RSSI_STALE_AFTER = 6.0

# Two color palettes for the whole window. "dark" is night-friendly; "light" is
# the conventional look. Every widget (incl. the tk.Canvas RSSI bar and the log
# pane + its severity tags) is recolored from these so nothing stays a stray
# black box against the rest of the GUI.
THEMES = {
    "dark": {
        "bg": "#1b2228", "fg": "#d6e2ea", "entry_bg": "#243038",
        "entry_fg": "#d6e2ea", "select_bg": "#2f4150", "muted": "#7c8b97",
        "log_bg": "#101418", "log_fg": "#d6e2ea",
        "ok": "#5fd17a", "warn": "#e8c34a", "err": "#ff6b6b",
        "bar_bg": "#243038", "bar_outline": "#5a6b78",
    },
    "light": {
        "bg": "#f2f3f5", "fg": "#1b2228", "entry_bg": "#ffffff",
        "entry_fg": "#1b2228", "select_bg": "#cfe3f3", "muted": "#6b7780",
        "log_bg": "#fbfbfb", "log_fg": "#1b2228",
        "ok": "#1e8e3e", "warn": "#a9791c", "err": "#c5221f",
        "bar_bg": "#e3e6e8", "bar_outline": "#9aa6ad",
    },
}


def list_com_ports():
    """Every COM port the system knows about, as [(device, description)].

    The description is the friendly name you'd see in Device Manager (e.g.
    "com0com - serial port emulator (COM11)", "USB-SERIAL CH340 (COM3)"), with
    the manufacturer appended when it adds anything.

    pyserial's comports() is built on SetupAPI enumeration, which is known to
    miss some virtual ports (notably com0com null-modem pairs). We merge in the
    registry's SERIALCOMM map — the authoritative list of every COM *name* the
    system has handed out — so com0com COM10/COM11 etc. always appear.
    """
    found = {}
    for p in list_ports.comports():
        desc = p.description or p.device
        if p.manufacturer and p.manufacturer.lower() not in desc.lower():
            desc = f"{desc}  ·  {p.manufacturer}"
        found[p.device] = desc
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
        i = 0
        while True:
            try:
                source, name, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            # `name` is the COM port (e.g. "COM11"); `source` the device path.
            # Only used for ports SetupAPI missed, so dress it up a little.
            found.setdefault(name, f"{source}  (virtual / no driver details)")
            i += 1
        winreg.CloseKey(key)
    except Exception:
        pass

    def _num(dev):
        m = re.search(r"(\d+)", dev)
        return (int(m.group(1)) if m else 0, dev)

    return sorted(found.items(), key=lambda kv: _num(kv[0]))


class BleScanner(threading.Thread):
    """Background passive BLE scan that keeps a live {mac: name, rssi} table.

    Runs its own asyncio loop. Can be paused (we pause it while the bridge child
    owns the radio, both to free the adapter and because a connected radio stops
    advertising, so there'd be nothing to read anyway).
    """

    def __init__(self):
        super().__init__(daemon=True)
        self._devices = {}            # MAC(upper) -> {"name","rssi","t"}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.error = None

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _main(self):
        from bleak import BleakScanner

        def cb(device, adv):
            with self._lock:
                self._devices[device.address.upper()] = {
                    "name": device.name or adv.local_name or "(unknown)",
                    "rssi": adv.rssi,
                    "t": time.monotonic(),
                }

        while not self._stop.is_set():
            if self._paused.is_set():
                await asyncio.sleep(0.3)
                continue
            scanner = BleakScanner(detection_callback=cb)
            try:
                await scanner.start()
                self.error = None
                while not self._stop.is_set() and not self._paused.is_set():
                    await asyncio.sleep(0.3)
            except Exception as exc:
                self.error = repr(exc)
                await asyncio.sleep(1.0)
            finally:
                try:
                    await scanner.stop()
                except Exception:
                    pass

    def snapshot(self):
        with self._lock:
            return {k: dict(v) for k, v in self._devices.items()}

    def rssi_for(self, mac):
        if not mac:
            return None
        with self._lock:
            d = self._devices.get(mac.upper())
            if d and time.monotonic() - d["t"] <= RSSI_STALE_AFTER:
                return d["rssi"]
        return None

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stop.set()


def classify(line):
    """Pick a color tag for a bridge/log line: ok / warn / err / None."""
    low = line.lower()
    err = ("!!!", "failed", "error", "traceback", "exception",
           "could not get", "link lost", "lost;", "lost (", "byte(s)")
    warn = ("no-unlock", "no unlock", "could not", "incomplete", "retry",
            "retrying", "reconnect", "corrupt", "caveat", "warning", "timeout",
            "skipping", "drop", "experiments only", "almost certainly fail")
    ok = ("bridge live", "connected", "unlock sent", "ff31 unlock", "opened ",
          "scan done", "write returned ok", " ok ", "present", "live:",
          "exited (code 0)")
    if any(k in low for k in err):
        return "err"
    if any(k in low for k in warn):
        return "warn"
    if any(k in low for k in ok):
        return "ok"
    return None


class App:
    def __init__(self, root):
        self.root = root
        root.title("Radtel BT-RT950PRO BLE Bridge")
        # Window + taskbar icon. The .ico via iconbitmap gives a crisp Windows
        # taskbar/title-bar icon; the PNG via iconphoto is the cross-platform
        # fallback. Keep the PhotoImage reference so it isn't garbage-collected.
        try:
            root.iconbitmap(default=ICON_ICO)
        except Exception:
            pass
        try:
            self._icon_img = tk.PhotoImage(file=ICON_PNG)
            root.iconphoto(True, self._icon_img)
        except Exception:
            pass
        self.proc = None
        self.log_q = queue.Queue()
        self.devices = []             # [(label, mac)]  — labels are STABLE
        self.sel_mac = None           # selected MAC, tracked independent of label
        self.ports = []               # [(label, device)] for the COM dropdown
        self.style = ttk.Style(root)
        self.dark = tk.BooleanVar(value=True)   # night-friendly by default
        self.pal = THEMES["dark"]
        self._rssi_hidden = False               # bar hidden while bridge runs
        self.bridge_ready = False               # True once the bridge reports live

        self.scanner = BleScanner()
        self.scanner.start()

        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(root, padding=10)
        frm.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        # --- Device (BLE) ---
        ttk.Label(frm, text="Radio (BLE):").grid(row=0, column=0, sticky="w", **pad)
        self.dev_var = tk.StringVar()
        self.dev_cb = ttk.Combobox(frm, textvariable=self.dev_var, state="readonly")
        self.dev_cb.grid(row=0, column=1, sticky="ew", **pad)
        self.dev_cb.bind("<<ComboboxSelected>>", self._on_pick_device)
        self.scan_btn = ttk.Button(frm, text="Rescan", command=self.on_scan)
        self.scan_btn.grid(row=0, column=2, **pad)

        # --- Live signal strength (RSSI) ---
        ttk.Label(frm, text="Signal (RSSI):").grid(row=1, column=0, sticky="w", **pad)
        sig = ttk.Frame(frm)
        sig.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        # Hand-drawn bar so we can color it (ttk Progressbar ignores color on the
        # Windows theme). Green/yellow/red by signal quality.
        self.rssi_canvas = tk.Canvas(sig, width=180, height=16, bd=0,
                                     highlightthickness=1,
                                     highlightbackground="#5a6b78",
                                     background="#1b2228")
        self.rssi_canvas.pack(side="left")
        self.rssi_lbl = tk.Label(sig, text="Scanning…", width=30, anchor="w")
        self.rssi_lbl.pack(side="left", padx=8)

        # --- COM port (dropdown shows the friendly name next to each port) ---
        ttk.Label(frm, text="COM port (bridge side):").grid(
            row=2, column=0, sticky="w", **pad)
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(frm, textvariable=self.port_var)
        self.port_cb.grid(row=2, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Refresh", command=self.refresh_ports).grid(
            row=2, column=2, **pad)

        # --- Options (plain language; the GUI builds the flags for you) ---
        opt = ttk.LabelFrame(frm, text="Options", padding=8)
        opt.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        opt.columnconfigure(0, weight=1)
        self.fast = tk.BooleanVar(value=False)
        self.no_unlock = tk.BooleanVar(value=False)
        self.verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt, text="Fast connection — speeds up reads, but can corrupt the "
            "APRS block. Leave off unless reads are too slow.",
            variable=self.fast).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(
            opt, text="Skip unlock — for experiments only; the radio needs the "
            "unlock, so the handshake will fail without it.",
            variable=self.no_unlock).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(
            opt, text="Verbose log — print every frame in/out as hex (noisy; "
            "useful for debugging).",
            variable=self.verbose).grid(row=2, column=0, columnspan=4, sticky="w")

        ttk.Label(opt, text="Write mode:").grid(
            row=3, column=0, sticky="w", pady=(8, 0))
        self.wmode = tk.StringVar(value="auto")
        modes = [
            ("Auto (recommended)", "auto",
             "Auto: let the radio decide. The bridge reads the characteristic's "
             "advertised write type and picks flow-controlled writes when "
             "offered. Best default for both CPS and CHIRP."),
            ("With response", "rsp",
             "With response: wait for the radio to confirm each chunk before "
             "sending the next (flow control, like the USB cable). Slower but "
             "the most reliable, especially through the APRS flash commit."),
            ("Without response", "norsp",
             "Without response: blast chunks back-to-back with no confirmation. "
             "Fastest (matches the old CHIRP behaviour) but no backpressure, so "
             "it can be flaky on a weak link or during the APRS commit."),
        ]
        self.wmode_help = {v: h for _, v, h in modes}
        for i, (lab, val, _h) in enumerate(modes):
            ttk.Radiobutton(
                opt, text=lab, value=val, variable=self.wmode,
                command=self._update_wmode_help).grid(
                row=4, column=i, sticky="w", padx=(0, 10))
        self.wmode_lbl = ttk.Label(opt, text="", wraplength=620,
                                   foreground="#5a6b78")
        self.wmode_lbl.grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self._update_wmode_help()

        # --- Start / Stop ---
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.start_btn = ttk.Button(btns, text="Start Bridge", command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.on_stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.status = ttk.Label(btns, text="Idle")
        self.status.pack(side="left", padx=12)
        ttk.Checkbutton(btns, text="Dark mode", variable=self.dark,
                        command=self.apply_theme).pack(side="right")

        # --- Log pane ---
        self.log = tk.Text(frm, height=18, width=92, wrap="none",
                           background="#101418", foreground="#d6e2ea",
                           insertbackground="#d6e2ea")
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(5, weight=1)
        sb = ttk.Scrollbar(frm, command=self.log.yview)
        sb.grid(row=5, column=3, sticky="ns")
        self.log["yscrollcommand"] = sb.set

        self.apply_theme()        # paint everything from the active palette
        self.refresh_ports()
        self.root.after(100, self._drain_log)
        self.root.after(500, self._update_rssi)
        self.root.after(2000, self._auto_refresh_devices)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- helpers ----
    def append(self, text, tag=None):
        self.log.insert("end", text, (tag,) if tag else ())
        self.log.see("end")

    def log_gui(self, text):
        """A line from the GUI itself (no [gui] prefix shown), auto-colored."""
        if not text.endswith("\n"):
            text += "\n"
        self.append(text, classify(text))

    def apply_theme(self):
        """Repaint every widget from the active (dark/light) palette."""
        pal = THEMES["dark" if self.dark.get() else "light"]
        self.pal = pal
        st = self.style
        st.theme_use("clam")          # clam honors our color overrides
        st.configure(".", background=pal["bg"], foreground=pal["fg"],
                     fieldbackground=pal["entry_bg"])
        for w in ("TFrame", "TLabel", "TLabelframe", "TLabelframe.Label",
                  "TCheckbutton", "TRadiobutton"):
            st.configure(w, background=pal["bg"], foreground=pal["fg"])
        st.configure("TButton", background=pal["entry_bg"], foreground=pal["fg"])
        st.map("TButton",
               background=[("active", pal["select_bg"]), ("disabled", pal["bg"])],
               foreground=[("active", pal["fg"]), ("disabled", pal["muted"])])
        st.configure("TScrollbar", background=pal["entry_bg"],
                     troughcolor=pal["bg"], arrowcolor=pal["fg"],
                     bordercolor=pal["bg"])
        st.map("TScrollbar", background=[("active", pal["select_bg"])])
        # Keep check/radio text readable on hover: clam's default "active"
        # background is light and would wash out the label in dark mode.
        for w in ("TCheckbutton", "TRadiobutton"):
            st.map(w,
                   background=[("active", pal["bg"])],
                   foreground=[("active", pal["fg"]), ("disabled", pal["muted"])])
        st.configure("TCombobox", fieldbackground=pal["entry_bg"],
                     background=pal["entry_bg"], foreground=pal["entry_fg"],
                     arrowcolor=pal["fg"])
        st.map("TCombobox",
               fieldbackground=[("readonly", pal["entry_bg"]),
                                ("active", pal["entry_bg"])],
               foreground=[("readonly", pal["entry_fg"])],
               selectbackground=[("readonly", pal["entry_bg"])],
               selectforeground=[("readonly", pal["entry_fg"])],
               arrowcolor=[("active", pal["fg"])])
        # The combobox dropdown popup is a tk Listbox; the option DB is a hint…
        self.root.option_add("*TCombobox*Listbox.background", pal["entry_bg"])
        self.root.option_add("*TCombobox*Listbox.foreground", pal["entry_fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", pal["select_bg"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", pal["fg"])
        # …but configuring each popup listbox directly is what actually sticks.
        for combo in (self.dev_cb, self.port_cb):
            self._theme_combo_popup(combo, pal)
        self.root.configure(background=pal["bg"])
        # Plain tk widgets (not ttk) need their colors set directly.
        self.rssi_canvas.configure(background=pal["bar_bg"],
                                   highlightbackground=pal["bar_outline"])
        self.rssi_lbl.configure(background=pal["bg"])
        self.log.configure(background=pal["log_bg"], foreground=pal["log_fg"],
                           insertbackground=pal["log_fg"])
        self.log.tag_config("ok", foreground=pal["ok"])
        self.log.tag_config("warn", foreground=pal["warn"])
        self.log.tag_config("err", foreground=pal["err"])
        self.wmode_lbl.configure(foreground=pal["muted"])

    def _theme_combo_popup(self, combo, pal):
        """Color a combobox's dropdown list to match the theme. Reaching into
        the popdown window is the only reliable way across Tk versions."""
        try:
            popdown = combo.tk.call("ttk::combobox::PopdownWindow", combo)
            combo.tk.call(f"{popdown}.f.l", "configure",
                          "-background", pal["entry_bg"],
                          "-foreground", pal["entry_fg"],
                          "-selectbackground", pal["select_bg"],
                          "-selectforeground", pal["fg"])
        except tk.TclError:
            pass

    def _update_wmode_help(self):
        self.wmode_lbl.config(text=self.wmode_help.get(self.wmode.get(), ""))

    def refresh_ports(self):
        info = list_com_ports()                # [(device, description)]
        # Show the friendly name right in the dropdown, e.g.
        # "COM11  —  com0com - serial port emulator". The bare device is kept so
        # we can hand just "COM11" to the bridge.
        self.ports = [
            (f"{dev}  —  {desc}" if desc and desc != dev else dev, dev)
            for dev, desc in info
        ]
        self.port_cb["values"] = [lab for lab, _ in self.ports]
        cur = self.selected_port()
        target = cur if any(dev == cur for _, dev in self.ports) else None
        if target is None:                     # default to COM10, then first port
            target = next((d for _, d in self.ports if d.upper() == "COM10"),
                          self.ports[0][1] if self.ports else "")
        for lab, dev in self.ports:
            if dev == target:
                self.port_var.set(lab)
                break
        else:
            self.port_var.set(target)

    def selected_port(self):
        """The bare device name (e.g. "COM11") behind the chosen dropdown label;
        falls back to whatever the user typed if it isn't one of the labels."""
        text = self.port_var.get().strip()
        for lab, dev in self.ports:
            if lab == text:
                return dev
        return text

    def _on_pick_device(self, _evt=None):
        """User chose an entry: remember its MAC (not its label, which the live
        RSSI readout would otherwise keep changing under us)."""
        label = self.dev_var.get()
        for lab, mac in self.devices:
            if lab == label:
                self.sel_mac = mac
                break

    def selected_mac(self):
        return self.sel_mac

    # ---- scan / device list ----
    def on_scan(self):
        """Repopulate the device list from the live scanner snapshot. A short
        delay lets fresh advertisements arrive and keeps the "Scanning…" status
        visible (the background scan itself is always running)."""
        self.log_gui("Rescanning for BLE devices…")
        if self.proc is None:
            self.status.config(text="Scanning…")
        self.root.after(700, self._finish_scan)

    def _finish_scan(self):
        self._apply_scan(self.scanner.snapshot())
        if self.proc is None:
            self.status.config(text="Idle")

    def _auto_refresh_devices(self):
        # Keep the dropdown fresh from the continuous scan without stomping the
        # user's current selection.
        if self.proc is None:
            self._apply_scan(self.scanner.snapshot(), announce=False)
        self.root.after(1000, self._auto_refresh_devices)

    def _apply_scan(self, snap, announce=True):
        # snap: {mac: {"name","rssi","t"}}. Labels stay STABLE (name + MAC, no
        # RSSI) so the live signal readout never disturbs the dropdown selection.
        items = sorted(
            snap.items(),
            key=lambda kv: (kv[1]["name"].startswith("(unknown)"),
                            kv[1]["name"].lower()))
        found = [(f"{v['name']}  [{mac}]", mac) for mac, v in items]
        self.devices = found
        self.dev_cb["values"] = [lab for lab, _ in found]
        # Pick a default the first time we see devices; otherwise keep the user's
        # choice (tracked by MAC) and just refresh its label text.
        if self.sel_mac is None and found:
            default = next((m for _, m in found
                            if m.upper() == DEFAULT_ADDR.upper()), found[0][1])
            self.sel_mac = default
        for lab, mac in found:
            if mac == self.sel_mac:
                self.dev_var.set(lab)
                break
        if announce:
            self.log_gui(f"Found {len(found)} device(s) in range")

    # ---- live RSSI ----
    def _show_rssi_bar(self, visible, pct=0, color=None):
        """Show or hide the signal bar. The bar only makes sense when there's an
        actual reading; the rest of the time it's hidden and the label slides
        left (padx 0) so its text stays flush with the dropdown above it."""
        if visible and self._rssi_hidden:
            self.rssi_canvas.pack(side="left", before=self.rssi_lbl)
            self.rssi_lbl.pack_configure(padx=8)
            self._rssi_hidden = False
        elif not visible and not self._rssi_hidden:
            self.rssi_canvas.pack_forget()
            self.rssi_lbl.pack_configure(padx=(0, 8))
            self._rssi_hidden = True
        if visible:
            self.rssi_canvas.delete("all")
            w = int(176 * max(0, min(100, pct)) / 100)
            if w > 0:
                self.rssi_canvas.create_rectangle(2, 2, 2 + w, 14,
                                                  fill=color, outline="")

    def _update_rssi(self):
        pal = self.pal
        GREY = pal["muted"]
        # Decide the (bar?, text, color); the bar appears ONLY for a real reading.
        if self.proc is not None:
            # Bridge owns the device: no advertisement to measure, so no bar.
            if self.bridge_ready:
                text, fg = "Connected to the bridge", pal["ok"]      # green
            else:
                text, fg = "Connecting…", pal["warn"]                # yellow
            self._show_rssi_bar(False)
        else:
            mac = self.selected_mac()
            rssi = self.scanner.rssi_for(mac)
            if self.scanner.error:
                self._show_rssi_bar(False)
                text, fg = "Scan error (see log)", pal["err"]
            elif not mac:
                self._show_rssi_bar(False)
                if not self.devices:
                    text, fg = "Searching for radios…", pal["warn"]  # yellow
                else:
                    text, fg = "Select a radio above", GREY
            elif rssi is None:
                self._show_rssi_bar(False)
                text, fg = "Searching for signal…", pal["warn"]      # yellow
            else:
                # The device's BLE module is low-power, so even up close it rarely
                # beats ~-55 dBm. Map -95..-45 dBm onto 0..100% and grade it.
                pct = max(0, min(100, (rssi + 95) * 2))
                if rssi >= -60:
                    color, quality = "#3fbf5f", "excellent"   # green
                elif rssi >= -72:
                    color, quality = "#8ac926", "good"         # light green
                elif rssi >= -83:
                    color, quality = "#e8c34a", "fair"         # yellow
                else:
                    color, quality = "#ff6b6b", "weak"         # red
                self._show_rssi_bar(True, pct, color)
                text, fg = f"{rssi} dBm  ({quality})", color
        self.rssi_lbl.config(text=text, fg=fg)
        self.root.after(500, self._update_rssi)

    # ---- start / stop ----
    def build_cmd(self):
        mac = self.selected_mac() or DEFAULT_ADDR
        # --gui tells the bridge to print GUI-friendly status (no terminal-only
        # "Ctrl+C to stop" hints).
        cmd = [sys.executable, "-u", BRIDGE, self.selected_port(), "--addr", mac,
               "--gui"]
        if self.fast.get():
            cmd.append("--fast")
        if self.no_unlock.get():
            cmd.append("--no-unlock")
        if self.verbose.get():
            cmd.append("-v")
        if self.wmode.get() == "rsp":
            cmd.append("--rsp")
        elif self.wmode.get() == "norsp":
            cmd.append("--norsp")
        return cmd

    def on_start(self):
        if self.proc is not None:
            return
        cmd = self.build_cmd()
        self.bridge_ready = False
        # Free the BLE adapter for the bridge child while it owns the device.
        self.scanner.pause()
        self.log_gui("Launching: " + " ".join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self.log_gui(f"Failed to launch: {exc!r}")
            self.proc = None
            self.scanner.resume()
            return
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.config(text="Running")
        threading.Thread(target=self._read_proc, args=(self.proc,),
                         daemon=True).start()

    def _read_proc(self, proc):
        for line in proc.stdout:
            self.log_q.put(("line", line))
        proc.wait()
        self.log_q.put(("exit", proc.returncode))

    def on_stop(self):
        if self.proc is None:
            return
        self.log_gui("Stopping bridge…")
        try:
            self.proc.terminate()
        except Exception:
            pass

    def _drain_log(self):
        try:
            while True:
                kind, payload = self.log_q.get_nowait()
                if kind == "line":
                    # Track the link state from the bridge's own status lines so
                    # the RSSI label can show "Connecting…" vs "Connected".
                    text = payload
                    low = text.lower()
                    if "bridge live" in low:
                        self.bridge_ready = True
                    elif ("link lost" in low
                          or "dropped the ble connection" in low):
                        self.bridge_ready = False
                    self.append(text, classify(text))
                elif kind == "exit":
                    self.log_gui(f"Bridge exited (code {payload})")
                    self.bridge_ready = False
                    self.proc = None
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.config(text="Idle")
                    self.scanner.resume()
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def on_close(self):
        self.scanner.stop()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.root.destroy()


def main():
    _set_app_id()                 # must run before the window is created
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
