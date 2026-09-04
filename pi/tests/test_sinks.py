"""Rotating CSV output."""

import csv
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from wtvb01.model import Accumulator
from wtvb01_logger.sinks import RotatingCsvSink, unique_path


def _sample(t=0.0, velocity=17):
    return Accumulator().updated({0x3A: velocity, 0x40: 2790}).sample(t)


class UniquePathTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def test_name_carries_the_sensor_and_a_timestamp(self):
        path = unique_path(self.dir, "rotor", datetime(2026, 9, 4, 10, 30, 0))
        self.assertEqual(path.name, "rotor-20260904-103000.csv")

    def test_a_second_roll_in_the_same_second_does_not_overwrite(self):
        when = datetime(2026, 9, 4, 10, 30, 0)
        first = unique_path(self.dir, "rotor", when)
        first.touch()
        second = unique_path(self.dir, "rotor", when)
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "rotor-20260904-103000-1.csv")


class RotatingCsvSinkTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def _files(self):
        return sorted(self.dir.glob("*.csv"))

    def _rows(self, path):
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_writes_physical_units_with_a_sensor_column(self):
        sink = RotatingCsvSink(self.dir, "rotor", rotate_seconds=60)
        sink.write(_sample())
        sink.close()
        rows = self._rows(self._files()[0])
        self.assertEqual(rows[0]["sensor"], "rotor")
        self.assertEqual(rows[0]["velocity_x"], "17")
        self.assertEqual(rows[0]["temperature"], "27.9")

    def test_rotates_when_the_period_elapses(self):
        sink = RotatingCsvSink(self.dir, "rotor", rotate_seconds=0.05)
        for i in range(3):
            sink.write(_sample(float(i)))
            time.sleep(0.06)
        sink.close()
        self.assertEqual(len(self._files()), 3)
        self.assertEqual(sink.status().files, 3)

    def test_roll_starts_a_new_file_on_demand(self):
        sink = RotatingCsvSink(self.dir, "rotor", rotate_seconds=3600)
        sink.write(_sample(1.0))
        first = sink.path
        sink.roll()
        sink.write(_sample(2.0))
        sink.close()
        self.assertEqual(len(self._files()), 2)
        self.assertNotEqual(first, sink.path)

    def test_pause_stops_writing_without_losing_the_file(self):
        sink = RotatingCsvSink(self.dir, "rotor", rotate_seconds=3600)
        sink.write(_sample(1.0))
        sink.pause()
        self.assertFalse(sink.write(_sample(2.0)))
        sink.resume()
        self.assertTrue(sink.write(_sample(3.0)))
        sink.close()
        rows = self._rows(self._files()[0])
        self.assertEqual(len(rows), 2)
        self.assertEqual(sink.status().dropped, 1)

    def test_max_rate_decimates_instead_of_writing_every_sample(self):
        # At 100 Hz one sensor writes roughly 90 MB per hour; capping the rate
        # is how an SD card survives a long flight.
        sink = RotatingCsvSink(self.dir, "rotor", rotate_seconds=3600, max_rate_hz=20)
        written = sum(sink.write(_sample(float(i))) for i in range(50))
        sink.close()
        self.assertEqual(written, 1)
        self.assertEqual(sink.status().dropped, 49)

    def test_status_reports_progress(self):
        sink = RotatingCsvSink(self.dir, "rotor", rotate_seconds=3600)
        sink.write(_sample())
        status = sink.status()
        self.assertEqual(status.sensor, "rotor")
        self.assertEqual(status.rows_total, 1)
        self.assertTrue(status.path.endswith(".csv"))
        sink.close()

    def test_creates_the_directory_it_was_given(self):
        nested = self.dir / "deep" / "deeper"
        sink = RotatingCsvSink(nested, "rotor", rotate_seconds=3600)
        sink.write(_sample())
        sink.close()
        self.assertTrue(nested.is_dir())


if __name__ == "__main__":
    unittest.main()
