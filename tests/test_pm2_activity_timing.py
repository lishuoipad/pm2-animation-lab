from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from pm2_animation_lab.pm2_activity_timing import TimingError, build_timing_report, main


HASH = "a" * 64


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_rgb_sha(path: Path) -> str:
    with Image.open(path) as image:
        image.load()
        return hashlib.sha256(image.tobytes()).hexdigest()


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    ).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def boundary_us(frame: int) -> int:
    return (frame * 1_000_000 + 30) // 60


def expect_error(function, text: str) -> None:
    try:
        function()
    except TimingError as exc:
        assert text.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError("expected TimingError")


def make_fixture(root: Path) -> dict[str, Path | str]:
    evidence = root / "evidence"
    repository = root / "repository"
    receipts = evidence / "receipts"
    runtime_dir = evidence / "runtime_selected"
    index_dir = evidence / "index"
    receipts.mkdir(parents=True)
    runtime_dir.mkdir()
    index_dir.mkdir()
    (repository / "work").mkdir(parents=True)

    pt1 = repository / "work" / "pt1_report.json"
    write_json(pt1, {"schema_version": 1, "status": "fixture"})
    base_binding = receipts / "base_binding.json"
    write_json(base_binding, {"scene_id": "SYNTHETIC_JOB", "authorization_effect": "none"})
    sequence_spec = receipts / "sequence_spec.json"
    write_json(sequence_spec, {"calls": [{"tick_count": 2}, {"tick_count": 2}]})
    sequence_timeline_value = {
        "schema_version": "pm2_activity_timeline_sequence/v1",
        "scene_id": "SYNTHETIC_JOB",
        "ticks": [0, 1, 2, 3],
    }
    sequence_timeline = receipts / "sequence_timeline.json"
    write_json(sequence_timeline, sequence_timeline_value)

    colors = [(12, 34, 56), (70, 80, 90), (101, 112, 123), (201, 202, 203)]
    pngs: list[Path] = []
    for tick, color in enumerate(colors):
        path = runtime_dir / f"frame_{tick:02d}.png"
        Image.new("RGB", (2, 1), color).save(path, format="PNG")
        pngs.append(path)

    stable = [3, 7, 14, 18]
    ranges = [(2, 4), (7, 8), (12, 15), (17, 18)]
    aggregate_frames: list[dict] = []
    for tick, stable_frame in enumerate(stable):
        index_path = index_dir / f"tick_{tick:02d}.idx"
        index_path.write_bytes(bytes([tick, tick + 1, tick + 2]))
        aggregate_frames.append(
            {
                "global_tick": tick,
                "call_tick": tick % 2,
                "index_frame_bytes": 3,
                "index_frame_sha256": sha256(index_path),
                "index_path": relative(evidence, index_path),
                "runtime_stable_zero_based_frame": stable_frame,
            }
        )

    calls: list[dict] = []
    for call_index in range(2):
        start = call_index * 2
        end = start + 2
        timeline_value = {
            "schema_version": "pm2_activity_timeline/v1",
            "scene_id": "SYNTHETIC_JOB",
            "ticks": [0, 1],
        }
        timeline = receipts / f"call_{call_index}_timeline.json"
        write_json(timeline, timeline_value)
        binding = receipts / f"call_{call_index}_binding.json"
        write_json(binding, {"scene_id": "SYNTHETIC_JOB", "authorization_effect": "none"})
        composition = receipts / f"call_{call_index}_composition.json"
        write_json(
            composition,
            {
                "schema_version": "pm2_activity_composition_manifest/v1",
                "scene_id": "SYNTHETIC_JOB",
                "frames": [0, 1],
                "authorization_effect": "none",
            },
        )
        calls.append(
            {
                "call_index": call_index,
                "global_tick_start": start,
                "global_tick_end_exclusive": end,
                "runtime_stable_zero_based_frames": stable[start:end],
                "timeline_projection": {
                    "path": relative(evidence, timeline),
                    "file_sha256": sha256(timeline),
                    "canonical_sha256": canonical_sha(timeline_value),
                },
                "binding_manifest": {
                    "path": relative(evidence, binding),
                    "file_sha256": sha256(binding),
                },
                "composition_manifest": {
                    "path": relative(evidence, composition),
                    "file_sha256": sha256(composition),
                },
                "frames": aggregate_frames[start:end],
            }
        )

    aggregate = {
        "schema_version": "pm2_activity_persistent_composition_manifest/v1",
        "status": "offline_candidate",
        "scene_id": "SYNTHETIC_JOB",
        "stage": "R4",
        "call_count": 2,
        "tick_count": 4,
        "inputs": {
            "fixed_archive_sha256": "1" * 64,
            "base_binding_manifest": {
                "path": relative(evidence, base_binding),
                "sha256": sha256(base_binding),
            },
            "pt1_structural_report": {
                "repo_path": "work/pt1_report.json",
                "sha256": sha256(pt1),
            },
            "sequence_spec": {
                "path": relative(evidence, sequence_spec),
                "sha256": sha256(sequence_spec),
            },
            "sequence_timeline": {
                "path": relative(evidence, sequence_timeline),
                "file_sha256": sha256(sequence_timeline),
                "canonical_sha256": canonical_sha(sequence_timeline_value),
            },
        },
        "calls": calls,
        "frames": aggregate_frames,
        "last_workday_five_index_frame_sha256": [
            frame["index_frame_sha256"] for frame in aggregate_frames[-2:]
        ],
        "authorization_effect": "none",
        "safety": {"game_build_input": False},
    }
    aggregate_path = receipts / "aggregate.json"
    write_json(aggregate_path, aggregate)

    stage = {
        "schema_version": 1,
        "scene_id": "SYNTHETIC_JOB",
        "crop": {"x": 1, "y": 1, "width": 2, "height": 1},
        "time_base": "1/60",
        "frame_count": 24,
        "visible_runs": [],
    }
    complete_runs = [
        (2, 4, raw_rgb_sha(pngs[0])),
        (5, 6, "7" * 64),
        (7, 8, raw_rgb_sha(pngs[1])),
        (9, 11, "8" * 64),
        (12, 15, raw_rgb_sha(pngs[2])),
        (16, 16, "9" * 64),
        (17, 18, raw_rgb_sha(pngs[3])),
        (19, 21, "b" * 64),
    ]
    stage["visible_runs"] = [
        {
            "run": run,
            "start_frame": start,
            "end_frame": end,
            "frame_count": end - start + 1,
            "rgb_sha256": rgb_sha256,
        }
        for run, (start, end, rgb_sha256) in enumerate(complete_runs)
    ]
    stage_path = receipts / "stage_visible_runs.json"
    write_json(stage_path, stage)

    runtime_frames = []
    for tick, stable_frame in enumerate(stable):
        png = pngs[tick]
        runtime_frames.append(
            {
                "frame": stable_frame + 1,
                "source_sequence_position": stable_frame,
                "png_path": png.name,
                "png_sha256": sha256(png),
                "rgb_sha256": raw_rgb_sha(png),
                "source_png_sha256": "2" * 64,
                "source_rgb_sha256": "3" * 64,
                "timestamp_us": boundary_us(stable_frame),
                "duration_us": boundary_us(stable_frame + 1) - boundary_us(stable_frame),
            }
        )
    runtime = {
        "schema_version": 1,
        "manifest_kind": "pm2_activity_runtime_rgb_frames",
        "status": "ready",
        "scene_id": "SYNTHETIC_JOB",
        "archive_sha256": "1" * 64,
        "source_mkv_sha256": "4" * 64,
        "framehash_sha256": "5" * 64,
        "capture_receipt_sha256": "6" * 64,
        "source_sequence": {
            "frame_count": 24,
            "first_frame": 1,
            "last_frame": 24,
            "width": 4,
            "height": 2,
            "mode": "RGB",
            "filename_pattern": "frame_%06d.png",
            "framehash_pixel_format": "rgb24",
        },
        "selection": {
            "kind": "explicit_list",
            "frame_list": [frame + 1 for frame in stable],
        },
        "crop": {"x": 1, "y": 1, "width": 2, "height": 1},
        "native_raster": {"width": 2, "height": 1, "mode": "RGB"},
        "frames": runtime_frames,
        "semantic_interpretation": None,
        "automatic_s2_promotion": False,
        "authorization_effect": "none",
    }
    runtime_path = runtime_dir / "manifest.json"
    write_json(runtime_path, runtime)
    return {
        "evidence": evidence,
        "repository": repository,
        "stage": stage_path,
        "aggregate": aggregate_path,
        "runtime": runtime_path,
        "index_0": index_dir / "tick_00.idx",
        "png_0": pngs[0],
    }


