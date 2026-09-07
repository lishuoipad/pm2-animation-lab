#!/usr/bin/env python3
"""Strict indexed-frame compositor for redacted PM2 activity timelines.

Real PM2 payloads and composed frames are accepted only through a external-data
research quarantine.  Repository tests use in-memory original fixtures.  The
module emits indexed bytes and hash-only manifests; it never embeds a palette,
converts to RGB, scales, interpolates, or guesses PT1 record bindings.
"""

from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pm2_animation_lab.pm2_lbx_pt1 import (
    DecodedPattern,
    FormatError,
    apply_masked_pattern,
    decode_mask_body_pair,
    decode_record_planes,
    parse_pt1,
    planes_to_indices,
)


TIMELINE_SCHEMA = "pm2_activity_timeline/v1"
BINDING_SCHEMA = "pm2_activity_compositor_bindings/v1"
MANIFEST_SCHEMA = "pm2_activity_composition_manifest/v1"
BLOCKER_SCHEMA = "pm2_activity_composition_blocker/v1"
RECEIPT_SCHEMA = "pm2_activity_composition_receipt/v1"
ALLOWED_CALIBRATION_STATUS = {"source_candidate", "runtime_calibrated"}
ALLOWED_CLIP_POLICIES = {"canvas", "reject"}
MAX_CANVAS_PIXELS = 4096 * 4096


class CompositionError(RuntimeError):
    code = "COMPOSITION_ERROR"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}:{detail}" if detail else self.code)


class BindingError(CompositionError):
    code = "BINDING_ERROR"


class TimelineContractError(CompositionError):
    code = "TIMELINE_CONTRACT_ERROR"


class QuarantineError(CompositionError):
    code = "QUARANTINE_BOUNDARY_ERROR"


@dataclass(frozen=True)
class BackgroundBinding:
    indices: bytes
    width: int
    height: int
    payload_sha256: str
    record_index: int


@dataclass(frozen=True)
class PatternBinding:
    pattern_number: int
    pattern: DecodedPattern
    payload_sha256: str
    mask_record_index: int
    body_record_index: int


@dataclass(frozen=True)
class CompositionContract:
    timeline_x_unit_pixels: int
    timeline_y_unit_pixels: int
    pattern_author_offset_mode: str
    pattern_record_binding_mode: str
    clip_policy: str
    bit_order: str
    calibration_status: str


@dataclass(frozen=True)
class LoadedBindings:
    background: BackgroundBinding
    patterns: Mapping[int, PatternBinding]
    contract: CompositionContract
    manifest_sha256: str


@dataclass(frozen=True)
class CompositionResult:
    frames: tuple[bytes, ...]
    manifest: dict[str, Any]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes | bytearray) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise BindingError(f"sha256:{field}")
    return value.lower()


def _validate_contract(contract: CompositionContract) -> None:
    if contract.timeline_x_unit_pixels != 8 or contract.timeline_y_unit_pixels != 1:
        raise BindingError("timeline_coordinate_units")
    if contract.pattern_author_offset_mode != "function7_coordinate_override":
        raise BindingError("author_offset_mode")
    if contract.pattern_record_binding_mode != "zero_based_mask_body_pairs":
        raise BindingError("pattern_record_binding_mode")
    if contract.clip_policy not in ALLOWED_CLIP_POLICIES:
        raise BindingError("clip_policy")
    if contract.bit_order not in {"msb_left", "lsb_left"}:
        raise BindingError("bit_order")
    if contract.calibration_status not in ALLOWED_CALIBRATION_STATUS:
        raise BindingError("calibration_status")


