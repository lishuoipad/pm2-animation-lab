"""Time-bound research comparisons invoked only by pm2_activity_pipeline.

There is deliberately no standalone producer CLI. Equality and evidence
integrity are separate results; the strict activity delivery gate is unchanged.
"""
from __future__ import annotations
import bisect
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
from fractions import Fraction

import numpy as np
from PIL import Image, ImageDraw, ImageSequence
from pm2_animation_lab.pm2_activity_clock import exact_fields, presentation_groups, rational, pair
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError, bound, read_bound, digest, confined
from pm2_animation_lab.pm2_activity_video_index import verify_framehash
from pm2_animation_lab.pm2_activity_transition_copy import PlanarFit, find_planar_copy_fits
from pm2_animation_lab.pm2_activity_sequence_finish import rgb_to_indices, synthesize_copy, source_chain


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=True, indent=2)
        f.write('\n')


def verify_recording(observation, video, root):
    """Verify either current capture session or the original bound launch spec."""
    if ('capture_session' in observation)==('capture_spec' in observation):
        raise DeliveryError('one_capture_provenance_required')
    if 'capture_session' in observation:
        session_path=bound(observation['capture_session'],root)
        session=json.loads(session_path.read_text(encoding='utf-8'))
        if (session_path.parent!=video.parent.parent or session.get('record_native_video') is not True
                or session['inputs']['record_config']!=observation['recording_config']):
            raise DeliveryError('capture_session_mismatch')
    else:
        spec=read_bound(observation['capture_spec'],root)
        command=read_bound(observation['capture_command'],root)
        if (Path(spec['isolation_directory']).resolve()!=video.parent.parent
                or command.get('spec_sha256')!=observation['capture_spec']['sha256']
                or spec['inputs']['record_config']!=observation['recording_config']):
            raise DeliveryError('capture_spec_mismatch')
        argv=command['argv']
        for option,expected in (('-r',video),('--recordconfig',Path(observation['recording_config']['path']).resolve())):
            if argv.count(option)!=1 or argv.index(option)+1>=len(argv) or Path(argv[argv.index(option)+1]).resolve()!=expected:
                raise DeliveryError('capture_command_mismatch')
    config=bound(observation['recording_config'],root).read_text(encoding='utf-8')
    compact=''.join(config.split())
    if not all(s in compact for s in ('vcodec="libx264rgb"','video_qp="0"','frame_drop_ratio="1"')):
        raise DeliveryError('lossless_recording_config_required')


def native_observation(binding, root):
    observation = read_bound(binding, root)
    if observation.get('schema') != 'pm2_activity_ordered_observation/v1':
        raise DeliveryError('ordered_observation_schema')
    selection_doc = read_bound(observation['frozen_selection'], root)
    if selection_doc.get('selection_independent_of_candidate') is not True:
        raise DeliveryError('independent_selection_required')
    matching = [s for s in selection_doc['selections'] if s['scene'] == observation['scene']]
    if len(matching) != 1 or matching[0] != observation['selection']:
        raise DeliveryError('frozen_selection_changed')
    index_path = bound(observation['native_index'], root)
    index = json.loads(index_path.read_text(encoding='utf-8'))
    bound(index['video'], root)
    if (index.get('crop') != [32, 240, 320, 128]
            or index.get('capture_encoding') != 'lossless_rgb'
            or index.get('crop_stream_framehash_verified') is not True):
        raise DeliveryError('native_crop_or_encoding')
    for ref in index['provenance'].values():
        bound({'path': str(index_path.parent / ref['path']), 'sha256': ref['sha256']}, root)
    fh = index['provenance']['crop_rgb.framehash']
    rows, tb = verify_framehash((index_path.parent / fh['path']).read_text(encoding='utf-8'),
        [r['rgb_sha256'] for r in index['frames']], 320 * 128 * 3)
    if any(Fraction(r['pts'] * tb[0], tb[1]) != Fraction(n['pts'] * index['timebase'][0], index['timebase'][1])
           for r, n in zip(rows, index['frames'])):
        raise DeliveryError('native_pts_changed')
    # Check recording settings, rather than trusting the index's encoding label.
    verify_recording(observation,Path(index['video']['path']).resolve(),root)
    first = observation['selection']['first_frame']
    end = observation['selection']['end_frame_exclusive']
    if not 0 <= first < end < len(index['frames']):
        raise DeliveryError('unobserved_final_boundary')
    opening_end = observation['opening_end_frame']
    if not first <= opening_end < end:
        raise DeliveryError('opening_outside_selection')
    prior = opening_end
    for group in observation['groups']:
        if group['first_frame'] != prior or not prior < group['end_frame_exclusive'] <= end:
            raise DeliveryError('observed_group_gap_or_overlap')
        stable = group['stable_first_frame']
        if not prior <= stable < group['end_frame_exclusive']:
            raise DeliveryError('stable_frame_outside_group')
        prior = group['end_frame_exclusive']
    if prior != end:
        raise DeliveryError('observation_incomplete')
    return observation, index, index_path


