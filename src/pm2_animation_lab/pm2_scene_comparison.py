"""Internal scene-domain comparison, dispatched only by the unified pipeline."""
from __future__ import annotations
import bisect
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageSequence

from .pm2_activity_clock import exact_fields
from .pm2_activity_delivery_check import DeliveryError, bound, confined, digest, read_bound
from .pm2_activity_timed_comparison import difference, encode_timed_media
from .pm2_native_capture import binding, validate_request as validate_capture_request
from .pm2_scene_sources import compile_scene, compose_scene, integer, incremental_map_copy, scanout_map_copy

SCANOUT_SOURCE_SHA = '1466ccf256022b00393ba62542a268222cfee268d7e273f44a4b2ed357bf4044'
SCANOUT_CORE_SHA = 'f309d60319ae5e9b844ea081df12c8e2441cf0850eb8d831aec39f5d76c175ff'


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2)


def rgb_hash(image):
    return hashlib.sha256(image.convert('RGB').tobytes()).hexdigest()


def pair(value):
    return [value.numerator, value.denominator]


def native_capture(ref, root):
    receipt = read_bound(ref, root)
    if (receipt.get('schema') != 'pm2_native_capture/v1'
            or receipt.get('timing_claim') != 'emulator_reported_frame_cadence'
            or receipt.get('desktop_input_used') is not False):
        raise DeliveryError('scene_native_capture_provenance')
    request = read_bound(receipt['request'], root)
    validate_capture_request(request, root)
    runner = Path(receipt['runner']['path']).resolve()
    if digest(runner) != receipt['runner']['sha256']:
        raise DeliveryError('scene_native_runner_changed')
    index = read_bound(receipt['index'], root)
    actions = read_bound(receipt['actions'], root)
    if len(actions) != len(request['actions']):
        raise DeliveryError('scene_native_action_coverage')
    elapsed = request['boot_frames']+request['settle_frames']
    recorded = 0
    recording = False
    from .pm2_native_capture import validate_action
    for i, (action, row) in enumerate(zip(request['actions'], actions), 1):
        n = validate_action(action)
        recording |= action['op'] == 'record'
        elapsed += n
        if recording: recorded += n
        if (row['step'] != i or row['input'] != action or row['frame_count'] != elapsed
                or row['recorded_frames'] != recorded):
            raise DeliveryError('scene_native_action_order')
    if (index.get('schema') != 'pm2_libretro_native_frames/v1' or index['frame_size'] != [640, 480]
            or index['pixel_format'] != 'XRGB8888' or index['frame_count'] != recorded
            or len(index['frames']) != recorded or index['timing_claim'] != 'emulator_reported_frame_cadence'):
        raise DeliveryError('scene_native_index_shape')
    for key in ('core', 'content', 'anchor'):
        if index[key] != request[key]: raise DeliveryError('scene_native_input_mismatch')
    fps = float.fromhex(index['fps_hex'])
    if not 1 <= fps <= 1000 or Fraction(*index['timebase']) != 1/Fraction.from_float(fps):
        raise DeliveryError('scene_native_timebase')
    for i, row in enumerate(index['frames']):
        if row != {'frame': i, 'pts': i, 'duration': 1, 'rgb_sha256': row['rgb_sha256']}:
            raise DeliveryError('scene_native_frame_order')
        if row['rgb_sha256'] not in index['unique_images']:
            raise DeliveryError('scene_native_frame_binding')
    return index, bound(receipt['index'], root)


