from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from pm2_animation_lab.pm2_activity_alignment import build_alignment_report
from pm2_animation_lab.pm2_activity_runtime_capture import (
    QuarantineError,
    RuntimeCaptureError,
    export_runtime_rgb_frames,
    sha256_path,
)


ARCHIVE_HASH = "a" * 64
SCENE_ID = "SYNTHETIC_JOB"
FIRST_FRAME = 10
SOURCE_SIZE = (324, 130)
CROP = (2, 1, 320, 128)


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )


def make_image(frame_offset: int) -> Image.Image:
    width, height = SOURCE_SIZE
    image = Image.new("RGB", SOURCE_SIZE)
    image.putdata(
        [
            (x % 256, y % 256, (x + y + frame_offset) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def build_fixture(root: Path, *, pts: tuple[int, ...] = (0, 10_000, 20_000)) -> dict:
    capture = root / "capture"
    frames = capture / "frames"
    frames.mkdir(parents=True)
    source_mkv = capture / "source.mkv"
    source_mkv.write_bytes(b"synthetic lossless MKV identity fixture")

    raw_hashes = []
    for position in range(3):
        image = make_image(position)
        raw_hashes.append(hashlib.sha256(image.tobytes()).hexdigest())
        image.save(frames / f"frame_{FIRST_FRAME + position:06d}.png", format="PNG")

    width, height = SOURCE_SIZE
    framehash = capture / "source.framehash"
    rows = [
        "#format: frame checksums",
        "#version: 2",
        "#hash: SHA256",
        "#software: synthetic fixture",
        "#tb 0: 1/1000000",
        "#media_type 0: video",
        f"#dimensions 0: {width}x{height}",
        "#stream#, dts, pts, duration, size, hash",
    ]
    for position, (frame_pts, digest) in enumerate(zip(pts, raw_hashes, strict=True)):
        rows.append(
            f"0, {position * 10000}, {frame_pts}, 10000, {width * height * 3}, {digest}"
        )
    framehash.write_text("\n".join(rows) + "\n", encoding="ascii")

    receipt = capture / "capture_receipt.json"
    write_json(
        receipt,
        {
            "schema_version": 1,
            "receipt_kind": "pm2_activity_capture_receipt",
            "status": "ready",
            "scene_id": SCENE_ID,
            "archive_sha256": ARCHIVE_HASH,
            "source_mkv_sha256": sha256_path(source_mkv),
            "framehash_sha256": sha256_path(framehash),
            "framehash": {
                "stream_index": 0,
                "hash_algorithm": "sha256",
                "pixel_format": "rgb24",
            },
            "png_sequence": {
                "filename_pattern": "frame_%06d.png",
                "first_frame": FIRST_FRAME,
                "frame_count": 3,
                "width": width,
                "height": height,
                "mode": "RGB",
            },
            "automatic_s2_promotion": False,
            "authorization_effect": "none",
        },
    )
    return {
        "frames": frames,
        "framehash": framehash,
        "source_mkv": source_mkv,
        "receipt": receipt,
    }


def export(root: Path, fixture: dict, output: Path, **kwargs):
    with mock.patch(
        "pm2_animation_lab.pm2_activity_runtime_capture._resolve_quarantine_root",
        return_value=root.resolve(),
    ):
        return export_runtime_rgb_frames(
            fixture["frames"],
            fixture["framehash"],
            fixture["source_mkv"],
            fixture["receipt"],
            output,
            root,
            scene_id=SCENE_ID,
            archive_sha256=ARCHIVE_HASH,
            crop=kwargs.pop("crop", CROP),
            frame_range=kwargs.pop("frame_range", None),
            frame_list=kwargs.pop("frame_list", None),
            **kwargs,
        )


def expect_raises(exception_type, function, contains: str | None = None):
    try:
        function()
    except exception_type as exc:
        if contains is not None:
            assert contains.lower() in str(exc).lower(), str(exc)
        return exc
    raise AssertionError(f"expected {exception_type.__name__}")


def check_explicit_list_export_preserves_column_order_and_hashes() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "runtime_rgb"
        result = export(
            root,
            fixture,
            output,
            frame_list=[FIRST_FRAME, FIRST_FRAME + 2],
        )
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="ascii"))

        assert result["status"] == "ready_written"
        assert result["selected_frame_numbers"] == [FIRST_FRAME, FIRST_FRAME + 2]
        assert manifest["manifest_kind"] == "pm2_activity_runtime_rgb_frames"
        assert manifest["status"] == "ready"
        assert manifest["scene_id"] == SCENE_ID
        assert manifest["archive_sha256"] == ARCHIVE_HASH
        assert manifest["selection"] == {
            "kind": "explicit_list",
            "frame_list": [FIRST_FRAME, FIRST_FRAME + 2],
        }
        assert manifest["native_raster"] == {"width": 320, "height": 128, "mode": "RGB"}
        assert manifest["semantic_interpretation"] is None
        assert manifest["automatic_s2_promotion"] is False
        assert manifest["authorization_effect"] == "none"
        assert [item["timestamp_us"] for item in manifest["frames"]] == [0, 20_000]
        assert [item["duration_us"] for item in manifest["frames"]] == [10_000, 10_000]

        first_output = output / f"frame_{FIRST_FRAME:06d}.png"
        with Image.open(first_output) as cropped, Image.open(
            fixture["frames"] / f"frame_{FIRST_FRAME:06d}.png"
        ) as source:
            assert cropped.mode == "RGB"
            assert cropped.size == (320, 128)
            # Both endpoints and one cross-row sample prove that the crop kept
            # the source's row/column ordering rather than transposing bytes.
            assert cropped.getpixel((0, 0)) == source.getpixel((2, 1))
            assert cropped.getpixel((319, 0)) == source.getpixel((321, 1))
            assert cropped.getpixel((17, 73)) == source.getpixel((19, 74))
            assert cropped.getpixel((319, 127)) == source.getpixel((321, 128))
            rgb_hash = hashlib.sha256(cropped.convert("RGB").tobytes()).hexdigest()
        entry = manifest["frames"][0]
        assert entry["png_sha256"] == sha256_path(first_output)
        assert entry["rgb_sha256"] == rgb_hash
        assert entry["source_png_sha256"] == sha256_path(
            fixture["frames"] / f"frame_{FIRST_FRAME:06d}.png"
        )

        offline_manifest = {
            "schema_version": 1,
            "manifest_kind": "pm2_activity_offline_rgb_ticks",
            "status": "ready",
            "scene_id": SCENE_ID,
            "timeline_sha256": "1" * 64,
            "pt1_report_sha256": "2" * 64,
            "binding_manifest_sha256": "3" * 64,
            "composition_manifest_sha256": "4" * 64,
            "ticks": [
                {
                    "tick": position,
                    "png_path": frame["png_path"],
                    "png_sha256": frame["png_sha256"],
                    "duration_us": frame["duration_us"],
                }
                for position, frame in enumerate(manifest["frames"])
            ],
        }
        offline_path = output / "offline_manifest.json"
        write_json(offline_path, offline_manifest)
        alignment = build_alignment_report(manifest_path, offline_path)
        assert alignment["status"] == "machine_exact"


def check_framehash_order_is_rejected_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root, pts=(0, 20_000, 10_000))
        output = root / "must_not_exist"
        expect_raises(
            RuntimeCaptureError,
            lambda: export(root, fixture, output, frame_range=[FIRST_FRAME, FIRST_FRAME + 2]),
            "strictly increasing",
        )
        assert not output.exists()


