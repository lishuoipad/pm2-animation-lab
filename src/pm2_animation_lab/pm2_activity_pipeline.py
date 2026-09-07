"""The only publication entry for new PM2 activity research deliveries.

Low-level legacy CLIs remain diagnostic and cannot mint this delivery receipt.
Raw assets and visual outputs remain in a external-data quarantine.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory
import argparse
import hashlib
import json
import os
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from PIL import Image
from pm2_animation_lab import pm2_activity_timeline as timeline
from pm2_animation_lab import pm2_activity_compositor as compositor
from pm2_animation_lab.pm2_activity_scene_profiles import PROFILES
from pm2_animation_lab.pm2_activity_clock import ClockError, exact_fields, validate_schedule, presentation_groups, rational
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError, bound, read_bound, digest, confined
from pm2_animation_lab.pm2_lbx_pt1 import read_lbx_asset_from_zip, parse_pt1, decode_record_planes, planes_to_indices, decode_mask_body_pair


def write(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=True, indent=2); f.write("\n")


def bind(path):
    return {"path": str(Path(path).resolve()), "sha256": digest(path)}


def compile_sequence(request, source_root):
    scene = request["scene"]
    if scene not in PROFILES:
        raise DeliveryError("unsupported_scene_requires_profile_and_oracle")
    profile = PROFILES[scene]
    source = source_root/"KOSOTEXT"/(scene+".TXT")
    timeline.verify_source_checkout(source_root, timeline.FIXED_SOURCE_COMMIT, scene)
    if digest(source) != profile["source_sha256"]:
        raise DeliveryError("fixed_scene_changed")
    # Asset names are checked against actual loading calls, never inferred from
    # the activity number (JOB003 loads J004, for example).
    text = source.read_text(encoding="cp932")
    for opcode, key in ((3,"background"),(6,"patterns")):
        token = f'WWANIME({opcode},0,"{profile[key][:-4]}")'
        if token not in text:
            raise DeliveryError("profile_asset_load_not_in_source")
    parsed = timeline.parse_source(source,scene)
    rt = timeline._new_runtime(parsed)
    init_tape=timeline.RandomTape(list(request["initialization_random_values"]))
    timeline.RestrictedInterpreter(parsed,rt,init_tape).execute_routine("ANIME_INIT")
    init_tape.require_exhausted()
    calls=[{k:day[k] for k in ("branch","expected_state","random_values")} | {"tick_count":rt.scalars["TIMELOPMAX"]}
           for day in validate_schedule(request["schedule"])]
    if 'prelude' in request:
        prelude=request['prelude']
        exact_fields(prelude,('branch','expected_state','tick_count','random_values'),'prelude')
        if (request.get('schema')!='pm2_activity_comparison_request/v1' or scene!='JOB009'
                or prelude['branch']!='hunting_intro' or prelude['expected_state'] is not None
                or type(prelude['tick_count']) is not int or prelude['tick_count']!=12):
            raise DeliveryError('unsupported_source_prelude')
        calls.insert(0,dict(prelude))
    result=timeline.build_timeline_sequence(source,scene,
        initialization_random_values=request["initialization_random_values"],entry_state=request["entry_state"],
        calls=calls,source_commit=timeline.FIXED_SOURCE_COMMIT)
    return result, bind(source)


def compose(sequence, request, root, *, include_opening=False):
    profile=PROFILES[request["scene"]]
    archive=bound(request["archive"],root)
    records={};metadata={}
    for name in (profile["background"],profile["patterns"]):
        library=profile.get('background_library',profile['library']) if name==profile['background'] else profile['library']
        payload,meta=read_lbx_asset_from_zip(archive,library,name,
            expected_archive_sha256=request["archive"]["sha256"])
        records[name]=parse_pt1(payload).records;metadata[name]=meta
    backgrounds=records[profile["background"]]
    if len(backgrounds)!=1:
        raise DeliveryError("background_record_count")
    bg=backgrounds[0]
    background=compositor.BackgroundBinding(
        planes_to_indices(decode_record_planes(bg),bg.width_bytes,bg.height_pixels),
        bg.width_bytes*8,bg.height_pixels,metadata[profile["background"]]["entry_sha256"],0)
    ptn=records[profile["patterns"]]
    foreground = None
    if 'foreground' in profile:
        binding=profile['foreground']
        payload,meta=read_lbx_asset_from_zip(archive,binding['library'],binding['asset'],
            expected_archive_sha256=request['archive']['sha256'])
        front_records=parse_pt1(payload).records
        if binding['asset'] != 'J007BK.PT1' or request['scene'] not in ('JOB006','JOB009'):
            raise DeliveryError('unreviewed_foreground_mask_storage')
        foreground=compositor.PatternBinding(1000,decode_mask_body_pair(front_records[0],front_records[1],
            mask_storage_plane_count=4),meta['entry_sha256'],0,1)
        meta['mask_storage_plane_count']=4
        meta['mask_read_semantics']='PLSLD7_VCXRTW_MASK_reuses_first_plane'
        metadata['foreground']=meta
    # ANIME_OPEN restores and presents the background before the first daily
    # actor call. Opt-in is explicit; publication callers keep their tick count.
    opening=background.indices
    if include_opening and foreground is not None:
        # Fixed JOB006:333-340 / JOB009:424-431 ANIME_OPEN restores
        # background, draws foreground bank 1 with opcode 17, then copies.
        opening=compositor.apply_masked_pattern(opening,background.width,background.height,
            foreground.pattern,anchor_x_pixels=0,anchor_y_pixels=0,include_author_offset=True,clip=True)
    frames=[opening] if include_opening else []
    # A source sequence keeps the same hash-bound PT1 bank. Decode each pattern
    # once per composition, not once per day; no process-global asset cache.
    decoded_patterns={}
    for i in range(len(sequence["calls"])):
        call=timeline.sequence_call_as_v1_timeline(sequence,i)
        needed=compositor.required_pattern_numbers(call)
        for n in needed:
            if n != 1000 and n not in decoded_patterns:
                decoded_patterns[n]=compositor.PatternBinding(n,decode_mask_body_pair(ptn[n*2],ptn[n*2+1]),
                    metadata[profile["patterns"]]["entry_sha256"],n*2,n*2+1)
        patterns={n:decoded_patterns[n] for n in needed if n != 1000}
        if 1000 in compositor.required_pattern_numbers(call):
            if foreground is None:raise DeliveryError('foreground_binding_missing')
            patterns[1000]=foreground
        contract=compositor.CompositionContract(8,1,"function7_coordinate_override",
            "zero_based_mask_body_pairs","canvas","msb_left","source_candidate")
        frames.extend(compositor.compose_activity_timeline(call,background,patterns,contract,
            binding_manifest_sha256=timeline.canonical_sha256(metadata)).frames)
    palette=read_bound(request["palette"],root)["entries"]
    if len(palette)!=16 or any(len(c)!=3 or any(type(x)is not int or not 0<=x<=255 for x in c) for c in palette):
        raise DeliveryError("palette_rgb16")
    if len({tuple(c) for c in palette})!=16:
        raise DeliveryError("palette_unique")
    images=[]
    for frame in frames:
        im=Image.frombytes("P",(background.width,background.height),frame)
        im.putpalette(sum(palette,[])+[0]*(768-48));images.append(im.convert("RGB"))
    return images,metadata


def publish(request_path, output, quarantine, source_root):
    root=Path(quarantine).resolve();source_root=Path(source_root).resolve()
    if (not is_data_directory(root) or not source_root.is_dir()):
        raise DeliveryError("existing_data_and_source_directories_required")
    output=confined(output,root)
    if "game" in {part.lower() for part in output.parts}:
        raise DeliveryError("game_build_inputs_forbidden")
    request_path=confined(request_path,root)
    if output.exists():
        raise DeliveryError("output_already_exists")
    request=json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") == "pm2_activity_comparison_request/v1":
        # A review is still produced through this entry and shared compiler,
        # compositor and clock. Its receipt verifies the comparison, never
        # promotes unequal pixels into a passed activity delivery.
        from pm2_animation_lab.pm2_activity_timed_comparison import publish_comparison
        return publish_comparison(request_path, output, root, source_root,
                                  compile_sequence, compose)
    exact_fields(request,("schema","scene","level","schedule","initialization_random_values",
        "entry_state","archive","palette","clock","oracle"),"request")
    if request["schema"]!="pm2_activity_request/v1" or request["level"] not in ("action","activity","refresh"):
        raise DeliveryError("request_schema_or_level")
    validate_schedule(request["schedule"])
    clock=read_bound(request["clock"],root)
    for ref in clock["evidence"]:bound(ref,root)
    bound(request["oracle"],root)
    sequence,source_ref=compile_sequence(request,source_root)
    images,assets=compose(sequence,request,root)
    groups=presentation_groups(sequence["ticks"],clock)
    output.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix=".pending-pm2-",dir=output.parent))
    try:
        frame_dir=staging/"frames";frame_dir.mkdir()
        rendered=[];exposures=[]
        for i,group in enumerate(groups):
            image=images[group["ticks"][0]]
            raw=image.tobytes()
            if any(images[t].tobytes()!=raw for t in group["ticks"]):
                raise DeliveryError("ambiguous_group_contains_distinct_poses")
            relative=f"frames/{i:04d}.png";image.save(staging/relative)
            rendered.append(image)
            exposures.append(dict(group,image=relative,rgb_sha256=hashlib.sha256(raw).hexdigest()))
        durations=[]
        for group in groups:
            # Cumulative rounding, not per-frame rounded drift.
            a=round(rational(group["start"])*100)
            z=round(rational(group["end"])*100)
            if z<=a:
                raise DeliveryError("gif_cannot_represent_exposure")
            durations.append((z-a)*10)
        rendered[0].save(staging/"preview.gif",save_all=True,append_images=rendered[1:],
            duration=durations,optimize=False,disposal=1)
        write(staging/"sequence.json",sequence)
        write(staging/"asset_bindings.json",assets)
        refresh = None
        if request["level"] == "refresh":
            if clock["kind"] != "capture_calibrated":
                raise DeliveryError("refresh_hardware_prediction_not_implemented")
            from pm2_animation_lab.pm2_activity_refresh import reconstruct
            refresh = reconstruct(staging,root,request,exposures,images,source_root)
            write(staging/"refresh_sequence.json",refresh)
        manifest={"schema":"pm2_activity_delivery/v1","entrypoint":"pm2_activity_pipeline",
            "producer_sha256":digest(Path(__file__)),"scene":request["scene"],"level":request["level"],
            "source_sha256":source_ref["sha256"],"request":bind(request_path),
            "clock":request["clock"],"oracle":request["oracle"],"schedule":request["schedule"],
            "size":list(rendered[0].size),"groups":exposures,
            "timing_claim":"capture_conditioned" if clock["kind"]=="capture_calibrated" else "model_prediction",
            "inputs":[source_ref,request["archive"],request["palette"]],
            "outputs":{p.relative_to(staging).as_posix():digest(p) for p in staging.rglob("*") if p.is_file()},
            "scope_note":"Declared activity window only; not game dialogue, economy, audio or unobserved branches.",
            "formal_s2":False,"authorization_effect":"none"}
        # Source is permitted outside runtime quarantine, but never outside D.
        manifest["source_binding"]=manifest["inputs"].pop(0)
        if refresh is not None:
            manifest["refresh_frames"]=refresh["frames"]
            manifest["refresh_images"]=refresh["images"]
        write(staging/"candidate.json",manifest)
        from pm2_animation_lab.pm2_activity_delivery_check import check
        result=check(staging,root,oracle_ref=request["oracle"])
        write(staging/"delivery_check.json",result)
        os.rename(staging,output)
        return {"status":"published","path":str(output),"level":request["level"],
                "groups":len(groups),"timing_claim":manifest["timing_claim"]}
    except Exception as error:
        write(staging/"FAILED.json",{"status":"not_published","error":type(error).__name__+":"+str(error)})
        raise


def playback(request_path, directory, quarantine, source_root, *, allow_differences=False):
    from pm2_animation_lab.pm2_activity_playback import prepare_playback
    return prepare_playback(request_path, directory, quarantine, source_root,
                            allow_differences=allow_differences,
                            compile_sequence=compile_sequence, compose=compose)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--request",required=True,help="Explicit current request for publication or playback")
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output",help="Publish to a new directory")
    mode.add_argument("--playback-directory",help="Recheck this published directory against the current request before playback")
    p.add_argument("--allow-differences",action="store_true",help="Playback only: explicitly label unequal comparisons as difference diagnostics")
    p.add_argument("--quarantine-root",required=True);p.add_argument("--source-root",required=True)
    args=p.parse_args(argv)
    if args.allow_differences and not args.playback_directory:
        p.error('--allow-differences is only valid with --playback-directory')
    try:
        if args.playback_directory:
            result=playback(args.request,args.playback_directory,args.quarantine_root,args.source_root,
                            allow_differences=args.allow_differences)
        else:
            result=publish(args.request,args.output,args.quarantine_root,args.source_root)
        print(json.dumps(result));return 0
    except (ValueError,OSError,KeyError,IndexError,timeline.TimelineError) as error:
        print("PIPELINE_REJECTED:"+str(error),file=sys.stderr);return 2


if __name__=="__main__":
    raise SystemExit(main())
