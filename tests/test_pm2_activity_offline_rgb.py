from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from pm2_animation_lab.pm2_activity_alignment import build_alignment_report
from pm2_animation_lab.pm2_activity_offline_rgb import (
    NotReadyError,
    OfflineRgbError,
    QuarantineError,
    canonical_sha256,
    generate_offline_rgb_ticks,
    sha256_path,
)


HASH_TIMELINE = "1" * 64
HASH_R4_BINDING = "2" * 64
HASH_PT1_REPORT = "3" * 64
HASH_CAPTURE = "4" * 64


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def write_r4_manifest(root: Path) -> Path:
    r4 = root / "r4"
    r4.mkdir()
    frames = []
    frame_values = (
        bytes((0, 1, 2, 3, 4, 5, 6, 7)),
        bytes((7, 6, 5, 4, 3, 2, 1, 0)),
    )
    for tick, values in enumerate(frame_values):
        path = r4 / f"tick_{tick:04d}.idx"
        path.write_bytes(values)
        operations = [
            {
                "status": "skipped_negative_pattern",
                "pattern_number": -1,
            }
        ]
        if tick == 0:
            operations.insert(
                0,
                {
                    "status": "composited",
                    "pattern_number": 1,
                },
            )
        frames.append(
            {
                "tick": tick,
                "index_file": path.name,
                "index_frame_bytes": len(values),
                "index_frame_sha256": hashlib.sha256(values).hexdigest(),
                "operations": operations,
            }
        )
    manifest = {
        "schema_version": "pm2_activity_composition_manifest/v1",
        "status": "offline_candidate",
        "scene_id": "SYNTHETIC_JOB",
        "timeline_sha256": HASH_TIMELINE,
        "binding_manifest_sha256": HASH_R4_BINDING,
        "calibration_status": "source_candidate",
        "canvas": {
            "width": 4,
            "height": 2,
            "format": "index4_one_byte_per_pixel",
        },
        "coordinate_contract": {
            "timeline_x_unit_pixels": 8,
            "timeline_y_unit_pixels": 1,
            "pattern_author_offset_mode": "function7_coordinate_override",
            "wwanime_function": 7,
            "clip_policy": "canvas",
            "bit_order": "msb_left",
        },
        "transform_policy": {
            "interpolation": "none",
            "scaling": "none",
            "palette_conversion": "none",
            "color_remap": "none",
            "frame_insertion": "none",
        },
        "frames": frames,
        "authorization_effect": "none",
    }
    path = r4 / "manifest.json"
    write_json(path, manifest)
    return path


