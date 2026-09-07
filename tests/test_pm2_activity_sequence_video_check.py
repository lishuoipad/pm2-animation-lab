import contextlib
import io
import unittest
from unittest import mock

from pm2_animation_lab.pm2_activity_sequence_video_check import ordered_exact_matches, main, render_sequence
from pm2_animation_lab.pm2_activity_video_index import VideoIndexError


class SequenceVideoTests(unittest.TestCase):
    def runs(self, values):
        return [{"rgb_sha256": v, "first_frame": i * 10, "last_frame": i * 10 + 9,
                 "first_seconds": i, "last_seconds": i + .9} for i, v in enumerate(values)]

    def test_order_not_just_membership(self):
        rows = ordered_exact_matches(["b", "a"], self.runs(["a", "b"]))
        self.assertEqual([r["matched"] for r in rows], [True, False])

    def test_identical_ticks_explicitly_indistinguishable(self):
        rows = ordered_exact_matches(["a", "a", "b"], self.runs(["a", "b"]))
        self.assertTrue(all(r["matched"] for r in rows))
        self.assertTrue(rows[1]["shares_previous_run"])
        self.assertFalse(rows[2]["shares_previous_run"])

    def test_never_accept_near_matches(self):
        rows = ordered_exact_matches(["abc"], self.runs(["abd"]))
        self.assertFalse(rows[0]["matched"])

    def test_empty_runs_do_not_pass(self):
        self.assertFalse(ordered_exact_matches(["a"], [])[0]["matched"])

    def test_palette_not_guessed_or_clamped(self):
        with self.assertRaises(VideoIndexError):
            render_sequence({}, None, [[0, 0, 999]] * 16)

    def test_structural_errors_are_concise(self):
        args = ["--quarantine-root", "D:/fixture", "--source-root", "D:/fixture",
                "--scene", "JOB008", "--output-directory", "D:/fixture/output",
                "--start-seconds", "0", "--end-seconds", "1"]
        for name in ("spec", "bindings", "base-timeline", "palette", "video-index"):
            args += ["--" + name, "D:/fixture/" + name, "--" + name + "-sha256", "0" * 64]
        with mock.patch("pm2_animation_lab.pm2_activity_sequence_video_check.run", side_effect=KeyError("frames")):
            with contextlib.redirect_stderr(io.StringIO()) as captured:
                self.assertEqual(main(args), 2)
        self.assertNotIn("Traceback", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
