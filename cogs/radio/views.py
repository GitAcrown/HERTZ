"""LayoutViews Components v2 — même philosophie que MARIA_R.

Structure type :
    Container(
        ## titre,
        -# méta,
        Separator,
        corps,
        Separator + ActionRow(s),
        -# note
    )

Les boutons du panneau « en cours » ont des ``custom_id`` stables pour survivre
à un redémarrage du bot (vue persistante enregistrée au chargement du cog).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import discord

from .models import TrackStatus

if TYPE_CHECKING:
    from .radio import Radio

_VIEW_TIMEOUT = 300
_CONFIRM_TIMEOUT = 120
QUEUE_PAGE = 8
_PREVIEW = 8

# custom_id stables — ne pas renommer sans casser les anciens messages.
CID_LIKE = "hz:like"
CID_DISLIKE = "hz:dislike"
CID_SKIP = "hz:skip"
CID_PAUSE = "hz:pause"
CID_QUEUE = "hz:queue"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clip(text: str, n: int) -> str:
    raw = (text or "").strip().replace("\n", " ")
    if len(raw) <= n:
        return raw
    return raw[: n - 1] + "…"


def _ui_note(note: str) -> str:
    text = (note or "").strip()
    if not text:
        return ""
    return text if text.startswith("-#") else f"-# {text}"


def _append_controls(
    children: list[discord.ui.Item],
    *,
    note: str = "",
    rows: list[discord.ui.ActionRow] | None = None,
) -> None:
    notif = _ui_note(note)
    if notif:
        children += [discord.ui.Separator(), discord.ui.TextDisplay(notif)]
    if rows:
        children.append(discord.ui.Separator())
        for i, row in enumerate(rows):
            if i:
                children.append(discord.ui.Separator())
            children.append(row)


def section_with_thumbnail(body: discord.ui.Item, url: Optional[str]) -> discord.ui.Item:
    if not url:
        return body
    try:
        return discord.ui.Section(body, accessory=discord.ui.Thumbnail(url))
    except Exception:
        return body


def _radio(interaction: discord.Interaction) -> Optional["Radio"]:
    return interaction.client.get_cog("Radio")  # type: ignore[return-value]


def fmt_duration(ms: Optional[int]) -> str:
    if not ms or ms <= 0:
        return ""
    total = int(ms // 1000)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


# ---------------------------------------------------------------------------
# État now playing (DTO — pas de Wavelink ici)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class NowPlayingState:
    guild_id: int
    display: str
    via_radio: bool
    added_by: int
    likes: int
    dislikes: int
    play_count: int
    status: str
    artwork: Optional[str]
    skip_votes: int
    skip_required: int
    paused: bool
    queue_len: int
    duration_label: str
    note: str = ""


# ---------------------------------------------------------------------------
# Vues lecture seule / notices
# ---------------------------------------------------------------------------

class NoticeView(discord.ui.LayoutView):
    """Message court (erreur, confirmation, Hall of Fame…)."""

    def __init__(self, title: str, body: str = "", *, note: str = "", timeout: float | None = 60):
        super().__init__(timeout=timeout)
        children: list[discord.ui.Item] = [discord.ui.TextDisplay(f"## {title}")]
        if body:
            children += [discord.ui.Separator(), discord.ui.TextDisplay(body)]
        _append_controls(children, note=note)
        self.add_item(discord.ui.Container(*children))


class HelpView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=60)
        body = (
            "**Ajouter** · `/add` un lien YouTube / Spotify ou une recherche.\n"
            "**File** · ce que vous ajoutez passe **avant** la radio.\n"
            "**Voter** · Like / Dislike / Skip depuis le panneau, en vocal.\n"
            "**Hall of Fame** · 15 likes : le morceau ne s’archive plus.\n"
            "**Bibliothèque** · ~100 actifs ; le reste s’archive tout seul."
        )
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Radio Hz"),
            discord.ui.TextDisplay("-# Queue collaborative · mode radio dès que la file est vide"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(body),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "-# Commandes · `/add` `/queue` `/nowplaying` `/stats` `/pause` `/volume`"
            ),
        ]
        self.add_item(discord.ui.Container(*children))


class ConfigView(discord.ui.LayoutView):
    def __init__(self, voice: discord.VoiceChannel, text: Optional[discord.abc.Messageable], *, note: str = ""):
        super().__init__(timeout=60)
        lines = [f"**Salon vocal** · {voice.mention}"]
        if text is not None and hasattr(text, "mention"):
            lines.append(f"**Annonces** · {text.mention}")
        else:
            lines.append("**Annonces** · non défini (le panneau ne sera pas posté)")
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Radio configurée"),
            discord.ui.TextDisplay("-# Un salon, une file, une bibliothèque"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines)),
        ]
        _append_controls(children, note=note)
        self.add_item(discord.ui.Container(*children))


class StatsView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        top_tracks: list[str],
        top_contrib: list[str],
        active: int,
        hof: int,
        archived: int,
        total: int,
        queue_len: int,
    ) -> None:
        super().__init__(timeout=60)
        tracks = "\n".join(top_tracks) if top_tracks else "-# Aucun morceau pour l’instant."
        contrib = "\n".join(top_contrib) if top_contrib else "-# Personne n’a encore ajouté."
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Statistiques"),
            discord.ui.TextDisplay(
                f"-# File en attente · {queue_len} · bibliothèque {active} actifs"
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay("**Top morceaux**\n" + tracks),
            discord.ui.Separator(),
            discord.ui.TextDisplay("**Top contributeurs**\n" + contrib),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                f"**Bibliothèque** · {active} actifs · {hof} Hall of Fame · "
                f"{archived} archivés · {total} total"
            ),
        ]
        self.add_item(discord.ui.Container(*children))


class AddedView(discord.ui.LayoutView):
    def __init__(self, lines: list[str], *, note: str = "", show_queue: bool = True):
        super().__init__(timeout=_VIEW_TIMEOUT)
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Ajouté"),
            discord.ui.TextDisplay("-# Prioritaire sur le mode radio"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines) if lines else "-# Rien à afficher."),
        ]
        rows = [discord.ui.ActionRow(QueueButton())] if show_queue else None
        _append_controls(children, note=note, rows=rows)
        self.add_item(discord.ui.Container(*children))


# ---------------------------------------------------------------------------
# Now playing
# ---------------------------------------------------------------------------

class NowPlayingView(discord.ui.LayoutView):
    def __init__(self, state: NowPlayingState) -> None:
        super().__init__(timeout=None)
        source = "Mode Radio" if state.via_radio else "File"
        pause_bit = "en pause" if state.paused else "en cours"
        dur = f" · {state.duration_label}" if state.duration_label else ""
        meta = f"-# {source} · {pause_bit}{dur} · file {state.queue_len}"
        if state.status == TrackStatus.HALL_OF_FAME:
            meta += " · Hall of Fame"

        header_lines = [f"## {state.display}", meta]
        if not state.via_radio:
            header_lines.append(f"Ajouté par <@{state.added_by}>")
        header = discord.ui.TextDisplay("\n".join(header_lines))
        main = section_with_thumbnail(header, state.artwork)

        stats = (
            f"**Score** · +{state.likes} / -{state.dislikes}\n"
            f"**Écoutes** · {state.play_count}\n"
            f"**Skip** · {state.skip_votes}/{state.skip_required}"
        )
        children: list[discord.ui.Item] = [
            main,
            discord.ui.Separator(),
            discord.ui.TextDisplay(stats),
        ]
        pause_label = "Reprendre" if state.paused else "Pause"
        rows = [
            discord.ui.ActionRow(LikeButton(), DislikeButton(), SkipButton()),
            discord.ui.ActionRow(PauseButton(pause_label), QueueButton()),
        ]
        _append_controls(children, note=state.note, rows=rows)
        self.add_item(discord.ui.Container(*children))


def persistent_now_playing_stub() -> discord.ui.LayoutView:
    """Vue minimale enregistrée au boot pour router les clics post-restart."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.ActionRow(LikeButton(), DislikeButton(), SkipButton()))
    view.add_item(discord.ui.ActionRow(PauseButton(), QueueButton()))
    return view


class LikeButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.success, label="Like", custom_id=CID_LIKE)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_vote_button(interaction, 1)


class DislikeButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.danger, label="Dislike", custom_id=CID_DISLIKE)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_vote_button(interaction, -1)


class SkipButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Skip", custom_id=CID_SKIP)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_skip_button(interaction)


class PauseButton(discord.ui.Button):
    def __init__(self, label: str = "Pause") -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label=label, custom_id=CID_PAUSE)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_pause_button(interaction)


class QueueButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="File", custom_id=CID_QUEUE)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_queue_button(interaction)


# ---------------------------------------------------------------------------
# File d'attente
# ---------------------------------------------------------------------------

class QueueView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        guild_id: int,
        lines: list[str],
        total: int,
        page: int,
        page_count: int,
        removable: list[tuple[str, str]],
        note: str = "",
    ) -> None:
        super().__init__(timeout=_VIEW_TIMEOUT)
        subtitle = "-# Prioritaire sur la radio"
        if total:
            subtitle += f" · {total} en attente"
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## File"),
            discord.ui.TextDisplay(subtitle),
        ]
        if not lines:
            children.append(discord.ui.TextDisplay("-# File vide — la radio prend le relais."))
        else:
            children += [
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n\n".join(lines)),
                discord.ui.TextDisplay(f"-# Page {page + 1}/{page_count}"),
            ]

        rows: list[discord.ui.ActionRow] = []
        if removable:
            rows.append(discord.ui.ActionRow(_RemoveQueuedSelect(guild_id, removable, page)))
        actions: list[discord.ui.Button] = []
        if page > 0:
            actions.append(_QueuePageButton("Précédent", guild_id, page - 1))
        if page + 1 < page_count:
            actions.append(_QueuePageButton("Suivant", guild_id, page + 1))
        if actions:
            rows.append(discord.ui.ActionRow(*actions[:5]))
        _append_controls(children, note=note, rows=rows or None)
        self.add_item(discord.ui.Container(*children))