def root_relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def write_persistent_r4_manifest(root: Path) -> Path:
    receipts = root / "receipts"
    frames_root = root / "frames" / "persistent_fixture"
    receipts.mkdir()
    frames_root.mkdir(parents=True)

    scene = {"scene_id": "SYNTHETIC_JOB", "source_commit": "fixture", "source_ref": 0}
    coordinate = {
        "timeline_x_unit_pixels": 8,
        "timeline_y_unit_pixels": 1,
        "pattern_author_offset_mode": "function7_coordinate_override",
        "wwanime_function": 7,
        "clip_policy": "canvas",
        "bit_order": "msb_left",
    }
    transform = {
        "interpolation": "none",
        "scaling": "none",
        "palette_conversion": "none",
        "color_remap": "none",
        "frame_insertion": "none",
    }
    canvas = {
        "width": 4,
        "height": 2,
        "format": "index4_one_byte_per_pixel",
    }
    values_by_tick = (
        bytes((0, 1, 2, 3, 4, 5, 6, 7)),
        bytes((7, 6, 5, 4, 3, 2, 1, 0)),
        bytes((1, 2, 3, 4, 5, 6, 7, 8)),
        bytes((8, 7, 6, 5, 4, 3, 2, 1)),
    )

    base_binding = {
        "schema_version": "pm2_activity_compositor_bindings/v1",
        "scene_id": "SYNTHETIC_JOB",
        "authorization_effect": "none",
    }
    base_binding_path = receipts / "base_binding.json"
    write_json(base_binding_path, base_binding)

    calls = []
    aggregate_frames = []
    sequence_calls = []
    sequence_ticks = []
    spec_calls = []
    for call_index in range(2):
        start = call_index * 2
        end = start + 2
        call_dir = frames_root / f"call_{call_index:02d}"
        call_dir.mkdir()

        timeline = {
            "schema_version": "pm2_activity_timeline/v1",
            "scene": scene,
            "ticks": [{"tick": tick} for tick in range(2)],
        }
        timeline_path = receipts / f"call_{call_index:02d}_timeline.json"
        write_json(timeline_path, timeline)
        timeline_sha = canonical_sha256(timeline)

        call_binding = {
            "schema_version": "pm2_activity_compositor_bindings/v1",
            "scene_id": "SYNTHETIC_JOB",
            "timeline_sha256": timeline_sha,
            "authorization_effect": "none",
        }
        call_binding_path = receipts / f"call_{call_index:02d}_binding.json"
        write_json(call_binding_path, call_binding)
        call_binding_sha = sha256_path(call_binding_path)

        child_frames = []
        call_aggregate_frames = []
        for call_tick, global_tick in enumerate(range(start, end)):
            values = values_by_tick[global_tick]
            index_path = call_dir / f"tick_{call_tick:04d}.idx"
            index_path.write_bytes(values)
            index_sha = hashlib.sha256(values).hexdigest()
            child_frames.append(
                {
                    "tick": call_tick,
                    "index_file": index_path.name,
                    "index_frame_bytes": len(values),
                    "index_frame_sha256": index_sha,
                    "operations": [
                        {
                            "status": "composited",
                            "pattern_number": global_tick + 1,
                        }
                    ],
                }
            )
            aggregate_frame = {
                "call_tick": call_tick,
                "global_tick": global_tick,
                "index_frame_bytes": len(values),
                "index_frame_sha256": index_sha,
                "index_path": root_relative(root, index_path),
                "runtime_stable_zero_based_frame": 100 + global_tick,
            }
            call_aggregate_frames.append(aggregate_frame)
            aggregate_frames.append(aggregate_frame)
            sequence_ticks.append(
                {
                    "tick": global_tick,
                    "global_tick": global_tick,
                    "call_index": call_index,
                    "call_tick": call_tick,
                }
            )

        child_manifest = {
            "schema_version": "pm2_activity_composition_manifest/v1",
            "status": "offline_candidate",
            "scene_id": "SYNTHETIC_JOB",
            "timeline_sha256": timeline_sha,
            "binding_manifest_sha256": call_binding_sha,
            "calibration_status": "source_candidate",
            "canvas": canvas,
            "coordinate_contract": coordinate,
            "transform_policy": transform,
            "frames": child_frames,
            "authorization_effect": "none",
        }
        child_path = call_dir / "manifest.json"
        write_json(child_path, child_manifest)

        sequence_call = {
            "call_index": call_index,
            "global_tick_start": start,
            "global_tick_end_exclusive": end,
        }
        sequence_calls.append(sequence_call)
        spec_calls.append({"tick_count": 2})
        calls.append(
            {
                **sequence_call,
                "branch": "fixture",
                "expected_state": call_index,
                "timeline_projection": {
                    "path": root_relative(root, timeline_path),
                    "file_sha256": sha256_path(timeline_path),
                    "canonical_sha256": timeline_sha,
                },
                "binding_manifest": {
                    "path": root_relative(root, call_binding_path),
                    "file_sha256": call_binding_sha,
                },
                "composition_manifest": {
                    "path": root_relative(root, child_path),
                    "file_sha256": sha256_path(child_path),
                },
                "frames": call_aggregate_frames,
            }
        )

    sequence_spec = {"calls": spec_calls}
    sequence_spec_path = receipts / "sequence_spec.json"
    write_json(sequence_spec_path, sequence_spec)
    sequence = {
        "schema_version": "pm2_activity_timeline_sequence/v1",
        "scene": scene,
        "calls": sequence_calls,
        "ticks": sequence_ticks,
    }
    sequence_path = receipts / "sequence.json"
    write_json(sequence_path, sequence)
    aggregate = {
        "schema_version": "pm2_activity_persistent_composition_manifest/v1",
        "status": "offline_candidate",
        "scene_id": "SYNTHETIC_JOB",
        "stage": "R4",
        "call_count": len(calls),
        "tick_count": len(aggregate_frames),
        "decoder_contract": {
            "canvas": canvas,
            "pattern_author_offset_mode": "function7_coordinate_override",
            "pt1_storage_order": "x_byte_major",
            "timeline_coordinate_units": {
                "x_pixels_per_unit": 8,
                "y_pixels_per_unit": 1,
            },
        },
        "inputs": {
            "base_binding_manifest": {
                "path": root_relative(root, base_binding_path),
                "sha256": sha256_path(base_binding_path),
            },
            "pt1_structural_report": {
                "repo_path": "fixture/pt1_report.json",
                "sha256": HASH_PT1_REPORT,
            },
            "sequence_spec": {
                "path": root_relative(root, sequence_spec_path),
                "sha256": sha256_path(sequence_spec_path),
            },
            "sequence_timeline": {
                "path": root_relative(root, sequence_path),
                "file_sha256": sha256_path(sequence_path),
                "canonical_sha256": canonical_sha256(sequence),
            },
        },
        "calls": calls,
        "frames": aggregate_frames,
        "authorization_effect": "none",
        "safety": {
            "game_build_input": False,
            "reconstructed_index_frames_are_quarantined_on_d_drive": True,
        },
    }
    aggregate_path = receipts / "aggregate.json"
    write_json(aggregate_path, aggregate)
    return aggregate_path


