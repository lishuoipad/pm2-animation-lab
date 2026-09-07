from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm2_animation_lab.pm2_activity_transition_copy import (
    FIXED_JOB003_PALETTE_SHA256,
    FIXED_SOURCE_REGISTRY,
    Raster,
    TransitionCopyError,
    _write_json_atomic,
    build_report,
    classify_transition,
    find_planar_copy_fits,
    main,
)


def raster(width: int, rows: list[list[int]]) -> Raster:
    return Raster(
        width=width,
        height=len(rows),
        indices=tuple(value for row in rows for value in row),
        raw_rgb_sha256="0" * 64,
    )


def planar_middle(
    previous: Raster,
    following: Raster,
    *,
    seam: int,
    completed_planes: int,
    boundary_x: int,
) -> Raster:
    values: list[int] = []
    low = (1 << completed_planes) - 1
    high = (1 << (completed_planes + 1)) - 1
    for y in range(previous.height):
        for x in range(previous.width):
            position = y * previous.width + x
            old = previous.indices[position]
            new = following.indices[position]
            if y < seam:
                values.append(new)
            elif y > seam:
                values.append(old)
            else:
                mask = high if x < boundary_x else low
                values.append((old & (~mask & 15)) | (new & mask))
    return Raster(
        width=previous.width,
        height=previous.height,
        indices=tuple(values),
        raw_rgb_sha256="1" * 64,
    )


def cli_arguments(output: Path | None = None) -> list[str]:
    digest = "a" * 64
    arguments: list[str] = []
    for name in (
        "timing",
        "stage",
        "source-framehash",
        "stage-framehash",
        "runtime-manifest",
        "offline-manifest",
        "r4-aggregate",
        "palette",
        "job-script",
        "widanime",
        "pictuer",
        "vrmove",
    ):
        arguments.extend([f"--{name}", "unused", f"--{name}-sha256", digest])
    arguments.extend(["--full-frames-dir", "unused"])
    if output is not None:
        arguments.extend(["--output", str(output)])
    return arguments


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_kwargs(root: Path) -> dict[str, object]:
    unused = root / "unused"
    digest = "a" * 64
    return {
        "timing_path": unused,
        "timing_sha256": digest,
        "stage_path": unused,
        "stage_sha256": digest,
        "source_framehash_path": unused,
        "source_framehash_sha256": digest,
        "stage_framehash_path": unused,
        "stage_framehash_sha256": digest,
        "full_frames_dir": unused,
        "runtime_manifest_path": unused,
        "runtime_manifest_sha256": digest,
        "offline_manifest_path": unused,
        "offline_manifest_sha256": digest,
        "r4_aggregate_path": unused,
        "r4_aggregate_sha256": digest,
        "palette_path": unused,
        "palette_sha256": digest,
        "job_script_path": unused,
        "job_script_sha256": digest,
        "widanime_path": unused,
        "widanime_sha256": digest,
        "pictuer_path": unused,
        "pictuer_sha256": digest,
        "vrmove_path": unused,
        "vrmove_sha256": digest,
    }


def mocked_documents(palette_sha256: str) -> list[dict[str, object]]:
    return [
        {},
        {},
        {},
        {"palette_file_sha256": palette_sha256},
        {},
    ]


class TransitionCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        width = 16
        self.previous = raster(
            width,
            [
                [0] * width,
                [3, 5, 9, 12] * 4,
                [6, 10, 13, 15] * 4,
                [7, 11, 14, 4] * 4,
            ],
        )
        self.following = raster(
            width,
            [
                [15] * width,
                [12, 10, 6, 3] * 4,
                [9, 5, 2, 0] * 4,
                [8, 4, 1, 13] * 4,
            ],
        )

    def test_exact_top_down_planar_partial_row_fit(self) -> None:
        middle = planar_middle(
            self.previous,
            self.following,
            seam=2,
            completed_planes=1,
            boundary_x=8,
        )
        fits = find_planar_copy_fits(
            self.previous.indices,
            middle.indices,
            self.following.indices,
            self.previous.width,
            self.previous.height,
        )
        self.assertIn((2, 1, 8), [(x.seam_row, x.completed_plane_count, x.boundary_x) for x in fits])

        report = classify_transition(self.previous, middle, self.following)
        self.assertTrue(report["top_down_planar_copy_fit"]["exact"])
        self.assertGreater(report["pixel_counts"]["neither_stable_value"], 0)
        self.assertTrue(
            report["hybrid_palette_indices"]["all_match_cumulative_low_plane_masks"]
        )

    def test_pure_previous_following_mosaic_is_a_copy_subset(self) -> None:
        rows = [
            list(self.following.indices[0:16]),
            list(self.following.indices[16:32]),
            list(self.previous.indices[32:48]),
            list(self.previous.indices[48:64]),
        ]
        middle = raster(16, rows)
        report = classify_transition(self.previous, middle, self.following)
        self.assertTrue(report["raw_previous_or_following_only"])
        self.assertTrue(report["top_down_planar_copy_fit"]["exact"])

    def test_authored_third_value_does_not_fit_copy_model(self) -> None:
        middle = planar_middle(
            self.previous,
            self.following,
            seam=2,
            completed_planes=1,
            boundary_x=8,
        )
        values = list(middle.indices)
        position = 2 * middle.width
        old = self.previous.indices[position]
        new = self.following.indices[position]
        incompatible = next(
            value
            for value in range(16)
            if value not in {old, new}
            and all(((old & (~mask & 15)) | (new & mask)) != value for mask in (1, 3, 7))
        )
        values[position] = incompatible
        authored = Raster(16, 4, tuple(values), "2" * 64)
        report = classify_transition(self.previous, authored, self.following)
        self.assertFalse(report["top_down_planar_copy_fit"]["exact"])
        self.assertFalse(
            report["hybrid_palette_indices"]["all_match_cumulative_low_plane_masks"]
        )

    def test_rejects_non_byte_aligned_width(self) -> None:
        with self.assertRaises(TransitionCopyError):
            find_planar_copy_fits([0] * 10, [0] * 10, [0] * 10, 10, 1)

    def test_build_report_rejects_self_hashed_forged_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kwargs = build_kwargs(root)
            for label in FIXED_SOURCE_REGISTRY:
                path = root / f"forged_{label}.txt"
                path.write_text(
                    "WWANIME(4,0) WWANIME(5,0) MOVEVR REP MOVSB PT_MASK_PAT_PUT",
                    encoding="ascii",
                )
                kwargs[f"{label}_path"] = path
                kwargs[f"{label}_sha256"] = file_sha256(path)
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_bound_json",
                side_effect=mocked_documents(FIXED_JOB003_PALETTE_SHA256),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._parse_framehash",
                return_value=({0: "b" * 64}, "c" * 64),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_palette",
                return_value=({}, FIXED_JOB003_PALETTE_SHA256),
            ), self.assertRaisesRegex(TransitionCopyError, "pre-registered fixed-source"):
                build_report(**kwargs)

    def test_build_report_rejects_wrong_fixed_source_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")
            kwargs = build_kwargs(root)
            bound_paths: dict[str, Path] = {}
            for label, registration in FIXED_SOURCE_REGISTRY.items():
                path = root / Path(registration["repo_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="ascii")
                kwargs[f"{label}_path"] = path
                bound_paths[label] = path.resolve()

            def fixed_hash_binding(path: Path, _claimed: str, label: str):
                return bound_paths[label], FIXED_SOURCE_REGISTRY[label]["sha256"]

            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_bound_json",
                side_effect=mocked_documents(FIXED_JOB003_PALETTE_SHA256),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._parse_framehash",
                return_value=({0: "b" * 64}, "c" * 64),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_palette",
                return_value=({}, FIXED_JOB003_PALETTE_SHA256),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._bind_file",
                side_effect=fixed_hash_binding,
            ), self.assertRaisesRegex(TransitionCopyError, "pre-registered commit"):
                build_report(**kwargs)

    def test_build_report_rejects_palette_not_registered_by_offline_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            palette = root / "forged_palette.json"
            palette.write_text(
                json.dumps(
                    {"entries": [[index, index, index] for index in range(16)]},
                    separators=(",", ":"),
                ),
                encoding="ascii",
            )
            kwargs = build_kwargs(root)
            kwargs["palette_path"] = palette
            kwargs["palette_sha256"] = file_sha256(palette)
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_bound_json",
                side_effect=mocked_documents(FIXED_JOB003_PALETTE_SHA256),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._parse_framehash",
                return_value=({0: "b" * 64}, "c" * 64),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_palette",
                return_value=({}, file_sha256(palette)),
            ), self.assertRaisesRegex(TransitionCopyError, "registered by the offline"):
                build_report(**kwargs)

    def test_build_report_rejects_forged_palette_and_forged_manifest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            palette = root / "forged_palette.json"
            palette.write_text(
                json.dumps(
                    {"entries": [[index, index, index] for index in range(16)]},
                    separators=(",", ":"),
                ),
                encoding="ascii",
            )
            forged_sha256 = file_sha256(palette)
            kwargs = build_kwargs(root)
            kwargs["palette_path"] = palette
            kwargs["palette_sha256"] = forged_sha256
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_bound_json",
                side_effect=mocked_documents(forged_sha256),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._parse_framehash",
                return_value=({0: "b" * 64}, "c" * 64),
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy._load_palette",
                return_value=({}, forged_sha256),
            ), self.assertRaisesRegex(TransitionCopyError, "fixed JOB003 calibration"):
                build_report(**kwargs)

    def test_atomic_publish_refuses_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.json"
            output.write_text("keep", encoding="ascii")
            with self.assertRaisesRegex(TransitionCopyError, "already exists"):
                _write_json_atomic(output, {"status": "new"})
            self.assertEqual(output.read_text(encoding="ascii"), "keep")
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_atomic_publish_loses_race_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "racing.json"

            def competing_publish(_source, destination) -> None:
                Path(destination).write_text("competitor", encoding="ascii")
                raise FileExistsError("simulated competing publisher")

            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy.os.link", side_effect=competing_publish
            ), self.assertRaisesRegex(TransitionCopyError, "already exists"):
                _write_json_atomic(output, {"status": "candidate"})
            self.assertEqual(output.read_text(encoding="ascii"), "competitor")
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_atomic_publish_failure_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "failed.json"
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy.os.link",
                side_effect=OSError("simulated publication failure"),
            ), self.assertRaisesRegex(TransitionCopyError, "atomically publish"):
                _write_json_atomic(output, {"status": "candidate"})
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])

    def test_cli_success_writes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy.build_report",
                return_value={"status": "fixture"},
            ):
                self.assertEqual(main(cli_arguments(output)), 0)
            self.assertEqual(json.loads(output.read_text(encoding="ascii")), {"status": "fixture"})

    def test_cli_failures_return_two_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing.json"
            output.write_text("keep", encoding="ascii")
            stderr = io.StringIO()
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy.build_report",
                return_value={"status": "fixture"},
            ), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(cli_arguments(output)), 2)
            self.assertEqual(output.read_text(encoding="ascii"), "keep")
            self.assertIn("output already exists", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

        stderr = io.StringIO()
        with mock.patch(
            "pm2_animation_lab.pm2_activity_transition_copy.build_report",
            side_effect=OSError("simulated read failure"),
        ), contextlib.redirect_stderr(stderr):
            self.assertEqual(main(cli_arguments()), 2)
        self.assertIn("simulated read failure", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_cli_publish_failure_returns_two_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "publish_failed.json"
            stderr = io.StringIO()
            with mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy.build_report",
                return_value={"status": "fixture"},
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_transition_copy.os.link",
                side_effect=OSError("simulated publication failure"),
            ), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(cli_arguments(output)), 2)
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])
            self.assertIn("atomically publish", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
