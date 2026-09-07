from pm2_animation_lab.paths import is_data_directory
"""Compare two bound, independently launched Replay probe captures, without S2.

No alignment, crop-only matching, dropped frames or tolerance. This proves a
bounded capture comparison, never activity coverage, intent or effective R0.
"""
import argparse
from datetime import datetime
from fractions import Fraction
import json
from pathlib import Path
import sys

from pm2_animation_lab.pm2_activity_replay_audit import require, inspect_replay
from pm2_animation_lab.pm2_activity_replay_probe_run import runtime_errors
from pm2_animation_lab.pm2_activity_video_index import confined, file_hash, verify_framehash, write_json


def binding(path, digest, root):
    p = confined(path, root)
    require(file_hash(p) == digest.lower(), 'binding_hash_mismatch:'+p.name)
    return p


def bound_json(path, digest, root):
    p = binding(path, digest, root)
    return p, json.loads(p.read_text(encoding='utf-8'))


def compare_frames(a, b):
    require(a['frame_size'] == b['frame_size'], 'different_frame_geometry')
    for index in (a, b):
        require(index['raw_stream_framehash_verified'] is True, 'unverified_framehash')
        require(index['frame_count'] == len(index['frames']) > 0, 'frame_count')
        tb = index['timebase']
        require(len(tb) == 2 and all(type(v) is int and v > 0 for v in tb), 'timebase')
    differences = []
    if a['frame_count'] != b['frame_count']:
        differences.append({'kind': 'frame_count', 'a': a['frame_count'], 'b': b['frame_count']})
    rgb_differences, time_differences = 0, 0
    for ordinal, (x, y) in enumerate(zip(a['frames'], b['frames'])):
        require(x['frame'] == y['frame'] == ordinal, 'frame_ordinals')
        rgb_diff = x['rgb_sha256'] != y['rgb_sha256']
        time_diff = any(Fraction(x[k])*Fraction(*a['timebase']) !=
                        Fraction(y[k])*Fraction(*b['timebase']) for k in ('pts', 'duration'))
        rgb_differences += rgb_diff
        time_differences += time_diff
        if (rgb_diff or time_diff) and len(differences) < 20:
            differences.append({'frame': ordinal, 'rgb': rgb_diff, 'time': time_diff})
    return {'exact': not differences, 'frame_counts': [a['frame_count'], b['frame_count']],
            'rgb_differing_frames': rgb_differences, 'timing_differing_frames': time_differences,
            'first_differences': differences}


def verify_index(index_path, index, root):
    require(index['schema_version'] == 'pm2_activity_video_index/v1', 'index_schema')
    video = binding(index['video']['path'], index['video']['sha256'], root)
    require(index['capture_encoding'] == 'lossless_rgb', 'capture_encoding')
    files = {key: binding(index_path.parent / ref['path'], ref['sha256'], root)
             for key, ref in index['provenance'].items()}
    command = json.loads(files['command.json'].read_text(encoding='utf-8'))
    require(command['video_sha256'] == index['video']['sha256'], 'decode_video_binding')
    argv = command['argv']
    require(file_hash(Path(argv[0])) == command['ffmpeg_sha256'], 'decoder_changed')
    expected = [argv[0], '-nostdin', '-n', '-hide_banner', '-i', str(video)]
    for destination, options in [('pipe:1', ['-f', 'rawvideo']),
                                 (str(files['full_rgb.framehash']), ['-f', 'framehash', '-hash', 'sha256'])]:
        expected += ['-map', '0:v:0', '-an', '-pix_fmt', 'rgb24', '-fps_mode', 'passthrough',
                     *options, destination]
    require(argv == expected, 'unexpected_decode_transform')
    rows, tb = verify_framehash(files['full_rgb.framehash'].read_text(encoding='utf-8'),
                               [f['rgb_sha256'] for f in index['frames']],
                               index['frame_size'][0]*index['frame_size'][1]*3)
    require(tb == index['timebase'] and rows == [{k: f[k] for k in ('frame', 'pts', 'duration')}
                                               for f in index['frames']], 'index_pts_changed')
    return video


