from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from pm2_animation_lab.pm2_activity_capture_receipt import (
    CaptureReceiptError,
    QuarantineError,
    create_capture_receipt,
    sha256_path,
)
from pm2_animation_lab.pm2_activity_runtime_capture import export_runtime_rgb_frames


ARCHIVE_HASH_VALUE = "fixed archive fixture"
SCENE_ID = "SYNTHETIC_JOB"
FIRST_FRAME = 1
SOURCE_SIZE = (640, 480)


def make_source_image(index: int) -> Image.Image:
    image = Image.new(
        "RGB", SOURCE_SIZE, ((17 + index) % 256, (31 + index) % 256, (47 + index) % 256)
    )
    image.putpixel((index, index), (201, 202, 203))
    return image


def build_fixture(root: Path, ffmpeg_name: str = "ffmpeg.exe") -> dict:
    capture = root / "capture"
    frames = capture / "frames"
    environment = root / "environment"
    capture.mkdir()
    frames.mkdir()
    environment.mkdir()

    archive = root / "fixed_archive.zip"
    archive.write_text(ARCHIVE_HASH_VALUE, encoding="ascii")
    source_mkv = capture / "source.mkv"
    source_mkv.write_bytes(b"synthetic RGB lossless capture")

    frame_hashes = []
    for index in range(3):
        image = make_source_image(index)
        frame_hashes.append(hashlib.sha256(image.tobytes()).hexdigest())
        image.save(frames / f"frame_{FIRST_FRAME + index:06d}.png", format="PNG")
    width, height = SOURCE_SIZE
    framehash = capture / "source.framehash"
    lines = [
        "#format: frame checksums",
        "#version: 2",
        "#hash: SHA256",
        "#tb 0: 1/1000000",
        "#media_type 0: video",
        f"#dimensions 0: {width}x{height}",
        "#stream#, dts, pts, duration, size, hash",
    ]
    for index, digest in enumerate(frame_hashes):
        lines.append(
            f"0, {index * 10000}, {index * 10000}, 10000, {width * height * 3}, {digest}"
        )
    framehash.write_text("\n".join(lines) + "\n", encoding="ascii")

    component_paths = {
        "recording_config": environment / "record.cfg",
        "dosbox_pure_core": environment / "dosbox_pure_libretro.dll",
        "retroarch_executable": environment / "retroarch.exe",
    }
    for role, path in component_paths.items():
        path.write_text(f"synthetic {role}", encoding="ascii")
    components = [
        {"role": role, "path": path, "sha256": sha256_path(path)}
        for role, path in component_paths.items()
    ]

    ffmpeg_root = root / "ffmpeg"
    ffmpeg_root.mkdir()
    ffmpeg_executable = ffmpeg_root / ffmpeg_name
    ffmpeg_executable.write_bytes(b"synthetic ffmpeg executable fixture")
    ffmpeg_version = ffmpeg_root / "ffmpeg-version.txt"
    ffmpeg_version.write_text(
        "ffmpeg version synthetic-fixture\nconfiguration: test-only\n", encoding="ascii"
    )
    framehash_log = capture / "framehash-command.log"
    framehash_log.write_text("synthetic framehash execution completed\n", encoding="ascii")
    png_log = capture / "png-command.log"
    png_log.write_text("synthetic PNG execution completed\n", encoding="ascii")

    framehash_argv = [
        str(ffmpeg_executable),
        "-nostdin",
        "-n",
        "-i",
        str(source_mkv),
        "-map",
        "0:v:0",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-f",
        "framehash",
        "-hash",
        "sha256",
        str(framehash),
    ]
    png_argv = [
        str(ffmpeg_executable),
        "-nostdin",
        "-n",
        "-i",
        str(source_mkv),
        "-map",
        "0:v:0",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb24",
        "-start_number",
        str(FIRST_FRAME),
        str(frames / "frame_%06d.png"),
    ]
    return {
        "archive": archive,
        "source_mkv": source_mkv,
        "framehash": framehash,
        "frames": frames,
        "components": components,
        "ffmpeg_executable": ffmpeg_executable,
        "ffmpeg_version": ffmpeg_version,
        "framehash_log": framehash_log,
        "png_log": png_log,
        "framehash_argv": framehash_argv,
        "png_argv": png_argv,
    }