def raster_contract() -> dict:
    return {
        "input_canvas": {"width": 4, "height": 2},
        "crop": {"x": 1, "y": 0, "width": 2, "height": 2},
        "integer_repeat": {"x": 2, "y": 1},
        "output_raster": {"width": 4, "height": 2},
        "operation_order": ["crop", "integer_repeat"],
        "interpolation": "none",
        "automatic_scaling": False,
        "thresholding": "none",
    }


def build_fixture(
    root: Path,
    *,
    persistent: bool = False,
    evidence_status: str = "confirmed",
    claims: dict[str, str] | None = None,
    same_reviewer: bool = False,
    raster_override: dict | None = None,
) -> tuple[Path, Path, Path, list[list[int]]]:
    composition_path = (
        write_persistent_r4_manifest(root) if persistent else write_r4_manifest(root)
    )
    binding_root = root / "display_binding"
    binding_root.mkdir()
    palette_entries = [[index, 16 + index, 255 - index] for index in range(16)]
    palette = {
        "schema_version": 1,
        "palette_kind": "pm2_activity_rgb16_palette",
        "entries": palette_entries,
    }
    palette_path = binding_root / "palette.json"
    write_json(palette_path, palette)
    palette_sha = sha256_path(palette_path)

    raster = raster_contract() if raster_override is None else raster_override
    raster_sha = canonical_sha256(raster)
    evidence = {
        "schema_version": 1,
        "evidence_kind": "pm2_activity_display_binding_confirmation",
        "status": evidence_status,
        "scene_id": "SYNTHETIC_JOB",
        "basis": "fixed_build_runtime_capture",
        "capture_receipt_sha256": HASH_CAPTURE,
        "composition_manifest_sha256": sha256_path(composition_path),
        "palette_sha256": palette_sha,
        "raster_contract_sha256": raster_sha,
        "claims": claims
        or {
            "palette_rgb16": "confirmed",
            "crop_rect": "confirmed",
            "integer_repeat": "confirmed",
        },
        "candidate_producer": "fixture_capture_pipeline",
        "confirmed_by": (
            "fixture_capture_pipeline" if same_reviewer else "fixture_independent_reviewer"
        ),
        "authorization_effect": "none",
    }
    evidence_path = binding_root / "evidence.json"
    write_json(evidence_path, evidence)
    binding = {
        "schema_version": 1,
        "binding_kind": "pm2_activity_offline_rgb_binding",
        "scene_id": "SYNTHETIC_JOB",
        "composition_manifest_sha256": sha256_path(composition_path),
        "pt1_report_sha256": HASH_PT1_REPORT,
        "palette": {
            "path": palette_path.name,
            "sha256": palette_sha,
            "format": "rgb24_16_entries",
        },
        "raster_contract": raster,
        "raster_contract_sha256": raster_sha,
        "evidence_receipt": {
            "path": evidence_path.name,
            "sha256": sha256_path(evidence_path),
        },
        "authorization_effect": "none",
    }
    binding_path = binding_root / "binding.json"
    write_json(binding_path, binding)
    return composition_path, binding_path, palette_path, palette_entries


