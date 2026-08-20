"""Couche d'accès aux données du mode Radio (SQLite via aiosqlite).

TOUTES les requêtes SQL du projet vivent ici. Les services (Library / Radio)
et le cog ne manipulent jamais SQL directement : ils passent par ``Database``.
Cet isolement prépare une future migration SQLite -> PostgreSQL : il suffira
de réécrire cette classe en gardant la même interface publique.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import aiosqlite

from .models import Track, TrackStatus, Vote

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    artist      TEXT    NOT NULL DEFAULT '',
    source_url  TEXT    NOT NULL DEFAULT '',
    spotify_url TEXT,
    youtube_url TEXT,
    added_by    INTEGER NOT NULL,
    added_at    INTEGER NOT NULL,
    likes       INTEGER NOT NULL DEFAULT 0,
    dislikes    INTEGER NOT NULL DEFAULT 0,
    play_count  INTEGER NOT NULL DEFAULT 0,
    last_played INTEGER,
    last_liked  INTEGER,
    status      TEXT    NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS votes (
    user_id  INTEGER NOT NULL,
    track_id INTEGER NOT NULL,
    vote     INTEGER NOT NULL,
    voted_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, track_id),
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS play_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id  INTEGER NOT NULL,
    played_at INTEGER NOT NULL,
    via_radio INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contributors (
    user_id        INTEGER PRIMARY KEY,
    tracks_added   INTEGER NOT NULL DEFAULT 0,
    likes_received INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE INDEX IF NOT EXISTS idx_tracks_status      ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_tracks_youtube     ON tracks(youtube_url);
CREATE INDEX IF NOT EXISTS idx_tracks_spotify     ON tracks(spotify_url);
CREATE INDEX IF NOT EXISTS idx_history_played_at  ON play_history(played_at);
CREATE INDEX IF NOT EXISTS idx_guild_settings_key ON guild_settings(key);
"""


def _now() -> int:
    return int(time.time())