def create(root: Path, fixture: dict, output: Path, **overrides):
    values = {
        "scene_id": SCENE_ID,
        "archive_sha256": sha256_path(fixture["archive"]),
        "source_mkv_sha256": sha256_path(fixture["source_mkv"]),
        "framehash_sha256": sha256_path(fixture["framehash"]),
        "filename_pattern": "frame_%06d.png",
        "first_frame": FIRST_FRAME,
        "frame_count": 3,
        "width": SOURCE_SIZE[0],
        "height": SOURCE_SIZE[1],
        "mode": "RGB",
        "stream_index": 0,
        "framehash_hash_algorithm": "sha256",
        "framehash_pixel_format": "rgb24",
        "environment_components": fixture["components"],
        "ffmpeg_executable_path": fixture["ffmpeg_executable"],
        "ffmpeg_executable_sha256": sha256_path(fixture["ffmpeg_executable"]),
        "ffmpeg_version_path": fixture["ffmpeg_version"],
        "ffmpeg_version_sha256": sha256_path(fixture["ffmpeg_version"]),
        "framehash_argv": fixture["framehash_argv"],
        "framehash_command_log_path": fixture["framehash_log"],
        "framehash_command_log_sha256": sha256_path(fixture["framehash_log"]),
        "png_argv": fixture["png_argv"],
        "png_command_log_path": fixture["png_log"],
        "png_command_log_sha256": sha256_path(fixture["png_log"]),
    }
    values.update(overrides)
    with mock.patch(
        "pm2_animation_lab.pm2_activity_capture_receipt._resolve_quarantine_root",
        return_value=root.resolve(),
    ):
        return create_capture_receipt(
            fixture["archive"],
            fixture["source_mkv"],
            fixture["framehash"],
            fixture["frames"],
            output,
            root,
            **values,
        )


def expect_raises(exception_type, function, contains: str | None = None):
    try:
        function()
    except exception_type as exc:
        if contains is not None:
            assert contains.lower() in str(exc).lower(), str(exc)
        return exc
    raise AssertionError(f"expected {exception_type.__name__}")


def check_safe_receipt_and_runtime_bridge_compatibility() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "capture_receipt.json"
        result = create(root, fixture, output)
        receipt = json.loads(output.read_text(encoding="ascii"))

        assert result["status"] == "ready_written"
        assert result["output_sha256"] == sha256_path(output)
        assert receipt["receipt_kind"] == "pm2_activity_capture_receipt"
        assert receipt["status"] == "ready"
        assert receipt["scene_id"] == SCENE_ID
        assert receipt["archive_sha256"] == sha256_path(fixture["archive"])
        assert receipt["archive"]["path"] == "fixed_archive.zip"
        assert receipt["source_mkv"]["path"] == "capture/source.mkv"
        assert receipt["framehash"]["path"] == "capture/source.framehash"
        assert receipt["png_sequence"]["directory"] == "capture/frames"
        provenance = receipt["ffmpeg_provenance"]
        assert provenance["executable"]["path"] == "ffmpeg/ffmpeg.exe"
        assert provenance["executable"]["sha256"] == sha256_path(
            fixture["ffmpeg_executable"]
        )
        assert provenance["version_text"]["path"] == "ffmpeg/ffmpeg-version.txt"
        assert provenance["commands"]["framehash"]["argv"] == fixture["framehash_argv"]
        assert provenance["commands"]["png_extraction"]["argv"] == fixture["png_argv"]
        assert isinstance(provenance["commands"]["framehash"]["argv"], list)
        assert provenance["commands"]["framehash"]["execution_log"]["path"] == (
            "capture/framehash-command.log"
        )
        assert provenance["commands"]["png_extraction"]["execution_log"]["path"] == (
            "capture/png-command.log"
        )
        assert [item["role"] for item in receipt["environment_components"]] == [
            "dosbox_pure_core",
            "recording_config",
            "retroarch_executable",
        ]
        assert all(set(item) == {"role", "path", "sha256", "size_bytes"} for item in receipt["environment_components"])
        assert receipt["semantic_interpretation"] is None
        assert receipt["automatic_s2_promotion"] is False
        assert receipt["authorization_effect"] == "none"
        encoded = output.read_text(encoding="ascii")
        assert ARCHIVE_HASH_VALUE not in encoded
        assert "configuration: test-only" not in encoded
        assert '"contains_original_payload": false' in encoded

        runtime_output = root / "capture" / "runtime_rgb"
        with mock.patch(
            "pm2_animation_lab.pm2_activity_runtime_capture._resolve_quarantine_root",
            return_value=root.resolve(),
        ):
            bridge = export_runtime_rgb_frames(
                fixture["frames"],
                fixture["framehash"],
                fixture["source_mkv"],
                output,
                runtime_output,
                root,
                scene_id=SCENE_ID,
                archive_sha256=sha256_path(fixture["archive"]),
                crop=(2, 1, 320, 128),
                frame_list=[FIRST_FRAME, FIRST_FRAME + 2],
            )
        assert bridge["status"] == "ready_written"


