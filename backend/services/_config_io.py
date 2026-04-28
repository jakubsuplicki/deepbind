"""Shared `app/config.json` IO helpers — atomic writes + lost-update protection.

Used by `set_active_local_model()` and `clear_active_local_model()` in
`ollama_service`. Each does read-modify-write of the entire config. Two
correctness concerns the writers share:

1. **Crash atomicity** — a process crash mid-write must not leave a truncated
   `config.json`. `atomic_write_json()` writes to a temp file, fsyncs the
   content to disk, then `os.replace` to the target (atomic rename). After
   the rename, fsync the parent directory so the rename itself survives a
   power loss. Best-effort on filesystems / OSes that don't support dir-fsync.

2. **Lost-update race** — two writers running concurrently could both load
   the same starting state, both write back their version, and one would
   silently lose. `locked_config_update()` wraps the read-modify-write in a
   POSIX `fcntl.flock` so writers serialize through the lock.

Windows lacks `fcntl.flock` and using `msvcrt.locking` requires file-handle
shenanigans this codebase isn't ready to invest in; today's primary deployment
target per ADR 003 is macOS (Tauri) with Linux secondary, so the lock degrades
to a no-op on Windows with a documented narrow race window. Profile / model
toggle conflicts are infrequent UI-driven actions; the user can re-apply if
it ever bites.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically against process crash and (best-effort) power loss.

    Steps:
      1. Write to `<path>.tmp`
      2. fsync the temp file (commits content to disk)
      3. `os.replace` to target (atomic at the directory-entry level)
      4. fsync the parent directory (commits the rename through to disk)

    Step 4 is best-effort: not all filesystems / OSes support directory fsync,
    so we swallow OSError there. Steps 1-3 are required for correctness; step
    4 strengthens "atomic against process crash" toward "atomic against power
    loss" but isn't a hard guarantee.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Directory fsync isn't supported on every OS / FS combination
        # (Windows, some network mounts). The rename itself is durable
        # enough on those platforms; skip silently.
        pass


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive process-level lock on `lock_path`.

    POSIX: `fcntl.flock(LOCK_EX)` — blocks other writers until released.
    Windows: best-effort no-op. The race window is microseconds (read +
    in-memory mutation + atomic rename); the impact of the rare race is
    one lost UI-driven config toggle that the user can re-apply. A future
    Tauri-side native helper can close the gap on Windows if it matters.
    """
    if sys.platform == "win32":
        yield
        return

    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_config_update(config_path: Path) -> Iterator[Dict[str, Any]]:
    """Read-modify-write a JSON config under a file lock.

    Usage:
        with locked_config_update(config_path) as config:
            config["active_profile_id"] = profile_id

    The file is read inside the lock, the caller mutates the yielded dict,
    and an atomic write happens on context exit — but only if the dict
    actually changed. Skip-on-unchanged keeps mtime stable for any future
    mtime-based caches reading the same file.

    Exceptions inside the block abort the write and re-raise — the file is
    unchanged, the lock is released cleanly.
    """
    lock_path = config_path.with_suffix(config_path.suffix + ".lock")
    with _file_lock(lock_path):
        config: Dict[str, Any] = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
            except (json.JSONDecodeError, IOError) as exc:
                # Corrupt config: log and start from empty rather than
                # propagate. The atomic write below replaces the corrupted
                # file with valid JSON.
                logger.warning("Config %s unreadable, replacing: %s", config_path, exc)

        before = json.dumps(config, sort_keys=True)
        yield config
        after = json.dumps(config, sort_keys=True)
        if after != before:
            atomic_write_json(config_path, config)
