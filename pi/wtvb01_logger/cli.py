"""Command line: run the service, or drive a running one over SSH."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from wtvb01 import modes
from wtvb01.ports import available_ports

from . import config as config_module
from .control import ControlError, request
from .service import LoggerService

CONTROL_COMMANDS = ("status", "roll", "pause", "resume", "connect", "disconnect", "stop", "ping")

#: What usually goes wrong with BLE on a fresh Raspberry Pi, in the order it
#: is worth checking. The adapter ships soft-blocked on some images.
_BLE_HINTS = """
Comprobaciones habituales:
  sudo rfkill unblock bluetooth     # el adaptador suele venir bloqueado
  sudo hciconfig hci0 up            # y apagado
  systemctl is-active bluetooth     # BlueZ tiene que estar corriendo
  hciconfig hci0                    # debe decir UP RUNNING
""".rstrip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wtvb01-logger", description=__doc__)
    parser.add_argument(
        "-c", "--config", default=str(config_module.DEFAULT_CONFIG_PATH),
        help="path to the TOML configuration file",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run the logger in the foreground (what systemd starts)")
    sub.add_parser("validate", help="check the configuration file and print what it resolves to")
    sub.add_parser("ports", help="list candidate serial ports")

    scan = sub.add_parser("ble-scan", help="discover nearby BLE sensors and their addresses")
    scan.add_argument("-s", "--seconds", type=float, default=8.0)

    for name in CONTROL_COMMANDS:
        control = sub.add_parser(name, help=f"send '{name}' to the running service")
        control.add_argument("--socket", default=None, help="control socket path")
        control.add_argument("--json", action="store_true", help="print the raw reply")
        if name in ("roll", "pause", "resume", "connect", "disconnect", "status"):
            control.add_argument("--sensor", default=None, help="act on one sensor only")
    return parser


# --- helpers -----------------------------------------------------------------


def _load(args: argparse.Namespace) -> config_module.LoggerConfig:
    return config_module.load(args.config)


def _socket_path(args: argparse.Namespace) -> Path:
    if getattr(args, "socket", None):
        return Path(args.socket)
    try:
        return _load(args).control_socket
    except config_module.ConfigError:
        # Controlling a running service should not require a readable config.
        return config_module.DEFAULT_CONTROL_SOCKET


def _print_status(payload: dict[str, Any]) -> None:
    print(
        f"activo {payload.get('uptime_seconds', 0):.0f} s · "
        f"salida {payload.get('output_dir')} · "
        f"rotación {payload.get('rotate_minutes')} min"
    )
    for sensor in payload.get("sensors", []):
        source, sink = sensor.get("source", {}), sensor.get("sink", {})
        if not sensor.get("running"):
            state = "SUELTO (sin conectar)"
        elif sensor.get("connected"):
            state = "conectado"
        else:
            state = "SIN CONEXIÓN"
        print(f"\n  {sensor['sensor']}  [{sensor['transport']} {sensor['target']}]  {state}")
        if sensor.get("error"):
            print(f"    error      {sensor['error']}")
        if source:
            print(
                f"    enlace     {source.get('layout') or '—'} · "
                f"{source.get('rate_hz', 0)} Hz trama · "
                f"{source.get('poll_rate_hz', 0)} Hz sondeo · modo {source.get('mode')}"
            )
        paused = "  (PAUSADO)" if sink.get("paused") else ""
        print(f"    archivo    {sink.get('path') or '—'}{paused}")
        print(
            f"    filas      {sink.get('rows_in_file', 0)} en archivo · "
            f"{sink.get('rows_total', 0)} total · "
            f"{sink.get('files', 0)} archivos · {sink.get('dropped', 0)} descartadas"
        )
        overflow = sensor.get("overflow", 0)
        if sensor.get("queued") or overflow:
            warning = f"  ¡{overflow} perdidas por saturación!" if overflow else ""
            print(f"    cola       {sensor.get('queued', 0)} en espera{warning}")


# --- commands ----------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    settings = _load(args)
    print(f"wtvb01-logger: {len(settings.enabled_sensors)} sensor(es) → {settings.output_dir}")
    return LoggerService(settings).run_forever()


def cmd_validate(args: argparse.Namespace) -> int:
    settings = _load(args)
    print(f"configuración válida: {args.config}")
    print(f"  salida          {settings.output_dir}")
    print(f"  rotación        {settings.rotate_minutes} min")
    print(f"  socket control  {settings.control_socket}")
    for sensor in settings.sensors:
        mode = modes.get(sensor.mode)
        interval = mode.interval_for(sensor.poll_interval)
        rate = f"{1 / interval:.1f} Hz" if interval else "sin sondeo"
        state = "" if sensor.enabled else "  (deshabilitado)"
        print(
            f"  · {sensor.name:<16} {sensor.transport:<7} {sensor.target:<24} "
            f"modo {sensor.mode} · sondeo {rate}{state}"
        )
    return 0


def cmd_ports(_args: argparse.Namespace) -> int:
    ports = available_ports()
    if not ports:
        print("no se encontraron puertos serie")
        return 1
    for port in ports:
        mark = "*" if port.likely else " "
        print(f"{mark} {port.device:24s} {port.description}  [{port.hwid}]")
    print("\n* = parece un adaptador USB-serie")
    return 0


def cmd_ble_scan(args: argparse.Namespace) -> int:
    from wtvb01 import ble  # lazy: bleak is an optional dependency

    try:
        devices = asyncio.run(ble.scan(args.seconds))
    except ble.BleUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - BlueZ failures are the norm, not a crash
        print(f"error: no se pudo escanear: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(_BLE_HINTS, file=sys.stderr)
        return 2
    if not devices:
        print("no se encontró ningún dispositivo BLE")
        return 1
    for device in devices:
        mark = "*" if device.likely else " "
        print(f"{mark} {device.address:20s} rssi={device.rssi:4d}  {device.name or '(sin nombre)'}")
    print("\n* = parece un sensor WitMotion; copia la dirección al archivo de configuración")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"command": args.command}
    if getattr(args, "sensor", None):
        payload["sensor"] = args.sensor
    try:
        reply = request(_socket_path(args), payload)
    except ControlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json or not reply.get("ok"):
        print(json.dumps(reply, indent=2, ensure_ascii=False))
        return 0 if reply.get("ok") else 1
    if args.command == "status":
        _print_status(reply)
    elif args.command == "ping":
        print("el servicio responde")
    else:
        acted = (reply.get("rolled") or reply.get("paused") or reply.get("resumed")
                 or reply.get("connected") or reply.get("disconnected") or [])
        print(f"{args.command}: {', '.join(acted) if acted else 'ok'}")
    return 0


_COMMANDS = {
    "run": cmd_run,
    "validate": cmd_validate,
    "ports": cmd_ports,
    "ble-scan": cmd_ble_scan,
    **{name: cmd_control for name in CONTROL_COMMANDS},
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if args.command is None:
        parser.print_help()
        return 1
    try:
        return _COMMANDS[args.command](args)
    except config_module.ConfigError as exc:
        print(f"error de configuración: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
