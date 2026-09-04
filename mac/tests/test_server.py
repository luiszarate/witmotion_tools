"""HTTP API, exercised against a real loopback server."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from wtvb01 import source as source_module
from wtvb01.testing import UART_FRAME, FakeSerial
from wtvb01_monitor.app import Session
from wtvb01_monitor.server import ServerConfig, build_server


class ServerTest(unittest.TestCase):
    def setUp(self):
        FakeSerial.reset(UART_FRAME * 12)
        self._real_serial = source_module.serial
        source_module.serial = type("module", (), {"Serial": FakeSerial})
        self.session = Session()
        self.server = build_server(self.session, ServerConfig("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.session.close()
        source_module.serial = self._real_serial

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, json.loads(response.read())

    def _post(self, path, payload=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    @staticmethod
    def _drip_feed(port, interval: float = 0.02) -> threading.Event:
        """Keep handing the fake port one frame at a time until stopped."""
        stop = threading.Event()

        def feed():
            while not stop.wait(interval):
                port.feed(UART_FRAME)

        threading.Thread(target=feed, daemon=True).start()
        return stop

    def test_index_is_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            body = response.read().decode()
        self.assertIn("WTVB01-BT50", body)

    def test_static_assets_are_served(self):
        with urllib.request.urlopen(self.base + "/app.js", timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_path_traversal_is_refused(self):
        request = urllib.request.Request(self.base + "/%2e%2e/app.py")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_register_catalog(self):
        status, payload = self._get("/api/registers")
        self.assertEqual(status, 200)
        addresses = {entry["address"] for entry in payload["registers"]}
        self.assertIn("0x3A", addresses)
        self.assertIn("0x40", addresses)

    def test_status_before_and_after_connecting(self):
        status, payload = self._get("/api/status")
        self.assertFalse(payload["source"]["connected"])
        status, payload = self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["source"]["connected"])
        status, payload = self._post("/api/disconnect")
        self.assertFalse(payload["source"]["connected"])

    def test_connect_failure_returns_a_conflict(self):
        status, payload = self._post("/api/connect", {"port": ""})
        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])

    def test_connect_rejects_non_numeric_parameters(self):
        status, payload = self._post("/api/connect", {"port": "/dev/fake", "baudrate": "fast"})
        self.assertEqual(status, 400)

    def test_record_start_and_stop(self):
        self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        status, started = self._post("/api/record", {"action": "start"})
        self.assertEqual(status, 200)
        self.assertTrue(started["path"].endswith(".csv"))
        status, stopped = self._post("/api/record", {"action": "stop"})
        self.assertEqual(stopped["path"], started["path"])

    def test_record_rejects_an_unknown_action(self):
        status, payload = self._post("/api/record", {"action": "pause"})
        self.assertEqual(status, 400)

    def test_setting_requires_an_integer(self):
        status, payload = self._post("/api/setting", {"name": "sample_freq", "value": "x"})
        self.assertEqual(status, 400)

    def test_setting_without_a_connection_is_a_conflict(self):
        status, payload = self._post("/api/setting", {"name": "sample_freq", "value": 50})
        self.assertEqual(status, 409)

    def test_history_returns_samples(self):
        self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        status, payload = self._get("/api/history")
        self.assertEqual(status, 200)
        self.assertTrue(payload["samples"])
        self.assertIn("values", payload["samples"][0])

    def test_mode_endpoint_switches_capture_mode(self):
        self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        status, payload = self._post("/api/mode", {"mode": "high_speed", "poll_interval": 0.02})
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "high_speed")
        self.assertEqual(payload["source"]["mode"], "high_speed")

    def test_mode_endpoint_without_a_connection_is_a_conflict(self):
        status, _ = self._post("/api/mode", {"mode": "high_speed"})
        self.assertEqual(status, 409)

    def test_mode_endpoint_rejects_a_non_numeric_interval(self):
        self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        status, _ = self._post("/api/mode", {"mode": "normal", "poll_interval": "fast"})
        self.assertEqual(status, 400)

    def test_mode_endpoint_rejects_an_unknown_mode(self):
        self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        status, _ = self._post("/api/mode", {"mode": "turbo"})
        self.assertEqual(status, 409)

    def test_catalog_lists_capture_modes(self):
        _, payload = self._get("/api/registers")
        self.assertEqual([m["key"] for m in payload["modes"]], ["normal", "high_speed", "stream"])

    def test_unknown_endpoints(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/api/nope", timeout=5)
        self.assertEqual(caught.exception.code, 404)
        status, _ = self._post("/api/nope")
        self.assertEqual(status, 404)

    def test_stream_delivers_events(self):
        self._post("/api/connect", {"port": "/dev/fake", "mode": "stream"})
        with urllib.request.urlopen(self.base + "/api/stream", timeout=5) as response:
            self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
            # The handler subscribes just after sending headers, and the hub
            # rate-limits publication, so drip-feed frames rather than
            # delivering one burst that the limiter may swallow whole.
            stop = self._drip_feed(FakeSerial.instances[-1])
            self.addCleanup(stop.set)
            line = response.readline()
            while line in (b"\n", b": keepalive\n"):
                line = response.readline()
            self.assertTrue(line.startswith(b"data: "))
            payload = json.loads(line[len(b"data: "):])
            self.assertIn("velocity_x", payload["values"])


if __name__ == "__main__":
    unittest.main()
