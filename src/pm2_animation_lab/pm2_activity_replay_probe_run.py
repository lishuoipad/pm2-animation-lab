from pm2_animation_lab.paths import is_data_directory
"""Bounded, explicitly authorized isolated Replay playback diagnostic.

Consumes pre-frozen local bindings. No mouse/keyboard injection, no overwrite,
no S2. idle_record_from_state records NEW real idle queries from a copied state.
The old seed-then-record path is rejected because Replay EOF pauses emulation.
The provided config must already
redirect writable paths into the declared isolation directory.
"""
import argparse
import datetime
import json
import re
import subprocess
import socket
import sys
import time
from pathlib import Path

from pm2_animation_lab.pm2_activity_replay_audit import require
from pm2_animation_lab.pm2_activity_video_index import confined, file_hash, write_json


def config_value(config, key):
    values = re.findall(r'^\s*'+re.escape(key)+r'\s*=\s*"([^"\r\n]*)"\s*(?:#.*)?$', config, re.MULTILINE)
    require(len(values) == 1, 'missing_or_duplicate_config:'+key)
    return values[0]


def validate_mode(spec, config):
    mode = spec.get('probe_mode', 'playback')
    require(mode != 'idle_record', 'seed_then_record_pauses_at_eof_use_direct_state')
    require(mode in ('playback', 'idle_record_from_state'), 'unsupported_probe_mode')
    if mode == 'idle_record_from_state':
        port = spec['native_command_port']
        require(type(port) is int and 1024 <= port <= 65535, 'native_port')
        require(config_value(config, 'network_cmd_port') == str(port), 'native_port_binding')
        require(config_value(config, 'network_cmd_enable') == 'true', 'native_commands_disabled')
        duration = spec.get('recording_duration_seconds', 3)
        require(type(duration) is int and 1 <= duration <= 30, 'recording_duration_limit')
    return mode


def recording_target(config, content, initial_state, isolated):
    state_dir = Path(config_value(config, 'savestate_directory')).resolve()
    require(state_dir.is_relative_to(isolated), 'state_directory_outside_isolation')
    require(initial_state == state_dir/(content.stem+'.state'), 'entry_state_path')
    for key, value in {'replay_slot': '0', 'replay_auto_index': 'false',
                       'quit_press_twice': 'false', 'confirm_quit': 'false'}.items():
        require(config_value(config, key) == value, 'unsafe_record_config:'+key)
    target = state_dir/(content.stem+'.replay0')
    require(not target.exists(), 'recording_would_overwrite_existing_replay')
    return target


def runtime_limits(spec, mode):
    max_frames = spec.get('max_frames', 3000 if mode == 'idle_record_from_state' else 600)
    frame_ceiling = 120000 if mode == 'playback' else 20000
    require(type(max_frames) is int and 1 <= max_frames <= frame_ceiling, 'max_frames_limit')
    timeout = spec.get('process_timeout_seconds', 20)
    require(type(timeout) is int and 1 <= timeout <= 3600, 'process_timeout_limit')
    return max_frames, timeout


def runtime_errors(logs):
    return [line for line in logs.splitlines() if any(t in line.lower() for t in
            ('invalid file format', 'failed to deserialize', 'could not load movie', '[error]'))]


