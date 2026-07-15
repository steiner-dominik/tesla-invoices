"""Atomic, lock-protected access to the JSON metadata sidecar files.

Three writers touch the same sidecar files — the downloader (during a sync),
the email exporter, and the PDF re-scan. Their read-modify-write cycles must
not interleave (a lost ``email_sent`` flag means a duplicate email), and a
crash mid-write must never leave a truncated file behind (a corrupt sidecar
is treated as "never emailed", again causing duplicates).
"""

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)

# One process-wide lock: sidecar updates are tiny and rare, so per-file
# locking would buy nothing but bookkeeping.
_LOCK = threading.RLock()


def read_json(path: Path) -> dict:
    """The stored JSON object, or {} for a missing/unreadable/corrupt file."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        logger.warning(f"Could not parse {path.name}, treating it as empty")
        return {}
    return data if isinstance(data, dict) else {}


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Write via a temp file + rename, so readers never see a partial file.

    The temp file lives in the target directory (rename must not cross
    filesystems) and starts with a dot, which every file listing in the app
    treats as internal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
            # mkstemp creates the file 0600; the rename keeps that, which
            # makes invoices unreadable for other users/apps when the
            # directory is shared (e.g. an SMB mount off Home Assistant).
            # These are invoices and their metadata, not secrets — token
            # files are written elsewhere and stay 0600.
            os.fchmod(tmp.fileno(), 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def write_json_atomic(path: Path, data: dict) -> None:
    write_bytes_atomic(path, json.dumps(data, indent=4, sort_keys=True).encode())


def update_json(path: Path, updates: dict, remove: Iterable[str] = ()) -> bool:
    """Merge ``updates`` into the JSON file (dropping any ``remove`` keys)
    as one locked read-modify-write. Keys written by other components
    survive, and unchanged files are not rewritten (spares flash storage).
    Returns True when the file was actually written.
    """
    removed = set(remove)
    with _LOCK:
        existing = read_json(path)
        merged = {k: v for k, v in {**existing, **updates}.items() if k not in removed}
        if merged == existing and path.exists():
            return False
        write_json_atomic(path, merged)
        return True