def verify_run(path, report, index, root):
    require(report['schema_version'] == 'pm2_replay_probe_run/v1', 'run_schema')
    require(report['exit_code'] == 0 and report['timed_out'] is False and
            not report['errors'] and not report['mutated_inputs'], 'failed_runtime')
    require(report['native_commands'] == [], 'not_read_only_playback')
    path = Path(path).resolve()
    command_path, command = bound_json(path.parent/report['command']['path'],
                                       report['command']['sha256'], root)
    require(command_path == path.parent/'command.json', 'command_location')
    require(set(command['inputs']) >= {'executable', 'core', 'content', 'base_config',
            'isolation_config', 'core_options', 'replay', 'record_config'}, 'incomplete_runtime_bindings')
    for key, ref in command['inputs'].items():
        binding(ref['path'], ref['sha256'], root)
    argv = command['argv']
    require('--eof-exit' in argv and '-P' in argv, 'no_eof_bound_playback')
    require(Path(argv[argv.index('-P')+1]).resolve() ==
            Path(command['inputs']['replay']['path']).resolve(), 'replay_argument_binding')
    video = Path(index['video']['path']).resolve()
    require(video == path.parent/'runtime.mkv' and argv[argv.index('-r')+1] == str(video),
            'run_video_location')
    require(report['files']['runtime.mkv'] == index['video']['sha256'], 'run_video_hash')
    logs = ''
    for name in ('runtime.log', 'process.log'):
        f = binding(path.parent/name, report['files'][name], root)
        logs += f.read_text(encoding='utf-8', errors='replace')
    require(not runtime_errors(logs), 'runtime_error_log')
    require('[Replay] EOF' in logs, 'missing_replay_eof')
    require(report['input_origin'] == command['input_origin'], 'origin_mismatch')
    return command


def distinct_runs(paths, reports, commands, videos):
    require(paths[0] != paths[1] and not paths[0].samefile(paths[1]), 'same_run')
    require(videos[0] != videos[1] and not videos[0].samefile(videos[1]), 'same_video')
    require((reports[0]['pid'], commands[0]['time_utc']) !=
            (reports[1]['pid'], commands[1]['time_utc']), 'same_process_receipt')
    require(datetime.fromisoformat(commands[0]['time_utc']) !=
            datetime.fromisoformat(commands[1]['time_utc']), 'same_launch_time')
    require(commands[0]['inputs'] == commands[1]['inputs'], 'different_bound_environment_or_replay')
    require(commands[0]['input_origin'] == commands[1]['input_origin'], 'different_input_origins')


def run(a):
    root = Path(a.quarantine_root).resolve()
    require(is_data_directory(root), 'existing_data_and_source_directories_required')
    if getattr(a, 'dependencies', None):
        sys.path.insert(0, str(confined(a.dependencies, root)))
    indexes, index_paths, reports, run_paths, videos, commands = [], [], [], [], [], []
    for side in ('a', 'b'):
        ip, index = bound_json(getattr(a, 'index_'+side), getattr(a, 'index_'+side+'_sha256'), root)
        rp, report = bound_json(getattr(a, 'run_'+side), getattr(a, 'run_'+side+'_sha256'), root)
        videos.append(verify_index(ip, index, root))
        commands.append(verify_run(rp, report, index, root))
        indexes.append(index)
        index_paths.append(ip)
        reports.append(report)
        run_paths.append(rp)
    require(index_paths[0] != index_paths[1] and not index_paths[0].samefile(index_paths[1]), 'same_index')
    distinct_runs(run_paths, reports, commands, videos)
    replay_ref = commands[0]['inputs']['replay']
    replay, _ = inspect_replay(Path(replay_ref['path']).read_bytes())
    require(replay['input_frame_count'] > 0, 'empty_replay')
    comparison = compare_frames(*indexes)
    result = {'schema_version': 'pm2_replay_comparison/v1',
              'status': 'bounded_captures_exact' if comparison['exact'] else 'bounded_captures_differ',
              'comparison': comparison, 'replay': replay_ref,
              'replay_input_frames': replay['input_frame_count'],
              'input_origin': commands[0]['input_origin'],
              'bindings': [{'index': {'path': str(ip), 'sha256': file_hash(ip)},
                            'run': {'path': str(rp), 'sha256': file_hash(rp)}}
                           for ip, rp in zip(index_paths, run_paths)],
              'tool_sha256': file_hash(Path(__file__)),
              'formal_s2': False, 'authorization_effect': 'none',
              'activity_coverage_verified': False, 'semantic_review_verified': False,
              'boundary': 'Full captured frames and exact PTS only. Synthetic input is not recorded gameplay. '
                          'Distinct receipts are provenance, not tamper-proof attestation. Content dependencies, '
                          'effective post-load CPU/input state and activity-to-return boundaries require separate R0/R1 evidence.'}
    if 'zstandard' in sys.modules:
        module = sys.modules['zstandard']
        result['zstandard'] = {'version': module.__version__,
            'files': [{'path': str(p), 'sha256': file_hash(p)} for p in
                      [Path(module.__file__), *sorted(Path(module.__file__).parent.glob('*.pyd'))]]}
    output = confined(a.output_directory, root)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'report.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('quarantine-root', 'index-a', 'index-a-sha256', 'index-b', 'index-b-sha256',
                 'run-a', 'run-a-sha256', 'run-b', 'run-b-sha256', 'output-directory'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--dependencies', help='Existing quarantined optional Zstandard package directory')
    try:
        result = run(parser.parse_args(argv))
        print(json.dumps({'status': result['status'], **result['comparison']}))
        return 0 if result['comparison']['exact'] else 1
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        print(f'REPLAY_COMPARE_ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
