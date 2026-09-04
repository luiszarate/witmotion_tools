"""Auto-detection of the 0x61 output frame length.

The WTVB01-BT50 emits 28-, 32- or 40-byte output frames depending on the
transport and firmware, and the frames carry no length field or checksum. The
length is however observable: in a free-running stream the distance between
consecutive ``55 61`` sync pairs *is* the frame length. This module measures
that distance and reports it once enough consistent samples have accumulated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .protocol import SYNC, TYPE_OUTPUT

MIN_LENGTH = 8
MAX_LENGTH = 128
DEFAULT_MIN_SAMPLES = 6
DEFAULT_AGREEMENT = 0.75


@dataclass
class OutputLengthDetector:
    """Measures the gap between ``55 61`` sync pairs in a byte stream."""

    min_samples: int = DEFAULT_MIN_SAMPLES
    agreement: float = DEFAULT_AGREEMENT
    _gaps: Counter = field(default_factory=Counter, repr=False)
    _tail: bytes = b"", 
    _position: int = 0
    _last_sync: int | None = None

    def __post_init__(self) -> None:
        self._tail = b""

    @property
    def samples(self) -> int:
        return sum(self._gaps.values())

    def observe(self, data: bytes) -> int | None:
        """Feed raw bytes; return the detected length once confident."""
        window = self._tail + data
        base = self._position - len(self._tail)
        for index in range(len(window) - 1):
            if window[index] != SYNC or window[index + 1] != TYPE_OUTPUT:
                continue
            absolute = base + index
            if self._last_sync is not None:
                gap = absolute - self._last_sync
                if MIN_LENGTH <= gap <= MAX_LENGTH and gap % 2 == 0:
                    self._gaps[gap] += 1
            self._last_sync = absolute
        self._position += len(data)
        self._tail = window[-1:]
        return self.result()

    def result(self) -> int | None:
        """The winning frame length, or ``None`` while still undecided."""
        total = self.samples
        if total < self.min_samples:
            return None
        length, count = self._gaps.most_common(1)[0]
        if count / total < self.agreement:
            return None
        return length

    def reset(self) -> None:
        self._gaps.clear()
        self._tail = b""
        self._position = 0
        self._last_sync = None
