"""Register accumulation and physical-unit conversion."""

import unittest

from wtvb01.model import Accumulator, Vector3


class VectorTest(unittest.TestCase):
    def test_magnitude(self):
        self.assertAlmostEqual(Vector3(3.0, 4.0, 0.0).magnitude, 5.0)

    def test_as_dict(self):
        self.assertEqual(Vector3(1.0, 2.0, 3.0).as_dict(), {"x": 1.0, "y": 2.0, "z": 3.0})


class AccumulatorTest(unittest.TestCase):
    def test_updating_returns_a_new_accumulator(self):
        first = Accumulator()
        second = first.updated({0x3A: 17})
        self.assertEqual(first.raw, {})
        self.assertEqual(second.raw, {0x3A: 17})

    def test_later_registers_win_but_earlier_ones_survive(self):
        acc = Accumulator().updated({0x3A: 17, 0x40: 2790}).updated({0x3A: 20})
        self.assertEqual(acc.raw, {0x3A: 20, 0x40: 2790})

    def test_scaling_matches_the_manual(self):
        sample = Accumulator().updated(
            {0x3A: 17, 0x3B: 22, 0x3C: 2, 0x40: 2790, 0x41: 67, 0x44: 37, 0x3D: 32768}
        ).sample(t=0.0)
        self.assertEqual(sample.velocity, Vector3(17.0, 22.0, 2.0))
        self.assertAlmostEqual(sample.temperature, 27.90)
        self.assertEqual(sample.displacement.x, 67.0)
        self.assertEqual(sample.frequency.x, 37.0)
        self.assertAlmostEqual(sample.angle.x, 180.0)

    def test_accel_and_gyro_use_the_standard_full_scale(self):
        sample = Accumulator().updated({0x36: 2048, 0x37: 16384}).sample(t=0.0)
        self.assertAlmostEqual(sample.accel.z, 1.0)
        self.assertAlmostEqual(sample.gyro.x, 1000.0)

    def test_unknown_registers_are_kept_raw_but_not_converted(self):
        sample = Accumulator().updated({0x32: 1234}).sample(t=0.0)
        self.assertEqual(sample.raw[0x32], 1234)
        self.assertEqual(sample.values, {})

    def test_return_rate_code_resolves_to_hz(self):
        self.assertEqual(Accumulator().updated({0x03: 0x09}).sample(0.0).return_rate_hz, 100.0)
        self.assertIsNone(Accumulator().sample(0.0).return_rate_hz)

    def test_as_dict_formats_register_addresses(self):
        payload = Accumulator().updated({0x3A: 17}).sample(t=1.5).as_dict()
        self.assertEqual(payload["t"], 1.5)
        self.assertEqual(payload["raw"], {"0x3A": 17})
        self.assertEqual(payload["values"], {"velocity_x": 17.0})


if __name__ == "__main__":
    unittest.main()
