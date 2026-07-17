"""Crash-safe file writes (QA1-11 — the no-UPS discipline).

``tmp.write + rename`` alone is not power-cut safe on ext4: without an fsync
the rename can land while the data blocks haven't, leaving a zero-length or
partial file. Every non-SQLite persistence path (paper account, config file)
must go through :func:`atomic_write_text`.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path | str, data: str) -> None:
    """Write *data* to *path* so that after any crash/power cut the file is
    either the old content or the new content — never truncated."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # fsync the directory so the rename itself is durable.
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
