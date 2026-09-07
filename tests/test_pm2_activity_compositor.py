#!/usr/bin/env python3
"""Original in-memory fixture tests for the strict activity compositor."""

from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm2_animation_lab.pm2_activity_compositor import (
    BackgroundBinding,
    BindingError,
    CompositionContract,
    PatternBinding,
    QuarantineError,
    TimelineContractError,
    build_binding_blocker_receipt,
    canonical_sha256,
    compose_activity_timeline,
    load_bindings,
    write_composition_result,
)
from pm2_animation_lab.pm2_lbx_pt1 import DecodedPattern


HASH_A = hashlib.sha256(b"fixture-a").hexdigest()
HASH_B = hashlib.sha256(b"fixture-b").hexdigest()
HASH_C = hashlib.sha256(b"fixture-c").hexdigest()


def pattern_binding(
    number: int,
    color_indices: bytes,
    mask_bits: bytes,
    *,
    width_bytes: int = 1,
    height: int = 1,
    author_x_raw: int = 1,
    author_y_raw: int = 0,
) -> PatternBinding:
    return PatternBinding(
        pattern_number=number,
        pattern=DecodedPattern(
            width_bytes=width_bytes,
            height_pixels=height,
            author_x_raw=author_x_raw,
            author_y_raw=author_y_raw,
            mask_bits=mask_bits,
            color_indices=color_indices,
        ),
        payload_sha256=HASH_B if number == 0 else HASH_C,
        mask_record_index=number * 2,
        body_record_index=number * 2 + 1,
    )


def draw(order: int, track: int, number: int, x: int, y: int, *, emitted: bool = True) -> dict:
    return {
        "execution_order": order,
        "sequence": order + 2,
        "track": track,
        "film_value": number + 1,
        "pattern_number": number,
        "emitted": emitted,
        "author_coordinate": {"x": x, "y": y},
        "cursor": {
            "before": 0,
            "used": 0,
            "after": 1,
            "wrapped_before_draw": False,
        },
        "displacement_from_previous_draw": {"known": False, "dx": 0, "dy": 0},
        "provenance": {
            "anim_num": {"source_ref": 0, "routine": "ANMT001", "line": 10 + order},
            "aput": {"source_ref": 0, "routine": "ANMT001", "line": 10 + order},
            "film_value": {"source_ref": 0, "routine": "ANIME_INIT", "line": 5},
            "author_x": {"source_ref": 0, "routine": "ANIME_INIT", "line": 2},
            "author_y": {"source_ref": 0, "routine": "ANIME_INIT", "line": 3},
        },
    }


def tick(index: int, draws: list[dict]) -> dict:
    return {
        "tick": index,
        "background_restore": {
            "sequence": 0,
            "opcode": 4,
            "window": 0,
            "provenance": {"source_ref": 0, "routine": "ANMT001", "line": 1},
        },
        "timer": {
            "sequence": 1,
            "logical_wait": 8,
            "provenance": {"source_ref": 0, "routine": "ANMT001", "line": 2},
        },
        "draw_operations": draws,
        "composite_track_order": [item["track"] for item in draws if item["emitted"]],
        "cursor_changes": [],
        "position_changes": [],
        "state_changes": [],
        "wait": {
            "sequence": len(draws) + 3,
            "provenance": {"source_ref": 0, "routine": "ANMT001", "line": 20},
        },
        "frame_copy": {
            "sequence": len(draws) + 4,
            "opcode": 5,
            "window": 0,
            "provenance": {"source_ref": 0, "routine": "ANMT001", "line": 21},
        },
    }


def timeline(ticks: list[dict]) -> dict:
    return {
        "schema_version": "pm2_activity_timeline/v1",
        "scene": {"scene_id": "FIXTURE", "source_commit": "0" * 40, "source_ref": 0},
        "sources": [{"source_ref": 0, "sha256": HASH_A, "normalized_sha256": HASH_A}],
        "ticks": ticks,
    }


