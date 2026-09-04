"""Serial port discovery, on macOS and on Linux.

The two platforms name and duplicate ports very differently, so each gets its
own filter rather than one leaky heuristic:

* **macOS** lists every port twice. ``/dev/tty.*`` blocks on open waiting for
  carrier detect; ``/dev/cu.*`` does not, so only the latter is offered. Two
  built-in ports that are never a sensor are dropped by name.
* **Linux** exposes USB adapters as ``/dev/ttyUSB*`` (USB-serial bridges like
  the CH340 in this sensor) and ``/dev/ttyACM*`` (CDC-ACM devices). The
  Raspberry Pi's own UART shows up as ``/dev/ttyAMA*`` / ``/dev/ttyS*``, which
  is a legitimate way to wire a sensor, so those are listed but not flagged
  as likely.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

try:  # pragma: no cover - exercised only when pyserial is missing
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    list_ports = None

# --- platform rules ----------------------------------------------------------

_MACOS_PREFIX = "/dev/cu."
_MACOS_EXCLUDED = ("/dev/cu.Bluetooth-Incoming-Port", "/dev/cu.debug-console")
_LINUX_PREFIXES = ("/dev/ttyUSB", "/dev/ttyACM", "/dev/ttyAMA", "/dev/ttyS")

#: Substrings that mark a port as a USB-serial adapter, i.e. very likely the
#: sensor. The WTVB01-BT50 uses a CH340 (VID:PID 1A86:7523).
_LIKELY_HINTS = ("usbserial", "usbmodem", "wchusbserial", "ttyusb", "ttyacm",
                 "slab", "ch34", "cp210", "1a86")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _accepts(device: str) -> bool:
    """Whether a device node is worth offering on this platform."""
    if _is_macos():
        return device.startswith(_MACOS_PREFIX) and device not in _MACOS_EXCLUDED
    if sys.platform.startswith("linux"):
        return device.startswith(_LINUX_PREFIXES)
    return True  # Unknown platform: offer whatever pyserial found.


def _is_likely(device: str, description: str, hwid: str) -> bool:
    haystack = f"{device} {description} {hwid}".lower()
    return any(hint in haystack for hint in _LIKELY_HINTS)


# --- discovery ---------------------------------------------------------------


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    likely: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "device": self.device,
            "description": self.description,
            "hwid": self.hwid,
            "likely": self.likely,
        }


def available_ports() -> tuple[PortInfo, ...]:
    """Candidate serial ports, most likely sensor first."""
    if list_ports is None:
        raise RuntimeError("pyserial is not installed; run: pip install pyserial")
    found = []
    for port in list_ports.comports():
        if not _accepts(port.device):
            continue
        description = port.description or ""
        hwid = port.hwid or ""
        found.append(PortInfo(port.device, description, hwid, _is_likely(port.device, description, hwid)))
    return tuple(sorted(found, key=lambda p: (not p.likely, p.device)))


def default_port() -> str | None:
    """The port most likely to be the sensor, if there is one."""
    ports = available_ports()
    return ports[0].device if ports else None
