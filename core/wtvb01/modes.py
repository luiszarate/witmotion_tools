"""Capture modes: how the host acquires data, not a setting inside the sensor.

The WTVB01-BT50 has no mode register. What its PC software calls "normal" and
"high-speed" mode is a host-side choice of which register blocks to read back
and how often:

* the free-running 0x61 frame always arrives at the sensor's return rate and
  carries acceleration, gyro, vibration velocity and angular amplitude;
* temperature, displacement and frequency only ever arrive in answer to an
  explicit register read;
* registers 0x47-0x49 (FDNFX/Y/Z) hold the high-speed displacement waveform,
  which is only meaningful when read tens of times per second.

Measured on a physical unit at 115200 baud, with the 100 Hz output frame
running: polling one block every 20 ms answered 97 of 100 requests, while
every 10 ms answered only 25 of 200. Hence the 20 ms default and 10 ms floor
for high-speed capture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: Read-back of this block returns registers 0x3A-0x41: velocity, angle,
#: temperature.
BLOCK_VIBRATION = 0x3A
#: Read-back of this block returns registers 0x42-0x49: displacement,
#: frequency and the high-speed displacement waveform.
BLOCK_DISPLACEMENT = 0x42


@dataclass(frozen=True)
class CaptureMode:
    """One acquisition strategy."""

    key: str
    label: str
    description: str
    #: The round-robin sequence of register blocks to read back, one per tick.
    #: A block may repeat, which is how a mode gives one block more of the
    #: poll budget than another. Empty means the free-running frame is the
    #: only source.
    cycle: tuple[int, ...]
    #: Seconds between read-back requests.
    default_interval: float
    #: Fastest interval worth asking for; below this the sensor drops requests.
    min_interval: float
    #: Channel groups this mode refreshes at all.
    live_groups: tuple[str, ...]
    #: Groups this mode refreshes, but too slowly to read as a waveform. The
    #: UI marks them as undersampled rather than live.
    undersampled_groups: tuple[str, ...] = ()

    @property
    def blocks(self) -> tuple[int, ...]:
        """The distinct blocks this mode reads, in order of first appearance."""
        return tuple(dict.fromkeys(self.cycle))

    def interval_for(self, requested: float | None) -> float:
        """Clamp a requested interval into what this mode can actually do.

        ``None`` takes the mode's default; a non-positive value disables
        polling outright.
        """
        if not self.cycle:
            return 0.0
        if requested is None:
            return self.default_interval
        requested = float(requested)
        if requested <= 0:
            return 0.0
        return max(self.min_interval, requested)

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "blocks": [f"0x{block:02X}" for block in self.blocks],
            "cycle_length": len(self.cycle),
            "default_interval": self.default_interval,
            "min_interval": self.min_interval,
            "live_groups": list(self.live_groups),
            "undersampled_groups": list(self.undersampled_groups),
        }


STREAM = CaptureMode(
    key="stream",
    label="Solo stream",
    description=(
        "Sin sondeo: solo la trama continua del sensor. Aceleración, giro, "
        "velocidad y ángulo a la tasa de retorno del sensor. Temperatura, "
        "desplazamiento y frecuencia quedan congelados en su último valor."
    ),
    cycle=(),
    default_interval=0.0,
    min_interval=0.0,
    live_groups=("accel", "gyro", "velocity", "angle"),
)

NORMAL = CaptureMode(
    key="normal",
    label="Normal",
    description=(
        "Trama continua más sondeo alterno de los dos bloques de medición. "
        "Todos los canales de vibración: velocidad, ángulo, temperatura, "
        "desplazamiento y frecuencia."
    ),
    cycle=(BLOCK_VIBRATION, BLOCK_DISPLACEMENT),
    default_interval=0.5,
    min_interval=0.05,
    live_groups=("accel", "gyro", "velocity", "angle", "displacement", "frequency", "fast"),
    # Reading block 0x42 also returns 0x47-0x49, but once a second that is a
    # sample of a waveform, not the waveform.
    undersampled_groups=("fast",),
)

#: In high-speed capture, nine reads of the displacement block for every one
#: of the vibration block. Block 0x42 carries the high-speed waveform
#: (0x47-0x49), so it gets almost the whole poll budget; block 0x3A carries
#: temperature and displacement X, which only need refreshing occasionally.
#: Measured at 50 Hz over 3 s: 133 of 133 displacement replies, 14 of 15
#: vibration replies.
_HIGH_SPEED_CYCLE = (BLOCK_DISPLACEMENT,) * 9 + (BLOCK_VIBRATION,)

HIGH_SPEED = CaptureMode(
    key="high_speed",
    label="Alta velocidad",
    description=(
        "Da casi todo el sondeo al bloque de desplazamiento para capturar la "
        "forma de onda de alta velocidad (registros 0x47-0x49), e intercala "
        "una lectura del bloque de vibración de cada diez para que "
        "temperatura y desplazamiento X no se queden atrás."
    ),
    cycle=_HIGH_SPEED_CYCLE,
    default_interval=0.02,
    min_interval=0.01,
    live_groups=("accel", "gyro", "velocity", "angle", "displacement", "frequency", "fast"),
)

MODES: Mapping[str, CaptureMode] = {mode.key: mode for mode in (NORMAL, HIGH_SPEED, STREAM)}

DEFAULT_MODE = NORMAL.key


def get(key: str | None) -> CaptureMode:
    """Look up a mode, falling back to the default for ``None``."""
    if key is None:
        return MODES[DEFAULT_MODE]
    mode = MODES.get(key)
    if mode is None:
        raise KeyError(f"unknown capture mode {key!r}; expected one of {', '.join(MODES)}")
    return mode


def catalog() -> list[dict[str, object]]:
    """Every mode, in the order the UI should offer them."""
    return [mode.as_dict() for mode in MODES.values()]