def refresh_composition_binding(composition_path: Path, binding_path: Path) -> None:
    binding = json.loads(binding_path.read_text(encoding="ascii"))
    evidence_path = binding_path.parent / binding["evidence_receipt"]["path"]
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    composition_sha = sha256_path(composition_path)
    evidence["composition_manifest_sha256"] = composition_sha
    write_json(evidence_path, evidence)
    binding["composition_manifest_sha256"] = composition_sha
    binding["evidence_receipt"]["sha256"] = sha256_path(evidence_path)
    write_json(binding_path, binding)


def expect_raises(exception_type, function, contains: str | None = None):
    try:
        function()
    except exception_type as exc:
        if contains is not None:
            assert contains.lower() in str(exc).lower(), str(exc)
        return exc
    raise AssertionError(f"expected {exception_type.__name__}")


def generate(root: Path, composition: Path, binding: Path, output: Path):
    with mock.patch(
        "pm2_animation_lab.pm2_activity_offline_rgb._resolve_quarantine_root",
        return_value=root.resolve(),
    ):
        return generate_offline_rgb_ticks(composition, binding, output, root)


def check_confirmed_exact_lookup_crop_repeat_and_r5_compatibility() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, palette = build_fixture(root)
        output = root / "offline_rgb"
        receipt = generate(root, composition, binding, output)
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))

        assert receipt["status"] == "ready_written"
        assert receipt["native_raster"] == {"width": 4, "height": 2}
        assert manifest["schema_version"] == 1
        assert manifest["manifest_kind"] == "pm2_activity_offline_rgb_ticks"
        assert manifest["status"] == "ready"
        assert manifest["timeline_sha256"] == HASH_TIMELINE
        assert manifest["pt1_report_sha256"] == HASH_PT1_REPORT
        assert manifest["binding_manifest_sha256"] == HASH_R4_BINDING
        assert manifest["composition_manifest_sha256"] == sha256_path(composition)
        assert manifest["transform_policy"]["interpolation"] == "forbidden"
        assert manifest["authorization_effect"] == "none"
        assert len(manifest["ticks"]) == 2
        assert manifest["ticks"][0]["pattern_numbers"] == [1]
        assert "pattern_numbers" not in manifest["ticks"][1]

        with Image.open(output / "tick_0000.png") as image:
            assert image.mode == "RGB"
            assert image.size == (4, 2)
            pixels = [
                image.getpixel((x, y))
                for y in range(image.height)
                for x in range(image.width)
            ]
            assert pixels == [
                tuple(palette[1]),
                tuple(palette[1]),
                tuple(palette[2]),
                tuple(palette[2]),
                tuple(palette[5]),
                tuple(palette[5]),
                tuple(palette[6]),
                tuple(palette[6]),
            ]

        runtime = {
            "schema_version": 1,
            "manifest_kind": "pm2_activity_runtime_rgb_frames",
            "status": "ready",
            "scene_id": "SYNTHETIC_JOB",
            "archive_sha256": "a" * 64,
            "capture_receipt_sha256": "b" * 64,
            "replay_sha256": "c" * 64,
            "frames": [
                {
                    "frame": tick,
                    "png_path": f"tick_{tick:04d}.png",
                    "png_sha256": manifest["ticks"][tick]["png_sha256"],
                    "timestamp_us": tick * 10,
                    "pattern_numbers": manifest["ticks"][tick].get("pattern_numbers"),
                    "milestones": [],
                }
                for tick in range(2)
            ],
        }
        runtime_path = output / "runtime_manifest.json"
        write_json(runtime_path, runtime)
        report = build_alignment_report(runtime_path, manifest_path)
        assert report["status"] == "machine_exact"
        assert report["inputs"]["stage_provenance"]["complete"] is True

        encoded = manifest_path.read_text(encoding="ascii")
        assert '"entries"' not in encoded
        assert '"contains_palette_entries": false' in encoded


