"""BLE transport, for sensors reached over Bluetooth Low Energy.

WitMotion's BLE 5.0 devices expose a single vendor service carrying one
notify characteristic (frames out) and one write characteristic (commands
in). Their SDKs spell the UUIDs with a **non-standard** Bluetooth base
ending ``00805f9a34fb`` - note the ``9a``; the assigned base is ``9b``. That
is consistent across their Python, C# and Unity code, so it is what the
firmware really advertises, but stacks differ in how strictly they match.

Rather than bet on one spelling, this module prefers the documented UUIDs and
falls back to discovering the service by its 16-bit short form and picking
whichever characteristics actually carry the ``notify`` and ``write``
properties.

``bleak`` is an optional dependency: install the ``ble`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass

from . import modes
from .acquisition import Acquisition, PollPlan, SampleHandler, SourceStatus

#: Short (16-bit) forms, which is what the device really registers.
SERVICE_SHORT = "ffe5"
NOTIFY_SHORT = "ffe4"
WRITE_SHORT = "ffe9"

#: Full UUIDs as WitMotion's SDKs write them, plus the assigned-base variants.
_BASES = ("00805f9a34fb", "00805f9b34fb")
SERVICE_UUIDS = tuple(f"0000{SERVICE_SHORT}-0000-1000-8000-{base}" for base in _BASES)
NOTIFY_UUIDS = tuple(f"0000{NOTIFY_SHORT}-0000-1000-8000-{base}" for base in _BASES)
WRITE_UUIDS = tuple(f"0000{WRITE_SHORT}-0000-1000-8000-{base}" for base in _BASES)

DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_SCAN_SECONDS = 8.0
#: How long to look for the sensor's advertisement before giving up on a
#: connection attempt.
DEFAULT_FIND_TIMEOUT = 12.0
#: Advertised names seen on WitMotion vibration sensors. "wtsensor" is what a
#: physical WTVB01-BT50 actually broadcasts ("WTSensor-01").
NAME_HINTS = ("wtvb", "bt50", "wt901", "wtsensor", "witmotion")

_STOP_POLL_GRACE = 0.5

#: Serialises discovery across every source in the process. A BlueZ adapter
#: runs one discovery session at a time: when two sensors scan at once each
#: sees an empty result and the connection that would have worked times out
#: instead. Observed with two sensors on a Pi Zero 2 W - one connected, the
#: other never did.
_SCAN_LOCK = threading.Lock()


@contextlib.asynccontextmanager
async def exclusive_scan():
    """Hold the adapter's discovery session for the duration of a scan."""
    loop = asyncio.get_running_loop()
    # Acquire off the event loop so waiting does not block this source's
    # notifications while another source is scanning.
    await loop.run_in_executor(None, _SCAN_LOCK.acquire)
    try:
        yield
    finally:
        _SCAN_LOCK.release()
#: How long stop() waits for the session to disconnect before forcing the
#: event loop down. A leaked LE link is worse than a slow shutdown: the sensor
#: stops advertising while connected, so nothing can find it again.
_SHUTDOWN_TIMEOUT = 8.0
#: Slack added to find + connect before start() gives up. With several sensors
#: the scans are serialised, so a source can legitimately spend a while just
#: waiting its turn for the adapter; timing out mid-connect would throw away a
#: connection that was about to succeed.
_START_MARGIN = 20.0


class BleUnavailable(RuntimeError):
    """``bleak`` is not installed, or the platform refuses BLE access."""


class BleNotFound(BleUnavailable):
    """The sensor is not advertising: powered off, out of range, or already
    connected to something else."""


def _import_bleak():
    try:
        import bleak
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise BleUnavailable(
            "bleak is not installed; run: pip install 'wtvb01[ble]'"
        ) from exc
    return bleak


def _short(uuid: str) -> str:
    """The 16-bit form of a UUID, for base-insensitive comparison."""
    text = str(uuid).lower()
    return text[4:8] if len(text) == 36 and text.startswith("0000") else text