class _QueuePageButton(discord.ui.Button):
    def __init__(self, label: str, guild_id: int, page: int) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label=label)
        self.guild_id = guild_id
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_queue_page(interaction, self.page)


class _RemoveQueuedSelect(discord.ui.Select):
    def __init__(self, guild_id: int, options: list[tuple[str, str]], page: int) -> None:
        opts = [
            discord.SelectOption(label=_clip(label, 100) or "Morceau", value=value)
            for label, value in options[:25]
        ]
        super().__init__(placeholder="Retirer un morceau…", min_values=1, max_values=1, options=opts)
        self.guild_id = guild_id
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if not cog:
            return
        raw = (self.values or ["-1"])[0]
        try:
            index = int(raw)
        except ValueError:
            index = -1
        await cog.handle_queue_remove(interaction, index, self.page)


# ---------------------------------------------------------------------------
# Confirmation playlist
# ---------------------------------------------------------------------------

class PlaylistConfirmView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        token: str,
        previews: list[str],
        total: int,
        cap: int,
        source: str,
        bot: discord.Client,
    ) -> None:
        super().__init__(timeout=_CONFIRM_TIMEOUT)
        self.token = token
        self.bot = bot
        clipped = total > cap
        shown = min(total, cap)
        subtitle = f"-# {source} · {shown} morceau(x) seront ajoutés"
        if clipped:
            subtitle += f" (plafond {cap}, {total} trouvés)"
        preview = "\n".join(previews[:_PREVIEW]) if previews else "-# (aperçu vide)"
        extra = total - _PREVIEW
        if extra > 0:
            preview += f"\n-# +{min(extra, cap - _PREVIEW) if cap > _PREVIEW else extra} autres"
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay("## Playlist"),
            discord.ui.TextDisplay(subtitle),
            discord.ui.Separator(),
            discord.ui.TextDisplay(preview),
        ]
        rows = [discord.ui.ActionRow(
            _ConfirmPlaylistButton(token, shown),
            _CancelPlaylistButton(token),
        )]
        _append_controls(children, rows=rows)
        self.add_item(discord.ui.Container(*children))

    async def on_timeout(self) -> None:
        cog = self.bot.get_cog("Radio")
        drop = getattr(cog, "drop_pending", None)
        if callable(drop):
            drop(self.token)


class _ConfirmPlaylistButton(discord.ui.Button):
    def __init__(self, token: str, count: int) -> None:
        super().__init__(style=discord.ButtonStyle.success, label=f"Ajouter {count}")
        self.token = token

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_playlist_confirm(interaction, self.token)


class _CancelPlaylistButton(discord.ui.Button):
    def __init__(self, token: str) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Annuler")
        self.token = token

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _radio(interaction)
        if cog:
            await cog.handle_playlist_cancel(interaction, self.token)
