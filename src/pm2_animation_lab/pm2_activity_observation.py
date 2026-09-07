"""Freeze a native capture oracle independently of the animation producer.

Selection is explicit frame ordinals from a reviewed capture, never a search
for whichever frame makes a newly generated candidate pass.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from fractions import Fraction
from PIL import Image
from pm2_animation_lab.pm2_activity_delivery_check import bound, digest, confined, DeliveryError
from pm2_animation_lab.pm2_activity_video_index import read_frame, verify_framehash


def write(path,value):
    with Path(path).open("x",encoding="utf-8") as f:json.dump(value,f,indent=2)


def binding(path):return {"path":str(Path(path).resolve()),"sha256":digest(path)}
def pair(x):return [x.numerator,x.denominator]


def freeze(selection_path,output,root,ffmpeg):
    root=Path(root).resolve();output=confined(output,root)
    if not is_data_directory(root) or output.exists():raise DeliveryError("new_quarantined_output_required")
    selection_path=confined(selection_path,root)
    selection=json.loads(selection_path.read_text(encoding="utf-8"))
    required={"schema","scene","source_sha256","video","capture_spec","ffmpeg_sha256","tail_seconds","schedule","groups","boundary_complete"}
    if set(selection)!=required or selection["schema"]!="pm2_activity_observation_selection/v1":
        raise DeliveryError("selection_fields")
    if digest(ffmpeg)!=selection["ffmpeg_sha256"]:raise DeliveryError("ffmpeg_changed")
    video=bound(selection["video"],root)
    spec=json.loads(bound(selection["capture_spec"],root).read_text(encoding="utf-8"))
    if Path(spec["isolation_directory"]).resolve()!=video.parent.parent:
        raise DeliveryError("capture_session_video_mismatch")
    config=bound(spec["inputs"]["record_config"],root)
    normalized=re.sub(r"\s+","",config.read_text(encoding="utf-8"))
    if not all(token in normalized for token in ('vcodec="libx264rgb"','video_qp="0"','frame_drop_ratio="1"')):
        raise DeliveryError("lossless_recording_configuration_required")
    if type(selection["tail_seconds"])is not int or not 1<=selection["tail_seconds"]<=120:
        raise DeliveryError("tail_bound")
    output.mkdir(parents=True);(output/"images").mkdir()
    cmd=[str(ffmpeg),"-nostdin","-hide_banner","-loglevel","info","-sseof","-"+str(selection["tail_seconds"]),
        "-i",str(video),"-an","-vf","crop=320:128:32:240,showinfo","-fps_mode","passthrough",
        "-f","rawvideo","-pix_fmt","rgb24","pipe:1"]
    write(output/"decode_command.json",{"argv":cmd,"ffmpeg_sha256":digest(ffmpeg),"selection":binding(selection_path)})
    records=[];needed={g["first_frame"] for g in selection["groups"]};unique={}
    clip_start=selection["groups"][0]["first_frame"]
    clip_end=selection["groups"][-1]["end_frame_exclusive"]
    with (output/"decode.log").open("xb") as log:
        proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=log)
        try:
            while (raw:=read_frame(proc.stdout,320*128*3)) is not None:
                i=len(records);records.append({"frame":i,"rgb_sha256":hashlib.sha256(raw).hexdigest()})
                if i in needed:Image.frombytes("RGB",(320,128),raw).save(output/"images"/f"{i:05d}.png")
                if clip_start <= i < clip_end:
                    h=records[-1]["rgb_sha256"]
                    if h not in unique:
                        path=output/"images"/(h+".png");Image.frombytes("RGB",(320,128),raw).save(path)
                        unique[h]=binding(path)
                if i>10000:raise DeliveryError("capture_limit")
            if proc.wait(timeout=30):raise DeliveryError("decode_failure")
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
    log=(output/"decode.log").read_text(errors="replace")
    stamps={int(n):Fraction(t) for n,t in re.findall(r"\bn:\s*(\d+)\s+pts:\s*[-\d]+\s+pts_time:([-\d.e+]+)",log)}
    if len(stamps)!=len(records):raise DeliveryError("pts_coverage")
    independent=cmd[:]
    independent[independent.index("crop=320:128:32:240,showinfo")]="crop=320:128:32:240"
    independent[-5:]=["-f","framehash","-hash","sha256","-pix_fmt","rgb24","pipe:1"]
    independent[-1:-1]=["-enc_time_base","demux"]
    verified=subprocess.run(independent,capture_output=True,check=True,timeout=60).stdout.decode()
    (output/"independent.framehash").write_text(verified,encoding="utf-8")
    rows,timebase=verify_framehash(verified,[r["rgb_sha256"] for r in records],320*128*3)
    if any(Fraction(row["pts"]*timebase[0],timebase[1])!=stamps[i] for i,row in enumerate(rows)):
        raise DeliveryError("independent_pts_mismatch")
    if digest(video)!=selection["video"]["sha256"] or digest(ffmpeg)!=selection["ffmpeg_sha256"]:
        raise DeliveryError("input_changed_during_decode")
    for i,row in enumerate(records):row["time"]=pair(stamps[i])
    # The explicit selected right boundary must exist; never extrapolate a
    # final activity exposure from an unfinished capture.
    if any(g["end_frame_exclusive"]>=len(records) for g in selection["groups"]):
        raise DeliveryError("boundary_not_captured")
    origin=stamps[selection["groups"][0]["first_frame"]]
    groups=[]
    for g in selection["groups"]:
        if set(g)!={"ticks","first_frame","end_frame_exclusive"}:raise DeliveryError("group_selection_fields")
        first,end=g["first_frame"],g["end_frame_exclusive"]
        groups.append(dict(g,start=pair(stamps[first]-origin),end=pair(stamps[end]-origin),
            rgb_sha256=records[first]["rgb_sha256"],image=binding(output/"images"/f"{first:05d}.png")))
    index={"schema":"pm2_native_observation_index/v1","origin":"decoded_runtime_capture",
        "capture_encoding":"lossless_rgb","video_sha256":digest(video),"frames":records,"unique_images":unique,
        "end_time":records[-1]["time"],"decode_command":binding(output/"decode_command.json"),
        "independent_framehash":binding(output/"independent.framehash"),
        "independent_command":independent,
        "capture_spec":selection["capture_spec"],"recording_config":spec["inputs"]["record_config"],
        "decode_log":binding(output/"decode.log"),"extractor_sha256":digest(Path(__file__))}
    write(output/"index.json",index)
    oracle={"schema":"pm2_activity_observation/v1","origin":"runtime_capture",
        "scene":selection["scene"],"source_sha256":selection["source_sha256"],
        "video":selection["video"],"index":binding(output/"index.json"),"schedule":selection["schedule"],
        "size":[320,128],"origin_time":pair(origin),"groups":groups,"timing_tolerance":[0,1],
        "boundary_complete":selection["boundary_complete"],"refresh_complete":True,
        "refresh_frames":[{"frame":i-clip_start,"time":pair(stamps[i]-origin),
            "end":pair(stamps[i+1]-origin),"rgb_sha256":records[i]["rgb_sha256"]}
            for i in range(clip_start,clip_end)],
        "selection":binding(selection_path),"authorization_effect":"none"}
    write(output/"oracle.json",oracle)
    clock={"kind":"capture_calibrated","groups":[{k:g[k] for k in ("ticks","start","end")} for g in groups],
        "evidence":[binding(output/"index.json")],"boundary_complete":selection["boundary_complete"]}
    write(output/"clock.json",clock)
    return {"oracle":binding(output/"oracle.json"),"clock":binding(output/"clock.json")}


def main():
    p=argparse.ArgumentParser();p.add_argument("--selection",required=True);p.add_argument("--output",required=True)
    p.add_argument("--quarantine-root",required=True);p.add_argument("--ffmpeg",required=True)
    a=p.parse_args()
    print(json.dumps(freeze(a.selection,a.output,a.quarantine_root,Path(a.ffmpeg))))


if __name__=="__main__":main()
