#!/usr/bin/env python3
"""Minimal Parani-SD1000 simulator on a pseudo-terminal (macOS/Linux) – for testing without hardware.

    python3 sim_sd1000.py        # prints the pty path, e.g. /dev/ttys012
    python3 paraniwin.py -p /dev/ttys012 AT+BTINFO?
"""
import os
import re
import select
import sys
import time
import tty

state = dict(name="PSD1000v2.0.9-0011FF", mode=0, status="STANDBY", auth=0, enc=0,
             key="1234", last="000000000000", connected=None)
S = {1: 1, 3: 0, 4: 1, 6: 0, 10: 1, 11: 1, 12: 1, 13: 1, 14: 1, 15: 0, 22: 0, 23: 0, 24: 15,
     26: 0, 28: 43, 31: 20, 33: 30, 37: 5, 43: "001F00", 44: 0, 45: "9E8B33", 46: "000000000000",
     48: 5000, 49: 4500, 52: 5, 54: "000000000000", 55: "000000000000", 56: "000000000000",
     57: 3, 58: 0, 59: 0, 60: 5}
ADDR = "0001950011FF"


def handle(cmd):
    c = cmd.upper()
    st = state
    if c == "AT":
        return ["OK"]
    if c == "ATZ":
        st["status"] = "PENDING" if st["mode"] == 3 else "STANDBY"
        return ["OK"]
    if c == "AT&F":
        st.update(name="PSD1000v2.0.9-0011FF", mode=0, auth=0, enc=0)
        return ["OK"]
    if c == "AT+BTINFO?":
        return [f"{ADDR},{st['name']},MODE{st['mode']},{st['status']},{st['auth']},{st['enc']},NoFC", "OK"]
    if c == "AT+BTVER?":
        return ["SD1000v2.0.9", "OK"]
    if c == "AT+BTLAST?":
        return [st["last"], "OK"]
    if c == "AT+USEDIP?":
        return ["0", "OK"]
    if c == "AT+MLIST?":
        return ["CURRENT MODE:SINGLE CONNECTION MODE", "OK"]
    if c == "AT+BTSD?":
        return ["OK"]
    if c == "AT+BTINQ?":
        time.sleep(2)
        return ["000B530011AA,Laptop,3E0100", "000195000002,PSD1000-Other,001F00", "OK"]
    if c == "AT+BTCANCEL":
        st["status"] = "STANDBY"
        return ["OK"]
    if m := re.fullmatch(r"AT\+BTMODE,([0-3])", c):
        st["mode"] = int(m.group(1))
        return ["OK"]
    if m := re.fullmatch(r'AT\+BTNAME="?([^"]*)"?', cmd, re.I):
        st["name"] = m.group(1)
        return ["OK"]
    if re.fullmatch(r'AT\+BTKEY=.*', c):
        return ["OK"]
    if m := re.fullmatch(r"AT\+BTSEC,([01]),([01])", c):
        st["auth"], st["enc"] = int(m.group(1)), int(m.group(2))
        return ["OK"]
    if re.fullmatch(r"AT\+BTLPM,[01]", c) or re.fullmatch(r"AT\+UARTCONFIG,\d+,[NEO],[12]", c):
        return ["OK"]
    if c.startswith("AT+BTSCAN"):
        st["status"] = "PENDING"
        return ["OK"]
    if m := re.fullmatch(r"ATD([0-9A-F]{12})?", c):
        time.sleep(1)
        a = m.group(1) or st["last"]
        st.update(last=a, status="CONNECT", connected=a)
        S[46] = a
        return ["OK", f"CONNECT {a}"]
    if c == "ATH":
        st.update(status="STANDBY", connected=None)
        return ["OK", "DISCONNECT"]
    if c == "AT&V":
        return [";".join(f"S{k}:{v}" for k, v in S.items()), "OK"]
    if m := re.fullmatch(r"ATS(\d+)\?", c):
        n = int(m.group(1))
        return [str(S[n]), "OK"] if n in S else ["ERROR"]
    if m := re.fullmatch(r"ATS(\d+)=(\w+)", c):
        S[int(m.group(1))] = m.group(2)
        return ["OK"]
    if re.fullmatch(r"AT\+BTRSSI,[01]", c):
        return ["OK"] + (["0,240,0,-12"] if c.endswith("1") else [])
    return ["ERROR"]


def main():
    master, slave = os.openpty()
    tty.setraw(slave)
    print(os.ttyname(slave), flush=True)
    buf = b""
    while True:
        r, _, _ = select.select([master], [], [], 1)
        if not r:
            continue
        data = os.read(master, 1024)
        buf += data
        esc = chr(int(S[28])).encode("latin-1") * 3      # escape sequence follows S28
        if esc in buf:
            buf = buf.replace(esc, b"")
            os.write(master, b"\r\nOK\r\n")
        while b"\r" in buf:
            line, buf = buf.split(b"\r", 1)
            cmd = line.decode("latin-1").strip()
            if not cmd:
                continue
            for out in handle(cmd):
                os.write(master, f"\r\n{out}\r\n".encode())
            sys.stdout.flush()


if __name__ == "__main__":
    main()
