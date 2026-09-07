#!/usr/bin/env python3
"""Classify capture-only frames between stable PM2 activity ticks.

The tool tests a deliberately narrow hypothesis: a frame seen between two
stable activity ticks may be the visible state of the top-to-bottom, planar
``MOVEVR`` copy used by ``WWANIME(5)`` rather than a separately authored
exposure.  It binds the lossless full-frame PNG sequence, both FFmpeg
framehash ledgers, the stable-tick manifests, the R4 aggregate, the fixed
palette, and the relevant fixed-source files.

No source pixels, palette entries, reconstructed image, or difference image
is serialized.  The optional output is a metadata-only JSON report.  An exact
fit here does not promote the capture to S2 and grants no production use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image


TOOL_VERSION = "0.1.0"
REPORT_SCHEMA_VERSION = 1
REPORT_KIND = "pm2_activity_transition_copy_analysis"
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
GIT_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
FRAMEHASH_RE = re.compile(
    r"0,\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*([0-9a-fA-F]{64})$"
)
FIXED_SOURCE_COMMIT = "ec7bdef58357185fe5344973c156b857a5de2c1f"
FIXED_SOURCE_REGISTRY: dict[str, dict[str, str]] = {
    "job_script": {
        "repo_path": "PM2/KOSOTEXT/JOB003.TXT",
        "sha256": "75682752be653048a693ee261c6a07897b7586a623e21217b23bf1f0b5d6bb34",
    },
    "widanime": {
        "repo_path": "PM2/KOSO4/WIDANIME.ASM",
        "sha256": "0b71d5133b7c3cc14cd32f2a7d2286d81f7dada06403b34efe794f7123e845bb",
    },
    "pictuer": {
        "repo_path": "PM2/KOSO4/PICTUER.ASM",
        "sha256": "814c9f474ab6b924fd344bf222d4ca854f145904f63954e816a34e8661c3573a",
    },
    "vrmove": {
        "repo_path": "PM2/KOSO2/VRMOVE.ASM",
        "sha256": "fbf4d3cec845c7316aecb4f8a89d443da47b919959978c2409cd77ce86acecfd",
    },
}
FIXED_JOB003_PALETTE_SHA256 = (
    "aed4a56e1b5cdc2d97bcbde68f8ddab6da05b8b119b2281d6004016cbcfe3c71"
)


class TransitionCopyError(ValueError):
    """Raised when evidence is malformed, unbound, or internally inconsistent."""


@dataclass(frozen=True)
class Raster:
    width: int
    height: int
    indices: tuple[int, ...]
    raw_rgb_sha256: str


@dataclass(frozen=True)
class PlanarFit:
    seam_row: int
    completed_plane_count: int
    boundary_x: int

    @property
    def left_mask(self) -> int:
        return (1 << (self.completed_plane_count + 1)) - 1

    @property
    def right_mask(self) -> int:
        return (1 << self.completed_plane_count) - 1


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    )


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise TransitionCopyError(f"{field} must be a SHA-256 hex string")
    return value.lower()


def _require_int(value: object, field: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise TransitionCopyError(f"{field} must be an integer >= {minimum}")
    return value


def _load_bound_json(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    expected = _require_sha256(expected_sha256, f"{label}_sha256")
    try:
        raw = path.resolve(strict=True).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransitionCopyError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise TransitionCopyError(f"{label} must be a JSON object")
    actual = sha256_bytes(raw)
    if actual != expected:
        raise TransitionCopyError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return value


def _bind_file(path: Path, expected_sha256: str, label: str) -> tuple[Path, str]:
    expected = _require_sha256(expected_sha256, f"{label}_sha256")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TransitionCopyError(f"cannot resolve {label}: {path}") from exc
    if not resolved.is_file():
        raise TransitionCopyError(f"{label} must be a file")
    actual = sha256_path(resolved)
    if actual != expected:
        raise TransitionCopyError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return resolved, actual


def _git_directory(repository_root: Path) -> Path:
    marker = repository_root / ".git"
    if marker.is_dir():
        return marker.resolve(strict=True)
    if marker.is_file():
        try:
            text = marker.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise TransitionCopyError("cannot read fixed source .git indirection") from exc
        prefix = "gitdir: "
        if not text.startswith(prefix):
            raise TransitionCopyError("fixed source .git indirection is malformed")
        target = Path(text[len(prefix) :])
        if not target.is_absolute():
            target = repository_root / target
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            raise TransitionCopyError("fixed source git directory is unavailable") from exc
        if not resolved.is_dir():
            raise TransitionCopyError("fixed source git directory is not a directory")
        return resolved
    raise TransitionCopyError("fixed source repository has no .git identity")


def _read_git_head(repository_root: Path) -> str:
    git_directory = _git_directory(repository_root)
    try:
        head = (git_directory / "HEAD").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise TransitionCopyError("cannot read fixed source git HEAD") from exc
    if GIT_COMMIT_RE.fullmatch(head):
        return head.lower()
    prefix = "ref: "
    if not head.startswith(prefix):
        raise TransitionCopyError("fixed source git HEAD is malformed")
    reference = head[len(prefix) :]
    reference_path = Path(reference)
    if reference_path.is_absolute() or ".." in reference_path.parts:
        raise TransitionCopyError("fixed source git HEAD reference is unsafe")
    loose = git_directory / reference_path
    if loose.is_file():
        try:
            commit = loose.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise TransitionCopyError("cannot read fixed source git HEAD reference") from exc
        if GIT_COMMIT_RE.fullmatch(commit):
            return commit.lower()
        raise TransitionCopyError("fixed source loose git reference is malformed")
    packed = git_directory / "packed-refs"
    try:
        packed_lines = packed.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TransitionCopyError("fixed source git HEAD reference is unresolved") from exc
    suffix = f" {reference}"
    matches = [line.split(" ", 1)[0] for line in packed_lines if line.endswith(suffix)]
    if len(matches) != 1 or GIT_COMMIT_RE.fullmatch(matches[0]) is None:
        raise TransitionCopyError("fixed source packed git reference is unresolved")
    return matches[0].lower()


def _fixed_source_root(path: Path, repo_path: str) -> Path:
    root = path
    for _part in Path(repo_path).parts:
        root = root.parent
    return root


def _verify_fixed_source_identity(
    paths: Mapping[str, Path], caller_hashes: Mapping[str, str]
) -> tuple[dict[str, Path], dict[str, str], dict[str, Any]]:
    if set(paths) != set(FIXED_SOURCE_REGISTRY) or set(caller_hashes) != set(
        FIXED_SOURCE_REGISTRY
    ):
        raise TransitionCopyError("fixed source input labels do not match the registry")
    resolved: dict[str, Path] = {}
    actual_hashes: dict[str, str] = {}
    repository_root: Path | None = None
    for label, registration in FIXED_SOURCE_REGISTRY.items():
        source, actual = _bind_file(paths[label], caller_hashes[label], label)
        if actual != registration["sha256"]:
            raise TransitionCopyError(
                f"{label} does not match the pre-registered fixed-source SHA-256"
            )
        candidate_root = _fixed_source_root(source, registration["repo_path"])
        if repository_root is None:
            repository_root = candidate_root
        elif candidate_root != repository_root:
            raise TransitionCopyError("fixed source files do not share one repository root")
        expected_path = (candidate_root / Path(registration["repo_path"])).resolve(
            strict=True
        )
        if source != expected_path:
            raise TransitionCopyError(
                f"{label} does not match its pre-registered repository path"
            )
        resolved[label] = source
        actual_hashes[label] = actual
    assert repository_root is not None
    repository_root = repository_root.resolve(strict=True)
    actual_commit = _read_git_head(repository_root)
    if actual_commit != FIXED_SOURCE_COMMIT:
        raise TransitionCopyError(
            "fixed source repository HEAD does not match the pre-registered commit"
        )
    return (
        resolved,
        actual_hashes,
        {
            "registry_kind": "pm2_job003_transition_source_registry",
            "registry_version": 1,
            "repository_root": str(repository_root),
            "commit": actual_commit,
            "commit_validation": "git_head_exact",
            "files": {
                label: {
                    "repo_path": registration["repo_path"],
                    "sha256": registration["sha256"],
                    "validation": "path_and_sha256_exact",
                }
                for label, registration in FIXED_SOURCE_REGISTRY.items()
            },
        },
    )


def _parse_crop(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, Mapping) or set(value) != {"x", "y", "width", "height"}:
        raise TransitionCopyError("crop must contain exactly x, y, width, and height")
    x = _require_int(value["x"], "crop.x")
    y = _require_int(value["y"], "crop.y")
    width = _require_int(value["width"], "crop.width", 1)
    height = _require_int(value["height"], "crop.height", 1)
    if width % 8:
        raise TransitionCopyError("crop.width must be divisible by the planar byte width 8")
    return x, y, width, height


def _load_palette(path: Path, expected_sha256: str) -> tuple[dict[tuple[int, int, int], int], str]:
    value = _load_bound_json(path, expected_sha256, "palette")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) != 16:
        raise TransitionCopyError("palette.entries must contain exactly 16 entries")
    lookup: dict[tuple[int, int, int], int] = {}
    for index, entry in enumerate(entries):
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or any(
                not isinstance(channel, int)
                or isinstance(channel, bool)
                or not 0 <= channel <= 255
                for channel in entry
            )
        ):
            raise TransitionCopyError(f"palette.entries[{index}] is not an RGB triplet")
        color = tuple(entry)
        if color in lookup:
            raise TransitionCopyError("palette entries must be unique")
        lookup[color] = index
    return lookup, sha256_path(path)


def _parse_framehash(path: Path, expected_sha256: str, label: str) -> tuple[dict[int, str], str]:
    resolved, actual = _bind_file(path, expected_sha256, label)
    frames: dict[int, str] = {}
    try:
        lines = resolved.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise TransitionCopyError(f"cannot parse {label}") from exc
    for line in lines:
        match = FRAMEHASH_RE.fullmatch(line)
        if match is None:
            continue
        dts, pts, duration, size, digest = match.groups()
        if dts != pts or int(duration) <= 0 or int(size) <= 0:
            raise TransitionCopyError(f"{label} contains an unsupported frame row")
        frame = int(pts)
        if frame in frames:
            raise TransitionCopyError(f"{label} repeats frame {frame}")
        frames[frame] = digest.lower()
    if not frames or sorted(frames) != list(range(len(frames))):
        raise TransitionCopyError(f"{label} must contain a contiguous zero-based sequence")
    return frames, actual


def _image_raw_rgb(path: Path, crop: tuple[int, int, int, int] | None = None) -> tuple[int, int, bytes, str]:
    try:
        with Image.open(path) as image:
            image.load()
            rgb = image.convert("RGB")
            if crop is not None:
                x, y, width, height = crop
                if x + width > rgb.width or y + height > rgb.height:
                    raise TransitionCopyError(f"crop exceeds image bounds: {path}")
                rgb = rgb.crop((x, y, x + width, y + height))
            raw = rgb.tobytes()
            return rgb.width, rgb.height, raw, sha256_bytes(raw)
    except (OSError, ValueError) as exc:
        raise TransitionCopyError(f"cannot read RGB image: {path}") from exc


def _indices_from_rgb(
    raw: bytes,
    width: int,
    height: int,
    palette_lookup: Mapping[tuple[int, int, int], int],
) -> tuple[int, ...]:
    if len(raw) != width * height * 3:
        raise TransitionCopyError("RGB byte length does not match raster dimensions")
    result: list[int] = []
    for offset in range(0, len(raw), 3):
        color = (raw[offset], raw[offset + 1], raw[offset + 2])
        try:
            result.append(palette_lookup[color])
        except KeyError as exc:
            raise TransitionCopyError("capture crop contains a color outside the bound palette") from exc
    return tuple(result)


def _load_capture_raster(
    path: Path,
    crop: tuple[int, int, int, int],
    palette_lookup: Mapping[tuple[int, int, int], int],
) -> tuple[Raster, str]:
    _, _, full_raw, full_raw_sha = _image_raw_rgb(path)
    width, height, crop_raw, crop_raw_sha = _image_raw_rgb(path, crop)
    return (
        Raster(
            width=width,
            height=height,
            indices=_indices_from_rgb(crop_raw, width, height, palette_lookup),
            raw_rgb_sha256=crop_raw_sha,
        ),
        full_raw_sha,
    )


def _merge_index(previous: int, following: int, mask: int) -> int:
    return (previous & (~mask & 0xF)) | (following & mask)


def find_planar_copy_fits(
    previous: Sequence[int],
    transition: Sequence[int],
    following: Sequence[int],
    width: int,
    height: int,
) -> tuple[PlanarFit, ...]:
    """Return exact top-down row/plane/byte-copy states matching a transition."""

    expected = width * height
    if width <= 0 or height <= 0 or width % 8:
        raise TransitionCopyError("planar fit dimensions must be positive and byte aligned")
    if any(len(value) != expected for value in (previous, transition, following)):
        raise TransitionCopyError("planar fit rasters have inconsistent sizes")

    row_is_following: list[bool] = []
    row_is_previous: list[bool] = []
    for row in range(height):
        start = row * width
        end = start + width
        row_is_following.append(transition[start:end] == following[start:end])
        row_is_previous.append(transition[start:end] == previous[start:end])

    prefix_following = [True] * (height + 1)
    suffix_previous = [True] * (height + 1)
    for row in range(height):
        prefix_following[row + 1] = prefix_following[row] and row_is_following[row]
    for row in range(height - 1, -1, -1):
        suffix_previous[row] = suffix_previous[row + 1] and row_is_previous[row]

    fits: list[PlanarFit] = []
    for seam in range(height):
        if not prefix_following[seam] or not suffix_previous[seam + 1]:
            continue
        base = seam * width
        for completed_planes in range(4):
            right_mask = (1 << completed_planes) - 1
            left_mask = (1 << (completed_planes + 1)) - 1
            for boundary_x in range(0, width + 1, 8):
                exact = True
                for x in range(width):
                    mask = left_mask if x < boundary_x else right_mask
                    if transition[base + x] != _merge_index(
                        previous[base + x], following[base + x], mask
                    ):
                        exact = False
                        break
                if exact:
                    fits.append(PlanarFit(seam, completed_planes, boundary_x))
    return tuple(fits)


def _bbox(points: Iterable[tuple[int, int]]) -> dict[str, int] | None:
    values = list(points)
    if not values:
        return None
    xs = [point[0] for point in values]
    ys = [point[1] for point in values]
    return {
        "x": min(xs),
        "y": min(ys),
        "width": max(xs) - min(xs) + 1,
        "height": max(ys) - min(ys) + 1,
    }


def classify_transition(
    previous: Raster, transition: Raster, following: Raster
) -> dict[str, Any]:
    if (previous.width, previous.height) != (transition.width, transition.height) or (
        previous.width,
        previous.height,
    ) != (following.width, following.height):
        raise TransitionCopyError("transition rasters must have identical dimensions")

    width = previous.width
    changed = 0
    unchanged = 0
    unchanged_mismatch = 0
    previous_only = 0
    following_only = 0
    hybrid = 0
    hybrid_masks: Counter[int] = Counter()
    following_points: list[tuple[int, int]] = []
    previous_points: list[tuple[int, int]] = []
    hybrid_points: list[tuple[int, int]] = []

    for position, (old, middle, new) in enumerate(
        zip(previous.indices, transition.indices, following.indices)
    ):
        x = position % width
        y = position // width
        if old == new:
            unchanged += 1
            if middle != old:
                unchanged_mismatch += 1
            continue
        changed += 1
        if middle == old:
            previous_only += 1
            previous_points.append((x, y))
        elif middle == new:
            following_only += 1
            following_points.append((x, y))
        else:
            hybrid += 1
            hybrid_points.append((x, y))
            compatible_masks = [
                mask
                for mask in (0x1, 0x3, 0x7)
                if _merge_index(old, new, mask) == middle
            ]
            if not compatible_masks:
                hybrid_masks[-1] += 1
            else:
                hybrid_masks[compatible_masks[0]] += 1

    fits = find_planar_copy_fits(
        previous.indices,
        transition.indices,
        following.indices,
        previous.width,
        previous.height,
    )
    serialized_fits = [
        [fit.seam_row, fit.completed_plane_count, fit.boundary_x] for fit in fits
    ]
    seam_rows = sorted({fit.seam_row for fit in fits})
    planes = sorted({fit.completed_plane_count for fit in fits})
    boundaries = sorted({fit.boundary_x for fit in fits})

    return {
        "pixel_counts": {
            "total": width * previous.height,
            "unchanged_between_stable_ticks": unchanged,
            "changed_between_stable_ticks": changed,
            "unchanged_but_transition_mismatch": unchanged_mismatch,
            "previous_stable_value": previous_only,
            "following_stable_value": following_only,
            "neither_stable_value": hybrid,
        },
        "changed_value_bounding_boxes": {
            "previous_stable_value": _bbox(previous_points),
            "following_stable_value": _bbox(following_points),
            "neither_stable_value": _bbox(hybrid_points),
        },
        "raw_previous_or_following_only": hybrid == 0 and unchanged_mismatch == 0,
        "hybrid_palette_indices": {
            "count": hybrid,
            "all_match_cumulative_low_plane_masks": hybrid_masks.get(-1, 0) == 0,
            "compatible_mask_histogram": {
                "0x1": hybrid_masks.get(0x1, 0),
                "0x3": hybrid_masks.get(0x3, 0),
                "0x7": hybrid_masks.get(0x7, 0),
                "incompatible": hybrid_masks.get(-1, 0),
            },
        },
        "top_down_planar_copy_fit": {
            "exact": bool(fits),
            "candidate_count": len(fits),
            "candidate_set_sha256": canonical_sha256(serialized_fits),
            "seam_rows": seam_rows,
            "completed_plane_counts": planes,
            "boundary_x_candidates": {
                "values": boundaries,
                "unit": "crop_pixels",
                "byte_alignment": 8,
            },
            "candidate_ambiguity_reason": (
                None
                if len(fits) == 1
                else "unchanged or equal-bit spans cannot identify the exact copy instruction boundary"
            ),
        },
    }


def _source_lines(path: Path, required_tokens: Sequence[bytes], label: str) -> list[int]:
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise TransitionCopyError(f"cannot read source file {label}") from exc
    found: list[int] = []
    start = 0
    for token in required_tokens:
        match = next((index for index in range(start, len(lines)) if token in lines[index]), None)
        if match is None:
            raise TransitionCopyError(f"{label} is missing required source token {token!r}")
        found.append(match + 1)
        start = match + 1
    return found


def _source_evidence(
    job_script: Path,
    widanime: Path,
    pictuer: Path,
    vrmove: Path,
    hashes: Mapping[str, str],
) -> list[dict[str, Any]]:
    job_lines = _source_lines(
        job_script,
        [
            b"*ANMT001",
            b"WWANIME(4,0)",
            b"TIMER1(TIMEWAIT1)",
            b"APUT(",
            b"WAIT1",
            b"WWANIME(5,0)",
            b"LOOP TIMELOP",
        ],
        "JOB003",
    )
    wid_virtual_lines = _source_lines(
        widanime,
        [
            b"ANIME_PTN_PUT\tPROC",
            b"ASSIGN_SSGR_VIRTUAL",
            b"PT_MASK_PAT_PUT",
        ],
        "WIDANIME virtual pattern path",
    )
    wid_copy_lines = _source_lines(
        widanime,
        [
            b"V2TOCRTCOPY\tPROC",
            b"ASSIGN_SSSOU_VIRTUAL",
            b"ASSIGN_SSDES_VRAM",
            b"MOV\tVX1",
            b"MOVX\tVX2,MOVSIZX",
            b"CALL_w\tMOVEVR",
        ],
        "WIDANIME display copy path",
    )
    pictuer_lines = _source_lines(
        pictuer,
        [
            b"FUNC_w\tPT_PATTERN_PUT",
            b"CALL\tPATTAN_PTX",
            b"FUNC_w\tPT_MASK_PAT_PUT",
            b"CALL\tMASK_PTX",
        ],
        "PICTUER pattern wrappers",
    )
    move_lines = _source_lines(
        vrmove,
        [
            b"MOVEVR\tPROC",
            b"MOV\tBP,VY2",
            b"MOVEVR1:",
            b"PUSH\tSSSOU3",
            b"PUSH\tSSSOU2",
            b"PUSH\tSSSOU1",
            b"PUSH\tSSSOU0",
            b"REP\tMOVSB",
            b"REP\tMOVSB",
            b"REP\tMOVSB",
            b"REP\tMOVSB",
            b"DEC\tBP",
            b"JNZ\tMOVEVR1",
        ],
        "VRMOVE row and plane loop",
    )
    return [
        {
            "evidence_level": "source_proven",
            "claim": "JOB003 composes background and masked patterns before its sole per-tick WWANIME(5) display copy",
            "source": {"path": str(job_script), "sha256": hashes["job_script"]},
            "ordered_token_lines": job_lines,
        },
        {
            "evidence_level": "source_proven",
            "claim": "WIDANIME renders patterns into virtual VRAM and copies the configured rectangle from virtual VRAM to display VRAM through MOVEVR",
            "source": {"path": str(widanime), "sha256": hashes["widanime"]},
            "virtual_pattern_token_lines": wid_virtual_lines,
            "display_copy_token_lines": wid_copy_lines,
        },
        {
            "evidence_level": "source_proven",
            "claim": "PICTUER exposes the ordinary and masked pattern-put wrappers used by the WIDANIME virtual composition path",
            "source": {"path": str(pictuer), "sha256": hashes["pictuer"]},
            "ordered_token_lines": pictuer_lines,
        },
        {
            "evidence_level": "source_proven",
            "claim": "MOVEVR advances by rows and copies planes 0, 1, 2, and 3 in that order with byte-wide REP MOVSB runs",
            "source": {"path": str(vrmove), "sha256": hashes["vrmove"]},
            "ordered_token_lines": move_lines,
        },
    ]


def _frame_for(path: Path, zero_based_frame: int) -> Path:
    candidate = path / f"frame_{zero_based_frame + 1:06d}.png"
    try:
        resolved_root = path.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise TransitionCopyError(f"missing full capture PNG for frame {zero_based_frame}") from exc
    if not resolved.is_file():
        raise TransitionCopyError(f"capture frame is not a file: {resolved}")
    return resolved


def _run_for_frame(stage: Mapping[str, Any], frame: int) -> Mapping[str, Any]:
    runs = stage.get("visible_runs")
    if not isinstance(runs, list):
        raise TransitionCopyError("stage visible runs are missing")
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping)
        and isinstance(run.get("start_frame"), int)
        and isinstance(run.get("end_frame"), int)
        and run["start_frame"] <= frame <= run["end_frame"]
    ]
    if len(matches) != 1:
        raise TransitionCopyError(f"frame {frame} is not covered by exactly one visible run")
    return matches[0]


def _compact_int_values(values: Sequence[int]) -> dict[str, Any]:
    ordered = sorted(set(values))
    if not ordered:
        return {"count": 0, "values": []}
    if len(ordered) <= 12:
        return {"count": len(ordered), "values": ordered}
    steps = {right - left for left, right in zip(ordered, ordered[1:])}
    return {
        "count": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "uniform_step": next(iter(steps)) if len(steps) == 1 else None,
    }


def build_report(
    *,
    timing_path: Path,
    timing_sha256: str,
    stage_path: Path,
    stage_sha256: str,
    source_framehash_path: Path,
    source_framehash_sha256: str,
    stage_framehash_path: Path,
    stage_framehash_sha256: str,
    full_frames_dir: Path,
    runtime_manifest_path: Path,
    runtime_manifest_sha256: str,
    offline_manifest_path: Path,
    offline_manifest_sha256: str,
    r4_aggregate_path: Path,
    r4_aggregate_sha256: str,
    palette_path: Path,
    palette_sha256: str,
    job_script_path: Path,
    job_script_sha256: str,
    widanime_path: Path,
    widanime_sha256: str,
    pictuer_path: Path,
    pictuer_sha256: str,
    vrmove_path: Path,
    vrmove_sha256: str,
) -> dict[str, Any]:
    timing = _load_bound_json(timing_path, timing_sha256, "timing_report")
    stage = _load_bound_json(stage_path, stage_sha256, "stage_visible_runs")
    runtime = _load_bound_json(
        runtime_manifest_path, runtime_manifest_sha256, "runtime_manifest"
    )
    offline = _load_bound_json(
        offline_manifest_path, offline_manifest_sha256, "offline_manifest"
    )
    r4 = _load_bound_json(r4_aggregate_path, r4_aggregate_sha256, "r4_aggregate")
    source_framehash, source_framehash_actual = _parse_framehash(
        source_framehash_path, source_framehash_sha256, "source_framehash"
    )
    stage_framehash, stage_framehash_actual = _parse_framehash(
        stage_framehash_path, stage_framehash_sha256, "stage_framehash"
    )
    palette_lookup, palette_actual = _load_palette(palette_path, palette_sha256)
    offline_palette_identity = _require_sha256(
        offline.get("palette_file_sha256"), "offline_manifest.palette_file_sha256"
    )
    if palette_actual != offline_palette_identity:
        raise TransitionCopyError(
            "palette does not match the identity registered by the offline manifest"
        )
    if offline_palette_identity != FIXED_JOB003_PALETTE_SHA256:
        raise TransitionCopyError(
            "offline manifest palette identity does not match the fixed JOB003 calibration"
        )

    resolved_sources, source_hashes, fixed_source_identity = (
        _verify_fixed_source_identity(
            {
                "job_script": job_script_path,
                "widanime": widanime_path,
                "pictuer": pictuer_path,
                "vrmove": vrmove_path,
            },
            {
                "job_script": job_script_sha256,
                "widanime": widanime_sha256,
                "pictuer": pictuer_sha256,
                "vrmove": vrmove_sha256,
            },
        )
    )

    if timing.get("scene_id") != "JOB003" or stage.get("scene_id") != "JOB003":
        raise TransitionCopyError("this calibrated analysis only accepts JOB003")
    if runtime.get("scene_id") != "JOB003" or offline.get("scene_id") != "JOB003":
        raise TransitionCopyError("stable manifests must both describe JOB003")
    if r4.get("scene_id") != "JOB003":
        raise TransitionCopyError("R4 aggregate must describe JOB003")
    if timing.get("inputs", {}).get("stage_visible_runs", {}).get("sha256") != stage_sha256:
        raise TransitionCopyError("timing report does not bind the supplied stage ledger")
    if (
        timing.get("inputs", {}).get("runtime_selected_frame_manifest", {}).get("sha256")
        != runtime_manifest_sha256
    ):
        raise TransitionCopyError("timing report does not bind the supplied runtime manifest")
    if (
        timing.get("inputs", {}).get("persistent_r4_aggregate", {}).get("sha256")
        != r4_aggregate_sha256
    ):
        raise TransitionCopyError("timing report does not bind the supplied R4 aggregate")
    if offline.get("composition_manifest_sha256") != r4_aggregate_sha256:
        raise TransitionCopyError("offline RGB manifest does not bind the supplied R4 aggregate")
    if runtime.get("framehash_sha256") != source_framehash_sha256:
        raise TransitionCopyError("runtime manifest does not bind the supplied source framehash")

    crop = _parse_crop(stage.get("crop"))
    if runtime.get("crop") != stage.get("crop"):
        raise TransitionCopyError("runtime and stage crops disagree")
    _, _, width, height = crop
    if offline.get("display_raster_contract", {}).get("output_raster") != {
        "width": width,
        "height": height,
    }:
        raise TransitionCopyError("offline display raster does not match the capture crop")

    ticks = timing.get("mapping", {}).get("ticks")
    runtime_frames = runtime.get("frames")
    offline_ticks = offline.get("ticks")
    if not isinstance(ticks, list) or len(ticks) != 50:
        raise TransitionCopyError("timing report must contain the calibrated 50 ticks")
    if not isinstance(runtime_frames, list) or len(runtime_frames) != len(ticks):
        raise TransitionCopyError("runtime manifest tick count disagrees")
    if not isinstance(offline_ticks, list) or len(offline_ticks) != len(ticks):
        raise TransitionCopyError("offline manifest tick count disagrees")

    full_root = full_frames_dir.resolve(strict=True)
    runtime_root = runtime_manifest_path.resolve(strict=True).parent
    offline_root = offline_manifest_path.resolve(strict=True).parent
    if not full_root.is_dir():
        raise TransitionCopyError("full_frames_dir must be a directory")

    raster_cache: dict[int, Raster] = {}
    frame_binding_cache: dict[int, dict[str, str]] = {}

    def capture(frame: int) -> Raster:
        if frame in raster_cache:
            return raster_cache[frame]
        path = _frame_for(full_root, frame)
        raster, full_rgb_sha = _load_capture_raster(path, crop, palette_lookup)
        if source_framehash.get(frame) != full_rgb_sha:
            raise TransitionCopyError(f"full PNG raw RGB does not match source framehash at {frame}")
        if stage_framehash.get(frame) != raster.raw_rgb_sha256:
            raise TransitionCopyError(f"capture crop does not match stage framehash at {frame}")
        run = _run_for_frame(stage, frame)
        if run.get("rgb_sha256") != raster.raw_rgb_sha256:
            raise TransitionCopyError(f"visible-run hash disagrees at frame {frame}")
        raster_cache[frame] = raster
        frame_binding_cache[frame] = {
            "full_png_sha256": sha256_path(path),
            "full_raw_rgb_sha256": full_rgb_sha,
            "crop_raw_rgb_sha256": raster.raw_rgb_sha256,
        }
        return raster

    stable_frames: list[int] = []
    for index, tick in enumerate(ticks):
        stable_frame = _require_int(
            tick.get("runtime_stable_zero_based_frame"),
            f"ticks[{index}].runtime_stable_zero_based_frame",
        )
        stable_frames.append(stable_frame)
        stable = capture(stable_frame)

        runtime_entry = runtime_frames[index]
        offline_entry = offline_ticks[index]
        if not isinstance(runtime_entry, Mapping) or not isinstance(offline_entry, Mapping):
            raise TransitionCopyError("stable manifest entries must be objects")
        if runtime_entry.get("source_sequence_position") != stable_frame:
            raise TransitionCopyError(f"runtime stable frame mismatch for tick {index}")
        if runtime_entry.get("rgb_sha256") != stable.raw_rgb_sha256:
            raise TransitionCopyError(f"runtime crop hash mismatch for tick {index}")

        runtime_png = (runtime_root / str(runtime_entry.get("png_path"))).resolve(strict=True)
        offline_png = (offline_root / str(offline_entry.get("png_path"))).resolve(strict=True)
        runtime_png.relative_to(runtime_root)
        offline_png.relative_to(offline_root)
        rw, rh, runtime_raw, runtime_raw_sha = _image_raw_rgb(runtime_png)
        ow, oh, offline_raw, offline_raw_sha = _image_raw_rgb(offline_png)
        if (rw, rh) != (width, height) or (ow, oh) != (width, height):
            raise TransitionCopyError("stable crop PNG has an unexpected raster size")
        if runtime_raw_sha != stable.raw_rgb_sha256 or offline_raw_sha != stable.raw_rgb_sha256:
            raise TransitionCopyError(f"stable runtime/offline RGB mismatch for tick {index}")
        if runtime_raw != offline_raw:
            raise TransitionCopyError(f"stable runtime/offline bytes mismatch for tick {index}")

    gap_reports: list[dict[str, Any]] = []
    one_frame_reports: list[dict[str, Any]] = []
    for index in range(len(ticks) - 1):
        old_tick = ticks[index]
        new_tick = ticks[index + 1]
        transition_info = old_tick.get("transition_after")
        if not isinstance(transition_info, Mapping):
            raise TransitionCopyError(f"tick {index} has no transition_after object")
        frame_count = transition_info.get("frame_count")
        if frame_count not in (0, 1):
            raise TransitionCopyError(
                f"calibrated JOB003 gap {index}->{index + 1} is outside the 0-1 frame scope"
            )
        gap: dict[str, Any] = {
            "previous_global_tick": index,
            "following_global_tick": index + 1,
            "scope": transition_info.get("scope"),
            "captured_transition_frame_count": frame_count,
        }
        if frame_count == 0:
            gap["classification"] = "no_captured_transition_frame"
            gap_reports.append(gap)
            continue

        frame = _require_int(
            transition_info.get("start_zero_based_frame"),
            f"ticks[{index}].transition_after.start_zero_based_frame",
        )
        if transition_info.get("end_zero_based_frame_inclusive") != frame:
            raise TransitionCopyError("one-frame transition boundaries disagree")
        analysis = classify_transition(
            capture(stable_frames[index]),
            capture(frame),
            capture(stable_frames[index + 1]),
        )
        fits = analysis["top_down_planar_copy_fit"]
        fits["seam_rows"] = _compact_int_values(fits["seam_rows"])
        fits["completed_plane_counts"] = _compact_int_values(
            fits["completed_plane_counts"]
        )
        fits["boundary_x_candidates"]["values"] = _compact_int_values(
            fits["boundary_x_candidates"]["values"]
        )
        gap.update(
            {
                "classification": (
                    "machine_exact_in_progress_planar_copy"
                    if fits["exact"]
                    else "not_explained_by_planar_copy_model"
                ),
                "transition_zero_based_frame": frame,
                "transition_one_based_png_frame": frame + 1,
                "frame_bindings": frame_binding_cache[frame],
                "analysis": analysis,
            }
        )
        gap_reports.append(gap)
        one_frame_reports.append(gap)

    exact_count = sum(
        gap["analysis"]["top_down_planar_copy_fit"]["exact"]
        for gap in one_frame_reports
    )
    raw_ab_count = sum(
        gap["analysis"]["raw_previous_or_following_only"] for gap in one_frame_reports
    )
    hybrid_count = sum(
        gap["analysis"]["pixel_counts"]["neither_stable_value"]
        for gap in one_frame_reports
    )
    hybrid_mask_total: Counter[str] = Counter()
    for gap in one_frame_reports:
        hybrid_mask_total.update(
            gap["analysis"]["hybrid_palette_indices"]["compatible_mask_histogram"]
        )

    focus = next(
        (
            gap
            for gap in one_frame_reports
            if gap.get("transition_zero_based_frame") == 7173
        ),
        None,
    )
    if focus is None:
        raise TransitionCopyError("calibrated focus frame 7173 is absent")

    source_evidence = _source_evidence(
        resolved_sources["job_script"],
        resolved_sources["widanime"],
        resolved_sources["pictuer"],
        resolved_sources["vrmove"],
        source_hashes,
    )
    selected_frame_numbers = sorted(raster_cache)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "tool_version": TOOL_VERSION,
        "scene_id": "JOB003",
        "status": "machine_exact_copy_intermediates_with_explicit_limits",
        "inputs": {
            "timing_report": {"path": str(timing_path.resolve()), "sha256": timing_sha256},
            "stage_visible_runs": {"path": str(stage_path.resolve()), "sha256": stage_sha256},
            "source_framehash": {
                "path": str(source_framehash_path.resolve()),
                "sha256": source_framehash_actual,
                "frame_count": len(source_framehash),
            },
            "stage_framehash": {
                "path": str(stage_framehash_path.resolve()),
                "sha256": stage_framehash_actual,
                "frame_count": len(stage_framehash),
            },
            "runtime_manifest": {
                "path": str(runtime_manifest_path.resolve()),
                "sha256": runtime_manifest_sha256,
            },
            "offline_manifest": {
                "path": str(offline_manifest_path.resolve()),
                "sha256": offline_manifest_sha256,
            },
            "r4_aggregate": {
                "path": str(r4_aggregate_path.resolve()),
                "sha256": r4_aggregate_sha256,
            },
            "palette": {
                "path": str(palette_path.resolve()),
                "sha256": palette_actual,
                "offline_manifest_registered_sha256": offline_palette_identity,
                "fixed_job003_calibration_sha256": FIXED_JOB003_PALETTE_SHA256,
                "cross_binding_validation": "exact",
                "serialized_entries": False,
            },
            "fixed_source_identity": fixed_source_identity,
            "full_frames_directory": str(full_root),
            "selected_full_png_frame_numbers": _compact_int_values(
                [frame + 1 for frame in selected_frame_numbers]
            ),
            "selected_zero_based_capture_frames": _compact_int_values(
                selected_frame_numbers
            ),
        },
        "source_evidence": source_evidence,
        "machine_summary": {
            "stable_tick_count": len(ticks),
            "adjacent_stable_gap_count": len(ticks) - 1,
            "zero_frame_gap_count": sum(
                gap["captured_transition_frame_count"] == 0 for gap in gap_reports
            ),
            "one_frame_gap_count": len(one_frame_reports),
            "selected_capture_frame_count": len(selected_frame_numbers),
            "selected_full_png_and_framehash_bindings_exact": True,
            "stable_runtime_r4_derived_rgb_exact_count": len(ticks),
            "raw_previous_or_following_only_transition_count": raw_ab_count,
            "raw_ab_only_model_not_exact_transition_count": len(one_frame_reports)
            - raw_ab_count,
            "top_down_planar_copy_exact_transition_count": exact_count,
            "top_down_planar_copy_nonmatching_transition_count": len(one_frame_reports)
            - exact_count,
            "neither_stable_value_pixel_count": hybrid_count,
            "hybrid_cumulative_plane_mask_histogram": dict(hybrid_mask_total),
        },
        "focus_frame_7173": focus,
        "gaps": gap_reports,
        "graded_conclusions": [
            {
                "evidence_level": "source_proven",
                "claim": "JOB003 authors each tick in virtual VRAM and invokes one display copy only after composition; MOVEVR copies rows top-to-bottom and planes 0 through 3 within each row",
            },
            {
                "evidence_level": "machine_confirmed",
                "claim": f"{exact_count}/{len(one_frame_reports)} captured one-frame gaps exactly fit the source-derived top-down planar-copy state model",
            },
            {
                "evidence_level": "machine_confirmed",
                "claim": f"only {raw_ab_count}/{len(one_frame_reports)} transitions are literal previous/next RGB mosaics; the other {len(one_frame_reports) - raw_ab_count} contain {hybrid_count} seam pixels, all tested in the planar fit rather than discarded",
            },
            {
                "evidence_level": "inference",
                "claim": "the 28 one-frame states are transfer/capture-visible intermediates, not separately authored exposures; stable ticks remain the authored exposure units",
                "basis": "the source has no display-copy call between per-tick composition operations and every observed intermediate has an exact source-derived copy-state fit",
            },
            {
                "evidence_level": "unknown",
                "claim": "the exact CPU-cycle and display-scan timing that selected each compatible byte boundary is not recovered, and equal-color/equal-bit spans leave many copy instruction positions observationally equivalent",
            },
        ],
        "evidence_limits": {
            "formal_s2_status": "not_promoted",
            "automatic_s2_promotion": False,
            "authorization_effect": "none",
            "actual_rng_recovered": False,
            "replay_bound": False,
            "second_identical_replay_available": False,
            "return_boundary_bound": False,
            "semantic_action_classification_from_transition_frames": False,
            "job008_covered": False,
        },
        "safety": {
            "contains_image_bytes": False,
            "contains_pixel_arrays": False,
            "contains_palette_entries": False,
            "writes_images": False,
            "writes_difference_assets": False,
            "game_build_input": False,
        },
    }


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    if path.suffix.lower() != ".json":
        raise TransitionCopyError("output must use the .json suffix")
    if not path.parent.is_dir():
        raise TransitionCopyError("output parent directory does not exist")
    if os.path.lexists(path):
        raise TransitionCopyError("output already exists")
    payload = (
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("ascii")
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
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-volume hard link is atomic and cannot overwrite a destination
        # created after the lexists precheck.  The staging name is always
        # removed after publication or failure.
        os.link(temporary_name, path)
    except FileExistsError as exc:
        raise TransitionCopyError("output already exists") from exc
    except OSError as exc:
        raise TransitionCopyError("failed to atomically publish output JSON") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "timing",
        "stage",
        "source_framehash",
        "stage_framehash",
        "runtime_manifest",
        "offline_manifest",
        "r4_aggregate",
        "palette",
        "job_script",
        "widanime",
        "pictuer",
        "vrmove",
    ):
        parser.add_argument(f"--{name.replace('_', '-')}", type=Path, required=True)
        parser.add_argument(f"--{name.replace('_', '-')}-sha256", required=True)
    parser.add_argument("--full-frames-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            timing_path=args.timing,
            timing_sha256=args.timing_sha256,
            stage_path=args.stage,
            stage_sha256=args.stage_sha256,
            source_framehash_path=args.source_framehash,
            source_framehash_sha256=args.source_framehash_sha256,
            stage_framehash_path=args.stage_framehash,
            stage_framehash_sha256=args.stage_framehash_sha256,
            full_frames_dir=args.full_frames_dir,
            runtime_manifest_path=args.runtime_manifest,
            runtime_manifest_sha256=args.runtime_manifest_sha256,
            offline_manifest_path=args.offline_manifest,
            offline_manifest_sha256=args.offline_manifest_sha256,
            r4_aggregate_path=args.r4_aggregate,
            r4_aggregate_sha256=args.r4_aggregate_sha256,
            palette_path=args.palette,
            palette_sha256=args.palette_sha256,
            job_script_path=args.job_script,
            job_script_sha256=args.job_script_sha256,
            widanime_path=args.widanime,
            widanime_sha256=args.widanime_sha256,
            pictuer_path=args.pictuer,
            pictuer_sha256=args.pictuer_sha256,
            vrmove_path=args.vrmove,
            vrmove_sha256=args.vrmove_sha256,
        )
        if args.output:
            _write_json_atomic(args.output, report)
        else:
            print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    except (TransitionCopyError, OSError, ValueError) as exc:
        print(f"transition copy analysis failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
