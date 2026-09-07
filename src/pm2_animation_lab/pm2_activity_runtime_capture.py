#!/usr/bin/env python3
"""Export exact PM2 runtime crops from a hash-bound FFmpeg PNG sequence.

This bridge is intentionally mechanical.  It accepts only inputs inside an
explicit external-data quarantine, verifies the complete RGB PNG sequence against an
FFmpeg ``framehash`` file and a capture receipt, and then applies one explicit
320x128 crop to explicitly selected source frames.  It never chooses a scene
interval, labels an action, or promotes the result to S2 evidence.

The source ``framehash`` must have been produced for an RGB24 video stream.
Each data-row hash is compared with the decoded row-major RGB bytes of the
corresponding PNG.  No scaling, interpolation, thresholding, colour correction,
or implicit frame selection is available.
"""

from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import io
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image


TOOL_VERSION = "0.1.0"
CAPTURE_RECEIPT_KIND = "pm2_activity_capture_receipt"
CAPTURE_RECEIPT_READY_STATUSES = {"complete", "ready"}
OUTPUT_MANIFEST_KIND = "pm2_activity_runtime_rgb_frames"
OUTPUT_RECEIPT_KIND = "pm2_activity_runtime_rgb_export"
OUTPUT_WIDTH = 320
OUTPUT_HEIGHT = 128
MAX_SOURCE_FRAMES = 100_000
MAX_SOURCE_DIMENSION = 4096
MAX_SOURCE_PIXELS = 16_777_216

_FRAME_PATTERN_RE = re.compile(r"([A-Za-z0-9_.-]*)%0([1-9][0-9]?)d\.png")
_TB_RE = re.compile(r"#tb\s+(\d+)\s*:\s*(-?\d+)\s*/\s*(\d+)\s*$", re.IGNORECASE)
_DIMENSIONS_RE = re.compile(
    r"#dimensions\s+(\d+)\s*:\s*(\d+)x(\d+)\s*$", re.IGNORECASE
)
_MEDIA_TYPE_RE = re.compile(r"#media_type\s+(\d+)\s*:\s*(\S+)\s*$", re.IGNORECASE)
_HASH_RE = re.compile(r"#hash\s*:\s*(\S+)\s*$", re.IGNORECASE)


class RuntimeCaptureError(ValueError):
    """Raised when a capture input or exact export contract is invalid."""


class QuarantineError(RuntimeCaptureError):
    """Raised when an input or output escapes the external-data quarantine."""


@dataclass(frozen=True)
class FramehashEntry:
    sequence_position: int
    stream_index: int
    dts: int
    pts: int
    duration: int
    byte_size: int
    rgb_sha256: str
    timestamp_us: int
    duration_us: int


@dataclass(frozen=True)
class SourceFrame:
    frame_number: int
    sequence_position: int
    path: Path
    png_sha256: str
    rgb_sha256: str
    width: int
    height: int
    timestamp_us: int
    duration_us: int


