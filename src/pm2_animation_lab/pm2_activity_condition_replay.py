"""Replay a frozen source-compatible condition tape as diagnostic candidates.

This is not RNG recovery. It never reads native pictures to choose inputs.
The input's provenance must explain whether it was selected using observations.
Outputs are untimed PNG/JSON; animated delivery requires the unified pipeline.
"""
import argparse
import hashlib
import json
from pathlib import Path
from pm2_animation_lab import pm2_activity_timeline as timeline
from pm2_animation_lab import pm2_activity_pipeline as pipeline
from pm2_animation_lab.pm2_activity_batch_research import RUNTIME, SOURCE, ARCHIVE, PALETTE
from pm2_animation_lab.pm2_activity_video_index import confined


def replay(scene, spec_path, output):
    spec_path=confined(spec_path,RUNTIME)
    output=confined(output,RUNTIME/'experiments')
    spec=json.loads(spec_path.read_text(encoding='utf-8'))
    if set(spec)!={'initialization_random_values','entry_state','calls'}:
        raise ValueError('Explicit initialization, entry state and daily calls required')
    timeline.verify_source_checkout(SOURCE,timeline.FIXED_SOURCE_COMMIT,scene)
    sequence=timeline.build_timeline_sequence(SOURCE/'KOSOTEXT'/(scene+'.TXT'),scene,
        source_commit=timeline.FIXED_SOURCE_COMMIT,**spec)
    inputs={'archive':pipeline.bind(ARCHIVE),'palette':pipeline.bind(PALETTE)}
    images,assets=pipeline.compose(sequence,{'scene':scene,**inputs},RUNTIME,include_opening=True)
    output.mkdir(parents=True,exist_ok=False)
    opening=images.pop(0);opening.save(output/'opening.png')
    frames=[];unique={}
    for tick,im in enumerate(images):
        h=hashlib.sha256(im.tobytes()).hexdigest()
        if h not in unique:
            unique[h]=f'{len(unique):04d}.png';im.save(output/unique[h])
        frames.append({'tick':tick,'rgb_sha256':h,'image':unique[h]})
    pipeline.write(output/'sequence.json',sequence)
    pipeline.write(output/'asset_bindings.json',assets)
    pipeline.write(output/'candidate.json',{'scene':scene,'branch':'explicit_condition_tape',
        'status':'unverified_source_candidate','inputs':inputs,'conditions':pipeline.bind(spec_path),
        'condition_claim':spec['entry_state']['provenance'],'frames':frames,
        'opening_frame':{'image':'opening.png','rgb_sha256':hashlib.sha256(opening.tobytes()).hexdigest(),'tick':-1},
        'media_policy':'untimed_stills_only','timing_claim':'no_duration_assigned',
        'playback_entrypoint':'pm2_activity_pipeline.py --playback-directory',
        'authorization_effect':'none'})
    return {'scene':scene,'ticks':len(frames),'unique_frames':len(unique),'output':str(output)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scene',required=True);p.add_argument('--conditions',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(replay(a.scene,a.conditions,a.output)))
