"""
Discord Verification Bot  (v2.2)
=======================================
What's new in v2.2 (visual refresh):
    - All colored circles (🟡 🟢 🔴 🟣 ⚪ 🟠 🔵) are gone. Everything now uses your purple custom emojis.
    - Cleaner verification card with an emoji on every field.
    - Buttons (Verify / Reject / Claim / Resolve / Seen / Describe Issue / Cancel) have custom emojis.
    - Footer texts are unchanged on purpose (the bot's logic compares against them).

v2.1 (verification alerts in #verify):
    - Reject asks the moderator for a REASON (pop-up); the member gets a DM with it (DM_ON_REJECT).
    - Rejoin spam control: while a request is open, rejoining only updates a counter on the same card.
    - A rejected member who comes back gets a new card that shows the previous rejection reason.
    - If the member leaves the server while their request is open, the card is closed ("Member left").

v2 (support panel in #describe-issue):
    - ANYONE can press "Describe Issue" - no voice channel needed.
    - A member is added to "Current queue" when they SEND their issue.
    - Members not in a voice channel get a short self-deleting ping when a moderator claims/resolves.

Requirements:
    pip install "discord.py>=2.4"

Before running:
    - Enable SERVER MEMBERS INTENT for your bot in the Discord Developer Portal.
    - Give the bot "Manage Nicknames" and put the bot's role ABOVE the roles it must rename.
    - The bot must be in the server that owns the custom emojis.
    - Set DISCORD_TOKEN as an environment variable (Railway > Variables).
"""

from __future__ import annotations

import os
import re
import asyncio
import time
from datetime import timedelta

import discord
from discord.ext import commands

# =========================== CONFIG ===========================
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707  # ELT server ID

# Channels
VERIFICATION_CHANNEL_ID = 1542531178526146670  # channel where verify requests get posted
WELCOME_CHANNEL_ID = None                        # welcome channel (optional)
WAITING_VC_ID = 1513904254535073883              # the "Waiting for Move" voice channel

HELP_ALERT_CHANNEL_ID = 1551163762084683866  # channel where the mod alert gets posted
MEMBER_HELP_PANEL_CHANNEL_ID = 1553786062579699722  # #describe-issue (the shared panel lives here)
ISSUE_REPORTS_CHANNEL_ID = 1553196616146493460  # channel where "Describe Issue" submissions get posted

# Per-VC settings for the "waiting for help" flow:
#   emoji: shown in the mod alert title | send_member_panel: list members on the shared panel queue
HELP_VC_CONFIG = {
    1517941411151085691: {"emoji": "", "send_member_panel": True},
}
# Used for requests sent from the panel by members who are NOT in a help voice channel.
DEFAULT_HELP_CONFIG = next(iter(HELP_VC_CONFIG.values()), {"emoji": "", "send_member_panel": True})

# Nickname emojis
WAITING_VC_EMOJI = "⏳"
REPORT_VC_EMOJI = "📛"
LEGACY_ALERT_EMOJIS = {"⛔"}
REPORT_VC_IDS = {
    1517940974125318166,
    1554981588880850964,
}
REPORT_CATEGORY_ID = 1517941029221695760
REPORT_CHANNEL_NAME = "📛┃𝗥𝗘𝗣𝗢𝗥𝗧"
LEGACY_REPORT_CHANNEL_NAMES = {"⛔┃𝗥𝗘𝗣𝗢𝗥𝗧"}

ALERT_VC_EMOJIS = {
    WAITING_VC_ID: WAITING_VC_EMOJI,
    **{vc_id: WAITING_VC_EMOJI for vc_id in HELP_VC_CONFIG},
    **{vc_id: REPORT_VC_EMOJI for vc_id in REPORT_VC_IDS},
}
ALERT_VC_IDS = set(ALERT_VC_EMOJIS)
ALERT_EMOJIS = set(ALERT_VC_EMOJIS.values())
_STRIPPABLE_EMOJIS = sorted(ALERT_EMOJIS | LEGACY_ALERT_EMOJIS, key=len, reverse=True)

# Roles
UNVERIFIED_ROLE_ID = 1513904174079934657
VERIFIED_ROLE_ID = 1513904156350353511
EXTRA_ROLES_ON_VERIFY = [1513904151309058159]  # MEMBER role

# ---------------- Custom emojis (from your server) ----------------
E_HOURGLASS = "<:hourglass:1553596332025974864>"
E_CHECK     = "<:positivo:1553555472811040788>"
E_X         = "<:purple_x:1554671367142645770>"
E_WARN      = "<:purple_warning:1554671365490212945>"
E_SHIELD_OK = "<:shield_check:1553472890601734325>"
E_SHIELD_X  = "<:shield_x:1553472897757224980>"
E_PEOPLE    = "<:people:1553472892111560754>"
E_SMILEY    = "<:smile:1553472899589873784>"
E_KEY       = "<:key:1553558393023897610>"
E_LINK      = "<:link:1554675609525821531>"
E_EXIT      = "<:leave:1553472901301280870>"
E_SPEAKER   = "<:speaker:1553558390397993070>"
E_PENCIL    = "<:pencil:1553472888906977300>"
E_CROWN     = "<:crown:1554186011809021953>"

VERIFY_EMOJI = discord.PartialEmoji(name="positivo", id=1553555472811040788)
REJECT_EMOJI = discord.PartialEmoji(name="purple_x", id=1554671367142645770)
CLAIM_EMOJI = discord.PartialEmoji(name="crown", id=1554186011809021953)
RESOLVE_EMOJI = discord.PartialEmoji(name="shield_check", id=1553472890601734325)
SEEN_EMOJI = discord.PartialEmoji(name="positivo", id=1553555472811040788)
DESCRIBE_EMOJI = discord.PartialEmoji(name="pencil", id=1553472888906977300)

STATUS_WAITING  = f"{E_HOURGLASS} **Waiting**"
STATUS_CLAIMED  = f"{E_CROWN} **Claimed**"
STATUS_VERIFIED = f"{E_SHIELD_OK} **Verified**"
STATUS_REJECTED = f"{E_SHIELD_X} **Rejected**"
STATUS_LEFT     = f"{E_EXIT} **Member left**"

# Panel (queue) statuses
PS_WAITING = f"{E_HOURGLASS} Waiting for a moderator"
PS_SENT = f"{E_HOURGLASS} Message sent — waiting for a moderator"
PS_DISCONNECTED = f"{E_EXIT} Disconnected — waiting for them to reconnect"


def ps_claimed(mention: str) -> str:
    return f"{E_CROWN} {mention} is handling your request"


# New-account warning (0 = off)
NEW_ACCOUNT_WARNING_DAYS = 7
WARN_EMOJI = E_WARN

# Anti-spam
VERIFY_REALERT_COOLDOWN = 5 * 60   # after a request was handled, a new card is only sent after this
REJOIN_PING_COOLDOWN = 5 * 60      # while a request is open: "still waiting" reminder at most this often
REMINDER_DELETE_AFTER = 10 * 60    # the reminder ping cleans itself up after this many seconds
HELP_LEAVE_GRACE = 20
REPORT_ROOM_COOLDOWN = 10

# Rejecting
DM_ON_REJECT = True                # DM the member the reason when a moderator rejects them
REJECT_REASON_MAX = 300
# ================================================================

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.invites = True

bot = commands.Bot(command_prefix="!", intents=intents)

invite_cache = {}
member_inviters = {}
report_vc_base_nicknames = {}
_nick_locks: dict[int, asyncio.Lock] = {}
_nick_failed: set[int] = set()

# help_panels[member_id] = {
#     "alert": the mod alert message (or None while it is being posted),
#     "origin": "vc" (joined the help VC) or "panel" (sent from #describe-issue),
#     "queued": True once the member SENT their issue -> only queued members are listed on the panel,
#     "show": the VC config allows listing on the panel,
#     "described", "claimed", "status", "joined_at", "queued_at"
# }
help_panels = {}

