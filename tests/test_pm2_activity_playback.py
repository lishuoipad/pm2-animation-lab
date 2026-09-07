"""Prevent legacy playback, stale receipts and source-condition drift."""
import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from pm2_animation_lab import pm2_activity_pipeline as pipeline
from pm2_animation_lab import pm2_activity_batch_research as batch
from pm2_animation_lab import pm2_activity_condition_replay as replay
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError
from pm2_animation_lab.pm2_activity_playback import verify_saved_receipt, verify_replayed_source
from pm2_animation_lab.pm2_activity_comparison_gallery import HTML


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {'status': 'passed', 'candidate_sha256': 'a',
                        'equality_status': 'equal', 'validator_sha256': 'old'}

    def test_upgraded_validator_requires_same_rechecked_semantics(self):
        current = dict(self.receipt, validator_sha256='new')
        verify_saved_receipt(self.receipt, current)

    def test_stale_manifest_is_rejected(self):
        with self.assertRaisesRegex(DeliveryError, 'receipt_stale'):
            verify_saved_receipt(self.receipt, dict(self.receipt, candidate_sha256='b'))

    def test_old_equality_receipt_cannot_hide_new_differences(self):
        with self.assertRaisesRegex(DeliveryError, 'receipt_stale'):
            verify_saved_receipt(self.receipt, dict(self.receipt, equality_status='differences_present'))

    def test_failed_receipt_cannot_be_reused(self):
        failed = dict(self.receipt, status='failed')
        with self.assertRaisesRegex(DeliveryError, 'receipt_stale'):
            verify_saved_receipt(failed, failed)


class SourceReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / 'source').mkdir()
        self.image = Image.new('RGB', (8, 4), 'blue')
        self.image.save(self.directory / 'source/000.png')
        self.sequence = {'ticks': [{'state': 1}]}
        (self.directory / 'sequence.json').write_text(json.dumps(self.sequence))
        (self.directory / 'asset_bindings.json').write_text('{}')
        self.source = {'path': str(self.directory / 'fixed_source'), 'sha256': 'source'}
        self.manifest = {'source_binding': self.source, 'outputs': {'source/000.png': 'bound'}}

    def verify(self):
        return verify_replayed_source(self.directory, {}, self.manifest, self.directory,
            self.directory, True, lambda *a: (copy.deepcopy(self.sequence), self.source),
            lambda *a, **kw: ([self.image], {}))

    def test_exact_requested_source_passes(self):
        self.assertEqual(self.verify(), 1)

    def test_saved_condition_sequence_cannot_replace_current_request(self):
        (self.directory / 'sequence.json').write_text('{"ticks":[{"state":2}]}')
        with self.assertRaisesRegex(DeliveryError, 'sequence_does_not_replay'):
            self.verify()

    def test_rehashed_wrong_source_pixels_still_fail_replay(self):
        Image.new('RGB', (8, 4), 'red').save(self.directory / 'source/000.png')
        with self.assertRaisesRegex(DeliveryError, 'pixels_do_not_replay'):
            self.verify()

    def test_source_image_without_output_binding_rejected(self):
        self.manifest['outputs'] = {}
        with self.assertRaisesRegex(DeliveryError, 'source_image_unbound'):
            self.verify()

    def test_changed_asset_bindings_rejected(self):
        (self.directory / 'asset_bindings.json').write_text('{"bank":"wrong"}')
        with self.assertRaisesRegex(DeliveryError, 'asset_bindings_do_not_replay'):
            self.verify()


