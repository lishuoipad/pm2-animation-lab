"""Forward-only source/native pose alignment for capture-calibrated clocks.

Never selects another source pose to reduce an error. Consecutive identical
source ticks can share one observed hold; all tick ordinals remain present.
Single native refreshes stay in the outgoing interval and must later pass
the unified publication's exact source-copy reconstruction.
"""
from __future__ import annotations
import hashlib
from pm2_animation_lab.pm2_activity_delivery_check import DeliveryError


def align(images, native_runs, first_frame, end_frame, *, include_opening=False):
    source=[]
    for i,image in enumerate(images):
        tick=i-1 if include_opening else i
        h=hashlib.sha256(image.tobytes()).hexdigest()
        if source and source[-1]['rgb_sha256']==h:
            source[-1]['ticks'].append(tick)
        else:
            source.append({'rgb_sha256':h,'ticks':[tick]})
    if (not native_runs or native_runs[0]['first_frame']!=first_frame
            or native_runs[-1]['end_frame_exclusive']!=end_frame
            or any(a['end_frame_exclusive']!=b['first_frame'] for a,b in zip(native_runs,native_runs[1:]))
            or any(r['end_frame_exclusive']<=r['first_frame'] for r in native_runs)):
        raise DeliveryError('alignment_requires_full_native_runs')
    stable=[r for r in native_runs if r['end_frame_exclusive']-r['first_frame']>=2]
    if len(source)!=len(stable):
        raise DeliveryError(f'ordered_pose_count:{len(source)}:{len(stable)}')
    if any(s['rgb_sha256']!=r['rgb_sha256'] for s,r in zip(source,stable)):
        raise DeliveryError('ordered_pose_pixels_differ')
    if stable[0]['first_frame']!=first_frame:
        raise DeliveryError('unexplained_initial_transition')
    groups=[];opening_end=first_frame;merged=False
    for i,(s,r) in enumerate(zip(source,stable)):
        end=stable[i+1]['first_frame'] if i+1<len(stable) else end_frame
        ticks=[t for t in s['ticks'] if t>=0]
        if not ticks:
            opening_end=end;continue
        merged=merged or -1 in s['ticks']
        groups.append({'ticks':ticks,'first_frame':r['first_frame'],
                       'stable_first_frame':r['first_frame'],'end_frame_exclusive':end})
    if not groups or [t for g in groups for t in g['ticks']]!=list(range(len(images)-int(include_opening))):
        raise DeliveryError('alignment_source_ticks_incomplete')
    return {'groups':groups,'opening_end_frame':opening_end,
            'opening_merged_with_first_sunday':merged,
            'alignment_claim':'forward_source_hash_equality_capture_calibrated_not_timing_prediction'}
