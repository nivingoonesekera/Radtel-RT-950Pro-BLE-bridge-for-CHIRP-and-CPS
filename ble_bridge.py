"""BLE <-> serial bridge that lets the official Radtel BT-RT950PRO CPS talk to
the radio over Bluetooth as if it were the USB programming cable.

Goal:
The CPS .exe speaks a plain UART protocol (115200 8N1: the "PROGRAMBT9000U"
handshake, then R/T reads and W/X writes of the EEPROM, with XOR encryption —
see BT-RT950PRO_CPS.exe_Decompiled/KDH/RWDataOperation.cs). Over the USB cable
that UART is transparent. This bridge makes BLE look identical: it does ONLY the
BLE connection + one-time unlock, then pipes bytes verbatim between a virtual COM
port and the radio's ffe1 characteristic. The CPS drives the entire protocol;
the bridge never parses, reorders, or answers it — it is a dumb cable.

Why a custom bridge (not ble-serial):
The radio refuses all traffic on ffe1 until it receives a one-time "unlock" write
on a *second* characteristic, ff31 (captured in gattattack.txt). ble-serial can
only drive a single write characteristic, so it can never both unlock (ff31) and
carry data (ffe1). This bridge does the unlock once at connect, then transparently
pipes ffe1 <-> the serial port. The unlock challenge is replayed verbatim from the
capture; the radio's response is deterministic and a replay is accepted, so no
live challenge computation is needed.

The radio occasionally drops the BLE link mid-session (observed right after the
APRS write block, most likely a flash-commit stall tripping the supervision
timeout). The bridge detects the disconnect, logs it with a timestamp, and
automatically reconnects + re-unlocks so the next CPS Read/Write just works
without restarting anything.

Topology:
    radio  <--BLE-->  ble_bridge.py  <--COM10 | COM11 (com0com)-->  CPS .exe

Set up a com0com virtual null-modem pair (e.g. COM10<->COM11). Run this bridge on
one side (COM10) and point the CPS port-select dialog at the other side (COM11).
Both ends are 115200 8N1.

Note on timing: each CPS read/write block is 132 bytes and CPS retries a block if
it doesn't complete within ~1 s (the 1000 ms timer in RWDataOperation). Over BLE
each 132-byte block arrives as ~7 notifications spaced one connection interval
apart, so a very slow interval can trip that timeout. The default interval is the
slow/reliable one; if CPS reports timeouts on reads, try --fast (but see the APRS
caveat below).

Usage:
    python ble_bridge.py             # defaults: COM10, slow/reliable interval
    python ble_bridge.py COM10       # pick the COM port the bridge opens
    python ble_bridge.py COM10 -v    # verbose: print every frame in/out as hex
    python ble_bridge.py COM10 --fast
                                     # opt into the throughput-optimized 7.5 ms
                                     # interval: faster reads, but known to
                                     # corrupt/drop the APRS write block. Default
                                     # (no flag) is the slow, reliable interval.
    python ble_bridge.py COM10 --no-unlock
                                     # skip the ff31 unlock (experiments only;
                                     # the radio needs it, so the handshake will
                                     # fail without it). Unlock is ON by default.
    python ble_bridge.py COM10 --rsp / --norsp
                                     # force ffe1 write WITH / WITHOUT response
                                     # (default: auto from the characteristic's
                                     # advertised properties)

Output is line-buffered, so you see [bridge] lines live in your terminal as
they happen (no need for `python -u`).

Copyright (c) 2026 Nivin Goonesekera - VK3NWG. MIT License (see LICENSE).
Part of the Radtel BT-RT950PRO BLE bridge project.
"""

import asyncio
import sys
import time

import serial
from bleak import BleakClient

# Flush each line immediately so progress shows live in any console.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

