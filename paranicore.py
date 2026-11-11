"""Core serial/AT-command layer for Sena Parani-SD1000 (and compatible SD/ESD) adapters.

Pure Python + pyserial, no GUI dependencies. Works on macOS, Linux and Windows.
Command set follows the Parani-SD1000 User Manual v2.0.9.2, Appendix B/C.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field

import serial
import serial.tools.list_ports

BAUDRATES = [1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
             115200, 230400, 460800, 921600]
PARITIES = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
STOPBITS = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}

FINAL = ("OK", "ERROR")
IDLE_FLUSH = 0.5     # s without new data before a line without CR/LF is passed on
MAX_LINE = 2048      # bytes; longer data without CR/LF is passed on immediately

# Appendix C – S-register descriptions (short)
SREG_INFO = {
    1: "Reconnect in Mode1 (0/1)",
    3: "UART policy: 0=throughput, 1=latency",
    4: "Request names during inquiry (0/1)",
    6: "Low power mode (0/1)",
    10: "Response messages OK/ERROR/CONNECT (0/1)",
    11: "Allow escape sequence (+++) (0/1)",
    12: "Clear buffer on disconnect (0/1)",
    13: "DCD signal (0/1)",
    14: "Pass DTR to remote DSR (0/1)",
    15: "Disconnect via DTR (0/1)",
    22: "Faster connection: 0/1=page/2=inq/3=both",
    23: "Intercharacter timeout factor (0-3)",
    24: "Max. number of inquiry results (0=unlimited)",
    26: "Intercharacter timeout",
    28: "Escape character (ASCII, 43='+')",
    31: "Page timeout ATD (s, steps of 5)",
    33: "Inquiry timeout (s)",
    37: "Supervision timeout (s)",
    43: "Class of Device",
    44: "CoD filter for inquiry (0=all)",
    45: "Inquiry Access Code",
    46: "BD address of last connection (task 1)",
    48: "Low power max interval (x625µs)",
    49: "Low power min interval (x625µs)",
    52: "Low power timeout (s)",
    54: "BD address of last connection (task 2)",
    55: "BD address of last connection (task 3)",
    56: "BD address of last connection (task 4)",
    57: "Slave disconnect timeout (s)",
    58: "Max TX power (0=chip, 1..8 = -12..16 dBm)",
    59: "Current slave (0-4)",
    60: "Reconnect interval (x2 s)",
}


class ParaniError(Exception):
    """Device answered ERROR or something unexpected."""


class Cancelled(Exception):
    pass


@dataclass
class BtInfo:
    address: str
    name: str
    mode: str
    status: str
    auth: bool
    encrypt: bool
    flow: str

    @classmethod
    def parse(cls, line: str) -> "BtInfo":
        parts = line.split(",")
        if len(parts) < 7:
            raise ParaniError(f"Unexpected BTINFO response: {line!r}")
        addr, *mid, mode, status, auth, enc, flow = parts
        return cls(addr, ",".join(mid), mode, status, auth == "1", enc == "1", flow)


@dataclass
class Found:
    address: str
    name: str = ""
    cod: str = ""


@dataclass
class SerialSettings:
    port: str
    baud: int = 9600
    parity: str = "N"
    stopbits: int = 1
    rtscts: bool = False


def list_ports() -> list[tuple[str, str]]:
    out = []
    for p in serial.tools.list_ports.comports():
        name = p.device
        # macOS: prefer cu.* (callout) over tty.* to avoid blocking on DCD
        if name.startswith("/dev/tty.") and name.replace("/dev/tty.", "/dev/cu.") != name:
            continue
        out.append((name, p.description or ""))
    return sorted(out)


class Parani:
    """Thread-safe AT-command client.

    A background reader splits incoming bytes into lines. Lines are both handed to
    `on_line` (for logging / unsolicited events such as CONNECT, DISCONNECT, RSSI)
    and queued for whatever command is currently waiting for a response.
    """

    def __init__(self, on_line=None, on_raw=None):
        self.ser: serial.Serial | None = None
        self.settings: SerialSettings | None = None
        self.on_line = on_line or (lambda direction, text: None)
        self.on_raw = on_raw
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self.escape_char = "+"   # follows S28; updated by sregs() / sreg(28) / set_sreg(28, …)

    # ---- port handling -------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self, s: SerialSettings):
        self.close()
        self.ser = serial.Serial(
            s.port, s.baud, bytesize=serial.EIGHTBITS,
            parity=PARITIES[s.parity], stopbits=STOPBITS[s.stopbits],
            rtscts=s.rtscts, timeout=0.05, write_timeout=2,
        )
        # Parani is DCE; assert DTR/RTS like a normal terminal would
        try:
            self.ser.dtr = True
            if not s.rtscts:
                self.ser.rts = True
        except (OSError, serial.SerialException):
            pass
        self.settings = s
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._stop.set()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=1)
        self._reader = None
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def _emit(self, raw: bytes):
        line = raw.decode("latin-1").strip()
        if line:
            self._q.put(line)
            self.on_line("<", line)

    def _read_loop(self):
        buf = b""
        last_rx = time.monotonic()
        while not self._stop.is_set():
            try:
                data = self.ser.read(512)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not self._stop.is_set():
                    self.on_line("!", f"Read error: {e}")
                break
            if not data:
                # a lone prompt without newline (e.g. remote-config prompt) – flush after idle
                if buf and time.monotonic() - last_rx >= IDLE_FLUSH:
                    self._emit(buf)
                    buf = b""
                continue
            last_rx = time.monotonic()
            if self.on_raw:
                self.on_raw(data)
            buf += data
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                raw, buf = buf[:m.start()], buf[m.end():]
                self._emit(raw)
            if len(buf) > MAX_LINE:
                self._emit(buf)
                buf = b""

    # ---- low level I/O --------------------------------------------------
    def write_raw(self, data: bytes | str):
        if not self.is_open:
            raise ParaniError("Serial port is not open")
        if isinstance(data, str):
            data = data.encode("latin-1")
        self.ser.write(data)
        self.ser.flush()

    def cancel(self):
        """Abort a long-running command (inquiry, connect, scan)."""
        self._cancel.set()
        try:
            self.write_raw("AT+BTCANCEL\r")
            self.on_line(">", "AT+BTCANCEL")
        except Exception:
            pass

    def _drain(self):
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def command(self, cmd: str, timeout: float = 3.0, until=FINAL,
                raise_on_error: bool = True) -> list[str]:
        """Send `cmd` + CR and collect lines until a line starting with one of `until`."""
        with self._lock:
            self._cancel.clear()
            self._drain()
            self.write_raw(cmd + "\r")
            self.on_line(">", cmd)
            lines: list[str] = []
            deadline = time.monotonic() + timeout
            while True:
                if self._cancel.is_set():
                    raise Cancelled(cmd)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"No response to {cmd!r} within {timeout:.0f}s "
                        "(wrong baud rate, S10=0, or device connected?)")
                try:
                    line = self._q.get(timeout=min(remaining, 0.2))
                except queue.Empty:
                    continue
                if line == cmd:          # local echo
                    continue
                lines.append(line)
                if any(line == u or line.startswith(u + " ") for u in until):
                    break
            if raise_on_error and lines and lines[-1].startswith("ERROR"):
                raise ParaniError(f"{cmd} → ERROR")
            return lines

    def body(self, cmd: str, **kw) -> list[str]:
        """Command lines without trailing OK."""
        return [l for l in self.command(cmd, **kw) if l != "OK"]

    def escape(self):
        """Go from Connect (data mode) to Standby with the escape sequence (3x S28)."""
        esc = self.escape_char
        with self._lock:
            self._drain()
            time.sleep(0.3)
            self.write_raw(esc * 3)
            self.on_line(">", esc * 3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    if self._q.get(timeout=0.2) == "OK":
                        return True
                except queue.Empty:
                    pass
            return False

    # ---- high level helpers ----------------------------------------------
    def ping(self, timeout=1.0) -> bool:
        try:
            return self.command("AT", timeout=timeout)[-1] == "OK"
        except (TimeoutError, ParaniError):
            return False

    def autobaud(self, port: str, parity="N", stopbits=1, rtscts=False,
                 progress=None) -> int | None:
        order = [9600, 115200, 57600, 38400, 19200, 4800, 2400, 1200,
                 14400, 230400, 460800, 921600]
        for b in order:
            if progress:
                progress(b)
            self.open(SerialSettings(port, b, parity, stopbits, rtscts))
            time.sleep(0.15)
            if self.ping(0.8) or self.ping(0.8):
                return b
        self.close()
        return None

    def info(self) -> BtInfo:
        lines = self.body("AT+BTINFO?")
        for l in lines:
            if "," in l:
                return BtInfo.parse(l)
        raise ParaniError(f"No BTINFO in response: {lines}")

    def version(self) -> str:
        return " ".join(self.body("AT+BTVER?"))

    def last(self) -> str:
        return " ".join(self.body("AT+BTLAST?"))

    def usedip(self) -> bool:
        """True when baud rate comes from dip switches (not S/W config)."""
        return " ".join(self.body("AT+USEDIP?")).strip() != "0"

    def mlist(self) -> list[str]:
        return self.body("AT+MLIST?")

    def paired(self) -> list[str]:
        return self.body("AT+BTSD?")

    def sregs(self) -> dict[int, str]:
        out = {}
        for l in self.body("AT&V", timeout=5):
            for m in re.finditer(r"S(\d+)\s*:\s*([^;]*)", l):
                out[int(m.group(1))] = m.group(2).strip()
        if 28 in out:
            self._track_escape(out[28])
        return out

    def sreg(self, n: int) -> str:
        v = " ".join(self.body(f"ATS{n}?"))
        if n == 28:
            self._track_escape(v)
        return v

    def set_sreg(self, n: int, value):
        self.command(f"ATS{n}={value}")
        if n == 28:
            self._track_escape(str(value))

    def _track_escape(self, value: str):
        """S28 holds the escape character as a decimal ASCII code (default 43 = '+')."""
        value = value.strip()
        if value.isdigit() and 32 < int(value) < 127:
            self.escape_char = chr(int(value))

    def inquiry(self, timeout=60) -> list[Found]:
        res = []
        for l in self.body("AT+BTINQ?", timeout=timeout):
            parts = l.split(",")
            if re.fullmatch(r"[0-9A-Fa-f]{12}", parts[0]):
                res.append(Found(parts[0], ",".join(parts[1:-1]) if len(parts) > 2 else
                                 (parts[1] if len(parts) > 1 else ""),
                                 parts[-1] if len(parts) > 2 else ""))
        return res

    def connect(self, address: str | None = None, timeout=330) -> str:
        cmd = f"ATD{address}" if address else "ATD"
        lines = self.command(cmd, timeout=timeout, until=("CONNECT", "ERROR"),
                             raise_on_error=False)
        last = lines[-1]
        if last.startswith("CONNECT"):
            return last.split(" ", 1)[1] if " " in last else ""
        raise ParaniError(f"Connection failed: {last}")

    def disconnect(self):
        self.escape()
        self.command("ATH", timeout=10, raise_on_error=False)

    def scan(self, n=3, timeout=0, address: str | None = None):
        if address:
            self.command(f"AT+BTSCAN{address},{timeout}")
        else:
            self.command(f"AT+BTSCAN,{n},{timeout}")


def quote(s: str) -> str:
    return '"' + s.replace('"', "") + '"'
