"""Frame decoding, against the vectors in the manual and from real hardware."""

import unittest

from wtvb01 import protocol
from wtvb01.registers import VIBRATION_ADDRESSES
from wtvb01.testing import MANUAL_FRAME, REGISTER_FRAME, UART_FRAME



class DecodeOutputTest(unittest.TestCase):
    def test_manual_frame_matches_documented_values(self):
        frame = protocol.decode_output(MANUAL_FRAME)
        self.assertEqual(frame.layout, "manual-28")
        self.assertEqual(frame.registers[0x3A], 17)
        self.assertEqual(frame.registers[0x3B], 22)
        self.assertEqual(frame.registers[0x3C], 2)
        self.assertEqual(frame.registers[0x40], 2790)
        self.assertEqual(frame.registers[0x41], 67)
        self.assertEqual(frame.registers[0x46], 37)

    def test_uart_frame_maps_accel_and_leaves_polled_registers_alone(self):
        frame = protocol.decode_output(UART_FRAME)
        self.assertEqual(frame.layout, "uart-40")
        self.assertEqual(frame.registers[0x36], 0x084F)  # accel Z, about 1 g
        self.assertEqual(frame.registers[0x3A], 0)
        # Slots past 0x3F are always zero on this firmware and must not be
        # mapped, or they would clobber polled temperature/displacement.
        self.assertNotIn(0x40, frame.registers)
        self.assertNotIn(0x41, frame.registers)

    def test_unknown_length_still_decodes_from_the_first_vibration_register(self):
        layout = protocol.layout_for(24)
        self.assertEqual(layout.name, "unknown-24")
        self.assertEqual(layout.slots[0], 0x3A)
        self.assertEqual(layout.value_count, 11)

    def test_negative_values_are_signed(self):
        frame = protocol.decode_output(UART_FRAME)
        self.assertEqual(frame.registers[0x34], -46)


class DecodeRegisterBlockTest(unittest.TestCase):
    def test_block_start_and_eight_registers(self):
        frame = protocol.decode_register_block(REGISTER_FRAME)
        self.assertEqual(frame.layout, "block-0x3A")
        self.assertEqual(len(frame.registers), protocol.REGISTERS_PER_BLOCK)
        self.assertEqual(frame.registers[0x40], 3427)
        self.assertEqual(frame.registers[0x41], 1)


class FrameParserTest(unittest.TestCase):
    def test_parses_back_to_back_frames(self):
        parser = protocol.FrameParser(output_length=28)
        frames = parser.feed(MANUAL_FRAME * 3)
        self.assertEqual(len(frames), 3)
        self.assertEqual(parser.dropped_bytes, 0)

    def test_reassembles_frames_split_across_chunks(self):
        parser = protocol.FrameParser(output_length=28)
        self.assertEqual(parser.feed(MANUAL_FRAME[:5]), [])
        frames = parser.feed(MANUAL_FRAME[5:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].registers[0x3A], 17)

    def test_resynchronises_after_garbage(self):
        parser = protocol.FrameParser(output_length=28)
        frames = parser.feed(b"\x01\x02\x03" + MANUAL_FRAME)
        self.assertEqual(len(frames), 1)
        self.assertEqual(parser.dropped_bytes, 3)

    def test_mixes_output_and_register_frames(self):
        parser = protocol.FrameParser(output_length=40)
        frames = parser.feed(UART_FRAME + REGISTER_FRAME + UART_FRAME)
        self.assertEqual([f.kind for f in frames], ["output", "register", "output"])

    def test_output_frames_are_held_until_the_length_is_known(self):
        parser = protocol.FrameParser(output_length=None)
        self.assertEqual(parser.feed(MANUAL_FRAME), [])
        # Bytes skipped before detection are not counted as drops.
        self.assertEqual(parser.dropped_bytes, 0)
        parser.set_output_length(28)
        self.assertEqual(len(parser.feed(MANUAL_FRAME)), 1)

    def test_a_sync_byte_inside_the_payload_does_not_desynchronise(self):
        payload = bytearray(MANUAL_FRAME)
        payload[10] = protocol.SYNC
        parser = protocol.FrameParser(output_length=28)
        frames = parser.feed(bytes(payload) * 2)
        self.assertEqual(len(frames), 2)


class CommandTest(unittest.TestCase):
    def test_read_register(self):
        self.assertEqual(protocol.read_register(0x3A), bytes.fromhex("ffaa273a00"))

    def test_write_register_is_little_endian(self):
        self.assertEqual(protocol.write_register(0x03, 0x0009), bytes.fromhex("ffaa030900"))

    def test_unlock_and_save(self):
        self.assertEqual(protocol.unlock(), bytes.fromhex("ffaa6988b5"))
        self.assertEqual(protocol.save(), bytes.fromhex("ffaa000000"))

    def test_measurement_poll_covers_every_vibration_register(self):
        commands = protocol.measurement_poll()
        self.assertEqual(len(commands), len(protocol.MEASUREMENT_BLOCKS))
        covered = set()
        for block in protocol.MEASUREMENT_BLOCKS:
            covered.update(range(block, block + protocol.REGISTERS_PER_BLOCK))
        self.assertTrue(set(VIBRATION_ADDRESSES).issubset(covered))


if __name__ == "__main__":
    unittest.main()
