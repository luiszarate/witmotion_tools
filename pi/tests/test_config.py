"""Configuration parsing and validation."""

import tempfile
import unittest
from pathlib import Path

from wtvb01_logger import config as cfg

MINIMAL = {"sensors": [{"name": "a", "transport": "serial", "port": "/dev/ttyUSB0"}]}


class ParseTest(unittest.TestCase):
    def test_minimal_document_gets_sensible_defaults(self):
        settings = cfg.parse(MINIMAL)
        self.assertEqual(settings.rotate_minutes, cfg.DEFAULT_ROTATE_MINUTES)
        self.assertEqual(settings.sensors[0].baudrate, 115200)
        self.assertEqual(settings.sensors[0].mode, "normal")
        self.assertTrue(settings.sensors[0].enabled)

    def test_the_service_comes_up_idle_unless_told_otherwise(self):
        # Sensors stay free until someone asks for them; opting in is explicit.
        self.assertFalse(cfg.parse(MINIMAL).connect_on_start)
        self.assertTrue(cfg.parse({**MINIMAL, "logger": {"connect_on_start": True}}).connect_on_start)

    def test_connect_on_start_must_be_a_boolean(self):
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse({**MINIMAL, "logger": {"connect_on_start": "si"}})
        self.assertIn("true or false", str(caught.exception))

    def test_rotate_minutes_becomes_seconds(self):
        self.assertEqual(cfg.parse({**MINIMAL, "logger": {"rotate_minutes": 2.5}}).rotate_seconds, 150.0)

    def test_defaults_section_applies_to_every_sensor(self):
        settings = cfg.parse({**MINIMAL, "defaults": {"mode": "high_speed", "max_rate_hz": 25}})
        self.assertEqual(settings.sensors[0].mode, "high_speed")
        self.assertEqual(settings.sensors[0].max_rate_hz, 25.0)

    def test_a_sensor_overrides_the_defaults(self):
        document = {
            "defaults": {"mode": "high_speed"},
            "sensors": [{"name": "a", "transport": "serial", "port": "/dev/x", "mode": "stream"}],
        }
        self.assertEqual(cfg.parse(document).sensors[0].mode, "stream")

    def test_disabled_sensors_are_kept_but_excluded_from_the_run(self):
        document = {"sensors": [
            {"name": "a", "transport": "serial", "port": "/dev/x"},
            {"name": "b", "transport": "serial", "port": "/dev/y", "enabled": False},
        ]}
        settings = cfg.parse(document)
        self.assertEqual(len(settings.sensors), 2)
        self.assertEqual([s.name for s in settings.enabled_sensors], ["a"])

    def test_target_is_the_port_or_the_address(self):
        serial_sensor = cfg.parse(MINIMAL).sensors[0]
        self.assertEqual(serial_sensor.target, "/dev/ttyUSB0")
        ble = cfg.parse({"sensors": [{"name": "b", "transport": "ble", "address": "AA:BB"}]}).sensors[0]
        self.assertEqual(ble.target, "AA:BB")


