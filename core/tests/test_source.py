"""The serial reader thread, driven by a fake port."""

import threading
import time
import unittest

from wtvb01 import source as source_module
from wtvb01.testing import REGISTER_FRAME, UART_FRAME, FakeSerial
from wtvb01.protocol import read_register
from wtvb01.source import SerialSource, SourceConfig



class _Collector:
    """Gathers samples and lets a test wait for a given count."""

    def __init__(self) -> None:
        self.samples = []
        self._target = 0
        self._reached = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, sample) -> None:
        with self._lock:
            self.samples.append(sample)
            if self._target and len(self.samples) >= self._target:
                self._reached.set()

    def wait_for(self, count: int, timeout: float = 3.0) -> bool:
        with self._lock:
            self._target = count
            if len(self.samples) >= count:
                return True
            self._reached.clear()
        return self._reached.wait(timeout)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class SerialSourceTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.reset()
        self._real_serial = source_module.serial
        source_module.serial = type("module", (), {"Serial": FakeSerial})
        self.collector = _Collector()
        self.source = None

    def tearDown(self):
        if self.source is not None:
            self.source.stop()
        source_module.serial = self._real_serial

    def _start(self, script: bytes, **config):
        FakeSerial.reset(script)
        self.source = SerialSource(SourceConfig("/dev/fake", **config), self.collector)
        self.source.start()
        return self.source

    def test_detects_frame_length_then_emits_samples(self):
        self._start(UART_FRAME * 12, mode="stream")
        self.assertTrue(self.collector.wait_for(3))
        status = self.source.status
        self.assertTrue(status.connected)
        self.assertEqual(status.output_length, 40)
        self.assertEqual(status.layout, "uart-40")
        self.assertAlmostEqual(self.collector.samples[-1].accel.z, 1.0388, places=3)

    def test_a_fixed_frame_length_skips_detection(self):
        self._start(UART_FRAME * 2, mode="stream", output_length=40)
        self.assertTrue(self.collector.wait_for(2))
        self.assertEqual(self.source.status.frames, 2)

    def test_polled_registers_fill_in_channels_the_frame_omits(self):
        self._start(UART_FRAME * 12 + REGISTER_FRAME, mode="stream")
        self.assertTrue(self.collector.wait_for(13))
        latest = self.collector.samples[-1]
        self.assertAlmostEqual(latest.temperature, 34.27)
        self.assertAlmostEqual(latest.accel.z, 1.0388, places=3)

    def test_poll_requests_blocks_round_robin(self):
        self._start(UART_FRAME * 12, mode="normal", poll_interval=0.01)
        port = FakeSerial.instances[-1]
        self.assertTrue(_wait_until(lambda: len(port.writes) >= 4))
        writes = list(port.writes)
        self.assertIn(read_register(0x3A), writes)
        self.assertIn(read_register(0x42), writes)
        # Never two reads of the same block back to back.
        self.assertNotEqual(writes[0], writes[1])

    def test_queued_commands_are_written(self):
        self._start(UART_FRAME * 12, mode="stream")
        port = FakeSerial.instances[-1]
        self.source.send(b"\xff\xaa\x27\x3a\x00")
        self.assertTrue(_wait_until(lambda: bool(port.writes)))
        self.assertEqual(port.writes, [b"\xff\xaa\x27\x3a\x00"])

    def test_stop_closes_the_port(self):
        source = self._start(UART_FRAME * 12, mode="stream")
        self.assertTrue(self.collector.wait_for(2))
        source.stop()
        self.source = None
        self.assertTrue(FakeSerial.instances[-1].closed)
        self.assertFalse(source.status.connected)

    def test_starting_twice_is_refused(self):
        self._start(UART_FRAME, mode="stream")
        with self.assertRaises(RuntimeError):
            self.source.start()

    def test_mode_decides_which_blocks_get_polled(self):
        self._start(UART_FRAME * 12, mode="high_speed", poll_interval=0.01)
        port = FakeSerial.instances[-1]
        # A full cycle is nine displacement reads and one vibration read.
        self.assertTrue(_wait_until(lambda: len(port.writes) >= 10))
        cycle = port.writes[:10]
        self.assertEqual(cycle.count(read_register(0x42)), 9)
        self.assertEqual(cycle.count(read_register(0x3A)), 1)
        self.assertEqual(self.source.status.mode, "high_speed")

    def test_switching_mode_changes_the_blocks_without_reconnecting(self):
        self._start(UART_FRAME * 12, mode="stream")
        port = FakeSerial.instances[-1]
        self.assertTrue(self.collector.wait_for(2))
        self.assertEqual(port.writes, [])

        self.source.set_mode("high_speed", 0.01)
        self.assertTrue(_wait_until(lambda: len(port.writes) >= 2))
        self.assertEqual(port.writes[0], read_register(0x42))

        self.source.set_mode("stream")
        self.assertTrue(_wait_until(lambda: self.source.status.mode == "stream"))
        settled = len(port.writes)
        time.sleep(0.15)  # ~15 polls' worth at the previous interval
        self.assertEqual(len(port.writes), settled)

    def test_switching_mode_reports_the_clamped_interval(self):
        self._start(UART_FRAME, mode="stream")
        plan = self.source.set_mode("high_speed", 0.0001)
        self.assertEqual(plan.interval, 0.01)
        self.assertEqual(self.source.status.poll_interval, 0.01)

    def test_switching_to_an_unknown_mode_is_refused(self):
        self._start(UART_FRAME, mode="stream")
        with self.assertRaises(KeyError):
            self.source.set_mode("turbo")
        self.assertEqual(self.source.status.mode, "stream")

    def test_poll_rate_is_reported(self):
        self._start(UART_FRAME * 12, mode="high_speed", poll_interval=0.01)
        self.assertTrue(_wait_until(lambda: self.source.status.poll_rate_hz > 0))

    def test_open_failure_propagates(self):
        FakeSerial.reset(open_error=OSError("no such device"))
        source = SerialSource(SourceConfig("/dev/missing"), self.collector)
        with self.assertRaises(OSError):
            source.start()


if __name__ == "__main__":
    unittest.main()
