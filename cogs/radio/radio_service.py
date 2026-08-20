"""Service de sélection du mode Radio.

Sélectionne automatiquement un morceau de la bibliothèque lorsque la queue
utilisateur est vide. Objectifs : variété, découverte, éviter les répétitions.

La pondération suit la spec :

    radio_weight = base_weight + likes - dislikes + novelty_bonus - recent_play_penalty

Contraintes dures :
- ne jamais rejouer un morceau présent dans les X dernières lectures,
- jamais deux morceaux du même artiste à la suite.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Optional

from .db import Database
from .models import Track, TrackStatus

logger = logging.getLogger("Hz.Radio.Service")

# --- Paramètres réglables ---
RECENT_PLAYS_WINDOW = 20      # X dernières lectures à exclure
BASE_WEIGHT = 10.0
NEW_TRACK_AGE_DAYS = 3        # "nouveauté" si ajouté il y a moins de N jours
NOVELTY_NEVER_PLAYED = 6.0    # bonus si jamais joué
NOVELTY_RECENTLY_ADDED = 4.0  # bonus si ajouté récemment
NOVELTY_LOW_PLAYS = 3.0       # bonus si peu écouté
LOW_PLAYS_THRESHOLD = 3
RECENT_PENALTY_MAX = 8.0      # pénalité max pour une écoute très récente
RECENT_PENALTY_DAYS = 5       # au-delà, plus de pénalité de récence
HOF_WEIGHT_FACTOR = 0.45      # le Hall of Fame n'apparaît qu'occasionnellement
MIN_WEIGHT = 1.0

DAY = 86400


class RadioService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def pick(
        self,
        *,
        last_artist: Optional[str] = None,
        exclude_ids: Optional[set[int]] = None,
    ) -> Optional[Track]:
        """Choisit le prochain morceau radio, ou ``None`` si la lib est vide."""
        candidates = await self.db.get_tracks_by_status(
            TrackStatus.ACTIVE, TrackStatus.HALL_OF_FAME
        )
        if not candidates:
            return None

        hard_exclude = set(exclude_ids or ())
        recent_ids = set(await self.db.recent_track_ids(RECENT_PLAYS_WINDOW)) | hard_exclude

        pool = self._filter(candidates, recent_ids, last_artist)
        if not pool:
            pool = self._filter(candidates, recent_ids, None)
        if not pool:
            pool = self._filter(candidates, hard_exclude, None)
        if not pool:
            return None

        now = int(time.time())
        weights = [self._weight(t, now=now) for t in pool]
        chosen = random.choices(pool, weights=weights, k=1)[0]
        logger.debug("Radio a choisi '%s'", chosen.display)
        return chosen

    # ------------------------------------------------------------------

    @staticmethod
    def _filter(
        tracks: list[Track], recent_ids: set[int], last_artist: Optional[str]
    ) -> list[Track]:
        result = []
        last_artist_lc = (last_artist or "").lower()
        for t in tracks:
            if t.id in recent_ids:
                continue
            if last_artist_lc and t.artist.lower() == last_artist_lc:
                continue
            result.append(t)
        return result

    @staticmethod
    def _weight(track: Track, *, now: int) -> float:
        weight = BASE_WEIGHT + track.likes - track.dislikes

        # novelty_bonus
        if track.play_count == 0:
            weight += NOVELTY_NEVER_PLAYED
        elif track.play_count <= LOW_PLAYS_THRESHOLD:
            weight += NOVELTY_LOW_PLAYS
        age_days = (now - track.added_at) / DAY
        if age_days <= NEW_TRACK_AGE_DAYS:
            weight += NOVELTY_RECENTLY_ADDED

        # recent_play_penalty : décroît linéairement avec le temps écoulé.
        if track.last_played:
            days_since = (now - track.last_played) / DAY
            if days_since < RECENT_PENALTY_DAYS:
                factor = 1.0 - (days_since / RECENT_PENALTY_DAYS)
                weight -= RECENT_PENALTY_MAX * factor

        if track.status == TrackStatus.HALL_OF_FAME:
            weight *= HOF_WEIGHT_FACTOR

        return max(MIN_WEIGHT, weight)
