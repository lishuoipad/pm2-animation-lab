"""Installed command; all video production and playback use the same pipeline."""
from __future__ import annotations
import argparse
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sys
from . import __version__


def emit(value):print(json.dumps(value,ensure_ascii=False,indent=2))


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    commands={'pipeline':'pm2_activity_pipeline','index-video':'pm2_activity_video_index',
              'observe':'pm2_activity_observation', 'capture':'pm2_native_capture'}
    if args and args[0] in commands:
        return importlib.import_module('.'+commands[args[0]],__package__).main(args[1:])
    parser=argparse.ArgumentParser(description='PM2 Animation Lab: source reconstruction and ordered native verification')
    parser.add_argument('--version',action='version',version=__version__)
    subs=parser.add_subparsers(dest='command',required=True)
    subs.add_parser('pipeline',help='Publish or verify playback using a bound request (pipeline --help)')
    subs.add_parser('index-video',help='Decode and verify a native video index (index-video --help)')
    subs.add_parser('observe',help='Freeze a strict independent oracle (observe --help)')
    subs.add_parser('capture',help='Run a bound headless native input sequence; PNG/JSON capture only (capture --help)')
    subs.add_parser('doctor',help='Check installation, optional tools and bundled profiles')
    subs.add_parser('scenes',help='List hash-guarded activities and additional scene structures as JSON')
    b=subs.add_parser('bind',help='Print an absolute path and SHA-256 binding for a local file')
    b.add_argument('path',type=Path)
    inf=subs.add_parser('infer',help='Search source-compatible conditions; outputs JSON/stills only')
    inf.add_argument('--request',required=True,type=Path)
    inf.add_argument('--output',required=True,type=Path)
    inf.add_argument('--quarantine-root',required=True,type=Path)
    inf.add_argument('--source-root',required=True,type=Path)
    inf.add_argument('--beam-width',type=int,default=256)
    a=parser.parse_args(args)
    try:
        if a.command=='doctor':
            from .pm2_activity_scene_profiles import PROFILES
            emit({'version':__version__,'python':sys.version.split()[0],
                  'dependencies':{p:version(p) for p in ('Pillow','numpy')},
                  'scene_profiles':len(PROFILES),'git_available':shutil.which('git') is not None,
                  'additional_publishable_scene_types':3,'headless_capture':'mouse-only native PNG/JSON; external core required',
                  'ffmpeg_available_on_path':shutil.which('ffmpeg') is not None,
                  'original_game_files_bundled':False,'native_validation':'requires user-supplied source, assets and observations'})
        elif a.command=='scenes':
            from .pm2_activity_scene_profiles import PROFILES
            from .pm2_scene_sources import SCENES
            emit(dict(PROFILES, **SCENES))
        elif a.command=='bind':
            from .pm2_activity_pipeline import bind
            emit(bind(a.path))
        elif a.command=='infer':
            from .pm2_activity_condition_search import infer
            emit(infer(a.request,a.output,a.quarantine_root,a.source_root,beam_width=a.beam_width))
        return 0
    except (ValueError,OSError,KeyError) as error:
        print('PM2_LAB_REJECTED:'+str(error),file=sys.stderr)
        return 2
