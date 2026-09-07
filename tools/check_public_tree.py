"""Reject accidental private data, binary assets or credentials in public files."""
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SKIP={'.git','.venv','venv','build','dist','__pycache__','.pytest_cache'}
ALLOWED={'.py','.json','.md','.toml','.yml','.yaml'}
NAMED={'LICENSE','.gitignore','MANIFEST.in'}


def inspect(paths):
    errors=[]
    rules=[('private_machine_path',re.compile(r'(?:[A-Z]:[/\\]Users[/\\]|[A-Z]:[/\\]Codex[/\\])',re.I)),
           ('credential',re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)'))]
    for path in paths:
        rel=path.relative_to(ROOT).as_posix()
        if path.name not in NAMED and path.suffix not in ALLOWED:
            errors.append(rel+': forbidden public file type');continue
        try:text=path.read_text(encoding='utf-8')
        except (UnicodeError,OSError):errors.append(rel+': not UTF-8 text');continue
        for label,pattern in rules:
            if pattern.search(text):errors.append(rel+': '+label)
    return errors


def main():
    # A nested checkout must not accidentally inspect the containing project.
    if (ROOT/'.git').exists():
        raw=subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z'])
        paths=[ROOT/name for name in raw.decode('utf-8').split('\0') if name]
    else:
        paths=[p for p in ROOT.rglob('*') if p.is_file() and not any(part in SKIP or part.endswith('.egg-info') for part in p.relative_to(ROOT).parts)]
    errors=inspect(paths)
    if not paths:errors.append('No tracked/public files found; stage the intended tree before checking')
    print(json.dumps({'status':'failed' if errors else 'passed','checked_files':len(paths),'errors':errors},indent=2))
    return bool(errors)


if __name__=='__main__':raise SystemExit(main())