verify_alerts: dict[int, dict] = {}
_verify_locks: dict[int, asyncio.Lock] = {}
member_report_rooms: dict[int, int] = {}
_report_locks: dict[int, asyncio.Lock] = {}
_report_last_created: dict[int, float] = {}
_leave_tasks: dict[int, asyncio.Task] = {}
VERIFY_OPEN_FOOTER = "Click Verify to let this member in"
VERIFY_DONE_FOOTERS = ("Verified ☑️", "Verified ✅", "Rejected ❌", "Member left ❌")

shared_panel: discord.Message | None = None
_panel_lock = asyncio.Lock()

_startup_sync_done = False

ALERT_DONE_FOOTERS = ("Resolved", "Member left", "Cancelled", "Resolved ✅", "Resolved ☑️", "Member left ❌", "Cancelled ⚪")
ALERT_STATUS = {
    "Resolved": f"{E_SHIELD_OK} **Resolved**",
    "Member left": f"{E_EXIT} **Member left**",
    "Cancelled": f"{E_X} **Cancelled**",
    "Member moved": f"{E_PEOPLE} **Member moved**",
}

# Short self-deleting @mention in #describe-issue (edits to the panel never notify anyone).
PING_ON_JOIN = True           # short ping when a member joins Waiting for Help (deletes itself)
PING_DELETE_AFTER = 8
JOIN_PING_TEXT = "your request is in — check the panel and press **Describe Issue** to tell us what is wrong."
NOTIFY_DELETE_AFTER = 12      # "a moderator picked up your request" pings stay a bit longer
SUPPORT_TITLE_EMOJI = "<:support:1555003977173573684>"
CANCEL_EMOJI = discord.PartialEmoji(name="cancel", id=1554671367142645770, animated=False)
PANEL_TITLE = f"{SUPPORT_TITLE_EMOJI} Support Request Received"
PURPLE = discord.Color.purple()
_bg_tasks: set = set()


async def cache_invites(guild: discord.Guild):
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {
            invite.code: {"uses": invite.uses or 0, "max_uses": invite.max_uses, "inviter": invite.inviter}
            for invite in invites
        }
        print(f"✅ Cached {len(invites)} invites for guild {guild.id}")
    except discord.Forbidden:
        print(f"⚠️ Bot doesn't have permission to view invites in guild {guild.id}")
    except discord.HTTPException as e:
        print(f"⚠️ Failed to cache invites: {e}")


async def send_with_retry(channel, **kwargs):
    max_attempts = 5
    backoff_delays = [1, 2, 4, 8, 16]
    for attempt in range(max_attempts):
        try:
            return await channel.send(**kwargs)
        except discord.HTTPException as e:
            if e.status == 429 and attempt < max_attempts - 1:
                delay = backoff_delays[attempt]
                print(f"⏳ Rate limited, retrying in {delay}s (attempt {attempt + 1}/{max_attempts})")
                await asyncio.sleep(delay)
            else:
                raise


