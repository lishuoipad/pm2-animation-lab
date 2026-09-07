"""Fixed activity profiles; no second interpreter or pixel compositor."""
from __future__ import annotations

PROFILES = {
    "JOB003": {"source_sha256": "75682752be653048a693ee261c6a07897b7586a623e21217b23bf1f0b5d6bb34", "library": "J0.LBX", "patterns": "J004A.PT1", "background": "J004B.PT1"},
    "JOB008": {"source_sha256": "57f66cadf5fc7a8deafb23f9a4bba595d89125635f50014577a93bf54aca1934", "library": "J0.LBX", "patterns": "J009A.PT1", "background": "J009B.PT1"},
    "TRG004": {"source_sha256": "f9944d15a3511ececde01e10c8df3cf4c9b7c2b86df96fef12e1cb37f4a00c0f", "library": "T0.LBX", "patterns": "T005A.PT1", "background": "T005B.PT1"},
    "TRG005": {"source_sha256": "1f9747fe83d52e981294a8ba52e278679494a95e17e6dc79a5045c526064a7be", "library": "T0.LBX", "patterns": "T006A.PT1", "background": "T006B.PT1"},
}
COURSES = ("TRG004", "TRG005")

# Each row was audited from the fixed source's actual load instructions.
import json
from pathlib import Path
ALL_PROFILES = json.loads(Path(__file__).with_name('pm2_all_scene_profiles.json').read_text(encoding='utf-8'))['profiles']
PROFILES.update(ALL_PROFILES)
COURSES = tuple(name for name in PROFILES if name.startswith('TRG'))


def lower_fixed_source(source):
    """Expand only the audited static actor loop; keep source line provenance."""
    from pm2_animation_lab.pm2_activity_timeline import Routine, SourceLine, SourceIdentityError, UnsupportedSyntaxError
    import re
    profile = PROFILES[source.scene_id]
    if source.raw_sha256 != profile['source_sha256']:
        raise SourceIdentityError('fixed_scene_profile_changed')
    for name, routine in list(source.routines.items()):
        output = []
        lines = routine.lines
        i = 0
        while i < len(lines):
            token = re.sub(r'\s+', '', lines[i].text.split(';', 1)[0])
            if source.scene_id == 'JOB009' and name == 'ANMT_INTRO' and token == 'C1=5C2=7':
                body = [re.sub(r'\s+', '', line.text.split(';',1)[0]) for line in lines[i+1:i+5]]
                if body != ['ANIM_NUM(C2)APUT(ALOCX[C2],ALOCY[C2],DX)', 'ALOCC[C2]++', 'C2++', 'LOOPC1']:
                    raise UnsupportedSyntaxError('hunting_intro_actor_loop_shape')
                for track in range(7,12):
                    output.append(SourceLine(lines[i+1].number, '\tANIM_NUM(%d) APUT(ALOCX[%d],ALOCY[%d],DX)' % (track,track,track)))
                    output.append(SourceLine(lines[i+2].number, '\tALOCC[%d]++' % track))
                i += 5
                continue
            begin = re.fullmatch(r'C3=(\d+)C4=(\d+)', token)
            if begin:
                count, first = map(int, begin.groups())
                if not 1 <= count <= 16 or i + 3 >= len(lines):
                    raise UnsupportedSyntaxError('actor_loop_bounds')
                body = [re.sub(r'\s+', '', line.text.split(';',1)[0]) for line in lines[i+1:i+4]]
                if body != ['ANIM_NUM(C4)APUT(ALOCX[C4],ALOCY[C4],DX)', 'C4++', 'LOOPC3']:
                    raise UnsupportedSyntaxError('actor_loop_shape')
                output.extend(SourceLine(lines[i+1].number, '\tANIM_NUM(%d) APUT(ALOCX[%d],ALOCY[%d],DX)' % (track,track,track)) for track in range(first,first+count))
                i += 4
            else:
                output.append(lines[i]); i += 1
        source.routines[name] = Routine(routine.name, routine.label_line, tuple(output))
    capacity = profile['capacity']
    last = max(profile['active_tracks']) + 1
    for name, values in source.array_defaults.items():
        if name in ('ALOCX','ALOCY','ALOCC','ALOCF','ALOCCNT'):
            if len(values) != capacity:
                raise UnsupportedSyntaxError('source_track_capacity_changed')
            source.array_defaults[name] = values[:last]
    return source


def lower_course_loop(source):
    """Lower one hash-guarded fixed-count loop, preserving original line numbers.

    ANIM_NUM/APUT still run through the shared interpreter. Full file identity
    guards the helpers delegated there; modified or new courses fail closed.
    """
    from pm2_animation_lab.pm2_activity_timeline import Routine, SourceLine, SourceIdentityError, UnsupportedSyntaxError
    import re
    if source.raw_sha256 != PROFILES[source.scene_id]["source_sha256"]:
        raise SourceIdentityError("course_profile_changed")
    routine = source.routines["ANMT001"]
    output = []
    recognized = []
    for line in routine.lines:
        token = re.sub(r"\s+", "", line.text.split(";", 1)[0])
        if token in ("C3=4C4=3", "C4++", "LOOPC3"):
            recognized.append(token)
        elif token == "ANIM_NUM(C4)APUT(ALOCX[C4],ALOCY[C4],DX)":
            recognized.append("draw")
            output.extend(SourceLine(line.number, f"\tANIM_NUM({i}) APUT(ALOCX[{i}],ALOCY[{i}],DX)") for i in range(3, 7))
        else:
            output.append(line)
    if recognized != ["C3=4C4=3", "draw", "C4++", "LOOPC3"]:
        raise UnsupportedSyntaxError("course_fixed_loop")
    source.routines["ANMT001"] = Routine(routine.name, routine.label_line, tuple(output))
    # The fixed courses reserve 10/12 slots but initialize/use only 7/11.
    # Model only those source-proven tracks; do not weaken the shared complete
    # call-state validator to accept zero-length active tracks.
    active = 7 if source.scene_id == "TRG004" else 11
    capacity = 10 if source.scene_id == "TRG004" else 12
    for name, values in source.array_defaults.items():
        if len(values) != capacity:
            raise UnsupportedSyntaxError("course_reserved_track_capacity")
        source.array_defaults[name] = values[:active]
    return source