def calibrated_groups(ticks, observation, index):
    tb = Fraction(*index['timebase'])
    origin = index['frames'][observation['opening_end_frame']]['pts'] * tb
    groups = [{'ticks': g['ticks'],
               'start': pair(index['frames'][g['first_frame']]['pts'] * tb - origin),
               'end': pair(index['frames'][g['end_frame_exclusive']]['pts'] * tb - origin)}
              for g in observation['groups']]
    clock = {'kind': 'capture_calibrated', 'groups': groups,
             'evidence': [observation['native_index']], 'boundary_complete': True}
    return presentation_groups(ticks, clock), clock


def source_frame_for_group(images, group):
    raw = images[group['ticks'][0]].tobytes()
    if any(images[t].tobytes() != raw for t in group['ticks']):
        raise DeliveryError('ambiguous_group_contains_distinct_poses')
    return images[group['ticks'][0]]


def difference(actual, synthesized):
    a, s = np.asarray(actual), np.asarray(synthesized)
    mask = np.any(a != s, axis=2)
    y, x = np.nonzero(mask)
    box = [int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1] if len(x) else None
    return int(mask.sum()), box, mask


def copy_image(previous, following, fit, palette):
    if (not 0<=fit.seam_row<128 or not 0<=fit.completed_plane_count<4
            or not 0<=fit.boundary_x<=320 or fit.boundary_x%8):
        raise DeliveryError('copy_fit_outside_source_model')
    pixels=synthesize_copy(rgb_to_indices(previous,palette),rgb_to_indices(following,palette),320,fit)
    rgb=b''.join(bytes(palette[v]) for v in pixels)
    return Image.frombytes('RGB',(320,128),rgb)


def video_preview(directory, name, encoder):
    executable = Path(encoder['path']).resolve()
    if not executable.is_file() or digest(executable) != encoder['sha256']:
        raise DeliveryError('bound_encoder_required')
    command = [str(executable), '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-ignore_loop', '1', '-i', str(directory / (name+'.gif')), '-an',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '16', '-bf', '0',
        '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough', '-enc_time_base', '1:100',
        '-video_track_timescale', '1000', '-movflags', '+faststart', str(directory / (name+'.mp4'))]
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode or result.stderr:
        raise DeliveryError('video_encode:' + result.stderr.decode(errors='replace'))
    decode = [str(executable), '-hide_banner', '-loglevel', 'error', '-i', str(directory / (name+'.mp4')),
        '-an', '-fps_mode', 'passthrough', '-enc_time_base', 'demux', '-f', 'framehash',
        '-hash', 'sha256', '-pix_fmt', 'rgb24', '-']
    result = subprocess.run(decode, capture_output=True, timeout=60)
    if result.returncode or result.stderr:
        raise DeliveryError('video_decode:' + result.stderr.decode(errors='replace'))
    text = result.stdout.decode()
    (directory / (name+'.framehash')).write_text(text, encoding='utf-8')
    tb = re.search(r'#tb 0: (\d+)/(\d+)', text)
    if not tb: raise DeliveryError('video_decode_timebase')
    timebase = Fraction(int(tb[1]), int(tb[2]))
    actual = []
    for line in text.splitlines():
        if line and not line.startswith('#'):
            fields = [f.strip() for f in line.split(',')]
            start, duration = int(fields[2])*timebase, int(fields[3])*timebase
            actual.append((start, start+duration))
    expected, elapsed = [], Fraction(0)
    with Image.open(directory / (name+'.gif')) as gif:
        for frame in ImageSequence.Iterator(gif):
            end = elapsed + Fraction(frame.info['duration'],1000)
            expected.append((elapsed,end)); elapsed=end
    if actual != expected:
        raise DeliveryError('video_changed_gif_timing:' + name)
    return {'encoder':encoder,'encode_command':command,'decode_command':decode,
        'decoded_frames':len(actual),'duration':pair(elapsed),
        'frame_times_equal_gif':True,'pixels':'lossy_H264_yuv420p_viewing_copy',
        'timing_precision':'GIF cumulative rounding, at most 5ms from native boundaries', 'audio':False}


