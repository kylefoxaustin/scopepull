"""SQLite manifest: the source of truth for "already local?".

A manifest row for an observation exists only after its archive directory has
been fully verified and atomically renamed into place (ingest.py, Phase 2).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from .config import manifest_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    obs_id      TEXT PRIMARY KEY,      -- the scope's vpath
    target      TEXT NOT NULL,
    started_at  TEXT,                  -- ISO 8601 UTC, NULL if scope gave none
    frame_count INTEGER NOT NULL,
    format      TEXT NOT NULL,
    dir         TEXT NOT NULL,         -- archive dir, relative to archive root
    bytes       INTEGER NOT NULL,
    pulled_at   TEXT NOT NULL          -- ISO 8601 UTC
);
CREATE TABLE IF NOT EXISTS files (
    obs_id  TEXT NOT NULL REFERENCES observations(obs_id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,             -- relative to the observation dir
    sha256  TEXT NOT NULL,
    size    INTEGER NOT NULL,
    PRIMARY KEY (obs_id, relpath)
);
"""


@dataclass(frozen=True)
class PulledObservation:
    obs_id: str
    target: str
    started_at: str | None
    frame_count: int
    format: str
    dir: str
    bytes: int
    pulled_at: str


class Manifest:
    def __init__(self, db_path: Path | None = None) -> None:
        path = db_path or manifest_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    def __enter__(self) -> Manifest:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- queries --------------------------------------------------------------

    def is_local(self, obs_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
        return row is not None

    def local_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT obs_id FROM observations")}

    def all_observations(self) -> list[PulledObservation]:
        rows = self._conn.execute(
            "SELECT obs_id, target, started_at, frame_count, format, dir, bytes, pulled_at "
            "FROM observations ORDER BY pulled_at"
        ).fetchall()
        return [PulledObservation(*r) for r in rows]

    def totals(self) -> tuple[int, int]:
        """(observation count, total bytes)."""
        n, b = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM observations"
        ).fetchone()
        return int(n), int(b)

    # -- mutations (called by ingest only, after the atomic rename) -----------

    def record(
        self,
        *,
        obs_id: str,
        target: str,
        started_at: datetime | None,
        frame_count: int,
        format: str,
        dir: str,
        files: list[tuple[str, str, int]],  # (relpath, sha256, size)
    ) -> None:
        total = sum(size for _, _, size in files)
        now = datetime.now(tz=UTC).isoformat(timespec="seconds")
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO observations "
                "(obs_id, target, started_at, frame_count, format, dir, bytes, pulled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs_id,
                    target,
                    started_at.isoformat(timespec="seconds") if started_at else None,
                    frame_count,
                    format,
                    dir,
                    total,
                    now,
                ),
            )
            self._conn.execute("DELETE FROM files WHERE obs_id = ?", (obs_id,))
            self._conn.executemany(
                "INSERT INTO files (obs_id, relpath, sha256, size) VALUES (?, ?, ?, ?)",
                [(obs_id, rp, sha, size) for rp, sha, size in files],
            )

    def forget(self, obs_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM observations WHERE obs_id = ?", (obs_id,))
