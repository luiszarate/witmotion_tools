"""Sample fan-out, history and rate limiting."""

import unittest

from wtvb01_monitor.hub import SampleHub
from wtvb01.model import Accumulator


def _sample(t: float, velocity: int = 0):
    return Accumulator().updated({0x3A: velocity}).sample(t)


class SampleHubTest(unittest.TestCase):
    def test_history_is_bounded_and_keeps_the_newest(self):
        hub = SampleHub(history=3, publish_hz=0)
        for i in range(5):
            hub.publish(_sample(float(i)))
        self.assertEqual([s.t for s in hub.history()], [2.0, 3.0, 4.0])

    def test_history_limit_takes_the_tail(self):
        hub = SampleHub(history=10, publish_hz=0)
        for i in range(5):
            hub.publish(_sample(float(i)))
        self.assertEqual([s.t for s in hub.history(2)], [3.0, 4.0])

    def test_subscriber_receives_published_samples(self):
        hub = SampleHub(publish_hz=0)
        subscription = hub.subscribe()
        hub.publish(_sample(1.0, velocity=17))
        received = subscription.queue.get_nowait()
        self.assertEqual(received.velocity.x, 17.0)

    def test_publication_is_rate_limited_but_history_is_not(self):
        hub = SampleHub(publish_hz=10.0)  # one sample per 100 ms
        subscription = hub.subscribe()
        for i in range(10):
            hub.publish(_sample(i * 0.01))  # 100 Hz
        self.assertEqual(subscription.queue.qsize(), 1)
        self.assertEqual(len(hub.history()), 10)

    def test_unsubscribe_ends_iteration(self):
        hub = SampleHub(publish_hz=0)
        subscription = hub.subscribe()
        hub.publish(_sample(1.0))
        hub.unsubscribe(subscription)
        self.assertEqual(hub.subscriber_count, 0)
        self.assertEqual(len(list(subscription)), 1)

    def test_a_full_subscriber_queue_does_not_block_the_publisher(self):
        hub = SampleHub(publish_hz=0)
        subscription = hub.subscribe()
        for i in range(400):  # queue depth is 256
            hub.publish(_sample(float(i)))
        self.assertEqual(hub.latest.t, 399.0)

    def test_clear_drops_history_and_latest(self):
        hub = SampleHub(publish_hz=0)
        hub.publish(_sample(1.0))
        hub.clear()
        self.assertIsNone(hub.latest)
        self.assertEqual(hub.history(), ())


if __name__ == "__main__":
    unittest.main()