def check_comparison(directory, root):
    """Re-read bound source/native PNGs, durations and exported GIF bytes."""
    directory = confined(directory, root)
    manifest = json.loads((directory / 'comparison.json').read_text(encoding='utf-8'))
    if (manifest.get('entrypoint') != 'pm2_activity_pipeline'
            or manifest.get('schema') != 'pm2_activity_timed_comparison/v1'
            or manifest.get('timing_claim') != 'capture_calibrated_not_prediction'
            or manifest.get('authorization_effect') != 'none'):
        raise DeliveryError('unregistered_comparison')
    for name, sha in manifest['outputs'].items():
        if digest(confined(directory / name, directory)) != sha:
            raise DeliveryError('comparison_output_changed:' + name)
    request = read_bound(manifest['request'], root)
    for key in ('archive', 'palette'):
        bound(request[key], root)
    palette=read_bound(request['palette'],root)['entries']
    copy_mode=request.get('refresh_reconstruction')=='capture_conditioned_planar_copy'
    for ref in manifest.get('copy_model_sources',[]):
        path=Path(ref['path']).resolve()
        if not path.is_file() or digest(path)!=ref['sha256']:
            raise DeliveryError('copy_model_source_changed')
    if copy_mode and len(manifest.get('copy_model_sources',[]))!=3:
        raise DeliveryError('copy_model_evidence_missing')
    source = Path(manifest['source_binding']['path']).resolve()
    if not source.is_file() or digest(source) != manifest['source_binding']['sha256']:
        raise DeliveryError('comparison_source_changed')
    observation, index, ip = native_observation(request['observation'], root)
    sequence = json.loads((directory / 'sequence.json').read_text(encoding='utf-8'))
    groups, clock = calibrated_groups(sequence['ticks'], observation, index)
    if clock != json.loads((directory / 'clock.json').read_text(encoding='utf-8')):
        raise DeliveryError('comparison_clock_changed')
    first, end = observation['selection']['first_frame'], observation['selection']['end_frame_exclusive']
    starts = [g['first_frame'] for g in observation['groups']]
    if len(manifest['frames']) != end - first:
        raise DeliveryError('comparison_frame_coverage')
    if manifest['native_frame_count'] != end-first or manifest['source_tick_count'] != len(sequence['ticks']):
        raise DeliveryError('comparison_summary_coverage')
    timebase = Fraction(*index['timebase'])
    origin = index['frames'][first]['pts'] * timebase
    errors = []
    for n, row in enumerate(manifest['frames'], first):
        expected_start = pair(index['frames'][n]['pts'] * timebase - origin)
        expected_end = pair(index['frames'][n + 1]['pts'] * timebase - origin)
        group_id = bisect.bisect_right(starts, n) - 1
        ticks = observation['groups'][group_id]['ticks'] if group_id >= 0 else []
        image_id = ticks[0] + 1 if ticks else 0
        if (row['native_frame'] != n or row['start'] != expected_start or row['end'] != expected_end
                or row['ticks'] != ticks or row['source_image'] != f'source/{image_id:03d}.png'):
            raise DeliveryError('comparison_order_or_time')
        native = index['unique_images'][index['frames'][n]['rgb_sha256']]
        p = bound({'path': str(ip.parent / native['path']), 'sha256': native['sha256']}, root)
        with Image.open(p) as im:
            actual = im.convert('RGB')
        if hashlib.sha256(actual.tobytes()).hexdigest() != index['frames'][n]['rgb_sha256']:
            raise DeliveryError('native_png_rgb_changed')
        with Image.open(directory / row['source_image']) as im:
            synthesized = im.convert('RGB')
        if 'copy_fit' in row:
            if not copy_mode or not first<n<end-1 or group_id+1>=len(starts):
                raise DeliveryError('unbound_copy_reconstruction')
            following_id=observation['groups'][group_id+1]['ticks'][0]+1
            with Image.open(directory/'source'/f'{following_id:03d}.png') as im: following=im.convert('RGB')
            if (hashlib.sha256(synthesized.tobytes()).hexdigest()!=index['frames'][n-1]['rgb_sha256']
                    or hashlib.sha256(following.tobytes()).hexdigest()!=index['frames'][n+1]['rgb_sha256']):
                raise DeliveryError('copy_endpoints_not_exact_source_poses')
            fit=PlanarFit(**row['copy_fit'])
            synthesized=copy_image(synthesized,following,fit,palette)
            if hashlib.sha256(synthesized.tobytes()).hexdigest()!=row['synthesized_rgb_sha256']:
                raise DeliveryError('copy_reconstruction_pixels_changed')
        count, box, _ = difference(actual, synthesized)
        if count != row['different_pixels'] or box != row['difference_box']:
            raise DeliveryError('comparison_metrics_changed')
        errors.append(count)
    def merged(rows):
        result = []
        for h, start, finish in rows:
            if result and result[-1][0] == h:
                result[-1] = (h, result[-1][1], finish)
            else:
                result.append((h, start, finish))
        return result
    for name in ('synthesized', 'actual', 'comparison'):
        expected = merged([(r[name + '_rgb_sha256'], rational(r['start']), rational(r['end'])) for r in manifest['frames']])
        decoded, elapsed = [], Fraction(0)
        with Image.open(directory / (name + '.gif')) as gif:
            if gif.info.get('loop') != 0:
                raise DeliveryError('comparison_preview_not_looping')
            for frame in ImageSequence.Iterator(gif):
                duration = Fraction(frame.info.get('duration', 0), 1000)
                if duration <= 0:
                    raise DeliveryError('comparison_zero_duration')
                decoded.append((hashlib.sha256(frame.convert('RGB').tobytes()).hexdigest(), elapsed, elapsed + duration))
                elapsed += duration
        decoded = merged(decoded)
        if len(expected) != len(decoded) or any(a[0] != b[0] or abs(a[1]-b[1]) > Fraction(1,200)
                or abs(a[2]-b[2]) > Fraction(1,200) for a,b in zip(expected, decoded)):
            raise DeliveryError('comparison_gif_pixels_or_timing:' + name)
    expected_status = 'equal' if not any(errors) else 'differences_present'
    if (manifest['equality_status'] != expected_status or manifest['exact_native_frames'] != errors.count(0)
            or manifest['mean_different_pixels'] != sum(errors)/len(errors)
            or manifest['max_different_pixels'] != max(errors)):
        raise DeliveryError('comparison_false_equality_claim')
    return {'schema': 'pm2_activity_comparison_check/v1', 'status': 'passed',
            'meaning': 'comparison evidence, order and encoding verified; not an equality delivery',
            'equality_status': expected_status, 'checked_native_frames': len(errors),
            'exact_native_frames': errors.count(0), 'candidate_sha256': digest(directory / 'comparison.json'),
            'validator_sha256': digest(Path(__file__)), 'authorization_effect': 'none'}


