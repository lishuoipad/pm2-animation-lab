#!/usr/bin/env python3
"""Map fixed-build stable RGB runs to persistent offline activity ticks.

This R5 helper is deliberately narrower than an animation analyser.  It binds
one manually prepared stable-run ledger, one persistent R4 aggregate, and one
runtime selected-frame manifest, then reports only mechanically observed frame
and time spans.  It does not classify actions, infer RNG, promote evidence to
S2, or grant a production authorization.

Every input document is caller hash-bound.  File references carried by the R4
aggregate and the selected-frame manifest are resolved without traversal and
verified before a report can be returned.  No image or difference asset is
written; the only optional output is one JSON document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


TOOL_VERSION = "0.1.0"
STAGE_SCHEMA_VERSION = 1
R4_SCHEMA = "pm2_activity_persistent_composition_manifest/v1"
RUNTIME_SCHEMA_VERSION = 1
RUNTIME_MANIFEST_KIND = "pm2_activity_runtime_rgb_frames"
REPORT_SCHEMA_VERSION = 1
REPORT_KIND = "pm2_activity_stable_run_timing_map"
MAX_TICKS = 100_000
MAX_FRAME_COUNT = 10_000_000
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
TIME_BASE_RE = re.compile(r"([1-9][0-9]*)/([1-9][0-9]*)")
SAFE_SCENE_RE = re.compile(r"[A-Za-z0-9_.-]+")


class TimingError(ValueError):
    """Raised when timing evidence is malformed, unbound, or inconsistent."""


@dataclass(frozen=True)
class TimeBase:
    numerator: int
    denominator: int
    serialized: str

    def boundary_us(self, zero_based_frame_boundary: int) -> int:
        """Round an absolute frame boundary exactly as the capture bridge does."""

        if zero_based_frame_boundary < 0:
            raise TimingError("frame boundaries must be non-negative")
        numerator = zero_based_frame_boundary * self.numerator * 1_000_000
        return (numerator + self.denominator // 2) // self.denominator

    def span_us(self, start: int, end_exclusive: int) -> int:
        if end_exclusive < start:
            raise TimingError("time span end precedes start")
        return self.boundary_us(end_exclusive) - self.boundary_us(start)


@dataclass(frozen=True)
class StableRun:
    run_id: int
    start: int
    end: int
    frame_count: int
    rgb_sha256: str


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return sha256_bytes(raw)


def _require_int(value: object, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise TimingError(f"{field} must be an integer >= {minimum}")
    return value


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TimingError(f"{field} must be a non-empty string")
    return value


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise TimingError(f"{field} must be a SHA-256 hex string")
    return value.lower()


def _validate_declared_hashes(value: Any, field: str) -> None:
    """Reject every malformed field whose name declares SHA-256 content."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            child_field = f"{field}.{key}"
            if str(key).lower().endswith("sha256") and child is not None:
                if isinstance(child, list):
                    if not child:
                        raise TimingError(
                            f"{child_field} must be a non-empty SHA-256 list"
                        )
                    for index, digest in enumerate(child):
                        _require_sha256(digest, f"{child_field}[{index}]")
                else:
                    _require_sha256(child, child_field)
            else:
                _validate_declared_hashes(child, child_field)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_declared_hashes(child, f"{field}[{index}]")


def _load_hash_bound_json(
    path: Path, expected_sha256: str, label: str
) -> tuple[Path, dict[str, Any], str]:
    expected = _require_sha256(expected_sha256, f"{label}_sha256")
    try:
        resolved = path.resolve(strict=True)
        raw = resolved.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TimingError(f"cannot read {label} JSON: {path}") from exc
    if not resolved.is_file() or not isinstance(value, dict):
        raise TimingError(f"{label} must be a JSON object file")
    actual = sha256_bytes(raw)
    if actual != expected:
        raise TimingError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    _validate_declared_hashes(value, label)
    return resolved, value, actual


def _resolve_root(root: Path, label: str) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise TimingError(f"{label} is unavailable") from exc
    if not resolved.is_dir():
        raise TimingError(f"{label} must be a directory")
    return resolved


