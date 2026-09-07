#!/usr/bin/env python3
"""Bind R4 index ticks to an independently confirmed native RGB raster.

The R4 compositor deliberately stops at palette-free 4-bit index frames.  This
tool is the only bridge from those frames to the ``pm2_activity_offline_rgb_ticks``
manifest consumed by R5.  It accepts no inferred palette or display geometry:
both are hash-bound, and an external confirmation receipt must bind them to a
fixed-build observation before any PNG is written.

Only three pixel operations exist here: exact palette lookup, an explicit crop,
and explicit integer X/Y repetition.  Thresholding, interpolation, automatic
scaling, quantisation, colour correction, and palette guessing are forbidden.
"""

from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import io
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


TOOL_VERSION = "0.2.0"
R4_MANIFEST_SCHEMA = "pm2_activity_composition_manifest/v1"
R4_PERSISTENT_MANIFEST_SCHEMA = "pm2_activity_persistent_composition_manifest/v1"
BINDING_SCHEMA_VERSION = 1
BINDING_KIND = "pm2_activity_offline_rgb_binding"
PALETTE_SCHEMA_VERSION = 1
PALETTE_KIND = "pm2_activity_rgb16_palette"
EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_KIND = "pm2_activity_display_binding_confirmation"
OUTPUT_SCHEMA_VERSION = 1
OUTPUT_MANIFEST_KIND = "pm2_activity_offline_rgb_ticks"
RECEIPT_KIND = "pm2_activity_offline_rgb_generation"
REQUIRED_CONFIRMATION_CLAIMS = (
    "palette_rgb16",
    "crop_rect",
    "integer_repeat",
)
ALLOWED_EVIDENCE_BASES = {
    "fixed_build_runtime_capture",
    "fixed_build_runtime_state_trace",
}
MAX_ENTRIES = 100_000
MAX_DIMENSION = 4096
MAX_OUTPUT_PIXELS = 16_777_216


class OfflineRgbError(ValueError):
    """Raised for malformed, unsafe, or inconsistent input."""


class QuarantineError(OfflineRgbError):
    """Raised when a real-data path escapes the external-data quarantine."""


class NotReadyError(OfflineRgbError):
    """Raised when explicit fixed-build confirmation is not yet complete."""

    def __init__(self, reasons: Sequence[str]) -> None:
        normalized = tuple(sorted(set(reasons)))
        if not normalized:
            normalized = ("display_binding_not_confirmed",)
        self.reasons = normalized
        super().__init__(", ".join(normalized))


@dataclass(frozen=True)
class RasterContract:
    input_width: int
    input_height: int
    crop_x: int
    crop_y: int
    crop_width: int
    crop_height: int
    repeat_x: int
    repeat_y: int
    output_width: int
    output_height: int
    canonical_sha256: str
    serialized: dict[str, Any]


@dataclass(frozen=True)
class IndexTick:
    tick: int
    indices: bytes
    source_sha256: str
    pattern_numbers: tuple[int, ...]


@dataclass(frozen=True)
class LoadedInputs:
    scene_id: str
    composition_manifest: dict[str, Any]
    composition_schema: str
    composition_manifest_sha256: str
    timeline_sha256: str
    binding_manifest_sha256: str
    display_binding_sha256: str
    evidence_receipt_sha256: str
    evidence_basis: str
    palette_file_sha256: str
    palette: tuple[tuple[int, int, int], ...]
    raster: RasterContract
    ticks: tuple[IndexTick, ...]
    pt1_report_sha256: str
    coordinate_contract: dict[str, Any]


@dataclass(frozen=True)
class CompositionSource:
    scene_id: str
    manifest: dict[str, Any]
    schema: str
    manifest_sha256: str
    timeline_sha256: str
    binding_manifest_sha256: str
    width: int
    height: int
    coordinate_contract: dict[str, Any]
    ticks: tuple[IndexTick, ...]
    declared_pt1_report_sha256: str | None = None


