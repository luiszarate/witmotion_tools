"""CSV output with time-based rotation.

One sink per sensor. It owns a :class:`~wtvb01.recorder.CsvRecorder` and
swaps it for a fresh one when the rotation period elapses or when someone
asks for a roll over the control socket.

Rotation matters on an unattended logger: it bounds how much is lost to a
power cut, and it makes a stretch of a flight easy to find by filename.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from wtvb01.model import Sample
from wtvb01.recorder import CsvRecorder

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_MAX_NAME_ATTEMPTS = 100


@dataclass(frozen=True)
class SinkStatus:
    sensor: str = ""
    path: str = ""
    rows_in_file: int = 0
    rows_total: int = 0
    files: int = 0
    dropped: int = 0
    paused: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "sensor": self.sensor,
            "path": self.path,
            "rows_in_file": self.rows_in_file,
            "rows_total": self.rows_total,
            "files": self.files,
            "dropped": self.dropped,
            "paused": self.paused,
        }


def unique_path(directory: Path, sensor: str, when: datetime | None = None) -> Path:
    """``<dir>/<sensor>-<stamp>.csv``, with a suffix if that name is taken.

    Two rolls inside the same second would otherwise collide and the first
    file's rows would be lost.
    """
    stamp = (when or datetime.now()).strftime(_STAMP_FORMAT)
    candidate = directory / f"{sensor}-{stamp}.csv"
    if not candidate.exists():
        return candidate
    for attempt in range(1, _MAX_NAME_ATTEMPTS):
        candidate = directory / f"{sensor}-{stamp}-{attempt}.csv"
        if not candidate.exists():
            return candidate
    raise OSError(f"cannot find a free filename for {sensor} at {stamp}")


class RotatingCsvSink:
    """Writes samples to a rotating series of CSV files."""

    def __init__(
        self,
        directory: Path,
        sensor: str,
        rotate_seconds: float,
        flush_seconds: float = 2.0,
        max_rate_hz: float = 0.0,
    ) -> None:
        self._directory = Path(directory)
        self._sensor = sensor
        self._rotate_seconds = rotate_seconds
        self._flush_seconds = flush_seconds
        self._min_interval = 1.0 / max_rate_hz if max_rate_hz > 0 else 0.0
        self._lock = threading.Lock()
        self._recorder: CsvRecorder | None = None
        self._opened_at = 0.0
        self._last_flush = 0.0
        self._last_written = 0.0
        self._paused = False
        self._rows_total = 0
        self._files = 0
        self._dropped = 0

    # --- lifecycle -----------------------------------------------------------

    def open(self) -> Path:
        """Start the first file. Safe to call again; rolls if already open."""
        with self._lock:
            return self._open_locked()

    def roll(self) -> Path:
        """Close the current file and start a new one."""
        with self._lock:
            return self._open_locked()

    def close(self) -> None:
        with self._lock:
            if self._recorder is not None:
                self._recorder.close()
                self._recorder = None

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            if self._recorder is not None:
                self._recorder.flush()

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    # --- writing -------------------------------------------------------------

    def write(self, sample: Sample) -> bool:
        """Append one sample. Returns False if it was decimated or paused."""
        now = time.monotonic()
        with self._lock:
            if self._paused:
                self._dropped += 1
                return False
            if self._min_interval and now - self._last_written < self._min_interval:
                self._dropped += 1
                return False
            if self._recorder is None or now - self._opened_at >= self._rotate_seconds:
                self._open_locked()
            recorder = self._recorder
            self._last_written = now
            self._rows_total += 1
            should_flush = self._flush_seconds and now - self._last_flush >= self._flush_seconds
            if should_flush:
                self._last_flush = now
        # Write outside the lock: the file handle has its own.
        if recorder is not None:
            recorder.write(sample)
            if should_flush:
                recorder.flush()
        return True

    def flush(self) -> None:
        with self._lock:
            recorder = self._recorder
            self._last_flush = time.monotonic()
        if recorder is not None:
            recorder.flush()

    # --- state ---------------------------------------------------------------

    @property
    def path(self) -> Path | None:
        with self._lock:
            return self._recorder.path if self._recorder else None

    def status(self) -> SinkStatus:
        with self._lock:
            return SinkStatus(
                sensor=self._sensor,
                path=str(self._recorder.path) if self._recorder else "",
                rows_in_file=self._recorder.rows if self._recorder else 0,
                rows_total=self._rows_total,
                files=self._files,
                dropped=self._dropped,
                paused=self._paused,
            )

    # --- internals -----------------------------------------------------------

    def _open_locked(self) -> Path:
        if self._recorder is not None:
            self._recorder.close()
        self._directory.mkdir(parents=True, exist_ok=True)
        path = unique_path(self._directory, self._sensor)
        self._recorder = CsvRecorder(path, constants={"sensor": self._sensor})
        self._opened_at = time.monotonic()
        self._last_flush = self._opened_at
        self._files += 1
        return path
