"""CSV recording."""

import csv
import tempfile
import unittest
from pathlib import Path

from wtvb01.model import Accumulator
from wtvb01.recorder import CsvRecorder, default_filename
from wtvb01.registers import REGISTERS


class DefaultFilenameTest(unittest.TestCase):
    def test_name_is_prefixed_and_timestamped(self):
        path = default_filename(Path("/tmp"), prefix="rig")
        self.assertTrue(path.name.startswith("rig-"))
        self.assertTrue(path.name.endswith(".csv"))


class CsvRecorderTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "nested" / "out.csv"

    def tearDown(self):
        self._dir.cleanup()

    def _read(self):
        with self.path.open(newline="", encoding="utf-8") as handle:
            return list(csv.reader(handle))

    def test_creates_missing_directories_and_writes_a_header(self):
        recorder = CsvRecorder(self.path)
        recorder.close()
        header = self._read()[0]
        self.assertEqual(header[:2], ["timestamp_iso", "t_epoch"])
        self.assertEqual(len(header), 2 + len(REGISTERS))

    def test_rows_line_up_with_the_header_even_when_channels_are_missing(self):
        recorder = CsvRecorder(self.path)
        recorder.write(Accumulator().updated({0x3A: 17, 0x40: 2790}).sample(t=1.0))
        recorder.close()
        header, row = self._read()
        self.assertEqual(len(row), len(header))
        self.assertEqual(row[header.index("velocity_x")], "17")
        self.assertEqual(row[header.index("temperature")], "27.9")
        self.assertEqual(row[header.index("frequency_x")], "")

    def test_row_count_and_close_are_idempotent(self):
        recorder = CsvRecorder(self.path)
        for i in range(3):
            recorder.write(Accumulator().updated({0x3A: i}).sample(t=float(i)))
        self.assertEqual(recorder.rows, 3)
        recorder.close()
        recorder.close()
        self.assertTrue(recorder.closed)

    def test_writing_after_close_is_ignored_rather_than_raising(self):
        recorder = CsvRecorder(self.path)
        recorder.close()
        recorder.write(Accumulator().updated({0x3A: 1}).sample(t=1.0))
        self.assertEqual(len(self._read()), 1)


if __name__ == "__main__":
    unittest.main()
