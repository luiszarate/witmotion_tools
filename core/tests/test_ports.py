"""Serial port discovery."""

import unittest
from dataclasses import dataclass
from unittest import mock

from wtvb01 import ports as ports_module


@dataclass
class _FakePort:
    device: str
    description: str = ""
    hwid: str = ""


class AvailablePortsTest(unittest.TestCase):
    def setUp(self):
        self._real = ports_module.list_ports
        self.addCleanup(lambda: setattr(ports_module, "list_ports", self._real))

    def _use(self, *fake_ports, platform="darwin"):
        ports_module.list_ports = type("m", (), {"comports": staticmethod(lambda: list(fake_ports))})
        patch = mock.patch.object(ports_module.sys, "platform", platform)
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_cu_devices_are_offered(self):
        # /dev/tty.* blocks on open waiting for carrier detect, so it is never
        # a candidate on macOS.
        self._use(_FakePort("/dev/tty.usbserial-110"), _FakePort("/dev/cu.usbserial-110"))
        self.assertEqual([p.device for p in ports_module.available_ports()], ["/dev/cu.usbserial-110"])

    def test_built_in_bluetooth_and_debug_ports_are_filtered_out(self):
        self._use(_FakePort("/dev/cu.Bluetooth-Incoming-Port"), _FakePort("/dev/cu.debug-console"))
        self.assertEqual(ports_module.available_ports(), ())

    def test_usb_serial_adapters_sort_first_and_are_flagged(self):
        self._use(_FakePort("/dev/cu.aaa"), _FakePort("/dev/cu.usbserial-110", "USB Serial", "VID:PID=1A86:7523"))
        found = ports_module.available_ports()
        self.assertTrue(found[0].likely)
        self.assertEqual(found[0].device, "/dev/cu.usbserial-110")
        self.assertFalse(found[1].likely)

    def test_default_port_picks_the_most_likely(self):
        self._use(_FakePort("/dev/cu.aaa"), _FakePort("/dev/cu.wchusbserial1"))
        self.assertEqual(ports_module.default_port(), "/dev/cu.wchusbserial1")

    def test_default_port_is_none_when_nothing_is_plugged_in(self):
        self._use()
        self.assertIsNone(ports_module.default_port())

    def test_as_dict_shape(self):
        self._use(_FakePort("/dev/cu.usbserial-110", "USB Serial", "hw"))
        self.assertEqual(
            ports_module.available_ports()[0].as_dict(),
            {"device": "/dev/cu.usbserial-110", "description": "USB Serial", "hwid": "hw", "likely": True},
        )

    def test_linux_offers_usb_adapters_and_flags_them(self):
        self._use(
            _FakePort("/dev/ttyUSB0", "USB Serial", "USB VID:PID=1A86:7523"),
            _FakePort("/dev/ttyAMA0", "ttyAMA0"),
            platform="linux",
        )
        found = ports_module.available_ports()
        self.assertEqual([p.device for p in found], ["/dev/ttyUSB0", "/dev/ttyAMA0"])
        self.assertTrue(found[0].likely)
        # The Pi's own UART is a valid way to wire a sensor, but it is not a
        # sign that one is attached.
        self.assertFalse(found[1].likely)

    def test_linux_ignores_unrelated_device_nodes(self):
        self._use(_FakePort("/dev/null"), _FakePort("/dev/i2c-1"), platform="linux")
        self.assertEqual(ports_module.available_ports(), ())

    def test_macos_cu_filter_does_not_apply_on_linux(self):
        self._use(_FakePort("/dev/ttyUSB0"), platform="linux")
        self.assertEqual(len(ports_module.available_ports()), 1)

    def test_unknown_platform_offers_everything(self):
        self._use(_FakePort("COM3", "USB Serial"), platform="win32")
        self.assertEqual([p.device for p in ports_module.available_ports()], ["COM3"])

    def test_missing_pyserial_is_reported_clearly(self):
        ports_module.list_ports = None
        with self.assertRaises(RuntimeError) as caught:
            ports_module.available_ports()
        self.assertIn("pyserial", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
