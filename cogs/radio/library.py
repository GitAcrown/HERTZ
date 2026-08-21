"""Gestionnaire de la bibliothèque communautaire.

Responsable de :
- la création / déduplication des morceaux,
- les votes (like / dislike) avec promotion Hall of Fame,
- le système de survie (survival_score) et l'archivage automatique
  lorsque la bibliothèque active dépasse sa taille cible (~100).

Toute logique métier vit ici ; l'accès SQL passe par :class:`Database`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from .db import Database
from .models import Track, TrackStatus, Vote

logger = logging.getLogger("Hz.Radio.Library")

# --- Paramètres réglables du système de survie ---
TARGET_ACTIVE_SIZE = 100          # taille cible de la bibliothèque active
HALL_OF_FAME_RATIO = 75           # % de likes parmi les votes (surchargeable)
HALL_OF_FAME_MIN_VOTES = 15       # votes min. avant d'évaluer le ratio
LIKE_WEIGHT = 3                   # poids d'un like dans survival_score
DISLIKE_WEIGHT = 2                # poids d'un dislike (pénalité)
AGE_PENALTY_PER_DAY = 0.5         # pénalité d'ancienneté par jour
INACTIVITY_GRACE_DAYS = 7         # pas de pénalité d'inactivité avant ce délai
INACTIVITY_PENALTY_PER_DAY = 0.3  # pénalité si jamais joué/aimé récemment

DAY = 86400


@dataclass(slots=True)
class VoteResult:
    track: Track
    likes: int
    dislikes: int
    changed: bool
    promoted_to_hof: bool
    demoted_from_hof: bool = False


class LibraryManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Création / déduplication
    # ------------------------------------------------------------------

    async def get_or_create_track(
        self,
        *,
        title: str,
        artist: str,
        source_url: str,
        youtube_url: Optional[str],
        spotify_url: Optional[str],
        added_by: int,
    ) -> tuple[Track, bool]:
        """Retourne (track, created). Déduplique sur YouTube/Spotify/titre+artiste."""
        existing = await self.db.find_track(
            youtube_url=youtube_url,
            spotify_url=spotify_url,
            title=title,
            artist=artist,
        )
        if existing is not None:
            # Un morceau archivé qui est ré-ajouté revient en lecture.
            if existing.status == TrackStatus.ARCHIVED:
                await self.db.set_track_status(existing.id, TrackStatus.ACTIVE)
                existing.status = TrackStatus.ACTIVE
            return existing, False

        track = await self.db.create_track(
            title=title,
            artist=artist,
            source_url=source_url,
            spotify_url=spotify_url,
            youtube_url=youtube_url,
            added_by=added_by,
        )
        await self.db.increment_tracks_added(added_by)
        return track, True

    # ------------------------------------------------------------------
    # Votes
    # ------------------------------------------------------------------

    @staticmethod
    def hof_qualifies(
        track: Track, *, hof_ratio: int, hof_min_votes: int
    ) -> bool:
        """True si assez de votes et likes / (likes+dislikes) >= ratio."""
        total = track.likes + track.dislikes
        if total < max(1, hof_min_votes):
            return False
        return track.likes * 100 >= max(1, hof_ratio) * total

    async def vote(
        self,
        user_id: int,
        track_id: int,
        value: int,
        *,
        hof_ratio: int = HALL_OF_FAME_RATIO,
        hof_min_votes: int = HALL_OF_FAME_MIN_VOTES,
    ) -> Optional[VoteResult]:
        """Pose un like (+1) ou dislike (-1). Toggle si le même vote est rejoué."""
        track = await self.db.get_track(track_id)
        if track is None:
            return None

        previous = await self.db.get_vote(user_id, track_id)
        if previous == value:
            # Re-cliquer le même vote l'annule.
            await self.db.clear_vote(user_id, track_id)
            changed = True
        else:
            await self.db.set_vote(user_id, track_id, value)
            changed = previous != value

        track = await self.db.get_track(track_id)
        assert track is not None

        promoted, demoted = await self._maybe_update_hall_of_fame(
            track, hof_ratio=hof_ratio, hof_min_votes=hof_min_votes
        )
        return VoteResult(
            track=track,
            likes=track.likes,
            dislikes=track.dislikes,
            changed=changed,
            promoted_to_hof=promoted,
            demoted_from_hof=demoted,
        )

    async def like(
        self,
        user_id: int,
        track_id: int,
        *,
        hof_ratio: int = HALL_OF_FAME_RATIO,
        hof_min_votes: int = HALL_OF_FAME_MIN_VOTES,
    ) -> Optional[VoteResult]:
        return await self.vote(
            user_id,
            track_id,
            Vote.LIKE,
            hof_ratio=hof_ratio,
            hof_min_votes=hof_min_votes,
        )

    async def dislike(
        self,
        user_id: int,
        track_id: int,
        *,
        hof_ratio: int = HALL_OF_FAME_RATIO,
        hof_min_votes: int = HALL_OF_FAME_MIN_VOTES,
    ) -> Optional[VoteResult]:
        return await self.vote(
            user_id,
            track_id,
            Vote.DISLIKE,
            hof_ratio=hof_ratio,
            hof_min_votes=hof_min_votes,
        )

    async def _maybe_update_hall_of_fame(
        self, track: Track, *, hof_ratio: int, hof_min_votes: int
    ) -> tuple[bool, bool]:
        qualifies = self.hof_qualifies(
            track, hof_ratio=hof_ratio, hof_min_votes=hof_min_votes
        )
        if qualifies and track.status != TrackStatus.HALL_OF_FAME:
            await self.db.set_track_status(track.id, TrackStatus.HALL_OF_FAME)
            track.status = TrackStatus.HALL_OF_FAME
            logger.info(
                "Hall of Fame : '%s' (+%d / -%d)",
                track.display,
                track.likes,
                track.dislikes,
            )
            return True, False
        if not qualifies and track.status == TrackStatus.HALL_OF_FAME:
            await self.db.set_track_status(track.id, TrackStatus.ACTIVE)
            track.status = TrackStatus.ACTIVE
            logger.info(
                "Sortie Hall of Fame : '%s' (+%d / -%d)",
                track.display,
                track.likes,
                track.dislikes,
            )
            return False, True
        return False, False

    async def apply_hof_rules(
        self, *, hof_ratio: int, hof_min_votes: int
    ) -> tuple[int, int]:
        """Réévalue actifs + HoF après un changement de règles.

        Retourne ``(promus, rétrogradés)``.
        """
        tracks = await self.db.get_tracks_by_status(
            TrackStatus.ACTIVE, TrackStatus.HALL_OF_FAME
        )
        promoted = 0
        demoted = 0
        for track in tracks:
            qualifies = self.hof_qualifies(
                track, hof_ratio=hof_ratio, hof_min_votes=hof_min_votes
            )
            if qualifies and track.status != TrackStatus.HALL_OF_FAME:
                await self.db.set_track_status(track.id, TrackStatus.HALL_OF_FAME)
                promoted += 1
            elif not qualifies and track.status == TrackStatus.HALL_OF_FAME:
                await self.db.set_track_status(track.id, TrackStatus.ACTIVE)
                demoted += 1
        return promoted, demoted

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------

    async def record_play(self, track_id: int, *, via_radio: bool) -> None:
        await self.db.record_play(track_id, via_radio=via_radio)

    # ------------------------------------------------------------------
    # Survie / archivage
    # ------------------------------------------------------------------

    @staticmethod
    def survival_score(track: Track, *, now: Optional[int] = None) -> float:
        """Score de survie d'un morceau.

        survival_score = likes*3 + play_count - dislikes*2
                         - age_penalty - inactivity_penalty

        Le Hall of Fame reçoit un bonus massif : il n'est quasiment jamais
        archivé.
        """
        now = now or int(time.time())

        age_days = max(0.0, (now - track.added_at) / DAY)
        age_penalty = age_days * AGE_PENALTY_PER_DAY

        last_activity = max(
            track.last_played or 0, track.last_liked or 0, track.added_at
        )
        inactive_days = max(0.0, (now - last_activity) / DAY)
        inactivity_penalty = (
            max(0.0, inactive_days - INACTIVITY_GRACE_DAYS) * INACTIVITY_PENALTY_PER_DAY
        )

        score = (
            track.likes * LIKE_WEIGHT
            + track.play_count
            - track.dislikes * DISLIKE_WEIGHT
            - age_penalty
            - inactivity_penalty
        )

        if track.status == TrackStatus.HALL_OF_FAME:
            score += 1000  # quasi indéboulonnable
        return score

    async def enforce_capacity(self) -> list[Track]:
        """Archive les morceaux actifs les moins performants au-delà de la cible.

        Renvoie la liste des morceaux nouvellement archivés. Le Hall of Fame
        n'est jamais archivé et ne compte pas dans la limite des actifs.
        """
        active = await self.db.get_tracks_by_status(TrackStatus.ACTIVE)
        if len(active) <= TARGET_ACTIVE_SIZE:
            return []

        now = int(time.time())
        ranked = sorted(active, key=lambda t: self.survival_score(t, now=now))
        to_archive = ranked[: len(active) - TARGET_ACTIVE_SIZE]

        for track in to_archive:
            await self.db.set_track_status(track.id, TrackStatus.ARCHIVED)
            logger.info(
                "Archivé : '%s' (score=%.1f)",
                track.display,
                self.survival_score(track, now=now),
            )
        return to_archive
