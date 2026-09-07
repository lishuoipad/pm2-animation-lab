"""Source-guarded adapters for scene structures outside WIDANIME.

Only self-authored format/semantic code is included. All art stays external.
These adapters reconstruct declared picture/map domains, not the whole game.
"""
from __future__ import annotations

from pathlib import Path
import struct

from PIL import Image
import numpy as np

from .pm2_activity_clock import exact_fields
from .pm2_activity_compositor import apply_masked_pattern
from .pm2_activity_delivery_check import DeliveryError, bound, digest, read_bound
from .pm2_activity_timeline import FIXED_SOURCE_COMMIT, verify_source_checkout
from .pm2_lbx_pt1 import (read_lbx_asset_from_zip, parse_pt1, decode_record_planes,
                         planes_to_indices, decode_mask_body_pair)

SOURCE_HASHES = {
    'KOSO/misc/LIBXX.BAT': '859b6a20eb3d075e5588ff5a705b3d71529d7d373950b8afcc73d34782812224',
    'KOSO2/PLSLD7.ASM': '2d70ca679a1d0d444280d996014fb96359d02aaf33e1c62604023974ddccebf9',
    'KOSOTEXT/RUNNING.TXT': '96a3f47632d6dca29191808d2ceddb78aa1b2ac010cc3a733ec19abac76c2576',
    'KOSOTEXT/SCNKYUSO.TXT': '4c666a95f6adbedd2fd391b57d700c947bb4f3f1e4990f15cb3bb88fb0e07ade',
    'KOSOTEXT/SCNVACAN.TXT': '4aa97f73030609527e0340a44fab023e1b23874738cdb426f17c4477d3371947',
    'KOSOTEXT/RPGRUN.TXT': '7c9d5fb950157ed2187954cf04031165a04d0bb11adf80b818d17eeaba4bd095',
    'KOSO4/WIDIVENT.ASM': '386ac829502d90b3ed84fe87c0aefb1e29487119f68c3fd4882449ed36926e8c',
    'KOSO4/WIDMUSYA.ASM': '36f5963a17605f19c41a75bceb030321537c4f8d61876d70304b8975b5a81dec',
    'KOSO7/VIEWMAP.ASM': '83477e255338ffa81bed4f4711d349535ff74852aea4a2fd6f86b9e2a3ccb0f2',
}

SCENES = {
    'FREE': {'structure': 'illustration_hold', 'variants': ['free', 'observe'],
             'source': 'SCNKYUSO', 'crop': [48, 130, 200, 128]},
    'VACATION': {'structure': 'masked_illustration_hold', 'variant_count': 32,
                 'source': 'SCNVACAN', 'crop': [104, 144, 432, 192]},
    'ADVENTURE_EAST': {'structure': 'tile_map_displacement', 'source': 'RPGRUN',
                       'crop': [64, 48, 448, 384]},
    'REST': {'structure': 'no_independent_illustration', 'publishable': False,
             'reason': 'SCNKYUSO:473 disables WWIVENT; statistics/UI only'},
    'SANATORIUM': {'structure': 'no_independent_illustration', 'publishable': False,
                   'reason': 'SCNKYUSO:482-489 has no illustration load'},
}


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise DeliveryError('scene_' + name)
    return value


def vacation_variant(conditions):
    exact_fields(conditions, ('age', 'season', 'destination'), 'vacation_conditions')
    age = integer(conditions['age'], 10, 18, 'age')
    season = integer(conditions['season'], 1, 4, 'season')
    destination = conditions['destination']
    if destination not in ('mountain', 'sea'):
        raise DeliveryError('scene_destination')
    # SCNVACAN:225-279: inclusive thresholds, each season mountain then sea.
    band = sum(age > limit for limit in (11, 13, 15))
    number = band * 8 + (season - 1) * 2 + 1 + (destination == 'sea')
    palette = (10 if season == 4 else 9) if destination == 'mountain' else (0, 11, 12, 13)[season-1]
    # LIBXX.BAT:1077-1109 packs V001-008 in V0, V009-032 in V2.
    return {'asset': f'V{number:03d}.PT1', 'library': 'V0.LBX' if number <= 8 else 'V2.LBX',
            'palette_record': palette}


