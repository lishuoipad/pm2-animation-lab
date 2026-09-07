import unittest
import hashlib
from unittest.mock import patch
from PIL import Image
from pm2_animation_lab.pm2_activity_sequence_finish import synthesize_copy, rgb_to_indices, verify_decoded_frames, main
from pm2_animation_lab.pm2_activity_timeline import TimelineError
from pm2_animation_lab.pm2_activity_transition_copy import PlanarFit, find_planar_copy_fits
from pm2_animation_lab.pm2_activity_video_index import VideoIndexError


class SequenceFinishTests(unittest.TestCase):
    def test_original_synthetic_planar_state(self):
        old, new = bytes([0] * 48), bytes([15] * 48)
        middle = synthesize_copy(old, new, 16, PlanarFit(1, 1, 8))
        self.assertEqual(middle, bytes([15]*16 + [3]*8 + [1]*8 + [0]*16))
        self.assertIn(PlanarFit(1, 1, 8), find_planar_copy_fits(old, middle, new, 16, 3))

    def test_rgb_indices_do_not_quantize(self):
        palette = [[i, i, i] for i in range(16)]
        image = Image.new("RGB", (2, 1), (3, 3, 3))
        self.assertEqual(rgb_to_indices(image, palette), bytes([3, 3]))
        with self.assertRaises(VideoIndexError):
            rgb_to_indices(Image.new("RGB", (1, 1), (3, 3, 4)), palette)

    def test_ambiguous_palette_rejected(self):
        with self.assertRaises(VideoIndexError):
            rgb_to_indices(Image.new("RGB", (1, 1)), [[0, 0, 0]]*16)

    def test_palette_mode_not_silently_converted(self):
        with self.assertRaises(VideoIndexError):
            rgb_to_indices(Image.new("P", (1, 1)), [[i,i,i] for i in range(16)])

    def test_encoded_roundtrip_byte_exact(self):
        raw = bytes([1, 2, 3, 4, 5, 6])
        rows = [{"rgb_sha256": hashlib.sha256(raw[i:i+3]).hexdigest()} for i in (0,3)]
        self.assertEqual(verify_decoded_frames(raw, rows, 3), hashlib.sha256(raw).hexdigest())
        with self.assertRaisesRegex(VideoIndexError, "frame_count"):
            verify_decoded_frames(raw[:-1], rows, 3)
        with self.assertRaisesRegex(VideoIndexError, "rgb_mismatch"):
            verify_decoded_frames(bytes([0])+raw[1:], rows, 3)

    def test_domain_error_is_concise_failure(self):
        names = ("quarantine-root", "source-root", "sequence-check", "sequence-check-sha256",
                 "output-directory", "ffmpeg", "ffmpeg-sha256")
        argv = [part for name in names for part in ("--"+name, "synthetic")]
        with patch("pm2_animation_lab.pm2_activity_sequence_finish.run", side_effect=TimelineError("synthetic_invalid_source")):
            self.assertEqual(main(argv), 2)


if __name__ == "__main__":
    unittest.main()
