# Parani-configurator (ParaniWin-py)

Cross-platform replacement for **Sena ParaniWin 1.0.7** (Windows only) for the **Parani-SD1000**
(and compatible Parani-SD/ESD) Bluetooth serial adapters. ParaniWin is just a GUI around the AT commands
from the user manual (Appendix B/C). This tool does the same in Python + Tkinter + pyserial and runs on
macOS, Ubuntu/Linux and Windows.

| ParaniWin                              | ParaniWin-py tab                          |
|----------------------------------------|-------------------------------------------|
| Serial Port Setting                    | Serial port (+ baud rate auto-detect)     |
| Information                            | Information                               |
| Device Setting (mode, security, RS232) | Device                                    |
| Connection (out) + Signal Strength     | Connect (out)                             |
| Connection (in)                        | Wait (in)                                 |
| —                                      | S-registers (AT&V, ATSnn)                 |
| —                                      | Terminal (free-form AT commands / data)   |

Not included: the *Connection Wizard*. It is easy to do by hand (see below).

## Hardware

The SD1000 is an RS-232 (DB9, DCE) adapter. On a laptop you need a **USB-to-RS232 cable**
(preferably with an FTDI chip). Use a regular (straight-through) cable, not a null-modem cable. The port is named:

- macOS: `/dev/cu.usbserial-XXXX`. FTDI and CH34x drivers are built into macOS; for Prolific PL2303 you need the manufacturer's driver.
- Ubuntu: `/dev/ttyUSB0`
- Windows: `COMx`

## Installation

Requirements: Python 3.10+ with Tkinter, and [pyserial](https://pypi.org/project/pyserial/).

### As the `parani` command (all platforms)
```sh
pipx install git+https://github.com/pboertje/Parani-configurator   # or: uv tool install git+…
parani                  # GUI
parani --list           # CLI, see below
```

### From a clone of the repository
```sh
git clone https://github.com/pboertje/Parani-configurator && cd Parani-configurator
uv run paraniwin.py     # with uv; creates a .venv with pyserial automatically
```
Without uv, use `python3 -m venv .venv`, `.venv/bin/pip install pyserial` and `.venv/bin/python paraniwin.py`
(on Windows: `.venv\Scripts\...`).

### macOS
Homebrew Python ships without Tkinter. Install it with `brew install python-tk` if
`python3 -c "import tkinter"` fails. Install pyserial in a venv, with pipx or with uv as shown above;
`pip install --user` does not work with Homebrew Python.

### Ubuntu / Debian
```sh
sudo apt install python3-tk python3-serial
sudo usermod -aG dialout $USER   # then log out and back in
python3 paraniwin.py
```

### Windows
Install Python from [python.org](https://www.python.org/downloads/) (includes Tkinter), then:
```bat
py -m pip install pyserial
py paraniwin.py
```

### VM (VirtualBox/VMware/UTM/Parallels)
Pass the **USB serial adapter** through to the VM (USB passthrough / "Connect to VM"). It then
shows up as `/dev/ttyUSB0` in the guest. From there, follow the Ubuntu steps.

## Command-line use (headless, SSH, scripts)
```sh
python3 paraniwin.py --list                              # list ports
python3 paraniwin.py -p /dev/ttyUSB0                     # BTINFO + version
python3 paraniwin.py -p /dev/ttyUSB0 -b 0 'AT+BTINFO?'   # -b 0 = detect baud rate
python3 paraniwin.py -p /dev/ttyUSB0 'AT+BTNAME="Workbench"' 'AT+BTMODE,3' ATZ
```
If you installed the `parani` command, use `parani` instead of `python3 paraniwin.py`. Put commands
containing `?` or `"` in single quotes in the shell. For an interactive terminal:
`python3 -m serial.tools.miniterm /dev/ttyUSB0 9600` (Ctrl-] to quit).

## Testing without hardware (macOS/Linux)
```sh
python3 sim_sd1000.py            # prints e.g. /dev/ttys003
python3 paraniwin.py             # select that port
```

The test suite uses the same simulator (simulator tests are skipped on Windows):
```sh
uv run pytest
```

## Pairing two SD1000s (replacement for the Connection Wizard)
1. SD2 (slave): `AT+BTMODE,3`, `ATZ`, and note its BD address (`AT+BTINFO?`).
2. SD1 (master): `ATD<address-of-SD2>` and wait for `CONNECT`. Then `+++`, `ATH`, `AT+BTMODE,1`, `ATZ`.
3. SD2: `AT+BTCANCEL`, `AT+BTMODE,2`, `ATZ`.

You can also do this without a PC using the pairing button (manual §3.8).

## Caveats
- **S10=0** disables responses (OK/ERROR). After that, no tool can configure the device any more.
  The only way to recover is the reset button (press for ≥1 s).
- Baud rate, parity and stop bits can only be set in software when DIP switches 1-3 are set to
  'S/W Config'. Flow control can only be set with DIP switch 4.
- Firmware updates (ParaniUpdater) are not supported.

## macOS: Bluetooth connection from the Mac
System Settings will not pair the SD1000 (it reports Class of Device 0x000000), and the macOS
serial port `/dev/cu.SD1000…` only connects the first time after pairing.
Use the Swift tools in `tools/` instead. They require the Xcode Command Line Tools
(`xcode-select --install`):
```sh
cd tools
swiftc -o parani-pair parani-pair.swift
swiftc -o parani-term parani-term.swift

./parani-pair <pin> <address>     # pair (once); --remove <address> to unpair
./parani-term <address>           # terminal over RFCOMM (channel 1), Ctrl-] to quit
```
`<address>` is the BD address of your SD1000 in the form `00-01-95-xx-xx-xx` (see `AT+BTINFO?`).

## License
[GNU AGPL v3.0](LICENSE). This project is not affiliated with Sena Technologies; *Parani* and *ParaniWin* are trademarks of their respective owners.
