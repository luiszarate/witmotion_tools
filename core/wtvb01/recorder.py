"""CSV recording of samples."""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path

from typing import Mapping

from .model import Sample
from .registers import REGISTERS

_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_BASE_COLUMNS = ("timestamp_iso", "t_epoch")
_VALUE_COLUMNS = tuple(register.key for register in REGISTERS)
_COLUMNS = (*_BASE_COLUMNS, *_VALUE_COLUMNS)


def default_filename(directory: Path, prefix: str = "wtvb01") -> Path:
    stamp = datetime.now().strftime(_TIMESTAMP_FORMAT)
    return directory / f"{prefix}-{stamp}.csv"


class CsvRecorder:
    """Appends samples to a CSV file, one row per sample.

    Columns are fixed to the full register set so files from different
    sessions line up even when a firmware omits some channels.
    """

    def __init__(self, path: Path, constants: Mapping[str, str] | None = None) -> None:
        self._path = Path(path)
        self._constants = dict(constants or {})
        self._columns = (*self._constants, *_COLUMNS)
        self._lock = threading.Lock()
        self._rows = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._writer.writerow(self._columns)
        self._handle.flush()

    @property
    def columns(self) -> tuple[str, ...]:
        return self._columns

    @property
    def path(self) -> Path:
        return self._path

    @property
    def rows(self) -> int:
        with self._lock:
            return self._rows

    @property
    def closed(self) -> bool:
        return self._handle.closed

    def write(self, sample: Sample) -> None:
        iso = datetime.fromtimestamp(sample.t, tz=timezone.utc).astimezone().isoformat()
        row = [*self._constants.values(), iso, f"{sample.t:.4f}"]
        for register in REGISTERS:
            value = sample.values.get(register.key)
            row.append("" if value is None else f"{value:g}")
        with self._lock:
            if self._handle.closed:
                return
            self._writer.writerow(row)
            self._rows += 1
            if self._rows % 50 == 0:
                self._handle.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()
                self._handle.close()
