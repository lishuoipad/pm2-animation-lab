"""Inject old failures into isolated delivery copies and require rejection."""
from __future__ import annotations
import argparse
import copy
import json
import shutil
from pathlib import Path
from PIL import Image
from pm2_animation_lab.pm2_activity_delivery_check import check, digest, DeliveryError, confined


def save(path,value):path.write_text(json.dumps(value,indent=2),encoding="utf-8")


def run(delivery,output,root):
    delivery=confined(delivery,root);output=confined(output,root)
    if output.exists():raise ValueError("new_fault_output_required")
    good=check(delivery,root)
    original=json.loads((delivery/"candidate.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True)
    cases=("wrong_pattern","phase_reset","reorder","constant_duration","drop_sunday_or_day",
        "terminal_hold","self_oracle","fitted_claims_prediction","missing_tick","film_off_by_one",
        "source_layer_order","gif_loop","gif_duration","input_hash","refresh_drop","refresh_pixels",
        "formal_s2_claim","missing_output_binding","request_scene","source_binding","refresh_media_manifest")
    outcomes=[]
    for name in cases:
        trial=output/name;shutil.copytree(delivery,trial,ignore=shutil.ignore_patterns("delivery_check.json"))
        save(trial/"FAULT_EXPERIMENT.json",{"intent":"rejection_test","fault":name,"not_a_delivery":True})
        m=copy.deepcopy(original)
        if name=="wrong_pattern":m["groups"][0]["rgb_sha256"]="0"*64
        elif name=="phase_reset":m["groups"][5]["rgb_sha256"]=m["groups"][0]["rgb_sha256"]
        elif name=="reorder":m["groups"].reverse()
        elif name=="constant_duration":
            for i,g in enumerate(m["groups"]):g["start"]=[i*17,100];g["end"]=[(i+1)*17,100]
        elif name=="drop_sunday_or_day":m["schedule"]["days"].pop()
        elif name=="terminal_hold":m["groups"][-1]["end"]=[45,1]
        elif name=="self_oracle":
            p=trial/"own_oracle.json";shutil.copyfile(original["oracle"]["path"],p)
            m["oracle"]={"path":str(p),"sha256":digest(p)}
        elif name=="fitted_claims_prediction":m["timing_claim"]="model_prediction"
        elif name=="missing_tick":m["groups"][0]["ticks"]=[]
        elif name in ("film_off_by_one","source_layer_order"):
            p=trial/"sequence.json";s=json.loads(p.read_text(encoding="utf-8"))
            if name=="film_off_by_one":s["ticks"][0]["draw_operations"][0]["pattern_number"]+=1
            else:s["ticks"][0]["composite_track_order"].reverse()
            save(p,s);m["outputs"]["sequence.json"]=digest(p)
        elif name in ("gif_loop","gif_duration"):
            p=trial/"preview.gif";frames=[];durations=[]
            with Image.open(p) as gif:
                for i in range(gif.n_frames):gif.seek(i);frames.append(gif.convert("RGB").copy());durations.append(gif.info["duration"])
            kwargs={"loop":0} if name=="gif_loop" else {}
            if name=="gif_duration":durations[0]+=30
            frames[0].save(p,save_all=True,append_images=frames[1:],duration=durations,disposal=1,**kwargs)
            m["outputs"]["preview.gif"]=digest(p)
        elif name=="input_hash":m["inputs"][0]["sha256"]="0"*64
        elif name=="refresh_drop":m["refresh_frames"].pop()
        elif name=="refresh_pixels":
            h=m["refresh_frames"][0]["rgb_sha256"];p=trial/m["refresh_images"][h]
            with Image.open(p) as im:changed=im.copy()
            changed.putpixel((0,0),(1,2,3));changed.save(p);m["outputs"][m["refresh_images"][h]]=digest(p)
        elif name=="formal_s2_claim":m["formal_s2"]=True
        elif name=="missing_output_binding":m["outputs"].pop("sequence.json")
        elif name=="request_scene":
            p=trial/"wrong_request.json";r=json.loads(Path(original["request"]["path"]).read_text(encoding="utf-8"))
            r["scene"]="TRG005" if r["scene"]=="TRG004" else "TRG004"
            save(p,r);m["request"]={"path":str(p),"sha256":digest(p)}
        elif name=="source_binding":m["source_binding"]["sha256"]="0"*64
        elif name=="refresh_media_manifest":
            p=trial/"refresh_sequence.json";r=json.loads(p.read_text(encoding="utf-8"))
            r["frames"].pop();save(p,r);m["outputs"]["refresh_sequence.json"]=digest(p)
        save(trial/"candidate.json",m)
        try:check(trial,root,oracle_ref=original["oracle"])
        except (DeliveryError,ValueError) as error:
            outcomes.append({"fault":name,"rejected":True,"reason":str(error)})
        else:raise AssertionError("fault_was_accepted:"+name)
    result={"baseline":good,"injected":len(outcomes),"rejected":len(outcomes),"outcomes":outcomes,
        "authorization_effect":"none"}
    save(output/"results.json",result);return result


def main():
    p=argparse.ArgumentParser();p.add_argument("--delivery",required=True);p.add_argument("--output",required=True)
    p.add_argument("--quarantine-root",required=True);a=p.parse_args()
    print(json.dumps(run(a.delivery,a.output,a.quarantine_root)))


if __name__=="__main__":main()