def check_unconfirmed_evidence_or_claim_is_not_ready_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root, evidence_status="pending")
        output = root / "must_not_exist"
        error = expect_raises(
            NotReadyError,
            lambda: generate(root, composition, binding, output),
            "status_not_confirmed",
        )
        assert error.reasons == ("display_confirmation_status_not_confirmed",)
        assert not output.exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(
            root,
            claims={
                "palette_rgb16": "confirmed",
                "crop_rect": "confirmed",
                "integer_repeat": "candidate",
            },
        )
        output = root / "must_not_exist"
        expect_raises(
            NotReadyError,
            lambda: generate(root, composition, binding, output),
            "integer_repeat_not_confirmed",
        )
        assert not output.exists()


def check_candidate_cannot_self_confirm() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root, same_reviewer=True)
        output = root / "must_not_exist"
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, output),
            "cannot confirm itself",
        )
        assert not output.exists()


def check_palette_and_index_hashes_are_enforced_before_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, palette_path, _ = build_fixture(root)
        palette = json.loads(palette_path.read_text(encoding="ascii"))
        palette["entries"][0] = [255, 255, 255]
        write_json(palette_path, palette)
        output = root / "must_not_exist"
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, output),
            "palette sha-256 mismatch",
        )
        assert not output.exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root)
        (composition.parent / "tick_0000.idx").write_bytes(bytes((15,) * 8))
        output = root / "must_not_exist"
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, output),
            "index frame sha-256 mismatch",
        )
        assert not output.exists()


def check_unapproved_transform_and_noninteger_repeat_fail_closed() -> None:
    for field, value, message in (
        ("interpolation", "bilinear", "interpolation is forbidden"),
        ("automatic_scaling", True, "automatic scaling is forbidden"),
        ("thresholding", "nearest_color", "thresholding is forbidden"),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            raster = raster_contract()
            raster[field] = value
            composition, binding, _, _ = build_fixture(root, raster_override=raster)
            expect_raises(
                OfflineRgbError,
                lambda: generate(root, composition, binding, root / "must_not_exist"),
                message,
            )
            assert not (root / "must_not_exist").exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        raster = raster_contract()
        raster["integer_repeat"]["x"] = 1.5
        composition, binding, _, _ = build_fixture(root, raster_override=raster)
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, root / "must_not_exist"),
            "integer_repeat.x",
        )


def check_existing_output_is_never_overwritten() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root)
        output = root / "existing"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("keep", encoding="ascii")
        expect_raises(
            QuarantineError,
            lambda: generate(root, composition, binding, output),
            "already exists",
        )
        assert marker.read_text(encoding="ascii") == "keep"


def check_persistent_aggregate_generates_contiguous_r5_ticks() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root, persistent=True)
        output = root / "persistent_rgb"
        receipt = generate(root, composition, binding, output)
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))

        assert receipt["status"] == "ready_written"
        assert receipt["tick_count"] == 4
        assert manifest["schema_version"] == 1
        assert manifest["manifest_kind"] == "pm2_activity_offline_rgb_ticks"
        assert manifest["source_composition_schema"] == (
            "pm2_activity_persistent_composition_manifest/v1"
        )
        assert manifest["composition_manifest_sha256"] == sha256_path(composition)
        assert manifest["timeline_sha256"] == canonical_sha256(
            json.loads((root / "receipts" / "sequence.json").read_text(encoding="ascii"))
        )
        assert [tick["tick"] for tick in manifest["ticks"]] == [0, 1, 2, 3]
        assert [tick["pattern_numbers"] for tick in manifest["ticks"]] == [
            [1],
            [2],
            [3],
            [4],
        ]
        assert all((output / f"tick_{tick:04d}.png").is_file() for tick in range(4))

        runtime = {
            "schema_version": 1,
            "manifest_kind": "pm2_activity_runtime_rgb_frames",
            "status": "ready",
            "scene_id": "SYNTHETIC_JOB",
            "archive_sha256": "a" * 64,
            "capture_receipt_sha256": "b" * 64,
            "replay_sha256": "c" * 64,
            "frames": [
                {
                    "frame": tick,
                    "png_path": f"tick_{tick:04d}.png",
                    "png_sha256": manifest["ticks"][tick]["png_sha256"],
                    "timestamp_us": tick * 10,
                    "pattern_numbers": manifest["ticks"][tick]["pattern_numbers"],
                    "milestones": [],
                }
                for tick in range(4)
            ],
        }
        runtime_path = output / "runtime_manifest.json"
        write_json(runtime_path, runtime)
        report = build_alignment_report(runtime_path, manifest_path)
        assert report["status"] == "machine_exact"


