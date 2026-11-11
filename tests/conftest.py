import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def sim_port():
    """Start the SD1000 simulator on a pseudo-terminal and yield its device path."""
    if not hasattr(os, "openpty"):
        pytest.skip("simulator needs a pseudo-terminal (macOS/Linux)")
    proc = subprocess.Popen([sys.executable, str(ROOT / "sim_sd1000.py")],
                            stdout=subprocess.PIPE, text=True)
    try:
        port = proc.stdout.readline().strip()
        assert port.startswith("/dev/"), f"simulator did not start: {port!r}"
        yield port
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def dev(sim_port):
    from paranicore import Parani, SerialSettings

    d = Parani()
    d.open(SerialSettings(sim_port))
    yield d
    d.close()