def _validate_timeline(timeline: Mapping[str, Any]) -> tuple[str, list[Mapping[str, Any]]]:
    if timeline.get("schema_version") != TIMELINE_SCHEMA:
        raise TimelineContractError("schema_version")
    scene = timeline.get("scene")
    if not isinstance(scene, Mapping) or not isinstance(scene.get("scene_id"), str):
        raise TimelineContractError("scene")
    scene_id = scene["scene_id"]
    sources = timeline.get("sources")
    if not isinstance(sources, list) or not sources:
        raise TimelineContractError("sources")
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise TimelineContractError(f"source:{index}")
        _require_sha256(source.get("sha256"), f"source:{index}")

    ticks = timeline.get("ticks")
    if not isinstance(ticks, list) or not ticks:
        raise TimelineContractError("ticks")
    for tick_index, tick in enumerate(ticks):
        if not isinstance(tick, Mapping) or tick.get("tick") != tick_index:
            raise TimelineContractError(f"tick:{tick_index}:index")
        background = tick.get("background_restore")
        frame_copy = tick.get("frame_copy")
        wait = tick.get("wait")
        if not isinstance(background, Mapping) or (
            background.get("opcode"), background.get("window")
        ) != (4, 0):
            raise TimelineContractError(f"tick:{tick_index}:background")
        if not isinstance(frame_copy, Mapping) or (
            frame_copy.get("opcode"), frame_copy.get("window")
        ) != (5, 0):
            raise TimelineContractError(f"tick:{tick_index}:frame_copy")
        if not isinstance(wait, Mapping):
            raise TimelineContractError(f"tick:{tick_index}:wait")
        envelope_sequences = (
            background.get("sequence"),
            wait.get("sequence"),
            frame_copy.get("sequence"),
        )
        if not all(_is_integer(value) for value in envelope_sequences):
            raise TimelineContractError(f"tick:{tick_index}:envelope_sequence")
        if not envelope_sequences[0] < envelope_sequences[1] < envelope_sequences[2]:
            raise TimelineContractError(f"tick:{tick_index}:envelope_order")

        draws = tick.get("draw_operations")
        if not isinstance(draws, list):
            raise TimelineContractError(f"tick:{tick_index}:draws")
        emitted_tracks: list[int] = []
        prior_sequence = envelope_sequences[0]
        for draw_index, draw in enumerate(draws):
            if not isinstance(draw, Mapping) or draw.get("execution_order") != draw_index:
                raise TimelineContractError(f"tick:{tick_index}:draw:{draw_index}:order")
            sequence = draw.get("sequence")
            track = draw.get("track")
            pattern_number = draw.get("pattern_number")
            emitted = draw.get("emitted")
            if not all(_is_integer(value) for value in (sequence, track, pattern_number)):
                raise TimelineContractError(f"tick:{tick_index}:draw:{draw_index}:numeric")
            if not prior_sequence < sequence < envelope_sequences[2]:
                raise TimelineContractError(f"tick:{tick_index}:draw:{draw_index}:sequence")
            prior_sequence = sequence
            if not isinstance(emitted, bool) or emitted != (pattern_number >= 0):
                raise TimelineContractError(f"tick:{tick_index}:draw:{draw_index}:emitted")
            coordinate = draw.get("author_coordinate")
            if not isinstance(coordinate, Mapping) or not all(
                _is_integer(coordinate.get(axis)) for axis in ("x", "y")
            ):
                raise TimelineContractError(f"tick:{tick_index}:draw:{draw_index}:coordinate")
            if not isinstance(draw.get("provenance"), Mapping):
                raise TimelineContractError(f"tick:{tick_index}:draw:{draw_index}:provenance")
            if emitted:
                emitted_tracks.append(track)
        if tick.get("composite_track_order") != emitted_tracks:
            raise TimelineContractError(f"tick:{tick_index}:composite_order")
    return scene_id, ticks


def required_pattern_numbers(timeline: Mapping[str, Any]) -> list[int]:
    _, ticks = _validate_timeline(timeline)
    return sorted(
        {
            draw["pattern_number"]
            for tick in ticks
            for draw in tick["draw_operations"]
            if draw["emitted"]
        }
    )


