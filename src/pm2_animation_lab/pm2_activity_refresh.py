"""Observed copy-state reconstruction, never independent hardware prediction."""
from __future__ import annotations
import hashlib
from pathlib import Path
from fractions import Fraction
from PIL import Image
from pm2_animation_lab.pm2_activity_delivery_check import read_bound, bound, image_rgb, DeliveryError, frac
from pm2_animation_lab.pm2_activity_transition_copy import find_planar_copy_fits
from pm2_animation_lab.pm2_activity_sequence_finish import rgb_to_indices, synthesize_copy, source_chain


def reconstruct(directory, root, request, exposures, images, source_root):
    oracle=read_bound(request["oracle"],root);index=read_bound(oracle["index"],root)
    if oracle.get("refresh_complete") is not True:
        raise DeliveryError("refresh_evidence_missing")
    sources=source_chain(source_root,request["scene"])
    palette=read_bound(request["palette"],root)["entries"]
    stable=[rgb_to_indices(im,palette) for im in images]
    groups=oracle["groups"]
    generated=[];fits_used=[];unique={}
    first=groups[0]["first_frame"];last=groups[-1]["end_frame_exclusive"]
    (directory/"refresh_frames").mkdir()
    cursor=0
    for frame in range(first,last):
        while frame>=groups[cursor]["end_frame_exclusive"]:cursor+=1
        g=groups[cursor];row=index["frames"][frame];h=row["rgb_sha256"]
        current=stable[g["ticks"][0]]
        expected_rgb=images[g["ticks"][0]].tobytes()
        if h==hashlib.sha256(expected_rgb).hexdigest():
            raw=expected_rgb
        else:
            if cursor+1>=len(groups) or frame!=g["end_frame_exclusive"]-1:
                raise DeliveryError("refresh_unknown_or_unbounded_transition")
            actual_path=bound(index["unique_images"][h],root)
            with Image.open(actual_path) as im:
                observed=rgb_to_indices(im,palette)
            following=stable[groups[cursor+1]["ticks"][0]]
            fits=find_planar_copy_fits(current,observed,following,oracle["size"][0],oracle["size"][1])
            if not fits:raise DeliveryError("refresh_copy_model_no_fit")
            fitted=synthesize_copy(current,following,oracle["size"][0],fits[0])
            raw=b"".join(bytes(palette[v]) for v in fitted)
            fits_used.append({"frame":frame-first,"parameters":[fits[0].seam_row,fits[0].completed_plane_count,fits[0].boundary_x]})
        if hashlib.sha256(raw).hexdigest()!=h:raise DeliveryError("refresh_exact_rgb_failed")
        if h not in unique:
            name="refresh_frames/"+h+".png"
            Image.frombytes("RGB",tuple(oracle["size"]),raw).save(directory/name);unique[h]=name
        start=frac(row["time"])-frac(oracle["origin_time"])
        end=frac(index["frames"][frame+1]["time"])-frac(oracle["origin_time"])
        generated.append({"frame":frame-first,"time":[start.numerator,start.denominator],
            "end":[end.numerator,end.denominator],"rgb_sha256":h})
    return {"frames":generated,"images":unique,"fits":fits_used,"source_chain":sources,
        "claim":"capture_conditioned_exact_refresh_sequence_not_hardware_prediction"}
