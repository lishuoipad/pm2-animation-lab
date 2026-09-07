"""Bounded source-condition inference; never a video delivery entry.

Enumerates RANDAM operands accepted by the fixed interpreter and scores
continuous daily calls against already frozen, ordered native observations.
An observed-compatible input tape is a witness, not recovery of the real RNG.
Final candidates must be recompiled/composed by pm2_activity_pipeline.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory
import copy
from functools import lru_cache
import json
from pathlib import Path
import re
import time

import numpy as np
from PIL import Image
from pm2_animation_lab import pm2_activity_timeline as tl
from pm2_animation_lab.pm2_activity_pipeline import read_bound, bound, bind, write
from pm2_animation_lab.pm2_activity_scene_profiles import PROFILES
from pm2_animation_lab.pm2_lbx_pt1 import (read_lbx_asset_from_zip, parse_pt1, decode_record_planes,
    planes_to_indices, decode_mask_body_pair, apply_masked_pattern)
from pm2_animation_lab.pm2_activity_sequence_finish import rgb_to_indices
from pm2_animation_lab.pm2_activity_timed_comparison import native_observation, calibrated_groups


def enumerate_tapes(execute, *, limit=10000):
    """Discover conditional RNG domains from the interpreter, not a seed grid."""
    pending=[[]];produced=0
    while pending:
        values=pending.pop()
        try:
            result=execute(values)
        except tl.MissingInputError as error:
            m=re.search(r'random:(\d+):bound:(\d+)',str(error))
            if not m or int(m[1])!=len(values):raise
            maximum=int(m[2])
            if not 1<=maximum<=100 or len(values)>=12:
                raise ValueError('unreviewed_random_domain')
            pending.extend(values+[v] for v in range(maximum,0,-1))
            continue
        produced+=1
        if produced>limit:raise ValueError('random_domain_limit')
        yield values,result


def execute_day(source, entry, branch, values):
    state=copy.deepcopy(entry);tape=tl.RandomTape(list(values));selected=None
    if branch!='sunday':
        success,hiko=tl._branch_values(branch,source.scene_id)
        state.scalars.update(SUCCESS_FLAG=success,S_HIKOUKA=hiko)
        if source.scene_id.startswith('TRG'):
            state.scalars['S_BYOUKI']=0
        selector=tl.RestrictedInterpreter(source,state,tape)
        selector.execute_routine('ANIME_WIDW')
        if len(selector.selected_states)!=1:raise ValueError('source_selector_not_unique')
        selected=selector.selected_states[0]['state'];state.scalars['SLCANM']=selected
    routine=source.routines['ANMTSUNDAY' if branch=='sunday' else 'ANMT001']
    tl.execute_call_edge(source,state,tape,routine,'before')
    ticks=[]
    for i in range(state.scalars['TIMELOPMAX']):
        trace=tl.TickTrace(i)
        tl.RestrictedInterpreter(source,state,tape,trace).execute_routine(routine.name,tl._loop_body(routine))
        ticks.append(trace.finalize())
    tl.execute_call_edge(source,state,tape,routine,'after')
    tape.require_exhausted();tl._validate_runtime_call_boundary(state)
    return state,ticks,{'branch':branch,'expected_state':selected,
                       'tick_count':len(ticks),'random_values':list(values)}


class ResearchRaster:
    """Cache the existing mask primitive for searching, with no new blit math."""
    def __init__(self,request,root):
        profile=PROFILES[request['scene']]
        archive=bound(request['archive'],root)
        def records(name,library):
            raw,_=read_lbx_asset_from_zip(archive,library,name,expected_archive_sha256=request['archive']['sha256'])
            return parse_pt1(raw).records
        background=records(profile['background'],profile.get('background_library',profile['library']))[0]
        self.background=planes_to_indices(decode_record_planes(background),background.width_bytes,background.height_pixels)
        self.width=background.width_bytes*8;self.height=background.height_pixels
        ptn=records(profile['patterns'],profile['library'])
        self.patterns={i//2:decode_mask_body_pair(ptn[i],ptn[i+1]) for i in range(0,len(ptn),2)}
        if profile.get('foreground'):
            binding=profile['foreground']
            if binding['asset']!='J007BK.PT1' or request['scene'] not in ('JOB006','JOB009'):
                raise ValueError('condition_search_foreground_not_reviewed')
            front=records(binding['asset'],binding['library'])
            self.patterns[1000]=decode_mask_body_pair(front[0],front[1],mask_storage_plane_count=4)
        self.render=lru_cache(maxsize=4096)(self._render)

    def key(self,tick):
        return tuple((d['pattern_number'],d['author_coordinate']['x'],d['author_coordinate']['y'],
                      d.get('source_operation',{}).get('opcode',7)) for d in tick['draw_operations'] if d['emitted'])

    def _render(self,key):
        frame=self.background
        for number,x,y,opcode in key:
            frame=apply_masked_pattern(frame,self.width,self.height,self.patterns[number],
                anchor_x_pixels=x*8,anchor_y_pixels=y,include_author_offset=opcode==17,clip=True)
        return frame


def infer(request_path, output, root, source_root, *, beam_width=256):
    root, source_root = Path(root).resolve(), Path(source_root).resolve()
    if (not is_data_directory(root) or not source_root.is_dir()):
        raise ValueError('existing_data_and_source_directories_required')
    if 'game' in {part.lower() for part in Path(output).resolve().parts}:
        raise ValueError('game_build_inputs_forbidden')
    request=read_bound(bind(request_path),root)
    scene=request['scene'];output=Path(output)
    if scene not in PROFILES or scene=='JOB009':
        raise ValueError('scene_requires_condition_search_review')
    from pm2_animation_lab.pm2_activity_clock import validate_schedule
    days=validate_schedule(request['schedule'])
    tick_count=len(days)*5
    if type(beam_width) is not int or not 1<=beam_width<=2048:
        raise ValueError('invalid_beam_width')
    if output.exists() or not output.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('new_quarantined_search_output_required')
    observation,index,ip=native_observation(request['observation'],root)
    # Reject missing, duplicate or reordered ticks before an expensive search.
    calibrated_groups([{}]*tick_count,observation,index)
    tl.verify_source_checkout(Path(source_root),tl.FIXED_SOURCE_COMMIT,scene)
    source_path=Path(source_root)/'KOSOTEXT'/(scene+'.TXT')
    source=tl.parse_source(source_path,scene)
    raster=ResearchRaster(request,root)
    palette=read_bound(request['palette'],root)['entries']
    targets=[None]*tick_count
    for group in observation['groups']:
        row=index['frames'][group['stable_first_frame']]
        ref=index['unique_images'][row['rgb_sha256']]
        with Image.open(ip.parent/ref['path']) as im:
            pixels=rgb_to_indices(im.convert('RGB'),palette)
        for tick in group['ticks']:targets[tick]=np.frombuffer(pixels,dtype=np.uint8)
    if any(t is None for t in targets):raise ValueError('ordered_target_missing_tick')
    def initialize(values):
        state=tl._new_runtime(source);tape=tl.RandomTape(list(values))
        tl.RestrictedInterpreter(source,state,tape).execute_routine('ANIME_INIT');tape.require_exhausted()
        if state.scalars['TIMELOPMAX']!=5:raise ValueError('unreviewed_daily_tick_count')
        return state
    started=time.monotonic();output.mkdir(parents=True)
    hypotheses=[{'initial':values,'state':state,'calls':[],'cost':0} for values,state in enumerate_tapes(initialize)]
    write(output/'inputs.json',{'request':bind(request_path),'source':bind(source_path),'observation':request['observation'],
        'initial_hypotheses':len(hypotheses),'beam_width':beam_width,
        'algorithm':'interpreter_RANDAM_domains_then_bounded_continuous_state_beam',
        'claim':'observed_compatible_inputs_not_actual_RNG_or_global_optimum'})
    print(json.dumps({'scene':scene,'initial_hypotheses':len(hypotheses)}),flush=True)
    for day in range(len(days)):
        weekday=(observation['start_weekday']+day)%7
        branches=['sunday'] if weekday==0 else ['success','failure','mischief']
        next_states={};expanded=0;cutoff=float('inf');best=float('inf')
        # Preserve invisible initialization choices through an initial Sunday.
        width=len(hypotheses) if day==0 and weekday==0 else beam_width
        score_cache={}
        for entry in hypotheses:
            if entry['cost']>cutoff:continue
            for branch in branches:
                for values,(state,ticks,call) in enumerate_tapes(lambda tape:execute_day(source,entry['state'],branch,tape),limit=256):
                    expanded+=1;cost=entry['cost']
                    for local,tick in enumerate(ticks):
                        key=raster.key(tick);score_key=(local,key)
                        if score_key not in score_cache:
                            frame=np.frombuffer(raster.render(key),dtype=np.uint8)
                            score_cache[score_key]=int(np.count_nonzero(frame!=targets[day*5+local]))
                        cost+=score_cache[score_key]
                        if cost>cutoff:break
                    if cost>cutoff:continue
                    state_key=json.dumps(state.clone_complete_numeric_state(),sort_keys=True,separators=(',',':'))
                    prior=next_states.get(state_key)
                    if prior is None or cost<prior['cost']:
                        next_states[state_key]={'initial':entry['initial'],'state':state,'calls':entry['calls']+[call],'cost':cost}
                    best=min(best,cost)
                    if len(next_states)>width*2:
                        ranked=sorted(next_states.items(),key=lambda kv:kv[1]['cost'])[:width]
                        next_states=dict(ranked);cutoff=ranked[-1][1]['cost']
        hypotheses=sorted(next_states.values(),key=lambda h:h['cost'])[:width]
        if not hypotheses:raise ValueError('no_source_condition_hypothesis')
        checkpoint={'scene':scene,'day':day,'expanded':expanded,'kept':len(hypotheses),'best_cost':hypotheses[0]['cost'],
            'best_initial':hypotheses[0]['initial'],'best_states':[c['expected_state'] for c in hypotheses[0]['calls']],
            'render_cache':raster.render.cache_info()._asdict(),'seconds':time.monotonic()-started}
        write(output/f'day_{day:02d}.json',checkpoint)
        print(json.dumps(checkpoint),flush=True)
    best=hypotheses[0]
    candidate={'initialization_random_values':best['initial'],
        'entry_state':{'mode':'fresh_init_with_overrides','provenance':'Source-domain bounded search against fixed ordered capture; compatible witness, actual RNG unknown','overrides':[]},
        'calls':best['calls']}
    write(output/'conditions.json',candidate)
    # Independent of the exploratory renderer, replay every input through the
    # ordinary full compiler/compositor before using it in a movie request.
    from pm2_animation_lab.pm2_activity_pipeline import compile_sequence,compose
    replay=copy.deepcopy(request);replay['initialization_random_values']=best['initial'];replay['entry_state']=candidate['entry_state']
    replay['schedule']['days']=[dict(ordinal=i,**{k:c[k] for k in ('branch','expected_state','random_values')}) for i,c in enumerate(best['calls'])]
    sequence,_=compile_sequence(replay,Path(source_root));images,_=compose(sequence,replay,root)
    checked_cost=0
    for i,(tick,im) in enumerate(zip(sequence['ticks'],images)):
        indexed=rgb_to_indices(im,palette)
        if indexed!=raster.render(raster.key(tick)):
            raise ValueError('search_renderer_disagrees_with_shared_compositor')
        checked_cost+=int(np.count_nonzero(np.frombuffer(indexed,dtype=np.uint8)!=targets[i]))
    if checked_cost!=best['cost']:raise ValueError('search_cost_does_not_replay')
    write(output/'request.json',replay)
    result={'scene':scene,'status':'condition_witness_replayed','cost':checked_cost,
        'mean_different_pixel_percent':checked_cost/(tick_count*320*128)*100,
        'equal_cost_retained_states':sum(h['cost']==best['cost'] for h in hypotheses),
        'uniqueness':'not_proven','actual_rng':'unknown','source_ticks':len(images),
        'request':bind(output/'request.json'),'seconds':time.monotonic()-started,'authorization_effect':'none'}
    write(output/'result.json',result);return result