def report_for(paths: dict[str, Path | str]) -> dict:
    stage = Path(paths["stage"])
    aggregate = Path(paths["aggregate"])
    runtime = Path(paths["runtime"])
    return build_timing_report(
        stage,
        aggregate,
        runtime,
        stage_visible_runs_sha256=sha256(stage),
        aggregate_sha256=sha256(aggregate),
        runtime_manifest_sha256=sha256(runtime),
        evidence_root=Path(paths["evidence"]),
        repository_root=Path(paths["repository"]),
    )


class Pm2ActivityTimingTests(unittest.TestCase):
    def test_maps_stable_runs_and_separates_call_boundary_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = report_for(make_fixture(Path(temporary)))

        self.assertEqual(report["status"], "machine_timing_mapped_with_explicit_limits")
        ticks = report["mapping"]["ticks"]
        self.assertEqual(ticks[0]["stable_run"]["frame_count"], 3)
        self.assertEqual(ticks[0]["stable_run"]["duration_us"], 50_000)
        self.assertEqual(ticks[0]["transition_after"]["frame_count"], 2)
        self.assertEqual(ticks[0]["transition_after"]["scope"], "within_call_transition")
        self.assertEqual(
            ticks[1]["transition_after"]["scope"],
            "call_boundary_extra_hold_or_transition",
        )
        self.assertEqual(ticks[1]["transition_after"]["frame_count"], 3)
        calls = report["mapping"]["calls"]
        self.assertEqual(calls[0]["stable_frame_count_total"], 5)
        self.assertEqual(calls[0]["within_call_transition_frame_count_total"], 2)
        self.assertEqual(calls[0]["boundary_after"]["frame_count"], 3)
        self.assertEqual(calls[0]["observed_call_span"]["frame_count"], 7)
        self.assertEqual(
            ticks[-1]["transition_after"]["scope"],
            "terminal_unmeasured_after_last_stable_run",
        )
        self.assertIsNone(ticks[-1]["transition_after"]["duration_us"])
        self.assertEqual(
            report["evidence_limits"]["last_tick_duration_scope"],
            "observed_stable_run_only",
        )

    def test_binds_manual_no_replay_and_never_promotes_or_interprets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = report_for(make_fixture(Path(temporary)))
        self.assertEqual(report["capture_contract"]["capture_method"], "manual_fixed_build_capture")
        self.assertFalse(report["capture_contract"]["replay_bound"])
        self.assertEqual(
            report["evidence_limits"]["rng_relationship"],
            "compatible_witness_not_actual_rng",
        )
        self.assertFalse(report["evidence_limits"]["automatic_semantic_interpretation"])
        self.assertIsNone(report["evidence_limits"]["semantic_interpretation"])
        self.assertFalse(report["evidence_limits"]["automatic_s2_promotion"])
        self.assertEqual(report["evidence_limits"]["authorization_effect"], "none")
        self.assertEqual(report["safety"]["output_kind"], "safe_json_only")
        self.assertFalse(report["safety"]["contains_image_bytes"])

    def test_tampered_rgb_or_input_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            png = Path(paths["png_0"])
            Image.new("RGB", (2, 1), (255, 0, 0)).save(png, format="PNG")
            expect_error(lambda: report_for(paths), "PNG SHA-256 mismatch")

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            expect_error(
                lambda: build_timing_report(
                    Path(paths["stage"]),
                    Path(paths["aggregate"]),
                    Path(paths["runtime"]),
                    stage_visible_runs_sha256="0" * 64,
                    aggregate_sha256=sha256(Path(paths["aggregate"])),
                    runtime_manifest_sha256=sha256(Path(paths["runtime"])),
                    evidence_root=Path(paths["evidence"]),
                    repository_root=Path(paths["repository"]),
                ),
                "stage_visible_runs SHA-256 mismatch",
            )

    def test_sha256_list_is_strictly_validated_item_by_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            report = report_for(paths)
            self.assertEqual(report["mapping"]["tick_count"], 4)

            aggregate_path = Path(paths["aggregate"])
            aggregate = json.loads(aggregate_path.read_text(encoding="ascii"))
            aggregate["last_workday_five_index_frame_sha256"][1] = "not-a-digest"
            write_json(aggregate_path, aggregate)
            expect_error(
                lambda: report_for(paths),
                "last_workday_five_index_frame_sha256[1] must be a SHA-256",
            )

    def test_reference_path_hash_and_tick_continuity_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            aggregate_path = Path(paths["aggregate"])
            aggregate = json.loads(aggregate_path.read_text(encoding="ascii"))
            aggregate["frames"][0]["index_path"] = "../escape.idx"
            aggregate["calls"][0]["frames"][0]["index_path"] = "../escape.idx"
            write_json(aggregate_path, aggregate)
            expect_error(lambda: report_for(paths), "traversal-free")

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            Path(paths["index_0"]).write_bytes(b"tampered")
            expect_error(lambda: report_for(paths), "index frame SHA-256 mismatch")

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            aggregate_path = Path(paths["aggregate"])
            aggregate = json.loads(aggregate_path.read_text(encoding="ascii"))
            aggregate["frames"][1]["global_tick"] = 2
            aggregate["calls"][0]["frames"][1]["global_tick"] = 2
            write_json(aggregate_path, aggregate)
            expect_error(lambda: report_for(paths), "global ticks must be contiguous")

    def test_scene_crop_frame_order_and_stable_run_rgb_fail_closed(self) -> None:
        mutations = (
            ("scene", "scene_id", "OTHER", "scene_id does not match"),
            ("crop", "crop", {"x": 0, "y": 1, "width": 2, "height": 1}, "crop does not match"),
        )
        for _, field, replacement, error in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                paths = make_fixture(Path(temporary))
                runtime_path = Path(paths["runtime"])
                runtime = json.loads(runtime_path.read_text(encoding="ascii"))
                runtime[field] = replacement
                write_json(runtime_path, runtime)
                expect_error(lambda: report_for(paths), error)

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            runtime_path = Path(paths["runtime"])
            runtime = json.loads(runtime_path.read_text(encoding="ascii"))
            runtime["frames"][1]["source_sequence_position"] += 1
            write_json(runtime_path, runtime)
            expect_error(lambda: report_for(paths), "one-based frame to zero-based position")

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            stage_path = Path(paths["stage"])
            stage = json.loads(stage_path.read_text(encoding="ascii"))
            stage["visible_runs"][0]["rgb_sha256"] = "0" * 64
            write_json(stage_path, stage)
            expect_error(lambda: report_for(paths), "visible-run RGB SHA-256 mismatch")

    def test_cli_writes_only_json_and_failure_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            output = Path(temporary) / "timing.json"
            arguments = [
                "--stage-visible-runs",
                str(paths["stage"]),
                "--stage-visible-runs-sha256",
                sha256(Path(paths["stage"])),
                "--aggregate",
                str(paths["aggregate"]),
                "--aggregate-sha256",
                sha256(Path(paths["aggregate"])),
                "--runtime-manifest",
                str(paths["runtime"]),
                "--runtime-manifest-sha256",
                sha256(Path(paths["runtime"])),
                "--evidence-root",
                str(paths["evidence"]),
                "--repository-root",
                str(paths["repository"]),
                "--output",
                str(output),
            ]
            self.assertEqual(main(arguments), 0)
            parsed = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(parsed["report_kind"], "pm2_activity_stable_run_timing_map")
            self.assertEqual(list(output.parent.glob("*.png")), [])

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            output = Path(temporary) / "must_not_exist.json"
            Path(paths["index_0"]).write_bytes(b"tampered")
            arguments = [
                "--stage-visible-runs",
                str(paths["stage"]),
                "--stage-visible-runs-sha256",
                sha256(Path(paths["stage"])),
                "--aggregate",
                str(paths["aggregate"]),
                "--aggregate-sha256",
                sha256(Path(paths["aggregate"])),
                "--runtime-manifest",
                str(paths["runtime"]),
                "--runtime-manifest-sha256",
                sha256(Path(paths["runtime"])),
                "--evidence-root",
                str(paths["evidence"]),
                "--repository-root",
                str(paths["repository"]),
                "--output",
                str(output),
            ]
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(main(arguments), 2)
            self.assertIn("index frame SHA-256 mismatch", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_never_overwrites_existing_or_racing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            output = Path(temporary) / "existing.json"
            output.write_text("keep", encoding="ascii")
            arguments = [
                "--stage-visible-runs", str(paths["stage"]),
                "--stage-visible-runs-sha256", sha256(Path(paths["stage"])),
                "--aggregate", str(paths["aggregate"]),
                "--aggregate-sha256", sha256(Path(paths["aggregate"])),
                "--runtime-manifest", str(paths["runtime"]),
                "--runtime-manifest-sha256", sha256(Path(paths["runtime"])),
                "--evidence-root", str(paths["evidence"]),
                "--repository-root", str(paths["repository"]),
                "--output", str(output),
            ]
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(main(arguments), 2)
            self.assertEqual(output.read_text(encoding="ascii"), "keep")
            self.assertNotIn("Traceback", stderr.getvalue())

        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            output = Path(temporary) / "racing.json"
            arguments = [
                "--stage-visible-runs", str(paths["stage"]),
                "--stage-visible-runs-sha256", sha256(Path(paths["stage"])),
                "--aggregate", str(paths["aggregate"]),
                "--aggregate-sha256", sha256(Path(paths["aggregate"])),
                "--runtime-manifest", str(paths["runtime"]),
                "--runtime-manifest-sha256", sha256(Path(paths["runtime"])),
                "--evidence-root", str(paths["evidence"]),
                "--repository-root", str(paths["repository"]),
                "--output", str(output),
            ]

            def competing_publish(_source, destination) -> None:
                Path(destination).write_text("competitor", encoding="ascii")
                raise FileExistsError("simulated competing publisher")

            stderr = io.StringIO()
            with mock.patch(
                "pm2_animation_lab.pm2_activity_timing.os.link", side_effect=competing_publish
            ), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(arguments), 2)
            self.assertEqual(output.read_text(encoding="ascii"), "competitor")
            self.assertIn("output already exists", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_cli_stdout_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(Path(temporary))
            arguments = [
                "--stage-visible-runs",
                str(paths["stage"]),
                "--stage-visible-runs-sha256",
                sha256(Path(paths["stage"])),
                "--aggregate",
                str(paths["aggregate"]),
                "--aggregate-sha256",
                sha256(Path(paths["aggregate"])),
                "--runtime-manifest",
                str(paths["runtime"]),
                "--runtime-manifest-sha256",
                sha256(Path(paths["runtime"])),
                "--evidence-root",
                str(paths["evidence"]),
                "--repository-root",
                str(paths["repository"]),
            ]
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(main(arguments), 0)
            self.assertEqual(json.loads(captured.getvalue())["status"], "machine_timing_mapped_with_explicit_limits")


if __name__ == "__main__":
    unittest.main()