def run(a):
    require(a.user_authorized_isolated_run, 'explicit_isolated_run_authorization_required')
    root = Path(a.quarantine_root).resolve()
    require(is_data_directory(root), 'existing_data_and_source_directories_required')
    spec_path = confined(a.spec, root)
    require(file_hash(spec_path) == a.spec_sha256.lower(), 'spec_hash')
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    required = ('executable', 'core', 'content', 'base_config', 'isolation_config', 'core_options', 'record_config')
    entry_key = 'initial_state' if spec.get('probe_mode') == 'idle_record_from_state' else 'replay'
    required += (entry_key,)
    bound = {}
    require(set(spec['inputs']) >= set(required), 'missing_bound_inputs')
    for key in spec['inputs']:
        b = spec['inputs'][key]
        p = confined(b['path'], root)
        require(file_hash(p) == b['sha256'], 'bound_input_changed:'+key)
        bound[key] = p
    isolated = confined(spec['isolation_directory'], root)
    # Fail closed on the essential opt-in safety contract; this is not a full
    # RetroArch config interpreter or proof of effective R0 values.
    config = bound['isolation_config'].read_text(encoding='utf-8')
    for key in ('savestate_directory', 'savefile_directory', 'screenshot_directory'):
        target = Path(config_value(config, key)).resolve()
        require(target.is_relative_to(isolated), 'writable_path_outside_isolation')
    for key in ('config_save_on_exit', 'savestate_auto_save', 'savestate_auto_load'):
        require(config_value(config, key) == 'false', 'unsafe_config:'+key)
    mode = validate_mode(spec, config)
    max_frames, process_timeout = runtime_limits(spec, mode)
    tape_target = None
    if mode == 'idle_record_from_state':
        tape_target = recording_target(config, bound['content'], bound['initial_state'], isolated)
    output = confined(a.output_directory, root)
    require(output.is_relative_to(isolated), 'output_outside_isolation')
    output.mkdir(parents=True, exist_ok=False)
    argv = [str(bound['executable']), '--verbose', '--log-file', str(output/'runtime.log'),
            '-c', str(bound['base_config']), '--appendconfig', str(bound['isolation_config']),
            '-L', str(bound['core']),
            *(['--entryslot', '0'] if mode == 'idle_record_from_state' else ['-P', str(bound['replay'])]),
            '--max-frames', str(max_frames), '--sram-mode', 'noload-nosave',
            '-r', str(output/'runtime.mkv'), '--recordconfig', str(bound['record_config']),
            str(bound['content'])]
    if mode == 'playback':
        argv.insert(1, '--eof-exit')
    command = {'argv': argv, 'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'spec_sha256': a.spec_sha256.lower(), 'inputs': spec['inputs'],
               'purpose': spec['purpose'], 'input_origin': spec['input_origin'],
               'tool_sha256': file_hash(Path(__file__))}
    write_json(output/'command.json', command)
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    events = []
    with (output/'process.log').open('xb') as log:
        process = subprocess.Popen(argv, cwd=bound['executable'].parent, stdout=log, stderr=log, startupinfo=info)
        timed_out = False
        try:
            if mode == 'idle_record_from_state':
                port = spec['native_command_port']
                time.sleep(5)  # Allow the explicit entry-state task to finish.
                for command, delay in (('RECORD_REPLAY', spec.get('recording_duration_seconds', 3)), ('HALT_REPLAY', 1), ('QUIT', 0)):
                    if process.poll() is not None:
                        break
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.sendto(command.encode('ascii'), ('127.0.0.1', port))
                    events.append({'command': command, 'time_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                   'endpoint': ['127.0.0.1', port], 'status': 'sent_log_confirmation_required'})
                    time.sleep(delay)
            exit_code = process.wait(timeout=process_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()  # Only this newly launched isolated child.
                process.wait()
    mutated = [k for k, p in bound.items() if file_hash(p) != spec['inputs'][k]['sha256']]
    logs = ''.join(p.read_text(encoding='utf-8', errors='replace') for p in
                   (output/'runtime.log', output/'process.log') if p.exists())
    errors = runtime_errors(logs)
    report = {'schema_version': 'pm2_replay_probe_run/v1', 'pid': process.pid, 'exit_code': exit_code,
              'timed_out': timed_out, 'mutated_inputs': mutated, 'errors': errors,
              'purpose': spec['purpose'], 'input_origin': spec['input_origin'],
              'native_commands': events,
              'ended_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'replay_eof_logged': '[Replay] EOF' in logs,
              'command': {'path': 'command.json', 'sha256': file_hash(output/'command.json')},
              'files': {p.name: file_hash(p) for p in output.iterdir() if p.is_file()},
              'game_input_injected': False, 'formal_s2': False,
              'status': 'process_failed' if timed_out or exit_code or errors or mutated else 'process_returned_frame_verification_required'}
    if tape_target is not None:
        report['recorded_replay'] = ({'path': str(tape_target), 'sha256': file_hash(tape_target),
                                      'bytes': tape_target.stat().st_size, 'input_frame_audit_required': True}
                                     if tape_target.exists() else None)
    write_json(output/'report.json', report)
    return {k: report[k] for k in ('status', 'exit_code', 'timed_out', 'errors', 'mutated_inputs')}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('quarantine-root', 'spec', 'spec-sha256', 'output-directory'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--user-authorized-isolated-run', action='store_true')
    try:
        result = run(p.parse_args(argv))
        print(json.dumps(result))
        if result['status'] == 'process_failed':
            return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f'REPLAY_PROBE_ERROR: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
