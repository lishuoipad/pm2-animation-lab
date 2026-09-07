# Scope and provenance

This repository contains independently implemented research tools, file-format
parsers, validation metadata and synthetic test fixtures. It was extracted
from the author's research tooling; see `provenance.json` for the source
revision and original tool-file hashes.

The MIT license applies to this repository's tool implementation and original
documentation/tests. It does not grant rights to Princess Maker 2, its source
code, game data, artwork, recordings, music, trademarks or other third-party
materials. Those materials are not distributed here. Supply your own authorized
local inputs. The project is unofficial and is not affiliated with the game's
rights holders.

Scene identifiers, asset filenames, source line references and hashes are
interoperability/validation metadata. Source-language operation names in the
parser describe the supported input grammar; complete original scripts and
animation frame tables are read from external user-supplied files.

Pillow and NumPy are installed separately under their respective licenses.
FFmpeg, Git, emulator binaries and optional zstandard are external dependencies;
none of their binaries are bundled in this repository.
