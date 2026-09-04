"""Local HTTP + Server-Sent-Events front end for a :class:`~wtvb01.app.Session`.

Bound to the loopback interface only: this is a desktop tool, not a service.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from wtvb01 import modes
from .app import Session, SessionError
from wtvb01.ports import available_ports
from wtvb01.registers import REGISTERS, RETURN_RATE_HZ

WEB_ROOT = Path(__file__).parent / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
_MAX_BODY = 64 * 1024
_KEEPALIVE_SECONDS = 15.0


@dataclass(frozen=True)
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, allow_nan=False, default=str).encode("utf-8")


def _register_catalog() -> list[dict[str, Any]]:
    return [
        {
            "address": f"0x{register.address:02X}",
            "key": register.key,
            "label": register.label,
            "unit": register.unit,
            "group": register.group,
        }
        for register in REGISTERS
    ]


class _Handler(BaseHTTPRequestHandler):
    server_version = "wtvb01/1.0"
    protocol_version = "HTTP/1.1"

    # Injected by :func:`build_server`.
    session: Session

    # --- plumbing ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        pass  # The CLI prints its own, quieter status line.

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    # --- routing -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        routes: dict[str, Callable[[], None]] = {
            "/api/ports": self._get_ports,
            "/api/status": self._get_status,
            "/api/registers": self._get_registers,
            "/api/history": self._get_history,
            "/api/stream": self._get_stream,
        }
        handler = routes.get(path)
        if handler is not None:
            handler()
            return
        self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        routes: dict[str, Callable[[dict[str, Any]], None]] = {
            "/api/connect": self._post_connect,
            "/api/disconnect": self._post_disconnect,
            "/api/mode": self._post_mode,
            "/api/record": self._post_record,
            "/api/setting": self._post_setting,
        }
        handler = routes.get(path)
        if handler is None:
            self._error("unknown endpoint", HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self._error(str(exc))
            return
        try:
            handler(body)
        except SessionError as exc:
            self._error(str(exc), HTTPStatus.CONFLICT)

    # --- static --------------------------------------------------------------

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if not target.is_file() or WEB_ROOT.resolve() not in target.parents:
            self._error("not found", HTTPStatus.NOT_FOUND)
            return
        content_type, _ = mimetypes.guess_type(target.name)
        self._send(HTTPStatus.OK, target.read_bytes(), content_type or "application/octet-stream")

    # --- GET handlers --------------------------------------------------------

    def _get_ports(self) -> None:
        try:
            ports = [port.as_dict() for port in available_ports()]
        except RuntimeError as exc:
            self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "ports": ports})

    def _get_status(self) -> None:
        self._send_json({"ok": True, **self.session.status()})

    def _get_registers(self) -> None:
        self._send_json(
            {
                "ok": True,
                "registers": _register_catalog(),
                "modes": modes.catalog(),
                "return_rates": {f"0x{code:02X}": hz for code, hz in RETURN_RATE_HZ.items()},
            }
        )

    def _get_history(self) -> None:
        limit = 600
        samples = self.session.hub.history(limit)
        self._send_json({"ok": True, "samples": [sample.as_dict() for sample in samples]})

    def _get_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        subscription = self.session.hub.subscribe()
        stop = threading.Event()
        keepalive = threading.Thread(
            target=self._keepalive, args=(subscription, stop), daemon=True
        )
        keepalive.start()
        try:
            for sample in subscription:
                self.wfile.write(b"data: " + _json_bytes(sample.as_dict()) + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            stop.set()
            self.session.hub.unsubscribe(subscription)

    def _keepalive(self, subscription: Any, stop: threading.Event) -> None:
        """Nudge the socket so a silent sensor does not look like a dead page."""
        while not stop.wait(_KEEPALIVE_SECONDS):
            try:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except Exception:  # noqa: BLE001 - the reader loop reports the real end
                subscription.close()
                return

    # --- POST handlers -------------------------------------------------------

    @staticmethod
    def _optional_float(body: dict[str, Any], key: str) -> float | None:
        raw = body.get(key)
        return None if raw is None or raw == "" else float(raw)

    def _post_connect(self, body: dict[str, Any]) -> None:
        port = str(body.get("port") or "")
        mode = body.get("mode") or None
        try:
            baudrate = int(body.get("baudrate") or 115200)
            poll_interval = self._optional_float(body, "poll_interval")
            raw_length = body.get("output_length")
            output_length = int(raw_length) if raw_length else None
        except (TypeError, ValueError):
            self._error("connect parameters must be numbers")
            return
        self.session.connect(port, baudrate, mode, poll_interval, output_length)
        self._send_json({"ok": True, **self.session.status()})

    def _post_mode(self, body: dict[str, Any]) -> None:
        try:
            poll_interval = self._optional_float(body, "poll_interval")
        except (TypeError, ValueError):
            self._error("poll_interval must be a number")
            return
        applied = self.session.set_capture_mode(body.get("mode") or None, poll_interval)
        self._send_json({"ok": True, **applied, **self.session.status()})

    def _post_disconnect(self, _body: dict[str, Any]) -> None:
        self.session.disconnect()
        self._send_json({"ok": True, **self.session.status()})

    def _post_record(self, body: dict[str, Any]) -> None:
        action = str(body.get("action") or "")
        if action == "start":
            path = self.session.start_recording(body.get("directory") or None)
            self._send_json({"ok": True, "path": str(path)})
        elif action == "stop":
            path = self.session.stop_recording()
            self._send_json({"ok": True, "path": str(path) if path else ""})
        else:
            self._error("action must be 'start' or 'stop'")

    def _post_setting(self, body: dict[str, Any]) -> None:
        name = str(body.get("name") or "")
        try:
            value = int(body.get("value"))
        except (TypeError, ValueError):
            self._error("value must be an integer")
            return
        self.session.set_register(name, value)
        self._send_json({"ok": True})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """Stay quiet when a browser drops an event stream - that is normal."""
        import sys
        import traceback

        exc = sys.exception()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        traceback.print_exc()


def build_server(session: Session, config: ServerConfig | None = None) -> ThreadingHTTPServer:
    """Create an HTTP server bound to ``config`` serving ``session``."""
    config = config or ServerConfig()
    handler = type("BoundHandler", (_Handler,), {"session": session})
    return _Server((config.host, config.port), handler)
