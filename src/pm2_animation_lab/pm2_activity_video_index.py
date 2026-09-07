"""Stream a quarantined video into a hash-verified native activity run index.

Research only: this is not a Replay receipt or an S2 approval. Every decoded
frame is checked against FFmpeg's separate full RGB framehash output. Cropped
unique PNGs are navigation aids, not substitutes for the full source video.
No frame-rate conversion, palette correction, or similarity threshold is used.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


class VideoIndexError(ValueError):
    pass


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def read_frame(stream, size):
    result = bytearray()
    while len(result) < size:
        block = stream.read(size - len(result))
        if not block:
            if result:
                raise VideoIndexError("partial_raw_frame")
            return None
        result.extend(block)
    return bytes(result)


def validate_geometry(width, height, crop):
    if any(type(v) is not int for v in (width, height, *crop)):
        raise VideoIndexError("integer_geometry_required")
    if not 1 <= width <= 4096 or not 1 <= height <= 4096:
        raise VideoIndexError("frame_geometry_limit")
    x, y, w, h = crop
    if min(x, y) < 0 or min(w, h) < 1 or x + w > width or y + h > height:
        raise VideoIndexError("crop_outside_frame")


def verify_framehash(raw_text, hashes, frame_size):
    rows, timebase = [], None
    for line in raw_text.splitlines():
        match = re.fullmatch(r"#tb 0: (\d+)/(\d+)", line.strip())
        if match:
            timebase = [int(match[1]), int(match[2])]
        if not line.strip() or line.startswith("#"):
            continue
        fields = [s.strip() for s in line.split(",")]
        if len(fields) != 6 or fields[0] != "0":
            raise VideoIndexError("framehash_row")
        ordinal = len(rows)
        if ordinal >= len(hashes) or fields[5].lower() != hashes[ordinal]:
            raise VideoIndexError(f"raw_framehash_mismatch:{ordinal}")
        if int(fields[4]) != frame_size:
            raise VideoIndexError("framehash_size")
        pts, duration = int(fields[2]), int(fields[3])
        if duration <= 0 or (rows and pts <= rows[-1]["pts"]):
            raise VideoIndexError("nonmonotonic_framehash")
        rows.append({"frame": ordinal, "pts": pts, "duration": duration})
    if not rows or len(rows) != len(hashes) or timebase is None or timebase[1] <= 0:
        raise VideoIndexError("incomplete_framehash")
    return rows, timebase


def append_run(runs, ordinal, digest):
    if runs and runs[-1]["rgb_sha256"] == digest:
        runs[-1]["last_frame"] = ordinal
    else:
        runs.append({"first_frame": ordinal, "last_frame": ordinal,
                     "rgb_sha256": digest})


def confined(path, root):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise VideoIndexError("outside_quarantine")
    return resolved


def write_json(path, data):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=True)
        stream.write("\n")


def run(args):
    root = Path(args.quarantine_root).resolve()
    if not is_data_directory(root) or not root.is_dir():
        raise VideoIndexError("external_data_directory_required")
    video = confined(args.video, root)
    output = confined(args.output_directory, root)
    ffmpeg = Path(args.ffmpeg).resolve(strict=True)
    validate_geometry(args.width, args.height, args.crop)
    if not 1 <= args.max_frames <= 100000:
        raise VideoIndexError("max_frames_limit")
    actual_hash = file_hash(video)
    if actual_hash != args.video_sha256.lower():
        raise VideoIndexError("video_identity_mismatch")
    tool_hash = file_hash(ffmpeg)
    if tool_hash != args.ffmpeg_sha256.lower():
        raise VideoIndexError("ffmpeg_identity_mismatch")
    output.mkdir(parents=True, exist_ok=False)
    (output / "unique").mkdir()
    crop_stream = getattr(args, 'crop_stream', False)
    x, y, w, h = args.crop
    streamed_width, streamed_height = (w,h) if crop_stream else (args.width,args.height)
    hash_filename = 'crop_rgb.framehash' if crop_stream else 'full_rgb.framehash'
    command = [str(ffmpeg), "-nostdin", "-n", "-hide_banner", "-i", str(video)]
    for destination, options in [
        ("pipe:1", ["-f", "rawvideo"]),
        (str(output / hash_filename), ["-f", "framehash", "-hash", "sha256"]),
    ]:
        command += ["-map", "0:v:0", "-an", "-pix_fmt", "rgb24"]
        if crop_stream:command += ['-vf',f'crop={w}:{h}:{x}:{y}']
        command += ["-fps_mode", "passthrough", "-enc_time_base", "demux", *options, destination]
    started = datetime.now(timezone.utc).isoformat()
    write_json(output / "command.json", {"argv": command, "started_at_utc": started,
               "video_sha256": actual_hash, "ffmpeg_sha256": tool_hash,
               "script_sha256": file_hash(Path(__file__))})
    version = subprocess.run([str(ffmpeg), "-version"], check=True,
                             capture_output=True).stdout
    with (output / "ffmpeg_version.txt").open("xb") as stream:
        stream.write(version)
    hashes, runs, unique = [], [], {}
    with (output / "decode.log").open("xb") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log)
        try:
            while True:
                raw = read_frame(process.stdout, streamed_width * streamed_height * 3)
                if raw is None:
                    break
                if len(hashes) >= args.max_frames:
                    raise VideoIndexError("frame_limit_exceeded")
                ordinal = len(hashes)
                hashes.append(hashlib.sha256(raw).hexdigest())
                x, y, w, h = args.crop
                native = Image.frombytes("RGB", (streamed_width, streamed_height), raw)
                crop = native if crop_stream else native.crop((x, y, x + w, y + h))
                digest = hashlib.sha256(crop.tobytes()).hexdigest()
                append_run(runs, ordinal, digest)
                if digest not in unique:
                    name = f"unique/{digest}.png"
                    crop.save(output / name)
                    unique[digest] = {"path": name, "sha256": file_hash(output / name)}
            if process.wait(timeout=30) != 0:
                raise VideoIndexError("ffmpeg_decode_failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    rows, timebase = verify_framehash(
        (output / hash_filename).read_text(encoding="utf-8"), hashes,
        streamed_width * streamed_height * 3)
    if file_hash(video) != actual_hash or file_hash(ffmpeg) != tool_hash:
        raise VideoIndexError("input_changed_during_decode")
    for item in runs:
        first, last = rows[item["first_frame"]], rows[item["last_frame"]]
        item["first_pts"] = first["pts"]
        item["last_pts"] = last["pts"]
        item["first_seconds"] = first["pts"] * timebase[0] / timebase[1]
        item["last_seconds"] = last["pts"] * timebase[0] / timebase[1]
    report = {
        "schema_version": "pm2_activity_video_index/v1", "scene": args.scene,
        "video": {"path": str(video), "sha256": actual_hash},
        "capture_encoding": args.capture_encoding,
        "encoding_claim_basis": "caller_observed_stream_metadata_not_auto_certification",
        "frame_size": [args.width, args.height], "crop": args.crop,
        "frame_count": len(rows), "timebase": timebase,
        "raw_stream_framehash_verified": not crop_stream,
        "crop_stream_framehash_verified": crop_stream,
        "frame_hash_scope": 'observed_crop' if crop_stream else 'full_rgb',
        "frames": [{**row, "rgb_sha256": sha} for row, sha in zip(rows, hashes)],
        "runs": runs, "unique_images": unique,
        "provenance": {name: {"path": name, "sha256": file_hash(output / name)}
                       for name in ("command.json", "decode.log", "ffmpeg_version.txt",
                                    hash_filename)},
        "formal_s2": False, "replay": None, "actual_rng": "unknown",
        "boundary": "Exact decoding hashes prove extraction integrity, not lossless capture, authored ticks, input replay, or hidden game state."
    }
    write_json(output / "index.json", report)
    return {"frame_count": len(rows), "run_count": len(runs),
            "unique_crop_count": len(unique), "index": str(output / "index.json")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("quarantine-root", "video", "video-sha256", "ffmpeg", "ffmpeg-sha256",
                 "output-directory"):
        parser.add_argument("--" + name, required=True)
    from pm2_animation_lab.pm2_activity_scene_profiles import PROFILES
    parser.add_argument("--scene", choices=(*PROFILES,'MULTI'), required=True)
    parser.add_argument('--crop-stream',action='store_true',help='Verify every native cropped frame without transferring the full room through Python; full video remains hash-bound')
    parser.add_argument("--capture-encoding", choices=("lossless_rgb", "lossy", "unknown"),
                        required=True)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--crop", nargs=4, type=int, required=True)
    parser.add_argument("--max-frames", type=int, default=20000)
    try:
        print(json.dumps(run(parser.parse_args(argv))))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"VIDEO_INDEX_ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
