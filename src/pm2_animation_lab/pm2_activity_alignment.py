"""Exact R5 alignment for PM2 runtime frames and offline tick reconstructions.

The tool never scales, interpolates, thresholds, or writes images.  It compares
native-size RGB pixels exactly and emits only hashes, dimensions, intervals,
counts, bounding boxes, provenance, and empty human-review fields.  A visual
difference is deliberately not assigned a root cause automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageChops


TOOL_VERSION = "0.1.0"
RUNTIME_MANIFEST_KIND = "pm2_activity_runtime_rgb_frames"
OFFLINE_MANIFEST_KIND = "pm2_activity_offline_rgb_ticks"
READY_STATUSES = {"ready", "complete"}
CLASSIFICATION_SLOTS = (
    "format",
    "palette",
    "video_mode",
    "timing_or_sampling",
    "version",
)
MANUAL_CONCLUSIONS = ("clear", "partial", "unreadable")
DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_DIMENSION = 4096
DEFAULT_MAX_PIXELS_PER_IMAGE = 16_777_216


class AlignmentError(ValueError):
    """Raised when an existing alignment input is malformed or unsafe."""


@dataclass(frozen=True)
class VisualEntry:
    position: int
    identifier: int
    png_path: Path
    png_sha256: str
    rgb_sha256: str
    width: int
    height: int
    source_mode: str
    timestamp_us: int | None
    duration_us: int | None
    logical_wait: int | float | None
    milestones: tuple[str, ...]
    pattern_numbers: tuple[int, ...] | None


@dataclass(frozen=True)
class VisualRun:
    index: int
    start_position: int
    end_position: int
    rgb_sha256: str
    width: int
    height: int


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AlignmentError(f"cannot read JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AlignmentError(f"manifest {path} must contain a JSON object")
    return value


def _manifest_identity(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": sha256_path(path)}


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _resolve_relative_file(manifest_path: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AlignmentError(f"{label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise AlignmentError(f"{label} must be relative to its manifest")
    root = manifest_path.parent.resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AlignmentError(f"{label} escapes its manifest directory") from exc
    if resolved.suffix.lower() != ".png":
        raise AlignmentError(f"{label} must reference a PNG")
    if not resolved.is_file():
        raise AlignmentError(f"{label} does not exist: {resolved}")
    return resolved


def _inspect_png(
    path: Path,
    *,
    max_dimension: int,
    max_pixels: int,
) -> tuple[str, int, int, str]:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise AlignmentError(f"image is not a PNG: {path}")
            if getattr(image, "n_frames", 1) != 1:
                raise AlignmentError(f"animated PNG is not accepted as one exact frame: {path}")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise AlignmentError(f"image has empty dimensions: {path}")
            if width > max_dimension or height > max_dimension or width * height > max_pixels:
                raise AlignmentError(
                    f"image dimensions {width}x{height} exceed the configured native-size limit"
                )
            source_mode = image.mode
            rgb = image.convert("RGB")
            rgb.load()
            rgb_sha256 = hashlib.sha256(rgb.tobytes()).hexdigest()
    except AlignmentError:
        raise
    except (OSError, ValueError) as exc:
        raise AlignmentError(f"cannot decode PNG {path}: {exc}") from exc
    return rgb_sha256, width, height, source_mode


def _parse_milestones(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise AlignmentError(f"{label} milestones must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise AlignmentError(f"{label} repeats a milestone")
    return tuple(value)


def _parse_pattern_numbers(value: object, label: str) -> tuple[int, ...] | None:
    if value is None:
        return None
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in values):
        raise AlignmentError(f"{label} pattern_numbers must contain non-negative integers")
    return tuple(values)


def _load_sequence(
    manifest_path: Path,
    *,
    expected_kind: str,
    list_field: str,
    identifier_field: str,
    max_entries: int,
    max_dimension: int,
    max_pixels: int,
    require_timestamps: bool,
) -> tuple[dict[str, Any], list[VisualEntry]]:
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("manifest_kind") != expected_kind:
        raise AlignmentError(
            f"{manifest_path} must use schema_version 1 and manifest_kind {expected_kind!r}"
        )
    raw_entries = manifest.get(list_field)
    if not isinstance(raw_entries, list) or not raw_entries:
        raise AlignmentError(f"{manifest_path} must contain a non-empty {list_field} list")
    if len(raw_entries) > max_entries:
        raise AlignmentError(f"{list_field} count {len(raw_entries)} exceeds limit {max_entries}")

    entries: list[VisualEntry] = []
    previous_identifier: int | None = None
    previous_timestamp: int | None = None
    seen_milestones: set[str] = set()
    for position, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise AlignmentError(f"{list_field}[{position}] must be an object")
        identifier = raw.get(identifier_field)
        if not isinstance(identifier, int) or isinstance(identifier, bool) or identifier < 0:
            raise AlignmentError(f"{list_field}[{position}].{identifier_field} must be non-negative integer")
        if previous_identifier is not None and identifier <= previous_identifier:
            raise AlignmentError(f"{identifier_field} values must be strictly increasing")
        previous_identifier = identifier

        png_path = _resolve_relative_file(
            manifest_path,
            raw.get("png_path"),
            f"{list_field}[{position}].png_path",
        )
        declared_png_sha256 = raw.get("png_sha256")
        if not _is_sha256(declared_png_sha256):
            raise AlignmentError(f"{list_field}[{position}].png_sha256 must be a SHA-256 hex string")
        actual_png_sha256 = sha256_path(png_path)
        if actual_png_sha256.lower() != declared_png_sha256.lower():
            raise AlignmentError(
                f"PNG SHA-256 mismatch for {png_path}: expected {declared_png_sha256}, got {actual_png_sha256}"
            )
        rgb_sha256, width, height, source_mode = _inspect_png(
            png_path,
            max_dimension=max_dimension,
            max_pixels=max_pixels,
        )
        declared_rgb_sha256 = raw.get("rgb_sha256")
        if declared_rgb_sha256 is not None:
            if not _is_sha256(declared_rgb_sha256):
                raise AlignmentError(
                    f"{list_field}[{position}].rgb_sha256 must be a SHA-256 hex string"
                )
            if rgb_sha256 != declared_rgb_sha256.lower():
                raise AlignmentError(
                    f"raw RGB SHA-256 mismatch for {png_path}: "
                    f"expected {declared_rgb_sha256}, got {rgb_sha256}"
                )

        timestamp = raw.get("timestamp_us")
        if timestamp is not None and (
            not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0
        ):
            raise AlignmentError(f"{list_field}[{position}].timestamp_us must be non-negative integer")
        if require_timestamps and timestamp is None:
            raise AlignmentError(f"{list_field}[{position}].timestamp_us is required")
        if timestamp is not None and previous_timestamp is not None and timestamp <= previous_timestamp:
            raise AlignmentError("runtime timestamp_us values must be strictly increasing when present")
        if timestamp is not None:
            previous_timestamp = timestamp

        duration = raw.get("duration_us")
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0
        ):
            raise AlignmentError(f"{list_field}[{position}].duration_us must be a positive integer")
        logical_wait = raw.get("logical_wait")
        if logical_wait is not None and (
            not isinstance(logical_wait, (int, float))
            or isinstance(logical_wait, bool)
            or logical_wait < 0
        ):
            raise AlignmentError(f"{list_field}[{position}].logical_wait must be non-negative number")
        milestones = _parse_milestones(raw.get("milestones"), f"{list_field}[{position}]")
        duplicate_across_entries = seen_milestones.intersection(milestones)
        if duplicate_across_entries:
            raise AlignmentError(
                f"milestones must identify one entry each; repeated {sorted(duplicate_across_entries)}"
            )
        seen_milestones.update(milestones)

        entries.append(
            VisualEntry(
                position=position,
                identifier=identifier,
                png_path=png_path,
                png_sha256=actual_png_sha256,
                rgb_sha256=rgb_sha256,
                width=width,
                height=height,
                source_mode=source_mode,
                timestamp_us=timestamp,
                duration_us=duration,
                logical_wait=logical_wait,
                milestones=milestones,
                pattern_numbers=_parse_pattern_numbers(
                    raw.get("pattern_numbers", raw.get("pattern_number")),
                    f"{list_field}[{position}]",
                ),
            )
        )
    return manifest, entries


def _collapse_runs(entries: list[VisualEntry]) -> tuple[list[VisualRun], list[int]]:
    runs: list[VisualRun] = []
    position_to_run: list[int] = []
    for entry in entries:
        signature = (entry.rgb_sha256, entry.width, entry.height)
        if runs and signature == (runs[-1].rgb_sha256, runs[-1].width, runs[-1].height):
            previous = runs[-1]
            runs[-1] = VisualRun(
                index=previous.index,
                start_position=previous.start_position,
                end_position=entry.position,
                rgb_sha256=previous.rgb_sha256,
                width=previous.width,
                height=previous.height,
            )
        else:
            runs.append(
                VisualRun(
                    index=len(runs),
                    start_position=entry.position,
                    end_position=entry.position,
                    rgb_sha256=entry.rgb_sha256,
                    width=entry.width,
                    height=entry.height,
                )
            )
        position_to_run.append(runs[-1].index)
    return runs, position_to_run


def _first_visible_change(entries: list[VisualEntry]) -> int | None:
    for position in range(1, len(entries)):
        current = entries[position]
        previous = entries[position - 1]
        if (current.width, current.height, current.rgb_sha256) != (
            previous.width,
            previous.height,
            previous.rgb_sha256,
        ):
            return position
    return None


def _milestone_positions(entries: list[VisualEntry]) -> dict[str, int]:
    return {
        milestone: entry.position
        for entry in entries
        for milestone in entry.milestones
    }


def _build_run_mapping(
    runtime_entries: list[VisualEntry],
    offline_entries: list[VisualEntry],
) -> tuple[dict[int, int], list[dict[str, object]], dict[str, object]]:
    runtime_runs, runtime_position_to_run = _collapse_runs(runtime_entries)
    offline_runs, offline_position_to_run = _collapse_runs(offline_entries)
    runtime_first = _first_visible_change(runtime_entries)
    offline_first = _first_visible_change(offline_entries)
    runtime_milestones = _milestone_positions(runtime_entries)
    offline_milestones = _milestone_positions(offline_entries)

    raw_anchors: list[tuple[int, int, str]] = [(0, 0, "start")]
    first_change_status = "paired"
    if runtime_first is not None and offline_first is not None:
        raw_anchors.append((runtime_first, offline_first, "first_visible_change"))
    elif runtime_first is None and offline_first is None:
        first_change_status = "absent_in_both"
    else:
        first_change_status = "missing_in_one_sequence"
    shared_milestones = sorted(set(runtime_milestones).intersection(offline_milestones))
    for milestone in shared_milestones:
        raw_anchors.append(
            (runtime_milestones[milestone], offline_milestones[milestone], f"milestone:{milestone}")
        )
    raw_anchors.append((len(runtime_entries) - 1, len(offline_entries) - 1, "end"))

    grouped: dict[tuple[int, int], list[str]] = {}
    for runtime_position, offline_position, basis in raw_anchors:
        pair = (
            runtime_position_to_run[runtime_position],
            offline_position_to_run[offline_position],
        )
        grouped.setdefault(pair, []).append(basis)
    anchors = [
        {"runtime_run": pair[0], "offline_run": pair[1], "basis": bases}
        for pair, bases in sorted(grouped.items())
    ]

    anchor_conflicts: list[dict[str, object]] = []
    runtime_targets: dict[int, set[int]] = {}
    offline_targets: dict[int, set[int]] = {}
    for anchor in anchors:
        runtime_targets.setdefault(int(anchor["runtime_run"]), set()).add(int(anchor["offline_run"]))
        offline_targets.setdefault(int(anchor["offline_run"]), set()).add(int(anchor["runtime_run"]))
    for run, targets in runtime_targets.items():
        if len(targets) > 1:
            anchor_conflicts.append({"side": "runtime", "run": run, "targets": sorted(targets)})
    for run, targets in offline_targets.items():
        if len(targets) > 1:
            anchor_conflicts.append({"side": "offline", "run": run, "targets": sorted(targets)})

    monotonic = all(
        int(current["offline_run"]) <= int(following["offline_run"])
        for current, following in zip(anchors, anchors[1:])
    )
    mapping: dict[int, int] = {}
    mapping_basis: dict[int, list[str]] = {}
    segment_issues: list[dict[str, object]] = []
    if not anchor_conflicts and monotonic:
        for anchor in anchors:
            runtime_run = int(anchor["runtime_run"])
            offline_run = int(anchor["offline_run"])
            mapping[runtime_run] = offline_run
            mapping_basis.setdefault(runtime_run, []).extend(anchor["basis"])
        for left, right in zip(anchors, anchors[1:]):
            runtime_start, runtime_end = int(left["runtime_run"]), int(right["runtime_run"])
            offline_start, offline_end = int(left["offline_run"]), int(right["offline_run"])
            runtime_span = runtime_end - runtime_start
            offline_span = offline_end - offline_start
            if runtime_span != offline_span:
                segment_issues.append(
                    {
                        "runtime_run_range": [runtime_start, runtime_end],
                        "offline_run_range": [offline_start, offline_end],
                        "reason": "visible_run_count_differs_between_anchors",
                    }
                )
                continue
            for offset in range(runtime_span + 1):
                runtime_run = runtime_start + offset
                offline_run = offline_start + offset
                existing = mapping.get(runtime_run)
                if existing is not None and existing != offline_run:
                    segment_issues.append(
                        {
                            "runtime_run": runtime_run,
                            "existing_offline_run": existing,
                            "new_offline_run": offline_run,
                            "reason": "ordinal_mapping_conflict",
                        }
                    )
                    continue
                mapping[runtime_run] = offline_run
                mapping_basis.setdefault(runtime_run, []).append("ordinal_between_structural_anchors")
    else:
        segment_issues.append(
            {
                "reason": "anchors_conflict_or_cross",
                "anchor_conflicts": anchor_conflicts,
                "monotonic": monotonic,
            }
        )

    anchor_report = {
        "first_visible_change": {
            "status": first_change_status,
            "runtime_frame": None if runtime_first is None else runtime_entries[runtime_first].identifier,
            "offline_tick": None if offline_first is None else offline_entries[offline_first].identifier,
        },
        "shared_milestones": shared_milestones,
        "runtime_only_milestones": sorted(set(runtime_milestones) - set(offline_milestones)),
        "offline_only_milestones": sorted(set(offline_milestones) - set(runtime_milestones)),
        "anchors": anchors,
        "anchor_conflicts": anchor_conflicts,
        "monotonic": monotonic,
        "runtime_visible_run_count": len(runtime_runs),
        "offline_visible_run_count": len(offline_runs),
        "segment_issues": segment_issues,
    }
    mapping_report = [
        {
            "runtime_run": runtime_run,
            "offline_run": offline_run,
            "basis": sorted(set(mapping_basis.get(runtime_run, []))),
        }
        for runtime_run, offline_run in sorted(mapping.items())
    ]
    return mapping, mapping_report, {
        "anchors": anchor_report,
        "runtime_runs": runtime_runs,
        "offline_runs": offline_runs,
    }


def _compare_native_rgb(runtime: VisualEntry, offline: VisualEntry) -> dict[str, object]:
    dimensions_match = (runtime.width, runtime.height) == (offline.width, offline.height)
    base: dict[str, object] = {
        "runtime_rgb_sha256": runtime.rgb_sha256,
        "offline_rgb_sha256": offline.rgb_sha256,
        "runtime_dimensions": [runtime.width, runtime.height],
        "offline_dimensions": [offline.width, offline.height],
        "dimensions_match": dimensions_match,
        "exact_rgb_match": dimensions_match and runtime.rgb_sha256 == offline.rgb_sha256,
    }
    if not dimensions_match:
        base.update(
            {
                "pixel_difference_count": None,
                "pixel_difference_bbox_ltrb": None,
                "maximum_channel_difference": None,
                "comparison_status": "size_mismatch",
            }
        )
        return base

    with Image.open(runtime.png_path) as runtime_image, Image.open(offline.png_path) as offline_image:
        runtime_rgb = runtime_image.convert("RGB")
        offline_rgb = offline_image.convert("RGB")
        difference = ImageChops.difference(runtime_rgb, offline_rgb)
        bbox = difference.getbbox()
        if bbox is None:
            changed_pixels = 0
            maximum = 0
        else:
            pixels = difference.tobytes()
            changed_pixels = 0
            maximum = 0
            for offset in range(0, len(pixels), 3):
                pixel_maximum = max(pixels[offset : offset + 3])
                if pixel_maximum:
                    changed_pixels += 1
                    maximum = max(maximum, pixel_maximum)
        base.update(
            {
                "pixel_difference_count": changed_pixels,
                "pixel_difference_bbox_ltrb": None if bbox is None else list(bbox),
                "maximum_channel_difference": maximum,
                "comparison_status": "exact" if bbox is None else "different",
            }
        )
    return base


def _classification_slots(comparison: dict[str, object]) -> dict[str, object]:
    exact = comparison["comparison_status"] == "exact"
    size_mismatch = comparison["comparison_status"] == "size_mismatch"
    slots = {
        slot: {
            "status": "not_applicable" if exact else "unassessed",
            "evidence": [],
        }
        for slot in CLASSIFICATION_SLOTS
    }
    if size_mismatch:
        slots["video_mode"] = {
            "status": "candidate_not_conclusion",
            "evidence": ["native_dimensions_differ"],
        }
        slots["format"] = {
            "status": "candidate_not_conclusion",
            "evidence": ["native_dimensions_differ"],
        }
    return {
        "selected": None,
        "selection_requires_source_or_human_investigation": not exact,
        "slots": slots,
    }


def _range_payload(entries: list[VisualEntry], run: VisualRun, side: str) -> dict[str, object]:
    first = entries[run.start_position]
    last = entries[run.end_position]
    payload: dict[str, object] = {
        "position_range": [run.start_position, run.end_position],
        f"{side}_id_range": [first.identifier, last.identifier],
        "rgb_sha256": run.rgb_sha256,
        "dimensions": [run.width, run.height],
    }
    if side == "frame":
        payload["timestamp_us_range"] = [first.timestamp_us, last.timestamp_us]
    else:
        payload["logical_wait_range"] = [first.logical_wait, last.logical_wait]
    return payload


def _explicit_pattern_status(runtime_run_entries: list[VisualEntry], offline_run_entries: list[VisualEntry]) -> dict[str, object]:
    runtime_values = {entry.pattern_numbers for entry in runtime_run_entries}
    offline_values = {entry.pattern_numbers for entry in offline_run_entries}
    if None in runtime_values or None in offline_values:
        return {"status": "unknown", "basis": "pattern_numbers_not_explicit_on_both_sides"}
    if len(runtime_values) != 1 or len(offline_values) != 1:
        return {"status": "unknown", "basis": "pattern_numbers_change_within_collapsed_rgb_run"}
    return {
        "status": "exact" if runtime_values == offline_values else "different",
        "basis": "explicit_manifest_pattern_numbers",
    }


def _duration_summary(runtime_entries: list[VisualEntry], offline_entries: list[VisualEntry]) -> dict[str, object]:
    runtime_duration = (
        sum(entry.duration_us for entry in runtime_entries if entry.duration_us is not None)
        if all(entry.duration_us is not None for entry in runtime_entries)
        else None
    )
    offline_duration = (
        sum(entry.duration_us for entry in offline_entries if entry.duration_us is not None)
        if all(entry.duration_us is not None for entry in offline_entries)
        else None
    )
    runtime_timestamp_span = (
        runtime_entries[-1].timestamp_us - runtime_entries[0].timestamp_us
        if all(entry.timestamp_us is not None for entry in runtime_entries)
        else None
    )
    logical_wait_total = (
        sum(entry.logical_wait for entry in offline_entries if entry.logical_wait is not None)
        if all(entry.logical_wait is not None for entry in offline_entries)
        else None
    )
    if runtime_duration is not None and offline_duration is not None:
        status = "exact" if runtime_duration == offline_duration else "different"
        basis = "explicit_duration_us_sum"
    else:
        status = "unknown"
        basis = "both_sequences_need_explicit_duration_us_for_exact_duration_comparison"
    return {
        "status": status,
        "basis": basis,
        "runtime_explicit_duration_us": runtime_duration,
        "offline_explicit_duration_us": offline_duration,
        "runtime_timestamp_span_us_not_full_duration": runtime_timestamp_span,
        "offline_logical_wait_total_not_seconds": logical_wait_total,
    }


def _contact_review_template(*manifests: dict[str, Any]) -> dict[str, object]:
    raw_items: object = None
    for manifest in manifests:
        if manifest.get("contact_review_items") is not None:
            raw_items = manifest["contact_review_items"]
            break
    if raw_items is None:
        items: list[dict[str, object]] = []
    else:
        if not isinstance(raw_items, list):
            raise AlignmentError("contact_review_items must be a list")
        items = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise AlignmentError(f"contact_review_items[{index}] must be an object")
            item_id = raw.get("id")
            if not isinstance(item_id, str) or not item_id or item_id in seen:
                raise AlignmentError("contact review item ids must be non-empty and unique")
            seen.add(item_id)
            expected: dict[str, str] = {}
            for field in ("hand", "tool", "target", "response"):
                value = raw.get(field)
                if not isinstance(value, str) or not value:
                    raise AlignmentError(f"contact review item {item_id!r} needs non-empty {field}")
                expected[field] = value
            items.append(
                {
                    "id": item_id,
                    "milestone": raw.get("milestone"),
                    "expected": expected,
                    "manual_conclusions": {
                        "hand": None,
                        "tool": None,
                        "target": None,
                        "response": None,
                        "overall": None,
                    },
                    "reviewer": None,
                    "notes": None,
                }
            )
    return {
        "status": "awaiting_human_review" if items else "awaiting_checklist",
        "review_at_native_size_required": True,
        "diagnostic_magnification_admissible_as_pass_evidence": False,
        "allowed_manual_conclusions": list(MANUAL_CONCLUSIONS),
        "items": items,
        "automatic_semantic_conclusion": None,
    }


def _not_run_report(
    runtime_manifest_path: Path | None,
    offline_manifest_path: Path | None,
    reasons: list[str],
    source_statuses: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "report_kind": "pm2_activity_runtime_offline_alignment",
        "tool_version": TOOL_VERSION,
        "status": "not_run",
        "reasons": reasons,
        "source_statuses": source_statuses or {},
        "inputs": {
            "runtime_manifest": None if runtime_manifest_path is None else str(runtime_manifest_path.resolve()),
            "offline_manifest": None if offline_manifest_path is None else str(offline_manifest_path.resolve()),
        },
        "alignment": {
            "first_visible_change": "unknown",
            "milestones": "unknown",
            "frame_tick_intervals": [],
        },
        "comparison_summary": {
            "pattern": {"status": "unknown"},
            "geometry": {"status": "unknown"},
            "occlusion": {"status": "unknown"},
            "phase": {"status": "unknown"},
            "duration": {"status": "unknown"},
        },
        "native_size_contact_review": {
            "status": "not_run",
            "review_at_native_size_required": True,
            "diagnostic_magnification_admissible_as_pass_evidence": False,
            "allowed_manual_conclusions": list(MANUAL_CONCLUSIONS),
            "items": [],
            "automatic_semantic_conclusion": None,
        },
        "classification_slots": list(CLASSIFICATION_SLOTS),
        "transform_policy": {
            "scaling": "forbidden",
            "interpolation": "forbidden",
            "fuzzy_threshold": "forbidden",
            "comparison_size": "native",
        },
    }


def _provenance_summary(
    runtime_manifest: dict[str, Any],
    offline_manifest: dict[str, Any],
) -> dict[str, object]:
    fields = {
        "r0_fixed_archive_sha256": runtime_manifest.get("archive_sha256"),
        "r1_capture_receipt_sha256": runtime_manifest.get("capture_receipt_sha256"),
        "r1_replay_sha256": runtime_manifest.get("replay_sha256"),
        "r2_timeline_sha256": offline_manifest.get("timeline_sha256"),
        "r3_pt1_report_sha256": offline_manifest.get("pt1_report_sha256"),
        "r4_binding_manifest_sha256": offline_manifest.get("binding_manifest_sha256"),
        "r4_composition_manifest_sha256": offline_manifest.get("composition_manifest_sha256"),
    }
    invalid = sorted(key for key, value in fields.items() if value is not None and not _is_sha256(value))
    if invalid:
        raise AlignmentError(f"provenance fields must be SHA-256 hex strings: {invalid}")
    missing = sorted(key for key, value in fields.items() if value is None)
    return {**fields, "complete": not missing, "missing_fields": missing}


def build_alignment_report(
    runtime_manifest_path: str | Path | None,
    offline_manifest_path: str | Path | None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    max_pixels_per_image: int = DEFAULT_MAX_PIXELS_PER_IMAGE,
) -> dict[str, object]:
    """Align exact visible runs and return a safe, image-free JSON structure."""

    runtime_path = None if runtime_manifest_path is None else Path(runtime_manifest_path)
    offline_path = None if offline_manifest_path is None else Path(offline_manifest_path)
    missing_reasons: list[str] = []
    if runtime_path is None:
        missing_reasons.append("runtime_manifest_not_provided")
    elif not runtime_path.is_file():
        missing_reasons.append("runtime_manifest_missing")
    if offline_path is None:
        missing_reasons.append("offline_manifest_not_provided")
    elif not offline_path.is_file():
        missing_reasons.append("offline_manifest_missing")
    if missing_reasons:
        return _not_run_report(runtime_path, offline_path, missing_reasons)

    runtime_header = _load_json(runtime_path)
    offline_header = _load_json(offline_path)
    source_statuses = {
        "runtime": runtime_header.get("status"),
        "offline": offline_header.get("status"),
    }
    not_ready = [
        side
        for side, status in source_statuses.items()
        if status not in READY_STATUSES
    ]
    if not_ready:
        return _not_run_report(
            runtime_path,
            offline_path,
            [f"{side}_manifest_status_not_ready" for side in not_ready],
            source_statuses,
        )

    runtime_manifest, runtime_entries = _load_sequence(
        runtime_path,
        expected_kind=RUNTIME_MANIFEST_KIND,
        list_field="frames",
        identifier_field="frame",
        max_entries=max_entries,
        max_dimension=max_dimension,
        max_pixels=max_pixels_per_image,
        require_timestamps=True,
    )
    offline_manifest, offline_entries = _load_sequence(
        offline_path,
        expected_kind=OFFLINE_MANIFEST_KIND,
        list_field="ticks",
        identifier_field="tick",
        max_entries=max_entries,
        max_dimension=max_dimension,
        max_pixels=max_pixels_per_image,
        require_timestamps=False,
    )
    scene_id = runtime_manifest.get("scene_id")
    if not isinstance(scene_id, str) or not scene_id:
        raise AlignmentError("runtime manifest scene_id must be a non-empty string")
    if scene_id != offline_manifest.get("scene_id"):
        raise AlignmentError("runtime and offline manifests must name the same scene_id")

    provenance = _provenance_summary(runtime_manifest, offline_manifest)

    mapping, mapping_report, alignment_state = _build_run_mapping(runtime_entries, offline_entries)
    runtime_runs: list[VisualRun] = alignment_state["runtime_runs"]
    offline_runs: list[VisualRun] = alignment_state["offline_runs"]
    comparisons: list[dict[str, object]] = []
    exact_count = 0
    size_mismatch_count = 0
    changed_pixel_total = 0
    for runtime_run_index, offline_run_index in sorted(mapping.items()):
        runtime_run = runtime_runs[runtime_run_index]
        offline_run = offline_runs[offline_run_index]
        runtime_representative = runtime_entries[runtime_run.start_position]
        offline_representative = offline_entries[offline_run.start_position]
        comparison = _compare_native_rgb(runtime_representative, offline_representative)
        if comparison["comparison_status"] == "exact":
            exact_count += 1
        elif comparison["comparison_status"] == "size_mismatch":
            size_mismatch_count += 1
        if comparison["pixel_difference_count"] is not None:
            changed_pixel_total += int(comparison["pixel_difference_count"])
        runtime_slice = runtime_entries[runtime_run.start_position : runtime_run.end_position + 1]
        offline_slice = offline_entries[offline_run.start_position : offline_run.end_position + 1]
        comparisons.append(
            {
                "runtime": _range_payload(runtime_entries, runtime_run, "frame"),
                "offline": _range_payload(offline_entries, offline_run, "tick"),
                "alignment_basis": next(
                    item["basis"] for item in mapping_report if item["runtime_run"] == runtime_run_index
                ),
                "exact_comparison": comparison,
                "pattern_number_comparison": _explicit_pattern_status(runtime_slice, offline_slice),
                "classification": _classification_slots(comparison),
            }
        )

    anchors = alignment_state["anchors"]
    full_run_coverage = (
        len(mapping) == len(runtime_runs) == len(offline_runs)
        and len(set(mapping.values())) == len(offline_runs)
        and not anchors["segment_issues"]
        and anchors["monotonic"]
    )
    all_exact = full_run_coverage and exact_count == len(comparisons) and bool(comparisons)
    dimensions_all_match = not size_mismatch_count
    duration = _duration_summary(runtime_entries, offline_entries)
    report_status = "machine_exact" if all_exact else "completed_with_differences_or_unknowns"
    return {
        "schema_version": 1,
        "report_kind": "pm2_activity_runtime_offline_alignment",
        "tool_version": TOOL_VERSION,
        "status": report_status,
        "scene_id": scene_id,
        "inputs": {
            "runtime_manifest": _manifest_identity(runtime_path),
            "offline_manifest": _manifest_identity(offline_path),
            "runtime_source_status": runtime_manifest.get("status"),
            "offline_source_status": offline_manifest.get("status"),
            "offline_coordinate_contract": offline_manifest.get("coordinate_contract"),
            "offline_timeline_sha256": offline_manifest.get("timeline_sha256"),
            "offline_binding_manifest_sha256": offline_manifest.get("binding_manifest_sha256"),
            "stage_provenance": provenance,
        },
        "alignment": {
            **anchors,
            "run_mapping": mapping_report,
            "frame_tick_intervals": comparisons,
            "full_visible_run_coverage": full_run_coverage,
        },
        "exact_totals": {
            "aligned_interval_count": len(comparisons),
            "exact_rgb_interval_count": exact_count,
            "size_mismatch_interval_count": size_mismatch_count,
            "pixel_difference_count_across_representative_runs": changed_pixel_total,
        },
        "comparison_summary": {
            "pattern": {
                "status": "exact" if all_exact else "unknown",
                "basis": "exact_native_rgb" if all_exact else "RGB differences do not identify pattern cause",
            },
            "geometry": {
                "status": "exact" if all_exact else "unknown",
                "basis": (
                    "exact_native_rgb"
                    if all_exact
                    else "dimension mismatch is recorded mechanically but may be format/video mode, not scene geometry"
                    if not dimensions_all_match
                    else "pixel differences do not isolate geometry"
                ),
            },
            "occlusion": {
                "status": "exact" if all_exact else "unknown",
                "basis": "exact_native_rgb" if all_exact else "requires source trace or native-size human review",
            },
            "phase": {
                "status": "aligned" if full_run_coverage else "unknown",
                "basis": "first visible change, explicit milestones, and equal visible-run counts",
            },
            "duration": duration,
        },
        "native_size_contact_review": _contact_review_template(runtime_manifest, offline_manifest),
        "fixed_build_s2_calibration": {
            "status": (
                "pending_stage_provenance"
                if not provenance["complete"]
                else "pending_native_size_human_contact_review"
            ),
            "automatic_s2_promotion": False,
            "machine_rgb_alignment_exact": all_exact,
        },
        "classification_slots": list(CLASSIFICATION_SLOTS),
        "transform_policy": {
            "scaling": "forbidden",
            "interpolation": "forbidden",
            "fuzzy_threshold": "forbidden",
            "comparison_size": "native",
        },
        "safety": {
            "contains_image_bytes": False,
            "writes_difference_images": False,
            "automatic_semantic_judgment": False,
        },
    }


def _write_new_json(path: Path, serialized: str) -> None:
    if not path.parent.is_dir():
        raise AlignmentError(f"output parent does not exist: {path.parent}")
    if os.path.lexists(path):
        raise AlignmentError(f"output already exists: {path}")
    encoded = serialized.encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_name, path)
    except FileExistsError as exc:
        raise AlignmentError(f"output already exists: {path}") from exc
    except OSError as exc:
        raise AlignmentError(f"cannot publish output JSON: {path}") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exactly align PM2 runtime RGB frames with offline RGB ticks."
    )
    parser.add_argument("--runtime-manifest")
    parser.add_argument("--offline-manifest")
    parser.add_argument("--output", default="-", help="Safe JSON output path, or '-' for stdout")
    args = parser.parse_args(argv)
    try:
        report = build_alignment_report(args.runtime_manifest, args.offline_manifest)
        serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output == "-":
            print(serialized, end="")
        else:
            _write_new_json(Path(args.output), serialized)
    except AlignmentError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0 if report["status"] != "not_run" else 3


if __name__ == "__main__":
    raise SystemExit(main())
