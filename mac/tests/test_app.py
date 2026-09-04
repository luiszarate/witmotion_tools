"""Session controller: connection, recording and configuration writes."""

import tempfile
import time
import unittest
from pathlib import Path

from wtvb01 import source as source_module
from wtvb01.testing import UART_FRAME, FakeSerial
from wtvb01_monitor.app import Session, SessionError
from wtvb01.protocol import save, unlock, write_register


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class SessionTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.reset(UART_FRAME * 12)
        self._real_serial = source_module.serial
        source_module.serial = type("module", (), {"Serial": FakeSerial})
        self.session = Session()
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.session.close()
        source_module.serial = self._real_serial
        self._dir.cleanup()

    def _connect(self):
        self.session.connect("/dev/fake", mode="stream")
        self.assertTrue(_wait_until(lambda: self.session.hub.latest is not None))

    def test_connect_populates_the_hub(self):
        self._connect()
        self.assertTrue(self.session.connected)
        self.assertAlmostEqual(self.session.hub.latest.accel.z, 1.0388, places=3)

    def test_connect_without_a_port_is_refused(self):
        with self.assertRaises(SessionError):
            self.session.connect("")

    def test_connecting_twice_is_refused(self):
        self._connect()
        with self.assertRaises(SessionError):
            self.session.connect("/dev/fake")

    def test_open_failure_is_reported_as_a_session_error(self):
        FakeSerial.reset(open_error=OSError("busy"))
        with self.assertRaises(SessionError) as caught:
            self.session.connect("/dev/busy")
        self.assertIn("/dev/busy", str(caught.exception))
        self.assertFalse(self.session.connected)

    def test_disconnect_is_safe_when_never_connected(self):
        self.session.disconnect()
        self.assertFalse(self.session.connected)

    def test_recording_writes_the_samples_that_arrive_while_it_runs(self):
        self.session.start_recording(Path(self._dir.name))
        self._connect()
        path = self.session.stop_recording()
        self.assertIsNotNone(path)
        self.assertGreater(len(path.read_text().splitlines()), 1)
        self.assertFalse(self.session.recording)

    def test_recording_twice_is_refused(self):
        self.session.start_recording(Path(self._dir.name))
        with self.assertRaises(SessionError):
            self.session.start_recording(Path(self._dir.name))

    def test_stop_recording_without_starting_returns_none(self):
        self.assertIsNone(self.session.stop_recording())

    def test_set_register_unlocks_writes_and_saves(self):
        self._connect()
        self.session.set_register("sample_freq", 50)
        port = FakeSerial.instances[-1]
        self.assertTrue(_wait_until(lambda: len(port.writes) >= 3))
        self.assertEqual(port.writes[:3], [unlock(), write_register(0x5F, 50), save()])

    def test_set_register_rejects_unknown_names_and_bad_values(self):
        self._connect()
        with self.assertRaises(SessionError):
            self.session.set_register("nope", 1)
        with self.assertRaises(SessionError):
            self.session.set_register("sample_freq", 999)

    def test_set_register_requires_a_connection(self):
        with self.assertRaises(SessionError):
            self.session.set_register("sample_freq", 50)

    def test_read_block_queues_a_read_command(self):
        self._connect()
        self.session.read_block(0x3A)
        port = FakeSerial.instances[-1]
        self.assertTrue(_wait_until(lambda: bool(port.writes)))

    def test_capture_mode_can_be_switched_on_a_live_connection(self):
        self._connect()
        applied = self.session.set_capture_mode("high_speed", 0.02)
        self.assertEqual(applied, {"mode": "high_speed", "poll_interval": 0.02})
        self.assertEqual(self.session.status()["source"]["mode"], "high_speed")

    def test_capture_mode_requires_a_connection(self):
        with self.assertRaises(SessionError):
            self.session.set_capture_mode("high_speed")

    def test_unknown_capture_mode_is_refused(self):
        self._connect()
        with self.assertRaises(SessionError):
            self.session.set_capture_mode("turbo")

    def test_connecting_with_an_unknown_mode_is_refused(self):
        with self.assertRaises(SessionError):
            self.session.connect("/dev/fake", mode="turbo")
        self.assertFalse(self.session.connected)

    def test_status_lists_the_available_modes(self):
        keys = [entry["key"] for entry in self.session.status()["modes"]]
        self.assertEqual(keys, ["normal", "high_speed", "stream"])

    def test_status_reports_everything_the_ui_needs(self):
        status = self.session.status()
        self.assertFalse(status["source"]["connected"])
        self.assertIsNone(status["latest"])
        self._connect()
        status = self.session.status()
        self.assertTrue(status["source"]["connected"])
        self.assertIn("values", status["latest"])


if __name__ == "__main__":
    unittest.main()
