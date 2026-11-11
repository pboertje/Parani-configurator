#!/usr/bin/env python3
"""ParaniWin-py – cross-platform replacement for Sena ParaniWin (Parani-SD1000).

    python3 paraniwin.py                 # GUI
    python3 paraniwin.py --list          # list serial ports
    python3 paraniwin.py -p PORT [-b 9600] AT+BTINFO? ATS10?   # CLI, one or more commands
"""
from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
import time

from paranicore import (BAUDRATES, SREG_INFO, Cancelled, Parani, ParaniError,
                        SerialSettings, list_ports, quote)

APP = "ParaniWin-py"


# ============================================================================ CLI
def cli(args) -> int:
    if args.list:
        for dev, desc in list_ports():
            print(f"{dev:40} {desc}")
        return 0
    dev = Parani(on_line=(lambda d, t: print(f"{d} {t}")) if args.verbose else None)
    if args.baud == 0:
        b = dev.autobaud(args.port, args.parity, args.stop, args.rtscts)
        if not b:
            print("No response to AT at any baud rate.", file=sys.stderr)
            return 1
        print(f"# found at {b} baud", file=sys.stderr)
    else:
        dev.open(SerialSettings(args.port, args.baud, args.parity, args.stop, args.rtscts))
    rc = 0
    try:
        for cmd in args.commands:
            if cmd == "+++":
                print("OK" if dev.escape() else "(no OK on +++)")
                continue
            long = cmd.upper().startswith(("AT+BTINQ", "ATD"))
            try:
                lines = dev.command(cmd, timeout=330 if long else args.timeout,
                                    until=("CONNECT", "ERROR") if cmd.upper().startswith("ATD")
                                    else ("OK", "ERROR"), raise_on_error=False)
            except TimeoutError as e:
                print(e, file=sys.stderr)
                rc = 2
                continue
            if not args.verbose:
                print("\n".join(lines))
            if lines and lines[-1].startswith("ERROR"):
                rc = 3
    finally:
        dev.close()
    return rc


