"""Reads WTVB01-BT50 frames off a serial port in a background thread."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import serial

from . import modes
from .acquisition import (
    DEFAULT_BAUDRATE,
    Acquisition,
    PollPlan,
    SampleHandler,
    SourceStatus,
)

_READ_TIMEOUT = 0.05
_IDLE_SLEEP = 0.005

__all__ = [
    "DEFAULT_BAUDRATE",
    "PollPlan",
    "SampleHandler",
    "SerialSource",
    "SourceConfig",
    "SourceStatus",
]


@dataclass(frozen=True)
class SourceConfig:
    port: str
    baudrate: int = DEFAULT_BAUDRATE
    #: Capture mode key, see :mod:`wtvb01.modes`. Decides which register
    #: blocks get read back and how often.
    mode: str = modes.DEFAULT_MODE
    #: Seconds between read-back requests. ``None`` takes the mode's default;
    #: a non-positive value disables polling.
    poll_interval: float | None = None
    #: Fixed 0x61 frame length; None auto-detects from the stream.
    output_length: int | None = None


class SerialSource:
    """Owns the serial port and pumps its bytes into an :class:`Acquisition`."""

    def __init__(self, config: SourceConfig, on_sample: SampleHandler) -> None:
        self._config = config
        self._acquisition = Acquisition(
            on_sample,
            mode=config.mode,
            poll_interval=config.poll_interval,
            output_length=config.output_length,
            target=config.port,
            transport="serial",
        )
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending_writes: list[bytes] = []

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Open the port and begin reading. Raises on open failure."""
        if self._thread is not None:
            raise RuntimeError("source already started")
        self._serial = serial.Serial(
            self._config.port, self._config.baudrate, timeout=_READ_TIMEOUT
        )
        self._serial.reset_input_buffer()
        self._acquisition.update(connected=True, error="")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wtvb01-serial", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        self._acquisition.update(connected=False, rate_hz=0.0, poll_rate_hz=0.0)

    @property
    def status(self) -> SourceStatus:
        return self._acquisition.status

    @property
    def config(self) -> SourceConfig:
        return self._config

    @property
    def plan(self) -> PollPlan:
        return self._acquisition.plan

    def set_mode(self, mode_key: str | None, interval: float | None = None) -> PollPlan:
        """Switch capture mode while running. Raises ``KeyError`` if unknown."""
        return self._acquisition.set_mode(mode_key, interval)

    def send(self, command: bytes) -> None:
        """Queue a command for the reader thread to write."""
        with self._lock:
            self._pending_writes.append(command)

    # --- internals -----------------------------------------------------------

    def _run(self) -> None:
        next_poll = 0.0
        active_plan = self._acquisition.plan
        try:
            while not self._stop.is_set():
                self._drain_writes()
                plan = self._acquisition.plan
                if plan is not active_plan:
                    # A mode switch takes effect on the next tick, not after
                    # the previous mode's interval has elapsed.
                    active_plan, next_poll = plan, 0.0
                now = time.monotonic()
                if plan.active and now >= next_poll:
                    self._poll(plan)
                    next_poll = now + plan.interval
                if not self._read_once():
                    time.sleep(_IDLE_SLEEP)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            self._acquisition.update(connected=False, error=f"{type(exc).__name__}: {exc}")

    def _drain_writes(self) -> None:
        with self._lock:
            pending, self._pending_writes = self._pending_writes, []
        for command in pending:
            if self._serial is not None:
                self._serial.write(command)

    def _poll(self, plan: PollPlan) -> None:
        command = self._acquisition.next_poll_command(plan)
        if command is not None and self._serial is not None:
            self._serial.write(command)

    def _read_once(self) -> bool:
        port = self._serial
        if port is None:
            return False
        waiting = port.in_waiting
        chunk = port.read(waiting if waiting else 1)
        if not chunk:
            return False
        self._acquisition.consume(chunk)
        return True