def check_persistent_aggregate_rejects_global_tick_gap_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root, persistent=True)
        aggregate = json.loads(composition.read_text(encoding="ascii"))
        aggregate["frames"][2]["global_tick"] = 3
        write_json(composition, aggregate)
        refresh_composition_binding(composition, binding)
        output = root / "must_not_exist"
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, output),
            "zero-based global_tick",
        )
        assert not output.exists()


def check_persistent_aggregate_rejects_index_hash_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root, persistent=True)
        aggregate = json.loads(composition.read_text(encoding="ascii"))
        index_path = root / aggregate["frames"][2]["index_path"]
        index_path.write_bytes(bytes((15,) * 8))
        output = root / "must_not_exist"
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, output),
            "index frame sha-256 mismatch",
        )
        assert not output.exists()


def check_persistent_aggregate_rejects_path_escape_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        composition, binding, _, _ = build_fixture(root, persistent=True)
        aggregate = json.loads(composition.read_text(encoding="ascii"))
        aggregate["frames"][0]["index_path"] = "../escape.idx"
        write_json(composition, aggregate)
        refresh_composition_binding(composition, binding)
        output = root / "must_not_exist"
        expect_raises(
            OfflineRgbError,
            lambda: generate(root, composition, binding, output),
            "parent traversal",
        )
        assert not output.exists()


class Pm2ActivityOfflineRgbTests(unittest.TestCase):
    def test_confirmed_exact_lookup_crop_repeat_and_r5_compatibility(self) -> None:
        check_confirmed_exact_lookup_crop_repeat_and_r5_compatibility()

    def test_unconfirmed_evidence_or_claim_is_not_ready_and_writes_nothing(self) -> None:
        check_unconfirmed_evidence_or_claim_is_not_ready_and_writes_nothing()

    def test_candidate_cannot_self_confirm(self) -> None:
        check_candidate_cannot_self_confirm()

    def test_palette_and_index_hashes_are_enforced_before_output(self) -> None:
        check_palette_and_index_hashes_are_enforced_before_output()

    def test_unapproved_transform_and_noninteger_repeat_fail_closed(self) -> None:
        check_unapproved_transform_and_noninteger_repeat_fail_closed()

    def test_existing_output_is_never_overwritten(self) -> None:
        check_existing_output_is_never_overwritten()

    def test_persistent_aggregate_generates_contiguous_r5_ticks(self) -> None:
        check_persistent_aggregate_generates_contiguous_r5_ticks()

    def test_persistent_aggregate_rejects_global_tick_gap_without_output(self) -> None:
        check_persistent_aggregate_rejects_global_tick_gap_without_output()

    def test_persistent_aggregate_rejects_index_hash_without_output(self) -> None:
        check_persistent_aggregate_rejects_index_hash_without_output()

    def test_persistent_aggregate_rejects_path_escape_without_output(self) -> None:
        check_persistent_aggregate_rejects_path_escape_without_output()


def run() -> None:
    check_confirmed_exact_lookup_crop_repeat_and_r5_compatibility()
    check_unconfirmed_evidence_or_claim_is_not_ready_and_writes_nothing()
    check_candidate_cannot_self_confirm()
    check_palette_and_index_hashes_are_enforced_before_output()
    check_unapproved_transform_and_noninteger_repeat_fail_closed()
    check_existing_output_is_never_overwritten()
    check_persistent_aggregate_generates_contiguous_r5_ticks()
    check_persistent_aggregate_rejects_global_tick_gap_without_output()
    check_persistent_aggregate_rejects_index_hash_without_output()
    check_persistent_aggregate_rejects_path_escape_without_output()
    print(
        "PM2 offline RGB tests passed: explicit hash-bound RGB16 palette, confirmed crop/repeat, "
        "R5-compatible single/persistent native PNG manifests, independent confirmation, "
        "and fail-closed guards."
    )


if __name__ == "__main__":
    run()
