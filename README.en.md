# PM2 Animation Lab

An independent command-line toolkit for source-driven reconstruction of
Princess Maker 2 course/job animations and ordered native-frame verification.

Version 0.2 adds free-time illustrations, the 32 age/season/destination vacation
selectors, and an eastern adventure map adapter. `capture` runs an explicitly
supplied DOSBox Pure core without a visible frontend, restores an anchor and
replays bounded emulated mouse inputs. It saves native PNG/JSON only; `pipeline`
remains the sole animation publication/playback entry. See [scene types](docs/scene-types.md).

It supports 25 fixed scene profiles, continuous daily state, explicit random
inputs, LBX/PT1 decoding, native RGB/PTS comparison, and verified playback.
The source interpreter, compositor, publication gate and playback gate are
shared. Diagnostics do not produce unofficial fixed-duration animations.

## Install

Python 3.11+ is required. Install from a checkout with `python -m pip install .`,
then run `pm2-animation-lab doctor`. Git and FFmpeg are separate dependencies.
You can also run `python -m pm2_animation_lab`.

No original game scripts, assets, media, save files or emulator binaries are
included or automatically downloaded. Supply authorized local copies. The
supported source checkout is pinned to
`ec7bdef58357185fe5344973c156b857a5de2c1f`; scene hashes are verified separately.

Keep private data outside this repository and pass explicit absolute paths:

```sh
pm2-animation-lab pipeline --request /data/requests/activity.json --output /data/deliveries/new --quarantine-root /data --source-root /sources/pm2
pm2-animation-lab pipeline --request /data/requests/activity.json --playback-directory /data/deliveries/new --quarantine-root /data --source-root /sources/pm2
```

The second command returns bound media only after rechecking the exact request,
source state, images and publication receipt. Other commands include `scenes`,
`bind`, `index-video`, `observe`, and `infer`. Use `--help` for each command.

The [Chinese workflow guide](docs/workflow.md) documents all required inputs.
Templates contain placeholders, not playable game conditions.

## Scope of equality

The prior 25-scene experiment verified 15,122 native frames and 1,242 source
ticks. This is a bounded reconstruction of specified captured intervals with
source-compatible conditions, not recovery of the actual RNG, all possible
game states, or an independent hardware timing prediction. Native PTS are
retained exactly; GIF/MP4 viewing copies round cumulative boundaries within
5ms. MP4 pixels are lossy and are not used as the equality oracle.

The new local representative intervals cover 321 native frames (115 free-time,
149 vacation, 57 eastern-map movement). Their equality is conditioned on explicit
RGB palettes, observed state boundaries, and one source-ordered SVGA scanout fit.
This is not coverage of every vacation image or every adventure branch. The
headless input sequence also reproduced all 3,097 native frames on replay under
the same local core/options/anchor. Cross-platform CI uses synthetic inputs and
does not certify native emulator behavior on other systems.

## Test

```sh
python -m unittest discover -s tests -p 'test_*.py'
python tools/check_public_tree.py
```

Tests needing the fixed external source skip unless `PM2_SOURCE_ROOT` is set.
CI uses synthetic fixtures; it never fetches original game material.

This unofficial project is licensed under MIT for its own implementation,
documentation and original tests only. See [NOTICE.md](NOTICE.md).