def build_binding_blocker_receipt(timeline: Mapping[str, Any]) -> dict[str, Any]:
    scene_id, _ = _validate_timeline(timeline)
    return {
        "schema_version": BLOCKER_SCHEMA,
        "stage": "R4",
        "status": "blocked",
        "scene_id": scene_id,
        "timeline_sha256": canonical_sha256(timeline),
        "required_pattern_numbers": required_pattern_numbers(timeline),
        "reason_codes": [
            "BACKGROUND_PT1_RECORD_BINDING_REQUIRED",
            "PATTERN_MASK_BODY_RECORD_BINDINGS_REQUIRED",
            "COORDINATE_AND_CLIP_CONTRACT_REQUIRED",
            "BIT_ORDER_EVIDENCE_REQUIRED",
        ],
        "authorization_effect": "none",
    }


def _pattern_binding_sha256(binding: PatternBinding) -> str:
    pattern = binding.pattern
    return canonical_sha256(
        {
            "pattern_number": binding.pattern_number,
            "payload_sha256": binding.payload_sha256,
            "mask_record_index": binding.mask_record_index,
            "body_record_index": binding.body_record_index,
            "width_pixels": pattern.width_pixels,
            "height_pixels": pattern.height_pixels,
            "author_x_pixels": pattern.author_x_pixels,
            "author_y_pixels": pattern.author_y_pixels,
            "mask_sha256": sha256_bytes(pattern.mask_bits),
            "body_sha256": sha256_bytes(pattern.color_indices),
        }
    )


def _intersection(
    x: int, y: int, width: int, height: int, canvas_width: int, canvas_height: int
) -> tuple[int, int, int, int]:
    left = max(0, x)
    top = max(0, y)
    right = min(canvas_width, x + width)
    bottom = min(canvas_height, y + height)
    return left, top, max(0, right - left), max(0, bottom - top)