@dataclass(frozen=True)
class CaptureInputs:
    scene_id: str
    archive_sha256: str
    source_mkv_path: Path
    source_mkv_sha256: str
    framehash_path: Path
    framehash_sha256: str
    capture_receipt_path: Path
    capture_receipt_sha256: str
    replay_sha256: str | None
    source_width: int
    source_height: int
    source_mode: str
    filename_pattern: str
    frames: tuple[SourceFrame, ...]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeCaptureError(f"{label} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise RuntimeCaptureError(f"{label} must be a SHA-256 hex string") from exc
    return value.lower()


def _require_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RuntimeCaptureError(f"{label} must be an integer >= {minimum}")
    return value


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeCaptureError(f"cannot read {label} JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeCaptureError(f"{label} must contain a JSON object")
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
        raise QuarantineError("an input or output path cannot equal the quarantine root")
    return resolved


def _require_file(path: Path, root: Path, label: str) -> Path:
    resolved = _require_quarantined_path(path, root, must_exist=True)
    if not resolved.is_file():
        raise RuntimeCaptureError(f"{label} must be a file: {resolved}")
    return resolved


def _require_directory(path: Path, root: Path, label: str) -> Path:
    resolved = _require_quarantined_path(path, root, must_exist=True)
    if not resolved.is_dir():
        raise RuntimeCaptureError(f"{label} must be a directory: {resolved}")
    return resolved


def _round_positive_fraction(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise RuntimeCaptureError("frame timestamps must be non-negative with positive time base")
    return (numerator + denominator // 2) // denominator


def _parse_hash_token(value: str) -> str:
    token = value.strip()
    if "=" in token:
        algorithm, token = token.split("=", 1)
        if algorithm.strip().lower() != "sha256":
            raise RuntimeCaptureError("framehash data row uses a non-SHA256 algorithm")
    return _normalize_sha256(token.strip(), "framehash frame digest")


def parse_ffmpeg_framehash(
    path: Path,
    *,
    stream_index: int,
) -> tuple[tuple[FramehashEntry, ...], int, int]:
    """Parse one RGB24 video stream from an FFmpeg v2 framehash file."""

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeCaptureError(f"cannot read FFmpeg framehash: {path}") from exc

    hash_algorithm: str | None = None
    time_bases: dict[int, tuple[int, int]] = {}
    dimensions: dict[int, tuple[int, int]] = {}
    media_types: dict[int, str] = {}
    raw_rows: list[tuple[int, int, int, int, int, str]] = []

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if match := _HASH_RE.fullmatch(line):
                candidate = match.group(1).lower()
                if hash_algorithm is not None and candidate != hash_algorithm:
                    raise RuntimeCaptureError("framehash declares conflicting hash algorithms")
                hash_algorithm = candidate
            elif match := _TB_RE.fullmatch(line):
                index, numerator, denominator = map(int, match.groups())
                if numerator <= 0 or denominator <= 0:
                    raise RuntimeCaptureError("framehash time base must be positive")
                value = (numerator, denominator)
                if index in time_bases and time_bases[index] != value:
                    raise RuntimeCaptureError("framehash declares conflicting time bases")
                time_bases[index] = value
            elif match := _DIMENSIONS_RE.fullmatch(line):
                index, width, height = map(int, match.groups())
                value = (width, height)
                if index in dimensions and dimensions[index] != value:
                    raise RuntimeCaptureError("framehash declares conflicting dimensions")
                dimensions[index] = value
            elif match := _MEDIA_TYPE_RE.fullmatch(line):
                index = int(match.group(1))
                value = match.group(2).lower()
                if index in media_types and media_types[index] != value:
                    raise RuntimeCaptureError("framehash declares conflicting media types")
                media_types[index] = value
            continue

        columns = [item.strip() for item in line.split(",")]
        if len(columns) != 6:
            raise RuntimeCaptureError(
                f"framehash line {line_number} must have exactly six columns"
            )
        try:
            row_stream, dts, pts, duration, byte_size = map(int, columns[:5])
        except ValueError as exc:
            raise RuntimeCaptureError(
                f"framehash line {line_number} has a non-integer timing column"
            ) from exc
        digest = _parse_hash_token(columns[5])
        if row_stream == stream_index:
            raw_rows.append((row_stream, dts, pts, duration, byte_size, digest))

    if hash_algorithm != "sha256":
        raise RuntimeCaptureError("framehash must declare #hash: SHA256")
    if media_types.get(stream_index) != "video":
        raise RuntimeCaptureError("selected framehash stream must declare media_type video")
    if stream_index not in time_bases:
        raise RuntimeCaptureError("selected framehash stream is missing a time base")
    if stream_index not in dimensions:
        raise RuntimeCaptureError("selected framehash stream is missing dimensions")
    if not raw_rows:
        raise RuntimeCaptureError("selected framehash stream contains no frames")
    if len(raw_rows) > MAX_SOURCE_FRAMES:
        raise RuntimeCaptureError("framehash frame count exceeds the safety limit")

    width, height = dimensions[stream_index]
    if (
        width <= 0
        or height <= 0
        or width > MAX_SOURCE_DIMENSION
        or height > MAX_SOURCE_DIMENSION
        or width * height > MAX_SOURCE_PIXELS
    ):
        raise RuntimeCaptureError("framehash dimensions exceed the native-size safety limits")
    expected_size = width * height * 3
    tb_numerator, tb_denominator = time_bases[stream_index]

    entries: list[FramehashEntry] = []
    previous_pts: int | None = None
    previous_timestamp: int | None = None
    for position, (row_stream, dts, pts, duration, byte_size, digest) in enumerate(raw_rows):
        if pts < 0:
            raise RuntimeCaptureError("framehash PTS values must be non-negative")
        if duration <= 0:
            raise RuntimeCaptureError("framehash durations must be positive")
        if byte_size != expected_size:
            raise RuntimeCaptureError(
                f"framehash RGB24 byte size mismatch at video frame {position}: "
                f"expected {expected_size}, got {byte_size}"
            )
        if previous_pts is not None and pts <= previous_pts:
            raise RuntimeCaptureError("framehash video PTS order must be strictly increasing")

        timestamp_us = _round_positive_fraction(
            pts * tb_numerator * 1_000_000,
            tb_denominator,
        )
        end_us = _round_positive_fraction(
            (pts + duration) * tb_numerator * 1_000_000,
            tb_denominator,
        )
        duration_us = end_us - timestamp_us
        if duration_us <= 0:
            raise RuntimeCaptureError("framehash duration rounds to zero microseconds")
        if previous_timestamp is not None and timestamp_us <= previous_timestamp:
            raise RuntimeCaptureError("framehash timestamps are not strictly increasing in microseconds")

        entries.append(
            FramehashEntry(
                sequence_position=position,
                stream_index=row_stream,
                dts=dts,
                pts=pts,
                duration=duration,
                byte_size=byte_size,
                rgb_sha256=digest,
                timestamp_us=timestamp_us,
                duration_us=duration_us,
            )
        )
        previous_pts = pts
        previous_timestamp = timestamp_us

    return tuple(entries), width, height


def _parse_filename_pattern(value: object) -> tuple[str, int, str]:
    if not isinstance(value, str):
        raise RuntimeCaptureError("png_sequence.filename_pattern must be a string")
    match = _FRAME_PATTERN_RE.fullmatch(value)
    if match is None:
        raise RuntimeCaptureError(
            "png_sequence.filename_pattern must look like frame_%06d.png"
        )
    return match.group(1), int(match.group(2)), value


def _expected_frame_name(prefix: str, digits: int, frame_number: int) -> str:
    return f"{prefix}{frame_number:0{digits}d}.png"


def _inspect_source_png(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
    expected_rgb_sha256: str,
) -> tuple[str, str]:
    png_sha256 = sha256_path(path)
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise RuntimeCaptureError(f"source frame is not a PNG: {path}")
            if getattr(image, "n_frames", 1) != 1:
                raise RuntimeCaptureError(f"source frame must be one non-animated PNG: {path}")
            if image.mode != "RGB":
                raise RuntimeCaptureError(f"source frame must be decoded RGB24, got {image.mode}: {path}")
            if image.size != (expected_width, expected_height):
                raise RuntimeCaptureError(
                    f"source frame dimensions mismatch for {path}: "
                    f"expected {expected_width}x{expected_height}, got {image.width}x{image.height}"
                )
            image.load()
            rgb_sha256 = sha256_bytes(image.tobytes())
    except RuntimeCaptureError:
        raise
    except (OSError, ValueError) as exc:
        raise RuntimeCaptureError(f"cannot decode source PNG: {path}") from exc
    if rgb_sha256 != expected_rgb_sha256:
        raise RuntimeCaptureError(
            f"source PNG RGB SHA-256 mismatch against framehash for {path}: "
            f"expected {expected_rgb_sha256}, got {rgb_sha256}"
        )
    return png_sha256, rgb_sha256


def _load_capture_inputs(
    png_directory: Path,
    framehash_path: Path,
    source_mkv_path: Path,
    capture_receipt_path: Path,
    root: Path,
    *,
    scene_id: str,
    archive_sha256: str,
) -> CaptureInputs:
    if not isinstance(scene_id, str) or not scene_id or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", scene_id
    ):
        raise RuntimeCaptureError("scene_id must be a non-empty safe identifier")
    archive_hash = _normalize_sha256(archive_sha256, "archive_sha256")

    png_root = _require_directory(png_directory, root, "PNG sequence directory")
    framehash = _require_file(framehash_path, root, "framehash")
    source_mkv = _require_file(source_mkv_path, root, "source MKV")
    if source_mkv.suffix.lower() != ".mkv":
        raise RuntimeCaptureError("source MKV must use the .mkv extension")
    receipt_path = _require_file(capture_receipt_path, root, "capture receipt")

    receipt, receipt_raw = _load_json(receipt_path, "capture receipt")
    if receipt.get("schema_version") != 1 or receipt.get("receipt_kind") != CAPTURE_RECEIPT_KIND:
        raise RuntimeCaptureError(
            f"capture receipt must use schema_version 1 and receipt_kind {CAPTURE_RECEIPT_KIND!r}"
        )
    if receipt.get("status") not in CAPTURE_RECEIPT_READY_STATUSES:
        raise RuntimeCaptureError("capture receipt status is not complete or ready")
    if receipt.get("scene_id") != scene_id:
        raise RuntimeCaptureError("capture receipt scene_id does not match the explicit scene")
    if _normalize_sha256(receipt.get("archive_sha256"), "receipt.archive_sha256") != archive_hash:
        raise RuntimeCaptureError("capture receipt archive SHA-256 does not match")

    expected_mkv_hash = _normalize_sha256(
        receipt.get("source_mkv_sha256"), "receipt.source_mkv_sha256"
    )
    actual_mkv_hash = sha256_path(source_mkv)
    if actual_mkv_hash != expected_mkv_hash:
        raise RuntimeCaptureError(
            f"source MKV SHA-256 mismatch: expected {expected_mkv_hash}, got {actual_mkv_hash}"
        )
    expected_framehash_hash = _normalize_sha256(
        receipt.get("framehash_sha256"), "receipt.framehash_sha256"
    )
    actual_framehash_hash = sha256_path(framehash)
    if actual_framehash_hash != expected_framehash_hash:
        raise RuntimeCaptureError(
            "FFmpeg framehash file SHA-256 does not match the capture receipt"
        )

    framehash_contract = receipt.get("framehash")
    if not isinstance(framehash_contract, dict):
        raise RuntimeCaptureError("capture receipt must contain a framehash object")
    stream_index = _require_integer(
        framehash_contract.get("stream_index"), "framehash.stream_index"
    )
    if str(framehash_contract.get("hash_algorithm", "")).lower() != "sha256":
        raise RuntimeCaptureError("capture receipt framehash algorithm must be sha256")
    if str(framehash_contract.get("pixel_format", "")).lower() != "rgb24":
        raise RuntimeCaptureError("capture receipt framehash pixel format must be rgb24")

    hash_entries, framehash_width, framehash_height = parse_ffmpeg_framehash(
        framehash,
        stream_index=stream_index,
    )

    sequence = receipt.get("png_sequence")
    if not isinstance(sequence, dict):
        raise RuntimeCaptureError("capture receipt must contain a png_sequence object")
    first_frame = _require_integer(sequence.get("first_frame"), "png_sequence.first_frame")
    frame_count = _require_integer(
        sequence.get("frame_count"), "png_sequence.frame_count", minimum=1
    )
    width = _require_integer(sequence.get("width"), "png_sequence.width", minimum=1)
    height = _require_integer(sequence.get("height"), "png_sequence.height", minimum=1)
    if sequence.get("mode") != "RGB":
        raise RuntimeCaptureError("png_sequence.mode must be RGB")
    prefix, digits, filename_pattern = _parse_filename_pattern(
        sequence.get("filename_pattern")
    )
    if len(str(first_frame + frame_count - 1)) > digits:
        raise RuntimeCaptureError("PNG frame numbers exceed filename pattern width")
    if frame_count != len(hash_entries):
        raise RuntimeCaptureError(
            "capture receipt PNG frame count does not equal framehash video frame count"
        )
    if (width, height) != (framehash_width, framehash_height):
        raise RuntimeCaptureError(
            "capture receipt PNG dimensions do not equal framehash video dimensions"
        )

    expected_names = [
        _expected_frame_name(prefix, digits, first_frame + position)
        for position in range(frame_count)
    ]
    actual_png_names = sorted(
        entry.name for entry in png_root.iterdir() if entry.is_file() and entry.suffix.lower() == ".png"
    )
    if actual_png_names != sorted(expected_names):
        missing = sorted(set(expected_names) - set(actual_png_names))
        unexpected = sorted(set(actual_png_names) - set(expected_names))
        raise RuntimeCaptureError(
            "PNG source sequence is not complete and continuous: "
            f"missing={missing[:5]}, unexpected={unexpected[:5]}"
        )

    source_frames: list[SourceFrame] = []
    for position, (name, hash_entry) in enumerate(
        zip(expected_names, hash_entries, strict=True)
    ):
        source_path = _require_file(png_root / name, root, "source PNG")
        png_hash, rgb_hash = _inspect_source_png(
            source_path,
            expected_width=width,
            expected_height=height,
            expected_rgb_sha256=hash_entry.rgb_sha256,
        )
        source_frames.append(
            SourceFrame(
                frame_number=first_frame + position,
                sequence_position=position,
                path=source_path,
                png_sha256=png_hash,
                rgb_sha256=rgb_hash,
                width=width,
                height=height,
                timestamp_us=hash_entry.timestamp_us,
                duration_us=hash_entry.duration_us,
            )
        )

    replay_value = receipt.get("replay_sha256")
    replay_sha256 = (
        None
        if replay_value is None
        else _normalize_sha256(replay_value, "receipt.replay_sha256")
    )
    return CaptureInputs(
        scene_id=scene_id,
        archive_sha256=archive_hash,
        source_mkv_path=source_mkv,
        source_mkv_sha256=actual_mkv_hash,
        framehash_path=framehash,
        framehash_sha256=actual_framehash_hash,
        capture_receipt_path=receipt_path,
        capture_receipt_sha256=sha256_bytes(receipt_raw),
        replay_sha256=replay_sha256,
        source_width=width,
        source_height=height,
        source_mode="RGB",
        filename_pattern=filename_pattern,
        frames=tuple(source_frames),
    )


def _validate_crop(crop: Sequence[int], width: int, height: int) -> tuple[int, int, int, int]:
    if isinstance(crop, (str, bytes)) or len(crop) != 4:
        raise RuntimeCaptureError("crop must contain exactly x y width height")
    values = tuple(
        _require_integer(value, f"crop[{index}]") for index, value in enumerate(crop)
    )
    x, y, crop_width, crop_height = values
    if (crop_width, crop_height) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        raise RuntimeCaptureError(
            f"crop output must be exactly {OUTPUT_WIDTH}x{OUTPUT_HEIGHT}"
        )
    if x + crop_width > width or y + crop_height > height:
        raise RuntimeCaptureError("explicit crop is outside the source frame bounds")
    return values


def _select_frames(
    frames: Sequence[SourceFrame],
    *,
    frame_range: Sequence[int] | None,
    frame_list: Sequence[int] | None,
) -> tuple[tuple[SourceFrame, ...], dict[str, Any]]:
    if (frame_range is None) == (frame_list is None):
        raise RuntimeCaptureError("provide exactly one explicit frame_range or frame_list")
    by_number = {frame.frame_number: frame for frame in frames}

    if frame_range is not None:
        if isinstance(frame_range, (str, bytes)) or len(frame_range) != 2:
            raise RuntimeCaptureError("frame_range must contain inclusive start and end")
        start = _require_integer(frame_range[0], "frame_range.start")
        end = _require_integer(frame_range[1], "frame_range.end")
        if end < start:
            raise RuntimeCaptureError("frame_range end must not precede start")
        numbers = list(range(start, end + 1))
        selection: dict[str, Any] = {
            "kind": "explicit_inclusive_range",
            "frame_range": [start, end],
        }
    else:
        assert frame_list is not None
        if isinstance(frame_list, (str, bytes)) or not frame_list:
            raise RuntimeCaptureError("frame_list must be a non-empty list")
        numbers = [
            _require_integer(value, f"frame_list[{index}]")
            for index, value in enumerate(frame_list)
        ]
        if any(right <= left for left, right in zip(numbers, numbers[1:])):
            raise RuntimeCaptureError("frame_list values must be strictly increasing and unique")
        selection = {"kind": "explicit_list", "frame_list": numbers}

    missing = [number for number in numbers if number not in by_number]
    if missing:
        raise RuntimeCaptureError(f"selected frame numbers are outside the source sequence: {missing[:5]}")
    return tuple(by_number[number] for number in numbers), selection


def _encode_explicit_crop(
    source: SourceFrame,
    crop: tuple[int, int, int, int],
) -> tuple[bytes, bytes]:
    # Recheck both hashes when the selected frame is reopened, closing the
    # validation-to-write window without relying on filesystem immutability.
    if sha256_path(source.path) != source.png_sha256:
        raise RuntimeCaptureError(f"source PNG changed after validation: {source.path}")
    try:
        with Image.open(source.path) as image:
            if image.format != "PNG" or image.mode != "RGB" or image.size != (
                source.width,
                source.height,
            ):
                raise RuntimeCaptureError(f"source PNG changed after validation: {source.path}")
            image.load()
            if sha256_bytes(image.tobytes()) != source.rgb_sha256:
                raise RuntimeCaptureError(f"source PNG RGB changed after validation: {source.path}")
            x, y, width, height = crop
            cropped = image.crop((x, y, x + width, y + height))
            if cropped.mode != "RGB" or cropped.size != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
                raise RuntimeCaptureError("explicit native crop produced an invalid raster")
            rgb = cropped.tobytes()
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG", optimize=False, compress_level=9)
            return rgb, buffer.getvalue()
    except RuntimeCaptureError:
        raise
    except (OSError, ValueError) as exc:
        raise RuntimeCaptureError(f"cannot crop source PNG: {source.path}") from exc


def _build_manifest(
    loaded: CaptureInputs,
    selected: Sequence[SourceFrame],
    selection: dict[str, Any],
    crop: tuple[int, int, int, int],
    rendered_hashes: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    for source, (rgb_sha256, png_sha256) in zip(
        selected, rendered_hashes, strict=True
    ):
        frames.append(
            {
                "frame": source.frame_number,
                "source_sequence_position": source.sequence_position,
                "png_path": source.path.name,
                "png_sha256": png_sha256,
                "rgb_sha256": rgb_sha256,
                "source_png_sha256": source.png_sha256,
                "source_rgb_sha256": source.rgb_sha256,
                "timestamp_us": source.timestamp_us,
                "duration_us": source.duration_us,
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "manifest_kind": OUTPUT_MANIFEST_KIND,
        "status": "ready",
        "scene_id": loaded.scene_id,
        "tool_version": TOOL_VERSION,
        "archive_sha256": loaded.archive_sha256,
        "source_mkv_sha256": loaded.source_mkv_sha256,
        "framehash_sha256": loaded.framehash_sha256,
        "capture_receipt_sha256": loaded.capture_receipt_sha256,
        "source_sequence": {
            "frame_count": len(loaded.frames),
            "first_frame": loaded.frames[0].frame_number,
            "last_frame": loaded.frames[-1].frame_number,
            "width": loaded.source_width,
            "height": loaded.source_height,
            "mode": loaded.source_mode,
            "filename_pattern": loaded.filename_pattern,
            "framehash_pixel_format": "rgb24",
        },
        "selection": selection,
        "crop": {
            "x": crop[0],
            "y": crop[1],
            "width": crop[2],
            "height": crop[3],
        },
        "native_raster": {"width": OUTPUT_WIDTH, "height": OUTPUT_HEIGHT, "mode": "RGB"},
        "transform_policy": {
            "crop": "external_explicit_rectangle_only",
            "scaling": "forbidden",
            "interpolation": "forbidden",
            "thresholding": "forbidden",
            "color_correction": "forbidden",
            "automatic_frame_selection": "forbidden",
        },
        "frames": frames,
        "semantic_interpretation": None,
        "automatic_s2_promotion": False,
        "authorization_effect": "none",
    }
    if loaded.replay_sha256 is not None:
        manifest["replay_sha256"] = loaded.replay_sha256
    return manifest


def export_runtime_rgb_frames(
    png_directory: Path,
    framehash_path: Path,
    source_mkv_path: Path,
    capture_receipt_path: Path,
    output_root: Path,
    quarantine_root: Path,
    *,
    scene_id: str,
    archive_sha256: str,
    crop: Sequence[int],
    frame_range: Sequence[int] | None = None,
    frame_list: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Validate a complete capture and atomically export an explicit crop/selection."""

    root = _resolve_quarantine_root(quarantine_root)
    output = _require_quarantined_path(output_root, root, must_exist=False)
    if output.exists():
        raise QuarantineError("output already exists")
    if not output.parent.is_dir():
        raise QuarantineError("output parent must already exist")

    # No output directory is created until the complete source sequence,
    # receipt, source MKV, framehash, selection, and crop have passed.
    loaded = _load_capture_inputs(
        png_directory,
        framehash_path,
        source_mkv_path,
        capture_receipt_path,
        root,
        scene_id=scene_id,
        archive_sha256=archive_sha256,
    )
    crop_contract = _validate_crop(crop, loaded.source_width, loaded.source_height)
    selected, selection = _select_frames(
        loaded.frames,
        frame_range=frame_range,
        frame_list=frame_list,
    )

    try:
        with tempfile.TemporaryDirectory(prefix=".pm2_runtime_rgb_", dir=output.parent) as temporary:
            staging = Path(temporary)
            rendered_hashes: list[tuple[str, str]] = []
            for source in selected:
                rgb, png = _encode_explicit_crop(source, crop_contract)
                rendered_hashes.append((sha256_bytes(rgb), sha256_bytes(png)))
                with (staging / source.path.name).open("xb") as handle:
                    handle.write(png)
            manifest = _build_manifest(
                loaded,
                selected,
                selection,
                crop_contract,
                rendered_hashes,
            )
            manifest_raw = (
                json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
            ).encode("ascii")
            with (staging / "manifest.json").open("xb") as handle:
                handle.write(manifest_raw)
            staging.rename(output)
    except RuntimeCaptureError:
        raise
    except OSError as exc:
        raise QuarantineError("failed to write runtime RGB output") from exc

    return {
        "schema_version": 1,
        "receipt_kind": OUTPUT_RECEIPT_KIND,
        "tool_version": TOOL_VERSION,
        "status": "ready_written",
        "scene_id": loaded.scene_id,
        "manifest_sha256": sha256_bytes(manifest_raw),
        "selected_frame_count": len(selected),
        "selected_frame_numbers": [frame.frame_number for frame in selected],
        "native_raster": {"width": OUTPUT_WIDTH, "height": OUTPUT_HEIGHT, "mode": "RGB"},
        "output_written": True,
        "semantic_interpretation": None,
        "automatic_s2_promotion": False,
        "authorization_effect": "none",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a quarantined FFmpeg RGB24 PNG/framehash capture and export an "
            "explicit 320x128 runtime frame selection without semantic interpretation."
        )
    )
    parser.add_argument("--png-directory", type=Path, required=True)
    parser.add_argument("--framehash", type=Path, required=True)
    parser.add_argument("--source-mkv", type=Path, required=True)
    parser.add_argument("--capture-receipt", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--quarantine-root", type=Path, required=True)
    parser.add_argument("--scene", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--crop", nargs=4, type=int, metavar=("X", "Y", "WIDTH", "HEIGHT"), required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--frame-range",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        help="inclusive source PNG frame-number range",
    )
    selection.add_argument(
        "--frame-list",
        nargs="+",
        type=int,
        metavar="FRAME",
        help="strictly increasing explicit source PNG frame numbers",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        receipt = export_runtime_rgb_frames(
            args.png_directory,
            args.framehash,
            args.source_mkv,
            args.capture_receipt,
            args.output_root,
            args.quarantine_root,
            scene_id=args.scene,
            archive_sha256=args.archive_sha256,
            crop=args.crop,
            frame_range=args.frame_range,
            frame_list=args.frame_list,
        )
    except RuntimeCaptureError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