def source_bindings(scene, source_root):
    if scene not in SCENES or SCENES[scene].get('publishable') is False:
        raise DeliveryError('scene_has_no_supported_visual_domain')
    profile = SCENES[scene]
    verify_source_checkout(source_root, FIXED_SOURCE_COMMIT, profile['source'])
    names = ['KOSOTEXT/RUNNING.TXT', 'KOSO/misc/LIBXX.BAT', f"KOSOTEXT/{profile['source']}.TXT"]
    names += {'FREE': ['KOSO4/WIDIVENT.ASM'], 'VACATION': [],
              'ADVENTURE_EAST': ['KOSO4/WIDMUSYA.ASM', 'KOSO7/VIEWMAP.ASM']}[scene]
    if scene in ('FREE', 'VACATION'): names.append('KOSO2/PLSLD7.ASM')
    refs = {}
    for name in names:
        path = (source_root / name).resolve()
        if not path.is_relative_to(source_root.resolve()) or digest(path) != SOURCE_HASHES[name]:
            raise DeliveryError('scene_fixed_source_changed:' + name)
        refs[name] = {'path': str(path), 'sha256': SOURCE_HASHES[name]}
    return refs


def compile_scene(request, source_root):
    scene, conditions = request['scene'], request['conditions']
    refs = source_bindings(scene, Path(source_root))
    profile = SCENES[scene]
    result = {'schema': 'pm2_scene_sequence/v1', 'scene': scene,
              'structure': profile['structure'], 'crop': profile['crop'],
              'sources': refs, 'source_commit': FIXED_SOURCE_COMMIT}
    if scene == 'FREE':
        exact_fields(conditions, ('mode',), 'free_conditions')
        if conditions['mode'] not in ('free', 'observe'):
            raise DeliveryError('scene_no_illustration_for_rest_mode')
        result['variant'] = {'asset': 'E009.PT1', 'library': 'E0.LBX', 'palette_record': 0}
        result['states'] = [{'hold': True}]
    elif scene == 'VACATION':
        result['variant'] = vacation_variant(conditions)
        result['states'] = [{'hold': True}]
    else:
        result['states'] = map_states(conditions)
        result['excluded_rectangles'] = [[432, 0, 16, 128]]
        result['exclusion_reason'] = 'VIEWMAP:4007-4011 reserves column27 rows0-7 for non-map UI'
    return result


def map_states(conditions):
    exact_fields(conditions, ('initial', 'steps'), 'map_conditions')
    initial = conditions['initial']
    exact_fields(initial, ('map_x', 'map_y', 'girl_x', 'girl_y', 'direction', 'phase', 'terrain_phase'), 'map_initial')
    current = dict(initial)
    for key, high in [('map_x', 172), ('map_y', 76), ('girl_x', 26), ('girl_y', 21),
                      ('direction', 3), ('phase', 1), ('terrain_phase', 1)]:
        integer(current[key], 0, high, key)
    states = [dict(current)]
    steps = conditions['steps']
    if not isinstance(steps, list) or len(steps) > 1000:
        raise DeliveryError('scene_map_steps')
    for step in steps:
        exact_fields(step, ('dx', 'dy', 'scroll_x', 'scroll_y', 'terrain_phase'), 'map_step')
        dx, dy = (integer(step[k], -1, 1, k) for k in ('dx', 'dy'))
        sx, sy = (integer(step[k], -1, 1, k) for k in ('scroll_x', 'scroll_y'))
        if sx not in (0, dx) or sy not in (0, dy):
            raise DeliveryError('scene_scroll_without_displacement')
        # World displacement, not elapsed video frames, advances GIRL_ANIME_NUM.
        if dx or dy:
            current['phase'] ^= 1
            current['direction'] = (1 if dy < 0 else 0) if dy else (2 if dx < 0 else 3)
        current['map_x'] += sx; current['map_y'] += sy
        current['girl_x'] += dx-sx; current['girl_y'] += dy-sy
        current['terrain_phase'] = integer(step['terrain_phase'], 0, 1, 'terrain_phase')
        for key, high in [('map_x', 172), ('map_y', 76), ('girl_x', 26), ('girl_y', 21)]:
            integer(current[key], 0, high, key)
        states.append(dict(current))
    return states