def compose_activity_timeline(
    timeline: Mapping[str, Any],
    background: BackgroundBinding,
    patterns: Mapping[int, PatternBinding],
    contract: CompositionContract,
    *,
    binding_manifest_sha256: str,
) -> CompositionResult:
    """Compose exact 4-bit index frames; never perform palette conversion."""

    scene_id, ticks = _validate_timeline(timeline)
    _validate_contract(contract)
    binding_manifest_sha256 = _require_sha256(
        binding_manifest_sha256, "binding_manifest"
    )
    if not _is_integer(background.width) or not _is_integer(background.height):
        raise BindingError("background_dimensions")
    if background.width <= 0 or background.height <= 0:
        raise BindingError("background_dimensions")
    if background.width * background.height > MAX_CANVAS_PIXELS:
        raise BindingError("canvas_limit")
    if len(background.indices) != background.width * background.height:
        raise BindingError("background_length")
    if any(value > 15 for value in background.indices):
        raise BindingError("background_index_range")
    background_payload_sha = _require_sha256(
        background.payload_sha256, "background_payload"
    )
    if not _is_integer(background.record_index) or background.record_index < 0:
        raise BindingError("background_record_index")

    normalized_patterns: dict[int, PatternBinding] = {}
    for key, binding in patterns.items():
        if not _is_integer(key) or key < 0 or binding.pattern_number != key:
            raise BindingError("pattern_key")
        _require_sha256(binding.payload_sha256, f"pattern_payload:{key}")
        if not all(
            _is_integer(value) and value >= 0
            for value in (binding.mask_record_index, binding.body_record_index)
        ):
            raise BindingError(f"pattern_record_index:{key}")
        foreground = key == 1000 and scene_id in ('JOB006','JOB009') and any(
            draw.get('pattern_number') == key for tick in ticks for draw in tick['draw_operations'])
        if foreground:
            if (binding.mask_record_index,binding.body_record_index)!=(0,1):
                raise BindingError('foreground_record_binding')
            for tick in ticks:
                for draw in tick['draw_operations']:
                    if draw.get('pattern_number') == key and draw.get('source_operation')!={'opcode':17,'bank':0,'record':1}:
                        raise BindingError('foreground_source_operation')
        elif (
            binding.mask_record_index != key * 2
            or binding.body_record_index != key * 2 + 1
        ):
            raise BindingError(f"pattern_pair_index:{key}")
        normalized_patterns[key] = binding
    missing = sorted(set(required_pattern_numbers(timeline)) - set(normalized_patterns))
    if missing:
        raise BindingError("missing_patterns:" + ",".join(str(value) for value in missing))

    background_indices_sha = sha256_bytes(background.indices)
    output_frames: list[bytes] = []
    frame_manifests: list[dict[str, Any]] = []
    used_patterns: set[int] = set()
    for tick_index, tick in enumerate(ticks):
        frame = bytes(background.indices)
        operations: list[dict[str, Any]] = []
        for draw in tick["draw_operations"]:
            pattern_number = draw["pattern_number"]
            base_operation: dict[str, Any] = {
                "execution_order": draw["execution_order"],
                "track": draw["track"],
                "pattern_number": pattern_number,
                "timeline_provenance_sha256": canonical_sha256(draw["provenance"]),
                "frame_before_sha256": sha256_bytes(frame),
            }
            if not draw["emitted"]:
                base_operation.update(
                    {
                        "status": "skipped_negative_pattern",
                        "frame_after_sha256": sha256_bytes(frame),
                    }
                )
                operations.append(base_operation)
                continue

            binding = normalized_patterns[pattern_number]
            used_patterns.add(pattern_number)
            pattern = binding.pattern
            coordinate = draw["author_coordinate"]
            anchor_x = coordinate["x"] * contract.timeline_x_unit_pixels
            anchor_y = coordinate["y"] * contract.timeline_y_unit_pixels
            # Function 7 overrides authored coordinates; the explicitly bound
            # function 17 foreground adds them in both pixels and provenance.
            destination_x = anchor_x
            destination_y = anchor_y
            if draw.get('source_operation',{}).get('opcode') == 17:
                destination_x += pattern.author_x_pixels
                destination_y += pattern.author_y_pixels
            applied_x, applied_y, applied_width, applied_height = _intersection(
                destination_x,
                destination_y,
                pattern.width_pixels,
                pattern.height_pixels,
                background.width,
                background.height,
            )
            if applied_width == 0 or applied_height == 0:
                raise BindingError(f"fully_clipped_pattern:{pattern_number}:tick:{tick_index}")
            applied_pixels = applied_width * applied_height
            source_pixels = pattern.width_pixels * pattern.height_pixels
            cropped_pixels = source_pixels - applied_pixels
            if cropped_pixels and contract.clip_policy == "reject":
                raise BindingError(f"cropping_rejected:{pattern_number}:tick:{tick_index}")
            try:
                composed = apply_masked_pattern(
                    frame,
                    background.width,
                    background.height,
                    pattern,
                    anchor_x_pixels=anchor_x,
                    anchor_y_pixels=anchor_y,
                    include_author_offset=draw.get('source_operation',{}).get('opcode') == 17,
                    clip=contract.clip_policy == "canvas",
                )
            except (FormatError, ValueError) as error:
                raise BindingError(
                    f"mask_body_apply:{pattern_number}:tick:{tick_index}"
                ) from error
            base_operation.update(
                {
                    "status": "composited",
                    "binding_sha256": _pattern_binding_sha256(binding),
                    "mask_sha256": sha256_bytes(pattern.mask_bits),
                    "body_sha256": sha256_bytes(pattern.color_indices),
                    "timeline_anchor": {
                        "x": coordinate["x"],
                        "y": coordinate["y"],
                    },
                    "anchor_pixels": {"x": anchor_x, "y": anchor_y},
                    "pattern_authored_offset": {
                        "x": pattern.author_x_pixels,
                        "y": pattern.author_y_pixels,
                        "applied": draw.get('source_operation',{}).get('opcode') == 17,
                        "reason": "function17_author_offset" if draw.get('source_operation',{}).get('opcode') == 17 else "function7_coordinate_override",
                    },
                    "destination_rect_before_clip": {
                        "x": destination_x,
                        "y": destination_y,
                        "width": pattern.width_pixels,
                        "height": pattern.height_pixels,
                    },
                    "applied_rect": {
                        "x": applied_x,
                        "y": applied_y,
                        "width": applied_width,
                        "height": applied_height,
                    },
                    "cropped_pixels": cropped_pixels,
                    "frame_after_sha256": sha256_bytes(composed),
                }
            )
            operations.append(base_operation)
            frame = composed
        frame_sha = sha256_bytes(frame)
        output_frames.append(frame)
        frame_manifests.append(
            {
                "tick": tick_index,
                "background_restore_sha256": background_indices_sha,
                "background_provenance_sha256": canonical_sha256(
                    tick["background_restore"]
                ),
                "operations": operations,
                "frame_copy_provenance_sha256": canonical_sha256(tick["frame_copy"]),
                "index_frame_bytes": len(frame),
                "index_frame_sha256": frame_sha,
            }
        )

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "status": "offline_candidate",
        "scene_id": scene_id,
        "timeline_sha256": canonical_sha256(timeline),
        "binding_manifest_sha256": binding_manifest_sha256,
        "calibration_status": contract.calibration_status,
        "canvas": {
            "width": background.width,
            "height": background.height,
            "format": "index4_one_byte_per_pixel",
        },
        "background": {
            "payload_sha256": background_payload_sha,
            "record_index": background.record_index,
            "indices_sha256": background_indices_sha,
        },
        "coordinate_contract": {
            "timeline_x_unit_pixels": contract.timeline_x_unit_pixels,
            "timeline_y_unit_pixels": contract.timeline_y_unit_pixels,
            "pattern_author_offset_mode": contract.pattern_author_offset_mode,
            "wwanime_function": 7,
            "clip_policy": contract.clip_policy,
            "bit_order": contract.bit_order,
        },
        "pattern_record_binding": {
            "mode": contract.pattern_record_binding_mode,
            "mask_index_expression": "2n",
            "body_index_expression": "2n_plus_1",
        },
        "transform_policy": {
            "interpolation": "none",
            "scaling": "none",
            "palette_conversion": "none",
            "color_remap": "none",
            "frame_insertion": "none",
        },
        "used_pattern_numbers": sorted(used_patterns),
        "frames": frame_manifests,
        "authorization_effect": "none",
    }
    return CompositionResult(tuple(output_frames), manifest)


