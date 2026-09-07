import struct
import unittest
import zlib

from pm2_animation_lab.pm2_activity_replay_audit import (
    Reader, ReplayError, decode_statestream, inspect_replay, synthetic_idle_replay,
    unwrap_state, core_summary, decompress, MAX_BYTES,
)


def core():
    return struct.pack('<IBBBBB', 0xd05b5747, 8, 0, 5, 16, 16) + b'ORIGINAL_TEST_BYTES'


def state(raw):
    return b'RASTATE\x01MEM ' + struct.pack('<I', len(raw)) + raw + bytes((-len(raw)) % 8) + b'END ' + bytes(4)


def pack(value):
    if type(value) is int:
        return bytes([value]) if 0 <= value < 128 else b'\xce'+struct.pack('>I', value)
    if isinstance(value, bytes):
        return b'\xc5'+struct.pack('>H', len(value))+value
    return b'\xdc'+struct.pack('>H', len(value))+b''.join(pack(v) for v in value)


class ReplayAuditTests(unittest.TestCase):
    def test_synthetic_frames_are_structurally_readable_not_restore_verified(self):
        report, raw = inspect_replay(synthetic_idle_replay(core(), 3, 123))
        self.assertEqual(raw, core())
        self.assertEqual(report['input_frame_count'], 3)
        self.assertFalse(report['core_restore_verified'])
        self.assertFalse(report['formal_s2'])

    def test_header_only_is_rejected_not_a_recording(self):
        report, raw = inspect_replay(synthetic_idle_replay(core(), 3, 123)[:40])
        self.assertEqual(report['status'], 'header_only_rejected')
        self.assertIsNone(raw)

    def test_checkpoint_only_has_no_inputs(self):
        data = bytearray(synthetic_idle_replay(core(), 3, 123)[:-24])
        struct.pack_into('<I', data, 24, 0)
        report, _ = inspect_replay(data)
        self.assertEqual(report['status'], 'initial_checkpoint_only_rejected')

    def test_truncated_every_header_boundary(self):
        for n in (0, 1, 7, 39, 41, 43, 53):
            with self.subTest(n=n), self.assertRaises(ReplayError):
                inspect_replay(synthetic_idle_replay(core(), 3, 1)[:n])

    def test_truncated_frame(self):
        with self.assertRaises(ReplayError):
            inspect_replay(synthetic_idle_replay(core(), 3, 1)[:-1])

    def test_header_count_mismatch(self):
        b = bytearray(synthetic_idle_replay(core(), 3, 1))
        struct.pack_into('<I', b, 24, 4)
        with self.assertRaisesRegex(ReplayError, 'frame_count'):
            inspect_replay(b)

    def test_bad_initial_size(self):
        b = bytearray(synthetic_idle_replay(core(), 3, 1))
        struct.pack_into('<I', b, 12, 1)
        with self.assertRaisesRegex(ReplayError, 'checkpoint_length'):
            inspect_replay(b)

    def test_bad_frame_token(self):
        with self.assertRaisesRegex(ReplayError, 'frame_token'):
            inspect_replay(synthetic_idle_replay(core(), 1, 1)[:-1]+b'?')

    def test_bad_backref(self):
        b = bytearray(synthetic_idle_replay(core(), 3, 1))
        struct.pack_into('<I', b, len(b)-8, 1)
        with self.assertRaisesRegex(ReplayError, 'backref'):
            inspect_replay(b)

    def test_unsupported_version(self):
        b = bytearray(synthetic_idle_replay(core(), 3, 1))
        struct.pack_into('<I', b, 4, 99)
        with self.assertRaisesRegex(ReplayError, 'replay_header'):
            inspect_replay(b)

    def test_statestream_zero_and_authored_synthetic_block(self):
        encoded = b''.join(pack(x) for x in (0, 0, 1, 1, b'1234', 2, 1, [1, 0], 3, [1]))
        raw, frame = decode_statestream(encoded, 7, 4, 2, ({}, {}))
        self.assertEqual(raw, b'1234\0\0\0')
        self.assertEqual(frame, 0)

    def test_statestream_tables_persist_but_updates_replace(self):
        tables = ({}, {})
        first = b''.join(pack(x) for x in (0, 0, 1, 1, b'1234', 2, 1, [1], 3, [1]))
        second = b''.join(pack(x) for x in (0, 5, 1, 1, b'abcd', 3, [1]))
        decode_statestream(first, 4, 4, 1, tables)
        self.assertEqual(decode_statestream(second, 4, 4, 1, tables), (b'abcd', 5))

    def test_statestream_undefined_references(self):
        encoded = b''.join(pack(x) for x in (0, 0, 3, [9]))
        with self.assertRaisesRegex(ReplayError, 'undefined_superblock'):
            decode_statestream(encoded, 4, 4, 1, ({}, {}))

    def test_statestream_wrong_coverage(self):
        encoded = b''.join(pack(x) for x in (0, 0, 3, []))
        with self.assertRaisesRegex(ReplayError, 'coverage'):
            decode_statestream(encoded, 4, 4, 1, ({}, {}))

    def test_statestream_trailing_bytes(self):
        encoded = b''.join(pack(x) for x in (0, 0, 3, [0], 0))
        with self.assertRaisesRegex(ReplayError, 'trailing'):
            decode_statestream(encoded, 4, 4, 1, ({}, {}))

    def test_statestream_cannot_redefine_zero(self):
        encoded = b''.join(pack(x) for x in (0, 0, 1, 0, b'1234', 3, [0]))
        with self.assertRaisesRegex(ReplayError, 'block_definition'):
            decode_statestream(encoded, 4, 4, 1, ({}, {}))

    def test_msgpack_rejects_non_schema_types(self):
        for b in (b'\xc0', b'\xa1x', b'\x80', b'\xcb'+bytes(8)):
            with self.assertRaises(ReplayError):
                Reader(b).item()

    def test_raw_core_header_is_not_layout_validation(self):
        r = core_summary(core())
        self.assertTrue(r['magic_matches_dosbox_pure'])
        self.assertFalse(r['internal_layout_validated'])

    def test_rastate_mem_roundtrip(self):
        raw, report = unwrap_state(state(core()))
        self.assertEqual(raw, core())
        self.assertEqual(report['container'], 'RASTATE1')

    def test_rzip_chunked_roundtrip(self):
        raw = state(core())
        chunks = [zlib.compress(raw[i:i+13]) for i in range(0, len(raw), 13)]
        encoded = b'#RZIPv\x01#'+struct.pack('<IQ', 13, len(raw))
        encoded += b''.join(struct.pack('<I', len(c))+c for c in chunks)
        self.assertEqual(unwrap_state(encoded)[0], core())
        with self.assertRaises(ReplayError):
            unwrap_state(encoded+b'!')

    def test_missing_rastate_end(self):
        with self.assertRaisesRegex(ReplayError, 'missing_mem_or_end'):
            unwrap_state(state(core())[:-8])

    def test_duplicate_mem(self):
        with self.assertRaisesRegex(ReplayError, 'duplicate_core'):
            unwrap_state(state(core())[:-8]+state(core())[8:])

    def test_decompression_rejects_size_and_trailing(self):
        encoded = zlib.compress(b'1234')
        with self.assertRaises(ReplayError):
            decompress(encoded, 1, 3)
        with self.assertRaises(ReplayError):
            decompress(encoded+b'!', 1, 4)
        with self.assertRaises(ReplayError):
            decompress(encoded, 1, MAX_BYTES+1)

    def test_probe_frame_limits(self):
        for n in (0, -1, 3601):
            with self.assertRaises(ReplayError):
                synthetic_idle_replay(core(), n, 1)

    def test_probe_crc_limits(self):
        for crc in (-1, 0x100000000, True):
            with self.assertRaises(ReplayError):
                synthetic_idle_replay(core(), 3, crc)


if __name__ == '__main__':
    unittest.main()
