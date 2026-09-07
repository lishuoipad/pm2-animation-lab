"""Headless DOSBox Pure capture. Native PNG/JSON only; no video publication.

Input goes to emulated libretro devices, never to the desktop. A core is native
code supplied explicitly by the caller. This module does not download a core.
"""
from __future__ import annotations
import argparse
import ctypes as C
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

from PIL import Image

from .paths import is_data_directory
from .pm2_activity_clock import exact_fields
from .pm2_activity_delivery_check import DeliveryError, bound, confined, digest
from .pm2_activity_replay_audit import unwrap_state


def binding(path):
    return {'path': str(Path(path).resolve()), 'sha256': digest(path)}


def validate_action(action):
    if not isinstance(action, dict) or not set(action) <= {'op', 'frames', 'mouse', 'release'}:
        raise DeliveryError('native_action_fields')
    if action.get('op') not in ('step', 'record'):
        raise DeliveryError('native_action_operation')
    n = action.get('frames', 1)
    if type(n) is not int or not 1 <= n <= 3600 or type(action.get('release', False)) is not bool:
        raise DeliveryError('native_action_frames_or_release')
    mouse = action.get('mouse', {})
    if not isinstance(mouse, dict) or not set(mouse) <= {'0', '1', '2', '3'}:
        raise DeliveryError('native_mouse_fields')
    for key, value in mouse.items():
        low, high = (-32767, 32767) if key in ('0', '1') else (0, 1)
        if type(value) is not int or not low <= value <= high:
            raise DeliveryError('native_mouse_value')
    return n + (10 if action.get('release') else 0)


def validate_request(request, root):
    exact_fields(request, ('schema', 'core', 'content', 'content_files', 'anchor', 'options',
                           'boot_frames', 'settle_frames', 'actions'), 'native_capture_request')
    if request['schema'] != 'pm2_native_capture_request/v1':
        raise DeliveryError('native_capture_schema')
    for key in ('core', 'content', 'anchor', 'options'):
        bound(request[key], root)
    content = bound(request['content'], root)
    if content.suffix.lower() != '.m3u':
        raise DeliveryError('native_explicit_m3u_required')
    members = []
    for line in content.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            members.append(confined(content.parent / line, root))
    if not members or len(set(members)) != len(members):
        raise DeliveryError('native_content_members')
    if [bound(ref, root) for ref in request['content_files']] != members:
        raise DeliveryError('native_transitive_content_binding')
    for key in ('boot_frames', 'settle_frames'):
        if type(request[key]) is not int or not 1 <= request[key] <= 3600:
            raise DeliveryError('native_boot_or_settle_frames')
    actions = request['actions']
    if not isinstance(actions, list) or not 1 <= len(actions) <= 10000:
        raise DeliveryError('native_actions_required')
    total = sum(validate_action(a) for a in actions)
    if total > 1000000 or sum(a['op'] == 'record' for a in actions) != 1:
        raise DeliveryError('native_bounded_single_recording_required')
    return total


class Variable(C.Structure):
    _fields_ = [('key', C.c_char_p), ('value', C.c_char_p)]


class Game(C.Structure):
    _fields_ = [('path', C.c_char_p), ('data', C.c_void_p), ('size', C.c_size_t), ('meta', C.c_char_p)]


class Geometry(C.Structure):
    _fields_ = [('base_width', C.c_uint), ('base_height', C.c_uint), ('max_width', C.c_uint),
                ('max_height', C.c_uint), ('aspect_ratio', C.c_float)]


class Timing(C.Structure):
    _fields_ = [('fps', C.c_double), ('sample_rate', C.c_double)]


class AV(C.Structure):
    _fields_ = [('geometry', Geometry), ('timing', Timing)]