def decode_chip(bank, number):
    if len(bank) != 16384:
        raise DeliveryError('scene_bnk_size')
    integer(number, 0, 127, 'chip_number')
    block = bank[number*128:(number+1)*128]
    # PUT_CHIP_VGA writes four consecutive 32-byte planes, MSB at left.
    # BNK planes are row-major; PT1's byte-column-major converter is different.
    return bytes(sum(((block[p*32+y*2+x//8] >> (7-x%8)) & 1) << p
                     for p in range(4)) for y in range(16) for x in range(16))


def masked_chip(background, body, mask):
    # BUFF_MASK ANDs all four mask planes separately, then ORs the body.
    if not len(background) == len(body) == len(mask) == 256:
        raise DeliveryError('scene_chip_dimensions')
    return bytes((a & m) | b for a, b, m in zip(background, body, mask))


def incremental_map_copy(previous, following, actual, palette):
    """Fit a source-ordered VGA tile-copy prefix; never use captured pixels as output.

    VIEW_D_ALL visits changed tiles row-major. PUT_CHIP_VGA/PUT_BUFFER_VGA
    write four planes, each with 16 rows of two bytes. Identical tile writes
    are observationally indistinguishable and omitted from this canonical fit.
    """
    if len(previous) != 448*384 or len(following) != len(previous) or actual.size != (448, 384):
        raise DeliveryError('scene_incremental_copy_dimensions')
    before = np.frombuffer(previous, dtype=np.uint8).reshape(384, 448)
    target = np.frombuffer(following, dtype=np.uint8).reshape(384, 448)
    current = before.copy()
    colors = np.asarray(palette, dtype=np.uint8)
    observed = np.asarray(actual)
    different = np.any(colors[current] != observed, axis=2)
    different[:128, 432:] = False
    errors = int(different.sum())
    for ty in range(24):
        for tx in range(28):
            if tx == 27 and ty < 8: continue
            yy, xx = ty*16, tx*16
            if np.array_equal(before[yy:yy+16, xx:xx+16], target[yy:yy+16, xx:xx+16]): continue
            for plane in range(4):
                for row in range(16):
                    for byte_x in range(2):
                        y, x = yy+row, xx+byte_x*8
                        old_errors = int(different[y, x:x+8].sum())
                        current[y, x:x+8] = (current[y, x:x+8] & (15 ^ (1 << plane))) | (target[y, x:x+8] & (1 << plane))
                        different[y, x:x+8] = np.any(colors[current[y, x:x+8]] != observed[y, x:x+8], axis=1)
                        errors += int(different[y, x:x+8].sum())-old_errors
                        if errors == 0:
                            image = Image.fromarray(colors[current])
                            image.paste((24, 24, 24), (432, 0, 448, 128))
                            return image, {'tile_x': tx, 'tile_y': ty, 'plane': plane,
                                           'row': row, 'byte_columns_done': byte_x+1}
    return None


def scanout_map_copy(previous, following, actual, palette):
    """Four-part SVGA scanout interleaved with monotonically ordered tile writes.

    The caller must verify the fixed core, options and vga_draw.cpp evidence.
    Each part reads the then-current VRAM. Progress is observed, not predicted.
    """
    if len(previous) != 448*384 or len(following) != len(previous) or actual.size != (448, 384):
        raise DeliveryError('scene_scanout_dimensions')
    before = np.frombuffer(previous, dtype=np.uint8).reshape(384, 448)
    target = np.frombuffer(following, dtype=np.uint8).reshape(384, 448)
    current = before.copy()
    colors = np.asarray(palette, dtype=np.uint8)
    observed = np.asarray(actual)
    writes = []
    for ty in range(24):
        for tx in range(28):
            if tx == 27 and ty < 8: continue
            yy, xx = ty*16, tx*16
            if np.array_equal(before[yy:yy+16, xx:xx+16], target[yy:yy+16, xx:xx+16]): continue
            writes.extend((yy+r, xx+bx*8, p) for p in range(4) for r in range(16) for bx in range(2))
    # vga_draw.cpp: VGA_PARTS=4, 480 native rows, map crop starts at row48.
    bands = [(0, 72), (72, 192), (192, 312), (312, 384)]
    output = np.empty((384, 448, 3), dtype=np.uint8)
    cursor, progress = 0, []
    for start, end in bands:
        mismatch = np.any(colors[current[start:end]] != observed[start:end], axis=2)
        if start < 128: mismatch[:min(end, 128)-start, 432:] = False
        errors = int(mismatch.sum())
        while errors:
            if cursor == len(writes): return None
            y, x, plane = writes[cursor]; cursor += 1
            old_errors = int(mismatch[y-start, x:x+8].sum()) if start <= y < end else 0
            current[y, x:x+8] = (current[y, x:x+8] & (15 ^ (1 << plane))) | (target[y, x:x+8] & (1 << plane))
            if start <= y < end:
                mismatch[y-start, x:x+8] = np.any(colors[current[y, x:x+8]] != observed[y, x:x+8], axis=1)
                errors += int(mismatch[y-start, x:x+8].sum())-old_errors
        output[start:end] = colors[current[start:end]]
        progress.append({'first_row': start, 'end_row_exclusive': end, 'completed_byte_writes': cursor})
    image = Image.fromarray(output)
    image.paste((24, 24, 24), (432, 0, 448, 128))
    return image, {'scanout_parts': progress, 'claim': 'observed_monotonic_source_write_progress'}


def render_map(banks, map_data, attributes, state):
    if len(map_data) != 40000 or len(attributes) != 20000:
        raise DeliveryError('scene_east_map_dimensions')
    if set(banks) != set(range(7)) or any(len(b) != 16384 for b in banks.values()):
        raise DeliveryError('scene_map_banks')
    def chip(bank, number):
        return decode_chip(banks[bank], number)
    words = []
    attrs = []
    for y in range(24):
        for x in range(28):
            pos = (state['map_y']+y)*200 + state['map_x']+x
            word = struct.unpack_from('<H', map_data, pos*2)[0]
            if state['terrain_phase']:
                low = word & 1023
                for a, b in ((0x180, 0x1c0), (0x190, 0x1d0)):
                    if a <= low <= a+1: low += b-a; break
                    if b <= low <= b+1: low -= b-a; break
                word = (word & 0xfc00) | low
            words.append(word); attrs.append(attributes[pos])
    gx, gy = state['girl_x'], state['girl_y']
    # CHECK_EAST_EF: water at both foot chips needs a separate effect state.
    feet = [words[(gy+2)*28+gx+dx] & 1023 for dx in range(2)]
    if all(w in (0x180, 0x181, 0x190, 0x191, 0x1c0, 0x1c1, 0x1d0, 0x1d1) for w in feet):
        raise DeliveryError('scene_water_effect_not_supported')
    chars = {}
    start = state['direction']*4 + state['phase']*2
    for dx in range(2):
        over = True
        # Priority propagates upward from the tile underneath each foot column.
        for dy in (2, 1, 0):
            x, y = gx+dx, gy+dy
            below = (y+1)*28+x
            over = over and below < len(words) and bool(words[below] & 0xfc00)
            attr = attrs[y*28+x]
            if attr != 2:
                chars[y*28+x] = (start+dy*16+dx, False if attr == 6 else (over or attr == 3))
    canvas = bytearray(448*384)
    for i, word in enumerate(words):
        y, x = divmod(i, 28)
        if x == 27 and y < 8:
            continue
        bank, number, mask = (word >> 7) & 7, word & 127, word >> 10
        if bank == 7:
            raise DeliveryError('scene_unloaded_map_bank')
        pixels = chip(bank, number)
        actor = chars.get(i)
        def foreground(base):
            return masked_chip(base, chip(4, mask), chip(4, mask+64)) if mask else base
        if actor:
            number, over = actor
            if not over: pixels = foreground(pixels)
            pixels = masked_chip(pixels, chip(5, number), chip(5, number+48))
            if over: pixels = foreground(pixels)
        else:
            pixels = foreground(pixels)
        for row in range(16):
            pos = (y*16+row)*448+x*16
            canvas[pos:pos+16] = pixels[row*16:(row+1)*16]
    return bytes(canvas)


def compose_scene(sequence, request, root):
    archive = bound(request['archive'], root)
    metadata = {}
    def asset(library, name):
        payload, meta = read_lbx_asset_from_zip(archive, library, name,
                                               expected_archive_sha256=request['archive']['sha256'])
        metadata[library+'/'+name] = meta
        return payload
    scene = sequence['scene']
    if scene in ('FREE', 'VACATION'):
        variant = sequence['variant']
        records = parse_pt1(asset(variant['library'], variant['asset'])).records
        if len(records) != 1:
            raise DeliveryError('scene_illustration_record_count')
        record = records[0]
        size = (record.width_pixels, record.height_pixels)
        if list(size) != sequence['crop'][2:]:
            raise DeliveryError('scene_illustration_dimensions')
        frame = planes_to_indices(decode_record_planes(record), record.width_bytes, record.height_pixels)
        if scene == 'VACATION':
            front = parse_pt1(asset('FR5.LBX', 'FRMVACNM.PT1')).records
            if len(front) != 2:
                raise DeliveryError('scene_vacation_frame_records')
            frame = apply_masked_pattern(frame, *size,
                decode_mask_body_pair(front[0], front[1], mask_storage_plane_count=4),
                anchor_x_pixels=0, anchor_y_pixels=0, include_author_offset=False, clip=False)
        frames = [frame]
    else:
        banks = {i: asset('RPG.LBX', f'CIPE{i:02d}.BNK') for i in range(7)}
        map_data = asset('RPG.LBX', 'MAPE00.MAP')
        attributes = asset('RPG.LBX', 'MAPE00.ATR')
        frames = [render_map(banks, map_data, attributes, state) for state in sequence['states']]
        size = (448, 384)
    palette = read_bound(request['palette'], root)['entries']
    if (len(palette) != 16 or any(len(c) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in c) for c in palette)
            or len({tuple(c) for c in palette}) != 16):
        raise DeliveryError('scene_palette_rgb16')
    images = []
    for frame in frames:
        im = Image.frombytes('P', size, frame)
        im.putpalette(sum(palette, []) + [0]*(768-48))
        im = im.convert('RGB')
        for x, y, w, h in sequence.get('excluded_rectangles', []):
            im.paste((24, 24, 24), (x, y, x+w, y+h))
        images.append(im)
    return images, metadata
