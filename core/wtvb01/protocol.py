"""Frame-level protocol for the WitMotion WTVB01-BT50.

Every frame starts with 0x55 followed by a type byte:

  0x61  free-running output frame; payload is a run of little-endian int16
        register values. Its length is *not* the same on every transport, so
        the layout is looked up by frame length (see ``OUTPUT_LAYOUTS``).
  0x71  register read-back: 2 header bytes + 2 address bytes + 8 registers.

There is no checksum on either frame, so the parser resynchronises on the
sync byte and validates by frame length alone.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Mapping

SYNC = 0x55
TYPE_OUTPUT = 0x61
TYPE_REGISTER = 0x71

REGISTER_FRAME_LEN = 20
REGISTERS_PER_BLOCK = 8
HEADER_LEN = 2

_NONE = None


@dataclass(frozen=True)
class OutputLayout:
    """Which register each int16 slot of a 0x61 frame carries."""

    length: int
    name: str
    slots: tuple[int | None, ...]
    note: str = ""

    @property
    def value_count(self) -> int:
        return len(self.slots)


def _span(first: int, count: int) -> tuple[int, ...]:
    return tuple(range(first, first + count))


# 28 bytes: the layout documented in the WTVB01-BT50 manual (velocity, angle,
# temperature, displacement, frequency -> registers 0x3A..0x46).
LAYOUT_MANUAL = OutputLayout(
    length=28,
    name="manual-28",
    slots=_span(0x3A, 13),
    note="Documented WTVB01-BT50 output frame (manual section 5.2).",
)

# 32 bytes: what the BLE transport emits. Same 13 registers, one unused slot,
# then the undocumented power counter also readable at register 0x64.
LAYOUT_BLE = OutputLayout(
    length=32,
    name="ble-32",
    slots=_span(0x3A, 13) + (_NONE, 0x64),
    note="BLE notification frame; slot 13 is always zero.",
)

# 40 bytes: what the Type-C UART emits on the unit tested here. Slots 0..15
# map to registers 0x30..0x3F - timestamp words, raw accel/gyro, then
# vibration velocity and angle. Verified by comparing live frames against
# 0x71 read-backs of blocks 0x2E and 0x3A.
#
# Slots 16..18 sit where registers 0x40..0x42 would fall but read a constant
# zero on this firmware, while read-backs of the same registers return live
# values (0x40 -> 3427 = 34.27 degC). They are left unmapped so a frame never
# overwrites a polled temperature or displacement with zero; poll the
# measurement blocks to get those channels.
LAYOUT_UART = OutputLayout(
    length=40,
    name="uart-40",
    slots=_span(0x30, 16) + (_NONE, _NONE, _NONE),
    note="Type-C UART frame: timestamp + accel/gyro + velocity/angle. "
         "Temperature, displacement and frequency need register polling.",
)

OUTPUT_LAYOUTS: Mapping[int, OutputLayout] = {
    layout.length: layout for layout in (LAYOUT_MANUAL, LAYOUT_BLE, LAYOUT_UART)
}

KNOWN_OUTPUT_LENGTHS: tuple[int, ...] = tuple(sorted(OUTPUT_LAYOUTS))


def layout_for(length: int) -> OutputLayout:
    """Return the layout for ``length``, synthesising one if it is unknown.

    An unknown length still decodes: slots are mapped from register 0x3A, the
    first vibration register, which is where every documented frame starts.
    """
    known = OUTPUT_LAYOUTS.get(length)
    if known is not None:
        return known
    count = max(0, (length - HEADER_LEN) // 2)
    return OutputLayout(
        length=length,
        name=f"unknown-{length}",
        slots=_span(0x3A, count),
        note="Unrecognised frame length; register mapping is a guess.",
    )


# --- decoded frames ----------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """A decoded frame: raw int16 words plus the registers they resolve to."""

    kind: str
    values: tuple[int, ...]
    registers: Mapping[int, int]
    layout: str = ""


def _words(payload: bytes) -> tuple[int, ...]:
    count = len(payload) // 2
    return struct.unpack_from(f"<{count}h", payload, 0)


def decode_output(frame: bytes, layout: OutputLayout | None = None) -> Frame:
    """Decode a 0x55 0x61 output frame."""
    layout = layout or layout_for(len(frame))
    values = _words(frame[HEADER_LEN:])
    registers = {
        address: values[index]
        for index, address in enumerate(layout.slots)
        if address is not None and index < len(values)
    }
    return Frame("output", values, registers, layout.name)


def decode_register_block(frame: bytes) -> Frame:
    """Decode a 0x55 0x71 register read-back frame."""
    start = frame[2] | (frame[3] << 8)
    values = _words(frame[4:4 + REGISTERS_PER_BLOCK * 2])
    registers = {start + index: value for index, value in enumerate(values)}
    return Frame("register", values, registers, f"block-0x{start:02X}")


# --- streaming parser --------------------------------------------------------


@dataclass
class FrameParser:
    """Resynchronising byte-stream parser.

    ``output_length`` fixes the expected 0x61 frame size. Leave it ``None`` to
    let :class:`~wtvb01.detect.OutputLengthDetector` decide first; the parser
    then drops 0x61 frames until a length is set with :meth:`set_output_length`.
    """

    output_length: int | None = None
    _buffer: bytearray = field(default_factory=bytearray, repr=False)
    dropped_bytes: int = 0

    def set_output_length(self, length: int) -> None:
        self.output_length = length
        self._buffer.clear()

    def _frame_length(self, type_byte: int) -> int | None:
        if type_byte == TYPE_REGISTER:
            return REGISTER_FRAME_LEN
        if type_byte == TYPE_OUTPUT:
            return self.output_length
        return None

    def feed(self, data: bytes) -> list[Frame]:
        """Consume bytes and return every complete frame found."""
        self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                return frames
            frames.append(frame)

    def _take_frame(self) -> Frame | None:
        buffer = self._buffer
        while True:
            start = buffer.find(SYNC)
            if start < 0:
                if self.output_length is not None:
                    self.dropped_bytes += len(buffer)
                buffer.clear()
                return None
            if start:
                if self.output_length is not None:
                    self.dropped_bytes += start
                del buffer[:start]
            if len(buffer) < HEADER_LEN:
                return None
            length = self._frame_length(buffer[1])
            if length is None:
                # Not a frame we can size: skip this sync byte and retry.
                # Bytes skipped before the output length is known are not
                # counted as drops - nothing is decodable until then.
                if self.output_length is not None:
                    self.dropped_bytes += 1
                del buffer[:1]
                continue
            if len(buffer) < length:
                return None
            frame = bytes(buffer[:length])
            del buffer[:length]
            if frame[1] == TYPE_REGISTER:
                return decode_register_block(frame)
            return decode_output(frame)


# --- command builders --------------------------------------------------------

_CMD_PREFIX = (0xFF, 0xAA)
_READ_REGISTER = 0x27
_UNLOCK_REGISTER = 0x69
_UNLOCK_VALUE = 0xB588
_SAVE_REGISTER = 0x00
_SAVE_VALUE = 0x0000

#: Read-back blocks that together cover every vibration register (0x3A-0x49).
MEASUREMENT_BLOCKS: tuple[int, ...] = (0x3A, 0x42)


def write_register(address: int, value: int) -> bytes:
    """FF AA <addr> <value low> <value high>."""
    return bytes((*_CMD_PREFIX, address & 0xFF, value & 0xFF, (value >> 8) & 0xFF))


def read_register(address: int) -> bytes:
    """FF AA 27 <addr> 00 - asks for 8 registers starting at ``address``."""
    return write_register(_READ_REGISTER, address & 0xFF)


def unlock() -> bytes:
    """Unlock the configuration registers for ~10 s."""
    return write_register(_UNLOCK_REGISTER, _UNLOCK_VALUE)


def save() -> bytes:
    """Persist written configuration."""
    return write_register(_SAVE_REGISTER, _SAVE_VALUE)


def measurement_poll() -> tuple[bytes, ...]:
    """Commands that read every vibration register."""
    return tuple(read_register(block) for block in MEASUREMENT_BLOCKS)


def merge_registers(*sources: Iterable[tuple[int, int]]) -> dict[int, int]:
    """Combine register mappings, later sources winning."""
    merged: dict[int, int] = {}
    for source in sources:
        merged.update(source)
    return merged