class ValidationTest(unittest.TestCase):
    def _fails(self, document, fragment):
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.parse(document)
        self.assertIn(fragment, str(caught.exception))

    def test_no_sensors_is_refused(self):
        self._fails({"sensors": []}, "no sensors declared")

    def test_a_serial_sensor_needs_a_port(self):
        self._fails({"sensors": [{"name": "a", "transport": "serial"}]}, "needs 'port'")

    def test_a_ble_sensor_needs_an_address(self):
        self._fails({"sensors": [{"name": "a", "transport": "ble"}]}, "needs 'address'")

    def test_unknown_transport(self):
        self._fails({"sensors": [{"name": "a", "transport": "zigbee"}]}, "'transport' must be one of")

    def test_unknown_mode_names_the_valid_options(self):
        self._fails(
            {"sensors": [{"name": "a", "transport": "serial", "port": "/dev/x", "mode": "turbo"}]},
            "high_speed",
        )

    def test_names_that_would_break_filenames_are_refused(self):
        self._fails(
            {"sensors": [{"name": "rotor izq", "transport": "serial", "port": "/dev/x"}]},
            "cannot contain spaces or slashes",
        )

    def test_duplicate_names_are_refused(self):
        document = {"sensors": [
            {"name": "a", "transport": "serial", "port": "/dev/x"},
            {"name": "a", "transport": "serial", "port": "/dev/y"},
        ]}
        self._fails(document, "duplicate sensor name")

    def test_a_typo_in_a_key_is_an_error_not_a_shrug(self):
        # On a headless logger a silently ignored key is invisible until the
        # data is wrong.
        self._fails({**MINIMAL, "logger": {"rotate_minute": 5}}, "unknown key(s) rotate_minute")

    def test_a_typo_in_a_sensor_key_is_an_error(self):
        self._fails(
            {"sensors": [{"name": "a", "transport": "serial", "port": "/dev/x", "pol_interval": 1}]},
            "unknown key(s) pol_interval",
        )

    def test_non_numeric_values_are_refused(self):
        self._fails({**MINIMAL, "logger": {"rotate_minutes": "quince"}}, "expected a number")

    def test_out_of_range_values_are_refused(self):
        self._fails({**MINIMAL, "logger": {"rotate_minutes": 0}}, "must be at least")


class LoadTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "config.toml"

    def test_reads_a_file(self):
        self.path.write_text(
            '[logger]\nrotate_minutes = 3\n\n'
            '[[sensors]]\nname = "a"\ntransport = "serial"\nport = "/dev/ttyUSB0"\n'
        )
        self.assertEqual(cfg.load(self.path).rotate_minutes, 3.0)

    def test_a_missing_file_says_so(self):
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.load(self.path)
        self.assertIn("cannot read", str(caught.exception))

    def test_broken_toml_says_so(self):
        self.path.write_text("[logger\n")
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.load(self.path)
        self.assertIn("not valid TOML", str(caught.exception))

    def test_the_shipped_example_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        settings = cfg.load(example)
        self.assertEqual([s.name for s in settings.sensors], ["rotor-izq", "rotor-der", "mastil"])


if __name__ == "__main__":
    unittest.main()


class BleScanErrorTest(unittest.TestCase):
    """A BLE stack that is not ready must report, not traceback."""

    def test_scan_failure_is_reported_cleanly(self):
        import argparse
        import io
        from contextlib import redirect_stderr

        from wtvb01 import ble
        from wtvb01_logger import cli

        original = ble.scan

        async def failing(_seconds):
            raise OSError("[org.bluez.Error.NotReady] Resource Not Ready")

        ble.scan = failing
        self.addCleanup(lambda: setattr(ble, "scan", original))

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.cmd_ble_scan(argparse.Namespace(seconds=0.1))
        self.assertEqual(code, 2)
        self.assertIn("no se pudo escanear", stderr.getvalue())
        self.assertIn("rfkill unblock bluetooth", stderr.getvalue())


class EncodingErrorTest(unittest.TestCase):
    """A stray byte is an encoding fault, not a syntax fault."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "config.toml"

    def test_reports_the_byte_and_the_line_it_is_on(self):
        # 0xC3 alone is how an unfinished dead-key accent lands in a file.
        self.path.write_bytes(
            b'[logger]\nrotate_minutes = 5\noutput_dir = "\xc3~/logs"\n'
        )
        with self.assertRaises(cfg.ConfigError) as caught:
            cfg.load(self.path)
        message = str(caught.exception)
        self.assertIn("not UTF-8", message)
        self.assertIn("0xC3", message)
        self.assertIn("line 3", message)
        # The old message blamed TOML syntax, which sent you hunting for a
        # missing bracket that was never there.
        self.assertNotIn("not valid TOML", message)

    def test_valid_utf8_with_accents_is_fine(self):
        self.path.write_text(
            '# configuración con acentos\n'
            '[[sensors]]\nname = "a"\ntransport = "serial"\nport = "/dev/ttyUSB0"\n',
            encoding="utf-8",
        )
        self.assertEqual(len(cfg.load(self.path).sensors), 1)
