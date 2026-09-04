"""Immutable sample types built from decoded registers."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .registers import BY_ADDRESS, BY_KEY, RETURN_RATE_HZ

_AXIS_GROUPS = ("velocity", "displacement", "angle", "frequency", "accel", "gyro")


@dataclass(frozen=True)
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}

    @property
    def magnitude(self) -> float:
        return (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5


def _vector(values: Mapping[str, float], group: str) -> Vector3:
    return Vector3(
        values.get(f"{group}_x", 0.0),
        values.get(f"{group}_y", 0.0),
        values.get(f"{group}_z", 0.0),
    )


@dataclass(frozen=True)
class Sample:
    """One coherent view of the sensor at a point in time."""

    t: float
    values: Mapping[str, float]
    raw: Mapping[int, int]

    @property
    def velocity(self) -> Vector3:
        return _vector(self.values, "velocity")

    @property
    def displacement(self) -> Vector3:
        return _vector(self.values, "displacement")

    @property
    def angle(self) -> Vector3:
        return _vector(self.values, "angle")

    @property
    def frequency(self) -> Vector3:
        return _vector(self.values, "frequency")

    @property
    def accel(self) -> Vector3:
        return _vector(self.values, "accel")

    @property
    def gyro(self) -> Vector3:
        return _vector(self.values, "gyro")

    @property
    def temperature(self) -> float:
        return self.values.get("temperature", 0.0)

    @property
    def return_rate_hz(self) -> float | None:
        code = self.raw.get(0x03)
        return RETURN_RATE_HZ.get(code) if code is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "values": dict(self.values),
            "raw": {f"0x{addr:02X}": value for addr, value in sorted(self.raw.items())},
        }


@dataclass(frozen=True)
class Accumulator:
    """Latest raw value of every register seen so far.

    Output frames refresh every measurement register at once; register
    read-backs refresh only the eight registers in their block. Accumulating
    keeps a complete picture regardless of which arrives.
    """

    raw: Mapping[int, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.raw is None:
            object.__setattr__(self, "raw", {})

    def updated(self, registers: Mapping[int, int]) -> "Accumulator":
        """Return a new accumulator with ``registers`` applied."""
        return replace(self, raw={**self.raw, **registers})

    def sample(self, t: float | None = None) -> Sample:
        values = {
            BY_ADDRESS[address].key: BY_ADDRESS[address].value(raw)
            for address, raw in self.raw.items()
            if address in BY_ADDRESS
        }
        return Sample(t if t is not None else time.time(), values, dict(self.raw))


def value_keys() -> tuple[str, ...]:
    """Every physical channel name, in register order."""
    return tuple(BY_KEY)


def axis_groups() -> tuple[str, ...]:
    return _AXIS_GROUPS