def inputs(request_path, root, source_root):
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    extra = tuple(k for k in ('refresh_reconstruction', 'scanout_source') if k in request)
    exact_fields(request, ('schema', 'scene', 'conditions', 'archive', 'palette', 'observation', 'encoder')+extra, 'scene_request')
    if request['schema'] != 'pm2_scene_comparison_request/v1':
        raise DeliveryError('scene_request_schema')
    mode = request.get('refresh_reconstruction')
    if extra and (request['scene'] != 'ADVENTURE_EAST' or mode not in ('source_incremental_map_tiles', 'source_incremental_map_scanout')):
        raise DeliveryError('scene_refresh_model_scope')
    if ('scanout_source' in request) != (mode == 'source_incremental_map_scanout'):
        raise DeliveryError('scene_scanout_source_required')
    observation = read_bound(request['observation'], root)
    exact_fields(observation, ('schema', 'scene', 'capture', 'first_frame', 'end_frame_exclusive',
                               'states', 'scope_note', 'selection_evidence'), 'scene_observation')
    if observation['schema'] != 'pm2_scene_observation/v1' or observation['scene'] != request['scene']:
        raise DeliveryError('scene_observation_identity')
    if not isinstance(observation['scope_note'], str) or not observation['scope_note'].strip():
        raise DeliveryError('scene_observation_scope_required')
    bound(observation['selection_evidence'], root)
    index, ip = native_capture(observation['capture'], root)
    if mode == 'source_incremental_map_scanout':
        evidence = bound(request['scanout_source'], root)
        capture_receipt = read_bound(observation['capture'], root)
        capture_request = read_bound(capture_receipt['request'], root)
        options = bound(capture_request['options'], root).read_text()
        import re
        settings = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', options))
        if (digest(evidence) != SCANOUT_SOURCE_SHA or index['core']['sha256'] != SCANOUT_CORE_SHA
                or settings.get('dosbox_pure_machine') != 'svga' or settings.get('dosbox_pure_svga') != 'svga_s3'):
            raise DeliveryError('scene_scanout_core_source_or_configuration')
    first = integer(observation['first_frame'], 0, index['frame_count']-1, 'first_frame')
    end = integer(observation['end_frame_exclusive'], first+1, index['frame_count'], 'end_frame')
    sequence = compile_scene(request, Path(source_root))
    states = observation['states']
    if not isinstance(states, list) or len(states) != len(sequence['states']):
        raise DeliveryError('scene_source_state_coverage')
    previous = first-1
    for i, row in enumerate(states):
        exact_fields(row, ('first_frame', 'state'), 'scene_state_observation')
        if (type(row['state']) is not int or row['state'] != i or type(row['first_frame']) is not int
                or not previous < row['first_frame'] < end or (i == 0 and row['first_frame'] != first)):
            raise DeliveryError('scene_source_state_order')
        previous = row['first_frame']
    images, assets = compose_scene(sequence, request, root)
    palette = read_bound(request['palette'], root)
    changes = palette.get('changes', [])
    if changes and (request['scene'] != 'ADVENTURE_EAST' or palette.get('kind') != 'observed_native_palette_trace'):
        raise DeliveryError('scene_palette_trace_scope')
    previous = first-1
    for row in changes:
        exact_fields(row, ('first_frame', 'entries'), 'scene_palette_change')
        entries = row['entries']
        if (type(row['first_frame']) is not int or not previous < row['first_frame'] < end
                or len(entries) != 16 or any(len(c) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in c) for c in entries)):
            raise DeliveryError('scene_palette_trace_values')
        previous = row['first_frame']
    return request, observation, index, ip, sequence, images, assets, palette


def recolor(image, original, following, excluded):
    before = np.asarray(image)
    after = before.copy()
    for a, b in zip(original, following):
        after[np.all(before == a, axis=2)] = b
    result = Image.fromarray(after)
    for x, y, w, h in excluded:
        result.paste((24, 24, 24), (x, y, x+w, y+h))
    return result