def check_explicit_inclusive_range_export() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "runtime_rgb_range"
        export(
            root,
            fixture,
            output,
            frame_range=[FIRST_FRAME + 1, FIRST_FRAME + 2],
        )
        manifest = json.loads((output / "manifest.json").read_text(encoding="ascii"))
        assert manifest["selection"] == {
            "kind": "explicit_inclusive_range",
            "frame_range": [FIRST_FRAME + 1, FIRST_FRAME + 2],
        }
        assert [item["frame"] for item in manifest["frames"]] == [
            FIRST_FRAME + 1,
            FIRST_FRAME + 2,
        ]
        assert all((output / item["png_path"]).is_file() for item in manifest["frames"])


def check_out_of_bounds_crop_is_rejected_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "must_not_exist"
        expect_raises(
            RuntimeCaptureError,
            lambda: export(
                root,
                fixture,
                output,
                crop=(5, 1, 320, 128),
                frame_range=[FIRST_FRAME, FIRST_FRAME],
            ),
            "outside",
        )
        assert not output.exists()


def check_framehash_rgb_mismatch_is_rejected_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        make_image(99).save(
            fixture["frames"] / f"frame_{FIRST_FRAME + 1:06d}.png",
            format="PNG",
        )
        output = root / "must_not_exist"
        expect_raises(
            RuntimeCaptureError,
            lambda: export(root, fixture, output, frame_list=[FIRST_FRAME]),
            "rgb sha-256 mismatch",
        )
        assert not output.exists()


