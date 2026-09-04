"""Fan-out of samples to subscribers, with history and rate limiting."""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterator

from wtvb01.model import Sample

DEFAULT_HISTORY = 3000
DEFAULT_PUBLISH_HZ = 25.0
_QUEUE_DEPTH = 256


@dataclass(frozen=True)
class Subscription:
    """A consumer's queue; iterate it to receive samples."""

    queue: "queue.Queue[Sample | None]"

    def close(self) -> None:
        self.queue.put_nowait(None)

    def __iter__(self) -> Iterator[Sample]:
        while True:
            item = self.queue.get()
            if item is None:
                return
            yield item


class SampleHub:
    """Keeps recent samples and pushes new ones to every subscriber.

    Publication is rate limited: the sensor streams up to 100 Hz, which is
    more than a chart needs. History keeps every sample regardless, so nothing
    is lost for recording or export.
    """

    def __init__(self, history: int = DEFAULT_HISTORY, publish_hz: float = DEFAULT_PUBLISH_HZ) -> None:
        self._history: deque[Sample] = deque(maxlen=history)
        self._subscribers: set["queue.Queue[Sample | None]"] = set()
        self._lock = threading.Lock()
        self._min_interval = 1.0 / publish_hz if publish_hz > 0 else 0.0
        self._last_published: float | None = None
        self._latest: Sample | None = None

    def publish(self, sample: Sample) -> None:
        with self._lock:
            self._history.append(sample)
            self._latest = sample
            if self._last_published is not None and sample.t - self._last_published < self._min_interval:
                return
            self._last_published = sample.t
            targets = tuple(self._subscribers)
        for target in targets:
            try:
                target.put_nowait(sample)
            except queue.Full:
                pass  # A slow consumer skips samples rather than stalling the reader.

    def subscribe(self) -> Subscription:
        channel: "queue.Queue[Sample | None]" = queue.Queue(maxsize=_QUEUE_DEPTH)
        with self._lock:
            self._subscribers.add(channel)
        return Subscription(channel)

    def unsubscribe(self, subscription: Subscription) -> None:
        with self._lock:
            self._subscribers.discard(subscription.queue)
        subscription.close()

    @property
    def latest(self) -> Sample | None:
        with self._lock:
            return self._latest

    def history(self, limit: int | None = None) -> tuple[Sample, ...]:
        with self._lock:
            items = tuple(self._history)
        return items[-limit:] if limit else items

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._latest = None
            self._last_published = None

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)
