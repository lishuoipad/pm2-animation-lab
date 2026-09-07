"""Original fixtures for new structures, capture ledgers and strict playback."""
import copy
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from pm2_animation_lab import pm2_scene_sources as sources
from pm2_animation_lab import pm2_scene_comparison as comparison
from pm2_animation_lab import pm2_native_capture as capture
from pm2_animation_lab import pm2_activity_timed_comparison as encoding
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError


def initial():
    return dict(map_x=0, map_y=0, girl_x=4, girl_y=10, direction=3, phase=0, terrain_phase=0)


def step(dx=0, dy=0, sx=0, sy=0):
    return dict(dx=dx, dy=dy, scroll_x=sx, scroll_y=sy, terrain_phase=0)


class SceneSemanticTests(unittest.TestCase):
    def test_all_32_vacation_selectors_and_inclusive_age_boundaries(self):
        names = set()
        for age, band in ((10, 0), (11, 0), (12, 1), (13, 1), (14, 2), (15, 2), (16, 3), (18, 3)):
            for season in range(1, 5):
                for place, offset in (('mountain', 0), ('sea', 1)):
                    result = sources.vacation_variant(dict(age=age, season=season, destination=place))
                    number = band*8+(season-1)*2+1+offset
                    self.assertEqual(result['asset'], f'V{number:03d}.PT1')
                    self.assertEqual(result['library'], 'V0.LBX' if number <= 8 else 'V2.LBX')
                    names.add(result['asset'])
        self.assertEqual(len(names), 32)

    def test_sea_and_mountain_season_palettes(self):
        for place, expected in [('sea', [0, 11, 12, 13]), ('mountain', [9, 9, 9, 10])]:
            self.assertEqual([sources.vacation_variant(dict(age=15, season=s, destination=place))['palette_record'] for s in range(1, 5)], expected)

    def test_unknown_conditions_and_rest_are_rejected(self):
        for condition in [dict(age=True, season=1, destination='sea'), dict(age=9, season=1, destination='sea'),
                          dict(age=17, season=0, destination='sea'), dict(age=17, season=1, destination='forest'),
                          dict(age=17, season=1, destination='sea', fps=10)]:
            with self.assertRaises(ValueError): sources.vacation_variant(condition)
        for scene in ('REST', 'SANATORIUM'):
            with self.assertRaisesRegex(DeliveryError, 'no_supported_visual_domain'):
                sources.source_bindings(scene, Path('.'))

    def test_displacement_drives_phase_and_scrolling_preserves_world_position(self):
        states = sources.map_states({'initial': initial(), 'steps': [step(), step(1), step(1, sx=1), step(0, -1)]})
        self.assertEqual([s['phase'] for s in states], [0, 0, 1, 0, 1])
        self.assertEqual([s['direction'] for s in states], [3, 3, 3, 3, 1])
        self.assertEqual(states[3]['map_x']+states[3]['girl_x'], 6)
        self.assertEqual(states[3]['girl_x'], 5)

    def test_rejects_teleports_and_scroll_without_world_displacement(self):
        for action in (step(2), step(0, sx=1), step(1, sx=-1)):
            with self.assertRaises(ValueError): sources.map_states({'initial': initial(), 'steps': [action]})

    def test_bnk_is_row_major_not_pt1_column_major(self):
        bank = bytearray(16384)
        bank[0] = 0x80       # plane0, row0, byte0 -> x0,y0 =1
        bank[3] = 0x40       # plane0, row1, byte1 -> x9,y1 =1
        bank[64+31] = 1     # plane2, row15, byte1 -> x15,y15 =4
        pixels = sources.decode_chip(bytes(bank), 0)
        self.assertEqual([(i, v) for i, v in enumerate(pixels) if v], [(0, 1), (25, 1), (255, 4)])
        with self.assertRaises(DeliveryError): sources.decode_chip(b'bad', 0)

    def test_masks_preserve_individual_planes(self):
        self.assertEqual(sources.masked_chip(bytes([15])*256, bytes([8])*256, bytes([3])*256), bytes([11])*256)

    def test_map_mask_and_attribute_occlusion(self):
        # Every ground cell has index1; actor body index2, clears all planes.
        banks = {i: bytearray(16384) for i in range(7)}
        banks[0][:32] = bytes([255])*32
        for number in range(48): banks[5][number*128+32:number*128+64] = bytes([255])*32
        data = bytes(40000); attrs = bytearray(20000)
        state = initial()
        image = sources.render_map({k: bytes(v) for k, v in banks.items()}, data, bytes(attrs), state)
        self.assertEqual(image[160*448+64], 2)
        attrs[10*200+4] = 2
        image = sources.render_map({k: bytes(v) for k, v in banks.items()}, data, bytes(attrs), state)
        self.assertEqual(image[160*448+64], 1)
        self.assertEqual(image[0*448+440], 0)  # Explicit source UI reservation.

    def test_water_effect_is_not_silently_omitted(self):
        data = bytearray(40000)
        for x in (4, 5): struct.pack_into('<H', data, ((12*200)+x)*2, 0x180)
        with self.assertRaisesRegex(DeliveryError, 'water_effect'):
            sources.render_map({i: bytes(16384) for i in range(7)}, bytes(data), bytes(20000), initial())


