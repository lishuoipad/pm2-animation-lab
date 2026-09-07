import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from pm2_animation_lab.pm2_activity_replay_audit import ReplayError
from pm2_animation_lab.pm2_activity_replay_compare import compare_frames, distinct_runs, binding, verify_run
from pm2_animation_lab.pm2_activity_replay_probe_run import validate_mode, runtime_errors, run, config_value, recording_target, runtime_limits
from pm2_animation_lab.pm2_activity_video_index import file_hash


def index():
    return {'frame_size': [2, 2], 'raw_stream_framehash_verified': True,
            'frame_count': 2, 'timebase': [1, 60],
            'frames': [{'frame': i, 'pts': i, 'duration': 1, 'rgb_sha256': str(i)*64}
                       for i in range(2)]}


class ReplayCompareTests(unittest.TestCase):
    def test_exact_is_not_semantic_approval(self):
        self.assertTrue(compare_frames(index(), index())['exact'])

    def test_one_pixel_hash_change_rejected(self):
        a, b = index(), index()
        b['frames'][1]['rgb_sha256'] = 'f'*64
        r = compare_frames(a, b)
        self.assertFalse(r['exact'])
        self.assertEqual(r['rgb_differing_frames'], 1)

    def test_dropped_frame_rejected(self):
        a, b = index(), index()
        b['frames'].pop()
        b['frame_count'] -= 1
        self.assertFalse(compare_frames(a, b)['exact'])

    def test_same_rgb_different_pts_rejected(self):
        a, b = index(), index()
        b['frames'][1]['pts'] += 1
        self.assertFalse(compare_frames(a, b)['exact'])

    def test_same_rgb_different_duration_rejected(self):
        a, b = index(), index()
        b['frames'][1]['duration'] += 1
        self.assertFalse(compare_frames(a, b)['exact'])

    def test_equivalent_rational_timebases(self):
        a, b = index(), index()
        b['timebase'] = [1, 120]
        for row in b['frames']:
            row['pts'] *= 2
            row['duration'] *= 2
        self.assertTrue(compare_frames(a, b)['exact'])

    def test_invalid_geometry_count_or_ordinals(self):
        for field, value in [('frame_size', [1, 1]), ('frame_count', 99),
                             ('raw_stream_framehash_verified', False), ('timebase', [0, 1])]:
            a, b = index(), index()
            b[field] = value
            with self.subTest(field=field), self.assertRaises(ReplayError):
                compare_frames(a, b)
        b = index()
        b['frames'][1]['frame'] = 0
        with self.assertRaises(ReplayError):
            compare_frames(index(), b)

    def test_binding_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()/'synthetic'
            p.write_bytes(b'original synthetic test')
            sha = file_hash(p)
            self.assertEqual(binding(p, sha, Path(d).resolve()), p)
            p.write_bytes(b'changed')
            with self.assertRaisesRegex(ReplayError, 'hash_mismatch'):
                binding(p, sha, Path(d).resolve())

    def test_distinct_receipts_not_two_paths_to_one_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            paths = [root/'a', root/'b']
            videos = [root/'va', root/'vb']
            for p in paths + videos:
                p.write_text('synthetic')
            reports = [{'pid': 1}, {'pid': 2}]
            commands = [{'time_utc': f'2026-09-05T13:00:0{i}+00:00',
                         'inputs': {'replay': 'same'}, 'input_origin': 'synthetic'} for i in (1, 2)]
            distinct_runs(paths, reports, commands, videos)
            for rp, rr, cc, vv in [([paths[0]]*2, reports, commands, videos),
                                   (paths, reports, commands, [videos[0]]*2),
                                   (paths, [reports[0]]*2, [commands[0]]*2, videos)]:
                with self.assertRaises(ReplayError):
                    distinct_runs(rp, rr, cc, vv)
            changed = copy.deepcopy(commands)
            changed[1]['inputs']['replay'] = 'different'
            with self.assertRaisesRegex(ReplayError, 'different_bound'):
                distinct_runs(paths, reports, changed, videos)

    def test_runtime_failure_rejected_before_any_file_access(self):
        report = {'schema_version': 'pm2_replay_probe_run/v1', 'exit_code': 0xc0000005}
        with self.assertRaisesRegex(ReplayError, 'failed_runtime'):
            verify_run(Path('unused'), report, {}, Path('.'))

    def test_recovery_error_even_without_error_tag(self):
        self.assertTrue(runtime_errors('Load State Error: Invalid file format'))
        self.assertFalse(runtime_errors('[Replay] EOF after buttons'))

    def test_unauthorized_launch_never_starts_child(self):
        with patch('pm2_animation_lab.pm2_activity_replay_probe_run.subprocess.Popen') as child:
            with self.assertRaisesRegex(ReplayError, 'authorization_required'):
                run(SimpleNamespace(user_authorized_isolated_run=False))
            child.assert_not_called()

    def test_idle_record_validation_before_launch(self):
        config = 'network_cmd_port = "55466"\nnetwork_cmd_enable = "true"'
        self.assertEqual(validate_mode({'probe_mode': 'idle_record_from_state', 'native_command_port': 55466}, config),
                         'idle_record_from_state')
        for port in (True, 0, 65536, 55465):
            with self.subTest(port=port), self.assertRaises(ReplayError):
                validate_mode({'probe_mode': 'idle_record_from_state', 'native_command_port': port}, config)
        with self.assertRaises(ReplayError):
            validate_mode({'probe_mode': 'unknown'}, '')

    def test_seed_record_path_rejected_because_eof_pauses(self):
        with self.assertRaisesRegex(ReplayError, 'pauses_at_eof'):
            validate_mode({'probe_mode': 'idle_record'}, '')

    def test_record_duration_limits(self):
        config = 'network_cmd_port = "55466"\nnetwork_cmd_enable = "true"'
        for duration in (0, 31, True, 1.5):
            with self.subTest(duration=duration), self.assertRaisesRegex(ReplayError, 'duration_limit'):
                validate_mode({'probe_mode': 'idle_record_from_state', 'native_command_port': 55466,
                               'recording_duration_seconds': duration}, config)

    def test_explicit_long_playback_limits_preserve_short_defaults(self):
        self.assertEqual(runtime_limits({}, 'playback'), (600, 20))
        self.assertEqual(runtime_limits({}, 'idle_record_from_state'), (3000, 20))
        self.assertEqual(runtime_limits({'max_frames': 30000, 'process_timeout_seconds': 900},
                                        'playback'), (30000, 900))

    def test_invalid_long_playback_limits_rejected(self):
        for value in (0, True, 1.5, 120001):
            with self.subTest(value=value), self.assertRaisesRegex(ReplayError, 'max_frames_limit'):
                runtime_limits({'max_frames': value}, 'playback')
        for value in (0, True, 1.5, 3601):
            with self.subTest(value=value), self.assertRaisesRegex(ReplayError, 'process_timeout_limit'):
                runtime_limits({'process_timeout_seconds': value}, 'playback')
        with self.assertRaisesRegex(ReplayError, 'max_frames_limit'):
            runtime_limits({'max_frames': 20001}, 'idle_record_from_state')

    def test_config_duplicate_or_commented_false_rejected(self):
        self.assertEqual(config_value('k = "false" # comment', 'k'), 'false')
        for config in ('# k = "false"', 'k = "false"\nk = "true"', 'not_k = "false"'):
            with self.assertRaises(ReplayError):
                config_value(config, 'k')

    def test_record_target_no_overwrite_and_state_binding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = '\n'.join(f'{k} = "{v}"' for k, v in {
                'savestate_directory': str(root), 'replay_slot': '0', 'replay_auto_index': 'false',
                'quit_press_twice': 'false', 'confirm_quit': 'false'}.items())
            target = recording_target(config, root/'synthetic.m3u', root/'synthetic.state', root)
            self.assertEqual(target, root/'synthetic.replay0')
            target.write_bytes(b'prior synthetic evidence')
            with self.assertRaisesRegex(ReplayError, 'overwrite_existing'):
                recording_target(config, root/'synthetic.m3u', root/'synthetic.state', root)
            with self.assertRaisesRegex(ReplayError, 'entry_state_path'):
                recording_target(config, root/'synthetic.m3u', root/'other.state', root)

    def test_confirm_quit_cannot_silently_block_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            config = '\n'.join(f'{k} = "{v}"' for k, v in {
                'savestate_directory': str(root), 'replay_slot': '0', 'replay_auto_index': 'false',
                'quit_press_twice': 'true', 'confirm_quit': 'true'}.items())
            with self.assertRaisesRegex(ReplayError, 'unsafe_record_config'):
                recording_target(config, root/'synthetic.m3u', root/'synthetic.state', root)

    def runtime_fixture(self, root, log='[Replay] EOF after buttons'):
        # Original synthetic bytes only; never starts an emulator.
        video = root/'runtime.mkv'
        video.write_bytes(b'synthetic video identity')
        inputs = {}
        for key in ('executable', 'core', 'content', 'base_config', 'isolation_config',
                    'core_options', 'replay', 'record_config'):
            p = root/key
            p.write_text('synthetic '+key)
            inputs[key] = {'path': str(p), 'sha256': file_hash(p)}
        command = {'inputs': inputs, 'argv': ['synthetic', '--eof-exit', '-P', str(root/'replay'),
                   '-r', str(video)], 'input_origin': 'synthetic'}
        (root/'command.json').write_text(json.dumps(command))
        (root/'runtime.log').write_text(log)
        (root/'process.log').write_text('')
        report = {'schema_version': 'pm2_replay_probe_run/v1', 'exit_code': 0,
                  'timed_out': False, 'errors': [], 'mutated_inputs': [], 'native_commands': [],
                  'input_origin': 'synthetic', 'command': {'path': 'command.json',
                  'sha256': file_hash(root/'command.json')},
                  'files': {p.name: file_hash(p) for p in (video, root/'runtime.log', root/'process.log')}}
        return report, {'video': {'path': str(video), 'sha256': file_hash(video)}}

    def test_bound_runtime_fixture(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            report, ix = self.runtime_fixture(root)
            self.assertEqual(verify_run(root/'report.json', report, ix, root)['input_origin'], 'synthetic')

    def test_missing_eof_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            report, ix = self.runtime_fixture(root, '[Replay] started but unfinished')
            with self.assertRaisesRegex(ReplayError, 'missing_replay_eof'):
                verify_run(root/'report.json', report, ix, root)

    def test_recovery_error_with_success_exit_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            report, ix = self.runtime_fixture(root, '[Replay] EOF\nLoad State Error: Invalid file format')
            with self.assertRaisesRegex(ReplayError, 'runtime_error_log'):
                verify_run(root/'report.json', report, ix, root)

    def test_changed_runtime_binding_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            report, ix = self.runtime_fixture(root)
            (root/'core_options').write_text('changed synthetic option')
            with self.assertRaisesRegex(ReplayError, 'binding_hash'):
                verify_run(root/'report.json', report, ix, root)

    def test_borrowed_video_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            report, ix = self.runtime_fixture(root)
            ix['video']['path'] = str(root/'other_video')
            with self.assertRaisesRegex(ReplayError, 'run_video_location'):
                verify_run(root/'report.json', report, ix, root)

    def test_native_commands_cannot_masquerade_as_passive_playback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            report, ix = self.runtime_fixture(root)
            report['native_commands'] = [{'command': 'RECORD_REPLAY'}]
            with self.assertRaisesRegex(ReplayError, 'not_read_only_playback'):
                verify_run(root/'report.json', report, ix, root)


if __name__ == '__main__':
    unittest.main()