@dataclass(frozen=True)
class BleConfig:
    address: str
    mode: str = modes.DEFAULT_MODE
    poll_interval: float | None = None
    output_length: int | None = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    #: Seconds spent looking for the advertisement before each connect.
    find_timeout: float = DEFAULT_FIND_TIMEOUT


@dataclass(frozen=True)
class BleDevice:
    """A sensor seen while scanning."""

    address: str
    name: str
    rssi: int
    likely: bool

    def as_dict(self) -> dict[str, object]:
        return {"address": self.address, "name": self.name, "rssi": self.rssi, "likely": self.likely}


async def scan(seconds: float = DEFAULT_SCAN_SECONDS) -> tuple[BleDevice, ...]:
    """Discover nearby BLE devices, likely WitMotion sensors first."""
    bleak = _import_bleak()
    async with exclusive_scan():
        discovered = await bleak.BleakScanner.discover(timeout=seconds, return_adv=True)
    devices = []
    for address, (device, adv) in discovered.items():
        name = device.name or adv.local_name or ""
        services = {_short(uuid) for uuid in (adv.service_uuids or ())}
        likely = SERVICE_SHORT in services or any(hint in name.lower() for hint in NAME_HINTS)
        devices.append(BleDevice(address, name, adv.rssi or 0, likely))
    return tuple(sorted(devices, key=lambda d: (not d.likely, -d.rssi)))


