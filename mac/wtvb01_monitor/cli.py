"""Command line entry point: ``python3 -m wtvb01 [serve|monitor|record|ports]``."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from wtvb01 import modes
from .app import DEFAULT_RECORD_DIR, Session, SessionError
from wtvb01.ports import available_ports, default_port
from .server import DEFAULT_HOST, DEFAULT_PORT, ServerConfig, build_server
from wtvb01.source import DEFAULT_BAUDRATE

_MONITOR_INTERVAL = 0.25


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-p", "--port", help="serial port (default: first likely one)")
    parser.add_argument("-b", "--baudrate", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument(
        "-m",
        "--mode",
        choices=tuple(modes.MODES),
        default=modes.DEFAULT_MODE,
        help="capture mode: which register blocks to read back and how often",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="seconds between register read-backs; omit for the mode's default, 0 to disable",
    )
    parser.add_argument(
        "--frame-length",
        type=int,
        default=None,
        help="fixed 0x61 frame length (28/32/40); omit to auto-detect",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wtvb01", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the web visualiser (default)")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--http-port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--connect", action="store_true", help="connect on start-up")
    _add_connection_args(serve)

    monitor = sub.add_parser("monitor", help="print live readings to the terminal")
    _add_connection_args(monitor)

    record = sub.add_parser("record", help="log to CSV without a UI")
    record.add_argument("-o", "--output-dir", default=str(DEFAULT_RECORD_DIR))
    record.add_argument("-d", "--duration", type=float, default=0.0, help="seconds; 0 = until Ctrl-C")
    _add_connection_args(record)

    sub.add_parser("ports", help="list candidate serial ports")

    sub.add_parser("modes", help="describe the capture modes")
    return parser


def cmd_modes(_args: argparse.Namespace) -> int:
    for mode in modes.MODES.values():
        blocks = ", ".join(f"0x{block:02X}" for block in mode.blocks) or "-"
        default = f"{mode.default_interval:g}s" if mode.blocks else "sin sondeo"
        print(f"{mode.key:<11} {mode.label:<16} bloques {blocks:<12} cada {default}")
        print(f"{'':<11} {mode.description}\n")
    return 0


def _resolve_port(requested: str | None) -> str:
    port = requested or default_port()
    if not port:
        raise SessionError("no serial port found; plug the sensor in over USB-C")
    return port


def _connect(session: Session, args: argparse.Namespace) -> str:
    port = _resolve_port(getattr(args, "port", None))
    session.connect(port, args.baudrate, args.mode, args.poll_interval, args.frame_length)
    return port


def cmd_ports(_args: argparse.Namespace) -> int:
    ports = available_ports()
    if not ports:
        print("No serial ports found.")
        return 1
    for port in ports:
        mark = "*" if port.likely else " "
        print(f"{mark} {port.device:28s} {port.description}  [{port.hwid}]")
    print("\n* = looks like a USB-serial adapter")
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    session = Session()
    port = _connect(session, args)
    print(f"Reading {port} at {args.baudrate} baud in '{args.mode}' mode. Ctrl-C to stop.\n")
    try:
        while True:
            time.sleep(_MONITOR_INTERVAL)
            sample = session.hub.latest
            status = session.status()["source"]
            if sample is None:
                print("\rwaiting for frames…", end="", flush=True)
                continue
            velocity, displacement, frequency = sample.velocity, sample.displacement, sample.frequency
            line = (
                f"v {velocity.x:6.1f} {velocity.y:6.1f} {velocity.z:6.1f} mm/s | "
                f"d {displacement.x:6.0f} {displacement.y:6.0f} {displacement.z:6.0f} um | "
                f"f {frequency.x:5.1f} {frequency.y:5.1f} {frequency.z:5.1f} Hz | "
                f"{sample.temperature:5.2f} C | {status['rate_hz']:6.2f} Hz {status['layout']}"
                f" | poll {status['poll_rate_hz']:5.1f} Hz"
            )
            print(f"\r{line}", end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        session.close()
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    session = Session()
    port = _connect(session, args)
    path = session.start_recording(Path(args.output_dir))
    limit = args.duration if args.duration > 0 else float("inf")
    print(f"Reading {port} -> {path}")
    print("Ctrl-C to stop." if limit == float("inf") else f"Recording for {limit:g} s.")
    started = time.monotonic()
    try:
        while time.monotonic() - started < limit:
            time.sleep(0.5)
            rows = session.status()["recording"]["rows"]
            print(f"\r{rows} rows", end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        session.close()
    print(f"Saved {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    session = Session()
    if args.connect:
        print(f"Connected to {_connect(session, args)}")
    config = ServerConfig(args.host, args.http_port)
    server = build_server(session, config)
    thread = threading.Thread(target=server.serve_forever, name="wtvb01-http", daemon=True)
    thread.start()
    print(f"WTVB01-BT50 visualiser at {config.url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(config.url)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        print("\nShutting down…")
        server.shutdown()
        session.close()
    return 0


_COMMANDS = {
    "serve": cmd_serve,
    "monitor": cmd_monitor,
    "record": cmd_record,
    "ports": cmd_ports,
    "modes": cmd_modes,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command is None:
        args = parser.parse_args(["serve", *(argv if argv is not None else sys.argv[1:])])
    try:
        return _COMMANDS[args.command](args)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