class Database:
    """Façade asynchrone autour d'une connexion aiosqlite unique."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        # WAL : meilleures perfs en lecture/écriture concurrente, idéal Pi.
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA foreign_keys=ON;")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database non connectée — appeler connect() d'abord.")
        return self._db

    # ------------------------------------------------------------------
    # Settings (clé/valeur)
    # ------------------------------------------------------------------

    async def get_setting(self, key: str) -> Optional[str]:
        cur = await self.db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def get_guild_setting(self, guild_id: int, key: str) -> Optional[str]:
        cur = await self.db.execute(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_guild_setting(self, guild_id: int, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, value),
        )
        await self.db.commit()

    async def inherit_legacy_settings(self, guild_id: int) -> None:
        """Copie les anciens settings globaux vers un serveur, une seule fois."""
        cur = await self.db.execute("SELECT 1 FROM guild_settings LIMIT 1")
        if await cur.fetchone():
            return
        cur = await self.db.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
        for row in rows:
            await self.db.execute(
                "INSERT OR IGNORE INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?)",
                (guild_id, row["key"], row["value"]),
            )
        if rows:
            await self.db.commit()

    async def prune_play_history(self, keep: int = 2000) -> None:
        """Garde les ``keep`` lectures les plus récentes (carte SD / Pi)."""
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM play_history")
        row = await cur.fetchone()
        n = row["n"] if row else 0
        if n <= keep:
            return
        cur = await self.db.execute(
            "SELECT played_at FROM play_history ORDER BY played_at DESC LIMIT 1 OFFSET ?",
            (keep - 1,),
        )
        cutoff = await cur.fetchone()
        if cutoff is None:
            return
        await self.db.execute(
            "DELETE FROM play_history WHERE played_at < ?", (cutoff["played_at"],)
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Tracks
    # ------------------------------------------------------------------

    async def get_track(self, track_id: int) -> Optional[Track]:
        cur = await self.db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,))
        row = await cur.fetchone()
        return Track.from_row(row) if row else None

    async def find_track(
        self,
        *,
        youtube_url: Optional[str] = None,
        spotify_url: Optional[str] = None,
        title: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> Optional[Track]:
        """Recherche un morceau existant pour la déduplication.

        Priorité : URL YouTube > URL Spotify > (titre + artiste).
        """
        if youtube_url:
            cur = await self.db.execute(
                "SELECT * FROM tracks WHERE youtube_url = ?", (youtube_url,)
            )
            row = await cur.fetchone()
            if row:
                return Track.from_row(row)
        if spotify_url:
            cur = await self.db.execute(
                "SELECT * FROM tracks WHERE spotify_url = ?", (spotify_url,)
            )
            row = await cur.fetchone()
            if row:
                return Track.from_row(row)
        if title is not None:
            cur = await self.db.execute(
                "SELECT * FROM tracks WHERE LOWER(title) = LOWER(?) "
                "AND LOWER(artist) = LOWER(?)",
                (title, artist or ""),
            )
            row = await cur.fetchone()
            if row:
                return Track.from_row(row)
        return None

    async def create_track(
        self,
        *,
        title: str,
        artist: str,
        source_url: str,
        spotify_url: Optional[str],
        youtube_url: Optional[str],
        added_by: int,
    ) -> Track:
        now = _now()
        cur = await self.db.execute(
            "INSERT INTO tracks "
            "(title, artist, source_url, spotify_url, youtube_url, added_by, added_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, artist, source_url, spotify_url, youtube_url, added_by, now, TrackStatus.ACTIVE),
        )
        await self.db.commit()
        track = await self.get_track(cur.lastrowid)  # type: ignore[arg-type]
        assert track is not None
        return track

    async def set_track_status(self, track_id: int, status: str) -> None:
        await self.db.execute(
            "UPDATE tracks SET status = ? WHERE id = ?", (status, track_id)
        )
        await self.db.commit()

    async def get_tracks_by_status(self, *statuses: str) -> list[Track]:
        if not statuses:
            statuses = TrackStatus.ALL
        placeholders = ",".join("?" * len(statuses))
        cur = await self.db.execute(
            f"SELECT * FROM tracks WHERE status IN ({placeholders})", tuple(statuses)
        )
        rows = await cur.fetchall()
        return [Track.from_row(r) for r in rows]

    async def count_tracks_by_status(self, status: str) -> int:
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM tracks WHERE status = ?", (status,)
        )
        row = await cur.fetchone()
        return row["n"] if row else 0

    # ------------------------------------------------------------------
    # Lecture (play history + compteurs)
    # ------------------------------------------------------------------

    async def record_play(self, track_id: int, *, via_radio: bool) -> None:
        now = _now()
        await self.db.execute(
            "INSERT INTO play_history (track_id, played_at, via_radio) VALUES (?, ?, ?)",
            (track_id, now, 1 if via_radio else 0),
        )
        await self.db.execute(
            "UPDATE tracks SET play_count = play_count + 1, last_played = ? WHERE id = ?",
            (now, track_id),
        )
        await self.db.commit()

    async def recent_track_ids(self, limit: int) -> list[int]:
        """Renvoie les ``limit`` derniers ``track_id`` joués (du plus récent)."""
        cur = await self.db.execute(
            "SELECT track_id FROM play_history ORDER BY played_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [r["track_id"] for r in rows]

    # ------------------------------------------------------------------
    # Votes
    # ------------------------------------------------------------------

    async def get_vote(self, user_id: int, track_id: int) -> Optional[int]:
        cur = await self.db.execute(
            "SELECT vote FROM votes WHERE user_id = ? AND track_id = ?",
            (user_id, track_id),
        )
        row = await cur.fetchone()
        return row["vote"] if row else None

    async def set_vote(self, user_id: int, track_id: int, vote: int) -> Optional[int]:
        """Pose/modifie le vote d'un utilisateur (1 vote max par morceau).

        Renvoie l'ancien vote (ou ``None``). Recalcule les compteurs likes/
        dislikes du morceau et le ``likes_received`` du contributeur.
        """
        previous = await self.get_vote(user_id, track_id)
        now = _now()
        await self.db.execute(
            "INSERT INTO votes (user_id, track_id, vote, voted_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, track_id) DO UPDATE SET vote = excluded.vote, voted_at = excluded.voted_at",
            (user_id, track_id, vote, now),
        )
        await self._recompute_track_votes(track_id, set_last_liked=vote == Vote.LIKE)
        await self.db.commit()
        return previous

    async def clear_vote(self, user_id: int, track_id: int) -> Optional[int]:
        previous = await self.get_vote(user_id, track_id)
        if previous is None:
            return None
        await self.db.execute(
            "DELETE FROM votes WHERE user_id = ? AND track_id = ?", (user_id, track_id)
        )
        await self._recompute_track_votes(track_id, set_last_liked=False)
        await self.db.commit()
        return previous

    async def _recompute_track_votes(self, track_id: int, *, set_last_liked: bool) -> None:
        cur = await self.db.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN vote = 1 THEN 1 ELSE 0 END), 0) AS likes, "
            "COALESCE(SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END), 0) AS dislikes "
            "FROM votes WHERE track_id = ?",
            (track_id,),
        )
        row = await cur.fetchone()
        likes = row["likes"] if row else 0
        dislikes = row["dislikes"] if row else 0
        if set_last_liked:
            await self.db.execute(
                "UPDATE tracks SET likes = ?, dislikes = ?, last_liked = ? WHERE id = ?",
                (likes, dislikes, _now(), track_id),
            )
        else:
            await self.db.execute(
                "UPDATE tracks SET likes = ?, dislikes = ? WHERE id = ?",
                (likes, dislikes, track_id),
            )
        # Synchronise les likes reçus par le contributeur du morceau.
        cur = await self.db.execute(
            "SELECT added_by FROM tracks WHERE id = ?", (track_id,)
        )
        trow = await cur.fetchone()
        if trow is not None:
            await self._sync_contributor_likes(trow["added_by"])

    # ------------------------------------------------------------------
    # Contributeurs
    # ------------------------------------------------------------------

    async def increment_tracks_added(self, user_id: int) -> None:
        await self.db.execute(
            "INSERT INTO contributors (user_id, tracks_added) VALUES (?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET tracks_added = tracks_added + 1",
            (user_id,),
        )
        await self.db.commit()

    async def _sync_contributor_likes(self, user_id: int) -> None:
        cur = await self.db.execute(
            "SELECT COALESCE(SUM(likes), 0) AS total FROM tracks WHERE added_by = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        total = row["total"] if row else 0
        await self.db.execute(
            "INSERT INTO contributors (user_id, likes_received) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET likes_received = excluded.likes_received",
            (user_id, total),
        )

    # ------------------------------------------------------------------
    # Statistiques
    # ------------------------------------------------------------------

    async def top_tracks(self, limit: int = 5) -> list[Track]:
        cur = await self.db.execute(
            "SELECT * FROM tracks WHERE status != ? "
            "ORDER BY likes DESC, play_count DESC LIMIT ?",
            (TrackStatus.ARCHIVED, limit),
        )
        rows = await cur.fetchall()
        return [Track.from_row(r) for r in rows]

    async def top_contributors(self, limit: int = 5) -> list[tuple[int, int, int]]:
        cur = await self.db.execute(
            "SELECT user_id, tracks_added, likes_received FROM contributors "
            "ORDER BY likes_received DESC, tracks_added DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [(r["user_id"], r["tracks_added"], r["likes_received"]) for r in rows]

    async def library_size(self) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT status, COUNT(*) AS n FROM tracks GROUP BY status"
        )
        rows = await cur.fetchall()
        result = {s: 0 for s in TrackStatus.ALL}
        for r in rows:
            result[r["status"]] = r["n"]
        result["total"] = sum(result[s] for s in TrackStatus.ALL)
        return result