def publish_comparison(request_path, output, root, source_root, compile_sequence, compose):
    request = json.loads(request_path.read_text(encoding='utf-8'))
    extra=tuple(k for k in ('refresh_reconstruction','prelude') if k in request)
    exact_fields(request, ('schema','scene','schedule','initialization_random_values','entry_state',
                          'archive','palette','observation','encoder')+extra, 'comparison_request')
    if 'refresh_reconstruction' in request and request['refresh_reconstruction']!='capture_conditioned_planar_copy':
        raise DeliveryError('unsupported_copy_claim')
    copy_mode='refresh_reconstruction' in request
    observation, index, ip = native_observation(request['observation'], root)
    if not isinstance(observation.get('opening_note'),str) or not observation['opening_note'].strip():
        raise DeliveryError('explicit_opening_scope_note_required')
    if request['scene'] != observation['scene'] or request['schedule']['start_weekday'] != observation['start_weekday']:
        raise DeliveryError('observation_scene_or_calendar')
    sequence, source_ref = compile_sequence(request, source_root)
    if source_ref['sha256'] != observation['source_sha256']:
        raise DeliveryError('observation_source_changed')
    images, assets = compose(sequence, request, root, include_opening=True)
    copy_sources=source_chain(source_root,request['scene']) if copy_mode else []
    palette=read_bound(request['palette'],root)['entries']
    groups, clock = calibrated_groups(sequence['ticks'], observation, index)
    for group in groups:
        source_frame_for_group(images[1:], group)
    if observation['opening_merged_with_first_sunday']:
        if images[0].tobytes() != source_frame_for_group(images[1:], groups[0]).tobytes():
            raise DeliveryError('opening_sunday_merge_changes_pixels')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.pending-pm2-comparison-', dir=output.parent))
    try:
        (staging / 'source').mkdir()
        for i, im in enumerate(images): im.save(staging / 'source' / f'{i:03d}.png')
        write(staging / 'sequence.json', sequence)
        write(staging / 'asset_bindings.json', assets)
        write(staging / 'clock.json', clock)
        first, end = observation['selection']['first_frame'], observation['selection']['end_frame_exclusive']
        tb = Fraction(*index['timebase'])
        origin = index['frames'][first]['pts'] * tb
        starts = [g['first_frame'] for g in observation['groups']]
        media = {name: [] for name in ('synthesized', 'actual', 'comparison')}
        durations, rows, cache = [], [], {}
        for n in range(first, end):
            group_id = bisect.bisect_right(starts, n) - 1
            ticks = observation['groups'][group_id]['ticks'] if group_id >= 0 else []
            image_id = ticks[0] + 1 if ticks else 0
            synth = images[image_id]
            h = index['frames'][n]['rgb_sha256']
            if h not in cache:
                ref = index['unique_images'][h]
                p = bound({'path': str(ip.parent / ref['path']), 'sha256': ref['sha256']}, root)
                with Image.open(p) as im: cache[h] = im.convert('RGB')
            actual = cache[h]
            copy_fit=None
            if (copy_mode and first<n<end-1 and group_id+1<len(starts)
                    and hashlib.sha256(synth.tobytes()).hexdigest()!=h):
                following=images[observation['groups'][group_id+1]['ticks'][0]+1]
                if (hashlib.sha256(synth.tobytes()).hexdigest()==index['frames'][n-1]['rgb_sha256']
                        and hashlib.sha256(following.tobytes()).hexdigest()==index['frames'][n+1]['rgb_sha256']):
                    fits=find_planar_copy_fits(rgb_to_indices(synth,palette),rgb_to_indices(actual,palette),
                                               rgb_to_indices(following,palette),320,128)
                    if fits:
                        fit=fits[0];synth=copy_image(synth,following,fit,palette)
                        copy_fit={'seam_row':fit.seam_row,'completed_plane_count':fit.completed_plane_count,'boundary_x':fit.boundary_x}
            count, box, mask = difference(actual, synth)
            panel = Image.new('RGB', (960, 152), (24,24,24))
            panel.paste(synth, (0,24)); panel.paste(actual, (320,24))
            diff = np.asarray(actual).copy(); diff[mask] = [255,0,0]
            panel.paste(Image.fromarray(diff), (640,24))
            draw = ImageDraw.Draw(panel)
            for x, label in ((5,'SOURCE SYNTHESIS'),(325,'ACTUAL CAPTURE'),(645,'DIFFERENCE (RED)')):
                draw.text((x,7), label, fill=(255,255,255))
            start = index['frames'][n]['pts'] * tb - origin
            finish = index['frames'][n+1]['pts'] * tb - origin
            duration = (round(finish*100) - round(start*100)) * 10
            if duration <= 0: raise DeliveryError('unrepresentable_gif_interval')
            durations.append(duration)
            row = {'native_frame': n, 'start': pair(start), 'end': pair(finish), 'ticks': ticks,
                   'source_image': f'source/{image_id:03d}.png', 'different_pixels': count, 'difference_box': box}
            if copy_fit is not None:row['copy_fit']=copy_fit
            for name, im in (('synthesized',synth),('actual',actual),('comparison',panel)):
                media[name].append(im)
                row[name + '_rgb_sha256'] = hashlib.sha256(im.tobytes()).hexdigest()
            rows.append(row)
        for name, frames in media.items():
            frames[0].save(staging / (name+'.gif'), save_all=True, append_images=frames[1:],
                           duration=durations, loop=0, optimize=False, disposal=1)
        video_checks = {name:video_preview(staging,name,request['encoder']) for name in media}
        write(staging / 'video_check.json', video_checks)
        # Representative differences are chosen after the complete comparison;
        # they never alter its independently frozen range or source order.
        sheet = Image.new('RGB',(960,152*3),(24,24,24))
        samples = sorted(set([len(rows)//5, len(rows)//2, max(range(len(rows)),key=lambda i:rows[i]['different_pixels'])]))
        for i,n in enumerate(samples): sheet.paste(media['comparison'][n],(0,152*i))
        sheet.save(staging / 'difference_examples.png')
        values = [r['different_pixels'] for r in rows]
        manifest = {'schema':'pm2_activity_timed_comparison/v1','entrypoint':'pm2_activity_pipeline',
            'request':{'path':str(request_path),'sha256':digest(request_path)}, 'scene':request['scene'],
            'source_binding':source_ref,'timing_claim':'capture_calibrated_not_prediction',
            'condition_claim':'explicit_source_conditions_not_actual_rng',
            'equality_status':'equal' if not any(values) else 'differences_present',
            'duration':rows[-1]['end'],'frames':rows,'native_frame_count':len(rows),
            'exact_native_frames':values.count(0),'mean_different_pixels':sum(values)/len(values),
            'max_different_pixels':max(values),'source_tick_count':len(sequence['ticks']),
            'opening_note':observation['opening_note'], 'source_order':'complete_continuous_source_execution',
            'copy_model_sources':copy_sources,
            'copy_fit_claim':'observed_parameters_in_fixed_source_copy_model_not_hardware_prediction' if copy_mode else 'disabled',
            'copy_reconstructed_frames':sum('copy_fit' in r for r in rows),
            'outputs':{p.relative_to(staging).as_posix():digest(p) for p in staging.rglob('*') if p.is_file()},
            'formal_s2':False,'authorization_effect':'none'}
        write(staging / 'comparison.json', manifest)
        receipt = check_comparison(staging, root)
        write(staging / 'comparison_check.json', receipt)
        os.rename(staging,output)
        return {'status':'comparison_published','scene':request['scene'],'path':str(output),
                'equality_status':manifest['equality_status'],'exact_native_frames':values.count(0),
                'native_frames':len(rows),'mean_different_pixels':manifest['mean_different_pixels']}
    except Exception as error:
        write(staging / 'FAILED.json',{'status':'not_published','error':type(error).__name__+':'+str(error)})
        raise
