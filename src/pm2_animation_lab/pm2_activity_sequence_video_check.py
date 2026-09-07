"""Compare explicit source-compatible sequences with a verified video index.

RNG values in a spec are hypotheses, never recovered inputs. Identical frames
can share a run (notably Sunday); those ticks are not independently observable.
Only exact RGB matches are accepted. A lossy capture cannot receive this check.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image

from pm2_animation_lab.pm2_activity_compositor import (
    compose_activity_timeline, load_bindings, canonical_sha256,
)
from pm2_animation_lab.pm2_activity_timeline import (
    build_timeline_sequence, sequence_call_as_v1_timeline, verify_source_checkout,
    FIXED_SOURCE_COMMIT, TimelineError,
)
from pm2_animation_lab.pm2_activity_compositor import CompositionError
from pm2_animation_lab.pm2_activity_video_index import (
    VideoIndexError, confined, file_hash, write_json, verify_framehash,
)


def ordered_exact_matches(hashes, runs):
    """Earliest monotone witness, not a claim of unique tick timing."""
    cursor, previous_hash, rows = 0, None, []
    for tick, digest in enumerate(hashes):
        same = bool(rows and rows[-1]["matched"] and digest == previous_hash)
        start = max(0, cursor - 1) if same else cursor
        found = next((i for i in range(start, len(runs))
                      if runs[i]["rgb_sha256"] == digest), None)
        row = {"tick": tick, "rgb_sha256": digest, "matched": found is not None}
        if found is not None:
            row.update({"run_index": found, "first_frame": runs[found]["first_frame"],
                        "last_frame": runs[found]["last_frame"],
                        "first_seconds": runs[found]["first_seconds"],
                        "last_seconds": runs[found]["last_seconds"],
                        "shares_previous_run": same and found == cursor - 1})
            cursor = found + 1
        rows.append(row)
        previous_hash = digest
    return rows


def render_sequence(sequence, loaded, palette):
    if len(palette) != 16 or any(len(v) != 3 or any(type(c) is not int or not 0 <= c <= 255
                                                 for c in v) for v in palette):
        raise VideoIndexError("invalid_rgb16_palette")
    frames, results = [], []
    for i in range(len(sequence["calls"])):
        timeline = sequence_call_as_v1_timeline(sequence, i)
        result = compose_activity_timeline(timeline, loaded.background, loaded.patterns,
                                          loaded.contract,
                                          binding_manifest_sha256=loaded.manifest_sha256)
        results.append(result)
        for frame in result.frames:
            image = Image.frombytes("P", (loaded.background.width, loaded.background.height), frame)
            image.putpalette([c for entry in palette for c in entry] + [0] * (768 - 48))
            frames.append(image.convert("RGB"))
    return frames, results


def load_checked(path, expected):
    if file_hash(path) != expected.lower():
        raise VideoIndexError("input_hash_mismatch")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_index_provenance(index, index_path, root):
    for binding in index["provenance"].values():
        path = confined(index_path.parent / binding["path"], root)
        if file_hash(path) != binding["sha256"]:
            raise VideoIndexError("extraction_provenance_hash")
    source = confined(index["video"]["path"], root)
    if file_hash(source) != index["video"]["sha256"]:
        raise VideoIndexError("video_changed_since_extraction")
    hash_binding = index["provenance"]["full_rgb.framehash"]
    rows, timebase = verify_framehash(
        confined(index_path.parent / hash_binding["path"], root).read_text(encoding="utf-8"),
        [frame["rgb_sha256"] for frame in index["frames"]],
        index["frame_size"][0] * index["frame_size"][1] * 3)
    if timebase != index["timebase"] or len(rows) != index["frame_count"]:
        raise VideoIndexError("index_frame_count_or_timebase")
    for row, frame in zip(rows, index["frames"]):
        if any(row[k] != frame[k] for k in row):
            raise VideoIndexError("index_frame_timing")
    previous = -1
    for run in index["runs"]:
        first, last = run["first_frame"], run["last_frame"]
        if first != previous + 1 or not first <= last < len(rows):
            raise VideoIndexError("index_run_partition")
        for key, ordinal in (("first_seconds", first), ("last_seconds", last)):
            if run[key] != rows[ordinal]["pts"] * timebase[0] / timebase[1]:
                raise VideoIndexError("index_run_timing")
        previous = last
    if previous != len(rows) - 1:
        raise VideoIndexError("index_incomplete_runs")


def run(args):
    root = Path(args.quarantine_root).resolve()
    if not is_data_directory(root) or not root.is_dir():
        raise VideoIndexError("external_data_directory_required")
    paths = {name: confined(getattr(args, name), root) for name in
             ("spec", "bindings", "base_timeline", "palette", "video_index", "output_directory")}
    # All external inputs are explicit and hash-bound; no auto-selected palette.
    data = {name: load_checked(paths[name], getattr(args, name + "_sha256"))
            for name in ("spec", "bindings", "base_timeline", "palette", "video_index")}
    video = data["video_index"]
    if video.get("schema_version") != "pm2_activity_video_index/v1" or not video.get("raw_stream_framehash_verified"):
        raise VideoIndexError("unverified_video_index")
    if video.get("capture_encoding") != "lossless_rgb":
        raise VideoIndexError("lossless_rgb_required_for_exact_calibration")
    verify_index_provenance(video, paths["video_index"], root)
    source_root = Path(args.source_root).resolve()
    if not source_root.is_dir():
        raise VideoIndexError("source_directory_required")
    verify_source_checkout(source_root, FIXED_SOURCE_COMMIT, args.scene)
    sequence = build_timeline_sequence(source_root / "KOSOTEXT" / (args.scene + ".TXT"),
                                       args.scene, source_commit=FIXED_SOURCE_COMMIT, **data["spec"])
    loaded = load_bindings(paths["bindings"], root, data["base_timeline"])
    if video["scene"] != args.scene or data["bindings"]["scene_id"] != args.scene:
        raise VideoIndexError("scene_identity_mismatch")
    if video["crop"][2:] != [loaded.background.width, loaded.background.height]:
        raise VideoIndexError("native_geometry_mismatch")
    frames, results = render_sequence(sequence, loaded, data["palette"]["entries"])
    hashes = [hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames]
    if not 0 <= args.start_seconds < args.end_seconds:
        raise VideoIndexError("invalid_time_window")
    runs = [r for r in video["runs"] if r["first_seconds"] >= args.start_seconds
            and r["last_seconds"] <= args.end_seconds]
    matches = ordered_exact_matches(hashes, runs)
    # Recheck every matched PNG rather than trusting a self-reported index hash.
    for row in matches:
        if row["matched"]:
            image_binding = video["unique_images"][row["rgb_sha256"]]
            path = confined(paths["video_index"].parent / image_binding["path"], root)
            if file_hash(path) != image_binding["sha256"]:
                raise VideoIndexError("runtime_png_file_hash")
            with Image.open(path) as image:
                if image.mode != "RGB" or image.size != frames[row["tick"]].size:
                    raise VideoIndexError("runtime_png_geometry")
                if image.tobytes() != frames[row["tick"]].tobytes():
                    raise VideoIndexError("runtime_png_pixels")
    output = paths["output_directory"]
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "sequence.json", sequence)
    for i, frame in enumerate(frames):
        frame.save(output / f"tick_{i:04d}.png")
    for i, result in enumerate(results):
        write_json(output / f"composition_call_{i:02d}.json", result.manifest)
    count = sum(row["matched"] for row in matches)
    report = {
        "schema_version": "pm2_activity_sequence_video_check/v1", "scene": args.scene,
        "inputs": {name: {"path": str(paths[name]), "sha256": file_hash(paths[name])}
                   for name in data},
        "sequence_sha256": file_hash(output / "sequence.json"),
        "source_commit": FIXED_SOURCE_COMMIT,
        "source_sha256": sequence["sources"][0]["sha256"],
        "tool_sha256": file_hash(Path(__file__)),
        "dependency_sha256": {name: file_hash(Path(__file__).with_name(name)) for name in
                              ("pm2_activity_timeline.py", "pm2_activity_compositor.py",
                               "pm2_activity_video_index.py", "pm2_lbx_pt1.py")},
        "call_states": [call["selection"]["state"] for call in sequence["calls"]],
        "tick_count": len(frames), "ordered_exact_ticks": count,
        "all_ticks_have_ordered_exact_match": count == len(frames),
        "indistinguishable_same_run_ticks": sum(row.get("shares_previous_run", False) for row in matches),
        "search_window_seconds": [args.start_seconds, args.end_seconds], "matches": matches,
        "rng_status": "compatible_witness_not_actual_rng", "formal_s2": False,
        "timing_status": "candidate_run_mapping_not_unique_authored_exposure_timing",
        "boundary": "Matched native stable rasters only; unmatched intermediates are not classified. Palette is an explicit candidate, not globally confirmed by this check. No Replay or hidden state recovery."
    }
    write_json(output / "report.json", report)
    return {key: report[key] for key in ("tick_count", "ordered_exact_ticks",
                                        "indistinguishable_same_run_ticks", "call_states")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarantine-root", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--scene", choices=("JOB003", "JOB008"), required=True)
    for name in ("spec", "bindings", "base-timeline", "palette", "video-index"):
        parser.add_argument("--" + name, required=True)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--start-seconds", type=float, required=True)
    parser.add_argument("--end-seconds", type=float, required=True)
    try:
        print(json.dumps(run(parser.parse_args(argv))))
    except (OSError, ValueError, KeyError, TypeError, TimelineError, CompositionError) as exc:
        print(f"SEQUENCE_VIDEO_CHECK_ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
