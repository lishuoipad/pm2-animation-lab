from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from pm2_animation_lab.pm2_activity_alignment import AlignmentError, build_alignment_report, main


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rgb_sha256(path: Path) -> str:
    with Image.open(path) as image:
        image.load()
        return hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()


def expect_raises(exception_type, function, contains: str | None = None) -> None:
    try:
        function()
    except exception_type as exc:
        if contains is not None:
            assert contains.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError(f"expected {exception_type.__name__}")


def write_png(path: Path, color: tuple[int, int, int], size: tuple[int, int] = (3, 2)) -> None:
    Image.new("RGB", size, color).save(path, format="PNG")


def write_manifest(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_entry(
    identifier_name: str,
    identifier: int,
    png_path: Path,
    *,
    timestamp_us: int | None = None,
    duration_us: int | None = None,
    logical_wait: int | None = None,
    milestones: list[str] | None = None,
    pattern_number: int | None = None,
) -> dict:
    entry = {
        identifier_name: identifier,
        "png_path": png_path.name,
        "png_sha256": sha256(png_path),
        "rgb_sha256": rgb_sha256(png_path),
        "milestones": milestones or [],
    }
    if timestamp_us is not None:
        entry["timestamp_us"] = timestamp_us
    if duration_us is not None:
        entry["duration_us"] = duration_us
    if logical_wait is not None:
        entry["logical_wait"] = logical_wait
    if pattern_number is not None:
        entry["pattern_number"] = pattern_number
    return entry


def build_ready_manifests(root: Path, *, final_runtime_color=(0, 255, 0), runtime_size=(3, 2)):
    colors_runtime = [(0, 0, 0), (0, 0, 0), (255, 0, 0), (255, 0, 0), final_runtime_color]
    colors_offline = [(0, 0, 0), (255, 0, 0), (255, 0, 0), (0, 255, 0)]
    runtime_paths = []
    offline_paths = []
    for index, color in enumerate(colors_runtime):
        path = root / f"runtime_{index}.png"
        write_png(path, color, runtime_size)
        runtime_paths.append(path)
    for index, color in enumerate(colors_offline):
        path = root / f"offline_{index}.png"
        write_png(path, color)
        offline_paths.append(path)

    runtime = {
        "schema_version": 1,
        "manifest_kind": "pm2_activity_runtime_rgb_frames",
        "status": "ready",
        "scene_id": "synthetic_job",
        "archive_sha256": "0" * 64,
        "capture_receipt_sha256": "3" * 64,
        "replay_sha256": "4" * 64,
        "contact_review_items": [
            {
                "id": "contact_peak",
                "milestone": "contact",
                "hand": "key hand visible",
                "tool": "tool endpoint visible",
                "target": "target surface visible",
                "response": "response follows contact",
            }
        ],
        "frames": [
            make_entry(
                "frame",
                index,
                path,
                timestamp_us=index * 10,
                duration_us=10,
                milestones=["contact"] if index == 4 else [],
                pattern_number=0 if index < 2 else 1 if index < 4 else 2,
            )
            for index, path in enumerate(runtime_paths)
        ],
    }
    offline = {
        "schema_version": 1,
        "manifest_kind": "pm2_activity_offline_rgb_ticks",
        "status": "ready",
        "scene_id": "synthetic_job",
        "timeline_sha256": "1" * 64,
        "pt1_report_sha256": "5" * 64,
        "binding_manifest_sha256": "2" * 64,
        "composition_manifest_sha256": "6" * 64,
        "coordinate_contract": {
            "pattern_author_offset_mode": "function7_coordinate_override",
            "wwanime_function": 7,
        },
        "ticks": [
            make_entry(
                "tick",
                index,
                path,
                duration_us=(10, 20, 10, 10)[index],
                logical_wait=(1, 2, 1, 1)[index],
                milestones=["contact"] if index == 3 else [],
                pattern_number=(0, 1, 1, 2)[index],
            )
            for index, path in enumerate(offline_paths)
        ],
    }
    runtime_path = root / "runtime_manifest.json"
    offline_path = root / "offline_manifest.json"
    write_manifest(runtime_path, runtime)
    write_manifest(offline_path, offline)
    return runtime_path, offline_path


def check_exact_alignment_and_human_review_template() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime, offline = build_ready_manifests(root)
        report = build_alignment_report(runtime, offline)
    assert report["status"] == "machine_exact"
    assert report["alignment"]["first_visible_change"] == {
        "status": "paired",
        "runtime_frame": 2,
        "offline_tick": 1,
    }
    assert report["alignment"]["shared_milestones"] == ["contact"]
    intervals = report["alignment"]["frame_tick_intervals"]
    assert len(intervals) == 3
    assert intervals[0]["runtime"]["frame_id_range"] == [0, 1]
    assert intervals[1]["offline"]["tick_id_range"] == [1, 2]
    assert all(item["exact_comparison"]["exact_rgb_match"] for item in intervals)
    assert report["exact_totals"] == {
        "aligned_interval_count": 3,
        "exact_rgb_interval_count": 3,
        "size_mismatch_interval_count": 0,
        "pixel_difference_count_across_representative_runs": 0,
    }
    assert report["comparison_summary"]["duration"]["status"] == "exact"
    assert report["inputs"]["offline_coordinate_contract"]["pattern_author_offset_mode"] == "function7_coordinate_override"
    assert report["inputs"]["stage_provenance"]["complete"] is True
    review = report["native_size_contact_review"]
    assert review["status"] == "awaiting_human_review"
    assert review["allowed_manual_conclusions"] == ["clear", "partial", "unreadable"]
    assert review["items"][0]["manual_conclusions"] == {
        "hand": None,
        "tool": None,
        "target": None,
        "response": None,
        "overall": None,
    }
    assert review["automatic_semantic_conclusion"] is None
    assert report["fixed_build_s2_calibration"] == {
        "status": "pending_native_size_human_contact_review",
        "automatic_s2_promotion": False,
        "machine_rgb_alignment_exact": True,
    }
    assert report["transform_policy"] == {
        "scaling": "forbidden",
        "interpolation": "forbidden",
        "fuzzy_threshold": "forbidden",
        "comparison_size": "native",
    }


def check_exact_pixel_difference_and_unclassified_slots() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime, offline = build_ready_manifests(root, final_runtime_color=(0, 0, 255))
        report = build_alignment_report(runtime, offline)
    assert report["status"] == "completed_with_differences_or_unknowns"
    comparison = report["alignment"]["frame_tick_intervals"][-1]["exact_comparison"]
    assert comparison["comparison_status"] == "different"
    assert comparison["pixel_difference_count"] == 6
    assert comparison["pixel_difference_bbox_ltrb"] == [0, 0, 3, 2]
    classification = report["alignment"]["frame_tick_intervals"][-1]["classification"]
    assert classification["selected"] is None
    assert all(item["status"] == "unassessed" for item in classification["slots"].values())
    assert report["comparison_summary"]["pattern"]["status"] == "unknown"
    assert report["comparison_summary"]["geometry"]["status"] == "unknown"
    assert report["comparison_summary"]["occlusion"]["status"] == "unknown"


def check_size_mismatch_is_not_scaled() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime, offline = build_ready_manifests(root, runtime_size=(4, 2))
        report = build_alignment_report(runtime, offline)
    comparison = report["alignment"]["frame_tick_intervals"][0]["exact_comparison"]
    assert comparison["comparison_status"] == "size_mismatch"
    assert comparison["pixel_difference_count"] is None
    slots = report["alignment"]["frame_tick_intervals"][0]["classification"]["slots"]
    assert slots["video_mode"]["status"] == "candidate_not_conclusion"
    assert slots["format"]["status"] == "candidate_not_conclusion"
    assert report["comparison_summary"]["geometry"]["status"] == "unknown"


def check_not_run_and_input_guards() -> None:
    missing = build_alignment_report(None, None)
    assert missing["status"] == "not_run"
    assert missing["comparison_summary"]["pattern"]["status"] == "unknown"
    assert missing["native_size_contact_review"]["automatic_semantic_conclusion"] is None

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime, offline = build_ready_manifests(root)
        offline_data = json.loads(offline.read_text(encoding="utf-8"))
        offline_data["status"] = "offline_candidate"
        write_manifest(offline, offline_data)
        not_ready = build_alignment_report(runtime, offline)
        assert not_ready["status"] == "not_run"
        assert not_ready["reasons"] == ["offline_manifest_status_not_ready"]

        offline_data["status"] = "ready"
        offline_data["ticks"][0]["png_sha256"] = "0" * 64
        write_manifest(offline, offline_data)
        expect_raises(
            AlignmentError,
            lambda: build_alignment_report(runtime, offline),
            "sha-256 mismatch",
        )

        offline_data["ticks"][0]["png_sha256"] = sha256(root / "offline_0.png")
        offline_data["ticks"][0]["png_path"] = "../escape.png"
        write_manifest(offline, offline_data)
        expect_raises(
            AlignmentError,
            lambda: build_alignment_report(runtime, offline),
            "escapes",
        )


def check_declared_rgb_hash_and_cli_no_overwrite() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime, offline = build_ready_manifests(root)
        runtime_data = json.loads(runtime.read_text(encoding="utf-8"))
        runtime_data["frames"][0]["rgb_sha256"] = "0" * 64
        write_manifest(runtime, runtime_data)
        expect_raises(
            AlignmentError,
            lambda: build_alignment_report(runtime, offline),
            "raw RGB SHA-256 mismatch",
        )

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        runtime, offline = build_ready_manifests(root)
        output = root / "existing.json"
        output.write_text("keep", encoding="ascii")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "--runtime-manifest",
                    str(runtime),
                    "--offline-manifest",
                    str(offline),
                    "--output",
                    str(output),
                ]
            )
        assert result == 2
        assert output.read_text(encoding="ascii") == "keep"
        assert "output already exists" in stderr.getvalue()
        assert "Traceback" not in stderr.getvalue()

        created = root / "created.json"
        assert main(
            [
                "--runtime-manifest",
                str(runtime),
                "--offline-manifest",
                str(offline),
                "--output",
                str(created),
            ]
        ) == 0
        assert json.loads(created.read_text(encoding="utf-8"))["status"] == "machine_exact"


class Pm2ActivityAlignmentTests(unittest.TestCase):
    def test_exact_alignment_and_human_review_template(self) -> None:
        check_exact_alignment_and_human_review_template()

    def test_exact_pixel_difference_and_unclassified_slots(self) -> None:
        check_exact_pixel_difference_and_unclassified_slots()

    def test_size_mismatch_is_not_scaled(self) -> None:
        check_size_mismatch_is_not_scaled()

    def test_not_run_and_input_guards(self) -> None:
        check_not_run_and_input_guards()

    def test_declared_rgb_hash_and_cli_no_overwrite(self) -> None:
        check_declared_rgb_hash_and_cli_no_overwrite()


def run() -> None:
    check_exact_alignment_and_human_review_template()
    check_exact_pixel_difference_and_unclassified_slots()
    check_size_mismatch_is_not_scaled()
    check_not_run_and_input_guards()
    check_declared_rgb_hash_and_cli_no_overwrite()
    print(
        "PM2 activity alignment tests passed: exact native RGB, visible-run/milestone mapping, "
        "safe difference statistics, not-run state, and human-only contact review."
    )


if __name__ == "__main__":
    run()
