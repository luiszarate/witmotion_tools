"""Output frame length auto-detection."""

import unittest

from wtvb01.detect import OutputLengthDetector


def _stream(length: int, count: int) -> bytes:
    """A run of ``count`` frames of ``length`` bytes, payload all zero."""
    return (b"\x55\x61" + bytes(length - 2)) * count


class OutputLengthDetectorTest(unittest.TestCase):
    def test_undecided_until_enough_samples(self):
        detector = OutputLengthDetector(min_samples=6)
        self.assertIsNone(detector.observe(_stream(40, 3)))
        self.assertEqual(detector.observe(_stream(40, 5)), 40)

    def test_detects_each_known_length(self):
        for length in (28, 32, 40):
            with self.subTest(length=length):
                detector = OutputLengthDetector(min_samples=4)
                self.assertEqual(detector.observe(_stream(length, 10)), length)

    def test_works_when_frames_are_split_across_chunks(self):
        detector = OutputLengthDetector(min_samples=4)
        data = _stream(40, 10)
        result = None
        for start in range(0, len(data), 7):
            result = detector.observe(data[start:start + 7]) or result
        self.assertEqual(result, 40)

    def test_sync_pair_straddling_a_chunk_boundary_is_still_seen(self):
        detector = OutputLengthDetector(min_samples=2)
        data = _stream(28, 6)
        # Split so that a 0x55 ends one chunk and 0x61 starts the next.
        detector.observe(data[:29])
        self.assertEqual(detector.observe(data[29:]), 28)

    def test_inconsistent_gaps_stay_undecided(self):
        detector = OutputLengthDetector(min_samples=4, agreement=0.9)
        mixed = _stream(28, 3) + _stream(40, 3) + _stream(32, 3)
        self.assertIsNone(detector.observe(mixed))

    def test_reset_forgets_everything(self):
        detector = OutputLengthDetector(min_samples=4)
        detector.observe(_stream(40, 10))
        detector.reset()
        self.assertEqual(detector.samples, 0)
        self.assertIsNone(detector.result())


if __name__ == "__main__":
    unittest.main()
