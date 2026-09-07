import struct
import unittest
from pm2_animation_lab.pm2_activity_input_census import census
from pm2_animation_lab.pm2_activity_replay_audit import synthetic_idle_replay, ReplayError


def tape(inputs):
    core = struct.pack('<IBBBBB', 0xd05b5747, 8, 0, 5, 16, 16) + b'ORIGINAL_FIXTURE'
    data = synthetic_idle_replay(core, 1, 0)[:-8]
    return data + struct.pack('<IBH', 0, 0, len(inputs)) + b''.join(struct.pack('<BBBBHh', *x) for x in inputs) + b'f'


class InputCensusTests(unittest.TestCase):
    def test_signed_axis_and_device_index(self):
        r = census(tape([(0, 2, 0, 0, 0, -8), (0, 2, 1, 0, 1, 12)]))
        self.assertEqual(r['input_queries'], 2)
        self.assertEqual(r['queries'][0]['minimum'], -8)
        self.assertEqual(r['queries'][1]['index'], 1)
        self.assertFalse(r['formal_s2'])

    def test_zero_input_values_count(self):
        r = census(tape([(0, 2, 0, 0, 2, 0)]))
        self.assertEqual(r['queries'][0]['nonzero'], 0)

    def test_duplicate_conflicts_are_not_hidden(self):
        r = census(tape([(0, 2, 0, 0, 2, 0), (0, 2, 0, 0, 2, 1)]))
        self.assertEqual(r['conflicting_duplicate_queries'], 1)

    def test_nonzero_padding_rejected(self):
        with self.assertRaisesRegex(ReplayError, 'padding'):
            census(tape([(0, 2, 0, 1, 2, 0)]))

    def test_truncation_rejected(self):
        with self.assertRaises(ReplayError):
            census(tape([(0, 2, 0, 0, 2, 0)])[:-1])

    def test_no_inputs_rejected(self):
        with self.assertRaisesRegex(ReplayError, 'recorded_inputs'):
            census(tape([])[:40])
