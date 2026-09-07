"""Portable explicit data roots; decoded materials remain outside tool code."""
from pathlib import Path


def code_root():
    package=Path(__file__).resolve().parent
    for parent in package.parents:
        if (parent/'pyproject.toml').is_file() and (parent/'src/pm2_animation_lab').is_dir():
            return parent
    return package


def is_data_directory(path):
    root=Path(path).resolve()
    # Installed commands must also recognize an external checkout, not only
    # their own site-packages directory.
    if any((p/'pyproject.toml').is_file() and (p/'src/pm2_animation_lab').is_dir()
           for p in (root,*root.parents)):
        return False
    return (root.is_dir() and root!=Path(root.anchor) and root!=Path.home().resolve()
            and not root.is_relative_to(code_root()))