def _require_under(path: Path, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise TimingError(f"{label} escapes or is unavailable under its root") from exc
    if not resolved.is_file():
        raise TimingError(f"{label} must be a file")
    return resolved


def _resolve_root_relative(
    root: Path, value: object, label: str, *, suffix: str | None = None
) -> Path:
    relative_text = _require_string(value, label)
    relative = Path(relative_text)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise TimingError(f"{label} must be a traversal-free root-relative path")
    resolved = _require_under(root / relative, root, label)
    if suffix is not None and resolved.suffix.lower() != suffix.lower():
        raise TimingError(f"{label} must reference a {suffix} file")
    return resolved


def _verify_reference(
    reference: object,
    root: Path,
    label: str,
    *,
    path_field: str = "path",
    hash_field: str = "sha256",
    canonical_field: str | None = None,
) -> tuple[Path, dict[str, Any] | None]:
    if not isinstance(reference, Mapping):
        raise TimingError(f"{label} must be a hash-bound file reference")
    path = _resolve_root_relative(root, reference.get(path_field), f"{label}.{path_field}")
    expected = _require_sha256(reference.get(hash_field), f"{label}.{hash_field}")
    actual = sha256_path(path)
    if actual != expected:
        raise TimingError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    if canonical_field is None:
        return path, None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TimingError(f"{label} canonical reference is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise TimingError(f"{label} canonical reference must contain a JSON object")
    expected_canonical = _require_sha256(
        reference.get(canonical_field), f"{label}.{canonical_field}"
    )
    actual_canonical = canonical_sha256(parsed)
    if actual_canonical != expected_canonical:
        raise TimingError(
            f"{label} canonical SHA-256 mismatch: expected {expected_canonical}, "
            f"got {actual_canonical}"
        )
    return path, parsed


def _parse_crop(value: object, field: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y", "width", "height"}:
        raise TimingError(f"{field} must contain exactly x, y, width, and height")
    crop = {
        "x": _require_int(value["x"], f"{field}.x"),
        "y": _require_int(value["y"], f"{field}.y"),
        "width": _require_int(value["width"], f"{field}.width", minimum=1),
        "height": _require_int(value["height"], f"{field}.height", minimum=1),
    }
    return crop


def _parse_time_base(value: object) -> TimeBase:
    text = _require_string(value, "stage_visible_runs.time_base")
    match = TIME_BASE_RE.fullmatch(text)
    if match is None:
        raise TimingError("stage_visible_runs.time_base must be a positive N/D fraction")
    return TimeBase(int(match.group(1)), int(match.group(2)), text)


def _load_stage(value: Mapping[str, Any]) -> tuple[str, dict[str, int], TimeBase, int, tuple[StableRun, ...]]:
    if value.get("schema_version") != STAGE_SCHEMA_VERSION:
        raise TimingError("stage visible runs must use schema_version 1")
    scene = _require_string(value.get("scene_id"), "stage_visible_runs.scene_id")
    if SAFE_SCENE_RE.fullmatch(scene) is None:
        raise TimingError("stage_visible_runs.scene_id is not a safe identifier")
    crop = _parse_crop(value.get("crop"), "stage_visible_runs.crop")
    time_base = _parse_time_base(value.get("time_base"))
    frame_count = _require_int(
        value.get("frame_count"), "stage_visible_runs.frame_count", minimum=1
    )
    if frame_count > MAX_FRAME_COUNT:
        raise TimingError("stage visible-run frame_count exceeds the safety limit")
    raw_runs = value.get("visible_runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise TimingError("stage_visible_runs.visible_runs must be a non-empty list")
    if len(raw_runs) > MAX_TICKS:
        raise TimingError("stage visible-run count exceeds the safety limit")

    runs: list[StableRun] = []
    for index, raw in enumerate(raw_runs):
        if not isinstance(raw, Mapping) or set(raw) != {
            "run",
            "start_frame",
            "end_frame",
            "frame_count",
            "rgb_sha256",
        }:
            raise TimingError(f"visible_runs[{index}] has an unsupported shape")
        run_id = _require_int(raw["run"], f"visible_runs[{index}].run")
        start = _require_int(raw["start_frame"], f"visible_runs[{index}].start_frame")
        end = _require_int(raw["end_frame"], f"visible_runs[{index}].end_frame")
        count = _require_int(raw["frame_count"], f"visible_runs[{index}].frame_count", minimum=1)
        if end < start or count != end - start + 1:
            raise TimingError(f"visible_runs[{index}] has an inconsistent inclusive range")
        if end >= frame_count:
            raise TimingError(f"visible_runs[{index}] escapes the source frame sequence")
        if runs and start <= runs[-1].end:
            raise TimingError("visible runs must be strictly ordered and non-overlapping")
        runs.append(
            StableRun(
                run_id=run_id,
                start=start,
                end=end,
                frame_count=count,
                rgb_sha256=_require_sha256(
                    raw["rgb_sha256"], f"visible_runs[{index}].rgb_sha256"
                ),
            )
        )
    ids = [run.run_id for run in runs]
    if ids not in (list(range(len(runs))), list(range(1, len(runs) + 1))):
        raise TimingError("visible run identifiers must be contiguous and zero- or one-based")
    return scene, crop, time_base, frame_count, tuple(runs)


def _verify_aggregate_references(
    aggregate: Mapping[str, Any], evidence_root: Path, repository_root: Path
) -> None:
    inputs = aggregate.get("inputs")
    if not isinstance(inputs, Mapping):
        raise TimingError("R4 aggregate inputs must be an object")
    _require_sha256(inputs.get("fixed_archive_sha256"), "inputs.fixed_archive_sha256")
    _verify_reference(
        inputs.get("base_binding_manifest"), evidence_root, "inputs.base_binding_manifest"
    )
    _verify_reference(inputs.get("sequence_spec"), evidence_root, "inputs.sequence_spec")
    _verify_reference(
        inputs.get("sequence_timeline"),
        evidence_root,
        "inputs.sequence_timeline",
        hash_field="file_sha256",
        canonical_field="canonical_sha256",
    )

    report = inputs.get("pt1_structural_report")
    if not isinstance(report, Mapping):
        raise TimingError("inputs.pt1_structural_report must be a hash-bound reference")
    report_path = _resolve_root_relative(
        repository_root,
        report.get("repo_path"),
        "inputs.pt1_structural_report.repo_path",
    )
    expected_report = _require_sha256(
        report.get("sha256"), "inputs.pt1_structural_report.sha256"
    )
    if sha256_path(report_path) != expected_report:
        raise TimingError("inputs.pt1_structural_report SHA-256 mismatch")

    calls = aggregate.get("calls")
    if not isinstance(calls, list):
        raise TimingError("R4 aggregate calls must be a list")
    for index, call in enumerate(calls):
        if not isinstance(call, Mapping):
            raise TimingError(f"calls[{index}] must be an object")
        _verify_reference(
            call.get("timeline_projection"),
            evidence_root,
            f"calls[{index}].timeline_projection",
            hash_field="file_sha256",
            canonical_field="canonical_sha256",
        )
        _verify_reference(
            call.get("binding_manifest"),
            evidence_root,
            f"calls[{index}].binding_manifest",
            hash_field="file_sha256",
        )
        _verify_reference(
            call.get("composition_manifest"),
            evidence_root,
            f"calls[{index}].composition_manifest",
            hash_field="file_sha256",
        )


def _load_aggregate(
    value: Mapping[str, Any], evidence_root: Path, repository_root: Path
) -> tuple[str, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    if value.get("schema_version") != R4_SCHEMA:
        raise TimingError(f"R4 aggregate must use schema {R4_SCHEMA}")
    if value.get("stage") != "R4" or value.get("status") != "offline_candidate":
        raise TimingError("R4 aggregate must be an offline_candidate at stage R4")
    if value.get("authorization_effect") != "none":
        raise TimingError("R4 aggregate cannot grant authorization")
    scene = _require_string(value.get("scene_id"), "aggregate.scene_id")
    call_count = _require_int(value.get("call_count"), "aggregate.call_count", minimum=1)
    tick_count = _require_int(value.get("tick_count"), "aggregate.tick_count", minimum=1)
    if tick_count > MAX_TICKS:
        raise TimingError("R4 aggregate tick_count exceeds the safety limit")
    calls = value.get("calls")
    frames = value.get("frames")
    if not isinstance(calls, list) or len(calls) != call_count:
        raise TimingError("R4 aggregate call_count does not match calls")
    if not isinstance(frames, list) or len(frames) != tick_count:
        raise TimingError("R4 aggregate tick_count does not match frames")

    _verify_aggregate_references(value, evidence_root, repository_root)

    normalized_frames: list[dict[str, Any]] = []
    previous_stable = -1
    for tick, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            raise TimingError(f"aggregate.frames[{tick}] must be an object")
        if frame.get("global_tick") != tick:
            raise TimingError("aggregate global ticks must be contiguous and zero-based")
        call_tick = _require_int(frame.get("call_tick"), f"frames[{tick}].call_tick")
        stable = _require_int(
            frame.get("runtime_stable_zero_based_frame"),
            f"frames[{tick}].runtime_stable_zero_based_frame",
        )
        if stable <= previous_stable:
            raise TimingError("aggregate runtime stable frames must be strictly increasing")
        previous_stable = stable
        index_path = _resolve_root_relative(
            evidence_root, frame.get("index_path"), f"frames[{tick}].index_path"
        )
        expected_index = _require_sha256(
            frame.get("index_frame_sha256"), f"frames[{tick}].index_frame_sha256"
        )
        if sha256_path(index_path) != expected_index:
            raise TimingError(f"aggregate index frame SHA-256 mismatch at tick {tick}")
        if "index_frame_bytes" in frame and index_path.stat().st_size != _require_int(
            frame["index_frame_bytes"], f"frames[{tick}].index_frame_bytes", minimum=1
        ):
            raise TimingError(f"aggregate index frame byte count mismatch at tick {tick}")
        normalized_frames.append(
            {
                "global_tick": tick,
                "call_tick": call_tick,
                "runtime_stable_zero_based_frame": stable,
                "index_frame_sha256": expected_index,
            }
        )

    normalized_calls: list[dict[str, Any]] = []
    expected_start = 0
    for call_index, call in enumerate(calls):
        if not isinstance(call, Mapping) or call.get("call_index") != call_index:
            raise TimingError("aggregate call indices must be contiguous and zero-based")
        start = _require_int(call.get("global_tick_start"), f"calls[{call_index}].global_tick_start")
        end = _require_int(
            call.get("global_tick_end_exclusive"),
            f"calls[{call_index}].global_tick_end_exclusive",
            minimum=1,
        )
        if start != expected_start or end <= start or end > tick_count:
            raise TimingError("aggregate calls must form one contiguous tick partition")
        call_frames = call.get("frames")
        if not isinstance(call_frames, list) or len(call_frames) != end - start:
            raise TimingError(f"calls[{call_index}].frames does not match its tick range")
        stable_list = call.get("runtime_stable_zero_based_frames")
        expected_stable = [
            normalized_frames[tick]["runtime_stable_zero_based_frame"]
            for tick in range(start, end)
        ]
        if stable_list != expected_stable:
            raise TimingError(
                f"calls[{call_index}].runtime_stable_zero_based_frames does not match top frames"
            )
        for local_tick, (child, top) in enumerate(
            zip(call_frames, frames[start:end], strict=True)
        ):
            if not isinstance(child, Mapping) or dict(child) != dict(top):
                raise TimingError(
                    f"calls[{call_index}].frames[{local_tick}] does not equal its top frame"
                )
            if normalized_frames[start + local_tick]["call_tick"] != local_tick:
                raise TimingError(f"call {call_index} local ticks are not contiguous")
        normalized_calls.append(
            {
                "call_index": call_index,
                "global_tick_start": start,
                "global_tick_end_exclusive": end,
            }
        )
        expected_start = end
    if expected_start != tick_count:
        raise TimingError("aggregate calls do not cover every tick")
    return scene, tuple(normalized_frames), tuple(normalized_calls)


def _verify_runtime_png(
    manifest_path: Path,
    frame: Mapping[str, Any],
    crop: Mapping[str, int],
    field: str,
) -> tuple[str, str]:
    relative_text = _require_string(frame.get("png_path"), f"{field}.png_path")
    relative = Path(relative_text)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise TimingError(f"{field}.png_path must be traversal-free and relative")
    root = manifest_path.parent.resolve(strict=True)
    png_path = _require_under(root / relative, root, f"{field}.png_path")
    expected_png = _require_sha256(frame.get("png_sha256"), f"{field}.png_sha256")
    actual_png = sha256_path(png_path)
    if actual_png != expected_png:
        raise TimingError(f"{field} PNG SHA-256 mismatch")
    try:
        with Image.open(png_path) as image:
            if image.format != "PNG" or image.mode != "RGB":
                raise TimingError(f"{field} must reference one RGB PNG")
            if getattr(image, "n_frames", 1) != 1 or image.size != (
                crop["width"],
                crop["height"],
            ):
                raise TimingError(f"{field} PNG does not match the native crop")
            image.load()
            actual_rgb = sha256_bytes(image.tobytes())
    except TimingError:
        raise
    except (OSError, ValueError) as exc:
        raise TimingError(f"cannot decode {field} PNG") from exc
    expected_rgb = _require_sha256(frame.get("rgb_sha256"), f"{field}.rgb_sha256")
    if actual_rgb != expected_rgb:
        raise TimingError(f"{field} raw RGB SHA-256 mismatch")
    return actual_png, actual_rgb


def _load_runtime(
    manifest_path: Path,
    value: Mapping[str, Any],
    *,
    expected_scene: str,
    expected_crop: Mapping[str, int],
    expected_frame_count: int,
    stable_frames: Sequence[int],
    time_base: TimeBase,
) -> tuple[dict[str, Any], ...]:
    if (
        value.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or value.get("manifest_kind") != RUNTIME_MANIFEST_KIND
        or value.get("status") != "ready"
    ):
        raise TimingError("runtime manifest schema, kind, or ready status is invalid")
    if value.get("scene_id") != expected_scene:
        raise TimingError("runtime manifest scene_id does not match")
    if "replay_sha256" in value and value.get("replay_sha256") is not None:
        raise TimingError("this manual-capture timing contract requires no bound Replay")
    crop = _parse_crop(value.get("crop"), "runtime.crop")
    if crop != dict(expected_crop):
        raise TimingError("runtime crop does not match the stable-run ledger")
    native = value.get("native_raster")
    if not isinstance(native, Mapping) or native != {
        "width": crop["width"],
        "height": crop["height"],
        "mode": "RGB",
    }:
        raise TimingError("runtime native_raster does not match the crop")

    source = value.get("source_sequence")
    if not isinstance(source, Mapping):
        raise TimingError("runtime source_sequence must be an object")
    if _require_int(source.get("frame_count"), "source_sequence.frame_count", minimum=1) != expected_frame_count:
        raise TimingError("runtime and stable-run source frame counts differ")
    if source.get("first_frame") != 1 or source.get("last_frame") != expected_frame_count:
        raise TimingError("runtime source frame numbering must be one-based and complete")
    width = _require_int(source.get("width"), "source_sequence.width", minimum=1)
    height = _require_int(source.get("height"), "source_sequence.height", minimum=1)
    if crop["x"] + crop["width"] > width or crop["y"] + crop["height"] > height:
        raise TimingError("runtime crop escapes the source raster")
    if source.get("mode") != "RGB" or source.get("framehash_pixel_format") != "rgb24":
        raise TimingError("runtime source must be RGB/rgb24")

    selection = value.get("selection")
    expected_frame_numbers = [frame + 1 for frame in stable_frames]
    if not isinstance(selection, Mapping) or selection.get("kind") != "explicit_list":
        raise TimingError("runtime selection must be an explicit_list")
    if selection.get("frame_list") != expected_frame_numbers:
        raise TimingError("runtime explicit frame list does not bind the R4 stable frames")
    raw_frames = value.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) != len(stable_frames):
        raise TimingError("runtime selected frame count does not match R4 ticks")

    normalized: list[dict[str, Any]] = []
    for tick, (raw, stable) in enumerate(zip(raw_frames, stable_frames, strict=True)):
        if not isinstance(raw, Mapping):
            raise TimingError(f"runtime.frames[{tick}] must be an object")
        if raw.get("frame") != stable + 1 or raw.get("source_sequence_position") != stable:
            raise TimingError(
                f"runtime.frames[{tick}] does not bind one-based frame to zero-based position"
            )
        expected_timestamp = time_base.boundary_us(stable)
        expected_duration = time_base.span_us(stable, stable + 1)
        if raw.get("timestamp_us") != expected_timestamp or raw.get("duration_us") != expected_duration:
            raise TimingError(f"runtime.frames[{tick}] timing disagrees with the stage time base")
        _, rgb_sha = _verify_runtime_png(
            manifest_path, raw, crop, f"runtime.frames[{tick}]"
        )
        normalized.append(
            {
                "frame": stable + 1,
                "source_sequence_position": stable,
                "timestamp_us": expected_timestamp,
                "duration_us": expected_duration,
                "rgb_sha256": rgb_sha,
            }
        )
    return tuple(normalized)


def _range_payload(time_base: TimeBase, start: int, end: int) -> dict[str, int]:
    return {
        "start_zero_based_frame": start,
        "end_zero_based_frame_inclusive": end,
        "frame_count": end - start + 1,
        "start_timestamp_us": time_base.boundary_us(start),
        "end_exclusive_timestamp_us": time_base.boundary_us(end + 1),
        "duration_us": time_base.span_us(start, end + 1),
    }


def _transition_payload(
    time_base: TimeBase,
    current: StableRun,
    following: StableRun,
    *,
    scope: str,
    next_tick: int,
) -> dict[str, Any]:
    start = current.end + 1
    end_exclusive = following.start
    frame_count = end_exclusive - start
    if frame_count < 0:
        raise TimingError("stable-run transitions overlap")
    return {
        "scope": scope,
        "next_global_tick": next_tick,
        "start_zero_based_frame": start if frame_count else None,
        "end_zero_based_frame_inclusive": end_exclusive - 1 if frame_count else None,
        "frame_count": frame_count,
        "start_timestamp_us": time_base.boundary_us(start),
        "end_exclusive_timestamp_us": time_base.boundary_us(end_exclusive),
        "duration_us": time_base.span_us(start, end_exclusive),
        "visual_content_classification": None,
    }


def _make_report(
    *,
    scene: str,
    crop: Mapping[str, int],
    time_base: TimeBase,
    frame_count: int,
    runs: Sequence[StableRun],
    aggregate_frames: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    runtime_frames: Sequence[Mapping[str, Any]],
    identities: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    if len(runtime_frames) != len(aggregate_frames):
        raise TimingError("one runtime selected frame is required for every persistent tick")

    selected_runs: list[StableRun] = []
    next_run_index = 0
    for tick, aggregate in enumerate(aggregate_frames):
        stable = aggregate["runtime_stable_zero_based_frame"]
        while next_run_index < len(runs) and runs[next_run_index].end < stable:
            next_run_index += 1
        if next_run_index >= len(runs) or not (
            runs[next_run_index].start <= stable <= runs[next_run_index].end
        ):
            raise TimingError(
                f"R4 stable frame for tick {tick} is not covered by a visible run"
            )
        if selected_runs and runs[next_run_index].run_id <= selected_runs[-1].run_id:
            raise TimingError(
                "persistent ticks must map to distinct, strictly increasing visible runs"
            )
        selected_runs.append(runs[next_run_index])

    tick_to_call: list[int] = [-1] * len(aggregate_frames)
    for call in calls:
        for tick in range(call["global_tick_start"], call["global_tick_end_exclusive"]):
            tick_to_call[tick] = call["call_index"]
    if any(index < 0 for index in tick_to_call):
        raise TimingError("call partition does not cover every timing tick")

    ticks: list[dict[str, Any]] = []
    for tick, (run, aggregate, runtime) in enumerate(
        zip(selected_runs, aggregate_frames, runtime_frames, strict=True)
    ):
        stable = aggregate["runtime_stable_zero_based_frame"]
        if not run.start <= stable <= run.end:
            raise TimingError(f"R4 stable frame for tick {tick} is outside visible run {run.run_id}")
        if runtime["rgb_sha256"] != run.rgb_sha256:
            raise TimingError(f"stable visible-run RGB SHA-256 mismatch at tick {tick}")
        call_index = tick_to_call[tick]
        transition: dict[str, Any]
        if tick + 1 < len(selected_runs):
            next_call = tick_to_call[tick + 1]
            scope = (
                "within_call_transition"
                if next_call == call_index
                else "call_boundary_extra_hold_or_transition"
            )
            transition = _transition_payload(
                time_base,
                run,
                selected_runs[tick + 1],
                scope=scope,
                next_tick=tick + 1,
            )
        else:
            transition = {
                "scope": "terminal_unmeasured_after_last_stable_run",
                "next_global_tick": None,
                "start_zero_based_frame": None,
                "end_zero_based_frame_inclusive": None,
                "frame_count": None,
                "start_timestamp_us": None,
                "end_exclusive_timestamp_us": None,
                "duration_us": None,
                "visual_content_classification": None,
                "reason": "no_following_stable_run_or_return_boundary_is_bound",
            }
        ticks.append(
            {
                "global_tick": tick,
                "call_index": call_index,
                "call_tick": aggregate["call_tick"],
                "runtime_stable_zero_based_frame": stable,
                "runtime_selected_one_based_frame": runtime["frame"],
                "stable_run": {
                    "run": run.run_id,
                    **_range_payload(time_base, run.start, run.end),
                    "rgb_sha256": run.rgb_sha256,
                },
                "transition_after": transition,
            }
        )

    call_reports: list[dict[str, Any]] = []
    for call in calls:
        start = call["global_tick_start"]
        end = call["global_tick_end_exclusive"]
        selected_ticks = ticks[start:end]
        stable_frames_total = sum(item["stable_run"]["frame_count"] for item in selected_ticks)
        stable_duration_total = sum(item["stable_run"]["duration_us"] for item in selected_ticks)
        internal = [
            item["transition_after"]
            for item in selected_ticks[:-1]
            if item["transition_after"]["scope"] == "within_call_transition"
        ]
        if len(internal) != max(0, len(selected_ticks) - 1):
            raise TimingError("call-internal transitions are not structurally separate")
        boundary = selected_ticks[-1]["transition_after"]
        first_run = selected_runs[start]
        last_run = selected_runs[end - 1]
        call_reports.append(
            {
                "call_index": call["call_index"],
                "global_tick_start": start,
                "global_tick_end_exclusive": end,
                "tick_count": end - start,
                "stable_frame_count_total": stable_frames_total,
                "stable_duration_us_total": stable_duration_total,
                "within_call_transition_frame_count_total": sum(
                    item["frame_count"] for item in internal
                ),
                "within_call_transition_duration_us_total": sum(
                    item["duration_us"] for item in internal
                ),
                "observed_call_span": _range_payload(
                    time_base, first_run.start, last_run.end
                ),
                "boundary_after": boundary,
            }
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "tool_version": TOOL_VERSION,
        "status": "machine_timing_mapped_with_explicit_limits",
        "scene_id": scene,
        "inputs": identities,
        "capture_contract": {
            "capture_method": "manual_fixed_build_capture",
            "replay_bound": False,
            "source_frame_count": frame_count,
            "source_frame_numbering": {
                "stage_and_sequence_position": "zero_based",
                "selected_png_frame": "one_based",
            },
            "time_base_seconds_per_frame": time_base.serialized,
            "crop": dict(crop),
        },
        "mapping": {
            "tick_count": len(ticks),
            "call_count": len(call_reports),
            "stage_visible_run_count": len(runs),
            "one_selected_stable_run_per_tick": True,
            "unselected_visible_runs_are_not_semantically_classified": True,
            "ticks": ticks,
            "calls": call_reports,
        },
        "evidence_limits": {
            "rng_relationship": "compatible_witness_not_actual_rng",
            "last_tick_duration_scope": "observed_stable_run_only",
            "last_teardown_or_hold_duration": None,
            "return_boundary_bound": False,
            "automatic_semantic_interpretation": False,
            "semantic_interpretation": None,
            "automatic_s2_promotion": False,
            "s2_status": "not_established_by_timing_mapping",
            "authorization_effect": "none",
        },
        "safety": {
            "contains_image_bytes": False,
            "writes_images": False,
            "writes_difference_assets": False,
            "output_kind": "safe_json_only",
        },
    }


def build_timing_report(
    stage_visible_runs_path: Path,
    aggregate_path: Path,
    runtime_manifest_path: Path,
    *,
    stage_visible_runs_sha256: str,
    aggregate_sha256: str,
    runtime_manifest_sha256: str,
    evidence_root: Path,
    repository_root: Path,
) -> dict[str, Any]:
    """Validate all three evidence streams and return a safe timing report."""

    evidence = _resolve_root(evidence_root, "evidence_root")
    repository = _resolve_root(repository_root, "repository_root")
    stage_path, stage, stage_sha = _load_hash_bound_json(
        stage_visible_runs_path, stage_visible_runs_sha256, "stage_visible_runs"
    )
    r4_path, aggregate, r4_sha = _load_hash_bound_json(
        aggregate_path, aggregate_sha256, "aggregate"
    )
    runtime_path, runtime, runtime_sha = _load_hash_bound_json(
        runtime_manifest_path, runtime_manifest_sha256, "runtime_manifest"
    )
    _require_under(stage_path, evidence, "stage_visible_runs")
    _require_under(r4_path, evidence, "aggregate")
    _require_under(runtime_path, evidence, "runtime_manifest")

    scene, crop, time_base, frame_count, runs = _load_stage(stage)
    aggregate_scene, aggregate_frames, calls = _load_aggregate(
        aggregate, evidence, repository
    )
    if aggregate_scene != scene:
        raise TimingError("stage and R4 aggregate scene_id values differ")
    stable_frames = [
        frame["runtime_stable_zero_based_frame"] for frame in aggregate_frames
    ]
    runtime_frames = _load_runtime(
        runtime_path,
        runtime,
        expected_scene=scene,
        expected_crop=crop,
        expected_frame_count=frame_count,
        stable_frames=stable_frames,
        time_base=time_base,
    )
    identities = {
        "stage_visible_runs": {"sha256": stage_sha, "validation": "exact"},
        "persistent_r4_aggregate": {"sha256": r4_sha, "validation": "exact"},
        "runtime_selected_frame_manifest": {
            "sha256": runtime_sha,
            "validation": "exact",
        },
    }
    return _make_report(
        scene=scene,
        crop=crop,
        time_base=time_base,
        frame_count=frame_count,
        runs=runs,
        aggregate_frames=aggregate_frames,
        calls=calls,
        runtime_frames=runtime_frames,
        identities=identities,
    )


def _atomic_write_json(path: Path, report: Mapping[str, Any]) -> None:
    if path.suffix.lower() != ".json":
        raise TimingError("output must use the .json suffix")
    if not path.parent.is_dir():
        raise TimingError("output parent directory does not exist")
    if os.path.lexists(path):
        raise TimingError("output already exists")
    serialized = (
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        # Publishing with a same-volume hard link is atomic and, unlike
        # Path.replace(), can never overwrite a destination that appears
        # after the precheck.
        os.link(temporary_name, path)
    except FileExistsError as exc:
        raise TimingError("output already exists") from exc
    except OSError as exc:
        raise TimingError("failed to atomically write timing JSON") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Map hash-bound PM2 stable capture runs to persistent R4 ticks."
    )
    parser.add_argument("--stage-visible-runs", type=Path, required=True)
    parser.add_argument("--stage-visible-runs-sha256", required=True)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--aggregate-sha256", required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output", default="-", help="new JSON path, or '-' for stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report = build_timing_report(
            args.stage_visible_runs,
            args.aggregate,
            args.runtime_manifest,
            stage_visible_runs_sha256=args.stage_visible_runs_sha256,
            aggregate_sha256=args.aggregate_sha256,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
            evidence_root=args.evidence_root,
            repository_root=args.repository_root,
        )
        if args.output == "-":
            print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            _atomic_write_json(Path(args.output), report)
    except TimingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
