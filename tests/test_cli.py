"""End-to-end tests of the command-line interface."""
import subprocess
import sys

from conftest import ROOT


def run_cli(*args, timeout=30):
    return subprocess.run([sys.executable, str(ROOT / "paraniwin.py"), *args],
                          capture_output=True, text=True, timeout=timeout)


def test_help():
    r = run_cli("--help")
    assert r.returncode == 0
    assert "serial port" in r.stdout


def test_list_runs():
    assert run_cli("--list").returncode == 0


def test_default_commands(sim_port):
    r = run_cli("-p", sim_port)
    assert r.returncode == 0, r.stderr
    assert "0001950011FF,PSD1000v2.0.9-0011FF,MODE0" in r.stdout
    assert "SD1000v2.0.9" in r.stdout


def test_error_exit_code(sim_port):
    r = run_cli("-p", sim_port, "AT", "AT+BOGUS")
    assert r.returncode == 3
    assert r.stdout.split() == ["OK", "ERROR"]


def test_autobaud(sim_port):
    r = run_cli("-p", sim_port, "-b", "0", "AT")
    assert r.returncode == 0
    assert "found at 9600 baud" in r.stderr
