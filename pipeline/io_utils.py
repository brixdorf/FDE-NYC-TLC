"""Small file helpers shared by every stage: atomic writes and checksums."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import pandas as pd


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_replace(destination: Path, write) -> None:
    """Write to a temp file in the same folder, then rename over the target.

    A crash mid-write leaves the previous file intact, never a half-written one.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    os.close(fd)
    try:
        write(Path(tmp))
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_json(obj, destination: Path) -> None:
    def _write(tmp: Path) -> None:
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")

    _atomic_replace(destination, _write)


def write_csv(df: pd.DataFrame, destination: Path) -> None:
    _atomic_replace(destination, lambda tmp: df.to_csv(tmp, index=False, lineterminator="\n"))


def write_text(text: str, destination: Path) -> None:
    _atomic_replace(destination, lambda tmp: tmp.write_text(text, encoding="utf-8"))


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
