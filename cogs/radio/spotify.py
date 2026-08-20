"""Résolution Spotify (métadonnées uniquement) via Spotipy.

Conformément au flux validé : un lien Spotify ne sert qu'à récupérer des
métadonnées (titre, artiste). La lecture audio passe ensuite par une
recherche YouTube côté Lavalink. On ne télécharge ni ne streame jamais
depuis Spotify.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("Hz.Radio.Spotify")

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials

    _SPOTIPY_AVAILABLE = True
except ImportError:  # pragma: no cover - dépendance optionnelle au runtime
    spotipy = None  # type: ignore[assignment]
    SpotifyClientCredentials = None  # type: ignore[assignment]
    _SPOTIPY_AVAILABLE = False


_TRACK_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]+/)?track/|spotify:track:)([A-Za-z0-9]+)"
)
_PLAYLIST_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]+/)?playlist/|spotify:playlist:)([A-Za-z0-9]+)"
)
_ALBUM_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]+/)?album/|spotify:album:)([A-Za-z0-9]+)"
)
SPOTIFY_FETCH_CAP = 50


@dataclass(slots=True)
class SpotifyTrack:
    """Métadonnées d'un morceau Spotify, prêtes pour une recherche YouTube."""

    title: str
    artist: str
    spotify_url: str

    @property
    def search_query(self) -> str:
        return f"{self.artist} {self.title}".strip()


class SpotifyResolver:
    """Détecte et résout les liens Spotify en listes de :class:`SpotifyTrack`."""

    def __init__(self, client_id: Optional[str], client_secret: Optional[str]) -> None:
        self._client = None
        if not (_SPOTIPY_AVAILABLE and client_id and client_secret):
            logger.warning(
                "Spotify désactivé (spotipy absent ou identifiants manquants). "
                "Les liens Spotify ne pourront pas être résolus."
            )
            return
        try:
            auth = SpotifyClientCredentials(
                client_id=client_id, client_secret=client_secret
            )
            self._client = spotipy.Spotify(auth_manager=auth)
            logger.info("Client Spotify initialisé.")
        except Exception as exc:  # pragma: no cover - dépend du réseau
            logger.error("Échec d'initialisation du client Spotify : %s", exc)
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @staticmethod
    def is_spotify_url(text: str) -> bool:
        return bool(
            _TRACK_RE.search(text)
            or _PLAYLIST_RE.search(text)
            or _ALBUM_RE.search(text)
        )

    async def resolve(self, url: str) -> list[SpotifyTrack]:
        """Résout un lien Spotify (track / playlist / album) en métadonnées.

        Les appels Spotipy sont bloquants : on les exécute dans un thread.
        """
        if not self.enabled:
            raise RuntimeError(
                "La résolution Spotify est désactivée (identifiants manquants)."
            )

        track_match = _TRACK_RE.search(url)
        if track_match:
            return await asyncio.to_thread(self._resolve_track, track_match.group(1))

        playlist_match = _PLAYLIST_RE.search(url)
        if playlist_match:
            return await asyncio.to_thread(self._resolve_playlist, playlist_match.group(1))

        album_match = _ALBUM_RE.search(url)
        if album_match:
            return await asyncio.to_thread(self._resolve_album, album_match.group(1))

        return []

    # ------------------------------------------------------------------
    # Implémentations bloquantes (exécutées via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _resolve_track(self, track_id: str) -> list[SpotifyTrack]:
        data = self._client.track(track_id)  # type: ignore[union-attr]
        item = self._to_track(data)
        return [item] if item else []

    def _resolve_playlist(self, playlist_id: str) -> list[SpotifyTrack]:
        results: list[SpotifyTrack] = []
        page = self._client.playlist_items(playlist_id, additional_types=("track",))  # type: ignore[union-attr]
        while page:
            for entry in page.get("items", []):
                track = entry.get("track") if entry else None
                item = self._to_track(track)
                if item:
                    results.append(item)
                if len(results) >= SPOTIFY_FETCH_CAP:
                    return results
            page = self._client.next(page) if page.get("next") else None  # type: ignore[union-attr]
        return results

    def _resolve_album(self, album_id: str) -> list[SpotifyTrack]:
        results: list[SpotifyTrack] = []
        page = self._client.album_tracks(album_id)  # type: ignore[union-attr]
        while page:
            for track in page.get("items", []):
                item = self._to_track(track)
                if item:
                    results.append(item)
                if len(results) >= SPOTIFY_FETCH_CAP:
                    return results
            page = self._client.next(page) if page.get("next") else None  # type: ignore[union-attr]
        return results

    @staticmethod
    def _to_track(data: Optional[dict]) -> Optional[SpotifyTrack]:
        if not data or not data.get("name"):
            return None
        artists = data.get("artists") or []
        artist = artists[0]["name"] if artists else ""
        ext = data.get("external_urls") or {}
        spotify_url = ext.get("spotify") or ""
        if not spotify_url and data.get("id"):
            spotify_url = f"https://open.spotify.com/track/{data['id']}"
        return SpotifyTrack(title=data["name"], artist=artist, spotify_url=spotify_url)
