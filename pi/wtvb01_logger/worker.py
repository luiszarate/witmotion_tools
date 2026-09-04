"""One supervised sensor: connect, log, and keep reconnecting.

An unattended logger must treat a missing sensor as normal, not fatal. A
worker whose sensor is unplugged, out of BLE range or not powered yet keeps
retrying on its own schedule while every other sensor carries on logging.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from wtvb01.model import Sample
from wtvb01.source import SerialSource, SourceConfig

from .config import SensorConfig
from .sinks import RotatingCsvSink

_WATCH_INTERVAL = 0.5
#: Samples buffered between acquisition and disk. Formatting and writing a CSV
#: row takes long enough on a Pi Zero to throttle the transport if it happens
#: on the acquisition callback - over BLE that runs on the asyncio loop, and
#: measured throughput halved from ~100 Hz to ~50 Hz. Handing samples to a
#: writer thread keeps the radio free. 2000 samples is ~20 s of headroom at
#: 100 Hz, enough to ride out an SD card stall.
_QUEUE_DEPTH = 2000
_QUEUE_POLL = 0.25
#: Backoff grows from the configured retry delay up to this ceiling, so a
#: sensor that is simply absent stops hammering the bus.
_MAX_BACKOFF_MULTIPLIER = 6.0


@dataclass(frozen=True)
class WorkerStatus:
    sensor: str
    transport: str
    target: str
    running: bool
    connected: bool
    error: str
    attempts: int
    #: Samples dropped because the writer could not keep up.
    overflow: int
    queued: int
    source: dict[str, Any]
    sink: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor": self.sensor,
            "transport": self.transport,
            "target": self.target,
            "running": self.running,
            "connected": self.connected,
            "error": self.error,
            "attempts": self.attempts,
            "overflow": self.overflow,
            "queued": self.queued,
            "source": self.source,
            "sink": self.sink,
        }


def build_source(config: SensorConfig, on_sample):
    """Create the transport this sensor is configured for."""
    if config.transport == "serial":
        return SerialSource(
            SourceConfig(
                port=config.port,
                baudrate=config.baudrate,
                mode=config.mode,
                poll_interval=config.poll_interval,
                output_length=config.output_length,
            ),
            on_sample,
        )
    if config.transport == "ble":
        from wtvb01.ble import BleConfig, BleSource  # imported lazily: bleak is optional

        return BleSource(
            BleConfig(
                address=config.address,
                mode=config.mode,
                poll_interval=config.poll_interval,
                output_length=config.output_length,
            ),
            on_sample,
        )
    raise ValueError(f"unknown transport {config.transport!r}")


class SensorWorker:
    """Keeps one sensor connected and its samples flowing into a sink."""

    def __init__(self, config: SensorConfig, sink: RotatingCsvSink, source_factory=build_source) -> None:
        self._config = config
        self._sink = sink
        self._source_factory = source_factory
        self._source = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error = ""
        self._attempts = 0
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_DEPTH)
        self._writer: threading.Thread | None = None
        self._overflow = 0

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"worker for {self._config.name} already started")
        with self._lock:
            self._error = ""
        self._stop.clear()
        self._writer = threading.Thread(
            target=self._write_loop, name=f"wtvb01-write-{self._config.name}", daemon=True
        )
        self._writer.start()
        self._thread = threading.Thread(
            target=self._supervise, name=f"wtvb01-{self._config.name}", daemon=True
        )
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None

    def stop(self) -> None:
        """Release the transport and close the file. The worker can start again."""
        if self._thread is None and self._writer is None:
            return
        self._stop.set()
        thread, self._thread = self._thread, None
        self._close_source()
        if thread is not None:
            thread.join(timeout=12.0)
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.join(timeout=8.0)
        self._drain()  # whatever the writer did not reach, before closing
        self._sink.close()

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def sink(self) -> RotatingCsvSink:
        return self._sink

    # --- status --------------------------------------------------------------

    def status(self) -> WorkerStatus:
        with self._lock:
            source, error, attempts = self._source, self._error, self._attempts
            overflow = self._overflow
        source_status = source.status.as_dict() if source is not None else {}
        return WorkerStatus(
            sensor=self._config.name,
            transport=self._config.transport,
            target=self._config.target,
            running=self._thread is not None,
            connected=bool(source_status.get("connected")),
            error=error,
            attempts=attempts,
            overflow=overflow,
            queued=self._queue.qsize(),
            source=source_status,
            sink=self._sink.status().as_dict(),
        )

    # --- internals -----------------------------------------------------------

    def _on_sample(self, sample: Sample) -> None:
        """Hand the sample to the writer thread; never block the transport."""
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            with self._lock:
                self._overflow += 1

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self._queue.get(timeout=_QUEUE_POLL)
            except queue.Empty:
                continue
            self._sink.write(sample)

    def _drain(self) -> None:
        while True:
            try:
                self._sink.write(self._queue.get_nowait())
            except queue.Empty:
                return

    def _close_source(self) -> None:
        with self._lock:
            source, self._source = self._source, None
        if source is not None:
            try:
                source.stop()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass

    def _supervise(self) -> None:
        backoff = self._config.reconnect_seconds
        while not self._stop.is_set():
            if self._connect():
                backoff = self._config.reconnect_seconds
                self._watch()
                self._close_source()
                if self._stop.is_set():
                    return
            else:
                ceiling = self._config.reconnect_seconds * _MAX_BACKOFF_MULTIPLIER
                backoff = min(backoff * 1.5, ceiling)
            self._stop.wait(backoff)

    def _connect(self) -> bool:
        try:
            source = self._source_factory(self._config, self._on_sample)
            source.start()
        except Exception as exc:  # noqa: BLE001 - absence is normal, keep retrying
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._attempts += 1
            return False
        with self._lock:
            self._source = source
            self._error = ""
            self._attempts += 1
        return True

    def _watch(self) -> None:
        """Stay until the link drops, stalls, or the worker is asked to stop."""
        last_frames = -1
        last_progress = time.monotonic()
        stall_after = self._config.stall_seconds
        while not self._stop.wait(_WATCH_INTERVAL):
            with self._lock:
                source = self._source
            if source is None:
                return
            status = source.status
            if not status.connected or status.error:
                with self._lock:
                    self._error = status.error or "link dropped"
                return
            now = time.monotonic()
            if status.frames != last_frames:
                last_frames, last_progress = status.frames, now
            elif stall_after and now - last_progress >= stall_after:
                # Connected but silent: a half-open link looks healthy and
                # would otherwise log nothing forever.
                with self._lock:
                    self._error = f"sin tramas en {stall_after:g} s; reconectando"
                return
