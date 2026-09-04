"""Supervision: reconnecting after a drop, and after a silent stall."""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from wtvb01.acquisition import SourceStatus
from wtvb01_logger.config import SensorConfig
from wtvb01_logger.sinks import RotatingCsvSink
from wtvb01_logger.worker import SensorWorker


def _wait_until(predicate, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _FakeSource:
    """A source whose connection state and frame counter a test drives."""

    def __init__(self):
        self.status = SourceStatus(connected=True, port="/dev/fake")
        self.stopped = False
        self._lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def advance(self, frames):
        with self._lock:
            self.status = SourceStatus(**{**self.status.__dict__, "frames": frames})

    def drop(self, error="peer disconnected"):
        with self._lock:
            self.status = SourceStatus(**{**self.status.__dict__, "connected": False, "error": error})


class WorkerSupervisionTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.sink = RotatingCsvSink(Path(self._dir.name), "s", rotate_seconds=3600)
        self.sources = []
        self.created = threading.Event()

    def _factory(self, _config, _on_sample):
        source = _FakeSource()
        self.sources.append(source)
        self.created.set()
        return source

    def _worker(self, **kwargs):
        config = SensorConfig(
            name="s", transport="serial", port="/dev/fake",
            reconnect_seconds=0.2, **kwargs
        )
        worker = SensorWorker(config, self.sink, self._factory)
        worker.start()
        self.addCleanup(worker.stop)
        return worker

    def test_a_dropped_link_is_rebuilt(self):
        worker = self._worker()
        self.assertTrue(_wait_until(lambda: self.sources))
        self.sources[0].drop()
        self.assertTrue(_wait_until(lambda: len(self.sources) >= 2))
        self.assertTrue(self.sources[0].stopped)
        self.assertTrue(worker.status().connected)

    def test_a_connected_but_silent_link_is_rebuilt(self):
        # A half-open BLE link reports itself connected while delivering
        # nothing. Without a watchdog the worker would log nothing forever.
        worker = self._worker(stall_seconds=1.0)
        self.assertTrue(_wait_until(lambda: self.sources))

        # The reason is only visible between the stall being detected and the
        # reconnect succeeding, so sample it rather than reading it after.
        seen = []
        watcher = threading.Thread(
            target=lambda: [seen.append(worker.status().error) or time.sleep(0.01)
                            for _ in range(600)],
            daemon=True,
        )
        watcher.start()
        self.assertTrue(_wait_until(lambda: len(self.sources) >= 2, timeout=8.0))
        self.assertTrue(self.sources[0].stopped)
        self.assertTrue(
            any("sin tramas" in error for error in seen),
            f"no se registró el motivo del reinicio; errores vistos: {set(seen)}",
        )

    def test_a_link_that_keeps_delivering_is_left_alone(self):
        worker = self._worker(stall_seconds=1.0)
        self.assertTrue(_wait_until(lambda: self.sources))
        source = self.sources[0]
        deadline = time.monotonic() + 3.0
        frames = 0
        while time.monotonic() < deadline:
            frames += 1
            source.advance(frames)
            time.sleep(0.1)
        self.assertEqual(len(self.sources), 1)
        self.assertEqual(worker.status().error, "")

    def test_the_watchdog_can_be_disabled(self):
        self._worker(stall_seconds=0.0)
        self.assertTrue(_wait_until(lambda: self.sources))
        time.sleep(2.0)
        self.assertEqual(len(self.sources), 1)


if __name__ == "__main__":
    unittest.main()