class ScanoutTests(unittest.TestCase):
    def setUp(self):
        self.palette = [[n*17]*3 for n in range(16)]
        self.before = bytes(448*384)
        self.after = np.zeros((384, 448), dtype=np.uint8)
        self.after[160:208, 64:80] = 15

    def test_source_copy_prefix_is_not_arbitrary_pixel_patching(self):
        indices = np.zeros((384, 448), dtype=np.uint8)
        indices[160, 64:72] = 1
        actual = Image.fromarray(np.array(self.palette, dtype=np.uint8)[indices])
        result = sources.incremental_map_copy(self.before, self.after.tobytes(), actual, self.palette)
        self.assertIsNotNone(result)
        bad = actual.copy(); bad.putpixel((200, 200), (17, 17, 17))
        self.assertIsNone(sources.incremental_map_copy(self.before, self.after.tobytes(), bad, self.palette))

    def test_four_part_scanout_can_observe_old_middle_and_new_lower_tiles(self):
        indices = self.after.copy()
        indices[176:192, 64:80] = 0
        indices[176, 64:72] = 1
        actual = Image.fromarray(np.array(self.palette, dtype=np.uint8)[indices])
        actual.paste((24, 24, 24), (432, 0, 448, 128))
        self.assertIsNone(sources.incremental_map_copy(self.before, self.after.tobytes(), actual, self.palette))
        result = sources.scanout_map_copy(self.before, self.after.tobytes(), actual, self.palette)
        self.assertIsNotNone(result)
        self.assertEqual(result[0].tobytes(), actual.tobytes())
        progress = [p['completed_byte_writes'] for p in result[1]['scanout_parts']]
        self.assertEqual(progress, sorted(progress))

    def test_scanout_rejects_pixels_never_written_by_either_source_state(self):
        actual = Image.new('RGB', (448, 384))
        actual.putpixel((0, 0), (17, 17, 17))
        self.assertIsNone(sources.scanout_map_copy(self.before, self.after.tobytes(), actual, self.palette))


class CaptureContractTests(unittest.TestCase):
    def test_mouse_only_bounded_input(self):
        self.assertEqual(capture.validate_action({'op': 'step', 'frames': 2, 'mouse': {'2': 1}, 'release': True}), 12)
        for action in [{'op': 'step', 'frames': True}, {'op': 'step', 'frames': 3601},
                       {'op': 'step', 'keys': [13]}, {'op': 'step', 'mouse': {'2': 2}},
                       {'op': 'delete'}, {'op': 'step', 'release': 1}]:
            with self.assertRaises(DeliveryError): capture.validate_action(action)

    def test_transitive_content_and_recording_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            for name in ('core', 'game.zip', 'anchor', 'options'): (root/name).write_bytes(b'synthetic')
            (root/'game.m3u').write_text('#EXTM3U\ngame.zip\n')
            request = {'schema': 'pm2_native_capture_request/v1',
                       **{key: capture.binding(root/name) for key, name in [('core', 'core'), ('content', 'game.m3u'), ('anchor', 'anchor'), ('options', 'options')]},
                       'content_files': [capture.binding(root/'game.zip')], 'boot_frames': 1, 'settle_frames': 1,
                       'actions': [{'op': 'record', 'frames': 3}]}
            self.assertEqual(capture.validate_request(request, root), 3)
            changed = copy.deepcopy(request); changed['content_files'] = []
            with self.assertRaisesRegex(DeliveryError, 'transitive'): capture.validate_request(changed, root)
            changed = copy.deepcopy(request); changed['actions'].append({'op': 'record'})
            with self.assertRaisesRegex(DeliveryError, 'single_recording'): capture.validate_request(changed, root)
            (root/'game.zip').write_bytes(b'changed')
            with self.assertRaises(DeliveryError): capture.validate_request(request, root)


