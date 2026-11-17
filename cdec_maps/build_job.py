"""Support for running a cache build out-of-band (e.g. as a scheduled job).

The cache is a directory of per-series Parquet files read by the web app, so a
build running elsewhere must never leave the app a half-written file, must not
overlap with another build, and should say how it went.

* :func:`atomic_to_parquet` writes through a temp file and renames it into place.
* :class:`BuildLock` keeps a second build from starting while one is running; the
  holder refreshes it with :meth:`BuildLock.heartbeat`, so a crashed build's lock
  goes stale instead of blocking forever.
* :func:`run_locked_build` wraps a build function with the lock and records
  ``build_status.json`` in the cache directory (state, timings, counts).

This module is intentionally identical in ``cdec_maps`` and ``usgs_maps``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

LOCK_NAME = ".build.lock"
STATUS_NAME = "build_status.json"
STALE_AFTER_SECONDS = 30 * 60
# Individual series legitimately fail (no data, bad responses); a build where more
# than this fraction fails points at the environment (network, blocked API).
MAX_FAILED_FRACTION = 0.5


class BuildLockedError(RuntimeError):
    """Another build holds the lock."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path, write):
    """Call ``write(tmp)`` then rename tmp over *path*; readers never see a partial file."""
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_to_parquet(df, path):
    _atomic_write(path, df.to_parquet)


def atomic_to_csv(df, path, **kwargs):
    _atomic_write(path, lambda tmp: df.to_csv(tmp, **kwargs))


def _write_json(path, payload):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


class BuildLock:
    """Exclusive build lock in the cache directory (``O_EXCL`` file, refreshed by heartbeat)."""

    def __init__(self, dbase_dir, stale_after=STALE_AFTER_SECONDS):
        self.path = os.path.join(dbase_dir, LOCK_NAME)
        self.stale_after = stale_after
        self._held = False

    def _is_stale(self):
        try:
            return time.time() - os.path.getmtime(self.path) > self.stale_after
        except OSError:
            return False  # vanished meanwhile; the next attempt will take it

    def acquire(self):
        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if attempt == 0 and self._is_stale():
                    logger.warning("Removing stale build lock %s", self.path)
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                raise BuildLockedError(f"Another build holds {self.path}") from None
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"host": socket.gethostname(), "pid": os.getpid(), "started_at": _now()}, f)
            self._held = True
            return self
        raise BuildLockedError(f"Could not take {self.path}")

    def heartbeat(self):
        if self._held:
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self):
        if self._held:
            self._held = False
            try:
                os.unlink(self.path)
            except OSError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def read_status(dbase_dir):
    """Return the last recorded build status, or ``None``."""
    try:
        with open(os.path.join(dbase_dir, STATUS_NAME), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_locked_build(dbase_dir, build, name="cache"):
    """Run ``build(heartbeat)`` under the build lock and record its outcome.

    *build* returns a dict that may contain ``ok``, ``failed`` and ``failed_items``.
    The status file is rewritten at start and finish. Raises :class:`BuildLockedError`
    if another build is running, and ``RuntimeError`` (state ``failed``) when more
    than :data:`MAX_FAILED_FRACTION` of the series failed, so a job runner can flag it.
    """
    os.makedirs(dbase_dir, exist_ok=True)
    status_path = os.path.join(dbase_dir, STATUS_NAME)
    started = time.monotonic()
    status = {"name": name, "host": socket.gethostname(), "started_at": _now()}
    with BuildLock(dbase_dir) as lock:
        _write_json(status_path, {**status, "state": "running"})
        try:
            result = build(lock.heartbeat) or {}
        except BaseException as exc:
            _write_json(
                status_path,
                {**status, "state": "failed", "finished_at": _now(), "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        ok, failed = int(result.get("ok", 0)), int(result.get("failed", 0))
        total = ok + failed
        too_many = total > 0 and failed / total > MAX_FAILED_FRACTION
        _write_json(
            status_path,
            {
                **status,
                "state": "failed" if too_many else "success",
                "finished_at": _now(),
                "elapsed_seconds": round(time.monotonic() - started),
                "ok": ok,
                "failed": failed,
                "failed_items": list(result.get("failed_items", []))[:200],
            },
        )
        logger.info("%s build finished: %d ok, %d failed", name, ok, failed)
        if too_many:
            raise RuntimeError(f"{failed} of {total} series failed; see {status_path}")
    return result
