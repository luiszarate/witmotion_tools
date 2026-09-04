"""Session controller: owns the serial source, the hub and the recorder."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from wtvb01 import modes, protocol
from .hub import SampleHub
from wtvb01.model import Sample
from wtvb01.recorder import CsvRecorder, default_filename
from wtvb01.registers import RETURN_RATE_HZ
from wtvb01.source import DEFAULT_BAUDRATE, SerialSource, SourceConfig

DEFAULT_RECORD_DIR = Path.home() / "Documents" / "wtvb01-logs"

#: Configuration writes exposed to the UI. Each entry is (register, validator).
_SETTABLE = {
    "return_rate": (0x03, lambda v: v in RETURN_RATE_HZ),
    "cutoff_int": (0x5D, lambda v: 0 <= v <= 100),
    "cutoff_frac": (0x5E, lambda v: 0 <= v <= 99),
    "sample_freq": (0x5F, lambda v: 1 <= v <= 100),
}


class SessionError(RuntimeError):
    """A request that could not be satisfied, reported back to the caller."""


class Session:
    """One connected sensor, plus whatever is listening to it."""

    def __init__(self, hub: SampleHub | None = None) -> None:
        self.hub = hub or SampleHub()
        self._source: SerialSource | None = None
        self._recorder: CsvRecorder | None = None
        self._lock = threading.Lock()

    # --- connection ----------------------------------------------------------

    def connect(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        mode: str | None = None,
        poll_interval: float | None = None,
        output_length: int | None = None,
    ) -> None:
        if not port:
            raise SessionError("no serial port given")
        try:
            mode_key = modes.get(mode).key
        except KeyError as exc:
            raise SessionError(str(exc)) from exc
        with self._lock:
            if self._source is not None:
                raise SessionError(f"already connected to {self._source.config.port}")
            config = SourceConfig(port, baudrate, mode_key, poll_interval, output_length)
            source = SerialSource(config, self._on_sample)
            # Clear before the reader starts, or the first samples it delivers
            # are wiped along with the previous session's history.
            self.hub.clear()
            try:
                source.start()
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                raise SessionError(f"could not open {port}: {exc}") from exc
            self._source = source

    def disconnect(self) -> None:
        with self._lock:
            source, self._source = self._source, None
        if source is not None:
            source.stop()

    @property
    def connected(self) -> bool:
        return self._source is not None

    # --- recording -----------------------------------------------------------

    def start_recording(self, directory: Path | str | None = None) -> Path:
        target = Path(directory) if directory else DEFAULT_RECORD_DIR
        with self._lock:
            if self._recorder is not None:
                raise SessionError(f"already recording to {self._recorder.path}")
            recorder = CsvRecorder(default_filename(target))
            self._recorder = recorder
        return recorder.path

    def stop_recording(self) -> Path | None:
        with self._lock:
            recorder, self._recorder = self._recorder, None
        if recorder is None:
            return None
        recorder.close()
        return recorder.path

    @property
    def recording(self) -> bool:
        return self._recorder is not None

    # --- capture mode --------------------------------------------------------

    def set_capture_mode(self, mode: str | None, poll_interval: float | None = None) -> dict[str, Any]:
        """Switch acquisition strategy on a live connection.

        This changes only what the host asks for; nothing is written to the
        sensor, so it takes effect immediately and is always reversible.
        """
        source = self._source
        if source is None:
            raise SessionError("not connected")
        try:
            plan = source.set_mode(mode, poll_interval)
        except KeyError as exc:
            raise SessionError(str(exc)) from exc
        return {"mode": plan.mode, "poll_interval": plan.interval}

    # --- commands ------------------------------------------------------------

    def set_register(self, name: str, value: int) -> None:
        """Unlock, write one configuration register, then save."""
        entry = _SETTABLE.get(name)
        if entry is None:
            raise SessionError(f"unknown setting {name!r}")
        address, is_valid = entry
        if not is_valid(value):
            raise SessionError(f"value {value} out of range for {name}")
        source = self._source
        if source is None:
            raise SessionError("not connected")
        source.send(protocol.unlock())
        source.send(protocol.write_register(address, value))
        source.send(protocol.save())

    def read_block(self, address: int) -> None:
        source = self._source
        if source is None:
            raise SessionError("not connected")
        source.send(protocol.read_register(address))

    # --- state ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        source = self._source
        recorder = self._recorder
        latest = self.hub.latest
        return {
            "source": source.status.as_dict() if source else {"connected": False},
            "recording": {
                "active": recorder is not None,
                "path": str(recorder.path) if recorder else "",
                "rows": recorder.rows if recorder else 0,
            },
            "modes": modes.catalog(),
            "subscribers": self.hub.subscriber_count,
            "latest": latest.as_dict() if latest else None,
        }

    def close(self) -> None:
        self.disconnect()
        self.stop_recording()

    # --- internals -----------------------------------------------------------

    def _on_sample(self, sample: Sample) -> None:
        recorder = self._recorder
        if recorder is not None:
            recorder.write(sample)
        self.hub.publish(sample)
