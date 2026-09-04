"""WTVB01-BT50 register map.

Addresses and scale factors come from the WitMotion WTVB01-BT50 manual
(section 5, "Registers") and were verified against a physical unit over the
Type-C UART link: register 0x40 read 3414 -> 34.14 degC (ambient), 0x3A-0x3C
read ~0 mm/s at rest, 0x41-0x43 read 3-16 um, 0x44-0x46 read 6-8 Hz.

Registers 0x34-0x39 (raw accelerometer / gyroscope) are not in the vibration
manual but are present in the free-running frame this unit emits, and follow
the standard WitMotion scaling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

# --- scale helpers -----------------------------------------------------------

_ACCEL_RANGE_G = 16.0
_GYRO_RANGE_DPS = 2000.0
_ANGLE_RANGE_DEG = 180.0
_INT16_FULL_SCALE = 32768.0


def _identity(raw: int) -> float:
    return float(raw)


def _scaled(full_scale: float) -> Callable[[int], float]:
    def convert(raw: int) -> float:
        return raw / _INT16_FULL_SCALE * full_scale

    return convert


def _divided(divisor: float) -> Callable[[int], float]:
    def convert(raw: int) -> float:
        return raw / divisor

    return convert


# --- register table ----------------------------------------------------------


@dataclass(frozen=True)
class Register:
    """One 16-bit register: how to name, scale and label its raw value."""

    address: int
    key: str
    label: str
    unit: str
    convert: Callable[[int], float]
    group: str = ""

    def value(self, raw: int) -> float:
        return self.convert(raw)


def _r(address, key, label, unit, convert, group=""):
    return Register(address, key, label, unit, convert, group)


REGISTERS: tuple[Register, ...] = (
    _r(0x03, "return_rate", "Return rate", "code", _identity, "config"),
    _r(0x34, "accel_x", "Accel X", "g", _scaled(_ACCEL_RANGE_G), "accel"),
    _r(0x35, "accel_y", "Accel Y", "g", _scaled(_ACCEL_RANGE_G), "accel"),
    _r(0x36, "accel_z", "Accel Z", "g", _scaled(_ACCEL_RANGE_G), "accel"),
    _r(0x37, "gyro_x", "Gyro X", "deg/s", _scaled(_GYRO_RANGE_DPS), "gyro"),
    _r(0x38, "gyro_y", "Gyro Y", "deg/s", _scaled(_GYRO_RANGE_DPS), "gyro"),
    _r(0x39, "gyro_z", "Gyro Z", "deg/s", _scaled(_GYRO_RANGE_DPS), "gyro"),
    _r(0x3A, "velocity_x", "Velocity X", "mm/s", _identity, "velocity"),
    _r(0x3B, "velocity_y", "Velocity Y", "mm/s", _identity, "velocity"),
    _r(0x3C, "velocity_z", "Velocity Z", "mm/s", _identity, "velocity"),
    _r(0x3D, "angle_x", "Angle X", "deg", _scaled(_ANGLE_RANGE_DEG), "angle"),
    _r(0x3E, "angle_y", "Angle Y", "deg", _scaled(_ANGLE_RANGE_DEG), "angle"),
    _r(0x3F, "angle_z", "Angle Z", "deg", _scaled(_ANGLE_RANGE_DEG), "angle"),
    _r(0x40, "temperature", "Temperature", "degC", _divided(100.0), "device"),
    _r(0x41, "displacement_x", "Displacement X", "um", _identity, "displacement"),
    _r(0x42, "displacement_y", "Displacement Y", "um", _identity, "displacement"),
    _r(0x43, "displacement_z", "Displacement Z", "um", _identity, "displacement"),
    _r(0x44, "frequency_x", "Frequency X", "Hz", _identity, "frequency"),
    _r(0x45, "frequency_y", "Frequency Y", "Hz", _identity, "frequency"),
    _r(0x46, "frequency_z", "Frequency Z", "Hz", _identity, "frequency"),
    _r(0x47, "fast_disp_x", "Fast displacement X", "um", _identity, "fast"),
    _r(0x48, "fast_disp_y", "Fast displacement Y", "um", _identity, "fast"),
    _r(0x49, "fast_disp_z", "Fast displacement Z", "um", _identity, "fast"),
    _r(0x5D, "cutoff_int", "Cut-off freq (int)", "Hz", _identity, "config"),
    _r(0x5E, "cutoff_frac", "Cut-off freq (frac)", "-", _identity, "config"),
    _r(0x5F, "sample_freq", "Detection cycle", "Hz", _identity, "config"),
    _r(0x64, "power_raw", "Power (raw)", "-", _identity, "device"),
)

BY_ADDRESS: Mapping[int, Register] = {reg.address: reg for reg in REGISTERS}
BY_KEY: Mapping[str, Register] = {reg.key: reg for reg in REGISTERS}

# Vibration registers, the ones the manual documents as the sensor's payload.
VIBRATION_ADDRESSES: tuple[int, ...] = tuple(range(0x3A, 0x47))

# Return-rate register 0x03 codes -> Hz, from the WitMotion standard protocol.
RETURN_RATE_HZ: Mapping[int, float] = {
    0x01: 0.2, 0x02: 0.5, 0x03: 1.0, 0x04: 2.0, 0x05: 5.0, 0x06: 10.0,
    0x07: 20.0, 0x08: 50.0, 0x09: 100.0, 0x0A: 125.0, 0x0B: 200.0,
}
