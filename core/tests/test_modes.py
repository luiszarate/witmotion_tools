"""Capture mode definitions and interval clamping."""

import unittest

from wtvb01 import modes
from wtvb01.source import PollPlan


class CatalogTest(unittest.TestCase):
    def test_every_mode_is_reachable_by_key(self):
        for entry in modes.catalog():
            self.assertIs(modes.get(entry["key"]).key, modes.MODES[entry["key"]].key)

    def test_normal_is_the_default(self):
        self.assertEqual(modes.get(None).key, "normal")
        self.assertEqual(modes.DEFAULT_MODE, "normal")

    def test_unknown_mode_names_the_valid_options(self):
        with self.assertRaises(KeyError) as caught:
            modes.get("turbo")
        self.assertIn("high_speed", str(caught.exception))

    def test_normal_alternates_both_measurement_blocks(self):
        self.assertEqual(modes.NORMAL.cycle, (0x3A, 0x42))

    def test_high_speed_gives_most_of_the_budget_to_the_displacement_block(self):
        cycle = modes.HIGH_SPEED.cycle
        self.assertEqual(cycle.count(0x42), 9)
        self.assertEqual(cycle.count(0x3A), 1)
        self.assertIn("fast", modes.HIGH_SPEED.live_groups)
        self.assertEqual(modes.HIGH_SPEED.undersampled_groups, ())

    def test_blocks_deduplicates_the_cycle_for_display(self):
        self.assertEqual(modes.HIGH_SPEED.blocks, (0x42, 0x3A))
        self.assertEqual(modes.HIGH_SPEED.as_dict()["cycle_length"], 10)

    def test_normal_marks_the_high_speed_waveform_as_undersampled(self):
        # Block 0x42 read-backs do return 0x47-0x49, but once a second.
        self.assertIn("fast", modes.NORMAL.live_groups)
        self.assertIn("fast", modes.NORMAL.undersampled_groups)

    def test_a_group_is_never_both_absent_and_undersampled(self):
        for mode in modes.MODES.values():
            with self.subTest(mode=mode.key):
                self.assertTrue(set(mode.undersampled_groups) <= set(mode.live_groups))

    def test_stream_does_not_poll(self):
        self.assertEqual(modes.STREAM.cycle, ())
        self.assertNotIn("displacement", modes.STREAM.live_groups)

    def test_as_dict_renders_blocks_as_hex(self):
        self.assertEqual(modes.NORMAL.as_dict()["blocks"], ["0x3A", "0x42"])


class IntervalTest(unittest.TestCase):
    def test_none_takes_the_mode_default(self):
        self.assertEqual(modes.NORMAL.interval_for(None), 0.5)
        self.assertEqual(modes.HIGH_SPEED.interval_for(None), 0.02)

    def test_too_fast_is_clamped_to_what_the_sensor_answers(self):
        # Measured: one block every 10 ms answered 25 of 200 requests.
        self.assertEqual(modes.HIGH_SPEED.interval_for(0.001), 0.01)

    def test_zero_or_negative_disables_polling(self):
        self.assertEqual(modes.NORMAL.interval_for(0), 0.0)
        self.assertEqual(modes.NORMAL.interval_for(-1), 0.0)

    def test_a_mode_without_blocks_never_polls(self):
        self.assertEqual(modes.STREAM.interval_for(0.02), 0.0)


class PollPlanTest(unittest.TestCase):
    def test_resolve_combines_mode_and_override(self):
        plan = PollPlan.resolve("high_speed", 0.05)
        self.assertEqual(plan.mode, "high_speed")
        self.assertEqual(plan.interval, 0.05)
        self.assertEqual(plan.cycle, modes.HIGH_SPEED.cycle)
        self.assertTrue(plan.active)

    def test_a_stream_plan_is_inactive(self):
        self.assertFalse(PollPlan.resolve("stream", None).active)

    def test_a_zero_interval_plan_is_inactive(self):
        self.assertFalse(PollPlan.resolve("normal", 0).active)


if __name__ == "__main__":
    unittest.main()


class BleHelpersTest(unittest.TestCase):
    """The parts of the BLE transport that need no radio."""

    def test_short_form_ignores_the_uuid_base(self):
        # WitMotion's SDKs spell the base as ...9a34fb, the assigned base is
        # ...9b34fb; matching on the 16-bit form works with either.
        from wtvb01 import ble

        self.assertEqual(ble._short("0000ffe4-0000-1000-8000-00805f9a34fb"), "ffe4")
        self.assertEqual(ble._short("0000FFE4-0000-1000-8000-00805F9B34FB"), "ffe4")
        self.assertEqual(ble._short("ffe4"), "ffe4")

    def test_both_uuid_bases_are_offered(self):
        from wtvb01 import ble

        self.assertEqual(len(ble.SERVICE_UUIDS), 2)
        self.assertTrue(all(uuid.startswith("0000ffe5") for uuid in ble.SERVICE_UUIDS))

    def test_the_name_a_real_sensor_advertises_is_recognised(self):
        from wtvb01 import ble

        self.assertTrue(any(hint in "wtsensor-01" for hint in ble.NAME_HINTS))

    def test_not_found_is_a_kind_of_unavailable(self):
        from wtvb01 import ble

        self.assertTrue(issubclass(ble.BleNotFound, ble.BleUnavailable))


class ExclusiveScanTest(unittest.TestCase):
    """BLE discovery must not overlap: one adapter, one discovery session."""

    def test_scans_from_separate_event_loops_do_not_overlap(self):
        import asyncio
        import threading

        from wtvb01 import ble

        active = 0
        overlapped = False
        guard = threading.Lock()

        async def scan_once():
            nonlocal active, overlapped
            async with ble.exclusive_scan():
                with guard:
                    active += 1
                    if active > 1:
                        overlapped = True
                await asyncio.sleep(0.05)
                with guard:
                    active -= 1

        threads = [threading.Thread(target=lambda: asyncio.run(scan_once())) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(overlapped, "dos escaneos BLE se solaparon")
        self.assertEqual(active, 0)

    def test_the_lock_is_released_when_a_scan_raises(self):
        import asyncio

        from wtvb01 import ble

        async def failing():
            async with ble.exclusive_scan():
                raise RuntimeError("scan failed")

        with self.assertRaises(RuntimeError):
            asyncio.run(failing())
        # A leaked lock would deadlock every later connection attempt.
        asyncio.run(self._acquire_once())

    @staticmethod
    async def _acquire_once():
        from wtvb01 import ble

        async with ble.exclusive_scan():
            pass