ARGS = [a for a in sys.argv[1:]]
VERBOSE = "-v" in ARGS or "--verbose" in ARGS
# Connection-interval policy. The radio's BLE module is cheap and its preferred
# interval is slow (100-200 ms, per its 0x2a04 value). Requesting Windows'
# "throughput-optimized" 7.5 ms interval speeds up reads a lot, BUT the module
# can't hold that pace through the APRS flash commit and the link corrupts/drops
# on the final APRS block. ble-serial never requested fast params -> slow but
# reliable, which is the behavior the developer saw working. So DEFAULT IS SLOW
# (reliable); pass --fast to opt into the throughput-optimized interval.
FAST = "--fast" in ARGS
# ff31 "unlock" write (captured from the mobile BLE app, replayed once at connect).
# This is REQUIRED and ON by default: the radio's BLE module will not carry the
# CPS protocol — not even the PROGRAMBT9000U handshake — until it receives this
# write on ff31. (Proven empirically: with --no-unlock the handshake fails. The
# CPS itself cannot do it: it is a serial/COM app with no BLE awareness, so it
# can't write a GATT characteristic.) --no-unlock is left only for experiments.
DO_UNLOCK = "--no-unlock" not in ARGS
# Write type for ffe1. A BLE characteristic advertises which write types it
# supports ("write" = with-response, "write-without-response"). By DEFAULT the
# bridge auto-picks from those advertised properties (preferring with-response
# for flow control when available). Force it only for diagnostics:
#   --rsp    force write-WITH-response
#   --norsp  force write-WITHOUT-response
FORCE_RSP = "--rsp" in ARGS
FORCE_NORSP = "--norsp" in ARGS
# Set by bridge_gui.py when it launches us. Only affects wording: the GUI has
# Start/Stop buttons, so the terminal-only "Ctrl+C to stop" hints are suppressed
# and the "live" line becomes a simple "ready" message.
IS_GUI = "--gui" in ARGS
def _arg_value(name, default):
    """Read `--name VALUE` (or `--name=VALUE`) from ARGS, else return default."""
    for i, a in enumerate(ARGS):
        if a == name and i + 1 < len(ARGS):
            return ARGS[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


# BLE MAC of the radio. Defaults to the original hardcoded unit so existing
# command lines keep working; the GUI passes --addr from its scan list.
ADDR = _arg_value("--addr", "E4:66:E5:78:28:3C")

# The COM port is the first bare (non-flag) argument. Skip the value that
# follows a "--addr" so the MAC isn't mistaken for the port.
_skip = set()
for _i, _a in enumerate(ARGS):
    if _a == "--addr" and _i + 1 < len(ARGS):
        _skip.add(_i + 1)
PORT = next(
    (a for i, a in enumerate(ARGS) if not a.startswith("-") and i not in _skip),
    "COM10",
)

# Command byte of the APRS *write* frame, used to recognise the flash-commit
# block on the wire so the bridge can pace it gently. This is the 'X' (0x58)
# command the CPS sends for the APRS region: see WriteRadioData() in
# BT-RT950PRO_CPS.exe_Decompiled/KDH/RWDataOperation.cs, where after the main
# EEPROM it sets the command byte to 88 (0x58) and writes addr 0, len 0x80. The
# 4-byte header is plaintext on the wire (XOR encryption only covers the 128
# payload bytes that follow), so this marker is reliable. Keep it in sync if the
# CPS ever changes which command commits APRS.
APRS_WRITE_CMD = 0x58
APRS_MARKER = bytes((APRS_WRITE_CMD, 0x00, 0x00, 0x80))

# Boot-image (start-up logo) import uses a different, 0xA5-framed protocol
# (KDH/ImportBmpOperation.cs): every packet is <A5> <cmd> <id16> <len16> ... <crc16>
# and the radio replies with frames that also start with 0xA5. We only *log* these
# (never alter them) so a failed import shows exactly how far it got. 0xA5 = the
# Fram_Header in COMMAND_TYPE.cs; cmd 0x02 = Handshake.
BOOT_FRAME_HEADER = 0xA5
BOOT_HANDSHAKE_MARKER = bytes((BOOT_FRAME_HEADER, 0x02))

# Resolved at connect time from the ffe1 characteristic's advertised properties
# (overridable by --rsp / --norsp). True = write-with-response.
WRITE_WITH_RESPONSE = True


def _resolve_write_mode(char) -> bool:
    """Decide the ffe1 write type from its GATT properties (+ CLI overrides)."""
    if FORCE_RSP:
        return True
    if FORCE_NORSP:
        return False
    props = list(getattr(char, "properties", []) or [])
    has_rsp = "write" in props
    has_norsp = "write-without-response" in props
    print(f"[bridge] ffe1 properties: {props}")
    if has_rsp:
        return True          # prefer flow control when the radio offers it
    if has_norsp:
        return False         # module is write-without-response only
    # Nothing advertised (some stacks under-report); default to with-response.
    return True

NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"  # notify IN  (radio -> PC)
DATA_UUID   = "0000ffe1-0000-1000-8000-00805f9b34fb"  # write OUT  (PC -> radio)
UNLOCK_UUID = "0000ff31-0000-1000-8000-00805f9b34fb"  # one-time unlock (write)

# Fixed unlock frame from gattattack.txt; replay is accepted by the radio.
UNLOCK = bytes.fromhex("3F3F3F3F022E171D5E57252F57136256044B2342")

# Keep the connection-parameters request object alive for the whole session;
# the fast interval stays in effect only while this reference exists.
_conn_param_request = None


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _request_fast_connection(client):
    """Ask Windows for the fastest BLE connection interval (the Android trick).

    Each 132-byte block is delivered as ~7 notifications (MTU is pinned at
    23 by the radio), and the gap between notifications is the connection
    interval. Windows' default interval is slow/power-saving; requesting
    ThroughputOptimized drops it toward 7.5 ms, the same thing Android does,
    which is the single biggest read-speed win available to us. Best-effort:
    if the API isn't available we just keep the default interval.
    """
    global _conn_param_request
    try:
        from winrt.windows.devices.bluetooth import (
            BluetoothLEPreferredConnectionParameters as P,
        )

        device = client._backend._requester  # underlying WinRT BluetoothLEDevice
        _conn_param_request = device.request_preferred_connection_parameters(
            P.throughput_optimized
        )
        status = getattr(_conn_param_request, "status", None)
        print(f"[bridge] requested throughput-optimized connection (status={status})")
    except Exception as exc:  # pragma: no cover - platform/version dependent
        print(f"[bridge] could not request fast connection ({exc!r}); using default interval")


async def connect_and_unlock(ser, stats, disconnected: asyncio.Event):
    """Connect, verify the GATT table, start notify, unlock. Returns the client.

    Retries the connection: the radio's GATT table occasionally comes back
    incomplete on a fresh connection, so verify ffe1/ff31 are present and
    reconnect if not.
    """

    def on_disconnect(_client):
        t0 = stats.get("aprs_t0")
        extra = f" ({time.monotonic() - t0:.2f}s after APRS block sent)" if t0 else ""
        print(f"[bridge] !!! {_ts()} device dropped the BLE connection{extra}")
        disconnected.set()

    def on_notify(_char, data: bytearray):
        # Only relevant with --unlock: swallow the ff31 unlock reply (starts with
        # 0x21 "!") exactly once, before the CPS sends anything, so it can't land
        # in the CPS's first handshake read. Without --unlock the radio never
        # sends it, so the bridge swallows nothing and is a pure pipe.
        if (DO_UNLOCK and not stats["unlock_swallowed"] and stats["tx"] == 0
                and data and data[0] == 0x21):
            stats["unlock_swallowed"] = True
            if VERBOSE:
                print(f"  device->  (unlock reply, swallowed) {bytes(data).hex().upper()}")
            return
        stats["rx"] += len(data)
        stats["notifies"] += 1
        # After the APRS write block goes out, spotlight EVERY byte the radio
        # sends back, with timing, so we can see whether it NAKs, ACKs, or just
        # goes silent before the link drops.
        t0 = stats.get("aprs_t0")
        if t0 is not None:
            print(f"  [APRS] {_ts()} device->PC +{time.monotonic() - t0:.3f}s "
                  f"[{len(data):3}] {bytes(data).hex().upper()}")
        elif data and data[0] == BOOT_FRAME_HEADER:
            # Bootloader is alive and answering over BLE — proves the 'D' jump
            # did NOT kill the link. Log every reply so we can see how far the
            # import gets before any timeout/drop.
            print(f"  [BOOT] {_ts()} device->PC reply [{len(data):3}] "
                  f"{bytes(data).hex().upper()}")
        elif VERBOSE:
            print(f"  device->PC [{len(data):3}] {bytes(data).hex().upper()}")
        # radio -> PC: hand straight to the serial port for the CPS to read
        try:
            ser.write(bytes(data))
        except serial.SerialException:
            # Either the CPS isn't draining its port yet, or it has briefly
            # CLOSED its end (the boot-image import closes+reopens the port for
            # 1 s mid-session). Drop this chunk rather than crash the notify
            # callback or stall the BLE loop; the link must stay up across that
            # close so the radio doesn't get orphaned in programming mode.
            pass

    for attempt in range(1, 6):
        print(f"[bridge] connecting to {ADDR} (attempt {attempt}) ...")
        client = BleakClient(ADDR, disconnected_callback=on_disconnect)
        try:
            await client.connect()
        except Exception as exc:
            print(f"[bridge] connect failed ({exc}); retrying ...")
            await asyncio.sleep(1.5)
            continue
        notify_ch = client.services.get_characteristic(NOTIFY_UUID)
        unlock_ch = client.services.get_characteristic(UNLOCK_UUID)
        # ffe1 is mandatory (data + notify); ff31 is only needed for --unlock.
        if notify_ch is not None and (unlock_ch is not None or not DO_UNLOCK):
            print(f"[bridge] connected (mtu={client.mtu_size}); ffe1 present"
                  f"{' + ff31' if unlock_ch is not None else ''}")
            break
        print("[bridge] GATT table incomplete (ffe1 missing), reconnecting ...")
        await client.disconnect()
        await asyncio.sleep(1.0)
    else:
        raise SystemExit("[bridge] could not get a complete GATT table after 5 tries")

    # Pick the ffe1 write type from what the characteristic actually advertises.
    global WRITE_WITH_RESPONSE
    WRITE_WITH_RESPONSE = _resolve_write_mode(notify_ch)
    print(f"[bridge] ffe1 write mode: {'WITH' if WRITE_WITH_RESPONSE else 'WITHOUT'} response")

    if FAST:
        print("[bridge] --fast: requesting throughput-optimized interval "
              "(faster reads, but known to corrupt the APRS write block)")
        _request_fast_connection(client)
    else:
        print("[bridge] using the device's default (slow) connection interval "
              "for reliability — like ble-serial")
    await client.start_notify(notify_ch, on_notify)

    if DO_UNLOCK and unlock_ch is not None:
        print("[bridge] --unlock: sending one-time ff31 unlock ...")
        await client.write_gatt_char(UNLOCK_UUID, UNLOCK, response=True)
        await asyncio.sleep(0.4)
    else:
        print("[bridge] !!! --no-unlock: skipping ff31 unlock — the handshake "
              "will almost certainly fail; this is for experiments only")
    if IS_GUI:
        print(f"[bridge] {_ts()} bridge live and ready.")
    else:
        print(f"[bridge] {_ts()} bridge live. Start Read/Write in the CPS or CHIRP. Ctrl+C to stop.")
    return client, notify_ch


async def pipe(client, ser, stats, disconnected: asyncio.Event):
    """Pump the CPS's serial bytes to the radio until the link drops."""
    chunk = max(20, client.mtu_size - 3)
    loop = asyncio.get_event_loop()
    last_report = time.monotonic()
    def _safe_read():
        # The boot-image import closes its end of the com0com pair for ~1 s
        # mid-session; reading our end can raise instead of just returning empty.
        # Treat that as "no data" so we keep the BLE link up across the gap
        # rather than tearing it down (which would orphan the radio in its
        # bootloader/programming mode).
        try:
            return ser.read(256)
        except serial.SerialException:
            return b""

    while not disconnected.is_set():
        # PC -> radio: drain whatever the CPS wrote to the serial port
        data = await loop.run_in_executor(None, _safe_read)
        if data:
            stats["tx"] += len(data)
            # Spotlight the APRS write: the CPS sends the block as header
            # <cmd> 00 00 80 + 128 bytes. This is the flash-commit frame the
            # radio is most likely to drop the BLE link on, so mark the moment
            # it goes on the air (logging only — every frame is paced the same
            # now) to time the radio's reply or its silence + drop against it.
            is_aprs = APRS_MARKER in data
            response = WRITE_WITH_RESPONSE
            if data[0] == BOOT_FRAME_HEADER or BOOT_HANDSHAKE_MARKER in data:
                # Boot-image import frame. cmd byte (2=handshake, 3=set-addr,
                # 4=erase, 87=write-data, 6=over) is right after the 0xA5.
                cmd = data[1] if len(data) > 1 else 0
                print(f"\n[bridge] === {_ts()} BOOT-IMAGE frame -> device "
                      f"(cmd=0x{cmd:02X}, {len(data)} bytes, "
                      f"{(len(data)+chunk-1)//chunk} chunks) ===")
            if is_aprs:
                stats["aprs_t0"] = time.monotonic()
                print(f"\n[bridge] === {_ts()} APRS block (0x{APRS_WRITE_CMD:02X}) -> device, "
                      f"{len(data)} bytes in {(len(data)+chunk-1)//chunk} chunks "
                      f"(write {'WITH' if response else 'WITHOUT'} response) ===")
            if VERBOSE and not is_aprs:
                print(f"  PC->device [{len(data):3}] {bytes(data).hex().upper()}")
            try:
                for i in range(0, len(data), chunk):
                    await client.write_gatt_char(
                        DATA_UUID, data[i:i + chunk], response=response
                    )
                    if is_aprs:
                        print(f"  [APRS] {_ts()} chunk {i//chunk} "
                              f"({len(data[i:i+chunk])} B) write returned OK")
            except Exception as exc:
                t0 = stats.get("aprs_t0")
                where = f" (APRS chunk, +{time.monotonic()-t0:.3f}s)" if t0 else ""
                print(
                    f"[bridge] !!! {_ts()} GATT write failed mid-frame{where} "
                    f"({exc}); {len(data)} byte(s) from the CPS lost"
                )
                disconnected.set()
                break
        else:
            await asyncio.sleep(0.005)
        # Live throughput line once a second while traffic is flowing.
        now = time.monotonic()
        if now - last_report >= 1.0 and (stats["rx"] or stats["tx"]):
            print(
                f"[bridge] live: device->PC {stats['rx']} B in {stats['notifies']} pkts, "
                f"PC->device {stats['tx']} B"
            )
            last_report = now


async def main():
    ser = serial.Serial(PORT, baudrate=115200, timeout=0, write_timeout=2.0)
    print(f"[bridge] opened {PORT}")
    forced = "rsp" if FORCE_RSP else ("norsp" if FORCE_NORSP else "auto")
    print(f"[bridge] unlock={'on' if DO_UNLOCK else 'off'}, "
          f"write-mode={forced}, interval={'fast' if FAST else 'slow'}")

    while True:
        # Fresh per BLE session: unlock_swallowed gates the one-shot drop of the
        # ff31 unlock reply, and is reset here so a re-unlock after a reconnect
        # is swallowed again before the CPS's next handshake.
        stats = {"rx": 0, "tx": 0, "notifies": 0, "unlock_swallowed": False}
        disconnected = asyncio.Event()
        client = None
        try:
            client, notify_ch = await connect_and_unlock(ser, stats, disconnected)
            await pipe(client, ser, stats, disconnected)
        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        finally:
            if client is not None:
                try:
                    await client.stop_notify(notify_ch)
                except Exception:
                    pass
                try:
                    await client.disconnect()
                except Exception:
                    pass
        # Radio-side drop: flush whatever half-frame is stuck in the serial
        # buffers (the in-flight operation is dead anyway) and bring the link
        # back so the user's next CPS Read/Write just works.
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        hint = "" if IS_GUI else " (Ctrl+C to stop)"
        print(f"[bridge] {_ts()} link lost; reconnecting in 2 s{hint} ...")
        await asyncio.sleep(2.0)

    ser.close()
    print("\n[bridge] closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
