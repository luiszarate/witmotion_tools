"""Turning a byte stream into samples, independent of how the bytes arrive.

Serial and BLE deliver the same frames; only the plumbing differs. Everything
between "here are some bytes" and "here is a sample" lives here, so the
transports stay thin and the decoding rules have one home.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import modes, protocol
from .detect import OutputLengthDetector
from .model import Accumulator, Sample

DEFAULT_BAUDRATE = 115200
RATE_WINDOW = 2.0

SampleHandler = Callable[[Sample], None]


@dataclass(frozen=True)
class PollPlan:
    """What to request and how often, resolved from a mode and an override."""

    #: The round-robin sequence of blocks; repeats give a block more budget.
    cycle: tuple[int, ...] = ()
    interval: float = 0.0
    mode: str = modes.DEFAULT_MODE

    @property
    def active(self) -> bool:
        return bool(self.cycle) and self.interval > 0

    @classmethod
    def resolve(cls, mode_key: str | None, interval: float | None) -> "PollPlan":
        mode = modes.get(mode_key)
        return cls(mode.cycle, mode.interval_for(interval), mode.key)


@dataclass(frozen=True)
class SourceStatus:
    connected: bool = False
    #: What the transport is attached to: a device node, or a BLE address.
    port: str = ""
    transport: str = "serial"
    error: str = ""
    frames: int = 0
    bytes_read: int = 0
    dropped_bytes: int = 0
    output_length: int | None = None
    layout: str = ""
    rate_hz: float = 0.0
    mode: str = modes.DEFAULT_MODE
    poll_interval: float = 0.0
    poll_rate_hz: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "port": self.port,
            "transport": self.transport,
            "error": self.error,
            "frames": self.frames,
            "bytes_read": self.bytes_read,
            "dropped_bytes": self.dropped_bytes,
            "output_length": self.output_length,
            "layout": self.layout,
            "rate_hz": round(self.rate_hz, 2),
            "mode": self.mode,
            "poll_interval": round(self.poll_interval, 4),
            "poll_rate_hz": round(self.poll_rate_hz, 2),
        }


def windowed_rate(times: list[float], now: float) -> float:
    """Events per second over the trailing :data:`RATE_WINDOW` seconds."""
    times.append(now)
    cutoff = now - RATE_WINDOW
    while times and times[0] < cutoff:
        times.pop(0)
    span = times[-1] - times[0] if len(times) > 1 else 0.0
    return (len(times) - 1) / span if span > 0 else 0.0


class Acquisition:
    """Frame decoding, register accumulation, poll scheduling and status.

    A transport feeds it bytes with :meth:`consume` and asks it what to send
    with :meth:`next_poll_command`; everything else is bookkeeping.
    """

    def __init__(
        self,
        on_sample: SampleHandler,
        mode: str | None = None,
        poll_interval: float | None = None,
        output_length: int | None = None,
        target: str = "",
        transport: str = "serial",
    ) -> None:
        self._on_sample = on_sample
        self._parser = protocol.FrameParser(output_length)
        self._detector = OutputLengthDetector()
        self._accumulator = Accumulator()
        self._lock = threading.Lock()
        self._plan = PollPlan.resolve(mode, poll_interval)
        self._status = SourceStatus(
            port=target,
            transport=transport,
            output_length=output_length,
            mode=self._plan.mode,
            poll_interval=self._plan.interval,
        )
        self._frame_times: list[float] = []
        self._poll_times: list[float] = []
        self._poll_index = 0

    # --- state ---------------------------------------------------------------

    @property
    def status(self) -> SourceStatus:
        with self._lock:
            return self._status

    @property
    def plan(self) -> PollPlan:
        with self._lock:
            return self._plan

    def update(self, **changes: object) -> None:
        with self._lock:
            self._status = SourceStatus(**{**self._status.__dict__, **changes})

    def set_mode(self, mode_key: str | None, interval: float | None = None) -> PollPlan:
        """Switch capture mode. Raises ``KeyError`` if the mode is unknown."""
        plan = PollPlan.resolve(mode_key, interval)
        with self._lock:
            self._plan = plan
            self._poll_index = 0
            self._poll_times.clear()
        self.update(mode=plan.mode, poll_interval=plan.interval, poll_rate_hz=0.0)
        return plan

    # --- polling -------------------------------------------------------------

    def next_poll_command(self, plan: PollPlan | None = None) -> bytes | None:
        """The next register-read command, or ``None`` if this mode polls nothing.

        The sensor answers only the first read command of a back-to-back pair,
        so a mode with several blocks requests them round-robin, never
        together.
        """
        plan = plan or self.plan
        if not plan.cycle:
            return None
        with self._lock:
            block = plan.cycle[self._poll_index % len(plan.cycle)]
            self._poll_index += 1
            rate = windowed_rate(self._poll_times, time.time())
        self.update(poll_rate_hz=rate)
        return protocol.read_register(block)

    # --- decoding ------------------------------------------------------------

    def consume(self, chunk: bytes) -> None:
        """Feed received bytes; emits a sample per decoded frame."""
        if not chunk:
            return
        if self._parser.output_length is None:
            detected = self._detector.observe(chunk)
            if detected is not None:
                self._parser.set_output_length(detected)
                self.update(output_length=detected, layout=protocol.layout_for(detected).name)
        frames = self._parser.feed(chunk)
        self.update(
            bytes_read=self._status.bytes_read + len(chunk),
            dropped_bytes=self._parser.dropped_bytes,
        )
        for frame in frames:
            self._handle_frame(frame)

    def _handle_frame(self, frame: protocol.Frame) -> None:
        self._accumulator = self._accumulator.updated(frame.registers)
        now = time.time()
        if frame.kind == "output":
            with self._lock:
                rate = windowed_rate(self._frame_times, now)
            self.update(rate_hz=rate)
        self.update(frames=self._status.frames + 1, layout=frame.layout or self._status.layout)
        self._on_sample(self._accumulator.sample(now))