def capture(request_path, output, root):
    root = Path(root).resolve()
    if not is_data_directory(root):
        raise DeliveryError('native_external_data_root_required')
    request_path = confined(request_path, root)
    request = json.loads(request_path.read_text(encoding='utf-8'))
    validate_request(request, root)
    output = confined(output, root)
    if output.exists() or 'game' in {s.lower() for s in output.parts}:
        raise DeliveryError('native_new_isolated_output_required')
    output.mkdir(parents=True)
    for name in ('system', 'saves', 'frames'):
        (output/name).mkdir()
    paths = {name: str(output/name).encode() for name in ('system', 'saves')}
    options_path = bound(request['options'], root)
    options = {k.encode(): v.encode() for k, v in re.findall(r'(\w+)\s*=\s*"([^"]*)"', options_path.read_text())}
    if not options:
        raise DeliveryError('native_core_options_required')
    core = C.CDLL(str(bound(request['core'], root)))
    callbacks = []
    last = None
    pixel_format = 1
    recording = False
    controls = {}
    errors = []
    rows, unique, action_rows = [], {}, []
    callback_count = 0

    def environment(cmd, data):
        nonlocal pixel_format
        cmd &= 0xffff
        if cmd in (9, 30, 31):
            C.cast(data, C.POINTER(C.c_char_p))[0] = paths['saves' if cmd == 31 else 'system']; return True
        if cmd == 10:
            pixel_format = C.cast(data, C.POINTER(C.c_uint))[0]; return pixel_format == 1
        if cmd == 15:
            variable = C.cast(data, C.POINTER(Variable)).contents
            variable.value = options.get(variable.key); return variable.value is not None
        if cmd == 17:
            C.cast(data, C.POINTER(C.c_bool))[0] = False; return True
        if cmd in (39, 52):
            C.cast(data, C.POINTER(C.c_uint))[0] = 0; return True
        if cmd == 47:
            C.cast(data, C.POINTER(C.c_int))[0] = 3; return True
        # Keyboard callback is deliberately unavailable: this adapter is mouse-only.
        return cmd in (1, 6, 11, 16, 18, 21, 32, 35, 36, 37, 44, 51, 53, 55, 65, 69)

    def video(data, width, height, pitch):
        nonlocal last, callback_count
        try:
            callback_count += 1
            if data:
                if pixel_format != 1 or (width, height) != (640, 480) or not width*4 <= pitch <= width*4+4096:
                    raise DeliveryError('native_video_geometry_or_format')
                last = Image.frombytes('RGB', (width, height), C.string_at(data, pitch*height), 'raw', 'BGRX', pitch)
            if recording:
                if last is None:
                    raise DeliveryError('native_duplicate_without_frame')
                sha = hashlib.sha256(last.tobytes()).hexdigest()
                if sha not in unique:
                    path = output/'frames'/(sha+'.png'); last.save(path)
                    unique[sha] = {'path': 'frames/'+path.name, 'sha256': digest(path)}
                rows.append({'frame': len(rows), 'pts': len(rows), 'duration': 1, 'rgb_sha256': sha})
        except Exception as error:
            errors.append(str(error))

    signatures = [
        ('environment', C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p), environment),
        ('video_refresh', C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t), video),
        ('audio_sample', C.CFUNCTYPE(None, C.c_int16, C.c_int16), lambda l, r: None),
        ('audio_sample_batch', C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t), lambda data, n: n),
        ('input_poll', C.CFUNCTYPE(None), lambda: None),
        ('input_state', C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint),
         lambda port, device, index, key: controls.get(key, 0) if device == 2 else 0),
    ]
    for name, signature, function in signatures:
        callback = signature(function); callbacks.append(callback)
        setter = getattr(core, 'retro_set_'+name); setter.argtypes = [signature]; setter(callback)
    core.retro_load_game.argtypes = [C.POINTER(Game)]; core.retro_load_game.restype = C.c_bool
    core.retro_unserialize.argtypes = [C.c_void_p, C.c_size_t]; core.retro_unserialize.restype = C.c_bool
    core.retro_get_system_av_info.argtypes = [C.POINTER(AV)]
    core.retro_init()
    loaded = False
    def run_frames(n):
        for _ in range(n):
            before = callback_count
            core.retro_run()
            if errors or callback_count != before+1:
                raise DeliveryError('native_callback_failed:' + str(errors or 'one video callback per run required'))
    try:
        game = Game(str(bound(request['content'], root)).encode(), None, 0, None)
        loaded = bool(core.retro_load_game(C.byref(game)))
        if not loaded: raise DeliveryError('native_load_game_failed')
        run_frames(request['boot_frames'])
        raw, _ = unwrap_state(bound(request['anchor'], root).read_bytes())
        buffer = C.create_string_buffer(raw)
        if not core.retro_unserialize(buffer, len(raw)):
            raise DeliveryError('native_anchor_restore_failed')
        run_frames(request['settle_frames'])
        av = AV(); core.retro_get_system_av_info(C.byref(av))
        if not 1 <= av.timing.fps <= 1000:
            raise DeliveryError('native_invalid_reported_fps')
        tb = 1 / Fraction.from_float(av.timing.fps)
        for number, action in enumerate(request['actions'], 1):
            if action['op'] == 'record': recording = True
            controls = {int(k): v for k, v in action.get('mouse', {}).items()}
            run_frames(action.get('frames', 1)); controls = {}
            if action.get('release'): run_frames(10)
            if last is None: raise DeliveryError('native_no_video')
            snapshot = output/f'observed_{number:03d}.png'; last.save(snapshot)
            action_rows.append({'step': number, 'input': action, 'frame_count': callback_count,
                                'recorded_frames': len(rows), 'image': snapshot.name,
                                'image_sha256': digest(snapshot)})
        # Recheck every transitive input after running native code.
        validate_request(request, root)
        index = {'schema': 'pm2_libretro_native_frames/v1', 'core': request['core'],
                 'content': request['content'], 'anchor': request['anchor'],
                 'fps_hex': av.timing.fps.hex(), 'timebase': [tb.numerator, tb.denominator],
                 'frame_count': len(rows), 'frame_size': [640, 480], 'frames': rows, 'unique_images': unique,
                 'timing_claim': 'emulator_reported_frame_cadence', 'pixel_format': 'XRGB8888'}
        (output/'native_frames.json').write_text(json.dumps(index, indent=2), encoding='utf-8')
        (output/'actions.json').write_text(json.dumps(action_rows, indent=2), encoding='utf-8')
        # Preserve the producing runner when the installed tool is upgraded.
        (output/'runner_snapshot.py').write_bytes(Path(__file__).read_bytes())
        receipt = {'schema': 'pm2_native_capture/v1', 'request': binding(request_path),
                   'runner': binding(output/'runner_snapshot.py'), 'index': binding(output/'native_frames.json'),
                   'actions': binding(output/'actions.json'), 'timing_claim': 'emulator_reported_frame_cadence',
                   'desktop_input_used': False, 'audio_captured': False}
        (output/'capture.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
        return {'status': 'native_capture_saved', 'capture': binding(output/'capture.json'), 'frames': len(rows)}
    except Exception as error:
        (output/'FAILED.json').write_text(json.dumps({'error': str(error)}), encoding='utf-8')
        raise
    finally:
        if loaded: core.retro_unload_game()
        core.retro_deinit()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--quarantine-root', required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(capture(args.request, args.output, args.quarantine_root)))
        return 0
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, 'NATIVE_CAPTURE_REJECTED:'+str(error)+'\n')
