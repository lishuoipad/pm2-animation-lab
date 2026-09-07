from pm2_animation_lab.paths import is_data_directory
"""Summarize the complete recorded libretro query interface, not OS input history."""
import argparse
from collections import Counter
import json
from pathlib import Path
import struct
import sys
from pm2_animation_lab.pm2_activity_replay_audit import Reader, checkpoint, inspect_replay, require
from pm2_animation_lab.pm2_activity_video_index import confined, file_hash, write_json


def census(data):
    audit, _ = inspect_replay(data)
    require(audit['recording_has_inputs'], 'recorded_inputs_required')
    reader = Reader(data)
    header = reader.unpack('<10I')
    tables = ({}, {})
    checkpoint(reader, header, tables)
    rows, shapes = {}, Counter()
    duplicates = conflicts = frames = keys = 0
    while reader.pos < len(data):
        reader.take(4)  # Backreferences already validated by inspect_replay.
        keycount = reader.unpack('<B')[0]
        reader.take(12 * keycount)
        keys += keycount
        count = reader.unpack('<H')[0]
        seen, shape = {}, []
        for _ in range(count):
            port, device, index, padding, button, value = reader.unpack('<BBBBHh')
            require(padding == 0, 'nonzero_input_padding')
            key = (port, device, index, button)
            if key in seen:
                duplicates += 1
                conflicts += seen[key] != value
            else:
                seen[key] = value
            shape.append(key)
            row = rows.setdefault(key, {'count': 0, 'nonzero': 0, 'minimum': value, 'maximum': value})
            row['count'] += 1
            row['nonzero'] += value != 0
            row['minimum'] = min(value, row['minimum'])
            row['maximum'] = max(value, row['maximum'])
        shapes[tuple(shape)] += 1
        token = reader.take(1)
        if token == b'C':
            checkpoint(reader, header, tables)
        elif token == b'c':
            reader.take(reader.unpack('<Q')[0])
        else:
            require(token == b'f', 'frame_token')
        frames += 1
    require(frames == audit['input_frame_count'] and keys == audit['keyboard_event_count'], 'census_accounting')
    require(sum(r['count'] for r in rows.values()) == audit['input_query_count'], 'query_accounting')
    return {'schema_version': 'pm2_input_census/v1', 'input_frames': frames,
            'input_queries': audit['input_query_count'], 'keyboard_events': keys,
            'queries': [{'port': k[0], 'device': k[1], 'index': k[2], 'id': k[3], **v}
                        for k, v in sorted(rows.items())],
            'query_shapes': [{'frames': n, 'queries_per_frame': len(shape),
                              'ordered_query_tuples': list(shape)} for shape, n in shapes.items()],
            'duplicate_queries_within_frames': duplicates, 'conflicting_duplicate_queries': conflicts,
            'boundary': 'All recorded core-facing queries. Not frontend hotkey history, actual RNG, or complete dormant mapping discovery.',
            'formal_s2': False, 'authorization_effect': 'none'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('quarantine-root', 'replay', 'replay-sha256', 'output-directory'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--dependencies')
    a = p.parse_args()
    try:
        root = Path(a.quarantine_root).resolve()
        require(is_data_directory(root), 'existing_data_and_source_directories_required')
        if a.dependencies:
            sys.path.insert(0, str(confined(a.dependencies, root)))
        source = confined(a.replay, root)
        expected = a.replay_sha256.lower()
        require(file_hash(source) == expected, 'replay_hash')
        result = census(source.read_bytes())
        require(file_hash(source) == expected, 'replay_changed')
        result['input'] = {'path': str(source), 'sha256': expected}
        result['tool_sha256'] = file_hash(Path(__file__))
        result['audit_dependency_sha256'] = file_hash(Path(__file__).with_name('pm2_activity_replay_audit.py'))
        output = confined(a.output_directory, root)
        output.mkdir(parents=True, exist_ok=False)
        write_json(output/'report.json', result)
        print(json.dumps({k: result[k] for k in ('input_frames', 'input_queries', 'keyboard_events', 'conflicting_duplicate_queries')}))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print('INPUT_CENSUS_ERROR: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