def sha256_bytes(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    data = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return sha256_bytes(data)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_integer(value: object, field: str, *, minimum: int) -> int:
    if not _is_integer(value) or int(value) < minimum:
        raise OfflineRgbError(f"{field} must be an integer >= {minimum}")
    return int(value)


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise OfflineRgbError(f"{field} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise OfflineRgbError(f"{field} must be a SHA-256 hex string") from exc
    return value.lower()


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineRgbError(f"cannot read {label} JSON: {path}") from exc
    if not isinstance(value, dict):
        raise OfflineRgbError(f"{label} must contain a JSON object")
    return value, raw


def _resolve_quarantine_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise QuarantineError("quarantine root is unavailable") from exc
    if not is_data_directory(resolved):
        raise QuarantineError("quarantine root must be an existing external data directory")
    return resolved


def _require_quarantined_path(
    path: Path,
    root: Path,
    *,
    must_exist: bool,
) -> Path:
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise QuarantineError(f"quarantined path is unavailable: {path}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise QuarantineError(f"path escapes quarantine root: {path}") from exc
    if resolved == root:
        raise QuarantineError("a file or output directory cannot equal the quarantine root")
    return resolved


def _resolve_relative_file(
    owner_manifest: Path,
    value: object,
    label: str,
    root: Path,
    *,
    suffix: str,
) -> Path:
    if not isinstance(value, str) or not value:
        raise OfflineRgbError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise OfflineRgbError(f"{label} must be relative to its manifest")
    candidate = owner_manifest.parent / relative
    resolved = _require_quarantined_path(candidate, root, must_exist=True)
    try:
        resolved.relative_to(owner_manifest.parent.resolve())
    except ValueError as exc:
        raise OfflineRgbError(f"{label} escapes its manifest directory") from exc
    if resolved.suffix.lower() != suffix.lower():
        raise OfflineRgbError(f"{label} must reference a {suffix} file")
    if not resolved.is_file():
        raise OfflineRgbError(f"{label} is not a file")
    return resolved


def _resolve_root_relative_file(
    root: Path,
    value: object,
    label: str,
    *,
    suffix: str,
) -> Path:
    """Resolve a quarantine-root-relative aggregate reference without traversal."""

    if not isinstance(value, str) or not value:
        raise OfflineRgbError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise OfflineRgbError(f"{label} must be relative to the quarantine root")
    if any(part == ".." for part in relative.parts):
        raise OfflineRgbError(f"{label} contains forbidden parent traversal")
    resolved = _require_quarantined_path(root / relative, root, must_exist=True)
    if resolved.suffix.lower() != suffix.lower():
        raise OfflineRgbError(f"{label} must reference a {suffix} file")
    if not resolved.is_file():
        raise OfflineRgbError(f"{label} is not a file")
    return resolved


def _load_root_json_reference(
    root: Path,
    value: object,
    label: str,
    *,
    sha_field: str = "file_sha256",
) -> tuple[Path, dict[str, Any], bytes, str]:
    if not isinstance(value, dict):
        raise OfflineRgbError(f"{label} must be a hash-bound file reference")
    path = _resolve_root_relative_file(
        root,
        value.get("path"),
        f"{label}.path",
        suffix=".json",
    )
    expected_sha = _require_sha256(value.get(sha_field), f"{label}.{sha_field}")
    actual_sha = sha256_path(path)
    if actual_sha != expected_sha:
        raise OfflineRgbError(
            f"{label} SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    document, raw = _load_json(path, label)
    return path, document, raw, actual_sha


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise OfflineRgbError(
            f"{field} keys differ; missing={missing}, unexpected={unexpected}"
        )


def _parse_raster_contract(
    value: object,
    *,
    canvas_width: int,
    canvas_height: int,
    declared_sha256: object,
) -> RasterContract:
    if not isinstance(value, dict):
        raise OfflineRgbError("raster_contract must be an object")
    _require_exact_keys(
        value,
        {
            "input_canvas",
            "crop",
            "integer_repeat",
            "output_raster",
            "operation_order",
            "interpolation",
            "automatic_scaling",
            "thresholding",
        },
        "raster_contract",
    )
    input_canvas = value["input_canvas"]
    crop = value["crop"]
    repeat = value["integer_repeat"]
    output = value["output_raster"]
    if not all(isinstance(item, dict) for item in (input_canvas, crop, repeat, output)):
        raise OfflineRgbError("raster contract geometry fields must be objects")
    _require_exact_keys(input_canvas, {"width", "height"}, "input_canvas")
    _require_exact_keys(crop, {"x", "y", "width", "height"}, "crop")
    _require_exact_keys(repeat, {"x", "y"}, "integer_repeat")
    _require_exact_keys(output, {"width", "height"}, "output_raster")

    input_width = _require_integer(input_canvas["width"], "input_canvas.width", minimum=1)
    input_height = _require_integer(input_canvas["height"], "input_canvas.height", minimum=1)
    if (input_width, input_height) != (canvas_width, canvas_height):
        raise OfflineRgbError("raster input canvas does not match the R4 canvas")
    crop_x = _require_integer(crop["x"], "crop.x", minimum=0)
    crop_y = _require_integer(crop["y"], "crop.y", minimum=0)
    crop_width = _require_integer(crop["width"], "crop.width", minimum=1)
    crop_height = _require_integer(crop["height"], "crop.height", minimum=1)
    if crop_x + crop_width > input_width or crop_y + crop_height > input_height:
        raise OfflineRgbError("crop rectangle escapes the R4 canvas")
    repeat_x = _require_integer(repeat["x"], "integer_repeat.x", minimum=1)
    repeat_y = _require_integer(repeat["y"], "integer_repeat.y", minimum=1)
    output_width = _require_integer(output["width"], "output_raster.width", minimum=1)
    output_height = _require_integer(output["height"], "output_raster.height", minimum=1)
    if output_width != crop_width * repeat_x or output_height != crop_height * repeat_y:
        raise OfflineRgbError("output raster does not equal crop dimensions times integer repeat")
    if (
        output_width > MAX_DIMENSION
        or output_height > MAX_DIMENSION
        or output_width * output_height > MAX_OUTPUT_PIXELS
    ):
        raise OfflineRgbError("output raster exceeds native-size safety limits")
    if value["operation_order"] != ["crop", "integer_repeat"]:
        raise OfflineRgbError("operation_order must be exactly crop then integer_repeat")
    if value["interpolation"] != "none":
        raise OfflineRgbError("interpolation is forbidden")
    if value["automatic_scaling"] is not False:
        raise OfflineRgbError("automatic scaling is forbidden")
    if value["thresholding"] != "none":
        raise OfflineRgbError("thresholding is forbidden")

    actual_sha = canonical_sha256(value)
    expected_sha = _require_sha256(declared_sha256, "raster_contract_sha256")
    if actual_sha != expected_sha:
        raise OfflineRgbError(
            f"raster contract SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    return RasterContract(
        input_width=input_width,
        input_height=input_height,
        crop_x=crop_x,
        crop_y=crop_y,
        crop_width=crop_width,
        crop_height=crop_height,
        repeat_x=repeat_x,
        repeat_y=repeat_y,
        output_width=output_width,
        output_height=output_height,
        canonical_sha256=actual_sha,
        serialized=json.loads(json.dumps(value)),
    )


def _load_palette(
    binding_manifest: Path,
    palette_binding: object,
    root: Path,
) -> tuple[tuple[tuple[int, int, int], ...], str]:
    if not isinstance(palette_binding, dict):
        raise OfflineRgbError("palette must be a hash-bound file reference")
    _require_exact_keys(palette_binding, {"path", "sha256", "format"}, "palette")
    if palette_binding["format"] != "rgb24_16_entries":
        raise OfflineRgbError("palette format must be rgb24_16_entries")
    palette_path = _resolve_relative_file(
        binding_manifest,
        palette_binding["path"],
        "palette.path",
        root,
        suffix=".json",
    )
    expected_sha = _require_sha256(palette_binding["sha256"], "palette.sha256")
    actual_sha = sha256_path(palette_path)
    if actual_sha != expected_sha:
        raise OfflineRgbError(
            f"palette SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    value, _ = _load_json(palette_path, "palette")
    _require_exact_keys(value, {"schema_version", "palette_kind", "entries"}, "palette file")
    if (
        value["schema_version"] != PALETTE_SCHEMA_VERSION
        or value["palette_kind"] != PALETTE_KIND
    ):
        raise OfflineRgbError("palette file schema is unsupported")
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) != 16:
        raise OfflineRgbError("palette must contain exactly 16 RGB entries")
    normalized: list[tuple[int, int, int]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, list) or len(entry) != 3:
            raise OfflineRgbError(f"palette entry {index} must contain exactly three channels")
        channels = tuple(
            _require_integer(channel, f"palette.entries[{index}]", minimum=0)
            for channel in entry
        )
        if any(channel > 255 for channel in channels):
            raise OfflineRgbError(f"palette entry {index} channel exceeds 255")
        normalized.append(channels)
    return tuple(normalized), actual_sha


def _load_evidence(
    binding_manifest: Path,
    evidence_binding: object,
    root: Path,
    *,
    scene_id: str,
    composition_manifest_sha256: str,
    palette_sha256: str,
    raster_contract_sha256: str,
) -> tuple[str, str]:
    if evidence_binding is None:
        raise NotReadyError(["display_confirmation_evidence_not_provided"])
    if not isinstance(evidence_binding, dict):
        raise OfflineRgbError("evidence_receipt must be a hash-bound file reference")
    _require_exact_keys(evidence_binding, {"path", "sha256"}, "evidence_receipt")
    evidence_path = _resolve_relative_file(
        binding_manifest,
        evidence_binding["path"],
        "evidence_receipt.path",
        root,
        suffix=".json",
    )
    expected_sha = _require_sha256(evidence_binding["sha256"], "evidence_receipt.sha256")
    actual_sha = sha256_path(evidence_path)
    if actual_sha != expected_sha:
        raise OfflineRgbError(
            f"evidence receipt SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
        )
    evidence, _ = _load_json(evidence_path, "display evidence")
    if (
        evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
        or evidence.get("evidence_kind") != EVIDENCE_KIND
    ):
        raise OfflineRgbError("display evidence schema is unsupported")
    if evidence.get("status") != "confirmed":
        raise NotReadyError(["display_confirmation_status_not_confirmed"])

    reasons: list[str] = []
    claims = evidence.get("claims")
    if not isinstance(claims, dict):
        reasons.append("display_confirmation_claims_missing")
    else:
        for claim in REQUIRED_CONFIRMATION_CLAIMS:
            if claims.get(claim) != "confirmed":
                reasons.append(f"{claim}_not_confirmed")
    if reasons:
        raise NotReadyError(reasons)

    if evidence.get("scene_id") != scene_id:
        raise OfflineRgbError("display evidence scene_id does not match")
    basis = evidence.get("basis")
    if basis not in ALLOWED_EVIDENCE_BASES:
        raise OfflineRgbError("display evidence basis is not a fixed-build runtime basis")
    _require_sha256(evidence.get("capture_receipt_sha256"), "capture_receipt_sha256")
    expected_links = {
        "composition_manifest_sha256": composition_manifest_sha256,
        "palette_sha256": palette_sha256,
        "raster_contract_sha256": raster_contract_sha256,
    }
    for field, expected in expected_links.items():
        if _require_sha256(evidence.get(field), field) != expected:
            raise OfflineRgbError(f"display evidence {field} does not bind the selected input")
    producer = evidence.get("candidate_producer")
    confirmer = evidence.get("confirmed_by")
    if not isinstance(producer, str) or not producer.strip():
        raise OfflineRgbError("display evidence candidate_producer must be non-empty")
    if not isinstance(confirmer, str) or not confirmer.strip():
        raise OfflineRgbError("display evidence confirmed_by must be non-empty")
    if producer.strip() == confirmer.strip():
        raise OfflineRgbError("display binding candidate cannot confirm itself")
    if evidence.get("authorization_effect") != "none":
        raise OfflineRgbError("display evidence cannot grant authorization")
    return actual_sha, str(basis)


def _load_index_ticks(
    composition_manifest_path: Path,
    composition: Mapping[str, Any],
    root: Path,
    *,
    width: int,
    height: int,
) -> tuple[IndexTick, ...]:
    frames = composition.get("frames")
    if not isinstance(frames, list) or not frames:
        raise OfflineRgbError("R4 manifest must contain a non-empty frames list")
    if len(frames) > MAX_ENTRIES:
        raise OfflineRgbError("R4 frame count exceeds safety limit")
    expected_bytes = width * height
    ticks: list[IndexTick] = []
    for position, frame in enumerate(frames):
        if not isinstance(frame, dict) or frame.get("tick") != position:
            raise OfflineRgbError(f"R4 frame {position} must have the same zero-based tick")
        frame_path = _resolve_relative_file(
            composition_manifest_path,
            frame.get("index_file"),
            f"frames[{position}].index_file",
            root,
            suffix=".idx",
        )
        expected_sha = _require_sha256(
            frame.get("index_frame_sha256"), f"frames[{position}].index_frame_sha256"
        )
        actual_sha = sha256_path(frame_path)
        if actual_sha != expected_sha:
            raise OfflineRgbError(
                f"index frame SHA-256 mismatch at tick {position}: expected {expected_sha}, got {actual_sha}"
            )
        try:
            indices = frame_path.read_bytes()
        except OSError as exc:
            raise OfflineRgbError(f"cannot read index frame at tick {position}") from exc
        if len(indices) != expected_bytes or frame.get("index_frame_bytes") != expected_bytes:
            raise OfflineRgbError(f"index frame size mismatch at tick {position}")
        if any(value > 15 for value in indices):
            raise OfflineRgbError(f"index frame contains a value above 15 at tick {position}")
        operations = frame.get("operations")
        if not isinstance(operations, list):
            raise OfflineRgbError(f"R4 frame {position} operations must be a list")
        pattern_numbers: list[int] = []
        for operation in operations:
            if not isinstance(operation, dict):
                raise OfflineRgbError(f"R4 frame {position} contains a malformed operation")
            if operation.get("status") == "composited":
                number = operation.get("pattern_number")
                if not _is_integer(number) or int(number) < 0:
                    raise OfflineRgbError(f"R4 frame {position} has an invalid pattern number")
                pattern_numbers.append(int(number))
        ticks.append(
            IndexTick(
                tick=position,
                indices=indices,
                source_sha256=actual_sha,
                pattern_numbers=tuple(sorted(set(pattern_numbers))),
            )
        )
    return tuple(ticks)


def _parse_r4_canvas(value: object, label: str = "R4 canvas") -> tuple[int, int]:
    if not isinstance(value, dict):
        raise OfflineRgbError(f"{label} must be an object")
    width = _require_integer(value.get("width"), f"{label}.width", minimum=1)
    height = _require_integer(value.get("height"), f"{label}.height", minimum=1)
    if value.get("format") != "index4_one_byte_per_pixel":
        raise OfflineRgbError(
            f"{label} format must be index4_one_byte_per_pixel"
        )
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_OUTPUT_PIXELS:
        raise OfflineRgbError(f"{label} exceeds safety limits")
    return width, height


def _validate_r4_transform(value: object, label: str = "R4 manifest") -> None:
    if not isinstance(value, dict) or any(
        value.get(field) != "none"
        for field in (
            "interpolation",
            "scaling",
            "palette_conversion",
            "color_remap",
            "frame_insertion",
        )
    ):
        raise OfflineRgbError(f"{label} contains an unsupported transform policy")


def _validate_r4_coordinate(value: object, label: str = "R4") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OfflineRgbError(f"{label} coordinate_contract must be an object")
    expected_coordinate_values = {
        "timeline_x_unit_pixels": 8,
        "timeline_y_unit_pixels": 1,
        "pattern_author_offset_mode": "function7_coordinate_override",
        "wwanime_function": 7,
    }
    for field, expected in expected_coordinate_values.items():
        if value.get(field) != expected:
            raise OfflineRgbError(f"{label} coordinate_contract.{field} is unsupported")
    if value.get("clip_policy") not in {"canvas", "reject"}:
        raise OfflineRgbError(f"{label} coordinate_contract.clip_policy is unsupported")
    if value.get("bit_order") not in {"msb_left", "lsb_left"}:
        raise OfflineRgbError(f"{label} coordinate_contract.bit_order is unsupported")
    return json.loads(json.dumps(value))


def _load_single_composition_source(
    composition_path: Path,
    root: Path,
) -> CompositionSource:
    composition, composition_raw = _load_json(composition_path, "R4 composition manifest")
    if composition.get("schema_version") != R4_MANIFEST_SCHEMA:
        raise OfflineRgbError("unsupported R4 composition manifest schema")
    if composition.get("status") != "offline_candidate":
        raise OfflineRgbError("R4 composition manifest status must be offline_candidate")
    if composition.get("authorization_effect") != "none":
        raise OfflineRgbError("R4 composition manifest cannot grant authorization")
    scene_id = composition.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise OfflineRgbError("R4 scene_id must be a non-empty string")
    width, height = _parse_r4_canvas(composition.get("canvas"))
    _validate_r4_transform(composition.get("transform_policy"))
    coordinate = _validate_r4_coordinate(composition.get("coordinate_contract"))
    if composition.get("calibration_status") not in {
        "source_candidate",
        "runtime_calibrated",
    }:
        raise OfflineRgbError("R4 calibration_status is unsupported")
    ticks = _load_index_ticks(
        composition_path,
        composition,
        root,
        width=width,
        height=height,
    )
    return CompositionSource(
        scene_id=scene_id,
        manifest=composition,
        schema=R4_MANIFEST_SCHEMA,
        manifest_sha256=sha256_bytes(composition_raw),
        timeline_sha256=_require_sha256(
            composition.get("timeline_sha256"), "timeline_sha256"
        ),
        binding_manifest_sha256=_require_sha256(
            composition.get("binding_manifest_sha256"), "binding_manifest_sha256"
        ),
        width=width,
        height=height,
        coordinate_contract=coordinate,
        ticks=ticks,
    )


def _require_embedded_scene(document: Mapping[str, Any], scene_id: str, label: str) -> None:
    scene = document.get("scene")
    embedded = scene.get("scene_id") if isinstance(scene, dict) else document.get("scene_id")
    if embedded != scene_id:
        raise OfflineRgbError(f"{label} scene_id does not match aggregate")


def _load_persistent_composition_source(
    composition: dict[str, Any],
    composition_raw: bytes,
    root: Path,
) -> CompositionSource:
    if composition.get("status") != "offline_candidate":
        raise OfflineRgbError("persistent R4 aggregate status must be offline_candidate")
    if composition.get("stage") != "R4":
        raise OfflineRgbError("persistent R4 aggregate stage must be R4")
    if composition.get("authorization_effect") != "none":
        raise OfflineRgbError("persistent R4 aggregate cannot grant authorization")
    scene_id = composition.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise OfflineRgbError("persistent R4 aggregate scene_id must be a non-empty string")

    decoder = composition.get("decoder_contract")
    if not isinstance(decoder, dict):
        raise OfflineRgbError("persistent R4 decoder_contract must be an object")
    width, height = _parse_r4_canvas(
        decoder.get("canvas"), "persistent R4 decoder_contract.canvas"
    )
    units = decoder.get("timeline_coordinate_units")
    if not isinstance(units, dict) or units.get("x_pixels_per_unit") != 8 or units.get(
        "y_pixels_per_unit"
    ) != 1:
        raise OfflineRgbError("persistent R4 timeline coordinate units are unsupported")
    if decoder.get("pattern_author_offset_mode") != "function7_coordinate_override":
        raise OfflineRgbError("persistent R4 pattern author offset mode is unsupported")
    if decoder.get("pt1_storage_order") != "x_byte_major":
        raise OfflineRgbError("persistent R4 PT1 storage order is unsupported")

    calls = composition.get("calls")
    top_frames = composition.get("frames")
    if not isinstance(calls, list) or not calls:
        raise OfflineRgbError("persistent R4 aggregate must contain calls")
    if not isinstance(top_frames, list) or not top_frames:
        raise OfflineRgbError("persistent R4 aggregate must contain frames")
    call_count = _require_integer(composition.get("call_count"), "call_count", minimum=1)
    tick_count = _require_integer(composition.get("tick_count"), "tick_count", minimum=1)
    if call_count != len(calls):
        raise OfflineRgbError("persistent R4 call_count does not match calls")
    if tick_count != len(top_frames):
        raise OfflineRgbError("persistent R4 tick_count does not match frames")
    if tick_count > MAX_ENTRIES:
        raise OfflineRgbError("persistent R4 frame count exceeds safety limit")

    inputs = composition.get("inputs")
    if not isinstance(inputs, dict):
        raise OfflineRgbError("persistent R4 inputs must be an object")
    _, sequence_spec, _, _ = _load_root_json_reference(
        root,
        inputs.get("sequence_spec"),
        "inputs.sequence_spec",
        sha_field="sha256",
    )
    _, sequence, _, _ = _load_root_json_reference(
        root,
        inputs.get("sequence_timeline"),
        "inputs.sequence_timeline",
    )
    sequence_ref = inputs.get("sequence_timeline")
    assert isinstance(sequence_ref, dict)  # established by the loader
    sequence_canonical = canonical_sha256(sequence)
    declared_sequence_canonical = _require_sha256(
        sequence_ref.get("canonical_sha256"),
        "inputs.sequence_timeline.canonical_sha256",
    )
    if sequence_canonical != declared_sequence_canonical:
        raise OfflineRgbError(
            "inputs.sequence_timeline canonical SHA-256 does not match its document"
        )
    if sequence.get("schema_version") != "pm2_activity_timeline_sequence/v1":
        raise OfflineRgbError("persistent R4 sequence timeline schema is unsupported")
    _require_embedded_scene(sequence, scene_id, "sequence timeline")
    sequence_calls = sequence.get("calls")
    sequence_ticks = sequence.get("ticks")
    if not isinstance(sequence_calls, list) or len(sequence_calls) != call_count:
        raise OfflineRgbError("sequence timeline calls do not match aggregate")
    if not isinstance(sequence_ticks, list) or len(sequence_ticks) != tick_count:
        raise OfflineRgbError("sequence timeline ticks do not match aggregate")

    spec_calls = sequence_spec.get("calls")
    if not isinstance(spec_calls, list) or len(spec_calls) != call_count:
        raise OfflineRgbError("sequence spec calls do not match aggregate")

    _, base_binding, _, base_binding_sha = _load_root_json_reference(
        root,
        inputs.get("base_binding_manifest"),
        "inputs.base_binding_manifest",
        sha_field="sha256",
    )
    if base_binding.get("scene_id") != scene_id:
        raise OfflineRgbError("base binding scene_id does not match aggregate")
    if base_binding.get("authorization_effect") not in {None, "none"}:
        raise OfflineRgbError("base binding cannot grant authorization")
    pt1_reference = inputs.get("pt1_structural_report")
    if not isinstance(pt1_reference, dict):
        raise OfflineRgbError("inputs.pt1_structural_report must be a hash reference")
    pt1_report_sha = _require_sha256(
        pt1_reference.get("sha256"), "inputs.pt1_structural_report.sha256"
    )

    expected_bytes = width * height
    aggregate_frame_paths: list[Path] = []
    aggregate_frame_bytes: list[bytes] = []
    aggregate_frame_hashes: list[str] = []
    for global_tick, frame in enumerate(top_frames):
        if not isinstance(frame, dict) or frame.get("global_tick") != global_tick:
            raise OfflineRgbError(
                f"persistent R4 frame {global_tick} must have the same zero-based global_tick"
            )
        frame_path = _resolve_root_relative_file(
            root,
            frame.get("index_path"),
            f"frames[{global_tick}].index_path",
            suffix=".idx",
        )
        expected_sha = _require_sha256(
            frame.get("index_frame_sha256"),
            f"frames[{global_tick}].index_frame_sha256",
        )
        actual_sha = sha256_path(frame_path)
        if actual_sha != expected_sha:
            raise OfflineRgbError(
                f"index frame SHA-256 mismatch at global tick {global_tick}: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        try:
            indices = frame_path.read_bytes()
        except OSError as exc:
            raise OfflineRgbError(
                f"cannot read index frame at global tick {global_tick}"
            ) from exc
        if len(indices) != expected_bytes or frame.get("index_frame_bytes") != expected_bytes:
            raise OfflineRgbError(f"index frame size mismatch at global tick {global_tick}")
        if any(value > 15 for value in indices):
            raise OfflineRgbError(
                f"index frame contains a value above 15 at global tick {global_tick}"
            )
        aggregate_frame_paths.append(frame_path)
        aggregate_frame_bytes.append(indices)
        aggregate_frame_hashes.append(actual_sha)

    output_ticks: list[IndexTick | None] = [None] * tick_count
    shared_coordinate: dict[str, Any] | None = None
    next_global_tick = 0
    for call_index, call in enumerate(calls):
        if not isinstance(call, dict) or call.get("call_index") != call_index:
            raise OfflineRgbError(f"persistent R4 call {call_index} has an invalid call_index")
        start = _require_integer(
            call.get("global_tick_start"),
            f"calls[{call_index}].global_tick_start",
            minimum=0,
        )
        end = _require_integer(
            call.get("global_tick_end_exclusive"),
            f"calls[{call_index}].global_tick_end_exclusive",
            minimum=1,
        )
        if start != next_global_tick or end <= start or end > tick_count:
            raise OfflineRgbError(f"persistent R4 call {call_index} has a non-contiguous range")
        next_global_tick = end
        call_frames = call.get("frames")
        if not isinstance(call_frames, list) or len(call_frames) != end - start:
            raise OfflineRgbError(f"persistent R4 call {call_index} frame count is invalid")
        spec_call = spec_calls[call_index]
        if not isinstance(spec_call, dict) or spec_call.get("tick_count") != end - start:
            raise OfflineRgbError(f"sequence spec call {call_index} does not match its range")

        sequence_call = sequence_calls[call_index]
        if not isinstance(sequence_call, dict):
            raise OfflineRgbError(f"sequence timeline call {call_index} is malformed")
        for field, expected in (
            ("call_index", call_index),
            ("global_tick_start", start),
            ("global_tick_end_exclusive", end),
        ):
            if sequence_call.get(field) != expected:
                raise OfflineRgbError(
                    f"sequence timeline call {call_index} {field} does not match aggregate"
                )

        _, call_timeline, _, _ = _load_root_json_reference(
            root,
            call.get("timeline_projection"),
            f"calls[{call_index}].timeline_projection",
        )
        timeline_ref = call.get("timeline_projection")
        assert isinstance(timeline_ref, dict)
        call_timeline_canonical = canonical_sha256(call_timeline)
        if call_timeline_canonical != _require_sha256(
            timeline_ref.get("canonical_sha256"),
            f"calls[{call_index}].timeline_projection.canonical_sha256",
        ):
            raise OfflineRgbError(
                f"call {call_index} timeline canonical SHA-256 does not match"
            )
        if call_timeline.get("schema_version") != "pm2_activity_timeline/v1":
            raise OfflineRgbError(f"call {call_index} timeline schema is unsupported")
        _require_embedded_scene(call_timeline, scene_id, f"call {call_index} timeline")
        call_timeline_ticks = call_timeline.get("ticks")
        if not isinstance(call_timeline_ticks, list) or len(call_timeline_ticks) != end - start:
            raise OfflineRgbError(f"call {call_index} timeline tick count does not match")

        _, call_binding, _, call_binding_sha = _load_root_json_reference(
            root,
            call.get("binding_manifest"),
            f"calls[{call_index}].binding_manifest",
        )
        if call_binding.get("scene_id") != scene_id:
            raise OfflineRgbError(f"call {call_index} binding scene_id does not match")
        if call_binding.get("authorization_effect") not in {None, "none"}:
            raise OfflineRgbError(f"call {call_index} binding cannot grant authorization")

        child_path, _, _, _ = _load_root_json_reference(
            root,
            call.get("composition_manifest"),
            f"calls[{call_index}].composition_manifest",
        )
        child = _load_single_composition_source(child_path, root)
        if child.scene_id != scene_id:
            raise OfflineRgbError(f"call {call_index} composition scene_id does not match")
        if (child.width, child.height) != (width, height):
            raise OfflineRgbError(f"call {call_index} composition canvas does not match")
        if child.timeline_sha256 != call_timeline_canonical:
            raise OfflineRgbError(f"call {call_index} composition does not bind its timeline")
        if child.binding_manifest_sha256 != call_binding_sha:
            raise OfflineRgbError(f"call {call_index} composition does not bind its bindings")
        if len(child.ticks) != end - start:
            raise OfflineRgbError(f"call {call_index} composition tick count does not match")
        if shared_coordinate is None:
            shared_coordinate = child.coordinate_contract
        elif child.coordinate_contract != shared_coordinate:
            raise OfflineRgbError("persistent R4 child coordinate contracts differ")

        child_frames = child.manifest.get("frames")
        assert isinstance(child_frames, list)
        for call_tick, call_frame in enumerate(child_frames):
            global_tick = start + call_tick
            aggregate_call_frame = call_frames[call_tick]
            if not isinstance(aggregate_call_frame, dict):
                raise OfflineRgbError(
                    f"persistent R4 call {call_index} frame {call_tick} is malformed"
                )
            if aggregate_call_frame != top_frames[global_tick]:
                raise OfflineRgbError(
                    f"persistent R4 call {call_index} frame {call_tick} "
                    "does not match the top-level frame"
                )
            if (
                aggregate_call_frame.get("call_tick") != call_tick
                or aggregate_call_frame.get("global_tick") != global_tick
            ):
                raise OfflineRgbError(
                    f"persistent R4 call {call_index} frame ticks are not contiguous"
                )
            sequence_tick = sequence_ticks[global_tick]
            if not isinstance(sequence_tick, dict) or any(
                sequence_tick.get(field) != expected
                for field, expected in (
                    ("global_tick", global_tick),
                    ("tick", global_tick),
                    ("call_index", call_index),
                    ("call_tick", call_tick),
                )
            ):
                raise OfflineRgbError(
                    f"sequence timeline tick {global_tick} does not match aggregate"
                )
            if not isinstance(call_frame, dict) or call_frame.get("tick") != call_tick:
                raise OfflineRgbError(
                    f"call {call_index} child composition tick {call_tick} is invalid"
                )
            child_index_path = _resolve_relative_file(
                child_path,
                call_frame.get("index_file"),
                f"calls[{call_index}].child.frames[{call_tick}].index_file",
                root,
                suffix=".idx",
            )
            if child_index_path != aggregate_frame_paths[global_tick]:
                raise OfflineRgbError(
                    f"aggregate index path at global tick {global_tick} "
                    "does not bind the child composition frame"
                )
            child_tick = child.ticks[call_tick]
            if (
                child_tick.source_sha256 != aggregate_frame_hashes[global_tick]
                or call_frame.get("index_frame_bytes") != expected_bytes
            ):
                raise OfflineRgbError(
                    f"aggregate index metadata at global tick {global_tick} "
                    "does not match the child composition frame"
                )
            output_ticks[global_tick] = IndexTick(
                tick=global_tick,
                indices=aggregate_frame_bytes[global_tick],
                source_sha256=aggregate_frame_hashes[global_tick],
                pattern_numbers=child_tick.pattern_numbers,
            )

    if next_global_tick != tick_count or shared_coordinate is None:
        raise OfflineRgbError("persistent R4 calls do not cover every global tick")
    if (
        shared_coordinate.get("timeline_x_unit_pixels") != units.get("x_pixels_per_unit")
        or shared_coordinate.get("timeline_y_unit_pixels") != units.get("y_pixels_per_unit")
        or shared_coordinate.get("pattern_author_offset_mode")
        != decoder.get("pattern_author_offset_mode")
    ):
        raise OfflineRgbError("persistent R4 decoder and child coordinate contracts differ")
    if any(tick is None for tick in output_ticks):
        raise OfflineRgbError("persistent R4 aggregate leaves an uncovered global tick")

    return CompositionSource(
        scene_id=scene_id,
        manifest=composition,
        schema=R4_PERSISTENT_MANIFEST_SCHEMA,
        manifest_sha256=sha256_bytes(composition_raw),
        timeline_sha256=declared_sequence_canonical,
        binding_manifest_sha256=base_binding_sha,
        width=width,
        height=height,
        coordinate_contract=shared_coordinate,
        ticks=tuple(tick for tick in output_ticks if tick is not None),
        declared_pt1_report_sha256=pt1_report_sha,
    )


def load_confirmed_inputs(
    composition_manifest_path: Path,
    display_binding_path: Path,
    quarantine_root: Path,
) -> LoadedInputs:
    """Load and verify all R4, palette, raster, and confirmation inputs."""

    root = _resolve_quarantine_root(quarantine_root)
    composition_path = _require_quarantined_path(
        composition_manifest_path, root, must_exist=True
    )
    binding_path = _require_quarantined_path(display_binding_path, root, must_exist=True)
    composition, composition_raw = _load_json(composition_path, "R4 composition manifest")
    display_binding, binding_raw = _load_json(binding_path, "display binding")

    schema = composition.get("schema_version")
    if schema == R4_MANIFEST_SCHEMA:
        source = _load_single_composition_source(composition_path, root)
    elif schema == R4_PERSISTENT_MANIFEST_SCHEMA:
        source = _load_persistent_composition_source(
            composition,
            composition_raw,
            root,
        )
    else:
        raise OfflineRgbError("unsupported R4 composition manifest schema")
    scene_id = source.scene_id
    composition_sha = source.manifest_sha256

    if (
        display_binding.get("schema_version") != BINDING_SCHEMA_VERSION
        or display_binding.get("binding_kind") != BINDING_KIND
    ):
        raise OfflineRgbError("unsupported display binding schema")
    if display_binding.get("scene_id") != scene_id:
        raise OfflineRgbError("display binding scene_id does not match R4")
    if (
        _require_sha256(
            display_binding.get("composition_manifest_sha256"),
            "composition_manifest_sha256",
        )
        != composition_sha
    ):
        raise OfflineRgbError("display binding does not bind the selected R4 manifest")
    if display_binding.get("authorization_effect") != "none":
        raise OfflineRgbError("display binding cannot grant authorization")
    pt1_report_sha = _require_sha256(
        display_binding.get("pt1_report_sha256"), "pt1_report_sha256"
    )
    if (
        source.declared_pt1_report_sha256 is not None
        and source.declared_pt1_report_sha256 != pt1_report_sha
    ):
        raise OfflineRgbError(
            "display binding pt1_report_sha256 does not match persistent aggregate"
        )

    palette, palette_sha = _load_palette(binding_path, display_binding.get("palette"), root)
    raster = _parse_raster_contract(
        display_binding.get("raster_contract"),
        canvas_width=source.width,
        canvas_height=source.height,
        declared_sha256=display_binding.get("raster_contract_sha256"),
    )
    evidence_sha, evidence_basis = _load_evidence(
        binding_path,
        display_binding.get("evidence_receipt"),
        root,
        scene_id=scene_id,
        composition_manifest_sha256=composition_sha,
        palette_sha256=palette_sha,
        raster_contract_sha256=raster.canonical_sha256,
    )
    return LoadedInputs(
        scene_id=scene_id,
        composition_manifest=source.manifest,
        composition_schema=source.schema,
        composition_manifest_sha256=composition_sha,
        timeline_sha256=source.timeline_sha256,
        binding_manifest_sha256=source.binding_manifest_sha256,
        display_binding_sha256=sha256_bytes(binding_raw),
        evidence_receipt_sha256=evidence_sha,
        evidence_basis=evidence_basis,
        palette_file_sha256=palette_sha,
        palette=palette,
        raster=raster,
        ticks=source.ticks,
        pt1_report_sha256=pt1_report_sha,
        coordinate_contract=source.coordinate_contract,
    )


def _render_rgb(
    indices: bytes,
    palette: Sequence[tuple[int, int, int]],
    raster: RasterContract,
) -> bytes:
    """Apply exact lookup -> crop -> integer repeat without resampling."""

    output = bytearray()
    for source_y in range(raster.crop_y, raster.crop_y + raster.crop_height):
        row = bytearray()
        start = source_y * raster.input_width + raster.crop_x
        for palette_index in indices[start : start + raster.crop_width]:
            rgb = bytes(palette[palette_index])
            row.extend(rgb * raster.repeat_x)
        for _ in range(raster.repeat_y):
            output.extend(row)
    expected = raster.output_width * raster.output_height * 3
    if len(output) != expected:  # defensive invariant
        raise AssertionError("rendered RGB byte count does not match the raster contract")
    return bytes(output)


def _encode_png(rgb: bytes, width: int, height: int) -> bytes:
    image = Image.frombytes("RGB", (width, height), rgb)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    return buffer.getvalue()


def _build_output_manifest(
    loaded: LoadedInputs,
    rendered: Sequence[tuple[bytes, bytes]],
) -> dict[str, Any]:
    coordinate_contract = json.loads(json.dumps(loaded.coordinate_contract))
    coordinate_contract["display_raster_contract_sha256"] = loaded.raster.canonical_sha256
    coordinate_contract["display_raster_operation"] = "crop_then_integer_repeat"
    ticks: list[dict[str, Any]] = []
    for source, (rgb, png) in zip(loaded.ticks, rendered, strict=True):
        tick = {
            "tick": source.tick,
            "png_path": f"tick_{source.tick:04d}.png",
            "png_sha256": sha256_bytes(png),
            "rgb_sha256": sha256_bytes(rgb),
            "source_index_frame_sha256": source.source_sha256,
            "milestones": [],
        }
        # R5 treats an omitted pattern list as unknown, while an empty list is
        # invalid.  A legitimate background-only tick must therefore omit the
        # field rather than manufacture a pattern identity.
        if source.pattern_numbers:
            tick["pattern_numbers"] = list(source.pattern_numbers)
        ticks.append(tick)
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "manifest_kind": OUTPUT_MANIFEST_KIND,
        "status": "ready",
        "scene_id": loaded.scene_id,
        "tool_version": TOOL_VERSION,
        "timeline_sha256": loaded.timeline_sha256,
        "pt1_report_sha256": loaded.pt1_report_sha256,
        "binding_manifest_sha256": loaded.binding_manifest_sha256,
        "composition_manifest_sha256": loaded.composition_manifest_sha256,
        "source_composition_schema": loaded.composition_schema,
        "display_binding_manifest_sha256": loaded.display_binding_sha256,
        "palette_file_sha256": loaded.palette_file_sha256,
        "display_evidence_receipt_sha256": loaded.evidence_receipt_sha256,
        "display_evidence_basis": loaded.evidence_basis,
        "coordinate_contract": coordinate_contract,
        "display_raster_contract": loaded.raster.serialized,
        "transform_policy": {
            "palette_lookup": "exact_index_0_to_15",
            "crop": "explicit_confirmed_rectangle",
            "integer_repeat": "explicit_confirmed_xy",
            "interpolation": "forbidden",
            "automatic_scaling": "forbidden",
            "thresholding": "forbidden",
            "color_correction": "forbidden",
        },
        "ticks": ticks,
        "authorization_effect": "none",
        "safety": {
            "contains_palette_entries": False,
            "contains_index_or_rgb_pixel_arrays": False,
            "game_build_input": False,
        },
    }


def build_not_ready_receipt(
    reasons: Sequence[str],
    *,
    composition_manifest_path: Path | None = None,
    display_binding_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "tool_version": TOOL_VERSION,
        "status": "not_ready",
        "reasons": sorted(set(reasons)),
        "inputs": {
            "composition_manifest": (
                None if composition_manifest_path is None else str(composition_manifest_path)
            ),
            "display_binding": (
                None if display_binding_path is None else str(display_binding_path)
            ),
        },
        "output_written": False,
        "authorization_effect": "none",
    }


def generate_offline_rgb_ticks(
    composition_manifest_path: Path,
    display_binding_path: Path,
    output_root: Path,
    quarantine_root: Path,
) -> dict[str, Any]:
    """Write confirmed native RGB ticks atomically inside the quarantine."""

    root = _resolve_quarantine_root(quarantine_root)
    output = _require_quarantined_path(output_root, root, must_exist=False)
    if output.exists():
        raise QuarantineError("output already exists")
    if not output.parent.is_dir():
        raise QuarantineError("output parent must already exist")

    # No output directory is created until every hash, confirmation, index
    # value, and raster invariant has passed.
    loaded = load_confirmed_inputs(
        composition_manifest_path,
        display_binding_path,
        root,
    )
    rendered = tuple(
        (
            rgb := _render_rgb(tick.indices, loaded.palette, loaded.raster),
            _encode_png(rgb, loaded.raster.output_width, loaded.raster.output_height),
        )
        for tick in loaded.ticks
    )
    manifest = _build_output_manifest(loaded, rendered)
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")

    try:
        with tempfile.TemporaryDirectory(prefix=".pm2_rgb_", dir=output.parent) as temporary:
            staging = Path(temporary)
            for tick, (_, png) in zip(loaded.ticks, rendered, strict=True):
                with (staging / f"tick_{tick.tick:04d}.png").open("xb") as handle:
                    handle.write(png)
            with (staging / "manifest.json").open("xb") as handle:
                handle.write(manifest_raw)
            staging.rename(output)
    except OSError as exc:
        raise QuarantineError("failed to write native RGB output") from exc

    return {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "tool_version": TOOL_VERSION,
        "status": "ready_written",
        "scene_id": loaded.scene_id,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "tick_count": len(loaded.ticks),
        "png_sha256": [sha256_bytes(png) for _, png in rendered],
        "rgb_sha256": [sha256_bytes(rgb) for rgb, _ in rendered],
        "native_raster": {
            "width": loaded.raster.output_width,
            "height": loaded.raster.output_height,
        },
        "output_written": True,
        "authorization_effect": "none",
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind confirmed PM2 R4 index ticks to exact native-size RGB PNGs."
    )
    parser.add_argument("--composition-manifest", type=Path, required=True)
    parser.add_argument("--display-binding", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--quarantine-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        receipt = generate_offline_rgb_ticks(
            args.composition_manifest,
            args.display_binding,
            args.output_root,
            args.quarantine_root,
        )
    except NotReadyError as exc:
        print(
            json.dumps(
                build_not_ready_receipt(
                    exc.reasons,
                    composition_manifest_path=args.composition_manifest,
                    display_binding_path=args.display_binding,
                ),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    except OfflineRgbError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