class BleSource:
    """Reads frames over BLE, with the same surface as ``SerialSource``.

    An asyncio event loop runs on its own thread so callers stay synchronous;
    :meth:`start` blocks until the link is up or the connect timeout expires.
    """

    def __init__(self, config: BleConfig, on_sample: SampleHandler) -> None:
        self._config = config
        self._acquisition = Acquisition(
            on_sample,
            mode=config.mode,
            poll_interval=config.poll_interval,
            output_length=config.output_length,
            target=config.address,
            transport="ble",
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._connect_error: Exception | None = None
        self._client = None
        self._write_uuid: str | None = None
        self._write_response = False

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Connect and begin receiving. Raises if the link cannot be made."""
        if self._thread is not None:
            raise RuntimeError("source already started")
        _import_bleak()  # fail fast and clearly, before spawning anything
        self._ready.clear()
        self._stopping.clear()
        self._connect_error = None
        self._thread = threading.Thread(target=self._run, name="wtvb01-ble", daemon=True)
        self._thread.start()
        budget = self._config.find_timeout + self._config.connect_timeout + _START_MARGIN
        if not self._ready.wait(budget):
            self.stop()
            raise TimeoutError(
                f"BLE connect to {self._config.address} timed out after {budget:g}s"
            )
        if self._connect_error is not None:
            error = self._connect_error
            self.stop()
            raise error

    def stop(self) -> None:
        """Disconnect cleanly, then bring the event loop down.

        Order matters. Stopping the loop first would abandon the GATT link
        half-open: BlueZ keeps it, the sensor stays "connected" and stops
        advertising, and no later scan can find it again.
        """
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=_SHUTDOWN_TIMEOUT)
            if thread.is_alive():
                loop = self._loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=3.0)
        self._loop = None
        self._acquisition.update(connected=False, rate_hz=0.0, poll_rate_hz=0.0)

    # --- state ---------------------------------------------------------------

    @property
    def status(self) -> SourceStatus:
        return self._acquisition.status

    @property
    def config(self) -> BleConfig:
        return self._config

    @property
    def plan(self) -> PollPlan:
        return self._acquisition.plan

    def set_mode(self, mode_key: str | None, interval: float | None = None) -> PollPlan:
        return self._acquisition.set_mode(mode_key, interval)

    def send(self, command: bytes) -> None:
        """Queue a command; written from the BLE thread."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._write(command), loop)

    # --- internals -----------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._session())
        except Exception as exc:  # noqa: BLE001 - reported through status/start
            self._connect_error = exc
            self._acquisition.update(connected=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            self._ready.set()
            try:
                loop.close()
            finally:
                self._loop = None

    async def _find(self, bleak):
        """What to hand :class:`BleakClient`: a discovered device, or the address.

        BlueZ cannot connect to an address it has never seen advertise, so a
        bare ``BleakClient(address)`` fails with "device not found" on a
        freshly started service even when the sensor is sitting right there.
        Scanning first fixes that, and makes reconnection work after the
        sensor drops out.

        The address is still worth trying when the scan comes up empty: a
        sensor that BlueZ already holds a link to does not advertise, and
        connecting by address is what recovers from that.
        """
        async with exclusive_scan():
            device = await bleak.BleakScanner.find_device_by_address(
                self._config.address, timeout=self._config.find_timeout
            )
        return device if device is not None else self._config.address

    async def _session(self) -> None:
        bleak = _import_bleak()
        target = await self._find(bleak)
        # Without this callback an unexpected drop is invisible: the client
        # goes quiet but nothing marks the link down, so a supervisor keeps
        # believing it is connected and never reconnects. In a polling mode a
        # failed write eventually surfaces it; in stream mode nothing would.
        client = bleak.BleakClient(
            target,
            timeout=self._config.connect_timeout,
            disconnected_callback=self._on_disconnect,
        )
        try:
            try:
                await client.connect()
            except Exception as exc:  # noqa: BLE001 - reported with context
                raise BleNotFound(
                    f"cannot reach {self._config.address}: {exc}. Check it is powered on, "
                    f"in range, and not already connected elsewhere"
                ) from exc
            self._client = client
            notify_uuid, self._write_uuid, self._write_response = self._resolve_characteristics(client)
            await client.start_notify(notify_uuid, self._on_notify)
            self._acquisition.update(connected=True, error="")
            self._ready.set()
            await self._poll_forever()
        finally:
            self._client = None
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                pass

    def _resolve_characteristics(self, client) -> tuple[str, str | None, bool]:
        """Find the notify and write characteristics, by UUID or by property."""
        notify_uuid = write_uuid = None
        write_response = True
        for service in client.services:
            if _short(service.uuid) != SERVICE_SHORT:
                continue
            for characteristic in service.characteristics:
                short = _short(characteristic.uuid)
                properties = set(characteristic.properties)
                if short == NOTIFY_SHORT or (notify_uuid is None and "notify" in properties):
                    notify_uuid = characteristic.uuid
                if short == WRITE_SHORT or (
                    write_uuid is None and properties & {"write", "write-without-response"}
                ):
                    write_uuid = characteristic.uuid
                    write_response = "write-without-response" not in properties
        if notify_uuid is None:
            available = ", ".join(str(s.uuid) for s in client.services) or "none"
            raise BleUnavailable(
                f"no {SERVICE_SHORT}/{NOTIFY_SHORT} notify characteristic on "
                f"{self._config.address}; services seen: {available}"
            )
        return notify_uuid, write_uuid, write_response

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        self._acquisition.consume(bytes(data))

    def _on_disconnect(self, _client) -> None:
        """The peer went away: out of range, powered off, or reset."""
        self._acquisition.update(
            connected=False, error="peer disconnected", rate_hz=0.0, poll_rate_hz=0.0
        )

    async def _write(self, command: bytes) -> None:
        client, uuid = self._client, self._write_uuid
        if client is None or uuid is None:
            return
        try:
            await client.write_gatt_char(uuid, command, response=self._write_response)
        except Exception as exc:  # noqa: BLE001 - a failed poll is not fatal
            self._acquisition.update(error=f"write failed: {exc}")

    async def _poll_forever(self) -> None:
        """Issue register reads on the schedule the capture mode asks for."""
        while not self._stopping.is_set():
            if not self._acquisition.status.connected:
                return  # the peer dropped; let the supervisor reconnect
            plan = self._acquisition.plan
            if not plan.active:
                await asyncio.sleep(_STOP_POLL_GRACE)
                continue
            command = self._acquisition.next_poll_command(plan)
            if command is not None:
                await self._write(command)
            await asyncio.sleep(plan.interval)