def set_field(embed: discord.Embed, name: str, value: str, inline: bool = False):
    for i, f in enumerate(embed.fields):
        if f.name == name:
            embed.set_field_at(i, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


def get_field(embed: discord.Embed, name: str) -> str | None:
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


# ====================== Nickname emoji (⏳ waiting / 📛 report) ======================
report_room_ids: set[int] = set()


def _is_report_room(channel) -> bool:
    if channel is None or channel.id in REPORT_VC_IDS:
        return False
    if channel.id in report_room_ids:
        return True
    return (
        getattr(channel, "category_id", None) == REPORT_CATEGORY_ID
        and (channel.name == REPORT_CHANNEL_NAME or channel.name in LEGACY_REPORT_CHANNEL_NAMES)
    )


def _alert_emoji_for(member: discord.Member) -> str | None:
    channel = member.voice.channel if member.voice else None
    if channel is None:
        return None
    if _is_report_room(channel):
        return REPORT_VC_EMOJI
    return ALERT_VC_EMOJIS.get(channel.id)


async def create_report_room(member: discord.Member):
    guild = member.guild
    lock = _report_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        def _in_trigger() -> bool:
            m = guild.get_member(member.id)
            return bool(m and m.voice and m.voice.channel and m.voice.channel.id in REPORT_VC_IDS)

        if not _in_trigger():
            return

        room = None
        created_new = False
        existing_id = member_report_rooms.get(member.id)
        existing = guild.get_channel(existing_id) if existing_id else None
        if isinstance(existing, discord.VoiceChannel) and _is_report_room(existing):
            room = existing
        else:
            wait = REPORT_ROOM_COOLDOWN - (time.monotonic() - _report_last_created.get(member.id, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
                if not _in_trigger():
                    return

            category = guild.get_channel(REPORT_CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                print("⚠️ Couldn't find the REPORT category — check REPORT_CATEGORY_ID")
                return
            try:
                room = await guild.create_voice_channel(
                    REPORT_CHANNEL_NAME,
                    category=category,
                    overwrites=category.overwrites,
                    reason=f"Report room for {member}",
                )
            except discord.Forbidden:
                print("⚠️ Can't create the report room — give the bot 'Manage Channels'")
                return
            except discord.HTTPException as e:
                print(f"❌ Failed to create report room: {e}")
                return
            created_new = True
            report_room_ids.add(room.id)
            member_report_rooms[member.id] = room.id
            _report_last_created[member.id] = time.monotonic()

        try:
            await member.move_to(room, reason="Moved into their report room")
            print(f"✅ Report room {'created' if created_new else 'reused'} for {member}")
            await asyncio.sleep(0.5)
            await set_report_vc_alert(member)
        except discord.Forbidden:
            print("⚠️ Can't move the member — give the bot 'Move Members'")
            if created_new:
                await room.delete(reason="Couldn't move the member in")
        except discord.HTTPException as e:
            print(f"⚠️ Couldn't move {member} into the report room: {e}")
            if created_new:
                try:
                    await room.delete(reason="Member was no longer in voice")
                except discord.HTTPException:
                    pass


async def delete_if_empty_report_room(channel):
    if not _is_report_room(channel):
        return
    if any(not m.bot for m in channel.members):
        return
    try:
        await channel.delete(reason="Report room is empty")
        report_room_ids.discard(channel.id)
        for mid, rid in list(member_report_rooms.items()):
            if rid == channel.id:
                del member_report_rooms[mid]
        print(f"🗑️ Deleted empty report room {channel.id}")
    except discord.HTTPException:
        pass


def _in_alert_vc(member: discord.Member) -> bool:
    return _alert_emoji_for(member) is not None


def _strip_alert_prefix(nick: str | None) -> tuple[str | None, bool]:
    had_prefix = False
    while nick:
        for emoji in _STRIPPABLE_EMOJIS:
            if nick.startswith(emoji):
                nick = nick[len(emoji):].lstrip()
                had_prefix = True
                break
        else:
            break
    if had_prefix and not nick:
        nick = None
    return nick, had_prefix


_EMOJI_CHARS = (
    "\U0001F000-\U0001FAFF"
    "\U000E0020-\U000E007F"
    "\u00A9\u00AE\u203C\u2049\u2122\u2139"
    "\u2194-\u21AA\u221E"
    "\u231A-\u23FF"
    "\u24C2\u25AA-\u25FE"
    "\u2600-\u27BF"
    "\u2934\u2935\u2B00-\u2BFF\u3030\u303D\u3297\u3299"
    "\u200D\uFE0F\u20E3"
)
_LEADING_EMOJI_RE = re.compile(f"^[{_EMOJI_CHARS}\\s]+")


def _strip_leading_emoji(text: str | None) -> str:
    if not text:
        return ""
    return _LEADING_EMOJI_RE.sub("", text).strip()


def _has_exact_prefix(nick: str | None, emoji: str) -> bool:
    if not nick or not nick.startswith(f"{emoji} "):
        return False
    rest = nick[len(emoji) + 1:]
    return bool(rest) and not _LEADING_EMOJI_RE.match(rest)


def get_report_vc_base_nickname(member: discord.Member) -> str | None:
    if member.id not in report_vc_base_nicknames:
        nick, had_prefix = _strip_alert_prefix(member.nick)
        if had_prefix and (not nick or nick in (member.name, member.global_name)):
            nick = None
        report_vc_base_nicknames[member.id] = nick
    return report_vc_base_nicknames[member.id]


async def set_report_vc_alert(member: discord.Member):
    """Make the member's nickname match where they are RIGHT NOW (⏳ waiting / 📛 report / original)."""
    lock = _nick_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        fresh = member.guild.get_member(member.id) or member
        emoji = _alert_emoji_for(fresh)
        active = emoji is not None

        if active:
            base_nick = get_report_vc_base_nickname(fresh)
            visible_name = ""
            for candidate in (base_nick, fresh.global_name, fresh.name):
                visible_name = _strip_leading_emoji(candidate)
                if visible_name:
                    break
            visible_name = visible_name or fresh.name
            target_nick = f"{emoji} {visible_name}"[:32]
        else:
            _, had_prefix = _strip_alert_prefix(fresh.nick)
            if had_prefix:
                target_nick = get_report_vc_base_nickname(fresh)
            else:
                report_vc_base_nicknames.pop(fresh.id, None)
                return

        if fresh.nick == target_nick:
            if not active:
                report_vc_base_nicknames.pop(fresh.id, None)
            return

        try:
            await fresh.edit(nick=target_nick, reason="Waiting/help/report VC occupancy changed")
            print(f"✏️ {fresh} nickname -> {target_nick!r}")
            if not active:
                report_vc_base_nicknames.pop(fresh.id, None)
        except discord.Forbidden:
            if fresh.id == fresh.guild.owner_id:
                reason = "Discord never lets a bot change the server owner's nickname"
            else:
                reason = "give the bot 'Manage Nicknames' and move the bot's role ABOVE this member's top role"
            print(f"⚠️ Can't change {fresh}'s nickname — {reason}")
            _nick_failed.add(fresh.id)
            if not active:
                report_vc_base_nicknames.pop(fresh.id, None)
        except discord.HTTPException as e:
            print(f"⚠️ Failed to update {fresh}'s nickname: {e}")


# ====================== Persistent buttons ======================
def _already_done(message: discord.Message, done_footers: tuple[str, ...]) -> bool:
    return bool(message.embeds and message.embeds[0].footer.text in done_footers)


async def dm_rejected_member(guild: discord.Guild, member_id: int, reason: str) -> bool:
    """DMs the rejected member the reason. Returns True if the DM went through."""
    member = guild.get_member(member_id)
    if member is None:
        return False
    embed = discord.Embed(
        title=f"{E_SHIELD_X} Verification not accepted",
        description=(
            f"A moderator reviewed your request in **{guild.name}** and could not verify you right now.\n"
            f"-# You are welcome to join the voice channel again once the issue is fixed."
        ),
        color=PURPLE,
    )
    embed.add_field(name=f"{E_PENCIL} Reason", value=f">>> {reason}"[:1024], inline=False)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    embed.timestamp = discord.utils.utcnow()
    try:
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


class RejectReasonModal(discord.ui.Modal, title="Reject Member"):
    reason = discord.ui.TextInput(
        label="Why is this member being rejected?",
        style=discord.TextStyle.paragraph,
        placeholder="The reason is shown on the card and sent to the member.",
        required=True,
        max_length=REJECT_REASON_MAX,
    )

    def __init__(self, member_id: int):
        super().__init__()
        self.member_id = member_id

    async def on_submit(self, interaction: discord.Interaction):
        message = interaction.message
        if message is None or not message.embeds:
            return await interaction.response.send_message("Couldn't find the request message.", ephemeral=True)
        if _already_done(message, VERIFY_DONE_FOOTERS):
            return await interaction.response.send_message("This request was already handled.", ephemeral=True)

        reason = self.reason.value.strip()
        embed = message.embeds[0].copy()
        embed.color = PURPLE
        set_field(embed, "Status", STATUS_REJECTED, inline=True)
        set_field(
            embed, "Rejected by",
            f"{E_X} {interaction.user.mention} • {discord.utils.format_dt(discord.utils.utcnow(), 'R')}",
            inline=False,
        )
        set_field(embed, "Reason", f">>> {reason}"[:1024], inline=False)
        embed.set_footer(text="Rejected ❌")
        await interaction.response.edit_message(embed=embed, view=verify_view(self.member_id, disabled=True))

        notified = False
        if DM_ON_REJECT and interaction.guild is not None:
            notified = await dm_rejected_member(interaction.guild, self.member_id, reason)
        if DM_ON_REJECT:
            await interaction.followup.send(
                "Rejected. The member was sent the reason by DM." if notified
                else "Rejected. I couldn't DM the member (their DMs are closed or they left).",
                ephemeral=True,
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Reject modal error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)


class VerifyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"verify_(?P<action>accept|reject)_(?P<uid>[0-9]+)",
):
    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "accept":
            label, style, emoji = "Verify", discord.ButtonStyle.secondary, VERIFY_EMOJI
        else:
            label, style, emoji = "Reject", discord.ButtonStyle.secondary, REJECT_EMOJI
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"verify_{action}_{member_id}",
                disabled=disabled,
            )
        )
        self.action = action
        self.member_id = member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        # Reject: ask for the reason first (the pop-up must be the first response).
        if self.action == "reject":
            if _already_done(interaction.message, VERIFY_DONE_FOOTERS):
                return await interaction.response.send_message("This request was already handled.", ephemeral=True)
            return await interaction.response.send_modal(RejectReasonModal(self.member_id))

        await interaction.response.defer()

        if _already_done(interaction.message, VERIFY_DONE_FOOTERS):
            return await interaction.followup.send("This request was already handled.", ephemeral=True)

        embed = interaction.message.embeds[0].copy()

        guild = interaction.guild
        member = guild.get_member(self.member_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.member_id)
            except discord.HTTPException:
                member = None
        if member is None:
            return await interaction.followup.send(
                "That member isn't in the server anymore (they may have left).", ephemeral=True
            )

        unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
        verified_role = guild.get_role(VERIFIED_ROLE_ID)

        try:
            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason=f"Verified by {interaction.user}")
            if verified_role:
                await member.add_roles(verified_role, reason=f"Verified by {interaction.user}")
            for rid in EXTRA_ROLES_ON_VERIFY:
                r = guild.get_role(rid)
                if r:
                    await member.add_roles(r, reason="Extra role after verification")
        except discord.Forbidden:
            return await interaction.followup.send(
                "The bot doesn't have enough permission to change roles (make sure the bot's role is above the roles it manages).",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            print(f"❌ Failed to change roles for {member}: {e}")
            return await interaction.followup.send("Something went wrong while changing roles. Try again.", ephemeral=True)

        embed.color = PURPLE
        set_field(embed, "Status", STATUS_VERIFIED, inline=True)
        set_field(
            embed, "Verified by",
            f"{E_CHECK} {interaction.user.mention} • {discord.utils.format_dt(discord.utils.utcnow(), 'R')}",
            inline=False,
        )
        embed.set_footer(text="Verified ☑️")
        member_inviters.pop(self.member_id, None)
        await interaction.edit_original_response(embed=embed, view=verify_view(self.member_id, disabled=True))

        if WELCOME_CHANNEL_ID:
            welcome_channel = guild.get_channel(WELCOME_CHANNEL_ID)
            if welcome_channel:
                try:
                    await welcome_channel.send(
                        f"{E_SMILEY} Welcome {member.mention}, you're verified — glad to have you in the server!"
                    )
                except discord.HTTPException as e:
                    print(f"⚠️ Couldn't send welcome message: {e}")


def verify_view(member_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyButton("accept", member_id, disabled))
    view.add_item(VerifyButton("reject", member_id, disabled))
    return view


# ====================== MOD panel (help alerts) ======================
def _is_claimed(embed: discord.Embed) -> bool:
    return get_field(embed, "Claimed by") is not None


def _alert_origin(msg: discord.Message) -> str:
    """'panel' if the request was sent from #describe-issue by someone who is not in a voice channel."""
    if msg.embeds:
        value = get_field(msg.embeds[0], "Voice Channel") or ""
        if "Not in a voice channel" in value:
            return "panel"
    return "vc"


def notify_member(member_id: int, text: str):
    """Short self-deleting @mention in #describe-issue (used for members who are not in voice)."""
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    member = guild.get_member(member_id)
    channel = guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if member is None or not isinstance(channel, discord.TextChannel):
        return
    task = asyncio.create_task(ping_member(channel, member, text, NOTIFY_DELETE_AFTER))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def resolve_alert(interaction: discord.Interaction, member_id: int, note: str | None = None):
    message = interaction.message
    if message is None or not message.embeds:
        return await interaction.response.send_message("Couldn't find the alert message.", ephemeral=True)
    if _already_done(message, ALERT_DONE_FOOTERS):
        return await interaction.response.send_message("This request was already closed.", ephemeral=True)

    embed = message.embeds[0].copy()
    embed.color = PURPLE
    if note:
        set_field(embed, "What happened", f">>> {note}"[:1024], inline=False)
    set_field(embed, "Status", ALERT_STATUS["Resolved"], inline=True)
    set_field(embed, "Resolved by", interaction.user.mention, inline=True)
    embed.set_footer(text="Resolved", icon_url=embed.footer.icon_url)
    await interaction.response.edit_message(embed=embed, view=help_view(member_id, disabled=True))

    await close_help_request(
        member_id,
        f"{E_SHIELD_OK} Resolved by {interaction.user.mention}",
        PURPLE,
        notify=f"your request was resolved by {interaction.user.mention}. Thank you for your patience!",
    )


class ResolveNoteModal(discord.ui.Modal, title="Resolve Help Request"):
    note = discord.ui.TextInput(
        label="What was the problem? (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="What happened / why they needed help...",
        required=False,
        max_length=500,
    )

    def __init__(self, member_id: int):
        super().__init__()
        self.member_id = member_id

    async def on_submit(self, interaction: discord.Interaction):
        await resolve_alert(interaction, self.member_id, self.note.value or None)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Resolve modal error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)


class HelpButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"help_(?P<action>claim|resolved|note)_(?P<uid>[0-9]+)",
):
    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "claim":
            label, style, emoji = "Claim", discord.ButtonStyle.secondary, CLAIM_EMOJI
        elif action == "resolved":
            label, style, emoji = "Mark as Resolved", discord.ButtonStyle.secondary, RESOLVE_EMOJI
        else:
            label, style, emoji = "Resolve", discord.ButtonStyle.secondary, RESOLVE_EMOJI
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"help_{action}_{member_id}",
                disabled=disabled,
            )
        )
        self.action = action
        self.member_id = member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        message = interaction.message
        if message is None or not message.embeds:
            return await interaction.response.send_message("Couldn't find the alert message.", ephemeral=True)
        if _already_done(message, ALERT_DONE_FOOTERS):
            return await interaction.response.send_message("This request was already closed.", ephemeral=True)

        if self.action == "resolved":
            await resolve_alert(interaction, self.member_id)

        elif self.action == "note":
            await interaction.response.send_modal(ResolveNoteModal(self.member_id))

        else:  # claim
            embed = message.embeds[0].copy()
            claimed_by = get_field(embed, "Claimed by")
            if claimed_by:
                return await interaction.response.send_message(
                    f"Already claimed by {claimed_by}.", ephemeral=True
                )
            embed.color = PURPLE
            set_field(embed, "Status", STATUS_CLAIMED, inline=True)
            set_field(embed, "Claimed by", interaction.user.mention, inline=True)
            await interaction.response.edit_message(embed=embed, view=help_view(self.member_id, claimed=True))

            entry = help_panels.get(self.member_id)
            if entry is not None:
                entry["claimed"] = True
            await set_panel_status(
                self.member_id,
                ps_claimed(interaction.user.mention),
                PURPLE,
            )
            # A member who is not in voice has no other way to know -> short ping in #describe-issue.
            if entry is not None and entry.get("origin") == "panel":
                notify_member(
                    self.member_id,
                    f"{interaction.user.mention} picked up your request and will contact you shortly.",
                )


