"""The logger service: config in, CSV files out, control socket on the side."""

from __future__ import annotations

import signal
import threading
import time
from typing import Any, Callable

from .config import LoggerConfig, SensorConfig
from .control import ControlServer
from .sinks import RotatingCsvSink
from .worker import SensorWorker, build_source

COMMANDS = ("ping", "status", "roll", "pause", "resume", "connect", "disconnect", "stop")


class LoggerService:
    """Runs one worker per configured sensor and answers control commands."""

    def __init__(self, config: LoggerConfig, source_factory: Callable = build_source) -> None:
        self._config = config
        self._source_factory = source_factory
        self._workers: dict[str, SensorWorker] = {}
        self._control: ControlServer | None = None
        self._stop = threading.Event()
        self._started_at = 0.0

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        sensors = self._config.enabled_sensors
        if not sensors:
            raise RuntimeError("every configured sensor is disabled; nothing to log")
        self._started_at = time.time()
        for sensor in sensors:
            worker = SensorWorker(sensor, self._build_sink(sensor), self._source_factory)
            self._workers[sensor.name] = worker
            if self._config.connect_on_start:
                worker.start()
        self._control = ControlServer(self._config.control_socket, self.handle_command)
        self._control.start()

    def stop(self) -> None:
        self._stop.set()
        control, self._control = self._control, None
        if control is not None:
            control.stop()
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()

    def run_forever(self) -> int:
        """Start, install signal handlers, and block until asked to stop.

        SIGHUP rolls every file, so ``systemctl reload`` or ``kill -HUP`` is a
        second way to segment a recording when the control socket is
        inconvenient.
        """
        self.start()
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGHUP, lambda *_: self.roll())
        try:
            self._stop.wait()
        finally:
            self.stop()
        return 0

    # --- actions -------------------------------------------------------------

    def roll(self, sensor: str | None = None) -> list[str]:
        """Start a fresh file for one sensor, or for all of them."""
        rolled = []
        for worker in self._select(sensor):
            worker.sink.roll()
            rolled.append(worker.name)
        return rolled

    def pause(self, sensor: str | None = None) -> list[str]:
        for worker in self._select(sensor):
            worker.sink.pause()
        return [worker.name for worker in self._select(sensor)]

    def resume(self, sensor: str | None = None) -> list[str]:
        for worker in self._select(sensor):
            worker.sink.resume()
        return [worker.name for worker in self._select(sensor)]

    def connect(self, sensor: str | None = None) -> list[str]:
        """Attach to sensors that are currently released."""
        started = []
        for worker in self._select(sensor):
            if not worker.running:
                worker.start()
                started.append(worker.name)
        return started

    def disconnect(self, sensor: str | None = None) -> list[str]:
        """Release the transport and close the file, leaving the service up.

        Unlike ``pause``, this frees the radio or the serial port, so the
        sensor can be used by something else.
        """
        stopped = []
        for worker in self._select(sensor):
            if worker.running:
                worker.stop()
                stopped.append(worker.name)
        return stopped

    def status(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0.0,
            "output_dir": str(self._config.output_dir),
            "rotate_minutes": self._config.rotate_minutes,
            "control_socket": str(self._config.control_socket),
            "sensors": [worker.status().as_dict() for worker in self._workers.values()],
        }

    # --- control -------------------------------------------------------------

    def handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("command") or "")
        sensor = request.get("sensor") or None
        if command not in COMMANDS:
            return {"ok": False, "error": f"unknown command {command!r}; expected one of {', '.join(COMMANDS)}"}
        if sensor is not None and sensor not in self._workers:
            known = ", ".join(self._workers) or "none"
            return {"ok": False, "error": f"unknown sensor {sensor!r}; configured: {known}"}
        if command == "ping":
            return {"ok": True, "pong": True}
        if command == "status":
            return {"ok": True, **self.status()}
        if command == "roll":
            return {"ok": True, "rolled": self.roll(sensor)}
        if command == "pause":
            return {"ok": True, "paused": self.pause(sensor)}
        if command == "resume":
            return {"ok": True, "resumed": self.resume(sensor)}
        if command == "connect":
            return {"ok": True, "connected": self.connect(sensor)}
        if command == "disconnect":
            return {"ok": True, "disconnected": self.disconnect(sensor)}
        self._stop.set()
        return {"ok": True, "stopping": True}

    # --- internals -----------------------------------------------------------

    def _build_sink(self, sensor: SensorConfig) -> RotatingCsvSink:
        return RotatingCsvSink(
            directory=self._config.output_dir / sensor.name,
            sensor=sensor.name,
            rotate_seconds=self._config.rotate_seconds,
            flush_seconds=self._config.flush_seconds,
            max_rate_hz=sensor.max_rate_hz,
        )

    def _select(self, sensor: str | None) -> list[SensorWorker]:
        if sensor is None:
            return list(self._workers.values())
        worker = self._workers.get(sensor)
        return [worker] if worker is not None else []
