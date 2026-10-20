"""KiCad footprint library lookup (read-only; no pcbnew needed)."""

import os
import re
from functools import cache
from pathlib import Path

_PAD = re.compile(r'\(pad\s+"([^"]*)"')


def library_dir() -> Path:
    return Path(os.environ.get("KICAD10_FOOTPRINT_DIR", "/usr/share/kicad/footprints"))


def footprint_file(lib_id: str) -> Path | None:
    lib, sep, name = lib_id.partition(":")
    if not sep or not lib or not name:
        return None
    path = library_dir() / f"{lib}.pretty" / f"{name}.kicad_mod"
    return path if path.is_file() else None


@cache
def pad_numbers(lib_id: str) -> frozenset[str] | None:
    """Pad numbers of a footprint, or None if the footprint does not exist."""
    path = footprint_file(lib_id)
    if path is None:
        return None
    return frozenset(n for n in _PAD.findall(path.read_text(encoding="utf-8")) if n)
