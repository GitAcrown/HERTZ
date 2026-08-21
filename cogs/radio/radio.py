"""Cog Radio — bot musical communautaire (queue collaborative + mode radio).

Orchestration :
- connexion à Lavalink via Wavelink 3,
- auto-join du salon vocal dédié (et reprise après restart),
- priorité absolue Queue utilisateur > Mode Radio,
- retry si une piste refuse de charger,
- UI Components v2 (LayoutView) à la MARIA.

L'autoplay natif de Wavelink est volontairement DÉSACTIVÉ : on gère nous-mêmes
l'enchaînement dans ``on_wavelink_track_end``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
import wavelink
from discord import app_commands
from discord.ext import commands, tasks

from .db import Database
from .library import (
    HALL_OF_FAME_MIN_VOTES,
    HALL_OF_FAME_RATIO,
    LibraryManager,
)
from .models import Track, TrackStatus, Vote
from .radio_service import RadioService
from .spotify import SpotifyResolver
from .views import (
    QUEUE_PAGE,
    AddedView,
    ConfigView,
    HelpView,
    HofConfigView,
    NoticeView,
    NowPlayingState,
    NowPlayingView,
    PlaylistConfirmView,
    QueueView,
    StatsView,
    fmt_duration,
    persistent_now_playing_stub,
)

logger = logging.getLogger("Hz.Radio")

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "radio.sqlite")

SETTING_VOICE_CHANNEL = "voice_channel_id"
SETTING_TEXT_CHANNEL = "text_channel_id"
SETTING_NP_MESSAGE = "np_message_id"
SETTING_VOLUME = "volume"
SETTING_HOF_RATIO = "hof_ratio"
SETTING_HOF_MIN_VOTES = "hof_min_votes"
SETTING_HOF_LIKES = "hof_likes"  # ancien réglage, repli pour min_votes

DISCONNECT_GRACE_SECONDS = 30
CAPACITY_CHECK_MINUTES = 30
MAX_PLAY_ATTEMPTS = 8
PLAYLIST_ADD_CAP = 25
PLAYLIST_CONFIRM_AFTER = 8
DEFAULT_VOLUME = 80

_LOW_PRIORITY = (
    "karaoke",
    "made famous",
    "tribute",
    "nightcore",
    "8d audio",
    "slowed + reverb",
    "cover version",
)


@dataclass(slots=True)
class QueuedItem:
    playable: "wavelink.Playable"
    track_id: int
    requester_id: int
    via_radio: bool = False


@dataclass(slots=True)
class PendingAdd:
    title: str
    artist: str
    source_url: str
    spotify_url: Optional[str] = None
    playable: Optional["wavelink.Playable"] = None


class Radio(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = Database(DB_PATH)
        self.library = LibraryManager(self.db)
        self.radio = RadioService(self.db)
        self.spotify = SpotifyResolver(
            bot.config.get("SPOTIFY_CLIENT_ID"),  # type: ignore[attr-defined]
            bot.config.get("SPOTIFY_CLIENT_SECRET"),  # type: ignore[attr-defined]
        )
        raw_guild = (bot.config.get("RADIO_GUILD_ID") or "").strip()  # type: ignore[attr-defined]
        self._radio_guild_id = int(raw_guild) if raw_guild.isdigit() else None

        self._queues: dict[int, deque[QueuedItem]] = {}
        self._current: dict[int, QueuedItem] = {}
        self._skip_votes: dict[int, set[int]] = {}
        self._disconnect_tasks: dict[int, asyncio.Task] = {}
        self._play_locks: dict[int, asyncio.Lock] = {}
        self._failed: dict[int, set[int]] = {}
        self._pending: dict[str, list[PendingAdd]] = {}
        self._pending_meta: dict[str, tuple[int, int]] = {}  # token -> (user_id, guild_id)
        self._lavalink_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        await self.db.connect()
        if self._radio_guild_id is not None:
            await self.db.inherit_legacy_settings(self._radio_guild_id)
        logger.info("Base de données Radio prête (%s).", DB_PATH)
        self.bot.add_view(persistent_now_playing_stub())
        self.capacity_loop.start()
        # Wavelink exige bot.user (header User-Id). cog_load tourne avant login.
        self._lavalink_task = asyncio.create_task(self._connect_lavalink_when_ready())

    async def cog_unload(self) -> None:
        self.capacity_loop.cancel()
        if self._lavalink_task is not None:
            self._lavalink_task.cancel()
        for task in self._disconnect_tasks.values():
            task.cancel()
        await self.db.close()

    async def _connect_lavalink_when_ready(self) -> None:
        await self.bot.wait_until_ready()
        if self.bot.user is None:
            logger.error("Bot user introuvable, connexion Lavalink abandonnée.")
            return
        logger.info("Bot identifié (%s), connexion Wavelink…", self.bot.user.id)
        await self._connect_lavalink()

    async def _connect_lavalink(self) -> None:
        host = (self.bot.config.get("LAVALINK_HOST") or "127.0.0.1").strip()  # type: ignore[attr-defined]
        port = (self.bot.config.get("LAVALINK_PORT") or "2333").strip()  # type: ignore[attr-defined]
        password = (self.bot.config.get("LAVALINK_PASSWORD") or "youshallnotpass").strip()  # type: ignore[attr-defined]
        uri = f"http://{host}:{port}"
        ok = await self._probe_lavalink(uri, password)
        if not ok:
            logger.error(
                "Lavalink n'écoute pas sur %s. "
                "Dans un autre terminal : cd lavalink && java -Xmx200m -jar Lavalink.jar "
                "— attendre « ready to accept connections » avant de lancer le bot.",
                uri,
            )
        node = wavelink.Node(uri=uri, password=password)
        try:
            await wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)
            logger.info("Connexion au noeud Lavalink demandée (%s).", uri)
        except Exception as exc:
            logger.error(
                "Impossible de joindre Lavalink (%s) : %s: %r",
                uri,
                type(exc).__name__,
                exc,
            )

    async def _probe_lavalink(self, uri: str, password: str) -> bool:
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{uri}/version",
                    headers={"Authorization": password},
                ) as resp:
                    body = (await resp.text()).strip()
                    logger.info("Lavalink HTTP %s /version → %s", resp.status, body[:120] or "(vide)")
                    return resp.status < 500
        except Exception as exc:
            logger.error(
                "Sonde Lavalink échouée (%s) : %s: %s",
                uri,
                type(exc).__name__,
                exc or repr(exc),
            )
            return False

    # ------------------------------------------------------------------
    # Helpers d'état
    # ------------------------------------------------------------------

    def _guild_allowed(self, guild: Optional[discord.Guild]) -> bool:
        if guild is None:
            return False
        if self._radio_guild_id is None:
            return True
        return guild.id == self._radio_guild_id

    def _queue(self, guild_id: int) -> deque[QueuedItem]:
        return self._queues.setdefault(guild_id, deque())

    def _play_lock(self, guild_id: int) -> asyncio.Lock:
        return self._play_locks.setdefault(guild_id, asyncio.Lock())

    def _get_player(self, guild: discord.Guild) -> Optional[wavelink.Player]:
        vc = guild.voice_client
        return vc if isinstance(vc, wavelink.Player) else None

    def drop_pending(self, token: str) -> None:
        self._pending.pop(token, None)
        self._pending_meta.pop(token, None)

    @staticmethod
    def _parse_setting_int(raw: Optional[str], default: int) -> int:
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    async def _hof_rules(self, guild: discord.Guild) -> tuple[int, int]:
        raw_ratio = await self.db.get_guild_setting(guild.id, SETTING_HOF_RATIO)
        raw_votes = await self.db.get_guild_setting(guild.id, SETTING_HOF_MIN_VOTES)
        if raw_votes is None:
            raw_votes = await self.db.get_guild_setting(guild.id, SETTING_HOF_LIKES)
        ratio = min(100, max(1, self._parse_setting_int(raw_ratio, HALL_OF_FAME_RATIO)))
        votes = max(1, self._parse_setting_int(raw_votes, HALL_OF_FAME_MIN_VOTES))
        return ratio, votes

    async def _home_channel(self, guild: discord.Guild) -> Optional[discord.VoiceChannel]:
        channel = await self._get_config_channel(guild, SETTING_VOICE_CHANNEL)
        return channel if isinstance(channel, discord.VoiceChannel) else None

    @staticmethod
    def _humans(channel: Optional[discord.abc.Snowflake]) -> list[discord.Member]:
        members = getattr(channel, "members", None)
        if not members:
            return []
        return [m for m in members if not m.bot]

    async def _get_config_channel(self, guild: discord.Guild, key: str):
        await self.db.inherit_legacy_settings(guild.id)
        raw = await self.db.get_guild_setting(guild.id, key)
        if not raw:
            return None
        try:
            return guild.get_channel(int(raw))
        except (ValueError, TypeError):
            return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("Commande utilisable en serveur uniquement.", ephemeral=True)
            return False
        if not self._guild_allowed(interaction.guild):
            await interaction.response.send_message("La radio n'est pas active sur ce serveur.", ephemeral=True)
            return False
        return True

    async def _user_in_voice(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        guild = interaction.guild
        if not isinstance(member, discord.Member) or guild is None:
            return False
        if member.voice is None or member.voice.channel is None:
            return False
        player = self._get_player(guild)
        if player and player.channel:
            return member.voice.channel.id == player.channel.id
        configured = await self._get_config_channel(guild, SETTING_VOICE_CHANNEL)
        if isinstance(configured, discord.VoiceChannel):
            return member.voice.channel.id == configured.id
        return True

    async def _deny_voice(self, interaction: discord.Interaction) -> None:
        view = NoticeView(
            "Vocal requis",
            "Rejoins le salon où joue la radio pour voter, skip ou pause.",
        )
        await self._respond(interaction, view=view, ephemeral=True)

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        view: Optional[discord.ui.LayoutView] = None,
        ephemeral: bool = False,
        edit: bool = False,
    ) -> None:
        kwargs: dict = {}
        if view is not None:
            kwargs["view"] = view
        try:
            if edit and interaction.message is not None:
                if interaction.response.is_done():
                    await interaction.edit_original_response(**kwargs)
                else:
                    await interaction.response.edit_message(**kwargs)
                return
            if interaction.response.is_done():
                await interaction.followup.send(ephemeral=ephemeral, **kwargs)
            else:
                await interaction.response.send_message(ephemeral=ephemeral, **kwargs)
        except discord.HTTPException:
            logger.debug("Échec réponse interaction", exc_info=True)

    # ------------------------------------------------------------------
    # Résolution des sources
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_search(query: str) -> str:
        q = query.strip()
        if q.startswith(("http://", "https://", "ytsearch:", "scsearch:", "spsearch:")):
            return q
        return f"ytsearch:{q}"

    @staticmethod
    def _score_playable(playable: "wavelink.Playable", title: str, artist: str) -> tuple:
        pt = (playable.title or "").casefold()
        pa = (getattr(playable, "author", None) or "").casefold()
        t = (title or "").casefold()
        a = (artist or "").casefold()
        return (
            int(bool(t) and t == pt),
            int(bool(t) and t in pt),
            int(bool(a) and a in pa),
            -int(any(marker in pt for marker in _LOW_PRIORITY)),
        )

    async def _search_results(self, query: str) -> list["wavelink.Playable"]:
        q = self._prepare_search(query)
        try:
            results = await wavelink.Playable.search(q)
        except Exception as exc:
            logger.error("Échec recherche Wavelink '%s' : %s", q, exc)
            return []
        if not results:
            return []
        if isinstance(results, wavelink.Playlist):
            return list(results.tracks)
        return list(results)

    async def _search_one(
        self,
        query: str,
        *,
        title: str = "",
        artist: str = "",
    ) -> Optional["wavelink.Playable"]:
        results = await self._search_results(query)
        if not results:
            return None
        if not (title or artist):
            return results[0]
        return max(results[:8], key=lambda p: self._score_playable(p, title, artist))

    async def _pending_from_query(self, query: str) -> tuple[list[PendingAdd], str]:
        query = query.strip()
        if self.spotify.is_spotify_url(query):
            sp_tracks = await self.spotify.resolve(query)
            if not sp_tracks:
                raise ValueError("Lien Spotify vide ou introuvable.")
            items = [
                PendingAdd(
                    title=sp.title,
                    artist=sp.artist,
                    source_url=query,
                    spotify_url=sp.spotify_url,
                )
                for sp in sp_tracks
            ]
            kind = "Spotify"
            if len(items) == 1:
                kind = "Spotify · morceau"
            elif "playlist" in query.casefold() or "spotify:playlist" in query.casefold():
                kind = "Spotify · playlist"
            else:
                kind = "Spotify · album"
            return items, kind

        is_url = query.startswith("http://") or query.startswith("https://")
        results = await self._search_results(query)
        if not results:
            raise ValueError("Aucun résultat trouvé.")
        if not is_url:
            results = [
                max(results[:8], key=lambda p: self._score_playable(p, query, ""))
            ]
        items = [
            PendingAdd(
                title=p.title or query,
                artist=getattr(p, "author", None) or "",
                source_url=query if is_url else (p.uri or ""),
                playable=p,
            )
            for p in results
        ]
        kind = "YouTube · playlist" if is_url and len(items) > 1 else "YouTube"
        return items, kind

    async def _store_and_wrap(
        self,
        playable: "wavelink.Playable",
        requester_id: int,
        *,
        title: str,
        artist: str,
        spotify_url: Optional[str],
        source_url: str,
    ) -> tuple[QueuedItem, bool]:
        track, created = await self.library.get_or_create_track(
            title=title,
            artist=artist,
            source_url=source_url,
            youtube_url=playable.uri,
            spotify_url=spotify_url,
            added_by=requester_id,
        )
        return (
            QueuedItem(playable=playable, track_id=track.id, requester_id=requester_id),
            created,
        )

    async def _playable_for_track(self, track: Track) -> Optional["wavelink.Playable"]:
        query = track.youtube_url or track.source_url
        if not query:
            query = f"{track.artist} {track.title}".strip()
        return await self._search_one(query, title=track.title, artist=track.artist)

    @staticmethod
    def _artwork(playable: "wavelink.Playable") -> Optional[str]:
        art = getattr(playable, "artwork", None)
        if isinstance(art, str) and art.startswith("http"):
            return art
        return None

    # ------------------------------------------------------------------
    # Moteur de lecture
    # ------------------------------------------------------------------

    async def enqueue(self, guild: discord.Guild, item: QueuedItem) -> None:
        self._queue(guild.id).append(item)
        player = self._get_player(guild)
        if player and not player.playing:
            await self._play_next(player)

    async def _play_next(self, player: wavelink.Player) -> None:
        guild = player.guild
        if guild is None:
            return
        async with self._play_lock(guild.id):
            await self._play_next_locked(player)

    async def _play_next_locked(self, player: wavelink.Player) -> None:
        guild = player.guild
        if guild is None:
            return
        self._skip_votes.pop(guild.id, None)
        failed = self._failed.setdefault(guild.id, set())

        for _ in range(MAX_PLAY_ATTEMPTS):
            item = await self._pop_or_radio(guild, exclude=failed)
            if item is None:
                self._current.pop(guild.id, None)
                return
            self._current[guild.id] = item
            try:
                await player.play(item.playable, replace=True, add_history=False)
                failed.discard(item.track_id)
                return
            except Exception as exc:
                logger.error(
                    "Échec lecture '%s' : %s",
                    getattr(item.playable, "uri", "?"),
                    exc,
                )
                failed.add(item.track_id)

        logger.warning("Abandon lecture après %d essais (%s).", MAX_PLAY_ATTEMPTS, guild.name)
        self._current.pop(guild.id, None)

    async def _pop_or_radio(
        self, guild: discord.Guild, exclude: set[int]
    ) -> Optional[QueuedItem]:
        queue = self._queue(guild.id)
        while queue:
            item = queue.popleft()
            if item.track_id in exclude:
                continue
            item.via_radio = False
            return item
        return await self._build_radio_item(guild, exclude=exclude)

    async def _build_radio_item(
        self, guild: discord.Guild, exclude: set[int]
    ) -> Optional[QueuedItem]:
        last_artist = None
        current = self._current.get(guild.id)
        if current is not None:
            track = await self.db.get_track(current.track_id)
            if track:
                last_artist = track.artist
        local_exclude = set(exclude)
        bot_id = self.bot.user.id if self.bot.user else 0
        for _ in range(MAX_PLAY_ATTEMPTS):
            track = await self.radio.pick(last_artist=last_artist, exclude_ids=local_exclude)
            if track is None:
                return None
            playable = await self._playable_for_track(track)
            if playable is None:
                logger.warning("Impossible de résoudre le morceau radio '%s'.", track.display)
                local_exclude.add(track.id)
                exclude.add(track.id)
                continue
            return QueuedItem(
                playable=playable,
                track_id=track.id,
                requester_id=bot_id,
                via_radio=True,
            )
        return None

    async def _ensure_playing(self, player: wavelink.Player) -> None:
        if not player.playing:
            await self._play_next(player)

    async def _connect_player(self, channel: discord.VoiceChannel) -> Optional[wavelink.Player]:
        try:
            player = await channel.connect(cls=wavelink.Player, self_deaf=True)
        except Exception as exc:
            logger.error("Échec connexion vocale : %s", exc)
            return None
        player.autoplay = wavelink.AutoPlayMode.disabled
        await self._restore_volume(player)
        return player

    async def _move_to_voice(
        self, guild: discord.Guild, channel: discord.VoiceChannel
    ) -> Optional[wavelink.Player]:
        player = self._get_player(guild)
        if player is None:
            return await self._connect_player(channel)
        current = player.channel
        if current is not None and current.id == channel.id:
            return player
        try:
            await player.move_to(channel)
        except Exception as exc:
            logger.warning("move_to impossible (%s), reconnexion : %s", channel, exc)
            try:
                await player.disconnect()
            except Exception:
                pass
            return await self._connect_player(channel)
        await self._restore_volume(player)
        return player

    async def _restore_volume(self, player: wavelink.Player) -> None:
        guild = player.guild
        if guild is None:
            return
        raw = await self.db.get_guild_setting(guild.id, SETTING_VOLUME)
        try:
            vol = int(raw) if raw else DEFAULT_VOLUME
        except (TypeError, ValueError):
            vol = DEFAULT_VOLUME
        vol = max(0, min(100, vol))
        try:
            await player.set_volume(vol)
        except Exception:
            logger.debug("Impossible d'appliquer le volume %s", vol, exc_info=True)

    async def _ensure_connected(
        self,
        guild: discord.Guild,
        interaction: Optional[discord.Interaction] = None,
    ) -> Optional[wavelink.Player]:
        player = self._get_player(guild)
        if player is not None:
            return player
        target = await self._get_config_channel(guild, SETTING_VOICE_CHANNEL)
        user_voice = None
        if (
            interaction
            and isinstance(interaction.user, discord.Member)
            and interaction.user.voice
        ):
            user_voice = interaction.user.voice.channel
        channel = target if isinstance(target, discord.VoiceChannel) else user_voice
        if not isinstance(channel, discord.VoiceChannel):
            return None
        return await self._connect_player(channel)

    # ------------------------------------------------------------------
    # Panneau now playing
    # ------------------------------------------------------------------

    async def build_now_playing_state(
        self, guild: discord.Guild, *, note: str = ""
    ) -> Optional[NowPlayingState]:
        item = self._current.get(guild.id)
        if item is None:
            return None
        track = await self.db.get_track(item.track_id)
        if track is None:
            return None
        player = self._get_player(guild)
        paused = bool(player and getattr(player, "paused", False))
        required = self._required_skip_votes(player) if player else 1
        voters = self._skip_votes.get(guild.id, set())
        length = getattr(item.playable, "length", 0) or 0
        return NowPlayingState(
            guild_id=guild.id,
            display=track.display,
            via_radio=item.via_radio,
            added_by=track.added_by,
            likes=track.likes,
            dislikes=track.dislikes,
            play_count=track.play_count,
            status=track.status,
            artwork=self._artwork(item.playable),
            skip_votes=len(voters),
            skip_required=required,
            paused=paused,
            queue_len=len(self._queue(guild.id)),
            duration_label=fmt_duration(length),
            note=note,
        )

    async def build_now_playing_view(
        self, guild: discord.Guild, *, note: str = ""
    ) -> Optional[NowPlayingView]:
        state = await self.build_now_playing_state(guild, note=note)
        if state is None:
            return None
        return NowPlayingView(state)

    async def _upsert_now_playing(
        self, guild: discord.Guild, view: discord.ui.LayoutView
    ) -> None:
        channel = await self._get_config_channel(guild, SETTING_TEXT_CHANNEL)
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            return
        raw = await self.db.get_guild_setting(guild.id, SETTING_NP_MESSAGE)
        if raw:
            try:
                msg = await channel.fetch_message(int(raw))
                await msg.edit(view=view)
                return
            except (discord.NotFound, discord.HTTPException, ValueError):
                pass
        try:
            msg = await channel.send(view=view)
            await self.db.set_guild_setting(guild.id, SETTING_NP_MESSAGE, str(msg.id))
        except discord.HTTPException as exc:
            logger.error("Impossible de poster le panneau now playing : %s", exc)

    async def _refresh_now_playing(
        self,
        interaction: discord.Interaction,
        *,
        note: str = "",
        ephemeral_if_slash: bool = True,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        view = await self.build_now_playing_view(guild, note=note)
        if view is None:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Aucun morceau en cours."),
                ephemeral=True,
            )
            return
        np_id = await self.db.get_guild_setting(guild.id, SETTING_NP_MESSAGE)
        same_panel = (
            interaction.message is not None
            and np_id is not None
            and str(interaction.message.id) == np_id
        )
        if not same_panel:
            await self._upsert_now_playing(guild, view)
        if interaction.message is not None and not interaction.response.is_done():
            await interaction.response.edit_message(view=view)
            return
        if ephemeral_if_slash:
            await self._respond(interaction, view=view, ephemeral=True)

    async def _broadcast_notice(
        self,
        view: discord.ui.LayoutView,
        *,
        guild: Optional[discord.Guild] = None,
    ) -> None:
        targets = [guild] if guild is not None else list(self.bot.guilds)
        for g in targets:
            if g is None or not self._guild_allowed(g):
                continue
            channel = await self._get_config_channel(g, SETTING_TEXT_CHANNEL)
            if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                try:
                    await channel.send(view=view)
                except discord.HTTPException:
                    pass

    async def build_queue_view(
        self, guild: discord.Guild, page: int = 0, *, note: str = ""
    ) -> QueueView:
        q = list(self._queue(guild.id))
        total = len(q)
        page_count = max(1, math.ceil(total / QUEUE_PAGE) if total else 1)
        page = max(0, min(page, page_count - 1))
        start = page * QUEUE_PAGE
        chunk = q[start : start + QUEUE_PAGE]
        lines: list[str] = []
        removable: list[tuple[str, str]] = []
        for offset, item in enumerate(chunk):
            idx = start + offset
            track = await self.db.get_track(item.track_id)
            label = track.display if track else (item.playable.title or "Morceau")
            lines.append(f"**{idx + 1}.** {label}\n-# demandé par <@{item.requester_id}>")
            removable.append((label, str(idx)))
        return QueueView(
            guild_id=guild.id,
            lines=lines,
            total=total,
            page=page,
            page_count=page_count,
            removable=removable,
            note=note,
        )

    async def _commit_pending(
        self, guild: discord.Guild, items: list[PendingAdd], requester_id: int
    ) -> tuple[list[QueuedItem], int, int]:
        queued: list[QueuedItem] = []
        created = 0
        skipped = 0
        for pending in items[:PLAYLIST_ADD_CAP]:
            playable = pending.playable
            if playable is None:
                playable = await self._search_one(
                    f"{pending.artist} {pending.title}".strip(),
                    title=pending.title,
                    artist=pending.artist,
                )
            if playable is None:
                skipped += 1
                continue
            item, was_created = await self._store_and_wrap(
                playable,
                requester_id,
                title=pending.title or playable.title,
                artist=pending.artist or (getattr(playable, "author", None) or ""),
                spotify_url=pending.spotify_url,
                source_url=pending.source_url or (playable.uri or ""),
            )
            created += int(was_created)
            queued.append(item)
            await self.enqueue(guild, item)
        return queued, created, skipped

    # ------------------------------------------------------------------
    # Événements Wavelink
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_wavelink_node_ready(
        self, payload: wavelink.NodeReadyEventPayload
    ) -> None:
        logger.info(
            "Noeud Lavalink prêt (session %s, repris=%s).",
            payload.session_id,
            payload.resumed,
        )
        await self.bot.wait_until_ready()
        await self._resume_occupied_channels()

    async def _resume_occupied_channels(self) -> None:
        for guild in self.bot.guilds:
            if not self._guild_allowed(guild):
                continue
            player = self._get_player(guild)
            here = player.channel if player else None
            home = await self._home_channel(guild)
            if here is not None and (home is None or here.id != home.id):
                if self._humans(here):
                    await self._ensure_playing(player)
                    continue
                if home is not None:
                    moved = await self._move_to_voice(guild, home)
                    if moved and self._humans(home):
                        await self._ensure_playing(moved)
                    elif moved:
                        self._schedule_leave(guild, home)
                    continue
            if home is not None and self._humans(home):
                await self._handle_first_join(guild, home)

    @commands.Cog.listener()
    async def on_wavelink_track_start(
        self, payload: wavelink.TrackStartEventPayload
    ) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        item = self._current.get(player.guild.id)
        if item is None:
            return
        await self.library.record_play(item.track_id, via_radio=item.via_radio)
        view = await self.build_now_playing_view(player.guild)
        if view:
            await self._upsert_now_playing(player.guild, view)

    @commands.Cog.listener()
    async def on_wavelink_track_end(
        self, payload: wavelink.TrackEndEventPayload
    ) -> None:
        if payload.reason not in ("finished", "loadFailed"):
            return
        player = payload.player
        if player is None or not player.connected or player.guild is None:
            return
        if payload.reason == "loadFailed":
            item = self._current.get(player.guild.id)
            if item is not None:
                self._failed.setdefault(player.guild.id, set()).add(item.track_id)
                logger.warning("loadFailed : %s", getattr(item.playable, "uri", "?"))
        await self._play_next(player)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload) -> None:
        player = payload.player
        if player is None or not player.connected or player.guild is None:
            return
        item = self._current.get(player.guild.id)
        if item is not None:
            self._failed.setdefault(player.guild.id, set()).add(item.track_id)
        logger.error("Exception piste : %s", getattr(payload, "exception", payload))
        await self._play_next(player)

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload) -> None:
        player = payload.player
        if player is None or not player.connected:
            return
        logger.warning("Piste bloquée, on passe.")
        await self._play_next(player)

    # ------------------------------------------------------------------
    # Auto-join / auto-leave / drag
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        guild = member.guild
        if not self._guild_allowed(guild):
            return
        if self.bot.user is not None and member.id == self.bot.user.id:
            await self._on_own_voice_update(guild, after)
            return
        if member.bot:
            return

        player = self._get_player(guild)
        here = player.channel if player else None
        home = await self._home_channel(guild)

        if after.channel is not None and before.channel != after.channel:
            if here is not None and after.channel.id == here.id:
                self._cancel_leave(guild.id)
                await self._ensure_playing(player)
            elif here is None and home is not None and after.channel.id == home.id:
                await self._handle_first_join(guild, home)

        if before.channel is not None and before.channel != after.channel:
            if here is not None and before.channel.id == here.id:
                if not self._humans(before.channel):
                    self._schedule_leave(guild, before.channel)

    async def _on_own_voice_update(
        self, guild: discord.Guild, after: discord.VoiceState
    ) -> None:
        if after.channel is None:
            self._cancel_leave(guild.id)
            self._current.pop(guild.id, None)
            return
        if not isinstance(after.channel, discord.VoiceChannel):
            return
        self._cancel_leave(guild.id)
        player = self._get_player(guild)
        if self._humans(after.channel):
            if player:
                await self._ensure_playing(player)
            return
        self._schedule_leave(guild, after.channel)

    async def _handle_first_join(
        self, guild: discord.Guild, channel: discord.VoiceChannel
    ) -> None:
        self._cancel_leave(guild.id)
        player = self._get_player(guild)
        if player is not None and player.channel is not None and player.channel.id != channel.id:
            return
        if player is None:
            player = await self._connect_player(channel)
            if player is None:
                return
            logger.info("Rejoint le salon vocal radio de %s.", guild.name)
        await self._ensure_playing(player)

    def _cancel_leave(self, guild_id: int) -> None:
        task = self._disconnect_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_leave(self, guild: discord.Guild, channel: discord.VoiceChannel) -> None:
        self._cancel_leave(guild.id)
        self._disconnect_tasks[guild.id] = asyncio.create_task(
            self._delayed_leave(guild, channel)
        )

    async def _delayed_leave(
        self, guild: discord.Guild, channel: discord.VoiceChannel
    ) -> None:
        nxt: Optional[discord.VoiceChannel] = None
        try:
            await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
            if self._humans(channel):
                return
            player = self._get_player(guild)
            if player is None or player.channel is None or player.channel.id != channel.id:
                return
            home = await self._home_channel(guild)
            if home is not None and channel.id != home.id:
                logger.info("Salon temporaire vide : retour radio (%s).", guild.name)
                moved = await self._move_to_voice(guild, home)
                if moved and not self._humans(home):
                    nxt = home
                elif moved:
                    await self._ensure_playing(moved)
            else:
                await player.disconnect()
                logger.info("Salon vocal vide : déconnexion de %s.", guild.name)
                self._current.pop(guild.id, None)
        except asyncio.CancelledError:
            return
        finally:
            current = self._disconnect_tasks.get(guild.id)
            if current is asyncio.current_task():
                self._disconnect_tasks.pop(guild.id, None)
        if nxt is not None:
            self._schedule_leave(guild, nxt)

    # ------------------------------------------------------------------
    # Tâche de fond
    # ------------------------------------------------------------------

    @tasks.loop(minutes=CAPACITY_CHECK_MINUTES)
    async def capacity_loop(self) -> None:
        try:
            archived = await self.library.enforce_capacity()
            await self.db.prune_play_history()
            if archived:
                logger.info("Capacité : %d morceau(x) archivé(s).", len(archived))
                preview = "\n".join(f"· {t.display}" for t in archived[:10])
            extra = len(archived) - 10
            if extra > 0:
                preview += f"\n-# +{extra} autres"
            await self._broadcast_notice(
                NoticeView(
                    "Archivage",
                    preview,
                    note=f"{len(archived)} morceau(x) archivé(s) — bibliothèque pleine.",
                )
            )
        except Exception as exc:
            logger.error("Erreur enforce_capacity : %s", exc)

    @capacity_loop.before_loop
    async def _before_capacity(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Handlers boutons (LayoutView persistants)
    # ------------------------------------------------------------------

    async def handle_vote_button(self, interaction: discord.Interaction, value: int) -> None:
        await self._cast_vote(interaction, value, from_button=True)

    async def handle_skip_button(self, interaction: discord.Interaction) -> None:
        await self._cast_skip(interaction, from_button=True)

    async def handle_pause_button(self, interaction: discord.Interaction) -> None:
        await self._toggle_pause(interaction, from_button=True)

    async def handle_queue_button(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._respond(
                interaction, view=NoticeView("Radio", "Serveur uniquement."), ephemeral=True
            )
            return
        view = await self.build_queue_view(guild)
        await self._respond(interaction, view=view, ephemeral=True)

    async def handle_queue_page(self, interaction: discord.Interaction, page: int) -> None:
        guild = interaction.guild
        if guild is None:
            return
        view = await self.build_queue_view(guild, page)
        await self._respond(interaction, view=view, edit=True)

    async def handle_queue_remove(
        self, interaction: discord.Interaction, index: int, page: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        member = interaction.user
        q = self._queue(guild.id)
        if index < 0 or index >= len(q):
            view = await self.build_queue_view(guild, page, note="Morceau introuvable.")
            await self._respond(interaction, view=view, edit=True)
            return
        item = q[index]
        can = (
            isinstance(member, discord.Member)
            and (
                item.requester_id == member.id
                or member.guild_permissions.manage_guild
            )
        )
        if not can:
            view = await self.build_queue_view(
                guild, page, note="Tu ne peux retirer que tes propres ajouts."
            )
            await self._respond(interaction, view=view, edit=True)
            return
        del q[index]
        view = await self.build_queue_view(guild, page, note="Retiré de la file.")
        await self._respond(interaction, view=view, edit=True)
        refreshed = await self.build_now_playing_view(guild)
        if refreshed:
            await self._upsert_now_playing(guild, refreshed)

    async def handle_playlist_confirm(
        self, interaction: discord.Interaction, token: str
    ) -> None:
        meta = self._pending_meta.get(token)
        items = self._pending.get(token)
        if meta is None or items is None:
            await self._respond(
                interaction,
                view=NoticeView("Playlist", "Cette confirmation a expiré."),
                edit=True,
            )
            return
        user_id, guild_id = meta
        if interaction.user.id != user_id:
            await self._respond(
                interaction,
                view=NoticeView("Playlist", "Ce n'est pas ta playlist."),
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None or guild.id != guild_id:
            return
        self.drop_pending(token)
        await interaction.response.defer()
        queued, created, skipped = await self._commit_pending(
            guild, items, user_id
        )
        await self._ensure_connected(guild, interaction)
        lines = await self._added_lines(queued[:8], created, skipped, len(items))
        note = self._added_note(len(queued), created, skipped)
        await interaction.edit_original_response(
            view=AddedView(lines, note=note)
        )

    async def handle_playlist_cancel(
        self, interaction: discord.Interaction, token: str
    ) -> None:
        meta = self._pending_meta.get(token)
        if meta and interaction.user.id != meta[0]:
            await self._respond(
                interaction,
                view=NoticeView("Playlist", "Ce n'est pas ta playlist."),
                ephemeral=True,
            )
            return
        self.drop_pending(token)
        await self._respond(
            interaction,
            view=NoticeView("Playlist", "Ajout annulé."),
            edit=True,
        )

    async def _added_lines(
        self, queued: list[QueuedItem], created: int, skipped: int, requested: int
    ) -> list[str]:
        lines: list[str] = []
        for item in queued:
            track = await self.db.get_track(item.track_id)
            lines.append(f"**{track.display if track else item.playable.title}**")
        extra = requested - len(queued) - skipped
        if extra > 0:
            lines.append(f"-# +{extra} autres déjà en file")
        return lines

    @staticmethod
    def _added_note(count: int, created: int, skipped: int) -> str:
        bits = [f"{count} ajouté(s)"]
        if created:
            bits.append(f"{created} nouveau(x) en bibliothèque")
        if skipped:
            bits.append(f"{skipped} introuvable(s) sur YouTube")
        return " · ".join(bits)

    # ------------------------------------------------------------------
    # Votes / skip / pause (slash + boutons)
    # ------------------------------------------------------------------

    async def _cast_vote(
        self, interaction: discord.Interaction, value: int, *, from_button: bool
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._guild_allowed(guild):
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Indisponible ici."),
                ephemeral=True,
            )
            return
        if not await self._user_in_voice(interaction):
            await self._deny_voice(interaction)
            return
        item = self._current.get(guild.id)
        if item is None:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Aucun morceau en cours."),
                ephemeral=True,
            )
            return
        hof_ratio, hof_min_votes = await self._hof_rules(guild)
        result = await self.library.vote(
            interaction.user.id,
            item.track_id,
            value,
            hof_ratio=hof_ratio,
            hof_min_votes=hof_min_votes,
        )
        if result is None:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Morceau introuvable."),
                ephemeral=True,
            )
            return
        verb = "Like" if value == Vote.LIKE else "Dislike"
        if not result.changed:
            note = f"{verb} déjà enregistré."
        elif result.promoted_to_hof:
            note = f"{verb} · Hall of Fame !"
            await self._broadcast_notice(
                NoticeView(
                    "Hall of Fame",
                    result.track.display,
                    note=f"{hof_ratio} % de likes · {hof_min_votes} votes min — ne s'archive plus.",
                ),
                guild=guild,
            )
        elif result.demoted_from_hof:
            note = f"{verb} · sorti du Hall of Fame"
            await self._broadcast_notice(
                NoticeView(
                    "Hall of Fame",
                    result.track.display,
                    note="Le ratio likes / dislikes n'est plus suffisant.",
                ),
                guild=guild,
            )
        else:
            note = f"{verb} · +{result.likes} / -{result.dislikes}"
        if from_button:
            await self._refresh_now_playing(interaction, note=note, ephemeral_if_slash=False)
        else:
            await self._refresh_now_playing(interaction, note=note)

    async def _cast_skip(
        self, interaction: discord.Interaction, *, from_button: bool
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._guild_allowed(guild):
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Indisponible ici."),
                ephemeral=True,
            )
            return
        if not await self._user_in_voice(interaction):
            await self._deny_voice(interaction)
            return
        player = self._get_player(guild)
        if player is None or not player.playing:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Rien n'est en cours de lecture."),
                ephemeral=True,
            )
            return
        voters = self._skip_votes.setdefault(guild.id, set())
        voters.add(interaction.user.id)
        required = self._required_skip_votes(player)
        if len(voters) >= required:
            if not interaction.response.is_done():
                if from_button:
                    await interaction.response.defer()
                else:
                    await interaction.response.send_message(
                        view=NoticeView("Skip", "Morceau passé."),
                        ephemeral=True,
                    )
            await self._play_next(player)
            if from_button:
                view = await self.build_now_playing_view(guild, note="Skip · morceau passé.")
                if view is not None:
                    try:
                        await interaction.edit_original_response(view=view)
                    except discord.HTTPException:
                        pass
                    await self._upsert_now_playing(guild, view)
            return
        note = f"Skip · {len(voters)}/{required} vote(s)"
        if from_button:
            await self._refresh_now_playing(interaction, note=note, ephemeral_if_slash=False)
        else:
            await self._refresh_now_playing(interaction, note=note)

    def _required_skip_votes(self, player: Optional[wavelink.Player]) -> int:
        if player is None:
            return 1
        channel = player.channel
        humans = [m for m in channel.members if not m.bot] if channel else []
        return max(1, math.ceil(len(humans) / 2))

    async def _toggle_pause(
        self, interaction: discord.Interaction, *, from_button: bool
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._guild_allowed(guild):
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Indisponible ici."),
                ephemeral=True,
            )
            return
        if not await self._user_in_voice(interaction):
            await self._deny_voice(interaction)
            return
        player = self._get_player(guild)
        if player is None or not player.playing:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Rien n'est en cours de lecture."),
                ephemeral=True,
            )
            return
        paused = bool(getattr(player, "paused", False))
        try:
            await player.pause(not paused)
        except Exception as exc:
            await self._respond(
                interaction,
                view=NoticeView("Radio", f"Impossible de mettre en pause : {exc}"),
                ephemeral=True,
            )
            return
        note = "En pause." if not paused else "Lecture reprise."
        if from_button:
            await self._refresh_now_playing(interaction, note=note, ephemeral_if_slash=False)
        else:
            await self._refresh_now_playing(interaction, note=note)

    # ------------------------------------------------------------------
    # Commandes slash
    # ------------------------------------------------------------------

    @app_commands.command(name="add", description="Ajoute un morceau ou une playlist à la file.")
    @app_commands.describe(query="Lien Spotify/YouTube ou termes de recherche")
    async def add(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return
        try:
            pending, kind = await self._pending_from_query(query)
        except ValueError as exc:
            await interaction.followup.send(view=NoticeView("Erreur", str(exc)), ephemeral=True)
            return
        except RuntimeError as exc:
            await interaction.followup.send(view=NoticeView("Erreur", str(exc)), ephemeral=True)
            return

        total = len(pending)
        if total == 0:
            await interaction.followup.send(
                view=NoticeView("Erreur", "Aucun morceau à ajouter."),
                ephemeral=True,
            )
            return

        if total > PLAYLIST_CONFIRM_AFTER:
            capped = pending[:PLAYLIST_ADD_CAP]
            token = uuid.uuid4().hex
            self._pending[token] = capped
            self._pending_meta[token] = (interaction.user.id, guild.id)
            previews = [
                f"· **{p.artist} — {p.title}**" if p.artist else f"· **{p.title}**"
                for p in capped[:8]
            ]
            await interaction.followup.send(
                view=PlaylistConfirmView(
                    token=token,
                    previews=previews,
                    total=total,
                    cap=PLAYLIST_ADD_CAP,
                    source=kind,
                    bot=self.bot,
                )
            )
            return

        player = await self._ensure_connected(guild, interaction)
        if player is None and total == 1:
            # On enqueue quand même : la file attend le prochain join.
            pass

        queued, created, skipped = await self._commit_pending(
            guild, pending[:PLAYLIST_ADD_CAP], interaction.user.id
        )
        if not queued:
            await interaction.followup.send(
                view=NoticeView(
                    "Erreur",
                    "Aucune correspondance YouTube pour ce(s) morceau(x).",
                ),
                ephemeral=True,
            )
            return
        lines = []
        for item in queued[:8]:
            track = await self.db.get_track(item.track_id)
            lines.append(f"**{track.display if track else item.playable.title}**")
        await interaction.followup.send(
            view=AddedView(
                lines,
                note=self._added_note(len(queued), created, skipped),
            )
        )

    @app_commands.command(name="like", description="Like le morceau en cours.")
    async def like(self, interaction: discord.Interaction) -> None:
        await self._cast_vote(interaction, Vote.LIKE, from_button=False)

    @app_commands.command(name="dislike", description="Dislike le morceau en cours.")
    async def dislike(self, interaction: discord.Interaction) -> None:
        await self._cast_vote(interaction, Vote.DISLIKE, from_button=False)

    @app_commands.command(name="skip", description="Vote pour passer au morceau suivant.")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._cast_skip(interaction, from_button=False)

    @app_commands.command(name="nowplaying", description="Affiche le morceau en cours.")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        view = await self.build_now_playing_view(guild)
        if view is None:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Aucun morceau en cours."),
                ephemeral=True,
            )
            return
        await self._respond(interaction, view=view)

    @app_commands.command(name="queue", description="Affiche la file d'attente collaborative.")
    async def queue_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        view = await self.build_queue_view(guild)
        await self._respond(interaction, view=view, ephemeral=True)

    @app_commands.command(name="pause", description="Met en pause ou reprend la lecture.")
    async def pause_cmd(self, interaction: discord.Interaction) -> None:
        await self._toggle_pause(interaction, from_button=False)

    @app_commands.command(
        name="drag",
        description="Ramène la radio dans ton salon vocal (retour auto au salon radio).",
    )
    async def drag(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        target = member.voice.channel if member.voice else None
        if not isinstance(target, discord.VoiceChannel):
            await self._respond(
                interaction,
                view=NoticeView("Drag", "Rejoins un salon vocal d'abord."),
                ephemeral=True,
            )
            return
        home = await self._home_channel(guild)
        self._cancel_leave(guild.id)
        player = await self._move_to_voice(guild, target)
        if player is None:
            await self._respond(
                interaction,
                view=NoticeView("Drag", "Impossible de rejoindre ce salon."),
                ephemeral=True,
            )
            return
        await self._ensure_playing(player)
        if home is not None and target.id == home.id:
            body = f"De retour sur {target.mention}."
            note = "Salon radio définitif."
        elif home is not None:
            body = f"Je joue sur {target.mention}."
            note = f"Retour auto sur {home.mention} dès qu'il n'y a plus personne ici."
        else:
            body = f"Je joue sur {target.mention}."
            note = "Pas de salon radio configuré — `/radioconfig` pour en fixer un."
        await self._respond(
            interaction,
            view=NoticeView("Drag", body, note=note),
            ephemeral=True,
        )

    @app_commands.command(name="volume", description="Règle le volume de la radio (0–100).")
    @app_commands.describe(niveau="Volume entre 0 et 100")
    async def volume_cmd(
        self,
        interaction: discord.Interaction,
        niveau: app_commands.Range[int, 0, 100],
    ) -> None:
        guild = interaction.guild
        if guild is None or not self._guild_allowed(guild):
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Indisponible ici."),
                ephemeral=True,
            )
            return
        if not await self._user_in_voice(interaction):
            await self._deny_voice(interaction)
            return
        player = self._get_player(guild)
        if player is None:
            await self._respond(
                interaction,
                view=NoticeView("Radio", "Le bot n'est pas en vocal."),
                ephemeral=True,
            )
            return
        try:
            await player.set_volume(int(niveau))
        except Exception as exc:
            await self._respond(
                interaction,
                view=NoticeView("Radio", f"Volume impossible : {exc}"),
                ephemeral=True,
            )
            return
        await self.db.set_guild_setting(guild.id, SETTING_VOLUME, str(int(niveau)))
        await self._respond(
            interaction,
            view=NoticeView("Volume", f"{int(niveau)} %"),
            ephemeral=True,
        )

    @app_commands.command(name="stats", description="Statistiques de la bibliothèque communautaire.")
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        top_tracks = await self.db.top_tracks(5)
        top_contrib = await self.db.top_contributors(5)
        sizes = await self.db.library_size()
        track_lines = [
            f"`{i + 1}.` {t.display} — +{t.likes}/-{t.dislikes} · {t.play_count} écoutes"
            for i, t in enumerate(top_tracks)
        ]
        contrib_lines = [
            f"`{i + 1}.` <@{uid}> — {added} ajout(s), {likes} like(s) reçu(s)"
            for i, (uid, added, likes) in enumerate(top_contrib)
        ]
        await interaction.followup.send(
            view=StatsView(
                top_tracks=track_lines,
                top_contrib=contrib_lines,
                active=sizes.get(TrackStatus.ACTIVE, 0),
                hof=sizes.get(TrackStatus.HALL_OF_FAME, 0),
                archived=sizes.get(TrackStatus.ARCHIVED, 0),
                total=sizes.get("total", 0),
                queue_len=len(self._queue(guild.id)) if guild else 0,
            )
        )

    @app_commands.command(name="aide", description="Comment marche la radio Hz.")
    async def aide(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        hof_ratio, hof_min_votes = (
            await self._hof_rules(guild)
            if guild is not None
            else (HALL_OF_FAME_RATIO, HALL_OF_FAME_MIN_VOTES)
        )
        await self._respond(
            interaction,
            view=HelpView(hof_ratio=hof_ratio, hof_min_votes=hof_min_votes),
            ephemeral=True,
        )

    @app_commands.command(
        name="radioconfig", description="(Admin) Configure les salons vocal/texte de la radio."
    )
    @app_commands.describe(
        salon_vocal="Salon vocal dédié à la radio",
        salon_texte="Salon texte pour le panneau now playing (optionnel)",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def radioconfig(
        self,
        interaction: discord.Interaction,
        salon_vocal: discord.VoiceChannel,
        salon_texte: Optional[discord.TextChannel] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        await self.db.set_guild_setting(guild.id, SETTING_VOICE_CHANNEL, str(salon_vocal.id))
        text_target: Optional[discord.abc.Messageable] = salon_texte
        if salon_texte is not None:
            await self.db.set_guild_setting(guild.id, SETTING_TEXT_CHANNEL, str(salon_texte.id))
        note = ""
        me = guild.me
        if me is not None:
            perms = salon_vocal.permissions_for(me)
            missing = []
            if not perms.connect:
                missing.append("Connect")
            if not perms.speak:
                missing.append("Speak")
            if missing:
                note = f"Permissions manquantes dans le vocal : {', '.join(missing)}."
        await interaction.response.send_message(
            view=ConfigView(salon_vocal, text_target, note=note)
        )
        humans = self._humans(salon_vocal)
        player = self._get_player(guild)
        away = (
            player is not None
            and player.channel is not None
            and player.channel.id != salon_vocal.id
        )
        if humans and not away:
            await self._handle_first_join(guild, salon_vocal)

    @app_commands.command(
        name="hofconfig",
        description="(Admin) Ratio likes / dislikes du Hall of Fame.",
    )
    @app_commands.describe(
        ratio="Pourcentage de likes requis (ex. 75 = 75 % de likes)",
        votes="Nombre minimum de votes (likes + dislikes) avant d'évaluer le ratio",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def hofconfig(
        self,
        interaction: discord.Interaction,
        ratio: Optional[app_commands.Range[int, 1, 100]] = None,
        votes: Optional[app_commands.Range[int, 1, 500]] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        current_ratio, current_votes = await self._hof_rules(guild)
        if ratio is None and votes is None:
            await interaction.response.send_message(
                view=HofConfigView(current_ratio, current_votes),
                ephemeral=True,
            )
            return

        new_ratio = int(ratio) if ratio is not None else current_ratio
        new_votes = int(votes) if votes is not None else current_votes
        await self.db.set_guild_setting(guild.id, SETTING_HOF_RATIO, str(new_ratio))
        await self.db.set_guild_setting(guild.id, SETTING_HOF_MIN_VOTES, str(new_votes))
        promoted, demoted = await self.library.apply_hof_rules(
            hof_ratio=new_ratio, hof_min_votes=new_votes
        )
        note_bits = []
        if promoted:
            note_bits.append(f"{promoted} promu(s)")
        if demoted:
            note_bits.append(f"{demoted} sorti(s)")
        note = " · ".join(note_bits)
        await interaction.response.send_message(
            view=HofConfigView(new_ratio, new_votes, note=note)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Radio(bot))
