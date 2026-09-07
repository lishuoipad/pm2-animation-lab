"""Compare frozen native excerpts with explicit synthetic source candidates.

Nearest-frame retrieval diagnoses missing states/pixels. It does NOT validate
ordering, recover actual RNG, predict timing, or issue a delivery receipt.
"""
from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image
from pm2_animation_lab.pm2_activity_video_index import file_hash, confined, write_json
from pm2_animation_lab.pm2_activity_transition_copy import find_planar_copy_fits
from pm2_animation_lab.pm2_activity_sequence_finish import rgb_to_indices, synthesize_copy, source_chain


def nearest_pixels(actual, candidates):
    errors=np.count_nonzero(np.any(candidates != actual,axis=-1),axis=(1,2))
    return int(errors.argmin()), int(errors.min())


def compare(selection_path, output, candidate_roots, quarantine):
    root=Path(quarantine).resolve()
    if not is_data_directory(root):raise ValueError('External data directory required')
    source=confined(selection_path,root);selection=json.loads(source.read_text(encoding='utf-8'))
    if selection['selection_independent_of_candidate'] is not True:raise ValueError('Independent selection required')
    output=confined(output,root);output.mkdir(parents=True,exist_ok=False)
    reports=[]
    for sel in selection['selections']:
        scene=sel['scene'];ip=confined(sel['original_index'],root)
        idx=json.loads(ip.read_text(encoding='utf-8'))
        if not (idx.get('raw_stream_framehash_verified') or idx.get('crop_stream_framehash_verified')):
            raise ValueError('Unverified native decode')
        vid=confined(idx['video']['path'],root)
        if file_hash(vid)!=idx['video']['sha256']:raise ValueError('Native video changed')
        first,end=sel['first_frame'],sel['end_frame_exclusive']
        if not 0<=first<end<len(idx['frames']):raise ValueError('Observed boundaries unavailable')
        candidates={};sources=[]
        for candidate_root in candidate_roots:
            for p in sorted(confined(candidate_root,root).glob(scene+'/*/candidate.json')):
                manifest=json.loads(p.read_text(encoding='utf-8'));sources.append({'path':str(p),'sha256':file_hash(p)})
                for f in manifest['frames']+([manifest['opening_frame']] if 'opening_frame' in manifest else []):
                    image=confined(p.parent/f['image'],root)
                    if f['rgb_sha256'] in candidates:continue
                    arr=np.array(Image.open(image).convert('RGB'))
                    if hashlib.sha256(arr.tobytes()).hexdigest()!=f['rgb_sha256']:raise ValueError('Candidate RGB changed')
                    candidates[f['rgb_sha256']]={'path':str(image),'branch':manifest['branch'],'tick':f['tick'],'array':arr}
        if not candidates:raise ValueError('No successful candidates: '+scene)
        cand=list(candidates.values());stack=np.stack([x['array'] for x in cand]);memo={};runs=[]
        tb=idx['timebase'];origin=idx['frames'][first]['pts']
        exact=0;stable_exact=0;stable_total=0
        for run in idx['runs']:
            a,b=max(first,run['first_frame']),min(end,run['last_frame']+1)
            if a>=b:continue
            h=run['rgb_sha256']
            if h not in memo:
                ref=idx['unique_images'][h];p=confined(ip.parent/ref['path'],root)
                if file_hash(p)!=ref['sha256']:raise ValueError('Native PNG changed')
                actual=np.array(Image.open(p).convert('RGB'))
                if hashlib.sha256(actual.tobytes()).hexdigest()!=h:raise ValueError('Native RGB changed')
                n,err=nearest_pixels(actual,stack)
                memo[h]={'actual_image':str(p),'candidate_image':cand[n]['path'],
                    'candidate_branch':cand[n]['branch'],'candidate_tick':cand[n]['tick'],
                    'different_pixels':err,'pixel_count':actual.shape[0]*actual.shape[1]}
            match=memo[h];length=b-a
            if match['different_pixels']==0:exact+=length
            if length>=2:
                stable_total+=length
                if match['different_pixels']==0:stable_exact+=length
            runs.append({'first_frame':a,'end_frame_exclusive':b,
                'start_ms':(idx['frames'][a]['pts']-origin)*tb[0]*1000/tb[1],
                'end_ms':(idx['frames'][b]['pts']-origin)*tb[0]*1000/tb[1],**match})
        # Reuse the fixed-source planar-copy model. Only an isolated native
        # refresh between two exact source candidates is eligible; both source
        # endpoint pixels are frozen before fitting the observed copy position.
        from pm2_animation_lab.pm2_activity_batch_research import PALETTE, SOURCE
        palette=json.loads(PALETTE.read_text(encoding='utf-8'))['entries'];copy_frames=0
        source_refs=source_chain(SOURCE,scene)
        for n,run in enumerate(runs):
            if not run['different_pixels'] or run['end_frame_exclusive']-run['first_frame']!=1 or not 0<n<len(runs)-1:continue
            prev,nxt=runs[n-1],runs[n+1]
            if prev['different_pixels'] or nxt['different_pixels']:continue
            def indices(path):
                with Image.open(path) as im:return rgb_to_indices(im,palette)
            old,new=indices(prev['candidate_image']),indices(nxt['candidate_image'])
            fits=find_planar_copy_fits(old,indices(run['actual_image']),new,320,128)
            if fits:
                fit=fits[0];pixels=synthesize_copy(old,new,320,fit)
                rgb=b''.join(bytes(palette[v]) for v in pixels)
                with Image.open(run['actual_image']) as im:
                    if rgb!=im.convert('RGB').tobytes():raise ValueError('Planar reconstruction mismatch')
                path=output/(scene+'_refresh_'+str(n)+'.png');Image.frombytes('RGB',(320,128),rgb).save(path)
                run['copy_fit']={'seam_row':fit.seam_row,'completed_plane_count':fit.completed_plane_count,'boundary_x':fit.boundary_x}
                run['reconstructed_image']=str(path);copy_frames+=1
        report={'scene':scene,'status':'diagnostic_only','selection':sel,'source_selection':{'path':str(source),'sha256':file_hash(source)},
            'native_index':{'path':str(ip),'sha256':file_hash(ip)},'video':idx['video'],'candidate_manifests':sources,
            'frame_count':end-first,'exact_retrieval_frames':exact,'exact_retrieval_fraction':exact/(end-first),
            'multi_refresh_run_frames':stable_total,'exact_multi_refresh_run_frames':stable_exact,
            'native_unique_frames':len(memo),'candidate_unique_frames':len(cand),'runs':runs,
            'source_copy_explained_frames':copy_frames,'unexplained_frames':end-first-exact-copy_frames,
            'copy_model_source_chain':source_refs,'copy_fit_claim':'capture_conditioned_planar_copy_explanation_not_prediction',
            'claim':'Unordered nearest-frame retrieval across explicit synthetic conditions; neither temporal validation nor actual RNG recovery',
            'duration_ms':(idx['frames'][end]['pts']-origin)*tb[0]*1000/tb[1],
            'authorization_effect':'none'}
        write_json(output/(scene+'.json'),report);reports.append(report)
        print(json.dumps({k:report[k] for k in ('scene','frame_count','exact_retrieval_fraction','native_unique_frames','candidate_unique_frames')}),flush=True)
    write_json(output/'index.json',{'reports':[{'scene':r['scene'],'path':str(output/(r['scene']+'.json'))} for r in reports]})
    return reports


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--selection',required=True);p.add_argument('--output',required=True)
    p.add_argument('--candidate-root',action='append',required=True);p.add_argument('--quarantine',required=True)
    a=p.parse_args();compare(a.selection,a.output,a.candidate_root,a.quarantine)