def render_rows(observation, index, ip, sequence, images, palette, root, *, copy_mode=None):
    first, end = observation['first_frame'], observation['end_frame_exclusive']
    tb = Fraction(*index['timebase'])
    starts = [r['first_frame'] for r in observation['states']]
    changes = palette.get('changes', [])
    palette_starts = [r['first_frame'] for r in changes]
    cache = {}
    x, y, w, h = sequence['crop']
    excluded = sequence.get('excluded_rectangles', [])
    def source_at(n):
        state = bisect.bisect_right(starts, n)-1
        synth = images[state]
        palette_id = bisect.bisect_right(palette_starts, n)-1
        if palette_id >= 0:
            synth = recolor(synth, palette['entries'], changes[palette_id]['entries'], excluded)
        return state, synth, changes[palette_id]['entries'] if palette_id >= 0 else palette['entries']
    def actual_at(n):
        sha = index['frames'][n]['rgb_sha256']
        if sha not in cache:
            ref = index['unique_images'][sha]
            path = bound({'path': str(confined(ip.parent/ref['path'], ip.parent)), 'sha256': ref['sha256']}, root)
            with Image.open(path) as im:
                full = im.convert('RGB')
            if full.size != (640, 480) or rgb_hash(full) != sha:
                raise DeliveryError('scene_native_pixels_changed')
            actual = full.crop((x, y, x+w, y+h))
            for ex, ey, ew, eh in excluded:
                actual.paste((24, 24, 24), (ex, ey, ex+ew, ey+eh))
            cache[sha] = actual
        return cache[sha]
    source_indices = {}
    def indices(state):
        if state not in source_indices:
            data = np.asarray(images[state])
            output = np.zeros(data.shape[:2], dtype=np.uint8)
            for i, color in enumerate(palette['entries']): output[np.all(data == color, axis=2)] = i
            source_indices[state] = output.tobytes()
        return source_indices[state]
    for n in range(first, end):
        state, synth, entries = source_at(n)
        actual = actual_at(n)
        fit = None
        if copy_mode and first < n < end-1 and rgb_hash(synth) != rgb_hash(actual):
            before, old, _ = source_at(n-1)
            after, following, _ = source_at(n+1)
            if (after == before+1 and state in (before, after)
                    and rgb_hash(old) == rgb_hash(actual_at(n-1)) and rgb_hash(following) == rgb_hash(actual_at(n+1))):
                fit_function = scanout_map_copy if copy_mode == 'source_incremental_map_scanout' else incremental_map_copy
                result = fit_function(indices(before), indices(after), actual, entries)
                if result is not None:
                    synth, fit = result
                    fit = dict(fit, from_state=before, to_state=after)
        count, box, mask = difference(actual, synth)
        panel = Image.new('RGB', (w*3, h+24), (24, 24, 24))
        panel.paste(synth, (0, 24)); panel.paste(actual, (w, 24))
        diff = np.asarray(actual).copy(); diff[mask] = [255, 0, 0]
        panel.paste(Image.fromarray(diff), (w*2, 24))
        draw = ImageDraw.Draw(panel)
        for j, label in enumerate(('SOURCE SYNTHESIS', 'ACTUAL CAPTURE', 'DIFFERENCE (RED)')):
            draw.text((j*w+5, 7), label, fill='white')
        media = {'synthesized': synth, 'actual': actual, 'comparison': panel}
        row = {'native_frame': n, 'state': state, 'start': pair((n-first)*tb), 'end': pair((n-first+1)*tb),
               'different_pixels': count, 'difference_box': box,
               **{name+'_rgb_sha256': rgb_hash(im) for name, im in media.items()}}
        if fit is not None: row['copy_fit'] = fit
        yield row, media


def merged(rows):
    output = []
    for sha, first, end in rows:
        if output and output[-1][0] == sha:
            output[-1] = (sha, output[-1][1], end)
        else:
            output.append((sha, first, end))
    return output


