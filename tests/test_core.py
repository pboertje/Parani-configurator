"""Tests for paranicore: pure parsing (all platforms) and against the simulator (macOS/Linux)."""
import time

import pytest

from paranicore import BtInfo, ParaniError, quote


# ---------------------------------------------------------------- pure (no serial port)
def test_btinfo_parse():
    i = BtInfo.parse("0001950011FF,PSD1000v2.0.9-0011FF,MODE3,PENDING,1,0,HWFC")
    assert i.address == "0001950011FF"
    assert i.name == "PSD1000v2.0.9-0011FF"
    assert (i.mode, i.status, i.auth, i.encrypt, i.flow) == ("MODE3", "PENDING", True, False, "HWFC")


def test_btinfo_parse_name_with_comma():
    i = BtInfo.parse("0001950011FF,Bench, left,MODE0,STANDBY,0,0,NoFC")
    assert i.name == "Bench, left"
    assert i.mode == "MODE0"


def test_btinfo_parse_rejects_garbage():
    with pytest.raises(ParaniError):
        BtInfo.parse("OK")


def test_quote_strips_inner_quotes():
    assert quote('My "Parani"') == '"My Parani"'


def test_track_escape_ignores_invalid_values():
    from paranicore import Parani

    d = Parani()
    d._track_escape("42")
    assert d.escape_char == "*"
    for bad in ("", "abc", "0", "200"):
        d._track_escape(bad)
        assert d.escape_char == "*"


# ---------------------------------------------------------------- against the simulator
def test_ping_and_info(dev):
    assert dev.ping()
    info = dev.info()
    assert info.address == "0001950011FF"
    assert info.mode == "MODE0"
    assert dev.version() == "SD1000v2.0.9"


def test_set_name_mode_and_security(dev):
    dev.command('AT+BTNAME="Workbench"')
    dev.command("AT+BTMODE,3")
    dev.command("AT+BTSEC,1,1")
    info = dev.info()
    assert (info.name, info.mode, info.auth, info.encrypt) == ("Workbench", "MODE3", True, True)


def test_error_raises_and_can_be_suppressed(dev):
    with pytest.raises(ParaniError):
        dev.command("AT+BOGUS")
    assert dev.command("AT+BOGUS", raise_on_error=False) == ["ERROR"]


def test_sregs(dev):
    regs = dev.sregs()
    assert regs[10] == "1"
    assert regs[28] == "43"
    dev.set_sreg(24, 7)
    assert dev.sreg(24) == "7"


def test_inquiry(dev):
    found = dev.inquiry(timeout=10)
    assert [f.address for f in found] == ["000B530011AA", "000195000002"]
    assert found[0].name == "Laptop"
    assert found[0].cod == "3E0100"


def test_connect_escape_disconnect(dev):
    assert dev.connect("000195000002", timeout=10) == "000195000002"
    assert dev.escape()
    dev.command("ATH", timeout=5, raise_on_error=False)
    assert dev.info().status == "STANDBY"


def test_escape_follows_s28(dev):
    dev.set_sreg(28, 42)                 # '*'
    assert dev.escape_char == "*"
    assert dev.escape()                  # simulator only answers to '***' now
    dev.set_sreg(28, 43)
    assert dev.escape_char == "+"


class FakeSerial:
    """Minimal stand-in for serial.Serial: read() hands out queued chunks, else times out."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.is_open = True

    def read(self, _n):
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(0.05)
        return b""

    def close(self):
        self.is_open = False


def _run_reader(chunks, wait):
    import threading

    from paranicore import Parani

    got = []
    d = Parani(on_line=lambda direction, text: got.append(text))
    d.ser = FakeSerial(chunks)
    t = threading.Thread(target=d._read_loop, daemon=True)
    t.start()
    time.sleep(wait)
    d._stop.set()
    t.join(timeout=2)
    return got


def test_reader_splits_lines():
    assert _run_reader([b"\r\nO", b"K\r\nCONNECT 0001", b"95000002\r\n"], 0.3) == \
        ["OK", "CONNECT 000195000002"]


def test_reader_flushes_prompt_without_newline_after_idle():
    from paranicore import IDLE_FLUSH

    assert _run_reader([b"\r\nOK\r\nPassword: "], IDLE_FLUSH + 0.5) == ["OK", "Password:"]


def test_reader_does_not_flush_partial_line_too_early():
    assert _run_reader([b"\r\nOK\r\nPartial"], 0.2) == ["OK"]


def test_autobaud(sim_port):
    from paranicore import Parani

    d = Parani()
    try:
        assert d.autobaud(sim_port) == 9600
    finally:
        d.close()
