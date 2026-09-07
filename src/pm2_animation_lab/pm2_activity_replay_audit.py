"""Read-only BSV2/RZIP/RASTATE inspection for the isolated PM2 research plan.

Independent format implementation; no upstream implementation or game bytes
are embedded here. Structure/header validity never means core restore success.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

from pm2_animation_lab.pm2_activity_video_index import confined, file_hash, write_json

MAX_BYTES = 256 * 1024 * 1024
MAX_FRAMES = 2_000_000
MAGIC = 0x42535632


class ReplayError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ReplayError(code)


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        require(0 <= n <= MAX_BYTES and self.pos + n <= len(self.data),
                f"truncated_or_oversized_at:{self.pos}")
        result = self.data[self.pos:self.pos+n]
        self.pos += n
        return result

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def item(self, depth=0):
        """Only the integer/binary/array MessagePack subset used by StateStream."""
        require(depth < 4, "msgpack_nesting")
        c = self.take(1)[0]
        if c < 128:
            return c
        sizes = {0xcc: ('>B', 1), 0xcd: ('>H', 2), 0xce: ('>I', 4), 0xcf: ('>Q', 8),
                 0xd0: ('>b', 1), 0xd1: ('>h', 2), 0xd2: ('>i', 4), 0xd3: ('>q', 8)}
        if c in sizes:
            return self.unpack(sizes[c][0])[0]
        if c in (0xc4, 0xc5, 0xc6):
            n = self.unpack({0xc4: '>B', 0xc5: '>H', 0xc6: '>I'}[c])[0]
            return self.take(n)
        if 0x90 <= c <= 0x9f:
            n = c - 0x90
        elif c in (0xdc, 0xdd):
            n = self.unpack('>H' if c == 0xdc else '>I')[0]
        else:
            raise ReplayError(f"unsupported_msgpack_token:{c}")
        require(n <= 1_000_000 and n <= len(self.data)-self.pos, "msgpack_array_limit")
        return [self.item(depth+1) for _ in range(n)]


def uint(value):
    require(type(value) is int and 0 <= value <= 0xffffffff, "expected_uint32")
    return value


def decode_statestream(encoded, size, block_size, super_size, tables):
    require(0 < block_size <= 65536 and 0 < super_size <= 1024, "block_geometry")
    blocks, supers = tables
    blocks.setdefault(0, bytes(block_size))
    supers.setdefault(0, [0]*super_size)
    r = Reader(encoded)
    require(r.item() == 0, "statestream_start")
    frame = uint(r.item())
    defined_blocks, defined_supers = set(), set()
    while r.pos < len(encoded):
        token = r.item()
        if token == 1:
            index, data = uint(r.item()), r.item()
            require(index != 0 and index not in defined_blocks and isinstance(data, bytes)
                    and len(data) == block_size, "invalid_block_definition")
            blocks[index] = data
            defined_blocks.add(index)
        elif token == 2:
            index, refs = uint(r.item()), r.item()
            require(index != 0 and index not in defined_supers and isinstance(refs, list)
                    and len(refs) == super_size, "invalid_superblock_definition")
            require(all(uint(i) in blocks for i in refs), "undefined_block")
            supers[index] = refs
            defined_supers.add(index)
        elif token == 3:
            refs = r.item()
            expected = (size + block_size*super_size - 1)//(block_size*super_size)
            require(isinstance(refs, list) and len(refs) == expected, "sequence_coverage")
            require(all(uint(i) in supers for i in refs), "undefined_superblock")
            require(r.pos == len(encoded), "statestream_trailing_data")
            result = bytearray()
            for i in refs:
                for j in supers[i]:
                    result.extend(blocks[j][:max(0, min(block_size, size-len(result)))])
            require(len(result) == size, "statestream_output_size")
            return bytes(result), frame
        else:
            raise ReplayError("statestream_update_token")
    raise ReplayError("statestream_missing_sequence")


def decompress(payload, compression, expected):
    require(0 < expected <= MAX_BYTES, "uncompressed_size_limit")
    if compression == 0:
        result = payload
    elif compression == 1:
        d = zlib.decompressobj()
        result = d.decompress(payload, expected+1)
        require(d.eof and not d.unused_data and not d.unconsumed_tail, "zlib_boundary")
    elif compression == 2:
        try:
            import zstandard
        except ImportError as exc:
            raise ReplayError("zstandard_dependency_required") from exc
        try:
            advertised = zstandard.frame_content_size(payload)
            require(advertised in (expected, zstandard.CONTENTSIZE_UNKNOWN),
                    'zstd_advertised_size_mismatch')
            result = zstandard.ZstdDecompressor().decompress(
                payload, max_output_size=expected, allow_extra_data=False)
        except zstandard.ZstdError as exc:
            raise ReplayError('invalid_zstandard_payload') from exc
    else:
        raise ReplayError("unsupported_compression")
    require(len(result) == expected, "decompression_size_mismatch")
    return result


def core_summary(raw):
    require(len(raw) >= 9, "core_header_truncated")
    magic, version, invalid, machine, memory, vram = struct.unpack_from('<IBBBBB', raw)
    return {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "magic_matches_dosbox_pure": magic == 0xd05b5747,
            "format_version": version, "invalid_state_flags": invalid,
            "machine_code": machine, "memory_code": memory, "vram_code": vram,
            "internal_layout_validated": False, "runtime_restore_validated": False}


def checkpoint(r, header, tables):
    begin = r.pos
    compression, encoding = r.unpack('<BB')
    size, encoded_size, compressed_size = r.unpack('<III')
    require(0 < size <= MAX_BYTES and 0 < compressed_size <= MAX_BYTES, "checkpoint_size_limit")
    encoded = decompress(r.take(compressed_size), compression, encoded_size)
    frame = None
    if encoding == 0:
        require(size == encoded_size, "raw_checkpoint_size")
        raw = encoded
    elif encoding == 1:
        raw, frame = decode_statestream(encoded, size, header[7], header[8], tables)
    else:
        raise ReplayError("unsupported_checkpoint_encoding")
    return raw, {"offset": begin, "end_offset": r.pos, "compression": compression,
                 "encoding": encoding, "encoded_frame": frame, "core": core_summary(raw)}


def inspect_replay(data):
    require(len(data) <= MAX_BYTES, "file_size_limit")
    r = Reader(data)
    header = r.unpack('<10I')
    require(header[0] == MAGIC and header[1] == 2, "unsupported_replay_header")
    result = {"format": "BSV2", "header_frame_count": header[6], "content_crc32": f"{header[2]:08x}",
              "initial_checkpoint_bytes": header[3], "input_frame_count": 0,
              "keyboard_event_count": 0, "input_query_count": 0, "nonzero_input_query_count": 0,
              "checkpoints": [], "core_restore_verified": False, "formal_s2": False}
    if len(data) == 40:
        result.update(status="header_only_rejected", recording_has_inputs=False)
        return result, None
    tables = ({}, {})
    raw, first = checkpoint(r, header, tables)
    require(r.pos == 40+header[3], "initial_checkpoint_length")
    result['checkpoints'].append(first)
    starts = set()
    while r.pos < len(data):
        require(result['input_frame_count'] < MAX_FRAMES, "frame_limit")
        start = r.pos
        back = r.unpack('<I')[0]
        require((not starts and back == 0) or (starts and start-back in starts), "frame_backref")
        starts.add(start)
        keycount = r.unpack('<B')[0]
        require(keycount <= 128, "keyboard_event_limit")
        r.take(12*keycount)
        count = r.unpack('<H')[0]
        require(count <= 512, "input_event_limit")
        inputs = r.take(8*count)
        result['keyboard_event_count'] += keycount
        result['input_query_count'] += count
        result['nonzero_input_query_count'] += sum(struct.unpack_from('<h', inputs, i*8+6)[0] != 0 for i in range(count))
        token = r.take(1)
        if token == b'C':
            _, cp = checkpoint(r, header, tables)
            result['checkpoints'].append(cp)
        elif token == b'c':
            n = r.unpack('<Q')[0]
            cpraw = r.take(n)
            result['checkpoints'].append({"offset": r.pos-n, "encoding": "legacy_raw", "core": core_summary(cpraw)})
        else:
            require(token == b'f', "invalid_frame_token")
        result['input_frame_count'] += 1
    require(result['input_frame_count'] == header[6], "header_frame_count_mismatch")
    result['recording_has_inputs'] = result['input_frame_count'] > 0
    result['status'] = 'structurally_decoded_restore_unknown' if result['recording_has_inputs'] else 'initial_checkpoint_only_rejected'
    return result, raw


def unwrap_state(data):
    require(len(data) <= MAX_BYTES, "state_file_size_limit")
    wrapper = 'raw'
    if data.startswith(b'#RZIPv\x01#'):
        wrapper = 'RZIP1'
        r = Reader(data)
        r.take(8)
        chunk_size, size = r.unpack('<IQ')
        require(0 < chunk_size <= MAX_BYTES and 0 < size <= MAX_BYTES, "rzip_size_limit")
        parts, done = [], 0
        while done < size:
            n = r.unpack('<I')[0]
            require(n > 0, "rzip_empty_chunk")
            part = decompress(r.take(n), 1, min(chunk_size, size-done))
            parts.append(part)
            done += len(part)
        require(r.pos == len(data), "rzip_trailing_data")
        data = b''.join(parts)
    if data.startswith(b'RASTATE\x01'):
        r = Reader(data)
        r.take(8)
        core, blocks, ended = None, [], False
        while r.pos < len(data):
            marker, n = r.unpack('<4sI')
            payload = r.take(n)
            r.take((-n) % 8)
            blocks.append({"tag_hex": marker.hex(), "size": n})
            if marker == b'MEM ':
                require(core is None, "duplicate_core_block")
                core = payload
            if marker == b'END ':
                require(n == 0 and r.pos == len(data), "rastate_end_boundary")
                ended = True
                break
        require(ended and core is not None, "rastate_missing_mem_or_end")
        return core, {"wrapper": wrapper, "container": "RASTATE1", "blocks": blocks, "core": core_summary(core)}
    return data, {"wrapper": wrapper, "container": "raw_core", "core": core_summary(data)}


def synthetic_idle_replay(core, frame_count, crc32):
    """A diagnostic NEW input tape, explicitly not recovered historical input."""
    require(0 < frame_count <= 3600, "synthetic_probe_frame_limit")
    require(type(crc32) is int and 0 <= crc32 <= 0xffffffff, "content_crc32_range")
    require(core_summary(core)['magic_matches_dosbox_pure'], "synthetic_probe_core_magic")
    cp = struct.pack('<BBIII', 0, 0, len(core), len(core), len(core)) + core
    header = struct.pack('<10I', MAGIC, 2, crc32, len(cp), 0, 0, frame_count, 128, 16, 0)
    frames = b''.join(struct.pack('<IBHc', 0 if i == 0 else 8, 0, 0, b'f') for i in range(frame_count))
    return header + cp + frames


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--quarantine-root', required=True)
    p.add_argument('--input', required=True)
    p.add_argument('--input-sha256', required=True)
    p.add_argument('--kind', choices=['replay', 'state'], required=True)
    p.add_argument('--output-directory', required=True)
    p.add_argument('--dependencies')
    p.add_argument('--synthetic-idle-frames', type=int)
    p.add_argument('--content-crc32')
    a = p.parse_args(argv)
    try:
        root = Path(a.quarantine_root).resolve()
        require(is_data_directory(root), 'existing_data_and_source_directories_required')
        if a.dependencies:
            dep = confined(a.dependencies, root)
            sys.path.insert(0, str(dep))
        path = confined(a.input, root)
        require(path.stat().st_size <= MAX_BYTES, 'input_size_limit')
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == a.input_sha256.lower(), 'input_hash')
        if a.kind == 'replay':
            report, _ = inspect_replay(data)
            core = None
        else:
            core, report = unwrap_state(data)
        probe = None
        if a.synthetic_idle_frames is not None:
            require(core is not None and a.content_crc32 is not None, 'synthetic_requires_state_and_crc')
            probe = synthetic_idle_replay(core, a.synthetic_idle_frames, int(a.content_crc32, 16))
        output = confined(a.output_directory, root)
        output.mkdir(parents=True, exist_ok=False)
        report.update(schema_version='pm2_replay_audit/v1', input={'path': str(path), 'sha256': a.input_sha256.lower()},
                      tool_sha256=file_hash(Path(__file__)), authorization_effect='none', formal_s2=False)
        if probe is not None:
            with (output/'synthetic_idle_NOT_HISTORICAL.replay').open('xb') as f:
                f.write(probe)
            report['synthetic_probe'] = {'path': 'synthetic_idle_NOT_HISTORICAL.replay',
                'sha256': hashlib.sha256(probe).hexdigest(), 'frames': a.synthetic_idle_frames,
                'input_origin': 'synthetic_no_input_diagnostic', 'historical_replay': False}
        if 'zstandard' in sys.modules:
            module = sys.modules['zstandard']
            report['zstandard'] = {'version': module.__version__, 'module_path': module.__file__,
                                  'module_sha256': file_hash(Path(module.__file__))}
            report['zstandard']['native_backends'] = [
                {'path': str(p), 'sha256': file_hash(p)}
                for p in sorted(Path(module.__file__).parent.glob('*.pyd'))]
        write_json(output/'report.json', report)
        print(json.dumps({k: report[k] for k in ('status', 'input_frame_count', 'synthetic_probe') if k in report}))
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        print(f'REPLAY_AUDIT_ERROR: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
