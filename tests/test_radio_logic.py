"""Tests unitaires du moteur radio (sans Discord ni SQLite)."""
from __future__ import annotations

import time
import unittest

from cogs.radio.library import LibraryManager
from cogs.radio.models import Track, TrackStatus
from cogs.radio.radio_service import RadioService


def _track(**kwargs) -> Track:
    now = int(time.time())
    data = dict(
        id=1,
        title="Song",
        artist="Artist",
        source_url="https://example.com",
        spotify_url=None,
        youtube_url="https://youtu.be/x",
        added_by=1,
        added_at=now,
        likes=0,
        dislikes=0,
        play_count=0,
        last_played=None,
        last_liked=None,
        status=TrackStatus.ACTIVE,
    )
    data.update(kwargs)
    return Track(**data)


class RadioWeightTests(unittest.TestCase):
    def test_likes_beat_neutral(self) -> None:
        now = int(time.time())
        liked = _track(id=1, likes=5)
        neutral = _track(id=2, likes=0)
        self.assertGreater(
            RadioService._weight(liked, now=now),
            RadioService._weight(neutral, now=now),
        )

    def test_dislikes_lower_weight(self) -> None:
        now = int(time.time())
        hated = _track(id=1, dislikes=8)
        ok = _track(id=2, dislikes=0)
        self.assertLess(
            RadioService._weight(hated, now=now),
            RadioService._weight(ok, now=now),
        )

    def test_hof_is_damped(self) -> None:
        now = int(time.time())
        hof = _track(id=1, likes=20, status=TrackStatus.HALL_OF_FAME)
        active = _track(id=2, likes=20)
        self.assertLess(
            RadioService._weight(hof, now=now),
            RadioService._weight(active, now=now),
        )

    def test_filter_excludes_recent_and_same_artist(self) -> None:
        tracks = [
            _track(id=1, artist="A"),
            _track(id=2, artist="B"),
            _track(id=3, artist="A"),
        ]
        pool = RadioService._filter(tracks, recent_ids={1}, last_artist="A")
        self.assertEqual([t.id for t in pool], [2])


class SurvivalTests(unittest.TestCase):
    def test_hof_outlives_active(self) -> None:
        now = int(time.time())
        hof = _track(id=1, likes=15, status=TrackStatus.HALL_OF_FAME)
        weak = _track(id=2, likes=0, play_count=0, added_at=now - 40 * 86400)
        self.assertGreater(
            LibraryManager.survival_score(hof, now=now),
            LibraryManager.survival_score(weak, now=now),
        )


class HofThresholdTests(unittest.TestCase):
    def test_needs_min_votes(self) -> None:
        track = _track(likes=14, dislikes=0)
        self.assertFalse(
            LibraryManager.hof_qualifies(track, hof_ratio=75, hof_min_votes=15)
        )
        self.assertTrue(
            LibraryManager.hof_qualifies(_track(likes=15, dislikes=0), hof_ratio=75, hof_min_votes=15)
        )

    def test_ratio_not_raw_likes(self) -> None:
        # 12 likes / 4 dislikes = 75 %, 16 votes
        ok = _track(likes=12, dislikes=4)
        self.assertTrue(LibraryManager.hof_qualifies(ok, hof_ratio=75, hof_min_votes=15))
        # 11 likes / 5 dislikes = 68,75 %
        low = _track(likes=11, dislikes=5)
        self.assertFalse(LibraryManager.hof_qualifies(low, hof_ratio=75, hof_min_votes=15))

    def test_high_ratio_blocked_by_dislikes(self) -> None:
        track = _track(likes=20, dislikes=20)
        self.assertFalse(LibraryManager.hof_qualifies(track, hof_ratio=75, hof_min_votes=15))
        self.assertTrue(LibraryManager.hof_qualifies(track, hof_ratio=50, hof_min_votes=15))


if __name__ == "__main__":
    unittest.main()