class EntrypointTests(unittest.TestCase):
    def args(self):
        return ['--request', 'current.json', '--quarantine-root', 'root', '--source-root', 'source']

    def test_existing_publish_cli_remains_compatible(self):
        with patch.object(pipeline, 'publish', return_value={}) as publish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(self.args() + ['--output', 'new']), 0)
        publish.assert_called_once_with('current.json', 'new', 'root', 'source')

    def test_playback_uses_current_request_and_never_republishes(self):
        with patch.object(pipeline, 'playback', return_value={}) as playback, \
                patch.object(pipeline, 'publish') as publish, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(self.args() + ['--playback-directory', 'existing']), 0)
        playback.assert_called_once_with('current.json', 'existing', 'root', 'source', allow_differences=False)
        publish.assert_not_called()

    def test_differences_require_explicit_diagnostic_option(self):
        with patch.object(pipeline, 'playback', return_value={}) as playback, contextlib.redirect_stdout(io.StringIO()):
            pipeline.main(self.args() + ['--playback-directory', 'existing', '--allow-differences'])
        self.assertTrue(playback.call_args.kwargs['allow_differences'])

    def test_allow_differences_cannot_weaken_publish(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main(self.args() + ['--output', 'new', '--allow-differences'])

    def test_fixed_duration_shortcut_not_accepted(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main(self.args() + ['--output', 'new', '--frame-ms', '100'])


class DiagnosticIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'experiments').mkdir()
        self.archive = self.root / 'synthetic.zip'
        self.palette = self.root / 'palette.json'
        self.archive.write_bytes(b'synthetic fixture, no original assets')
        self.palette.write_text('{}')
        self.sequence = {'ticks': [{}, {}], 'calls': []}

    def images(self, *a, **kw):
        return [Image.new('RGB', (8, 4), c) for c in ('black', 'blue', 'red')], {}

    def assert_stills(self, directory):
        manifest = json.loads((directory / 'candidate.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['media_policy'], 'untimed_stills_only')
        self.assertEqual(manifest['timing_claim'], 'no_duration_assigned')
        self.assertEqual(len(manifest['frames']), 2)
        self.assertFalse(list(directory.rglob('*.gif')))
        self.assertFalse(list(directory.rglob('*.mp4')))

    def test_batch_diagnostic_produces_stills_without_100ms_movie(self):
        out = self.root / 'experiments/batch'
        with patch.object(batch, 'RUNTIME', self.root), patch.object(batch, 'ARCHIVE', self.archive), \
                patch.object(batch, 'PALETTE', self.palette), patch.object(batch.timeline, 'SUPPORTED_BRANCHES', ('success',)), \
                patch.object(batch, 'scenario', return_value=(self.sequence, {})), \
                patch.object(pipeline, 'compose', side_effect=self.images), contextlib.redirect_stdout(io.StringIO()):
            rows = batch.build(out, ['JOB001'], call_count=1)
        self.assertEqual(rows[0]['status'], 'unverified_source_candidate')
        self.assert_stills(out / 'JOB001/success')

    def test_condition_diagnostic_produces_stills_without_100ms_movie(self):
        out = self.root / 'experiments/replay'
        spec = self.root / 'conditions.json'
        spec.write_text(json.dumps({'initialization_random_values': [], 'calls': [],
                                    'entry_state': {'provenance': 'synthetic test'}}))
        with patch.object(replay, 'RUNTIME', self.root), patch.object(replay, 'ARCHIVE', self.archive), \
                patch.object(replay, 'PALETTE', self.palette), patch.object(replay.timeline, 'verify_source_checkout'), \
                patch.object(replay.timeline, 'build_timeline_sequence', return_value=self.sequence), \
                patch.object(pipeline, 'compose', side_effect=self.images):
            replay.replay('JOB001', spec, out)
        self.assert_stills(out)

    def test_animated_image_writers_remain_in_registered_modules(self):
        approved = {'pm2_activity_pipeline.py', 'pm2_activity_timed_comparison.py', 'pm2_pipeline_fault_regression.py'}
        violations = []
        for path in Path(pipeline.__file__).parent.glob('*.py'):
            if path.name.startswith('test_') or path.name in approved:
                continue
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and any(k.arg == 'save_all' for k in node.keywords):
                    violations.append(f'{path.name}:{node.lineno}')
        self.assertEqual(violations, [], 'Animated encoding requires a reviewed pipeline module')

    def test_diagnostic_gallery_has_no_autoplay_or_fixed_frame_clock(self):
        for forbidden in ('requestAnimationFrame', 'setInterval', 'Math.floor(t/100)', 'id="play"', 'id="speed"'):
            self.assertNotIn(forbidden, HTML)
        self.assertIn('id="rawtick"', HTML)


if __name__ == '__main__':
    unittest.main()