def check_hash_mismatch_and_duplicate_role_write_nothing() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, source_mkv_sha256="0" * 64),
            "sha-256 mismatch",
        )
        assert not output.exists()

        duplicates = [dict(fixture["components"][0]), dict(fixture["components"][0])]
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, environment_components=duplicates),
            "not unique",
        )
        assert not output.exists()


def check_environment_hash_and_path_escape_write_nothing() -> None:
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"
        changed = [dict(item) for item in fixture["components"]]
        changed[0]["sha256"] = "f" * 64
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, environment_components=changed),
            "sha-256 mismatch",
        )
        assert not output.exists()

        outside_path = Path(outside).resolve() / "retroarch.exe"
        outside_path.write_bytes(b"outside")
        escaped = [
            {
                "role": "outside_component",
                "path": outside_path,
                "sha256": sha256_path(outside_path),
            }
        ]
        expect_raises(
            QuarantineError,
            lambda: create(root, fixture, output, environment_components=escaped),
            "escapes",
        )
        assert not output.exists()


class PortableFFmpegReceiptTest(unittest.TestCase):
    def test_unix_name_retains_hash_and_command_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = build_fixture(root, ffmpeg_name="ffmpeg")
            output = root / "capture" / "receipt.json"
            with self.assertRaisesRegex(CaptureReceiptError, "SHA-256 mismatch"):
                create(root, fixture, output, ffmpeg_executable_sha256="0" * 64)
            self.assertFalse(output.exists())
            changed_argv = [str(root / "different"), *fixture["png_argv"][1:]]
            with self.assertRaises(CaptureReceiptError):
                create(root, fixture, output, png_argv=changed_argv)
            self.assertFalse(output.exists())
            self.assertEqual(create(root, fixture, output)["status"], "ready_written")
            receipt = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(receipt["ffmpeg_provenance"]["executable"]["path"], "ffmpeg/ffmpeg")


def check_ffmpeg_provenance_is_mandatory_and_hash_bound() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, ffmpeg_version_path=None),
            "path is required",
        )
        assert not output.exists()

        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, framehash_argv=None),
            "non-empty array",
        )
        assert not output.exists()

        expect_raises(
            CaptureReceiptError,
            lambda: create(
                root, fixture, output, ffmpeg_executable_sha256="0" * 64
            ),
            "sha-256 mismatch",
        )
        assert not output.exists()

        expect_raises(
            CaptureReceiptError,
            lambda: create(
                root, fixture, output, png_command_log_sha256="f" * 64
            ),
            "sha-256 mismatch",
        )
        assert not output.exists()


