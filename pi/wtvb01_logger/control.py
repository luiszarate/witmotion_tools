"""Control channel: a Unix socket the service listens on.

This is what makes ``ssh pi 'wtvb01-logger roll'`` work. A Unix socket rather
than a TCP port because control should never leave the machine, and file
permissions are the access check.

The protocol is one JSON object per line, request and response:

    -> {"command": "roll", "sensor": "rotor-izq"}
    <- {"ok": true, "rolled": ["rotor-izq"]}
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable

#: Owner and group may talk to the service; nobody else.
SOCKET_MODE = 0o660
_MAX_REQUEST = 64 * 1024
_TIMEOUT = 10.0

CommandHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ControlError(RuntimeError):
    """The control channel could not be reached or refused the request."""


class _Handler(socketserver.StreamRequestHandler):
    timeout = _TIMEOUT

    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_REQUEST)
        if not raw:
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            self._reply({"ok": False, "error": f"bad request: {exc}"})
            return
        try:
            response = self.server.command_handler(request)
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the service
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._reply(response)

    def _reply(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload, default=str).encode("utf-8") + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, handler_class, command_handler: CommandHandler) -> None:
        self.command_handler = command_handler
        super().__init__(path, handler_class)

    def handle_error(self, request, client_address) -> None:
        """A client that hangs up mid-request is routine, not an incident."""
        import sys

        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class ControlServer:
    """Serves control commands on a Unix socket until stopped."""

    def __init__(self, path: Path | str, handler: CommandHandler) -> None:
        self._path = Path(path)
        self._handler = handler
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A socket left behind by a crash would block bind(); it is safe to
        # remove because bind() below would fail if one were really in use.
        self._unlink_stale()
        self._server = _Server(str(self._path), _Handler, self._handler)
        os.chmod(self._path, SOCKET_MODE)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="wtvb01-control", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self._path.unlink(missing_ok=True)

    def _unlink_stale(self) -> None:
        if not self._path.exists():
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self._path))
        except OSError:
            self._path.unlink(missing_ok=True)  # nobody home; it is a leftover
            return
        finally:
            probe.close()
        raise ControlError(f"another logger is already listening on {self._path}")


def request(path: Path | str, payload: dict[str, Any], timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Send one command to a running service and return its reply."""
    path = Path(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
    except FileNotFoundError as exc:
        raise ControlError(f"no service listening on {path}; is wtvb01-logger running?") from exc
    except PermissionError as exc:
        raise ControlError(f"permission denied on {path}; try sudo, or move control_socket") from exc
    except OSError as exc:
        raise ControlError(f"cannot reach {path}: {exc}") from exc
    try:
        client.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        with client.makefile("rb") as stream:
            raw = stream.readline(_MAX_REQUEST)
    except OSError as exc:
        raise ControlError(f"control request failed: {exc}") from exc
    finally:
        client.close()
    if not raw:
        raise ControlError("service closed the connection without replying")
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise ControlError(f"unreadable reply: {exc}") from exc
    if not isinstance(reply, dict):
        raise ControlError("unreadable reply: expected a JSON object")
    return reply
