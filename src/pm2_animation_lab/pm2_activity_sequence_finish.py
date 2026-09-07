"""Classify native inter-tick copy states and reconstruct a bounded interval.

Consumes an exact sequence check, not a user assertion of success. Copy seam
parameters and exposure lengths are fitted observations, NOT independently
recovered CPU timing or Replay. Original visuals stay in the external-data quarantine.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image

from pm2_animation_lab.pm2_activity_video_index import VideoIndexError, confined, file_hash, write_json
from pm2_animation_lab.pm2_activity_sequence_video_check import load_checked, verify_index_provenance
from pm2_animation_lab.pm2_activity_transition_copy import (
    Raster, classify_transition, find_planar_copy_fits, _source_lines,
)
from pm2_animation_lab.pm2_activity_timeline import FIXED_SOURCE_COMMIT, verify_source_checkout, TimelineError


def synthesize_copy(previous, following, width, fit):
    """Original implementation of the tested row/plane copy-state model."""
    result = bytearray(len(previous))
    for i, (old, new) in enumerate(zip(previous, following)):
        y, x = divmod(i, width)
        if y < fit.seam_row:
            mask = 15
        elif y > fit.seam_row:
            mask = 0
        else:
            mask = fit.left_mask if x < fit.boundary_x else fit.right_mask
        result[i] = (old & (15 ^ mask)) | (new & mask)
    return bytes(result)


def rgb_to_indices(image, palette):
    if image.mode != "RGB":
        raise VideoIndexError("native_rgb_required")
    lookup = {tuple(color): i for i, color in enumerate(palette)}
    if len(lookup) != 16:
        raise VideoIndexError("palette_must_be_unique_rgb16")
    try:
        return bytes(lookup[color] for color in struct.iter_unpack("BBB", image.tobytes()))
    except KeyError as exc:
        raise VideoIndexError("observed_rgb_not_in_bound_palette") from exc


def verify_decoded_frames(raw, frame_hashes, frame_size):
    """Verify delivery bytes after codec round trip, not just encoder input."""
    if frame_size <= 0 or len(raw) != len(frame_hashes) * frame_size:
        raise VideoIndexError("preview_decoded_frame_count")
    for i, row in enumerate(frame_hashes):
        frame = raw[i * frame_size:(i + 1) * frame_size]
        if hashlib.sha256(frame).hexdigest() != row["rgb_sha256"]:
            raise VideoIndexError(f"preview_decoded_rgb_mismatch:{i}")
    return hashlib.sha256(raw).hexdigest()


def source_chain(root, scene):
    verify_source_checkout(root, FIXED_SOURCE_COMMIT, scene)
    sources = {
        f"KOSOTEXT/{scene}.TXT": [b"*ANMT001", b"WWANIME(4,0)", b"TIMER1(TIMEWAIT1)",
                                  b"APUT(", b"WAIT1", b"WWANIME(5,0)", b"LOOP TIMELOP"],
        "KOSO4/WIDANIME.ASM": [b"V2TOCRTCOPY\tPROC", b"ASSIGN_SSSOU_VIRTUAL",
                                b"ASSIGN_SSDES_VRAM", b"CALL_w\tMOVEVR"],
        "KOSO2/VRMOVE.ASM": [b"MOVEVR\tPROC", b"MOV\tBP,VY2", b"MOVEVR1:",
                             b"PUSH\tSSSOU3", b"PUSH\tSSSOU2", b"PUSH\tSSSOU1", b"PUSH\tSSSOU0",
                             b"REP\tMOVSB", b"REP\tMOVSB", b"REP\tMOVSB", b"REP\tMOVSB",
                             b"DEC\tBP", b"JNZ\tMOVEVR1"],
    }
    result = []
    for relative, tokens in sources.items():
        subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "diff", "--exit-code", FIXED_SOURCE_COMMIT,
                        "--", relative], check=True, capture_output=True)
        path = root / relative
        result.append({"path": str(path), "sha256": file_hash(path),
                       "ordered_token_lines": _source_lines(path, tokens, relative)})
    return result


def run(args):
    root = Path(args.quarantine_root).resolve()
    source_root = Path(args.source_root).resolve()
    if not is_data_directory(root) or not source_root.is_dir():
        raise VideoIndexError("existing_data_and_source_directories_required")
    check_path = confined(args.sequence_check, root)
    check = load_checked(check_path, args.sequence_check_sha256)
    if check.get("schema_version") != "pm2_activity_sequence_video_check/v1":
        raise VideoIndexError("sequence_check_schema")
    matches = check["matches"]
    if not matches or not all(row["matched"] for row in matches):
        raise VideoIndexError("all_stable_ticks_must_match")
    sequence_path = check_path.parent / "sequence.json"
    load_checked(sequence_path, check["sequence_sha256"])
    inputs = {name: load_checked(confined(b["path"], root), b["sha256"])
              for name, b in check["inputs"].items()}
    index = inputs["video_index"]
    if index["capture_encoding"] != "lossless_rgb":
        raise VideoIndexError("lossless_rgb_required")
    index_path = confined(check["inputs"]["video_index"]["path"], root)
    verify_index_provenance(index, index_path, root)
    sources = source_chain(source_root, check["scene"])
    if sources[0]["sha256"] != check["source_sha256"]:
        raise VideoIndexError("source_changed")
    width, height = index["crop"][2:]
    palette = inputs["palette"]["entries"]
    begin, end = matches[0]["first_frame"], matches[-1]["last_frame"]
    runs = [r for r in index["runs"] if r["first_frame"] >= begin and r["last_frame"] <= end]
    stable, expected = {}, {}
    for row in matches:
        with Image.open(check_path.parent / f"tick_{row['tick']:04d}.png") as image:
            if image.mode != "RGB" or image.size != (width, height):
                raise VideoIndexError("offline_native_geometry")
            raw = image.tobytes()
            if hashlib.sha256(raw).hexdigest() != row["rgb_sha256"]:
                raise VideoIndexError("offline_tick_changed")
            indices = rgb_to_indices(image, palette)
        stable[row["first_frame"]] = (raw, indices)
        expected[row["first_frame"]] = row["last_frame"]
    reconstructed, classifications = [], []
    for i, current in enumerate(runs):
        binding = index["unique_images"][current["rgb_sha256"]]
        png = confined(index_path.parent / binding["path"], root)
        if file_hash(png) != binding["sha256"]:
            raise VideoIndexError("observed_png_changed")
        with Image.open(png) as observed:
            if observed.mode != "RGB" or observed.size != (width, height):
                raise VideoIndexError("observed_native_geometry")
            actual = observed.tobytes()
            middle_indices = rgb_to_indices(observed, palette)
        if hashlib.sha256(actual).hexdigest() != current["rgb_sha256"]:
            raise VideoIndexError("observed_raw_hash")
        first = current["first_frame"]
        if first in stable:
            if current["last_frame"] != expected[first]:
                raise VideoIndexError("stable_run_boundary")
            raw = stable[first][0]
        else:
            if current["last_frame"] != first or i == 0 or i == len(runs) - 1:
                raise VideoIndexError("intermediate_outside_single_frame_model")
            if runs[i - 1]["first_frame"] not in stable or runs[i + 1]["first_frame"] not in stable:
                raise VideoIndexError("intermediate_not_bracketed_by_stable_ticks")
            old = stable[runs[i - 1]["first_frame"]][1]
            new = stable[runs[i + 1]["first_frame"]][1]
            analysis = classify_transition(Raster(width, height, tuple(old), ""),
                       Raster(width, height, tuple(middle_indices), ""),
                       Raster(width, height, tuple(new), ""))
            fits = find_planar_copy_fits(old, middle_indices, new, width, height)
            if not fits:
                raise VideoIndexError(f"unclassified_intermediate:{first}")
            fitted = synthesize_copy(old, new, width, fits[0])
            raw = b"".join(bytes(palette[value]) for value in fitted)
            classifications.append({"frame": first, "analysis": analysis,
                "chosen_observational_fit": [fits[0].seam_row, fits[0].completed_plane_count,
                                             fits[0].boundary_x]})
        if raw != actual:
            raise VideoIndexError(f"reconstruction_rgb_mismatch:{first}")
        reconstructed.append((current, raw))
    rows = index["frames"][begin:end + 1]
    if any(rows[i + 1]["pts"] - row["pts"] != 1 for i, row in enumerate(rows[:-1])):
        raise VideoIndexError("preview_requires_unit_pts_steps")
    output = confined(args.output_directory, root)
    output.mkdir(parents=True, exist_ok=False)
    ffmpeg = Path(args.ffmpeg).resolve(strict=True)
    if file_hash(ffmpeg) != args.ffmpeg_sha256.lower():
        raise VideoIndexError("ffmpeg_identity")
    numerator, denominator = index["timebase"]
    command = [str(ffmpeg), "-nostdin", "-n", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{width}x{height}", "-framerate", f"{denominator}/{numerator}", "-i", "pipe:0",
               "-an", "-c:v", "libx264rgb", "-qp", "0", "-preset", "ultrafast",
               str(output / "reconstructed_interval.mkv")]
    write_json(output / "encode_command.json", {"argv": command, "ffmpeg_sha256": file_hash(ffmpeg)})
    digest, frame_hashes = hashlib.sha256(), []
    with (output / "encode.log").open("xb") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log, stdout=log)
        try:
            for current, raw in reconstructed:
                for frame in range(current["first_frame"], current["last_frame"] + 1):
                    process.stdin.write(raw)
                    digest.update(raw)
                    frame_hashes.append({"frame": frame, "rgb_sha256": hashlib.sha256(raw).hexdigest()})
            process.stdin.close()
            if process.wait(timeout=30) != 0:
                raise VideoIndexError("preview_encode_failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if len(frame_hashes) != end - begin + 1:
        raise VideoIndexError("reconstruction_coverage")
    decode_command = [str(ffmpeg), "-nostdin", "-hide_banner", "-i",
                      str(output / "reconstructed_interval.mkv"), "-map", "0:v:0",
                      "-an", "-fps_mode", "passthrough", "-f", "rawvideo",
                      "-pix_fmt", "rgb24", "pipe:1"]
    write_json(output / "verify_decode_command.json", {"argv": decode_command,
                                                       "ffmpeg_sha256": file_hash(ffmpeg)})
    with (output / "verify_decode.log").open("xb") as log:
        decoded = subprocess.run(decode_command, stdout=subprocess.PIPE, stderr=log,
                                 check=True, timeout=60).stdout
    decoded_digest = verify_decoded_frames(decoded, frame_hashes, width * height * 3)
    if decoded_digest != digest.hexdigest():
        raise VideoIndexError("preview_decoded_stream_hash")
    report = {"schema_version": "pm2_activity_sequence_finish/v1", "scene": check["scene"],
              "sequence_check": {"path": str(check_path), "sha256": args.sequence_check_sha256.lower()},
              "sources": sources, "source_commit": FIXED_SOURCE_COMMIT,
              "tool_sha256": file_hash(Path(__file__)),
              "copy_classifier_sha256": file_hash(Path(__file__).with_name("pm2_activity_transition_copy.py")),
              "first_frame": begin, "last_frame": end, "reconstructed_capture_frames": len(frame_hashes),
              "exact_rgb_capture_frames": len(frame_hashes), "stable_runs": len(stable),
              "copy_intermediate_count": len(classifications), "copy_intermediates": classifications,
              "rgb_stream_sha256": digest.hexdigest(), "frame_hashes": frame_hashes,
              "preview": {"path": "reconstructed_interval.mkv",
                          "sha256": file_hash(output / "reconstructed_interval.mkv"), "audio": False},
              "preview_roundtrip": {"decoded_frames": len(frame_hashes),
                                    "exact_frames": len(frame_hashes),
                                    "rgb_stream_sha256": decoded_digest},
              "provenance": {name: {"path": name, "sha256": file_hash(output / name)} for name in
                             ("encode_command.json", "encode.log", "verify_decode_command.json",
                              "verify_decode.log")},
              "timebase": index["timebase"], "duration_seconds": len(frame_hashes)*numerator/denominator,
              "formal_s2": False, "authorization_effect": "none",
              "timing_basis": "observed_capture_run_lengths_and_fitted_copy_parameters",
              "interpretation": "I: source order plus exact fit supports display-copy intermediates, not independently authored poses; precise CPU/display sampling remains unknown",
              "boundary": "Bound interval only. Not a whole-game Replay, unique RNG recovery, original timer emulator, audio reconstruction, or semantic signoff."}
    write_json(output / "report.json", report)
    return {k: report[k] for k in ("reconstructed_capture_frames", "copy_intermediate_count",
                                  "rgb_stream_sha256", "duration_seconds")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("quarantine-root", "source-root", "sequence-check", "sequence-check-sha256",
                 "output-directory", "ffmpeg", "ffmpeg-sha256"):
        parser.add_argument("--" + name, required=True)
    try:
        print(json.dumps(run(parser.parse_args(argv))))
    except (OSError, ValueError, KeyError, TypeError, TimelineError, subprocess.SubprocessError) as exc:
        print(f"SEQUENCE_FINISH_ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
