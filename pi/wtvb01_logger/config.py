"""Configuration file for the logger service.

TOML, because it is unambiguous, takes comments, and ``tomllib`` is in the
standard library from Python 3.11 (which is what Raspberry Pi OS Bookworm
ships). On 3.10 the third-party ``tomli`` is used if it is installed.

Everything is validated on load and turned into frozen dataclasses, so the
service never carries a half-checked dict around. Unknown keys are an error,
not a shrug: a typo in a config file on a headless logger is otherwise
invisible until the data is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from wtvb01 import modes

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on 3.10 and older
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

DEFAULT_CONFIG_PATH = Path("/etc/wtvb01-logger/config.toml")
DEFAULT_OUTPUT_DIR = Path("/var/lib/wtvb01-logger")
DEFAULT_CONTROL_SOCKET = Path("/run/wtvb01-logger/control.sock")
DEFAULT_BAUDRATE = 115200
DEFAULT_ROTATE_MINUTES = 15.0
DEFAULT_FLUSH_SECONDS = 2.0
DEFAULT_RECONNECT_SECONDS = 5.0
#: Seconds without a single frame before a link that still claims to be
#: connected is treated as dead and rebuilt.
DEFAULT_STALL_SECONDS = 20.0

TRANSPORTS = ("serial", "ble")

_LOGGER_KEYS = {"output_dir", "rotate_minutes", "flush_seconds", "control_socket",
                "connect_on_start"}
_DEFAULTS_KEYS = {"mode", "poll_interval", "baudrate", "reconnect_seconds", "max_rate_hz",
                  "stall_seconds"}
_SENSOR_KEYS = {
    "name", "transport", "port", "address", "baudrate", "mode",
    "poll_interval", "reconnect_seconds", "stall_seconds", "max_rate_hz",
    "output_length", "enabled",
}


class ConfigError(ValueError):
    """A configuration file that cannot be used, with a reason a human can act on."""


def _reject_unknown(section: str, table: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(
            f"[{section}]: unknown key(s) {', '.join(unknown)}; "
            f"expected any of {', '.join(sorted(allowed))}"
        )


def _number(section: str, table: Mapping[str, Any], key: str, fallback: float,
            minimum: float | None = None) -> float:
    value = table.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[{section}] {key}: expected a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"[{section}] {key}: must be at least {minimum}, got {value}")
    return float(value)


def _path(value: Any, fallback: Path) -> Path:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise ConfigError(f"expected a path string, got {value!r}")
    return Path(os.path.expanduser(value)).resolve()


@dataclass(frozen=True)
class SensorConfig:
    """One sensor the service should log."""

    name: str
    transport: str
    #: Serial device node, for ``transport = "serial"``.
    port: str = ""
    #: BLE address (or, on some stacks, a UUID), for ``transport = "ble"``.
    address: str = ""
    baudrate: int = DEFAULT_BAUDRATE
    mode: str = modes.DEFAULT_MODE
    poll_interval: float | None = None
    reconnect_seconds: float = DEFAULT_RECONNECT_SECONDS
    #: Rebuild a link that has not delivered a frame in this long, even if it
    #: still reports itself connected. 0 disables the watchdog.
    stall_seconds: float = DEFAULT_STALL_SECONDS
    #: Cap on rows written per second. 0 logs every sample. At 100 Hz one
    #: sensor writes roughly 90 MB per hour, which is worth bounding on an
    #: SD card.
    max_rate_hz: float = 0.0
    output_length: int | None = None
    enabled: bool = True

    @property
    def target(self) -> str:
        """What this sensor connects to, for logs and status output."""
        return self.port if self.transport == "serial" else self.address


@dataclass(frozen=True)
class LoggerConfig:
    """The whole service configuration."""

    output_dir: Path = DEFAULT_OUTPUT_DIR
    rotate_minutes: float = DEFAULT_ROTATE_MINUTES
    flush_seconds: float = DEFAULT_FLUSH_SECONDS
    control_socket: Path = DEFAULT_CONTROL_SOCKET
    #: Whether sensors are connected as soon as the service starts. The
    #: default is False: the service comes up idle, answering control commands
    #: but touching no radio and no serial port until 'wtvb01-logger connect'.
    #: That keeps the sensors free for other uses. Set it to True for
    #: unattended operation, where the logger must record after a power cut
    #: with nobody there to start it.
    connect_on_start: bool = False
    sensors: tuple[SensorConfig, ...] = field(default_factory=tuple)

    @property
    def rotate_seconds(self) -> float:
        return self.rotate_minutes * 60.0

    @property
    def enabled_sensors(self) -> tuple[SensorConfig, ...]:
        return tuple(sensor for sensor in self.sensors if sensor.enabled)


def _parse_sensor(index: int, raw: Any, defaults: Mapping[str, Any]) -> SensorConfig:
    where = f"sensors[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table, got {raw!r}")
    _reject_unknown(where, raw, _SENSOR_KEYS)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: 'name' is required and must be a non-empty string")
    name = name.strip()
    if any(character in name for character in "/\\ "):
        raise ConfigError(f"{where}: 'name' becomes a filename, so it cannot contain spaces or slashes: {name!r}")

    transport = raw.get("transport")
    if transport not in TRANSPORTS:
        raise ConfigError(f"{where}: 'transport' must be one of {', '.join(TRANSPORTS)}, got {transport!r}")

    port = raw.get("port", "")
    address = raw.get("address", "")
    if transport == "serial" and not port:
        raise ConfigError(f"{where}: a serial sensor needs 'port', e.g. port = \"/dev/ttyUSB0\"")
    if transport == "ble" and not address:
        raise ConfigError(f"{where}: a BLE sensor needs 'address', e.g. address = \"AA:BB:CC:DD:EE:FF\"")

    mode = raw.get("mode", defaults.get("mode", modes.DEFAULT_MODE))
    try:
        mode = modes.get(mode).key
    except KeyError as exc:
        raise ConfigError(f"{where}: {exc}") from exc

    poll_interval = raw.get("poll_interval", defaults.get("poll_interval"))
    if poll_interval is not None:
        poll_interval = _number(where, {"poll_interval": poll_interval}, "poll_interval", 0.0, minimum=0.0)

    output_length = raw.get("output_length")
    if output_length is not None and not isinstance(output_length, int):
        raise ConfigError(f"{where}: 'output_length' must be an integer number of bytes")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: 'enabled' must be true or false")

    return SensorConfig(
        name=name,
        transport=transport,
        port=str(port),
        address=str(address),
        baudrate=int(_number(where, raw, "baudrate", defaults.get("baudrate", DEFAULT_BAUDRATE), minimum=1)),
        mode=mode,
        poll_interval=poll_interval,
        reconnect_seconds=_number(
            where, raw, "reconnect_seconds",
            defaults.get("reconnect_seconds", DEFAULT_RECONNECT_SECONDS), minimum=0.1,
        ),
        stall_seconds=_number(
            where, raw, "stall_seconds",
            defaults.get("stall_seconds", DEFAULT_STALL_SECONDS), minimum=0.0,
        ),
        max_rate_hz=_number(where, raw, "max_rate_hz", defaults.get("max_rate_hz", 0.0), minimum=0.0),
        output_length=output_length,
        enabled=enabled,
    )


def parse(document: Mapping[str, Any]) -> LoggerConfig:
    """Validate an already-parsed TOML document."""
    _reject_unknown("<root>", document, {"logger", "defaults", "sensors"})

    logger = document.get("logger", {})
    if not isinstance(logger, dict):
        raise ConfigError("[logger]: expected a table")
    _reject_unknown("logger", logger, _LOGGER_KEYS)

    defaults = document.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("[defaults]: expected a table")
    _reject_unknown("defaults", defaults, _DEFAULTS_KEYS)

    raw_sensors = document.get("sensors", [])
    if not isinstance(raw_sensors, list):
        raise ConfigError("[[sensors]]: expected a list of tables")
    sensors = tuple(_parse_sensor(i, raw, defaults) for i, raw in enumerate(raw_sensors))

    names = [sensor.name for sensor in sensors]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"duplicate sensor name(s): {', '.join(duplicates)}")
    if not sensors:
        raise ConfigError("no sensors declared; add at least one [[sensors]] table")

    connect_on_start = logger.get("connect_on_start", False)
    if not isinstance(connect_on_start, bool):
        raise ConfigError("[logger] connect_on_start: must be true or false")

    return LoggerConfig(
        output_dir=_path(logger.get("output_dir"), DEFAULT_OUTPUT_DIR),
        rotate_minutes=_number("logger", logger, "rotate_minutes", DEFAULT_ROTATE_MINUTES, minimum=0.1),
        flush_seconds=_number("logger", logger, "flush_seconds", DEFAULT_FLUSH_SECONDS, minimum=0.0),
        control_socket=_path(logger.get("control_socket"), DEFAULT_CONTROL_SOCKET),
        connect_on_start=connect_on_start,
        sensors=sensors,
    )


def load(path: Path | str) -> LoggerConfig:
    """Read and validate a config file."""
    if tomllib is None:  # pragma: no cover
        raise ConfigError("TOML support is missing; use Python 3.11+ or install tomli")
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(document)
