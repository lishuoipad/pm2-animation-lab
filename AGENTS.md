# Tool maintenance rules

- Keep the single `pm2_activity_pipeline.publish/playback` implementation and installed `pipeline` command. Other modules are diagnostics; do not add another animation encoder or playback route.
- Never bundle original game sources, assets, screenshots, recordings, audio, saves, ROMs, binaries or real captured request tapes. Tests and examples use original synthetic fixtures or structural placeholders only.
- Require explicit external data roots. Preserve resolved-path confinement, fixed source hashes, complete daily state, all source ticks, ordered RGB/PTS checks and encoded-media readback.
- Capture-conditioned clocks and copy-progress fits must not be described as independent timing prediction or actual RNG recovery.
- Run the test suite and public-tree check before publication. Test installed wheel resource loading as well as source-checkout imports. Optional real-source/media validation belongs outside the repository and must be labeled separately from synthetic CI.
- Before presenting media, run `pipeline --request <current request> --playback-directory <published directory>` and use only the returned `media` entries. Do not choose files by recency, filename or a previous playlist.
- New semantic or source-version support needs source evidence and regression coverage. Do not lower validation thresholds to make a new output pass.
