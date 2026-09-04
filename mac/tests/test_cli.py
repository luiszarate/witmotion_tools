"""Argument parsing and the commands that do not need hardware."""

import io
import unittest
from contextlib import redirect_stdout

from wtvb01 import ports as ports_module
from wtvb01_monitor import cli
from wtvb01.ports import PortInfo


class ParserTest(unittest.TestCase):
    def test_serve_is_the_default_command(self):
        args = cli.build_parser().parse_args(["serve"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.http_port, 8787)

    def test_connection_flags_are_shared(self):
        args = cli.build_parser().parse_args(["monitor", "-p", "/dev/cu.x", "--frame-length", "28"])
        self.assertEqual(args.port, "/dev/cu.x")
        self.assertEqual(args.frame_length, 28)

    def test_record_takes_a_duration(self):
        args = cli.build_parser().parse_args(["record", "-d", "5"])
        self.assertEqual(args.duration, 5.0)


    def test_mode_defaults_to_normal_and_accepts_the_others(self):
        parser = cli.build_parser()
        self.assertEqual(parser.parse_args(["serve"]).mode, "normal")
        self.assertEqual(parser.parse_args(["monitor", "-m", "high_speed"]).mode, "high_speed")

    def test_poll_interval_defaults_to_the_mode_default(self):
        self.assertIsNone(cli.build_parser().parse_args(["monitor"]).poll_interval)


class ModesCommandTest(unittest.TestCase):
    def test_describes_every_mode(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_modes(None), 0)
        printed = out.getvalue()
        for key in ("normal", "high_speed", "stream"):
            self.assertIn(key, printed)


class PortsCommandTest(unittest.TestCase):
    def setUp(self):
        self._real = ports_module.available_ports
        self.addCleanup(lambda: setattr(ports_module, "available_ports", self._real))
        self._real_cli = cli.available_ports
        self.addCleanup(lambda: setattr(cli, "available_ports", self._real_cli))

    def test_lists_ports(self):
        cli.available_ports = lambda: (PortInfo("/dev/cu.usbserial-110", "USB Serial", "hw", True),)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_ports(None), 0)
        self.assertIn("/dev/cu.usbserial-110", out.getvalue())

    def test_reports_failure_when_nothing_is_connected(self):
        cli.available_ports = lambda: ()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_ports(None), 1)


class MainTest(unittest.TestCase):
    def test_session_errors_become_exit_code_2(self):
        self.assertEqual(cli.main(["monitor", "-p", "/dev/does-not-exist"]), 2)


if __name__ == "__main__":
    unittest.main()