# ============================================================================ GUI
def gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    class App:
        def __init__(self, root: tk.Tk):
            self.root = root
            root.title(APP)
            root.geometry("900x680")
            root.minsize(760, 560)
            self.log_q: queue.Queue = queue.Queue()
            self.ui_q: queue.Queue = queue.Queue()   # callables from worker threads, run on the Tk thread
            self.dev = Parani(on_line=self._on_line)
            self.busy = False
            self.connected_to = tk.StringVar(value="")
            self.rssi_running = False

            style = ttk.Style()
            if "clam" in style.theme_names() and sys.platform.startswith("linux"):
                style.theme_use("clam")

            paned = ttk.PanedWindow(root, orient="vertical")
            paned.pack(fill="both", expand=True, padx=6, pady=6)
            self.nb = ttk.Notebook(paned)
            paned.add(self.nb, weight=3)

            logf = ttk.Frame(paned)
            paned.add(logf, weight=1)
            self.log = tk.Text(logf, height=8, wrap="none", font="TkFixedFont")
            sb = ttk.Scrollbar(logf, command=self.log.yview)
            self.log.configure(yscrollcommand=sb.set, state="disabled")
            self.log.tag_configure("tx", foreground="#1f6feb")
            self.log.tag_configure("rx", foreground="#2da44e")
            self.log.tag_configure("err", foreground="#cf222e")
            self.log.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

            self.status = tk.StringVar(value="Not connected to a serial port")
            ttk.Label(root, textvariable=self.status, anchor="w", relief="sunken").pack(
                fill="x", side="bottom")

            self._tab_port()
            self._tab_info()
            self._tab_device()
            self._tab_out()
            self._tab_in()
            self._tab_sreg()
            self._tab_term()
            self._set_device_tabs(False)

            root.after(100, self._pump_log)
            root.protocol("WM_DELETE_WINDOW", self._quit)

        # ---------------------------------------------------------------- helpers
        def _on_line(self, direction, text):  # called from reader thread
            self.log_q.put((direction, text))

        def _ui(self, fn):
            """Schedule `fn` on the Tk thread. Safe to call from any thread (Tk itself is not)."""
            self.ui_q.put(fn)

        def _pump_log(self):
            try:
                while True:
                    d, t = self.log_q.get_nowait()
                    self._append(d, t)
                    if d == "<":
                        self._event(t)
            except queue.Empty:
                pass
            try:
                while True:
                    self.ui_q.get_nowait()()
            except queue.Empty:
                pass
            self.root.after(50, self._pump_log)

        def _append(self, d, t):
            tag = {"<": "rx", ">": "tx", "!": "err"}.get(d, "")
            self.log.configure(state="normal")
            self.log.insert("end", f"{time.strftime('%H:%M:%S')} {d} {t}\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")

        def _event(self, line):
            if line.startswith("CONNECT"):
                addr = line.split(" ", 1)[1] if " " in line else "?"
                self.connected_to.set(addr)
                self.status.set(f"Bluetooth connected to {addr}")
            elif line.startswith("DISCONNECT"):
                self.connected_to.set("")
                self.status.set("Bluetooth connection closed")
            elif self.rssi_running and re.fullmatch(r"\d+,\d+,\d+,-?\d+", line):
                _, lq, _, rssi = line.split(",")
                self.rssi_lq.set(int(lq))
                self.rssi_txt.set(f"LinkQuality {lq}/255   RSSI {rssi}")

        def run(self, label, fn, done=None, quiet=False):
            if not self.dev.is_open:
                messagebox.showwarning(APP, "Open a serial port first.")
                return
            if self.busy:
                self.status.set("Please wait, the previous command is still running…")
                return
            self.busy = True
            self.status.set(label + "…")
            self.root.config(cursor="watch")

            def work():
                try:
                    res, err = fn(), None
                except Exception as e:  # noqa: BLE001
                    res, err = None, e
                self._ui(lambda: finish(res, err))

            def finish(res, err):
                self.busy = False
                self.root.config(cursor="")
                if err is None:
                    self.status.set(label + ": done")
                    if done:
                        done(res)
                elif isinstance(err, Cancelled):
                    self.status.set(label + ": cancelled")
                else:
                    self.status.set(f"{label}: {err}")
                    self.log_q.put(("!", str(err)))
                    if not quiet:
                        messagebox.showerror(APP, f"{label} failed:\n{err}")

            threading.Thread(target=work, daemon=True).start()

        def _frame(self, title):
            f = ttk.Frame(self.nb, padding=12)
            self.nb.add(f, text=title)
            return f

        def _set_device_tabs(self, enabled):
            for i in range(1, self.nb.index("end")):
                self.nb.tab(i, state="normal" if enabled else "disabled")

        def _quit(self):
            try:
                if self.rssi_running:
                    self.dev.write_raw("AT+BTRSSI,0\r")
            except Exception:
                pass
            self.dev.close()
            self.root.destroy()

        # ---------------------------------------------------------------- tab: port
        def _tab_port(self):
            f = self._frame("Serial port")
            self.port_var = tk.StringVar()
            self.baud_var = tk.StringVar(value="9600")
            self.par_var = tk.StringVar(value="N")
            self.stop_var = tk.StringVar(value="1")
            self.hw_var = tk.BooleanVar(value=False)

            ttk.Label(f, text="Port").grid(row=0, column=0, sticky="w", pady=3)
            self.port_cb = ttk.Combobox(f, textvariable=self.port_var, width=40)
            self.port_cb.grid(row=0, column=1, sticky="we")
            ttk.Button(f, text="Refresh", command=self._refresh_ports).grid(row=0, column=2, padx=6)

            ttk.Label(f, text="Baud rate").grid(row=1, column=0, sticky="w", pady=3)
            ttk.Combobox(f, textvariable=self.baud_var, values=[str(b) for b in BAUDRATES],
                         width=12, state="readonly").grid(row=1, column=1, sticky="w")
            ttk.Label(f, text="Parity").grid(row=2, column=0, sticky="w", pady=3)
            ttk.Combobox(f, textvariable=self.par_var, values=["N", "E", "O"], width=4,
                         state="readonly").grid(row=2, column=1, sticky="w")
            ttk.Label(f, text="Stopbits").grid(row=3, column=0, sticky="w", pady=3)
            ttk.Combobox(f, textvariable=self.stop_var, values=["1", "2"], width=4,
                         state="readonly").grid(row=3, column=1, sticky="w")
            ttk.Checkbutton(f, text="Hardware flow control (RTS/CTS) – must match DIP switch 4",
                            variable=self.hw_var).grid(row=4, column=1, columnspan=2, sticky="w", pady=3)

            bf = ttk.Frame(f)
            bf.grid(row=5, column=1, columnspan=2, sticky="w", pady=10)
            self.open_btn = ttk.Button(bf, text="Open", command=self._open)
            self.open_btn.pack(side="left")
            ttk.Button(bf, text="Detect baud rate", command=self._autobaud).pack(side="left", padx=6)
            ttk.Button(bf, text="Close", command=self._close).pack(side="left")

            ttk.Label(f, foreground="gray", justify="left", wraplength=640, text=(
                "Factory default: 9600 8N1, Mode0. The baud rate is set by "
                "DIP switches 1-3 (position ‘S/W Config’ = set in software, default 9600).\n"
                "macOS: choose /dev/cu.usbserial-…  •  Ubuntu: /dev/ttyUSB0 (user in group "
                "‘dialout’)  •  Windows: COMx")).grid(row=6, column=0, columnspan=3, sticky="w", pady=8)
            f.columnconfigure(1, weight=1)
            self._refresh_ports()

        def _refresh_ports(self):
            ports = list_ports()
            self.port_cb["values"] = [p for p, _ in ports]
            if not self.port_var.get() and ports:
                pref = [p for p, d in ports if re.search(r"usb|serial|ttyUSB|ttyACM|COM", p + d, re.I)
                        and "Bluetooth" not in p]
                self.port_var.set((pref or [ports[0][0]])[0])

        def _settings(self, baud=None):
            return SerialSettings(self.port_var.get(), int(baud or self.baud_var.get()),
                                  self.par_var.get(), int(self.stop_var.get()), self.hw_var.get())

        def _open(self):
            try:
                self.dev.open(self._settings())
            except Exception as e:  # noqa: BLE001
                messagebox.showerror(APP, f"Cannot open port:\n{e}")
                return
            self._after_open()

        def _after_open(self):
            s = self.dev.settings
            self.status.set(f"Port {s.port} open ({s.baud} {8}{s.parity}{s.stopbits})")
            self._set_device_tabs(True)

            def check():
                if not self.dev.ping(1.5):
                    raise TimeoutError("No response to AT. Check baud rate/DIP switches, "
                                       "or the device is in a Bluetooth connection (use +++).")
                return self.dev.info()
            self.run("Checking device", check, done=self._show_info_quick)

        def _autobaud(self):
            port = self.port_var.get()
            if not port:
                return
            if self.busy:
                self.status.set("Please wait, the previous command is still running…")
                return
            self.busy = True
            self.root.config(cursor="watch")
            parity, stop, hw = self.par_var.get(), int(self.stop_var.get()), self.hw_var.get()

            def work():
                try:
                    b = self.dev.autobaud(port, parity, stop, hw,
                                          progress=lambda x: self._ui(
                                              lambda: self.status.set(f"Trying: {x} baud…")))
                except Exception as e:  # noqa: BLE001
                    self.log_q.put(("!", str(e)))
                    b = None
                self._ui(lambda: done(b))

            def done(b):
                self.busy = False
                self.root.config(cursor="")
                if b:
                    self.baud_var.set(str(b))
                    self._after_open()
                else:
                    self.status.set("No response found")
                    messagebox.showwarning(APP, "No response to AT at any baud rate.\n"
                                                "Check the cable (straight-through, not null-modem), power, "
                                                "flow control, and that S10 (response messages) is not off.")
            threading.Thread(target=work, daemon=True).start()

        def _close(self):
            self.dev.close()
            self._set_device_tabs(False)
            self.status.set("Port closed")

        def _show_info_quick(self, info):
            self.status.set(f"{info.name} ({info.address}) – {info.mode}, {info.status}")
            self._refresh_info()

        # ---------------------------------------------------------------- tab: info
        def _tab_info(self):
            f = self._frame("Information")
            self.info_vars = {}
            rows = [("address", "BD address"), ("name", "Name"), ("version", "Firmware"),
                    ("mode", "Operation mode"), ("status", "Status"), ("auth", "Authentication"),
                    ("encrypt", "Encryption"), ("flow", "Flow control"), ("dip", "Baud rate via"),
                    ("last", "Last connected"), ("multi", "Multi-connection"), ("paired", "Paired devices")]
            for i, (k, lbl) in enumerate(rows):
                ttk.Label(f, text=lbl + ":").grid(row=i, column=0, sticky="nw", pady=2)
                v = tk.StringVar(value="–")
                ttk.Label(f, textvariable=v, font=("TkFixedFont")).grid(row=i, column=1, sticky="w", pady=2)
                self.info_vars[k] = v
            ttk.Button(f, text="Refresh", command=self._refresh_info).grid(
                row=len(rows), column=0, pady=10, sticky="w")

        def _refresh_info(self):
            d = self.dev

            def work():
                i = d.info()
                r = dict(address=i.address, name=i.name, mode=i.mode, status=i.status,
                         auth="on" if i.auth else "off", encrypt="on" if i.encrypt else "off",
                         flow=i.flow)
                for key, fn in [("version", d.version), ("last", d.last),
                                ("dip", lambda: "DIP switches" if d.usedip() else "software (S/W Config)"),
                                ("multi", lambda: " | ".join(d.mlist())),
                                ("paired", lambda: ", ".join(d.paired()) or "none")]:
                    try:
                        r[key] = fn()
                    except Exception as e:  # noqa: BLE001
                        r[key] = f"({e})"
                return r

            def done(r):
                for k, v in r.items():
                    self.info_vars[k].set(v)
                self._fill_device(r)
            self.run("Reading information", work, done)

        # ---------------------------------------------------------------- tab: device
        def _tab_device(self):
            f = self._frame("Device")
            self.mode_var = tk.IntVar(value=0)
            self.name_var = tk.StringVar()
            self.pin_var = tk.StringVar()
            self.auth_var = tk.BooleanVar()
            self.enc_var = tk.BooleanVar()
            self.resp_var = tk.BooleanVar(value=True)
            self.lpm_var = tk.BooleanVar()
            self.ubaud_var = tk.StringVar(value="9600")
            self.upar_var = tk.StringVar(value="N")
            self.ustop_var = tk.StringVar(value="1")
            self.uart_var = tk.BooleanVar(value=False)

            mf = ttk.LabelFrame(f, text="Operation mode", padding=8)
            mf.grid(row=0, column=0, sticky="nwe", padx=(0, 8))
            for n, txt in [(0, "Mode0 – waits for AT commands (configuration)"),
                           (1, "Mode1 – master, connects to the last connected device"),
                           (2, "Mode2 – slave, only accepts the last connected device"),
                           (3, "Mode3 – slave, discoverable and connectable for everyone")]:
                ttk.Radiobutton(mf, text=txt, value=n, variable=self.mode_var).pack(anchor="w")

            sf = ttk.LabelFrame(f, text="Bluetooth & security", padding=8)
            sf.grid(row=1, column=0, sticky="nwe", padx=(0, 8), pady=8)
            ttk.Label(sf, text="Device name (max 30)").grid(row=0, column=0, sticky="w")
            ttk.Entry(sf, textvariable=self.name_var, width=32).grid(row=0, column=1, sticky="w")
            ttk.Checkbutton(sf, text="Authentication", variable=self.auth_var).grid(row=1, column=0, sticky="w")
            ttk.Checkbutton(sf, text="Encryption", variable=self.enc_var).grid(row=1, column=1, sticky="w")
            ttk.Label(sf, text="PIN code (empty = unchanged)").grid(row=2, column=0, sticky="w")
            ttk.Entry(sf, textvariable=self.pin_var, width=18).grid(row=2, column=1, sticky="w")
            ttk.Checkbutton(sf, text="Response messages OK/CONNECT/… (S10)",
                            variable=self.resp_var).grid(row=3, column=0, columnspan=2, sticky="w")
            ttk.Checkbutton(sf, text="Low power mode", variable=self.lpm_var).grid(
                row=4, column=0, columnspan=2, sticky="w")

            uf = ttk.LabelFrame(f, text="RS232 (only with DIP switch ‘S/W Config’)", padding=8)
            uf.grid(row=0, column=1, rowspan=2, sticky="nwe")
            ttk.Checkbutton(uf, text="Change UART settings", variable=self.uart_var).grid(
                row=0, column=0, columnspan=2, sticky="w")
            ttk.Label(uf, text="Baud rate").grid(row=1, column=0, sticky="w")
            ttk.Combobox(uf, textvariable=self.ubaud_var, values=[str(b) for b in BAUDRATES if b != 4800],
                         width=10, state="readonly").grid(row=1, column=1, sticky="w")
            ttk.Label(uf, text="Parity").grid(row=2, column=0, sticky="w")
            ttk.Combobox(uf, textvariable=self.upar_var, values=["N", "E", "O"], width=4,
                         state="readonly").grid(row=2, column=1, sticky="w")
            ttk.Label(uf, text="Stopbits").grid(row=3, column=0, sticky="w")
            ttk.Combobox(uf, textvariable=self.ustop_var, values=["1", "2"], width=4,
                         state="readonly").grid(row=3, column=1, sticky="w")
            ttk.Label(uf, foreground="gray", wraplength=260, justify="left",
                      text="After apply + reset, this app automatically reopens the port with the new "
                           "settings. Flow control can only be set with DIP switch 4.").grid(
                row=4, column=0, columnspan=2, sticky="w", pady=6)

            bf = ttk.Frame(f)
            bf.grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
            ttk.Button(bf, text="Load from device", command=self._refresh_info).pack(side="left")
            ttk.Button(bf, text="Apply", command=self._apply).pack(side="left", padx=6)
            ttk.Button(bf, text="Software reset (ATZ)", command=self._reset).pack(side="left")
            ttk.Button(bf, text="Factory defaults (AT&F)", command=self._factory).pack(side="left", padx=6)
            f.columnconfigure(0, weight=1)
            f.columnconfigure(1, weight=1)

        def _fill_device(self, r):
            self.mode_var.set(int(r.get("mode", "MODE0")[-1]) if r.get("mode", "").startswith("MODE") else 0)
            self.name_var.set(r.get("name", ""))
            self.auth_var.set(r.get("auth") == "on")
            self.enc_var.set(r.get("encrypt") == "on")

            def more():
                return self.dev.sreg(10), self.dev.sreg(6)

            def done(v):
                self.resp_var.set(v[0].strip() != "0")
                self.lpm_var.set(v[1].strip() == "1")
            # S10/S6 best effort – run after current job finishes
            self.root.after(300, lambda: self.run("Reading S-registers", more, done, quiet=True))

        def _apply(self):
            name = self.name_var.get().strip()
            pin = self.pin_var.get().strip()
            if len(name) > 30:
                messagebox.showerror(APP, "Name can be at most 30 characters.")
                return
            if pin and (len(pin) > 16 or not pin.isalnum()):
                messagebox.showerror(APP, "PIN: max 16 letters/digits.")
                return
            if self.auth_var.get() is False and self.enc_var.get():
                messagebox.showwarning(APP, "Encryption only works with authentication enabled.")
            mode = self.mode_var.get()
            uart = self.uart_var.get()
            new = self._settings(self.ubaud_var.get())
            new.parity, new.stopbits = self.upar_var.get(), int(self.ustop_var.get())
            d = self.dev

            def work():
                d.command("AT+BTCANCEL", raise_on_error=False)
                if name:
                    d.command(f"AT+BTNAME={quote(name)}")
                if pin:
                    d.command(f"AT+BTKEY={quote(pin)}")
                d.command(f"AT+BTSEC,{int(self.auth_var.get())},{int(self.enc_var.get())}")
                d.command(f"AT+BTLPM,{int(self.lpm_var.get())}")
                if uart:
                    d.command(f"AT+UARTCONFIG,{new.baud},{new.parity},{new.stopbits}")
                d.command(f"AT+BTMODE,{mode}")
                # S10 last: after S10=0 the device no longer answers OK
                if not self.resp_var.get():
                    d.write_raw("ATS10=0\r")
                    time.sleep(0.5)
                else:
                    d.command("ATS10=1")
                return True

            def done(_):
                if messagebox.askyesno(APP, "Settings saved.\nMode and UART changes only take effect "
                                            "after a reset. Reset now (ATZ)?"):
                    self._reset(reopen=new if uart else None)
            self.run("Applying settings", work, done)

        def _reset(self, reopen=None):
            def work():
                self.dev.write_raw("ATZ\r")
                time.sleep(1.5)
                if reopen:
                    self.dev.open(reopen)
                    time.sleep(0.3)
                return True

            def done(_):
                if reopen:
                    self.baud_var.set(str(reopen.baud))
                    self.par_var.set(reopen.parity)
                    self.stop_var.set(str(reopen.stopbits))
                if self.mode_var.get() == 0:
                    self.root.after(300, self._refresh_info)
                else:
                    self.status.set(f"Reset – device now starts in Mode{self.mode_var.get()}")
            self.run("Software reset", work, done)

        def _factory(self):
            if not messagebox.askyesno(APP, "Restore all settings to factory defaults?\n"
                                            "(Serial returns to 9600 8N1 if the DIP switches are set to S/W Config.)"):
                return
            self.run("Factory defaults", lambda: self.dev.command("AT&F", timeout=5),
                     lambda _: self.root.after(1500, self._refresh_info))

        # ---------------------------------------------------------------- tab: connection out
        def _tab_out(self):
            f = self._frame("Connect (out)")
            cols = ("addr", "name", "cod")
            self.tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
            for c, t, w in [("addr", "BD address", 140), ("name", "Name", 300), ("cod", "CoD", 90)]:
                self.tree.heading(c, text=t)
                self.tree.column(c, width=w, anchor="w")
            self.tree.grid(row=0, column=0, columnspan=6, sticky="nsew")
            ttk.Label(f, text="Address:").grid(row=1, column=0, sticky="w", pady=6)
            self.addr_var = tk.StringVar()
            ttk.Entry(f, textvariable=self.addr_var, width=16).grid(row=1, column=1, sticky="w")
            self.tree.bind("<<TreeviewSelect>>", lambda e: self.addr_var.set(
                self.tree.set(self.tree.selection()[0], "addr") if self.tree.selection() else ""))
            bf = ttk.Frame(f)
            bf.grid(row=2, column=0, columnspan=6, sticky="w")
            ttk.Button(bf, text="Search (±30 s)", command=self._inquiry).pack(side="left")
            ttk.Button(bf, text="Connect", command=self._connect).pack(side="left", padx=6)
            ttk.Button(bf, text="Connect to last (ATD)", command=lambda: self._connect(last=True)).pack(side="left")
            ttk.Button(bf, text="Cancel", command=self.dev.cancel).pack(side="left", padx=6)
            ttk.Button(bf, text="Disconnect", command=self._disconnect).pack(side="left")

            rf = ttk.LabelFrame(f, text="Signal strength", padding=8)
            rf.grid(row=3, column=0, columnspan=6, sticky="we", pady=10)
            self.rssi_lq = tk.IntVar(value=0)
            self.rssi_txt = tk.StringVar(value="–")
            ttk.Progressbar(rf, maximum=255, variable=self.rssi_lq, length=300).pack(side="left")
            ttk.Label(rf, textvariable=self.rssi_txt).pack(side="left", padx=10)
            ttk.Button(rf, text="Start", command=self._rssi_start).pack(side="left")
            ttk.Button(rf, text="Stop", command=self._rssi_stop).pack(side="left", padx=4)
            f.columnconfigure(5, weight=1)
            f.rowconfigure(0, weight=1)

        def _inquiry(self):
            def done(found):
                self.tree.delete(*self.tree.get_children())
                for x in found:
                    self.tree.insert("", "end", values=(x.address, x.name, x.cod))
                self.status.set(f"{len(found)} device(s) found")
            self.run("Searching for Bluetooth devices", lambda: (
                self.dev.command("AT+BTCANCEL", raise_on_error=False), self.dev.inquiry())[1], done)

        def _connect(self, last=False):
            addr = None if last else re.sub(r"[^0-9A-Fa-f]", "", self.addr_var.get()).upper()
            if not last and len(addr) != 12:
                messagebox.showwarning(APP, "Select a device or enter a 12-hex-digit BD address.")
                return
            self.run(f"Connecting to {addr or 'last device'}", lambda: self.dev.connect(addr),
                     lambda a: self.status.set(f"Connected to {a} – data now passes through transparently "
                                               "(Terminal tab), +++ for command mode"))

        def _disconnect(self):
            self.rssi_running = False
            self.run("Disconnecting", self.dev.disconnect)

        def _rssi_start(self):
            if not self.connected_to.get():
                messagebox.showinfo(APP, "The signal strength test only works during a Bluetooth connection.")
                return

            def work():
                self.dev.escape()
                self.dev.command("AT+BTRSSI,1")
            self.rssi_running = True
            self.run("Signal strength test", work)

        def _rssi_stop(self):
            self.rssi_running = False
            self.run("Stopping signal strength", lambda: self.dev.command("AT+BTRSSI,0", timeout=5,
                                                                        raise_on_error=False), quiet=True)

        # ---------------------------------------------------------------- tab: connection in
        def _tab_in(self):
            f = self._frame("Wait (in)")
            self.scan_n = tk.IntVar(value=3)
            self.scan_to = tk.StringVar(value="0")
            self.scan_addr = tk.StringVar()
            ttk.Radiobutton(f, text="Discoverable and connectable (inquiry + page scan)", value=3,
                            variable=self.scan_n).grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Radiobutton(f, text="Connectable only (page scan)", value=2,
                            variable=self.scan_n).grid(row=1, column=0, columnspan=2, sticky="w")
            ttk.Radiobutton(f, text="Discoverable only (inquiry scan)", value=1,
                            variable=self.scan_n).grid(row=2, column=0, columnspan=2, sticky="w")
            ttk.Label(f, text="Wait time (s, 0 = forever)").grid(row=3, column=0, sticky="w", pady=6)
            ttk.Entry(f, textvariable=self.scan_to, width=8).grid(row=3, column=1, sticky="w")
            ttk.Label(f, text="Only from BD address (optional)").grid(row=4, column=0, sticky="w")
            ttk.Entry(f, textvariable=self.scan_addr, width=16).grid(row=4, column=1, sticky="w")
            bf = ttk.Frame(f)
            bf.grid(row=5, column=0, columnspan=2, sticky="w", pady=10)
            ttk.Button(bf, text="Start waiting", command=self._scan).pack(side="left")
            ttk.Button(bf, text="Cancel", command=lambda: self.run(
                "Cancelling", lambda: self.dev.command("AT+BTCANCEL", raise_on_error=False))).pack(side="left", padx=6)
            ttk.Label(f, text="Connected to:").grid(row=6, column=0, sticky="w")
            ttk.Label(f, textvariable=self.connected_to, font=("TkFixedFont")).grid(row=6, column=1, sticky="w")

        def _scan(self):
            try:
                to = int(self.scan_to.get() or 0)
            except ValueError:
                messagebox.showerror(APP, "Wait time must be a number.")
                return
            addr = re.sub(r"[^0-9A-Fa-f]", "", self.scan_addr.get()).upper() or None
            if addr and len(addr) != 12:
                messagebox.showerror(APP, "BD address must be 12 hex digits.")
                return
            self.run("Waiting for connection", lambda: (
                self.dev.command("AT+BTCANCEL", raise_on_error=False),
                self.dev.scan(self.scan_n.get(), to, addr)))

        # ---------------------------------------------------------------- tab: S-registers
        def _tab_sreg(self):
            f = self._frame("S-registers")
            self.stree = ttk.Treeview(f, columns=("reg", "val", "desc"), show="headings", height=12)
            for c, t, w in [("reg", "Register", 70), ("val", "Value", 120), ("desc", "Description", 420)]:
                self.stree.heading(c, text=t)
                self.stree.column(c, width=w, anchor="w")
            sb = ttk.Scrollbar(f, command=self.stree.yview)
            self.stree.configure(yscrollcommand=sb.set)
            self.stree.grid(row=0, column=0, columnspan=6, sticky="nsew")
            sb.grid(row=0, column=6, sticky="ns")
            self.sreg_n = tk.StringVar()
            self.sreg_v = tk.StringVar()
            self.stree.bind("<<TreeviewSelect>>", self._sreg_select)
            ttk.Label(f, text="S").grid(row=1, column=0, sticky="e", pady=6)
            ttk.Entry(f, textvariable=self.sreg_n, width=4).grid(row=1, column=1, sticky="w")
            ttk.Label(f, text="=").grid(row=1, column=2)
            ttk.Entry(f, textvariable=self.sreg_v, width=14).grid(row=1, column=3, sticky="w")
            bf = ttk.Frame(f)
            bf.grid(row=1, column=4, sticky="w")
            ttk.Button(bf, text="Read", command=self._sreg_read).pack(side="left", padx=4)
            ttk.Button(bf, text="Write", command=self._sreg_write).pack(side="left")
            ttk.Button(bf, text="Load all (AT&V)", command=self._sreg_load).pack(side="left", padx=4)
            f.columnconfigure(5, weight=1)
            f.rowconfigure(0, weight=1)

        def _sreg_select(self, _e):
            sel = self.stree.selection()
            if sel:
                self.sreg_n.set(sel[0])
                self.sreg_v.set(self.stree.set(sel[0], "val"))

        def _sreg_load(self):
            def done(regs):
                self.stree.delete(*self.stree.get_children())
                for n in sorted(regs):
                    self.stree.insert("", "end", iid=str(n),
                                      values=(f"S{n}", regs[n], SREG_INFO.get(n, "")))
            self.run("Loading S-registers", self.dev.sregs, done)

        def _sreg_read(self):
            n = self.sreg_n.get().strip()
            if not n.isdigit():
                return
            self.run(f"Reading S{n}", lambda: self.dev.sreg(int(n)), lambda v: self.sreg_v.set(v))

        def _sreg_write(self):
            n, v = self.sreg_n.get().strip(), self.sreg_v.get().strip()
            if not n.isdigit() or not v:
                return
            if n == "10" and v == "0" and not messagebox.askyesno(
                    APP, "With S10=0 the device no longer sends OK/ERROR and this app "
                         "can no longer configure it until a factory reset. Continue?"):
                return

            def work():
                if n == "10" and v == "0":
                    self.dev.write_raw("ATS10=0\r")
                    return
                self.dev.set_sreg(int(n), v)
            self.run(f"Writing S{n}={v}", work, lambda _: self._sreg_load())

        # ---------------------------------------------------------------- tab: terminal
        def _tab_term(self):
            f = self._frame("Terminal")
            ttk.Label(f, wraplength=700, justify="left", text=(
                "Send arbitrary AT commands (in Standby) or data (when a Bluetooth connection is active). "
                "Responses appear in the log pane at the bottom.")).pack(anchor="w")
            row = ttk.Frame(f)
            row.pack(fill="x", pady=8)
            self.term_var = tk.StringVar()
            e = ttk.Entry(row, textvariable=self.term_var)
            e.pack(side="left", fill="x", expand=True)
            e.bind("<Return>", lambda _e: self._term_send())
            self.cr_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(row, text="+CR", variable=self.cr_var).pack(side="left", padx=4)
            ttk.Button(row, text="Send", command=self._term_send).pack(side="left")
            row2 = ttk.Frame(f)
            row2.pack(anchor="w")
            for c in ["AT", "AT+BTINFO?", "AT&V", "AT+BTLAST?", "ATH"]:
                ttk.Button(row2, text=c, command=lambda c=c: self._term_send(c)).pack(side="left", padx=2)
            ttk.Button(row2, text="+++ (escape)", command=lambda: self.run("Escape", self.dev.escape,
                                                                         quiet=True)).pack(side="left", padx=2)
            ttk.Button(row2, text="ATO (back to data)", command=lambda: self._term_send("ATO")).pack(side="left", padx=2)
            ttk.Button(row2, text="Clear log", command=self._clear_log).pack(side="left", padx=12)

        def _term_send(self, text=None):
            text = self.term_var.get() if text is None else text
            if not self.dev.is_open:
                messagebox.showwarning(APP, "Open a serial port first.")
                return
            try:
                self.dev.write_raw(text + ("\r" if self.cr_var.get() else ""))
                self._append(">", text)
            except Exception as e:  # noqa: BLE001
                self._append("!", str(e))
            if text == self.term_var.get():
                self.term_var.set("")

        def _clear_log(self):
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")

    root = tk.Tk()
    root.app = App(root)
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list serial ports")
    ap.add_argument("-p", "--port", help="serial port (CLI mode)")
    ap.add_argument("-b", "--baud", type=int, default=9600, help="baud rate, 0 = auto-detect")
    ap.add_argument("--parity", default="N", choices="NEO")
    ap.add_argument("--stop", type=int, default=1, choices=[1, 2])
    ap.add_argument("--rtscts", action="store_true", help="hardware flow control")
    ap.add_argument("-t", "--timeout", type=float, default=3)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("commands", nargs="*", help="AT commands (CLI mode)")
    args = ap.parse_args()
    if args.list or args.port:
        if args.port and not args.commands:
            args.commands = ["AT+BTINFO?", "AT+BTVER?"]
        try:
            sys.exit(cli(args))
        except ParaniError as e:
            sys.exit(f"Error: {e}")
    gui()


if __name__ == "__main__":
    main()