def verify_gifs(directory, rows):
    for name in ('synthesized', 'actual', 'comparison'):
        expected = merged([(r[name+'_rgb_sha256'], Fraction(*r['start']), Fraction(*r['end'])) for r in rows])
        decoded, elapsed = [], Fraction(0)
        with Image.open(directory/(name+'.gif')) as gif:
            if gif.info.get('loop') != 0: raise DeliveryError('scene_gif_loop')
            for frame in ImageSequence.Iterator(gif):
                duration = Fraction(frame.info.get('duration', 0), 1000)
                if duration <= 0: raise DeliveryError('scene_gif_zero_duration')
                decoded.append((rgb_hash(frame), elapsed, elapsed+duration)); elapsed += duration
        decoded = merged(decoded)
        if (len(expected) != len(decoded) or any(a[0] != b[0] or abs(a[1]-b[1]) > Fraction(1, 200)
                or abs(a[2]-b[2]) > Fraction(1, 200) for a, b in zip(expected, decoded))):
            raise DeliveryError('scene_gif_pixels_or_timing:' + name)


def check(directory, request_path, root, source_root):
    directory = confined(directory, root)
    manifest = json.loads((directory/'scene_comparison.json').read_text(encoding='utf-8'))
    if (manifest.get('schema') != 'pm2_scene_comparison/v1' or manifest.get('entrypoint') != 'pm2_activity_pipeline'
            or manifest.get('authorization_effect') != 'none' or manifest.get('request') != binding(request_path)
            or manifest.get('timing_claim') != 'emulator_reported_cadence_not_hardware_prediction'
            or manifest.get('formal_s2') is not False):
        raise DeliveryError('scene_publication_identity')
    for name, sha in manifest['outputs'].items():
        if digest(confined(directory/name, directory)) != sha:
            raise DeliveryError('scene_output_changed:' + name)
    request, observation, index, ip, sequence, images, assets, palette = inputs(request_path, root, source_root)
    if sequence != json.loads((directory/'sequence.json').read_text()) or assets != json.loads((directory/'asset_bindings.json').read_text()):
        raise DeliveryError('scene_source_does_not_replay')
    for i, image in enumerate(images):
        with Image.open(directory/'source'/f'{i:03d}.png') as saved:
            if saved.size != image.size or rgb_hash(saved) != rgb_hash(image):
                raise DeliveryError('scene_source_pixels_do_not_replay')
    rows = [row for row, _ in render_rows(observation, index, ip, sequence, images, palette, root,
                                         copy_mode=request.get('refresh_reconstruction'))]
    if rows != manifest['frames']:
        raise DeliveryError('scene_comparison_does_not_replay')
    errors = [row['different_pixels'] for row in rows]
    expected = 'equal' if not any(errors) else 'differences_present'
    if (manifest['scene'] != request['scene'] or manifest['native_frames'] != len(rows)
            or manifest['exact_native_frames'] != errors.count(0) or manifest['equality_status'] != expected
            or manifest['crop'] != sequence['crop'] or manifest['excluded_rectangles'] != sequence.get('excluded_rectangles', [])
            or manifest['scope_note'] != observation['scope_note']
            or manifest['condition_claim'] != 'explicit_source_conditions_not_recovered_runtime_state'
            or manifest['copy_claim'] != ('observed_progress_in_source_ordered_tile_copy_not_hardware_prediction' if 'refresh_reconstruction' in request else 'disabled')
            or manifest['palette_claim'] != ('observed_rgb_trace_not_palette_prediction' if palette.get('changes') else 'explicit_fixed_rgb_palette')):
        raise DeliveryError('scene_false_summary_claim')
    verify_gifs(directory, rows)
    required = {f'{role}.{ext}' for role in ('synthesized', 'actual', 'comparison') for ext in ('gif', 'mp4')}
    if not (required | {'video_check.json', 'sequence.json', 'asset_bindings.json'}).issubset(manifest['outputs']):
        raise DeliveryError('scene_media_binding_missing')
    return {'status': 'passed', 'equality_status': expected, 'native_frames': len(rows),
            'exact_native_frames': errors.count(0), 'candidate_sha256': digest(directory/'scene_comparison.json'),
            'validator_sha256': digest(__file__), 'authorization_effect': 'none'}


