"""Independent delivery checks: never import or run the producer."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path
from fractions import Fraction
from PIL import Image


class DeliveryError(ValueError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def frac(value):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(x) is not int for x in value) or value[1] <= 0):
        raise DeliveryError("invalid_time")
    return Fraction(*value)


def confined(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise DeliveryError("outside_quarantine")
    return path


def bound(ref, root):
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise DeliveryError("binding_fields")
    path = confined(ref["path"], root)
    if digest(path) != ref["sha256"]:
        raise DeliveryError("binding_changed:" + path.name)
    return path


def read_bound(ref, root):
    return json.loads(bound(ref, root).read_text(encoding="utf-8"))


def image_rgb(path, size):
    with Image.open(path) as im:
        if list(im.size) != size or im.mode != "RGB":
            raise DeliveryError("native_rgb_geometry")
        return im.tobytes()


def check_records(candidate, oracle):
    """Compare external expected records, not expectations from candidate code."""
    if candidate["scene"] != oracle["scene"] or candidate["source_sha256"] != oracle["source_sha256"]:
        raise DeliveryError("oracle_scene_or_source")
    if candidate["schedule"] != oracle["schedule"]:
        raise DeliveryError("schedule_or_boundary_mismatch")
    if candidate["size"] != oracle["size"]:
        raise DeliveryError("geometry_mismatch")
    actual, expected = candidate["groups"], oracle["groups"]
    if len(actual) != len(expected):
        raise DeliveryError("group_count")
    tolerance = frac(oracle["timing_tolerance"])
    if not 0 <= tolerance <= Fraction(1, 30):
        raise DeliveryError("timing_tolerance_outside_policy")
    for i, (a, e) in enumerate(zip(actual, expected)):
        if a["ticks"] != e["ticks"] or a["rgb_sha256"] != e["rgb_sha256"]:
            raise DeliveryError(f"ordered_rgb:{i}")
        if candidate["level"] != "action":
            if abs(frac(a["start"])-frac(e["start"])) > tolerance or abs(frac(a["end"])-frac(e["end"])) > tolerance:
                raise DeliveryError(f"timing:{i}")
    if candidate["level"] != "action" and oracle.get("boundary_complete") is not True:
        raise DeliveryError("incomplete_runtime_boundary")
    if candidate["level"] == "refresh":
        if oracle.get("refresh_complete") is not True:
            raise DeliveryError("refresh_evidence_missing")
        if candidate.get("refresh_frames") != oracle.get("refresh_frames"):
            raise DeliveryError("refresh_frame_or_pts_mismatch")


def check(directory, quarantine, *, oracle_ref=None):
    directory = confined(directory, quarantine)
    manifest_path = directory/"candidate.json"
    candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
    if candidate.get("schema") != "pm2_activity_delivery/v1" or candidate.get("entrypoint") != "pm2_activity_pipeline":
        raise DeliveryError("unregistered_delivery_entrypoint")
    if (candidate.get("authorization_effect") != "none" or candidate.get("formal_s2") is not False
            or candidate.get("level") not in ("action", "activity", "refresh")):
        raise DeliveryError("delivery_level_or_self_authorization")
    # Caller can pin the oracle independently of candidate-controlled metadata.
    if oracle_ref is not None and candidate["oracle"] != oracle_ref:
        raise DeliveryError("oracle_rebinding")
    oracle_path = bound(candidate["oracle"], quarantine)
    if oracle_path.is_relative_to(directory):
        raise DeliveryError("self_generated_oracle")
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    if oracle.get("schema") != "pm2_activity_observation/v1" or oracle.get("origin") != "runtime_capture":
        raise DeliveryError("independent_runtime_oracle_required")
    video = bound(oracle["video"], quarantine)
    index = read_bound(oracle["index"], quarantine)
    if index.get("video_sha256") != digest(video) or index.get("origin") != "decoded_runtime_capture":
        raise DeliveryError("oracle_index_video_binding")
    if index.get("capture_encoding") != "lossless_rgb":
        raise DeliveryError("lossless_reference_required")
    from pm2_animation_lab.pm2_activity_video_index import verify_framehash
    framehash = bound(index["independent_framehash"], quarantine).read_text(encoding="utf-8")
    verified_rows, tb = verify_framehash(framehash, [r["rgb_sha256"] for r in index["frames"]], oracle["size"][0]*oracle["size"][1]*3)
    if any(Fraction(row["pts"]*tb[0],tb[1]) != frac(index["frames"][i]["time"]) for i,row in enumerate(verified_rows)):
        raise DeliveryError("oracle_pts_not_verified")
    bound(index["decode_command"], quarantine)
    bound(index["decode_log"], quarantine)
    bound(index["capture_spec"], quarantine)
    bound(index["recording_config"], quarantine)
    # Oracle references must actually be present in the separately frozen decode
    # index, in order, with the indexed pixels and time range.
    previous = -1
    for group in oracle["groups"]:
        first, end = group["first_frame"], group["end_frame_exclusive"]
        if type(first) is not int or type(end) is not int or first <= previous or end <= first:
            raise DeliveryError("oracle_frame_order")
        previous = first
        row = index["frames"][first]
        if row["rgb_sha256"] != group["rgb_sha256"]:
            raise DeliveryError("oracle_rgb_not_in_index")
        if frac(row["time"]) != frac(group["start"]) + frac(oracle["origin_time"]):
            raise DeliveryError("oracle_start_not_in_index")
        end_time = index["frames"][end]["time"] if end < len(index["frames"]) else index["end_time"]
        if frac(end_time) != frac(group["end"]) + frac(oracle["origin_time"]):
            raise DeliveryError("oracle_end_not_in_index")
        im = bound(group["image"], quarantine)
        if hashlib.sha256(image_rgb(im, oracle["size"])).hexdigest() != group["rgb_sha256"]:
            raise DeliveryError("oracle_image_changed")
    request = read_bound(candidate["request"], quarantine)
    if (request["oracle"] != candidate["oracle"] or request["level"] != candidate["level"]
            or request["schedule"] != candidate["schedule"] or request["scene"] != candidate["scene"]):
        raise DeliveryError("request_scope_changed")
    if candidate["inputs"] != [request["archive"], request["palette"]]:
        raise DeliveryError("input_bindings_removed_or_replaced")
    clock = read_bound(candidate["clock"], quarantine)
    if request["clock"] != candidate["clock"]:
        raise DeliveryError("clock_rebinding")
    for ref in clock["evidence"]:
        bound(ref, quarantine)
    if clock["kind"] == "capture_calibrated" and candidate["timing_claim"] != "capture_conditioned":
        raise DeliveryError("fitted_timing_cannot_claim_prediction")
    if clock["kind"] == "engine_model":
        if candidate["timing_claim"] != "model_prediction":
            raise DeliveryError("engine_claim")
        if candidate["level"] != "action" and any(ref["sha256"] in {oracle["video"]["sha256"], oracle["index"]["sha256"], candidate["oracle"]["sha256"]} for ref in clock["evidence"]):
            raise DeliveryError("prediction_requires_holdout_capture")
    for ref in candidate["inputs"]:
        bound(ref, quarantine)
    source = Path(candidate["source_binding"]["path"]).resolve()
    if (not source.is_file() or digest(source) != candidate["source_sha256"]
            or candidate["source_binding"]["sha256"] != candidate["source_sha256"]):
        raise DeliveryError("source_changed")
    check_records(candidate, oracle)
    required_outputs = {"sequence.json", "asset_bindings.json", "preview.gif"}
    required_outputs.update(g["image"] for g in candidate["groups"])
    if candidate["level"] == "refresh":
        required_outputs.add("refresh_sequence.json")
        required_outputs.update(candidate["refresh_images"].values())
    if not required_outputs.issubset(candidate["outputs"]):
        raise DeliveryError("required_output_binding_missing")
    for name, sha in candidate["outputs"].items():
        path = confined(directory/name, directory)
        if digest(path) != sha:
            raise DeliveryError("output_changed:" + name)
    for group in candidate["groups"]:
        if hashlib.sha256(image_rgb(confined(directory/group["image"], directory), candidate["size"])).hexdigest() != group["rgb_sha256"]:
            raise DeliveryError("candidate_pixel_bytes")
    if candidate["level"] == "refresh":
        refresh = json.loads((directory/"refresh_sequence.json").read_text(encoding="utf-8"))
        if (refresh["frames"] != candidate["refresh_frames"] or refresh["images"] != candidate["refresh_images"]
                or refresh["claim"] != "capture_conditioned_exact_refresh_sequence_not_hardware_prediction"):
            raise DeliveryError("refresh_media_manifest_mismatch")
        for frame in candidate["refresh_frames"]:
            path = confined(directory/candidate["refresh_images"][frame["rgb_sha256"]],directory)
            if hashlib.sha256(image_rgb(path,candidate["size"])).hexdigest()!=frame["rgb_sha256"]:
                raise DeliveryError("refresh_encoded_pixels_changed")
        actual_begin=oracle["groups"][0]["first_frame"]
        expected_rows=[]
        for i in range(actual_begin,oracle["groups"][-1]["end_frame_exclusive"]):
            start=frac(index["frames"][i]["time"])-frac(oracle["origin_time"])
            end=frac(index["frames"][i+1]["time"])-frac(oracle["origin_time"])
            expected_rows.append({"frame":i-actual_begin,"time":[start.numerator,start.denominator],
                "end":[end.numerator,end.denominator],"rgb_sha256":index["frames"][i]["rgb_sha256"]})
        if candidate["refresh_frames"]!=expected_rows:
            raise DeliveryError("refresh_not_equal_to_native_index")
    sequence = json.loads((directory/"sequence.json").read_text(encoding="utf-8"))
    if len(sequence["calls"]) != len(candidate["schedule"]["days"]):
        raise DeliveryError("day_call_count")
    covered = [tick for group in candidate["groups"] for tick in group["ticks"]]
    if covered != list(range(len(sequence["ticks"]))):
        raise DeliveryError("delivery_tick_coverage")
    previous_end = Fraction(0)
    for group in candidate["groups"]:
        if frac(group["start"]) != previous_end or frac(group["end"]) <= previous_end:
            raise DeliveryError("delivery_exposure_gap")
        previous_end = frac(group["end"])
    for tick in sequence["ticks"]:
        for draw in tick["draw_operations"]:
            if draw["pattern_number"] != draw["film_value"] - 1:
                raise DeliveryError("one_based_film_mapping")
        if candidate["scene"] in ("TRG004", "TRG005"):
            day = candidate["schedule"]["days"][tick["call_index"]]
            expected_order = ([] if candidate["scene"] == "TRG004" else [7,8,9,10]) if day["branch"] == "sunday" else [3,4,5,6,day["expected_state"]]
            if tick["composite_track_order"] != expected_order:
                raise DeliveryError("source_draw_order")
    # Reopen the actual delivery GIF. Cumulative boundary rounding is bounded;
    # encoded delays are never used as the native timestamp authority.
    with Image.open(directory/"preview.gif") as gif:
        if "loop" in gif.info:
            raise DeliveryError("unexpected_loop")
        delivered = []
        elapsed = Fraction(0)
        for i in range(gif.n_frames):
            gif.seek(i)
            if list(gif.size) != candidate["size"]:
                raise DeliveryError("gif_geometry")
            raw = gif.convert("RGB").tobytes()
            duration = Fraction(gif.info.get("duration", 0), 1000)
            if duration <= 0:
                raise DeliveryError("gif_zero_duration")
            delivered.append((hashlib.sha256(raw).hexdigest(), elapsed, elapsed+duration))
            elapsed += duration
    def merge(rows):
        result = []
        for h, a, z in rows:
            if result and result[-1][0] == h:
                result[-1] = (h, result[-1][1], z)
            else:
                result.append((h,a,z))
        return result
    logical = merge([(g["rgb_sha256"], frac(g["start"]), frac(g["end"])) for g in candidate["groups"]])
    encoded = merge(delivered)
    if len(logical) != len(encoded):
        raise DeliveryError("gif_order_or_count")
    for a, e in zip(encoded, logical):
        if a[0] != e[0] or abs(a[1]-e[1]) > Fraction(1,200) or abs(a[2]-e[2]) > Fraction(1,200):
            raise DeliveryError("gif_rgb_or_cumulative_timing")
    return {"schema": "pm2_activity_delivery_check/v1", "status": "passed",
            "candidate_sha256": digest(manifest_path), "oracle_sha256": digest(oracle_path),
            "validator_sha256": digest(Path(__file__)), "level": candidate["level"],
            "timing_claim": candidate["timing_claim"], "checked_groups": len(logical),
            "formal_s2": False, "authorization_effect": "none"}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--directory",required=True);p.add_argument("--quarantine-root",required=True)
    args=p.parse_args(argv)
    try:
        print(json.dumps(check(args.directory,args.quarantine_root)))
        return 0
    except (DeliveryError, ValueError, OSError, KeyError, IndexError) as e:
        print("DELIVERY_REJECTED:"+str(e),file=sys.stderr);return 2


if __name__=="__main__":
    raise SystemExit(main())
