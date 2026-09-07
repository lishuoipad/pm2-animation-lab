import io
import unittest

from pm2_animation_lab.pm2_activity_video_index import (
    VideoIndexError, append_run, read_frame, validate_geometry, verify_framehash,
)


class VideoIndexTests(unittest.TestCase):
    def test_complete_and_partial_frames(self):
        stream = io.BytesIO(b"1234567")
        self.assertEqual(read_frame(stream, 3), b"123")
        self.assertEqual(read_frame(stream, 3), b"456")
        with self.assertRaises(VideoIndexError):
            read_frame(stream, 3)
        self.assertIsNone(read_frame(stream, 3))

    def test_geometry_fails_closed(self):
        validate_geometry(640, 480, [32, 240, 320, 128])
        for crop in ([32, 240, 640, 128], [-1, 0, 1, 1], [0, 0, 0, 1], [True, 0, 1, 1]):
            with self.assertRaises(VideoIndexError):
                validate_geometry(640, 480, crop)

    def test_exact_framehash_and_timing(self):
        text = "#tb 0: 1/60\n0, 0, 0, 1, 3, aaa\n0, 2, 2, 1, 3, bbb\n"
        rows, tb = verify_framehash(text, ["aaa", "bbb"], 3)
        self.assertEqual(tb, [1, 60])
        self.assertEqual(rows[1]["pts"], 2)  # Preserve a gap; never invent frames.
        for bad in (text.replace("bbb", "ccc"), text.replace("1, 3, aaa", "1, 4, aaa"),
                    text.replace("0, 2, 2", "0, 2, 0"), text.splitlines()[1]):
            with self.assertRaises(VideoIndexError):
                verify_framehash(bad, ["aaa", "bbb"], 3)

    def test_only_adjacent_identical_crops_merge(self):
        runs = []
        for i, digest in enumerate(["a", "a", "b", "a"]):
            append_run(runs, i, digest)
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[0]["last_frame"], 1)
        self.assertEqual(runs[2]["first_frame"], 3)


if __name__ == "__main__":
    unittest.main()
