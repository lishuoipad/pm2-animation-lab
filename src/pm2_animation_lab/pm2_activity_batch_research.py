"""Exercise all fixed activity branches through the shared compiler/compositor.

Untimed PNG/JSON diagnostics only. Videos and playback must use the unified
pipeline with a bound request, observed clock and verified receipt.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import random
import time
from pm2_animation_lab import pm2_activity_timeline as timeline
from pm2_animation_lab import pm2_activity_pipeline as pipeline
from pm2_animation_lab.pm2_activity_scene_profiles import PROFILES

import os
RUNTIME=Path(os.environ.get('PM2_DATA_ROOT','pm2-data')).resolve()
SOURCE=Path(os.environ.get('PM2_SOURCE_ROOT',str(RUNTIME/'source'))).resolve()
ARCHIVE=Path(os.environ.get('PM2_ARCHIVE',str(RUNTIME/'inputs/PM2_DOSBoxPure.zip'))).resolve()
PALETTE=Path(os.environ.get('PM2_PALETTE',str(RUNTIME/'palette.json'))).resolve()


class RecordedSyntheticTape(timeline.RandomTape):
    def __init__(self, generator):
        super().__init__([])
        self.generator=generator

    def consume(self, bound, routine, line):
        self.values.append(self.generator.randint(1,bound))
        return super().consume(bound,routine,line)


def scenario(scene,branch,call_count,seed,start_weekday=None,include_graveyard_event=False):
    path=SOURCE/'KOSOTEXT'/(scene+'.TXT')
    timeline.verify_source_checkout(SOURCE,timeline.FIXED_SOURCE_COMMIT,scene)
    source=timeline.parse_source(path,scene)
    state=timeline._new_runtime(source)
    generator=random.Random(seed)
    init=RecordedSyntheticTape(generator)
    timeline.RestrictedInterpreter(source,state,init).execute_routine('ANIME_INIT')
    timeline._validate_runtime_call_boundary(state)
    calls=[]
    if scene == 'JOB009':
        # R_HUNTER calls ANMT_INTRO at line117, before the first daily result.
        tape=RecordedSyntheticTape(generator)
        routine=source.routines['ANMT_INTRO']
        timeline.execute_call_edge(source,state,tape,routine,'before')
        for tick in range(12):
            trace=timeline.TickTrace(tick)
            timeline.RestrictedInterpreter(source,state,tape,trace).execute_routine(routine.name,timeline._loop_body(routine))
            trace.finalize()
        timeline.execute_call_edge(source,state,tape,routine,'after')
        timeline._validate_runtime_call_boundary(state)
        calls.append({'branch':'hunting_intro','expected_state':None,'tick_count':12,'random_values':tape.values})
    for day in range(call_count):
        actual_branch='sunday' if start_weekday is not None and (start_weekday+day)%7==0 else branch
        tape=RecordedSyntheticTape(generator)
        selected=None
        if actual_branch!='sunday':
            success,hiko=timeline._branch_values(actual_branch,scene)
            state.scalars.update(SUCCESS_FLAG=success,S_HIKOUKA=hiko,S_BYOUKI=0)
            selector=timeline.RestrictedInterpreter(source,state,tape)
            selector.execute_routine('ANIME_WIDW')
            if len(selector.selected_states)!=1:
                raise timeline.UnsupportedSyntaxError('batch_selection_count')
            selected=selector.selected_states[0]['state']
            state.scalars['SLCANM']=selected
        routine=source.routines['ANMTSUNDAY' if actual_branch=='sunday' else 'ANMT001']
        timeline.execute_call_edge(source,state,tape,routine,'before')
        ticks=state.scalars['TIMELOPMAX']
        for tick in range(ticks):
            trace=timeline.TickTrace(tick)
            timeline.RestrictedInterpreter(source,state,tape,trace).execute_routine(routine.name,timeline._loop_body(routine))
            trace.finalize()
        timeline.execute_call_edge(source,state,tape,routine,'after')
        timeline._validate_runtime_call_boundary(state)
        calls.append({'branch':actual_branch,'expected_state':selected,'tick_count':ticks,'random_values':tape.values})
    if include_graveyard_event:
        if scene != 'JOB010':raise ValueError('Graveyard event requires JOB010')
        tape=RecordedSyntheticTape(generator);routine=source.routines['ANMT_MOOOON']
        timeline.execute_call_edge(source,state,tape,routine,'before')
        ticks=0
        while state.scalars['TIMELOP'] > 0:
            if ticks >= 128:raise ValueError('Graveyard event exceeded source bound')
            trace=timeline.TickTrace(ticks)
            timeline.RestrictedInterpreter(source,state,tape,trace).execute_routine(routine.name,timeline._loop_body(routine))
            trace.finalize();state.scalars['TIMELOP']-=1;ticks+=1
        timeline.execute_call_edge(source,state,tape,routine,'after')
        calls.append({'branch':'graveyard_event','expected_state':None,'tick_count':ticks,'random_values':tape.values})
    # Replay frozen inputs through the actual shared compiler; exploration is
    # never used as its own pixel correctness oracle.
    entry={'mode':'fresh_init_with_overrides','provenance':'Explicit synthetic source branch exercise; no actual RNG/calendar claim','overrides':[]}
    sequence=timeline.build_timeline_sequence(path,scene,initialization_random_values=init.values,
        entry_state=entry,calls=calls,source_commit=timeline.FIXED_SOURCE_COMMIT)
    return sequence,{'scene':scene,'branch':branch,'seed':seed,'initialization_random_values':init.values,
        'calls':calls,'entry_state':entry,'random_claim':'synthetic_research_conditions',
        'calendar_simulated':start_weekday is not None,'start_weekday_sunday_zero':start_weekday}


def build(root,scenes,call_count=16,seed=20260907,start_weekday=None,include_graveyard_event=False):
    root=Path(root).resolve()
    if not root.is_relative_to((RUNTIME/'experiments').resolve()) or root.exists():
        raise ValueError('Require a new external experiment output directory')
    root.mkdir(parents=True)
    inputs={'archive':pipeline.bind(ARCHIVE),'palette':pipeline.bind(PALETTE)}
    rows=[]
    for scene in scenes:
        for branch in timeline.SUPPORTED_BRANCHES:
            start=time.perf_counter();output=root/scene/branch
            output.mkdir(parents=True)
            try:
                sequence,conditions=scenario(scene,branch,call_count,seed,start_weekday,include_graveyard_event)
                images,assets=pipeline.compose(sequence,{'scene':scene,**inputs},RUNTIME,include_opening=True)
                opening=images.pop(0)
                opening.save(output/'opening.png')
                (output/'frames').mkdir()
                unique={};frames=[]
                for tick,im in enumerate(images):
                    digest=hashlib.sha256(im.tobytes()).hexdigest()
                    if digest not in unique:
                        name=f'frames/{len(unique):04d}.png';im.save(output/name);unique[digest]=name
                    frames.append({'tick':tick,'rgb_sha256':digest,'image':unique[digest]})
                pipeline.write(output/'conditions.json',conditions)
                pipeline.write(output/'sequence.json',sequence)
                pipeline.write(output/'asset_bindings.json',assets)
                manifest={'scene':scene,'branch':branch,'status':'unverified_source_candidate','inputs':inputs,
                    'source_sha256':PROFILES[scene]['source_sha256'],'frames':frames,'unique_frames':len(unique),
                    'opening_frame':{'image':'opening.png','rgb_sha256':hashlib.sha256(opening.tobytes()).hexdigest(),
                        'tick':-1,'source_phase':'ANIME_OPEN_background_before_daily_calls','timing_claim':'no_duration_assigned'},
                    'media_policy':'untimed_stills_only','timing_claim':'no_duration_assigned','native_comparison':None,
                    'playback_entrypoint':'pm2_activity_pipeline.py --playback-directory',
                    'authorization_effect':'none','elapsed_seconds':round(time.perf_counter()-start,3)}
                pipeline.write(output/'candidate.json',manifest)
                rows.append({k:manifest[k] for k in ('scene','branch','status','unique_frames','elapsed_seconds')})
            except Exception as error:
                row={'scene':scene,'branch':branch,'status':'failed','error':type(error).__name__+':'+str(error)}
                pipeline.write(output/'FAILED.json',row);rows.append(row)
            print(json.dumps(rows[-1]),flush=True)
    pipeline.write(root/'index.json',{'schema':1,'scenes':scenes,'rows':rows,'scope':'Source branch exercises; not native verification or natural gameplay'})
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--scene',action='append',choices=sorted(PROFILES))
    parser.add_argument('--calls',type=int,default=16,choices=range(1,41))
    parser.add_argument('--start-weekday',type=int,choices=range(7),help='Insert explicit Sundays in the continuous source state; Sunday=0. Other outcomes remain synthetic.')
    parser.add_argument('--seed',type=int,default=20260907,help='Explicit deterministic synthetic input seed, never a claim about the game RNG')
    parser.add_argument('--include-graveyard-event',action='store_true',help='Explicit JOB010 event condition, not an observed native trigger')
    args=parser.parse_args()
    results=build(args.output,args.scene or sorted(PROFILES),args.calls,seed=args.seed,start_weekday=args.start_weekday,include_graveyard_event=args.include_graveyard_event)
    raise SystemExit(any(row['status']=='failed' for row in results))