def _resolve_quarantine_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise QuarantineError("root_unavailable") from error
    if not is_data_directory(resolved):
        raise QuarantineError("root_must_be_existing_external_directory")
    return resolved


def _require_quarantined_path(
    path: Path, root: Path, *, must_exist: bool
) -> Path:
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as error:
        raise QuarantineError("path_unavailable") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise QuarantineError("path_outside_root") from error
    if resolved == root:
        raise QuarantineError("path_equals_root")
    return resolved


def _read_json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BindingError(f"json:{label}") from error
    if not isinstance(value, dict):
        raise BindingError(f"json_object:{label}")
    return value, raw


def _positive_integer(value: Any, field: str) -> int:
    if not _is_integer(value) or value <= 0:
        raise BindingError(f"positive_integer:{field}")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if not _is_integer(value) or value < 0:
        raise BindingError(f"nonnegative_integer:{field}")
    return value


def load_bindings(
    manifest_path: Path,
    quarantine_root: Path,
    timeline: Mapping[str, Any],
) -> LoadedBindings:
    """Load explicitly mapped PT1 records; never infer record-to-pattern identity."""

    root = _resolve_quarantine_root(quarantine_root)
    manifest_path = _require_quarantined_path(manifest_path, root, must_exist=True)
    manifest, manifest_raw = _read_json_object(manifest_path, "bindings")
    if manifest.get("schema_version") != BINDING_SCHEMA:
        raise BindingError("binding_schema")
    scene_id, _ = _validate_timeline(timeline)
    if manifest.get("scene_id") != scene_id:
        raise BindingError("scene_id")
    timeline_sha = canonical_sha256(timeline)
    if _require_sha256(manifest.get("timeline_sha256"), "timeline") != timeline_sha:
        raise BindingError("timeline_hash")

    canvas = manifest.get("canvas")
    coordinate = manifest.get("coordinate_contract")
    if not isinstance(canvas, Mapping) or not isinstance(coordinate, Mapping):
        raise BindingError("contracts")
    width = _positive_integer(canvas.get("width"), "canvas_width")
    height = _positive_integer(canvas.get("height"), "canvas_height")
    if width * height > MAX_CANVAS_PIXELS:
        raise BindingError("canvas_limit")
    contract = CompositionContract(
        timeline_x_unit_pixels=_positive_integer(
            coordinate.get("timeline_x_unit_pixels"), "timeline_x_unit_pixels"
        ),
        timeline_y_unit_pixels=_positive_integer(
            coordinate.get("timeline_y_unit_pixels"), "timeline_y_unit_pixels"
        ),
        pattern_author_offset_mode=coordinate.get("pattern_author_offset_mode"),
        pattern_record_binding_mode=manifest.get("pattern_record_binding_mode"),
        clip_policy=coordinate.get("clip_policy"),
        bit_order=coordinate.get("bit_order"),
        calibration_status=coordinate.get("calibration_status"),
    )
    _validate_contract(contract)

    payload_cache: dict[tuple[Path, str], tuple[bytes, Any]] = {}

    def payload_records(binding: Mapping[str, Any], label: str) -> tuple[bytes, Any, str]:
        path_value = binding.get("payload_path")
        if not isinstance(path_value, str) or not path_value:
            raise BindingError(f"payload_path:{label}")
        candidate = Path(path_value)
        if not candidate.is_absolute():
            candidate = manifest_path.parent / candidate
        payload_path = _require_quarantined_path(candidate, root, must_exist=True)
        expected_sha = _require_sha256(binding.get("payload_sha256"), f"payload:{label}")
        cache_key = (payload_path, expected_sha)
        if cache_key in payload_cache:
            payload, parsed = payload_cache[cache_key]
            return payload, parsed, expected_sha
        try:
            payload = payload_path.read_bytes()
        except OSError as error:
            raise BindingError(f"payload_read:{label}") from error
        if sha256_bytes(payload) != expected_sha:
            raise BindingError(f"payload_hash:{label}")
        try:
            parsed = parse_pt1(payload)
        except (FormatError, ValueError) as error:
            raise BindingError(f"pt1_parse:{label}") from error
        payload_cache[cache_key] = (payload, parsed)
        return payload, parsed, expected_sha

    background_data = manifest.get("background")
    if not isinstance(background_data, Mapping):
        raise BindingError("background")
    _, background_parsed, background_sha = payload_records(
        background_data, "background"
    )
    background_index = _nonnegative_integer(
        background_data.get("record_index"), "background_record"
    )
    if background_index >= len(background_parsed.records):
        raise BindingError("background_record_bounds")
    background_record = background_parsed.records[background_index]
    if background_record.attribute not in (1, 2):
        raise BindingError("background_record_attribute")
    if background_record.author_x_raw != 0 or background_record.author_y_raw != 0:
        raise BindingError("background_author_offset")
    if (
        background_record.width_pixels != width
        or background_record.height_pixels != height
    ):
        raise BindingError("background_geometry")
    try:
        background_indices = planes_to_indices(
            decode_record_planes(background_record),
            background_record.width_bytes,
            background_record.height_pixels,
            msb_left=contract.bit_order == "msb_left",
        )
    except (FormatError, ValueError) as error:
        raise BindingError("background_decode") from error
    background = BackgroundBinding(
        indices=background_indices,
        width=width,
        height=height,
        payload_sha256=background_sha,
        record_index=background_index,
    )

    pattern_rows = manifest.get("patterns")
    if not isinstance(pattern_rows, list):
        raise BindingError("patterns")
    patterns: dict[int, PatternBinding] = {}
    for row_index, row in enumerate(pattern_rows):
        if not isinstance(row, Mapping):
            raise BindingError(f"pattern_row:{row_index}")
        number = _nonnegative_integer(row.get("pattern_number"), "pattern_number")
        if number in patterns:
            raise BindingError(f"duplicate_pattern:{number}")
        _, parsed, payload_sha = payload_records(row, f"pattern:{number}")
        mask_index = _nonnegative_integer(
            row.get("mask_record_index"), "mask_record_index"
        )
        body_index = _nonnegative_integer(
            row.get("body_record_index"), "body_record_index"
        )
        if mask_index >= len(parsed.records) or body_index >= len(parsed.records):
            raise BindingError(f"pattern_record_bounds:{number}")
        try:
            pattern = decode_mask_body_pair(
                parsed.records[mask_index],
                parsed.records[body_index],
                msb_left=contract.bit_order == "msb_left",
            )
        except (FormatError, ValueError) as error:
            raise BindingError(f"pattern_decode:{number}") from error
        patterns[number] = PatternBinding(
            pattern_number=number,
            pattern=pattern,
            payload_sha256=payload_sha,
            mask_record_index=mask_index,
            body_record_index=body_index,
        )

    missing = sorted(set(required_pattern_numbers(timeline)) - set(patterns))
    if missing:
        raise BindingError("missing_patterns:" + ",".join(str(value) for value in missing))
    return LoadedBindings(
        background=background,
        patterns=patterns,
        contract=contract,
        manifest_sha256=sha256_bytes(manifest_raw),
    )