def publish(request_path, output, root, source_root):
    request, observation, index, ip, sequence, images, assets, palette = inputs(request_path, root, source_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.pending-pm2-scene-', dir=output.parent))
    try:
        (staging/'source').mkdir()
        for i, image in enumerate(images): image.save(staging/'source'/f'{i:03d}.png')
        write(staging/'sequence.json', sequence); write(staging/'asset_bindings.json', assets)
        rows, durations, media = [], [], {name: [] for name in ('synthesized', 'actual', 'comparison')}
        for row, frames in render_rows(observation, index, ip, sequence, images, palette, root,
                                       copy_mode=request.get('refresh_reconstruction')):
            rows.append(row)
            duration = (round(Fraction(*row['end'])*100)-round(Fraction(*row['start'])*100))*10
            if duration <= 0: raise DeliveryError('scene_unrepresentable_gif_boundary')
            durations.append(duration)
            for name, frame in frames.items(): media[name].append(frame)
        write(staging/'video_check.json', encode_timed_media(staging, media, durations, request['encoder']))
        errors = [row['different_pixels'] for row in rows]
        manifest = {'schema': 'pm2_scene_comparison/v1', 'entrypoint': 'pm2_activity_pipeline',
                    'request': binding(request_path), 'scene': request['scene'], 'crop': sequence['crop'],
                    'excluded_rectangles': sequence.get('excluded_rectangles', []),
                    'scope_note': observation['scope_note'], 'frames': rows, 'native_frames': len(rows),
                    'exact_native_frames': errors.count(0), 'equality_status': 'equal' if not any(errors) else 'differences_present',
                    'timing_claim': 'emulator_reported_cadence_not_hardware_prediction',
                    'palette_claim': 'observed_rgb_trace_not_palette_prediction' if palette.get('changes') else 'explicit_fixed_rgb_palette',
                    'condition_claim': 'explicit_source_conditions_not_recovered_runtime_state',
                    'copy_claim': 'observed_progress_in_source_ordered_tile_copy_not_hardware_prediction' if 'refresh_reconstruction' in request else 'disabled',
                    'outputs': {p.relative_to(staging).as_posix(): digest(p) for p in staging.rglob('*') if p.is_file()},
                    'authorization_effect': 'none', 'formal_s2': False}
        write(staging/'scene_comparison.json', manifest)
        receipt = check(staging, request_path, root, source_root)
        write(staging/'scene_check.json', receipt)
        os.rename(staging, output)
        return {'status': 'scene_comparison_published', 'scene': request['scene'], 'path': str(output),
                'native_frames': len(rows), 'exact_native_frames': errors.count(0), 'equality_status': manifest['equality_status']}
    except Exception as error:
        write(staging/'FAILED.json', {'error': str(error)}); raise


def playback(request_path, directory, root, source_root, *, allow_differences=False):
    directory = confined(directory, root)
    if ('game' in {s.lower() for s in directory.parts} or any(s.startswith('.pending-pm2-') for s in directory.parts)
            or (directory/'FAILED.json').exists()):
        raise DeliveryError('scene_unpublished_directory')
    current = check(directory, request_path, root, source_root)
    saved = json.loads((directory/'scene_check.json').read_text())
    from .pm2_activity_playback import verify_saved_receipt
    verify_saved_receipt(saved, current)
    if current['equality_status'] != 'equal' and not allow_differences:
        raise DeliveryError('scene_differences_require_diagnostic_mode')
    return {'status': 'playback_verified', 'request': binding(request_path),
            'equality_status': current['equality_status'], 'native_frames': current['native_frames'],
            'timing_claim': 'emulator_reported_cadence_not_hardware_prediction',
            'media_role': 'ordered_scene_comparison' if current['equality_status'] == 'equal' else 'difference_diagnostic',
            'media': {name: binding(directory/name) for role in ('synthesized', 'actual', 'comparison')
                      for ext in ('gif', 'mp4') for name in [role+'.'+ext]}, 'authorization_effect': 'none'}