def help_view(member_id: int, claimed: bool = False, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(HelpButton("claim", member_id, disabled or claimed))
    view.add_item(HelpButton("note", member_id, disabled))
    return view


class IssueReportView(discord.ui.View):
    def __init__(self, seen: bool = False):
        super().__init__(timeout=None)
        if seen:
            self.mark_seen.label = "Seen"
            self.mark_seen.disabled = True

    @discord.ui.button(
        label="Mark as Seen",
        emoji=SEEN_EMOJI,
        style=discord.ButtonStyle.secondary,
        custom_id="issue_report_seen",
    )
    async def mark_seen(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0].copy()
        if any(f.name == "Seen by" for f in embed.fields):
            return await interaction.response.send_message("Already marked as seen.", ephemeral=True)
        embed.color = PURPLE
        set_field(embed, "Status", f"{E_CHECK} **Seen**", inline=True)
        set_field(embed, "Seen by", interaction.user.mention, inline=True)
        set_field(embed, "Seen At", discord.utils.format_dt(discord.utils.utcnow(), "R"), inline=True)
        await interaction.response.edit_message(embed=embed, view=IssueReportView(seen=True))


# ====================== MEMBER panel (ONE shared panel for everyone) ======================
def build_shared_embed(guild: discord.Guild) -> discord.Embed:
    """The single panel in #describe-issue. The queue lists everyone who has SENT an issue."""
    queue = sorted(
        ((mid, e) for mid, e in help_panels.items() if e.get("show") and e.get("queued")),
        key=lambda item: item[1].get("queued_at") or item[1]["joined_at"],
    )
    if queue:
        lines = [
            f"**{i}.** <@{mid}> — {e['status']} • {discord.utils.format_dt(e.get('queued_at') or e['joined_at'], 'R')}"
            for i, (mid, e) in enumerate(queue[:15], start=1)
        ]
        if len(queue) > 15:
            lines.append(f"*…and {len(queue) - 15} more*")
        queue_text = "\n".join(lines)
    else:
        queue_text = f"{E_SHIELD_OK} *Nobody is waiting right now.*"

    embed = discord.Embed(
        title=PANEL_TITLE,
        description=(
            "## Moderator Support\n"
            "Need help from a **moderator**? Press **Describe Issue** below and tell us what is wrong — "
            "**you do not need to join a voice channel.** You are added to the queue the moment you send it, "
            "and a moderator will pick it up as soon as one is available.\n"
            "\n"
            "### How it works\n"
            f"{E_PENCIL} **Describe** — press **Describe Issue** and tell us what happened, who is involved and when.\n"
            f"{E_HOURGLASS} **Wait** — you appear in the **Current queue** below, and get a short ping here when a moderator picks it up.\n"
            f"{E_X} **Cancel** — press **Cancel Request** if you no longer need help.\n"
            "\n"
            f"-# {E_SPEAKER} Prefer to talk? You can also wait in the **Waiting for Help** voice channel.\n"
            "\n"
            f"### {E_PEOPLE} Current queue\n"
            f"{queue_text}"
        ),
        color=PURPLE,
    )
    embed.set_footer(
        text="ELITE LEADERS COMMUNITY • Support System",
        icon_url=guild.icon.url if guild.icon else None,
    )
    embed.timestamp = discord.utils.utcnow()
    return embed


async def refresh_shared_panel(guild: discord.Guild):
    global shared_panel
    channel = guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print("⚠️ Couldn't find the #describe-issue channel — check MEMBER_HELP_PANEL_CHANNEL_ID")
        return
    async with _panel_lock:
        embed = build_shared_embed(guild)
        if shared_panel is not None:
            try:
                await shared_panel.edit(content=None, embed=embed, view=panel_view())
                return
            except discord.NotFound:
                shared_panel = None
            except discord.HTTPException as e:
                print(f"⚠️ Failed to update the shared panel: {e}")
                return
        try:
            shared_panel = await send_with_retry(channel, embed=embed, view=panel_view())
            print("✅ Shared support panel sent")
        except Exception as e:
            print(f"❌ Failed to send the shared panel: {e}")


async def ensure_shared_panel(guild: discord.Guild):
    global shared_panel
    channel = guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return
    found = []
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id == bot.user.id and msg.embeds and "Support Request Received" in (msg.embeds[0].title or ""):
                found.append(msg)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ Couldn't read #describe-issue history (needs 'Read Message History'): {e}")
    if found:
        shared_panel = found[0]
        for old in found[1:]:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
    await refresh_shared_panel(guild)


async def ping_member(channel: discord.TextChannel, member: discord.Member, text: str | None = None,
                      delete_after: float = PING_DELETE_AFTER):
    """Short @mention message so the member gets a notification; it deletes itself."""
    try:
        msg = await channel.send(
            f"{member.mention} {text or 'your request is in — check the panel.'}",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
        await asyncio.sleep(delete_after)
        await msg.delete()
    except discord.HTTPException:
        pass


async def set_panel_status(member_id: int, status: str, color: discord.Color | None = None, note: str | None = None):
    entry = help_panels.get(member_id)
    if entry is None:
        return
    entry["status"] = status
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await refresh_shared_panel(guild)


async def close_help_request(
    member_id: int,
    status: str,
    color: discord.Color,
    alert_footer: str | None = None,
    skip_panel: bool = False,
    note: str | None = None,
    lock_alert: bool = True,
    notify: str | None = None,
):
    """Finishes a help request: the member is removed from the queue. If `alert_footer` is given
    the mod alert is updated too. `notify` sends a short ping to members who are not in voice."""
    entry = help_panels.pop(member_id, None)
    if entry is None:
        return

    alert = entry.get("alert")
    if alert_footer and alert is not None:
        try:
            fresh = await alert.channel.fetch_message(alert.id)
            if fresh.embeds and fresh.embeds[0].footer.text not in ALERT_DONE_FOOTERS:
                embed = fresh.embeds[0].copy()
                embed.color = color
                set_field(embed, "Status", ALERT_STATUS.get(alert_footer, alert_footer), inline=True)
                embed.set_footer(text=alert_footer, icon_url=embed.footer.icon_url)
                if lock_alert:
                    view = help_view(member_id, disabled=True)
                else:
                    view = help_view(member_id, claimed=_is_claimed(embed))
                await fresh.edit(embed=embed, view=view)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"⚠️ Failed to update mod alert: {e}")

    if notify and entry.get("origin") == "panel":
        notify_member(member_id, notify)

    guild = bot.get_guild(GUILD_ID)
    if guild:
        await refresh_shared_panel(guild)


# ====================== Recovery (restarts) + leave grace period ======================
def entry_from_alert(msg: discord.Message, config: dict, origin: str = "vc") -> dict:
    """Rebuilds a help_panels entry from an alert message that is still open."""
    embed = msg.embeds[0]
    claimed_by = get_field(embed, "Claimed by")
    issue = get_field(embed, "Issue")
    described = bool(issue) and "hasn't described" not in issue
    if claimed_by:
        status = ps_claimed(claimed_by)
    elif described:
        status = PS_SENT
    else:
        status = PS_WAITING
    return {
        "alert": msg,
        "origin": origin,
        "described": described,
        "queued": described or origin == "panel",
        "claimed": bool(claimed_by),
        "show": config.get("send_member_panel", True),
        "status": status,
        "joined_at": msg.created_at,
        "queued_at": msg.created_at,
    }


async def scan_latest_alerts(channel) -> dict[int, discord.Message]:
    latest: dict[int, discord.Message] = {}
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id != bot.user.id or not msg.embeds:
                continue
            for row in msg.components:
                for c in getattr(row, "children", []):
                    m = re.fullmatch(r"help_(?:claim|resolved|note)_(\d+)", getattr(c, "custom_id", None) or "")
                    if m:
                        latest.setdefault(int(m.group(1)), msg)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ Couldn't read the help alert channel history: {e}")
    return latest


def _alert_is_open(msg: discord.Message) -> bool:
    footer = msg.embeds[0].footer.text if msg.embeds else None
    return footer not in ALERT_DONE_FOOTERS and footer != "Member moved"


async def recover_help_request_for(member: discord.Member) -> bool:
    """Used when a member presses a panel button but the bot has no record of their request
    (for example after a restart). Re-attaches their still-open mod alert. Never creates a new alert."""
    channel = member.guild.get_channel(HELP_ALERT_CHANNEL_ID)
    if channel is None:
        return False
    msg = (await scan_latest_alerts(channel)).get(member.id)
    if msg is None or not _alert_is_open(msg):
        return False
    origin = _alert_origin(msg)
    vc = member.voice.channel if member.voice else None
    in_help = vc is not None and vc.id in HELP_VC_CONFIG
    if origin == "vc" and not in_help:
        return False
    config = HELP_VC_CONFIG[vc.id] if in_help else DEFAULT_HELP_CONFIG
    if member.id not in help_panels:
        help_panels[member.id] = entry_from_alert(msg, config, origin)
        await refresh_shared_panel(member.guild)
    return True


async def recover_help_requests(guild: discord.Guild):
    """Runs once at startup so a restart / redeploy never loses the queue."""
    alert_channel = guild.get_channel(HELP_ALERT_CHANNEL_ID)
    if alert_channel is None:
        return
    latest = await scan_latest_alerts(alert_channel)

    waiting: dict[int, tuple[discord.Member, discord.abc.GuildChannel]] = {}
    for vc_id in HELP_VC_CONFIG:
        vc = guild.get_channel(vc_id)
        if isinstance(vc, (discord.VoiceChannel, discord.StageChannel)):
            for m in vc.members:
                if not m.bot:
                    waiting[m.id] = (m, vc)

    for mid, msg in latest.items():
        if mid in waiting or not _alert_is_open(msg):
            continue
        if _alert_origin(msg) == "panel" and guild.get_member(mid) is not None:
            # Sent from #describe-issue by someone who isn't in voice -> still a live request.
            help_panels[mid] = entry_from_alert(msg, DEFAULT_HELP_CONFIG, "panel")
            print(f"♻️ Recovered panel request for {mid}")
            continue
        try:
            embed = msg.embeds[0].copy()
            embed.color = PURPLE
            set_field(embed, "Status", ALERT_STATUS["Member left"], inline=True)
            embed.set_footer(text="Member left", icon_url=embed.footer.icon_url)
            await msg.edit(embed=embed, view=help_view(mid, disabled=True))
        except discord.HTTPException:
            pass

    for mid, (m, vc) in waiting.items():
        msg = latest.get(mid)
        config = HELP_VC_CONFIG[vc.id]
        if msg is not None and _alert_is_open(msg):
            help_panels[mid] = entry_from_alert(msg, config, _alert_origin(msg))
            print(f"♻️ Recovered help request for {m}")
        elif msg is not None and msg.embeds and msg.embeds[0].footer.text in ("Resolved", "Resolved ✅", "Resolved ☑️"):
            continue
        else:
            print(f"♻️ {m} is waiting with no open alert — sending one")
            await send_help_alert(m, vc, config, ping=False)


async def close_after_grace(member_id: int):
    """Closes a request only if the member did NOT come back within HELP_LEAVE_GRACE seconds."""
    try:
        await asyncio.sleep(HELP_LEAVE_GRACE)
    except asyncio.CancelledError:
        return
    _leave_tasks.pop(member_id, None)
    guild = bot.get_guild(GUILD_ID)
    m = guild.get_member(member_id) if guild else None
    if m and m.voice and m.voice.channel and m.voice.channel.id in HELP_VC_CONFIG:
        return
    await close_help_request(member_id, f"{E_EXIT} Left the queue", PURPLE, alert_footer="Member left")


class DescribeIssueModal(discord.ui.Modal, title="Describe Your Issue"):
    issue = discord.ui.TextInput(
        label="What do you need help with?",
        style=discord.TextStyle.paragraph,
        placeholder="What happened, who is involved, and when? A few lines is enough.",
        required=True,
        max_length=500,
    )

    def __init__(self, member_id: int):
        super().__init__()
        self.member_id = member_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"{E_CHECK} Got it, thanks. You are now in the queue — a moderator will pick up your request shortly. "
            "You will get a ping in this channel when someone is on it.",
            ephemeral=True,
        )

        guild = interaction.guild
        member = guild.get_member(self.member_id) if guild else None
        reports_channel = guild.get_channel(ISSUE_REPORTS_CHANNEL_ID) if guild else None
        text = self.issue.value
        now = discord.utils.utcnow()

        entry = help_panels.get(self.member_id)
        created_here = False

        if entry is None and member is not None:
            # No request yet (the member is NOT in the help voice channel) -> open one from the panel.
            vc = member.voice.channel if member.voice and member.voice.channel and member.voice.channel.id in HELP_VC_CONFIG else None
            config = HELP_VC_CONFIG[vc.id] if vc else DEFAULT_HELP_CONFIG
            if reserve_help_request(member, config, "vc" if vc else "panel"):
                entry = help_panels[self.member_id]
                entry.update(described=True, queued=True, queued_at=now, status=PS_SENT)
                created_here = True
                await _post_help_alert(member, vc, config, ping=False, issue=text)
                entry = help_panels.get(self.member_id)   # None if it was closed in the meantime
            else:
                entry = help_panels.get(self.member_id)

        if entry is not None and not created_here:
            # The alert may still be posting (member just joined the voice channel) -> wait a moment.
            for _ in range(10):
                if entry.get("alert") is not None:
                    break
                await asyncio.sleep(0.5)
            entry["described"] = True
            if not entry.get("queued"):
                entry["queued"] = True
                entry["queued_at"] = now
            alert = entry.get("alert")
            if alert is not None:
                try:
                    fresh = await alert.channel.fetch_message(alert.id)
                    if fresh.embeds and fresh.embeds[0].footer.text not in ALERT_DONE_FOOTERS:
                        embed = fresh.embeds[0].copy()
                        set_field(embed, "Issue", f">>> {text}"[:1024], inline=False)
                        await fresh.edit(embed=embed, view=help_view(self.member_id, claimed=_is_claimed(embed)))
                except discord.HTTPException as e:
                    print(f"⚠️ Couldn't add the issue to the mod alert: {e}")

            if not entry.get("claimed"):
                await set_panel_status(self.member_id, PS_SENT, PURPLE)
            elif guild:
                await refresh_shared_panel(guild)

        # Also post it to the issue reports channel.
        if reports_channel is None:
            print("⚠️ Couldn't find the issue reports channel — check ISSUE_REPORTS_CHANNEL_ID")
        else:
            embed = discord.Embed(
                title=f"{E_PENCIL} Issue Report",
                description=(
                    f"## New issue from <@{self.member_id}>\n"
                    "-# Sent from the support panel\n"
                    "\n"
                    "### Their message\n"
                    f">>> {text}"
                ),
                color=PURPLE,
            )
            if member:
                embed.set_author(name=f"{member.display_name} ({member})", icon_url=member.display_avatar.url)
            else:
                embed.set_author(name=f"Unknown member ({self.member_id})")

            voice = member.voice.channel if member and member.voice and member.voice.channel else None
            alert_msg = entry.get("alert") if entry else None

            embed.add_field(name=f"{E_SMILEY} Member", value=f"<@{self.member_id}>", inline=True)
            embed.add_field(name=f"{E_LINK} User ID", value=f"`{self.member_id}`", inline=True)
            embed.add_field(name=f"{E_HOURGLASS} Submitted", value=discord.utils.format_dt(now, "R"), inline=True)
            embed.add_field(
                name=f"{E_SPEAKER} Voice Channel",
                value=voice.mention if voice else "`Not in a voice channel`",
                inline=True,
            )
            embed.add_field(name="Status", value=f"{E_HOURGLASS} **Not seen yet**", inline=True)
            if alert_msg is not None:
                embed.add_field(name=f"{E_KEY} Help Request", value=f"[Jump to request]({alert_msg.jump_url})", inline=True)

            embed.set_footer(
                text="ELITE LEADERS COMMUNITY • Issue Reports",
                icon_url=guild.icon.url if guild and guild.icon else None,
            )
            embed.timestamp = now
            try:
                await send_with_retry(reports_channel, embed=embed, view=IssueReportView())
                print(f"✅ Issue report sent for {member or self.member_id}")
            except Exception as e:
                print(f"❌ Failed to send issue report: {e}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Describe-issue modal error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)


class MemberPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"member_(?P<action>describe|cancel)(?:_[0-9]+)?",
):
    """Buttons on the ONE shared panel. They act for whoever pressed them."""

    def __init__(self, action: str):
        if action == "describe":
            label, emoji = "Describe Issue", DESCRIBE_EMOJI
        else:
            label, emoji = "Cancel Request", CANCEL_EMOJI
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"member_{action}",
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.action == "describe":
            return True   # anyone can open / update a request - no voice channel needed

        entry = help_panels.get(interaction.user.id)
        if entry is None:
            member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
            if member is not None and await recover_help_request_for(member):
                entry = help_panels.get(interaction.user.id)
        if entry is None:
            await interaction.response.send_message(
                "You don't have an active request. Press **Describe Issue** to open one — no voice channel needed.",
                ephemeral=True,
            )
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        member_id = interaction.user.id
        if self.action == "describe":
            await interaction.response.send_modal(DescribeIssueModal(member_id))
            return

        await interaction.response.send_message(
            "Your request has been cancelled. Press **Describe Issue** any time you need a **moderator**.",
            ephemeral=True,
        )
        await close_help_request(
            member_id,
            f"{E_X} Cancelled",
            PURPLE,
            alert_footer="Cancelled",
        )


