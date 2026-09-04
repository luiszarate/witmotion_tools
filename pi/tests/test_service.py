"""The service end to end: config in, rotating CSV out, control socket live."""

import csv
import tempfile
import time
import unittest
from pathlib import Path

from wtvb01 import source as source_module
from wtvb01.testing import UART_FRAME, FakeSerial
from wtvb01_logger.config import LoggerConfig, SensorConfig
from wtvb01_logger.control import request
from wtvb01_logger.service import LoggerService
from wtvb01_logger.worker import build_source


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Harness:
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.socket = self.dir / "control.sock"

        # A fake port that keeps producing frames, so the reader never idles
        # out and the service behaves like it does with real hardware.
        FakeSerial.reset(UART_FRAME * 200)
        self._real_serial = source_module.serial
        source_module.serial = type("module", (), {"Serial": FakeSerial})
        self.addCleanup(lambda: setattr(source_module, "serial", self._real_serial))

        self.config = LoggerConfig(
            output_dir=self.dir / "logs",
            rotate_minutes=60.0,
            flush_seconds=0.0,
            control_socket=self.socket,
            # These cases are about logging, so they opt in to connecting.
            # The shipped default is to come up idle.
            connect_on_start=True,
            sensors=(
                SensorConfig(name="rotor", transport="serial", port="/dev/fake",
                             mode="stream", reconnect_seconds=0.2),
            ),
        )
        self.service = LoggerService(self.config)

    def _start(self):
        self.service.start()
        self.addCleanup(self.service.stop)
        self.assertTrue(_wait_until(lambda: self._files()))

    def _files(self):
        return sorted((self.dir / "logs" / "rotor").glob("*.csv"))

    def _rows(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def _send(self, command, **extra):
        return request(self.socket, {"command": command, **extra})


class ServiceTest(_Harness, unittest.TestCase):
    # --- logging -------------------------------------------------------------

    def test_logs_physical_units_to_a_file_per_sensor(self):
        self._start()
        self.assertTrue(_wait_until(lambda: self._rows(self._files()[0])))
        row = self._rows(self._files()[0])[0]
        self.assertEqual(row["sensor"], "rotor")
        # Acceleration Z reads about 1 g with the sensor at rest.
        self.assertAlmostEqual(float(row["accel_z"]), 1.0388, places=3)

    def test_each_sensor_gets_its_own_directory(self):
        self._start()
        self.assertTrue((self.dir / "logs" / "rotor").is_dir())

    # --- control -------------------------------------------------------------

    def test_ping(self):
        self._start()
        self.assertTrue(self._send("ping")["pong"])

    def test_status_reports_the_link_and_the_file(self):
        self._start()
        self.assertTrue(_wait_until(lambda: self._send("status")["sensors"][0]["connected"]))
        sensor = self._send("status")["sensors"][0]
        self.assertEqual(sensor["sensor"], "rotor")
        self.assertEqual(sensor["source"]["layout"], "uart-40")
        self.assertTrue(sensor["sink"]["path"].endswith(".csv"))

    def test_roll_starts_a_new_file_without_dropping_the_link(self):
        self._start()
        before = self._files()[0]
        self.assertEqual(self._send("roll")["rolled"], ["rotor"])
        self.assertTrue(_wait_until(lambda: len(self._files()) == 2))
        self.assertIn(before, self._files())
        self.assertTrue(self._send("status")["sensors"][0]["connected"])

    def test_pause_and_resume(self):
        self._start()
        self._send("pause")
        self.assertTrue(self._send("status")["sensors"][0]["sink"]["paused"])
        self._send("resume")
        self.assertFalse(self._send("status")["sensors"][0]["sink"]["paused"])

    def test_roll_can_target_one_sensor(self):
        self._start()
        self.assertEqual(self._send("roll", sensor="rotor")["rolled"], ["rotor"])

    def test_an_unknown_sensor_is_refused_with_the_configured_names(self):
        self._start()
        reply = self._send("roll", sensor="nope")
        self.assertFalse(reply["ok"])
        self.assertIn("rotor", reply["error"])

    def test_stop_command_asks_the_service_to_finish(self):
        self._start()
        self.assertTrue(self._send("stop")["stopping"])

    # --- resilience ----------------------------------------------------------

    def test_a_sensor_that_never_connects_does_not_stop_the_others(self):
        config = LoggerConfig(
            output_dir=self.dir / "logs",
            control_socket=self.socket,
            connect_on_start=True,
            sensors=(
                SensorConfig(name="ok", transport="serial", port="/dev/fake",
                             mode="stream", reconnect_seconds=0.2),
                SensorConfig(name="ausente", transport="serial", port="/dev/does-not-exist",
                             mode="stream", reconnect_seconds=0.2),
            ),
        )
        # A source whose start() always fails, standing in for a sensor that
        # is unplugged, out of range or not powered yet.
        class _Unreachable:
            def start(self_inner):
                raise OSError("no such device /dev/does-not-exist")

            def stop(self_inner):
                pass

        def factory(sensor, on_sample):
            if sensor.name == "ausente":
                return _Unreachable()
            return build_source(sensor, on_sample)

        service = LoggerService(config, source_factory=factory)
        service.start()
        self.addCleanup(service.stop)
        self.assertTrue(_wait_until(lambda: request(self.socket, {"command": "status"})["sensors"][0]["connected"]))
        sensors = {s["sensor"]: s for s in request(self.socket, {"command": "status"})["sensors"]}
        self.assertTrue(sensors["ok"]["connected"])
        self.assertFalse(sensors["ausente"]["connected"])
        self.assertIn("does-not-exist", sensors["ausente"]["error"])

    def test_a_configuration_with_every_sensor_disabled_is_refused(self):
        config = LoggerConfig(
            output_dir=self.dir / "logs",
            control_socket=self.socket,
            connect_on_start=True,
            sensors=(SensorConfig(name="a", transport="serial", port="/dev/fake", enabled=False),),
        )
        with self.assertRaises(RuntimeError):
            LoggerService(config).start()


if __name__ == "__main__":
    unittest.main()


class ManualConnectionTest(_Harness, unittest.TestCase):
    """Attaching and releasing sensors by command, without stopping the service."""

    def test_disconnect_releases_the_transport_and_connect_takes_it_back(self):
        self._start()
        self.assertTrue(_wait_until(lambda: self._send("status")["sensors"][0]["connected"]))

        released = self._send("disconnect")
        self.assertEqual(released["disconnected"], ["rotor"])
        sensor = self._send("status")["sensors"][0]
        self.assertFalse(sensor["running"])
        self.assertFalse(sensor["connected"])
        # Unlike pause, the port itself is given up.
        self.assertTrue(FakeSerial.instances[-1].closed)

        FakeSerial.reset(UART_FRAME * 200)
        self.assertEqual(self._send("connect")["connected"], ["rotor"])
        self.assertTrue(_wait_until(lambda: self._send("status")["sensors"][0]["connected"]))

    def test_disconnect_is_idempotent(self):
        self._start()
        self._send("disconnect")
        self.assertEqual(self._send("disconnect")["disconnected"], [])

    def test_connect_is_idempotent(self):
        self._start()
        self.assertTrue(_wait_until(lambda: self._send("status")["sensors"][0]["connected"]))
        self.assertEqual(self._send("connect")["connected"], [])

    def test_a_released_sensor_keeps_its_data(self):
        self._start()
        self.assertTrue(_wait_until(lambda: self._rows(self._files()[0])))
        rows_before = len(self._rows(self._files()[0]))
        self._send("disconnect")
        self.assertGreaterEqual(len(self._rows(self._files()[0])), rows_before)


class IdleStartTest(_Harness, unittest.TestCase):
    """connect_on_start = false brings the service up without touching sensors."""

    def test_the_service_starts_idle_by_default(self):
        from dataclasses import replace

        # connect_on_start defaults to False so the sensors stay free.
        service = LoggerService(replace(self.config, connect_on_start=False))
        service.start()
        self.addCleanup(service.stop)
        self.assertTrue(self._send("ping")["pong"])
        sensor = self._send("status")["sensors"][0]
        self.assertFalse(sensor["running"])
        self.assertEqual(FakeSerial.instances, [])

        self.assertEqual(self._send("connect")["connected"], ["rotor"])
        self.assertTrue(_wait_until(lambda: self._send("status")["sensors"][0]["connected"]))
