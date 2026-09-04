"""Test helpers: captured frames and a stand-in for ``serial.Serial``.

Shipped with the library rather than with any one application's tests, since
the desktop monitor and the Pi logger both need to exercise acquisition
without hardware attached.
"""

from __future__ import annotations

import threading

# --- captured frames ---------------------------------------------------------

#: The example packet from the WTVB01-BT50 manual, section 6. Decodes to
#: velocity 17/22/2 mm/s, 27.90 degC, displacement 67/71/10 um, 37 Hz.
MANUAL_FRAME = bytes.fromhex("5561110016000200020000000100E60A430047000A00250025002500")

#: One 0x61 frame captured from a physical unit over its Type-C UART.
UART_FRAME = bytes.fromhex(
    "5561" "0f01" "0100" "0e23" "dc00" "d2ff" "0b00" "4f08" "fbff" "0100"
    "0800" "0000" "0000" "0000" "0000" "0000" "0100" "0000" "0000" "0000"
)

#: A 0x71 read-back of block 0x3A from the same unit: registers 0x3A-0x3F read
#: zero at rest, 0x40 reads 0x0D63 = 34.27 degC, 0x41 reads 1.
REGISTER_FRAME = bytes.fromhex(
    "55713a00" "0000" "0000" "0000" "0000" "0000" "0000" "630d" "0100"
)


# --- fake port ---------------------------------------------------------------


class FakeSerial:
    """Replays a scripted byte stream and records everything written to it."""

    #: Set before constructing: bytes the fake will hand out, in order.
    script: bytes = b""
    #: Every instance created, newest last, so a test can inspect writes.
    instances: list["FakeSerial"] = []
    #: When set, opening raises this instead of succeeding.
    open_error: Exception | None = None

    def __init__(self, port: str, baudrate: int, timeout: float | None = None) -> None:
        if type(self).open_error is not None:
            raise type(self).open_error
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.writes: list[bytes] = []
        self.closed = False
        self._data = bytearray(type(self).script)
        self._lock = threading.Lock()
        type(self).instances.append(self)

    @classmethod
    def reset(cls, script: bytes = b"", open_error: Exception | None = None) -> None:
        cls.script = script
        cls.open_error = open_error
        cls.instances = []

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._data)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            chunk = bytes(self._data[:size])
            del self._data[:len(chunk)]
        return chunk

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(bytes(data))
        return len(data)

    def feed(self, data: bytes) -> None:
        """Append more bytes for the reader to pick up."""
        with self._lock:
            self._data.extend(data)

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def install_fake_serial(source_module, script: bytes = b"") -> type[FakeSerial]:
    """Point a module's ``serial`` attribute at :class:`FakeSerial`.

    Returns the class so a test can inspect ``FakeSerial.instances``. The
    caller is responsible for restoring the real module afterwards.
    """
    FakeSerial.reset(script)
    source_module.serial = type("module", (), {"Serial": FakeSerial})
    return FakeSerial