def panel_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MemberPanelButton("describe"))
    view.add_item(MemberPanelButton("cancel"))
    return view


# ================================ Events ================================
@bot.event
async def setup_hook():
    bot.add_dynamic_items(VerifyButton, HelpButton, MemberPanelButton)
    bot.add_view(IssueReportView())


async def nickname_watchdog():
    """Every 15 seconds makes sure everyone in a waiting / help / report room has exactly the right emoji."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(GUILD_ID)
            if guild:
                for vc in list(guild.channels):
                    if not isinstance(vc, (discord.VoiceChannel, discord.StageChannel)):
                        continue
                    if vc.id not in ALERT_VC_IDS and not _is_report_room(vc):
                        continue
                    for m in list(vc.members):
                        if m.bot or m.id in _nick_failed:
                            continue
                        want = _alert_emoji_for(m)
                        if want and not _has_exact_prefix(m.nick, want):
                            print(f"🔧 Watchdog: {m} has the wrong/missing emoji (want {want}), fixing")
                            await set_report_vc_alert(m)
        except Exception as e:
            print(f"⚠️ Nickname watchdog error: {e}")
        await asyncio.sleep(15)


@bot.event
async def on_ready():
    global _startup_sync_done
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print(f"❌ Guild {GUILD_ID} not found!")
        return

    await cache_invites(guild)

    if _startup_sync_done:
        return
    _startup_sync_done = True

    watchdog_task = asyncio.create_task(nickname_watchdog())
    _bg_tasks.add(watchdog_task)

    if not guild.me.guild_permissions.manage_nicknames:
        print("⚠️ The bot is missing the 'Manage Nicknames' permission — the ⏳/📛 nickname emoji will NOT work until you give it")

    for vc_id in ALERT_VC_IDS:
        channel = guild.get_channel(vc_id)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            for waiting_member in channel.members:
                if not waiting_member.bot:
                    await set_report_vc_alert(waiting_member)
        elif channel is not None:
            print(f"⚠️ Channel {vc_id} is a {type(channel).__name__}, not a voice channel — skipping it")
        else:
            print(f"⚠️ Can't find voice channel {vc_id}")

    report_category = guild.get_channel(REPORT_CATEGORY_ID)
    if isinstance(report_category, discord.CategoryChannel):
        for vc in list(report_category.voice_channels):
            if not _is_report_room(vc):
                continue
            if vc.members:
                for room_member in vc.members:
                    if not room_member.bot:
                        await set_report_vc_alert(room_member)
            else:
                await delete_if_empty_report_room(vc)

    for m in list(guild.members):
        if m.bot or _in_alert_vc(m):
            continue
        if _strip_alert_prefix(m.nick)[1]:
            await set_report_vc_alert(m)

    await ensure_shared_panel(guild)
    await recover_help_requests(guild)
    await refresh_shared_panel(guild)


@bot.event
async def on_invite_create(invite: discord.Invite):
    if invite.guild and invite.guild.id == GUILD_ID:
        await cache_invites(invite.guild)
        print(f"📝 Invite created: {invite.code}")


@bot.event
async def on_invite_delete(invite: discord.Invite):
    # Single-use invites vanish the moment they're used, so the cache must NOT be overwritten here.
    if invite.guild and invite.guild.id == GUILD_ID:
        print(f"🗑️ Invite deleted: {invite.code}")


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID:
        return

    print(f"➕ {member} joined the server")
    await asyncio.sleep(1)

    try:
        current_invites = await member.guild.invites()
        current_by_code = {invite.code: invite for invite in current_invites}
        old_cache = invite_cache.get(member.guild.id, {})

        inviter = None

        for invite in current_invites:
            old_uses = old_cache.get(invite.code, {}).get("uses", 0)
            if (invite.uses or 0) > old_uses:
                inviter = invite.inviter
                print(f"👤 {member} was invited by {inviter} (invite {invite.code})")
                break

        if inviter is None:
            for code, old_invite in old_cache.items():
                if code in current_by_code:
                    continue
                max_uses = old_invite.get("max_uses")
                if max_uses and old_invite.get("uses", 0) == max_uses - 1:
                    inviter = old_invite.get("inviter")
                    print(f"👤 {member} was invited by {inviter} (invite {code}, used up and deleted)")
                    break

        if inviter:
            member_inviters[member.id] = inviter
        else:
            print(f"❓ {member} joined via unknown invite (possibly vanity URL)")
            member_inviters[member.id] = None

        invite_cache[member.guild.id] = {
            invite.code: {"uses": invite.uses or 0, "max_uses": invite.max_uses, "inviter": invite.inviter}
            for invite in current_invites
        }
    except discord.Forbidden:
        print("⚠️ Bot doesn't have permission to view invites")
    except discord.HTTPException as e:
        print(f"⚠️ Failed to read invites: {e}")


@bot.event
async def on_member_remove(member: discord.Member):
    """If the member leaves the server while their verification card is still open, close the card."""
    if member.guild.id != GUILD_ID or member.bot:
        return

    entry = verify_alerts.get(member.id)
    msg = entry["message"] if entry else None
    if msg is None:
        channel = member.guild.get_channel(VERIFICATION_CHANNEL_ID)
        if channel is not None:
            msg = await find_latest_verification(channel, member.id)
    if msg is None:
        return

    try:
        fresh = await msg.channel.fetch_message(msg.id)
        if fresh.embeds and fresh.embeds[0].footer.text == VERIFY_OPEN_FOOTER:
            embed = fresh.embeds[0].copy()
            embed.color = PURPLE
            set_field(embed, "Status", STATUS_LEFT, inline=True)
            embed.set_footer(text="Member left ❌")
            await fresh.edit(embed=embed, view=verify_view(member.id, disabled=True))
            print(f"🚪 {member} left the server — closed their verification card")
    except discord.HTTPException as e:
        print(f"⚠️ Couldn't close the verification card for {member}: {e}")

    member_inviters.pop(member.id, None)


async def find_latest_verification(channel: discord.TextChannel, member_id: int) -> discord.Message | None:
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id != bot.user.id or not msg.embeds:
                continue
            ids = [
                getattr(c, "custom_id", None) or ""
                for row in msg.components for c in getattr(row, "children", [])
            ]
            if any(cid.startswith("verify_") and cid.endswith(f"_{member_id}") for cid in ids):
                return msg
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ Couldn't read the verification channel history: {e}")
    return None


async def send_verification_alert(member: discord.Member, voice_channel: discord.VoiceChannel):
    lock = _verify_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        await _send_verification_alert(member, voice_channel)


async def _send_verification_alert(member: discord.Member, voice_channel: discord.VoiceChannel):
    unverified_role = member.guild.get_role(UNVERIFIED_ROLE_ID)
    if unverified_role is None or unverified_role not in member.roles:
        print(f"⏭️ Skipped {member}: no Unverified role")
        return

    verification_channel = member.guild.get_channel(VERIFICATION_CHANNEL_ID)
    if verification_channel is None:
        print("⚠️ Couldn't find the verification channel — check VERIFICATION_CHANNEL_ID")
        return

    existing = None
    entry = verify_alerts.get(member.id)
    if entry is not None:
        try:
            existing = await verification_channel.fetch_message(entry["message"].id)
        except discord.NotFound:
            existing = None
            verify_alerts.pop(member.id, None)
            entry = None
        except discord.HTTPException:
            existing = entry["message"]
    if existing is None:
        existing = await find_latest_verification(verification_channel, member.id)
        if existing is not None:
            entry = verify_alerts[member.id] = {
                "message": existing, "rejoins": 0, "last_edit": 0.0, "last_ping": existing.created_at,
            }

    previous_reason = None
    if existing is not None and existing.embeds:
        footer_text = existing.embeds[0].footer.text

        # ---- request still open: spam control (counter on the same card + a reminder at most every 5 min) ----
        if footer_text == VERIFY_OPEN_FOOTER:
            entry["rejoins"] = entry.get("rejoins", 0) + 1
            print(f"⏭️ {member} re-joined, verification request already open (x{entry['rejoins']})")
            if time.monotonic() - entry.get("last_edit", 0.0) >= 5:
                try:
                    embed = existing.embeds[0].copy()
                    set_field(
                        embed, f"{E_HOURGLASS} Re-joined the voice channel",
                        f"`{entry['rejoins']}` time(s) since this request", inline=False,
                    )
                    await existing.edit(embed=embed)
                    entry["last_edit"] = time.monotonic()
                except discord.HTTPException:
                    pass

            last_ping = entry.get("last_ping") or existing.created_at
            since_ping = (discord.utils.utcnow() - last_ping).total_seconds()
            if since_ping >= REJOIN_PING_COOLDOWN:
                try:
                    await send_with_retry(
                        verification_channel,
                        content=(
                            f"@everyone {member.mention} is **still waiting** to be verified "
                            f"(joined the voice channel `{entry['rejoins'] + 1}` times). {existing.jump_url}"
                        ),
                        reference=existing.to_reference(fail_if_not_exists=False),
                        allowed_mentions=discord.AllowedMentions(everyone=True, users=False, replied_user=False),
                        delete_after=REMINDER_DELETE_AFTER,
                    )
                    entry["last_ping"] = discord.utils.utcnow()
                    print(f"🔔 Reminder ping sent for {member}")
                except Exception as e:
                    print(f"⚠️ Couldn't send the reminder ping: {e}")
            else:
                print(f"🔕 Reminder for {member} skipped — last ping was {int(since_ping)}s ago")
            return

        # ---- request already handled: wait out the cooldown (unless they left the server and came back) ----
        age = (discord.utils.utcnow() - existing.created_at).total_seconds()
        if footer_text != "Member left ❌" and age < VERIFY_REALERT_COOLDOWN:
            print(f"⏭️ {member} re-joined too soon after the last request was handled ({int(age)}s) — skipped")
            return

        if footer_text == "Rejected ❌":
            previous_reason = get_field(existing.embeds[0], "Reason")

    print(f"📤 Sending verification message for {member}")

    inviter = member_inviters.get(member.id)
    if inviter:
        invited_by_text = f"{inviter.mention}\n`{inviter.name}`"
    else:
        invited_by_text = "`Unknown`\n-# vanity URL or unknown invite"

    warning_text = ""
    if NEW_ACCOUNT_WARNING_DAYS:
        account_age = discord.utils.utcnow() - member.created_at
        if account_age < timedelta(days=NEW_ACCOUNT_WARNING_DAYS):
            if account_age.days >= 1:
                age_label = f"{account_age.days} day{'s' if account_age.days != 1 else ''} old"
            else:
                hours = max(account_age.seconds // 3600, 0)
                age_label = "less than an hour old" if hours < 1 else f"{hours} hour{'s' if hours != 1 else ''} old"
            warning_text = (
                f"{WARN_EMOJI} **New account** — only **{age_label}**\n"
                f"-# Check them carefully before verifying.\n\n"
            )

    embed = discord.Embed(
        title=f"{E_KEY} Verification Request",
        description=(
            f"{warning_text}"
            f"## {member.mention}\n"
            f"is waiting in {voice_channel.mention} to be verified.\n"
            f"-# Join them, check, then press a button below."
        ),
        color=PURPLE,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name=f"{E_PEOPLE} Member", value=member.mention, inline=True)
    embed.add_field(name=f"{E_LINK} ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Status", value=STATUS_WAITING, inline=True)
    embed.add_field(name=f"{E_HOURGLASS} Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(
        name=f"{E_EXIT} Joined",
        value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "`Unknown`",
        inline=True,
    )
    embed.add_field(name=f"{E_CROWN} Invited By", value=invited_by_text, inline=True)
    if previous_reason:
        embed.add_field(name=f"{E_SHIELD_X} Previous Rejection", value=previous_reason[:1024], inline=False)
    embed.set_footer(text=VERIFY_OPEN_FOOTER)
    embed.timestamp = discord.utils.utcnow()

    try:
        sent = await send_with_retry(
            verification_channel,
            content="@everyone A member in the voice channel needs verification",
            embed=embed,
            view=verify_view(member.id),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        verify_alerts[member.id] = {
            "message": sent, "rejoins": 0, "last_edit": 0.0, "last_ping": discord.utils.utcnow(),
        }
        print(f"✅ Verification message sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send verification message: {e}")


def reserve_help_request(member: discord.Member, config: dict, origin: str = "vc") -> bool:
    """Creates the member's request entry right away (before any await), so a second event can never
    start a second request. Returns False if they already have one.
    The member only shows up in the panel queue once `queued` is True (= they sent their issue)."""
    if member.id in help_panels:
        return False
    now = discord.utils.utcnow()
    help_panels[member.id] = {
        "alert": None,
        "origin": origin,
        "described": False,
        "queued": False,
        "claimed": False,
        "show": config.get("send_member_panel", True),
        "status": PS_WAITING,
        "joined_at": now,
        "queued_at": None,
    }
    return True


async def send_help_alert(member: discord.Member, voice_channel: discord.VoiceChannel, config: dict, ping: bool = True):
    """Member joined the help voice channel: opens the mod alert. They are NOT listed in the queue
    until they press Describe Issue and send their message."""
    if not reserve_help_request(member, config, "vc"):
        print(f"⏭️ {member} already has an active help request, skipping")
        return
    await _post_help_alert(member, voice_channel, config, ping)


async def _post_help_alert(member: discord.Member, voice_channel, config: dict, ping: bool = True, issue: str | None = None):
    """Posts the mod alert. `voice_channel` is None for requests sent from the panel by members
    who are not in a voice channel."""
    help_channel = member.guild.get_channel(HELP_ALERT_CHANNEL_ID)
    if help_channel is None:
        print("⚠️ Couldn't find the help alert channel — check HELP_ALERT_CHANNEL_ID")
        help_panels.pop(member.id, None)
        return

    emoji = config.get("emoji", "")
    print(f"📤 Sending help alert for {member} ({'voice' if voice_channel else 'panel'})")

    if voice_channel is not None:
        where = f"is waiting in {voice_channel.mention}\n{E_PEOPLE} *Join them to help.*"
    else:
        where = (
            "sent this from the support panel\n"
            f"{E_WARN} *They are **not** in a voice channel — contact them directly.*"
        )

    embed = discord.Embed(
        title=f"{emoji or E_CROWN} Help Request".strip(),
        description=f"## {member.mention}\n{where}",
        color=PURPLE,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name=f"{E_SMILEY} Member", value=member.mention, inline=True)
    embed.add_field(name=f"{E_LINK} ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Status", value=STATUS_WAITING, inline=True)
    embed.add_field(name=f"{E_HOURGLASS} Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(
        name="Voice Channel",
        value=voice_channel.mention if voice_channel else "`Not in a voice channel`",
        inline=True,
    )
    embed.add_field(name=f"{E_HOURGLASS} Waiting Since", value=discord.utils.format_dt(discord.utils.utcnow(), "R"), inline=True)
    if issue or config.get("send_member_panel", True):
        embed.add_field(
            name="Issue",
            value=f">>> {issue}"[:1024] if issue else "*The member hasn't described the issue yet.*",
            inline=False,
        )
    embed.set_footer(
        text="ELITE LEADERS COMMUNITY • Press Claim first, then Resolve when it's handled",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )
    embed.timestamp = discord.utils.utcnow()

    alert_message = None
    try:
        alert_message = await send_with_retry(
            help_channel,
            content="@everyone A member needs a moderator",
            embed=embed,
            view=help_view(member.id),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        print(f"✅ Help alert sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send help alert: {e}")

    entry = help_panels.get(member.id)
    if entry is None:
        # The request was closed (member left / cancelled) while the alert was being sent.
        if alert_message is not None:
            try:
                left = alert_message.embeds[0].copy()
                set_field(left, "Status", ALERT_STATUS["Member left"], inline=True)
                left.set_footer(text="Member left", icon_url=left.footer.icon_url)
                await alert_message.edit(embed=left, view=help_view(member.id, disabled=True))
            except discord.HTTPException:
                pass
        return
    entry["alert"] = alert_message

    if entry.get("show") and entry.get("queued"):
        await refresh_shared_panel(member.guild)

    # The short self-deleting ping when the member joins the help voice channel (kept as before).
    if PING_ON_JOIN and ping and entry.get("show"):
        ping_channel = member.guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
        if isinstance(ping_channel, discord.TextChannel):
            task = asyncio.create_task(ping_member(ping_channel, member, JOIN_PING_TEXT))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """
    - Waiting for Move VC: ⏳ on the nickname + verification alert.
    - Help VC: ⏳ on the nickname + mod alert (the member joins the queue when they send their issue).
    - Report VCs: 📛 on the nickname + a private report room.
    - Leaving restores the nickname. Requests that were sent from the panel (member not in voice)
      are NOT closed by voice events.
    """
    if member.guild.id != GUILD_ID or member.bot:
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    if before_id == after_id:
        return

    _nick_failed.discard(member.id)

    entry0 = help_panels.get(member.id)
    if entry0 is not None and entry0.get("origin", "vc") == "vc" and before_id in HELP_VC_CONFIG:
        if after_id is None:
            if member.id not in _leave_tasks:
                _leave_tasks[member.id] = asyncio.create_task(close_after_grace(member.id))
                entry0["status_before"] = entry0["status"]
                await set_panel_status(member.id, PS_DISCONNECTED)
        elif after_id != before_id:
            await close_help_request(
                member.id,
                f"{E_SHIELD_OK} Resolved — a moderator moved you",
                PURPLE,
                alert_footer="Member moved",
                lock_alert=False,
            )

    nick_task = None
    if (
        before_id in ALERT_VC_IDS
        or after_id in ALERT_VC_IDS
        or _is_report_room(before.channel)
        or _is_report_room(after.channel)
    ):
        nick_task = asyncio.create_task(set_report_vc_alert(member))

    if before.channel is not None and _is_report_room(before.channel):
        asyncio.create_task(delete_if_empty_report_room(before.channel))

    try:
        if after_id in REPORT_VC_IDS:
            await create_report_room(member)

        elif after_id == WAITING_VC_ID:
            if isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)):
                await send_verification_alert(member, after.channel)

        elif after_id in HELP_VC_CONFIG:
            pending = _leave_tasks.pop(member.id, None)
            if pending is not None:
                pending.cancel()
                entry = help_panels.get(member.id)
                if entry is not None:
                    await set_panel_status(member.id, entry.pop("status_before", PS_WAITING))
            if isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)):
                await send_help_alert(
                    member,
                    after.channel,
                    HELP_VC_CONFIG[after_id],
                )
    finally:
        if nick_task is not None:
            await nick_task


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