def check_noncontinuous_source_sequence_is_rejected_without_output() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        middle = fixture["frames"] / f"frame_{FIRST_FRAME + 1:06d}.png"
        middle.rename(fixture["frames"] / f"frame_{FIRST_FRAME + 3:06d}.png")
        output = root / "must_not_exist"
        expect_raises(
            RuntimeCaptureError,
            lambda: export(root, fixture, output, frame_range=[FIRST_FRAME, FIRST_FRAME + 2]),
            "complete and continuous",
        )
        assert not output.exists()


def check_missing_and_escape_inputs_fail_before_output() -> None:
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        outside_mkv = Path(outside).resolve() / "outside.mkv"
        outside_mkv.write_bytes(fixture["source_mkv"].read_bytes())
        escaped = dict(fixture)
        escaped["source_mkv"] = outside_mkv
        output = root / "must_not_exist"
        expect_raises(
            QuarantineError,
            lambda: export(root, escaped, output, frame_list=[FIRST_FRAME]),
            "escapes",
        )
        assert not output.exists()

        fixture["receipt"].unlink()
        expect_raises(
            QuarantineError,
            lambda: export(root, fixture, output, frame_list=[FIRST_FRAME]),
            "unavailable",
        )
        assert not output.exists()


class Pm2ActivityRuntimeCaptureTests(unittest.TestCase):
    def test_explicit_list_export_preserves_column_order_and_hashes(self) -> None:
        check_explicit_list_export_preserves_column_order_and_hashes()

    def test_framehash_order_is_rejected_without_output(self) -> None:
        check_framehash_order_is_rejected_without_output()

    def test_explicit_inclusive_range_export(self) -> None:
        check_explicit_inclusive_range_export()

    def test_out_of_bounds_crop_is_rejected_without_output(self) -> None:
        check_out_of_bounds_crop_is_rejected_without_output()

    def test_framehash_rgb_mismatch_is_rejected_without_output(self) -> None:
        check_framehash_rgb_mismatch_is_rejected_without_output()

    def test_noncontinuous_source_sequence_is_rejected_without_output(self) -> None:
        check_noncontinuous_source_sequence_is_rejected_without_output()

    def test_missing_and_escape_inputs_fail_before_output(self) -> None:
        check_missing_and_escape_inputs_fail_before_output()


def run() -> None:
    check_explicit_list_export_preserves_column_order_and_hashes()
    check_framehash_order_is_rejected_without_output()
    check_explicit_inclusive_range_export()
    check_out_of_bounds_crop_is_rejected_without_output()
    check_framehash_rgb_mismatch_is_rejected_without_output()
    check_noncontinuous_source_sequence_is_rejected_without_output()
    check_missing_and_escape_inputs_fail_before_output()
    print(
        "PM2 runtime capture bridge tests passed: complete RGB24 sequence/framehash binding, "
        "explicit native crop/selection, timestamp/hash manifest, and fail-closed quarantine guards."
    )


if __name__ == "__main__":
    run()