def background() -> BackgroundBinding:
    return BackgroundBinding(
        indices=bytes([1] * 64),
        width=32,
        height=2,
        payload_sha256=HASH_A,
        record_index=0,
    )


def contract(clip_policy: str = "canvas") -> CompositionContract:
    return CompositionContract(
        timeline_x_unit_pixels=8,
        timeline_y_unit_pixels=1,
        pattern_author_offset_mode="function7_coordinate_override",
        pattern_record_binding_mode="zero_based_mask_body_pairs",
        clip_policy=clip_policy,
        bit_order="msb_left",
        calibration_status="source_candidate",
    )


def pt1_record(
    attribute: int,
    payload: bytes,
    *,
    author_x: int = 0,
    author_y: int = 0,
    width_bytes: int = 1,
    height: int = 1,
) -> bytes:
    return struct.pack(
        "<6H", attribute, author_x, author_y, width_bytes, height, len(payload)
    ) + payload


def ple_literal(payload: bytes) -> bytes:
    if len(payload) % 2:
        raise AssertionError("fixture PLE literal must contain whole words")
    return struct.pack("<H", len(payload) // 2) + payload + b"\0\0"


class ActivityCompositorTests(unittest.TestCase):
    def test_foreground_offsets_and_source_provenance_are_enforced(self):
        import dataclasses
        fg = dataclasses.replace(pattern_binding(1000,bytes([2]*8),bytes([0]*8)),
            mask_record_index=0,body_record_index=1)
        d = draw(0,1000,1000,1,0)
        d['source_operation']={'opcode':17,'bank':0,'record':1}
        source=timeline([tick(0,[d])]);source['scene']['scene_id']='JOB006'
        result=compose_activity_timeline(source,background(),{1000:fg},contract(),binding_manifest_sha256=HASH_C)
        self.assertEqual(result.frames[0][8:16],bytes([1]*8))
        self.assertEqual(result.frames[0][16:24],bytes([2]*8))
        d['source_operation']['opcode']=7
        with self.assertRaises(BindingError):
            compose_activity_timeline(source,background(),{1000:fg},contract(),binding_manifest_sha256=HASH_C)

    def test_background_restore_author_offset_mask_body_and_order(self) -> None:
        replace = pattern_binding(0, bytes([2] * 8), bytes([0] * 8))
        overlay = pattern_binding(
            1,
            bytes([4] + [0] * 7),
            bytes([0] + [1] * 7),
        )
        source = timeline(
            [
                tick(0, [draw(0, 3, 0, 0, 0), draw(1, 4, 1, 0, 0)]),
                tick(1, [draw(0, 3, 0, 1, 0)]),
            ]
        )
        result = compose_activity_timeline(
            source,
            background(),
            {0: replace, 1: overlay},
            contract(),
            binding_manifest_sha256=HASH_C,
        )
        self.assertEqual(result.frames[0][0], 4)
        self.assertEqual(result.frames[0][1:8], bytes([2] * 7))
        # Tick 1 starts from the background; tick 0's X=0 drawing does not leak.
        self.assertEqual(result.frames[1][0], 1)
        self.assertEqual(result.frames[1][8:16], bytes([2] * 8))
        operation = result.manifest["frames"][0]["operations"][0]
        self.assertEqual(operation["anchor_pixels"], {"x": 0, "y": 0})
        self.assertEqual(
            operation["pattern_authored_offset"],
            {
                "x": 8,
                "y": 0,
                "applied": False,
                "reason": "function7_coordinate_override",
            },
        )
        self.assertEqual(operation["destination_rect_before_clip"]["x"], 0)
        self.assertEqual(result.manifest["used_pattern_numbers"], [0, 1])

    def test_negative_pattern_is_skipped_without_binding(self) -> None:
        skipped = draw(0, 7, -1, 0, 0, emitted=False)
        source = timeline([tick(0, [skipped])])
        result = compose_activity_timeline(
            source,
            background(),
            {},
            contract(),
            binding_manifest_sha256=HASH_B,
        )
        self.assertEqual(result.frames[0], background().indices)
        self.assertEqual(
            result.manifest["frames"][0]["operations"][0]["status"],
            "skipped_negative_pattern",
        )

    def test_explicit_canvas_crop_and_reject_policy(self) -> None:
        wide = pattern_binding(
            2,
            bytes([6] * 16),
            bytes([0] * 16),
            width_bytes=2,
            author_x_raw=0,
        )
        source = timeline([tick(0, [draw(0, 2, 2, 3, 0)])])
        result = compose_activity_timeline(
            source,
            background(),
            {2: wide},
            contract("canvas"),
            binding_manifest_sha256=HASH_B,
        )
        operation = result.manifest["frames"][0]["operations"][0]
        self.assertEqual(operation["cropped_pixels"], 8)
        self.assertEqual(operation["applied_rect"], {"x": 24, "y": 0, "width": 8, "height": 1})
        self.assertEqual(result.frames[0][24:32], bytes([6] * 8))
        with self.assertRaises(BindingError):
            compose_activity_timeline(
                source,
                background(),
                {2: wide},
                contract("reject"),
                binding_manifest_sha256=HASH_B,
            )

    def test_missing_binding_and_fully_clipped_pattern_fail(self) -> None:
        source = timeline([tick(0, [draw(0, 1, 0, 0, 0)])])
        with self.assertRaises(BindingError):
            compose_activity_timeline(
                source,
                background(),
                {},
                contract(),
                binding_manifest_sha256=HASH_B,
            )
        bound = pattern_binding(0, bytes([2] * 8), bytes([0] * 8))
        offscreen = timeline([tick(0, [draw(0, 1, 0, 4, 0)])])
        with self.assertRaises(BindingError):
            compose_activity_timeline(
                offscreen,
                background(),
                {0: bound},
                contract(),
                binding_manifest_sha256=HASH_B,
            )

    def test_timeline_order_and_coordinate_contract_fail_closed(self) -> None:
        item = draw(0, 1, 0, 0, 0)
        broken = timeline([tick(0, [item])])
        broken["ticks"][0]["composite_track_order"] = [99]
        with self.assertRaises(TimelineContractError):
            build_binding_blocker_receipt(broken)

        source = timeline([tick(0, [item])])
        bound = pattern_binding(0, bytes([2] * 8), bytes([0] * 8))
        bad_contract = CompositionContract(
            1,
            1,
            "function7_coordinate_override",
            "zero_based_mask_body_pairs",
            "canvas",
            "msb_left",
            "source_candidate",
        )
        with self.assertRaises(BindingError):
            compose_activity_timeline(
                source,
                background(),
                {0: bound},
                bad_contract,
                binding_manifest_sha256=HASH_B,
            )

    def test_blocker_receipt_and_composition_are_deterministic_and_redacted(self) -> None:
        source = timeline([tick(0, [draw(0, 1, 0, 0, 0)])])
        blocker = build_binding_blocker_receipt(source)
        self.assertEqual(blocker["status"], "blocked")
        self.assertEqual(blocker["required_pattern_numbers"], [0])
        self.assertEqual(len(blocker["reason_codes"]), 4)

        bound = pattern_binding(0, bytes([2] * 8), bytes([0] * 8))
        first = compose_activity_timeline(
            source,
            background(),
            {0: bound},
            contract(),
            binding_manifest_sha256=HASH_B,
        )
        second = compose_activity_timeline(
            source,
            background(),
            {0: bound},
            contract(),
            binding_manifest_sha256=HASH_B,
        )
        self.assertEqual(first.frames, second.frames)
        self.assertEqual(first.manifest, second.manifest)
        self.assertEqual(canonical_sha256(first.manifest), canonical_sha256(second.manifest))
        encoded = json.dumps(first.manifest, ensure_ascii=True, sort_keys=True)
        self.assertNotIn("payload_path", encoded)
        self.assertNotIn("color_indices", encoded)
        self.assertNotIn("mask_bits", encoded)
        self.assertNotIn(".PT1", encoded)

    def test_explicit_manifest_decoding_and_index_file_write(self) -> None:
        source = timeline([tick(0, [draw(0, 1, 0, 0, 0)])])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            # 32x2 background, four uncompressed planes, index 1 everywhere.
            background_planes = bytes([0xFF] * 8 + [0] * 24)
            background_payload = pt1_record(
                1, background_planes, width_bytes=4, height=2
            )
            # 8x2 opaque mask/body pair.  Nonzero authored X proves function 7
            # still uses the timeline coordinate override.
            mask_payload = ple_literal(bytes([0, 0]))
            body_planes = bytes([0, 0, 0xFF, 0xFF] + [0] * 4)
            pattern_payload = pt1_record(
                3,
                mask_payload,
                author_x=6,
                width_bytes=1,
                height=2,
            ) + pt1_record(
                2,
                ple_literal(body_planes),
                author_x=6,
                width_bytes=1,
                height=2,
            )
            background_path = root / "background.bin"
            pattern_path = root / "patterns.bin"
            background_path.write_bytes(background_payload)
            pattern_path.write_bytes(pattern_payload)
            manifest_data = {
                "schema_version": "pm2_activity_compositor_bindings/v1",
                "scene_id": "FIXTURE",
                "timeline_sha256": canonical_sha256(source),
                "canvas": {"width": 32, "height": 2},
                "coordinate_contract": {
                    "timeline_x_unit_pixels": 8,
                    "timeline_y_unit_pixels": 1,
                    "pattern_author_offset_mode": "function7_coordinate_override",
                    "clip_policy": "canvas",
                    "bit_order": "msb_left",
                    "calibration_status": "source_candidate",
                },
                "pattern_record_binding_mode": "zero_based_mask_body_pairs",
                "background": {
                    "payload_path": background_path.name,
                    "payload_sha256": hashlib.sha256(background_payload).hexdigest(),
                    "record_index": 0,
                },
                "patterns": [
                    {
                        "pattern_number": 0,
                        "payload_path": pattern_path.name,
                        "payload_sha256": hashlib.sha256(pattern_payload).hexdigest(),
                        "mask_record_index": 0,
                        "body_record_index": 1,
                    }
                ],
            }
            manifest_path = root / "bindings.json"
            manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
            with mock.patch(
                "pm2_animation_lab.pm2_activity_compositor._resolve_quarantine_root",
                return_value=root,
            ):
                loaded = load_bindings(manifest_path, root, source)
                result = compose_activity_timeline(
                    source,
                    loaded.background,
                    loaded.patterns,
                    loaded.contract,
                    binding_manifest_sha256=loaded.manifest_sha256,
                )
                self.assertEqual(result.frames[0][0:8], bytes([2] * 8))
                self.assertEqual(result.frames[0][8:16], bytes([1] * 8))
                output = root / "output"
                receipt = write_composition_result(result, output, root)
                self.assertEqual(receipt["frame_count"], 1)
                self.assertEqual((output / "tick_0000.idx").read_bytes(), result.frames[0])
                written_manifest = json.loads((output / "manifest.json").read_text("ascii"))
                self.assertEqual(written_manifest["frames"][0]["index_file"], "tick_0000.idx")
                with self.assertRaises(QuarantineError):
                    write_composition_result(result, output, root)

                failed_output = root / "failed_output"
                original_open = Path.open

                def fail_manifest_write(path, *args, **kwargs):
                    if path.name == "manifest.json":
                        raise OSError("simulated manifest write failure")
                    return original_open(path, *args, **kwargs)

                with mock.patch.object(Path, "open", new=fail_manifest_write):
                    with self.assertRaisesRegex(QuarantineError, "output_write"):
                        write_composition_result(result, failed_output, root)
                self.assertFalse(failed_output.exists())
                self.assertEqual(list(root.glob(".pm2_composition_*")), [])


if __name__ == "__main__":
    unittest.main()
