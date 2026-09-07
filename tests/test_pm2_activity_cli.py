from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm2_animation_lab import pm2_activity_alignment as alignment
from pm2_animation_lab import pm2_activity_capture_receipt as capture_receipt
from pm2_animation_lab import pm2_activity_compositor as compositor
from pm2_animation_lab import pm2_activity_offline_rgb as offline_rgb
from pm2_animation_lab import pm2_activity_runtime_capture as runtime_capture
from pm2_animation_lab import pm2_activity_timeline as timeline
from pm2_animation_lab import pm2_activity_timing as timing
from pm2_animation_lab import pm2_lbx_pt1 as lbx_pt1
SHA = "0" * 64


class Pm2ActivityCliTests(unittest.TestCase):
    def assert_domain_error(
        self,
        module,
        argv: list[str],
        patch_target: str,
        error: Exception,
    ) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(patch_target, side_effect=error), contextlib.redirect_stdout(
            stdout
        ), contextlib.redirect_stderr(stderr):
            result = module.main(argv)
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), "")
        lines = [line for line in stderr.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_all_cli_domain_errors_are_concise_and_return_two(self) -> None:
        self.assert_domain_error(
            capture_receipt,
            [
                "--quarantine-root", ".",
                "--output", "receipt.json",
                "--scene", "JOB003",
                "--archive", "archive.zip",
                "--archive-sha256", SHA,
                "--source-mkv", "capture.mkv",
                "--source-mkv-sha256", SHA,
                "--framehash", "capture.sha256",
                "--framehash-sha256", SHA,
                "--png-directory", "frames",
                "--filename-pattern", "frame_%06d.png",
                "--first-frame", "1",
                "--frame-count", "1",
                "--width", "320",
                "--height", "128",
                "--mode", "RGB",
                "--stream-index", "0",
                "--framehash-hash-algorithm", "sha256",
                "--framehash-pixel-format", "rgb24",
                "--environment-component", "state", "state.bin", SHA,
            ],
            "pm2_animation_lab.pm2_activity_capture_receipt.create_capture_receipt",
            capture_receipt.CaptureReceiptError("fixture"),
        )
        self.assert_domain_error(
            runtime_capture,
            [
                "--png-directory", "frames",
                "--framehash", "capture.sha256",
                "--source-mkv", "capture.mkv",
                "--capture-receipt", "receipt.json",
                "--output-root", "output",
                "--quarantine-root", ".",
                "--scene", "JOB003",
                "--archive-sha256", SHA,
                "--crop", "0", "0", "320", "128",
                "--frame-list", "1",
            ],
            "pm2_animation_lab.pm2_activity_runtime_capture.export_runtime_rgb_frames",
            runtime_capture.RuntimeCaptureError("fixture"),
        )
        self.assert_domain_error(
            alignment,
            [],
            "pm2_animation_lab.pm2_activity_alignment.build_alignment_report",
            alignment.AlignmentError("fixture"),
        )
        self.assert_domain_error(
            timing,
            [
                "--stage-visible-runs", "stage.json",
                "--stage-visible-runs-sha256", SHA,
                "--aggregate", "aggregate.json",
                "--aggregate-sha256", SHA,
                "--runtime-manifest", "runtime.json",
                "--runtime-manifest-sha256", SHA,
                "--evidence-root", ".",
                "--repository-root", ".",
            ],
            "pm2_animation_lab.pm2_activity_timing.build_timing_report",
            timing.TimingError("fixture"),
        )
        self.assert_domain_error(
            timeline,
            ["--source-root", ".", "--scene", "JOB003"],
            "pm2_animation_lab.pm2_activity_timeline.verify_source_checkout",
            timeline.TimelineError("fixture"),
        )
        self.assert_domain_error(
            compositor,
            ["--timeline", "timeline.json", "--quarantine-root", "."],
            "pm2_animation_lab.pm2_activity_compositor._resolve_quarantine_root",
            compositor.CompositionError("fixture"),
        )
        self.assert_domain_error(
            offline_rgb,
            [
                "--composition-manifest", "composition.json",
                "--display-binding", "binding.json",
                "--output-root", "output",
                "--quarantine-root", ".",
            ],
            "pm2_animation_lab.pm2_activity_offline_rgb.generate_offline_rgb_ticks",
            offline_rgb.OfflineRgbError("fixture"),
        )
        self.assert_domain_error(
            lbx_pt1,
            ["--archive", "archive.zip"],
            "pm2_animation_lab.pm2_lbx_pt1.build_activity_pt1_report",
            lbx_pt1.FormatError("fixture"),
        )

    def test_argparse_errors_remain_system_exit_two(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            timing.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("usage:", stderr.getvalue().lower())

    def test_pt1_cli_publishes_once_and_never_overwrites_a_racing_output(self) -> None:
        report = {"report_kind": "synthetic_safe_fixture"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "report.json"
            with mock.patch(
                "pm2_animation_lab.pm2_lbx_pt1.build_activity_pt1_report", return_value=report
            ):
                self.assertEqual(
                    lbx_pt1.main(["--archive", "unused.zip", "--output", str(output)]),
                    0,
                )
                first = output.read_bytes()
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(
                        lbx_pt1.main(
                            ["--archive", "unused.zip", "--output", str(output)]
                        ),
                        2,
                    )
            self.assertEqual(output.read_bytes(), first)
            self.assertIn("output already exists", stderr.getvalue())
            self.assertEqual(list(root.glob(f".{output.name}.*.tmp")), [])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "racing.json"

            def competing_publish(_source, destination) -> None:
                Path(destination).write_text("competitor", encoding="ascii")
                raise FileExistsError("simulated competing publisher")

            stderr = io.StringIO()
            with mock.patch(
                "pm2_animation_lab.pm2_lbx_pt1.build_activity_pt1_report", return_value=report
            ), mock.patch(
                "pm2_animation_lab.pm2_lbx_pt1.os.link", side_effect=competing_publish
            ), contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    lbx_pt1.main(["--archive", "unused.zip", "--output", str(output)]),
                    2,
                )
            self.assertEqual(output.read_text(encoding="ascii"), "competitor")
            self.assertIn("output already exists", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(list(root.glob(f".{output.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