def write_composition_result(
    result: CompositionResult, output_root: Path, quarantine_root: Path
) -> dict[str, Any]:
    root = _resolve_quarantine_root(quarantine_root)
    output = _require_quarantined_path(output_root, root, must_exist=False)
    if output.exists():
        raise QuarantineError("output_already_exists")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".pm2_composition_", dir=output.parent
        ) as temporary:
            staging = Path(temporary)
            manifest = json.loads(json.dumps(result.manifest))
            for tick, frame in enumerate(result.frames):
                name = f"tick_{tick:04d}.idx"
                with (staging / name).open("xb") as handle:
                    handle.write(frame)
                manifest["frames"][tick]["index_file"] = name
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=True, indent=2, sort_keys=True
            ).encode("ascii") + b"\n"
            with (staging / "manifest.json").open("xb") as handle:
                handle.write(manifest_bytes)
            staging.rename(output)
    except FileExistsError as error:
        raise QuarantineError("output_already_exists") from error
    except OSError as error:
        raise QuarantineError("output_write") from error
    return {
        "schema_version": RECEIPT_SCHEMA,
        "status": "offline_candidate_written",
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "frame_count": len(result.frames),
        "frame_sha256": [sha256_bytes(frame) for frame in result.frames],
        "authorization_effect": "none",
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compose strict indexed PM2 activity frames inside a external-data quarantine."
    )
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--quarantine-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        root = _resolve_quarantine_root(args.quarantine_root)
        timeline_path = _require_quarantined_path(args.timeline, root, must_exist=True)
        timeline, _ = _read_json_object(timeline_path, "timeline")
        if args.bindings is None:
            print(
                json.dumps(
                    build_binding_blocker_receipt(timeline),
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 3
        if args.output_root is None:
            raise QuarantineError("output_root_required")
        loaded = load_bindings(args.bindings, root, timeline)
        result = compose_activity_timeline(
            timeline,
            loaded.background,
            loaded.patterns,
            loaded.contract,
            binding_manifest_sha256=loaded.manifest_sha256,
        )
        receipt = write_composition_result(result, args.output_root, root)
    except CompositionError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