class ScenePublicationTests(unittest.TestCase):
    def fixture(self, root, mismatch=False):
        request_path = root/'request.json'; request_path.write_text('{}')
        request = {'scene': 'FREE', 'encoder': {}}
        observation = {'first_frame': 0, 'end_frame_exclusive': 3,
                       'states': [{'first_frame': 0, 'state': 0}], 'scope_note': 'Original synthetic picture fixture'}
        seq = {'scene': 'FREE', 'crop': [48, 130, 200, 128], 'states': [{'hold': True}]}
        base = Image.new('RGB', (200, 128), (0, 0, 0)); base.putpixel((20, 20), (255, 255, 255))
        unique, rows = {}, []
        for i in range(3):
            im = Image.new('RGB', (640, 480)); im.paste(base, (48, 130))
            if mismatch and i == 1: im.putpixel((50, 132), (255, 0, 0))
            sha = comparison.rgb_hash(im); p = root/(sha+'.png'); im.save(p)
            unique[sha] = {'path': p.name, 'sha256': capture.binding(p)['sha256']}
            rows.append({'frame': i, 'pts': i, 'duration': 1, 'rgb_sha256': sha})
        index = {'frames': rows, 'unique_images': unique, 'timebase': [1, 60]}
        payload = (request, observation, index, root/'index.json', seq, [base], {}, {'entries': [[i*17]*3 for i in range(16)]})
        return request_path, payload

    @staticmethod
    def fake_video(directory, name, encoder):
        (directory/(name+'.mp4')).write_bytes(b'synthetic encoder placeholder')
        return {'synthetic_test_stub': True}

    def test_publication_playback_and_tamper_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); request, fixture = self.fixture(root)
            output = root/'delivery'
            with patch.object(comparison, 'inputs', return_value=fixture), patch.object(encoding, 'video_preview', side_effect=self.fake_video):
                result = comparison.publish(request, output, root, root)
                self.assertEqual(result['exact_native_frames'], 3)
                self.assertEqual(comparison.playback(request, output, root, root)['status'], 'playback_verified')
                (output/'actual.gif').write_bytes(b'tampered')
                with self.assertRaisesRegex(DeliveryError, 'output_changed'): comparison.playback(request, output, root, root)

    def test_differences_remain_differences_and_require_explicit_playback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); request, fixture = self.fixture(root, mismatch=True)
            output = root/'delivery'
            with patch.object(comparison, 'inputs', return_value=fixture), patch.object(encoding, 'video_preview', side_effect=self.fake_video):
                self.assertEqual(comparison.publish(request, output, root, root)['exact_native_frames'], 2)
                with self.assertRaisesRegex(DeliveryError, 'diagnostic_mode'): comparison.playback(request, output, root, root)
                self.assertEqual(comparison.playback(request, output, root, root, allow_differences=True)['media_role'], 'difference_diagnostic')

    def test_gif_boundary_tamper_fails_readback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); request, fixture = self.fixture(root)
            with patch.object(comparison, 'inputs', return_value=fixture), patch.object(encoding, 'video_preview', side_effect=self.fake_video):
                output = root/'delivery'; comparison.publish(request, output, root, root)
            rows = json.loads((output/'scene_comparison.json').read_text())['frames']
            with Image.open(output/'actual.gif') as image:
                image.save(root/'changed.gif', duration=100, loop=0)
            (output/'actual.gif').write_bytes((root/'changed.gif').read_bytes())
            with self.assertRaisesRegex(DeliveryError, 'pixels_or_timing'): comparison.verify_gifs(output, rows)


@unittest.skipUnless(os.environ.get('PM2_SOURCE_ROOT'), 'optional external fixed source')
class OptionalSceneSourceTests(unittest.TestCase):
    def test_every_vacation_asset_binding_exists_in_fixed_packing_script(self):
        root = Path(os.environ['PM2_SOURCE_ROOT'])
        packing = (root/'KOSO/misc/LIBXX.BAT').read_text(encoding='cp932')
        for age in (11, 13, 15, 17):
            for season in range(1, 5):
                for destination in ('mountain', 'sea'):
                    variant = sources.vacation_variant(dict(age=age, season=season, destination=destination))
                    self.assertIn(variant['library']+' '+variant['asset'], packing)

    def test_fixed_source_chains_and_static_selectors(self):
        root = Path(os.environ['PM2_SOURCE_ROOT'])
        for scene, conditions in [('FREE', {'mode': 'free'}), ('FREE', {'mode': 'observe'}),
                                  ('VACATION', {'age': 17, 'season': 3, 'destination': 'mountain'}),
                                  ('ADVENTURE_EAST', {'initial': initial(), 'steps': [step(1)]})]:
            result = sources.compile_scene({'scene': scene, 'conditions': conditions}, root)
            self.assertTrue(result['sources'])


if __name__ == '__main__': unittest.main()
