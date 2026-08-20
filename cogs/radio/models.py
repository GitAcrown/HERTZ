"""Modèles de données du mode Radio communautaire.

Isolé volontairement de toute logique SQL : ces objets sont de simples
structures de transport entre la couche base de données et les services
(Library / Radio) ou les commandes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class TrackStatus:
    """Statuts possibles d'un morceau dans la bibliothèque."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    HALL_OF_FAME = "hall_of_fame"

    ALL = (ACTIVE, ARCHIVED, HALL_OF_FAME)


class Vote:
    """Valeurs de vote stockées dans la table ``votes``."""

    LIKE = 1
    DISLIKE = -1


@dataclass(slots=True)
class Track:
    """Représente un morceau de la bibliothèque communautaire.

    Les champs ``last_played`` / ``last_liked`` / ``added_at`` sont des
    timestamps Unix (secondes, entiers) afin de rester portables lors d'une
    future migration SQLite -> PostgreSQL.
    """

    id: int
    title: str
    artist: str
    source_url: str
    spotify_url: Optional[str]
    youtube_url: Optional[str]
    added_by: int
    added_at: int
    likes: int = 0
    dislikes: int = 0
    play_count: int = 0
    last_played: Optional[int] = None
    last_liked: Optional[int] = None
    status: str = TrackStatus.ACTIVE

    @classmethod
    def from_row(cls, row) -> "Track":
        """Construit un :class:`Track` depuis une ligne ``aiosqlite.Row``."""
        return cls(
            id=row["id"],
            title=row["title"],
            artist=row["artist"],
            source_url=row["source_url"],
            spotify_url=row["spotify_url"],
            youtube_url=row["youtube_url"],
            added_by=row["added_by"],
            added_at=row["added_at"],
            likes=row["likes"],
            dislikes=row["dislikes"],
            play_count=row["play_count"],
            last_played=row["last_played"],
            last_liked=row["last_liked"],
            status=row["status"],
        )

    @property
    def display(self) -> str:
        """Représentation courte ``Artiste — Titre``."""
        if self.artist:
            return f"{self.artist} — {self.title}"
        return self.title
