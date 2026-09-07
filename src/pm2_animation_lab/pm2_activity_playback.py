"""Playback preflight used only through pm2_activity_pipeline.

Recheck an explicit current request and replay its source before returning
bound media. Never select legacy previews by name, recency or a saved gallery.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import json
from pathlib import Path

from PIL import Image

from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError, bound, confined, digest


def binding(path):
    return {'path': str(Path(path).resolve()), 'sha256': digest(path)}


def verify_saved_receipt(saved, current):
    # A validator upgrade is allowed only after the complete current check.
    # All semantic claims and the exact manifest hash must still agree.
    clean = lambda value: {k: v for k, v in value.items() if k != 'validator_sha256'}
    if saved.get('status') != 'passed' or clean(saved) != clean(current):
        raise DeliveryError('playback_receipt_stale_or_changed')


def verify_replayed_source(directory, request, manifest, root, source_root,
                           comparison, compile_sequence, compose):
    sequence, source = compile_sequence(request, source_root)
    if (source['sha256'] != manifest['source_binding']['sha256']
            or Path(source['path']).resolve() != Path(manifest['source_binding']['path']).resolve()):
        raise DeliveryError('playback_source_binding_mismatch')
    recorded = json.loads((directory / 'sequence.json').read_text(encoding='utf-8'))
    if sequence != recorded:
        raise DeliveryError('playback_source_sequence_does_not_replay')
    images, assets = compose(sequence, request, root, include_opening=comparison)
    if assets != json.loads((directory / 'asset_bindings.json').read_text(encoding='utf-8')):
        raise DeliveryError('playback_asset_bindings_do_not_replay')
    if comparison:
        expected = [(f'source/{i:03d}.png', image) for i, image in enumerate(images)]
    else:
        expected = [(group['image'], images[group['ticks'][0]]) for group in manifest['groups']]
    for name, image in expected:
        if name not in manifest['outputs']:
            raise DeliveryError('playback_source_image_unbound')
        with Image.open(confined(directory / name, directory)) as recorded_image:
            if recorded_image.convert('RGB').tobytes() != image.tobytes() or recorded_image.size != image.size:
                raise DeliveryError('playback_source_pixels_do_not_replay')
    return len(sequence['ticks'])


def prepare_playback(request_path, directory, root, source_root, *, allow_differences=False,
                     compile_sequence, compose):
    root, source_root = Path(root).resolve(), Path(source_root).resolve()
    if (not is_data_directory(root) or not source_root.is_dir()):
        raise DeliveryError('existing_data_and_source_directories_required')
    directory = confined(directory, root)
    if ('game' in {part.lower() for part in directory.parts}
            or any(part.startswith('.pending-pm2-') for part in directory.parts)
            or (directory / 'FAILED.json').exists()):
        raise DeliveryError('playback_unpublished_directory')
    request_path = confined(request_path, root)
    request_ref = binding(request_path)
    request = json.loads(request_path.read_text(encoding='utf-8'))
    candidates = [name for name in ('comparison.json', 'candidate.json') if (directory / name).is_file()]
    if len(candidates) != 1:
        raise DeliveryError('playback_publication_manifest_required')
    manifest_path = directory / candidates[0]
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    comparison = candidates[0] == 'comparison.json'
    expected_schema = 'pm2_activity_timed_comparison/v1' if comparison else 'pm2_activity_delivery/v1'
    if (manifest.get('schema') != expected_schema or manifest.get('entrypoint') != 'pm2_activity_pipeline'
            or manifest.get('authorization_effect') != 'none' or manifest.get('formal_s2') is not False):
        raise DeliveryError('playback_diagnostic_candidate_not_published')
    stored_request = manifest['request']
    if (stored_request['sha256'] != request_ref['sha256']
            or bound(stored_request, root) != request_path
            or manifest['scene'] != request['scene']):
        raise DeliveryError('playback_request_does_not_match_current_task')
    receipt_path = directory / ('comparison_check.json' if comparison else 'delivery_check.json')
    if not receipt_path.is_file():
        raise DeliveryError('playback_published_receipt_required')
    saved = json.loads(receipt_path.read_text(encoding='utf-8'))
    if saved.get('candidate_sha256') != digest(manifest_path):
        raise DeliveryError('playback_receipt_stale_or_changed')
    if comparison:
        from pm2_animation_lab.pm2_activity_timed_comparison import check_comparison
        current = check_comparison(directory, root)
        equality = current['equality_status']
        if equality != 'equal' and not allow_differences:
            raise DeliveryError('playback_differences_require_explicit_diagnostic_mode')
        if (manifest.get('condition_claim') != 'explicit_source_conditions_not_actual_rng'
                or manifest.get('source_order') != 'complete_continuous_source_execution'):
            raise DeliveryError('playback_condition_or_order_claim_changed')
        names = [f'{role}.{extension}' for role in ('synthesized', 'actual', 'comparison')
                 for extension in ('gif', 'mp4')]
        required = set(names) | {'video_check.json', 'clock.json', 'sequence.json', 'asset_bindings.json'}
        timing = 'capture_calibrated_not_prediction'
        role = 'ordered_comparison' if equality == 'equal' else 'difference_diagnostic'
    else:
        from pm2_animation_lab.pm2_activity_delivery_check import check
        current = check(directory, root, oracle_ref=request['oracle'])
        equality = 'equal_at_declared_' + manifest['level'] + '_scope'
        names = ['preview.gif']
        required = set(names) | {'sequence.json', 'asset_bindings.json'}
        timing = manifest['timing_claim'] if manifest['level'] != 'action' else 'action_timing_not_verified'
        role = 'stable_action_preview_not_native_refresh_video'
    verify_saved_receipt(saved, current)
    if not required.issubset(manifest['outputs']):
        raise DeliveryError('playback_media_binding_missing')
    source_ticks = verify_replayed_source(directory, request, manifest, root, source_root,
                                          comparison, compile_sequence, compose)
    media = {name: binding(directory / name) for name in names}
    return {'schema': 'pm2_activity_verified_playback/v1', 'status': 'playback_verified',
            'scene': request['scene'], 'request': request_ref, 'manifest': binding(manifest_path),
            'receipt': binding(receipt_path), 'equality_status': equality, 'media_role': role,
            'timing_claim': timing, 'source_ticks_replayed': source_ticks,
            'condition_claim': 'explicit_request_conditions_not_recovered_actual_rng',
            'media': media, 'authorization_effect': 'none'}