def check_ffmpeg_provenance_paths_and_argv_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"
        outside_log = Path(outside).resolve() / "png-command.log"
        outside_log.write_text("synthetic outside execution log\n", encoding="ascii")
        expect_raises(
            QuarantineError,
            lambda: create(
                root,
                fixture,
                output,
                png_command_log_path=outside_log,
                png_command_log_sha256=sha256_path(outside_log),
            ),
            "escapes",
        )
        assert not output.exists()

        expect_raises(
            CaptureReceiptError,
            lambda: create(
                root,
                fixture,
                output,
                framehash_argv="ffmpeg -i source.mkv output.framehash",
            ),
            "array of strings",
        )
        assert not output.exists()

        escaped_argv = list(fixture["png_argv"])
        outside_pattern = Path(outside).resolve() / "frame_%06d.png"
        escaped_argv[-1] = str(outside_pattern)
        expect_raises(
            QuarantineError,
            lambda: create(root, fixture, output, png_argv=escaped_argv),
            "escapes",
        )
        assert not output.exists()


def check_ffmpeg_command_contract_rejects_resampling() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"

        missing_nostdin = list(fixture["framehash_argv"])
        missing_nostdin.remove("-nostdin")
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, framehash_argv=missing_nostdin),
            "-nostdin",
        )
        assert not output.exists()

        filtered = list(fixture["png_argv"])
        filtered[-1:-1] = ["-vf", "scale=320:240"]
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, png_argv=filtered),
            "video filters",
        )
        assert not output.exists()

        retimed = list(fixture["png_argv"])
        retimed[-1:-1] = ["-r", "30"]
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, png_argv=retimed),
            "frame rate",
        )
        assert not output.exists()

def check_png_sequence_contract_is_enforced() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"
        (fixture["frames"] / f"frame_{FIRST_FRAME + 1:06d}.png").unlink()
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output),
            "complete and continuous",
        )
        assert not output.exists()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "must_not_exist.json"
        expect_raises(
            CaptureReceiptError,
            lambda: create(root, fixture, output, width=SOURCE_SIZE[0] - 1),
            "raw capture",
        )
        assert not output.exists()


def check_existing_output_is_never_overwritten() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root)
        output = root / "capture" / "existing.json"
        output.write_text("keep", encoding="ascii")
        expect_raises(
            QuarantineError,
            lambda: create(root, fixture, output),
            "already exists",
        )
        assert output.read_text(encoding="ascii") == "keep"


class Pm2ActivityCaptureReceiptTests(unittest.TestCase):
    def test_safe_receipt_and_runtime_bridge_compatibility(self) -> None:
        check_safe_receipt_and_runtime_bridge_compatibility()

    def test_hash_mismatch_and_duplicate_role_write_nothing(self) -> None:
        check_hash_mismatch_and_duplicate_role_write_nothing()

    def test_environment_hash_and_path_escape_write_nothing(self) -> None:
        check_environment_hash_and_path_escape_write_nothing()

    def test_ffmpeg_provenance_is_mandatory_and_hash_bound(self) -> None:
        check_ffmpeg_provenance_is_mandatory_and_hash_bound()

    def test_ffmpeg_provenance_paths_and_argv_fail_closed(self) -> None:
        check_ffmpeg_provenance_paths_and_argv_fail_closed()

    def test_ffmpeg_command_contract_rejects_resampling(self) -> None:
        check_ffmpeg_command_contract_rejects_resampling()

    def test_png_sequence_contract_is_enforced(self) -> None:
        check_png_sequence_contract_is_enforced()

    def test_existing_output_is_never_overwritten(self) -> None:
        check_existing_output_is_never_overwritten()


def run() -> None:
    check_safe_receipt_and_runtime_bridge_compatibility()
    check_hash_mismatch_and_duplicate_role_write_nothing()
    check_environment_hash_and_path_escape_write_nothing()
    check_ffmpeg_provenance_is_mandatory_and_hash_bound()
    check_ffmpeg_provenance_paths_and_argv_fail_closed()
    check_ffmpeg_command_contract_rejects_resampling()
    check_png_sequence_contract_is_enforced()
    check_existing_output_is_never_overwritten()
    print(
        "PM2 capture receipt tests passed: quarantine-only path/hash bindings, unique environment "
        "roles, FFmpeg executable/version/argv/log provenance, raw 640x480 command contract, "
        "complete PNG contract, atomic no-overwrite output, and runtime bridge compatibility."
    )


if __name__ == "__main__":
    run()
