"""
Discord Verification Bot
=======================================
How it works:
1) When a member joins the "Waiting for Move" voice channel:
   - The bot prefixes that member's own server nickname with ⏳ while they're waiting,
     and removes it again the moment they leave.
   - The bot posts an embed in the "VERIFICATION" text channel, pinging @everyone,
     with a Verify button and a Reject button. Anyone can click the buttons.
   - Shows who invited the member if available.
2) When a member joins one of the "Waiting for Help" voice channels:
   - The member's own server nickname gets the ⏳ prefix while they wait there
     (and it's removed when they leave).
   - MOD PANEL: the bot posts an alert pinging @everyone with 2 buttons:
         Claim                 -> the member instantly sees WHO is handling them
         Resolve               -> closes the request (popup for an optional note about the
                                  problem); the member panel updates by itself
     The voice channel name is a clickable link, so a mod can jump straight in.
   - MEMBER PANEL: there is ONE single panel in the #describe-issue text channel, shared by
     everyone. It lists every member currently waiting (with their @mention and a live status:
         🟡 Waiting for a moderator  ->  🟣 <mod> is handling your request).
     When a member joins the help channel they are added to that same panel (and get a short
     ping that deletes itself); when they leave, cancel or get resolved they disappear from it.
     The panel's "Describe Issue" and "Cancel Request" buttons work for whoever pressed them.
   - When a member presses "Describe Issue", their text is added to the mod alert
     (so mods see everything in ONE place) and also posted to ISSUE_REPORTS_CHANNEL_ID.
   - If the member leaves or cancels, the mod alert closes by itself.
     If the member is MOVED to another channel, the alert stays OPEN (status "Member moved")
     so a mod can still press Claim / Resolve and leave a note.
   - Each waiting-for-help channel has its own emoji shown in the alert title, and can
     independently turn the member-facing panel on or off (see HELP_VC_CONFIG below).
3) When a member joins one of the "REPORT" voice channels:
   - No message is sent anywhere. Instead the bot prefixes that member's own server
     nickname with 📛 while they're waiting in the channel, and removes
     it again the moment they leave (see REPORT_VC_IDS below).
4) When anyone clicks Verify:
   - The "Verified" role and "Member" role (or any other roles you set) get added.
   - If the member already has the "Unverified" role, it gets removed.
   - An optional welcome message is sent in the welcome channel (if you set one up).

All buttons are PERSISTENT: they keep working after the bot restarts or redeploys
(they are re-registered on startup using dynamic items), so you will no longer get
"ELITE SYSTEM didn't respond in time" on older messages.

Requirements:
    pip install "discord.py>=2.4"

Before running:
    - Enable SERVER MEMBERS INTENT for your bot in the Discord Developer Portal.
    - Give the bot the "Manage Nicknames" permission, and put the bot's role ABOVE the
      roles of the members it needs to rename (Discord never lets a bot rename the
      server owner, or anyone whose top role is higher than the bot's).
    - Give the bot View Channel, Send Messages, Embed Links, Mention Everyone,
      Read Message History and Manage Messages in the #describe-issue channel, and let
      members see that channel.
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
# The token is read from an environment variable instead of being written here,
# so it's safe to upload this file. Set DISCORD_TOKEN in Railway's "Variables" tab.
TOKEN = os.environ.get("DISCORD_TOKEN")

GUILD_ID = 1410440666747633707  # ELT server ID

# Channels
VERIFICATION_CHANNEL_ID = 1542531178526146670  # channel where verify requests get posted
WELCOME_CHANNEL_ID = None                        # welcome channel (optional - leave as None if you don't want a welcome message)
WAITING_VC_ID = 1513904254535073883              # the "Waiting for Move" voice channel

HELP_ALERT_CHANNEL_ID = 1551163762084683866  # channel where the mod alert gets posted (the Waiting for Help voice channel's own chat)
# The #describe-issue text channel: the member panel (with the @mention and the buttons) is posted HERE.
# To get the ID: right-click #describe-issue -> Copy Channel ID, and paste it below.
MEMBER_HELP_PANEL_CHANNEL_ID = 1553786062579699722
ISSUE_REPORTS_CHANNEL_ID = 1553196616146493460  # channel where "Describe Issue" submissions get posted

# Per-VC settings for the "waiting for help" flow (mod alert + optional member panel):
#   - emoji: shown in the mod alert title, so you can tell at a glance which
#     channel triggered it
#   - send_member_panel: whether the member-facing panel (Describe Issue / Cancel)
#     gets sent for this channel (False = mod alert only, no member panel)
HELP_VC_CONFIG = {
    1517941411151085691: {"emoji": "", "send_member_panel": True},
}

# Nickname emojis. While a member is sitting in one of these voice channels, the bot
# prefixes THEIR OWN server nickname with the channel's emoji. As soon as they leave,
# their nickname is restored.
#   - Waiting for Move + every Waiting for Help channel -> WAITING_VC_EMOJI (⏳)
#   - REPORT channels                                   -> REPORT_VC_EMOJI  (📛)
WAITING_VC_EMOJI = "⏳"
REPORT_VC_EMOJI = "📛"
# Emoji the bot used in the past. They are still recognised (and stripped) so nicknames
# that still carry an old emoji get cleaned up instead of ending up with two emoji.
LEGACY_ALERT_EMOJIS = {"⛔"}
REPORT_VC_IDS = {
    1517940974125318166,
    1554981588880850964,  # new REPORT channel (members joining it get their own room + the 📛 emoji)
}
REPORT_CATEGORY_ID = 1517941029221695760
REPORT_CHANNEL_NAME = "📛┃𝗥𝗘𝗣𝗢𝗥𝗧"  # name of the voice channel created for each report
# Old names of the report rooms, so rooms created before the emoji change are still recognised.
LEGACY_REPORT_CHANNEL_NAMES = {"⛔┃𝗥𝗘𝗣𝗢𝗥𝗧"}

# {voice channel id: emoji to put on the member's nickname}
ALERT_VC_EMOJIS = {
    WAITING_VC_ID: WAITING_VC_EMOJI,
    **{vc_id: WAITING_VC_EMOJI for vc_id in HELP_VC_CONFIG},
    **{vc_id: REPORT_VC_EMOJI for vc_id in REPORT_VC_IDS},
}
ALERT_VC_IDS = set(ALERT_VC_EMOJIS)
ALERT_EMOJIS = set(ALERT_VC_EMOJIS.values())
# Every emoji we might find at the start of a nickname (current + old), longest first.
_STRIPPABLE_EMOJIS = sorted(ALERT_EMOJIS | LEGACY_ALERT_EMOJIS, key=len, reverse=True)

# Roles
UNVERIFIED_ROLE_ID = 1513904174079934657  # removed from the member at verify time if they have it (not given automatically anymore)
VERIFIED_ROLE_ID = 1513904156350353511    # given after verification

EXTRA_ROLES_ON_VERIFY = [1513904151309058159]  # MEMBER role - given alongside Verified

# New-account warning: if the member's Discord account is younger than this many days,
# the verification message shows a warning line (with the emoji below) so staff
# think twice before verifying. Set NEW_ACCOUNT_WARNING_DAYS = 0 to turn it off.
NEW_ACCOUNT_WARNING_DAYS = 7
WARN_EMOJI = "<:warn_purple:1554671365490212945>"  # if it's an animated emoji, change "<:" to "<a:"
# Anti-spam: members who keep leaving and re-joining the voice channels.
#   - VERIFY_REALERT_COOLDOWN: while a member's verification request is still open, re-joining
#     NEVER posts a new one (the open one just gets a "Rejoined" counter). After it was
#     handled (e.g. Rejected), a new request for the same member is only allowed after this
#     many seconds.
#   - REPORT_ROOM_COOLDOWN: minimum seconds between creating report rooms for the same member.
#     A member who already has a report room is moved back into it instead of getting another.
VERIFY_REALERT_COOLDOWN = 5 * 60
#   - HELP_LEAVE_GRACE: when a member leaves the help channel, their request stays open for this
#     many seconds. If they come back in that time (bad connection, accidental click, spam
#     leave/join) nothing is closed and NO new @everyone alert is sent.
HELP_LEAVE_GRACE = 20
REPORT_ROOM_COOLDOWN = 10
# ================================================================

intents = discord.Intents.default()
intents.members = True      # needed for member lookups / nicknames (enable in the Developer Portal)
intents.voice_states = True  # needed to detect members joining the voice channel
intents.invites = True      # needed to track invites
# NOTE: message_content is NOT needed (the bot reads no messages), and asking for it
# without enabling it in the portal would crash the bot on startup.

bot = commands.Bot(command_prefix="!", intents=intents)

# Invite tracking
# invite_cache[guild_id][code] = {"uses": int, "max_uses": int, "inviter": discord.User}
invite_cache = {}
member_inviters = {}  # {member_id: inviter_user_object}

# report_vc_base_nicknames[member_id] = that member's nickname (or None, meaning "no
# nickname set") with the alert emoji stripped off, so we can restore it exactly once
# they leave the alert voice channels.
report_vc_base_nicknames = {}

# One lock per member so a quick join -> leave can't make the "add emoji" and
# "remove emoji" edits run at the same time and leave the emoji stuck on the name.
_nick_locks: dict[int, asyncio.Lock] = {}

# Members the bot is NOT allowed to rename (server owner / higher role). The watchdog skips
# them so it doesn't hit the API every few seconds; cleared when they change voice channel.
_nick_failed: set[int] = set()

# help_panels[member_id] = {
#     "alert": the mod alert message,
#     "described": bool, "claimed": bool,
#     "show": listed on the shared panel?, "status": text shown next to them, "joined_at": datetime,
# }
# An entry here means "this member has an ACTIVE request": it is what the shared panel lists
# and what stops a second alert from ever being sent for the same member.
help_panels = {}

# Anti-spam state
verify_alerts: dict[int, dict] = {}       # member_id -> {"message": msg, "rejoins": int, "last_edit": float}
_verify_locks: dict[int, asyncio.Lock] = {}
member_report_rooms: dict[int, int] = {}  # member_id -> id of the report room made for them
_report_locks: dict[int, asyncio.Lock] = {}
_report_last_created: dict[int, float] = {}
_leave_tasks: dict[int, asyncio.Task] = {}   # member_id -> pending 'close after grace' task
VERIFY_OPEN_FOOTER = "Click Verify to let this member in"

# The ONE shared member panel in #describe-issue (a single message for everyone).
shared_panel: discord.Message | None = None
_panel_lock = asyncio.Lock()

_startup_sync_done = False  # on_ready can fire many times (reconnects) - only sync nicknames once

# Footers that mean "this mod alert is finished" (buttons are disabled).
# NOTE: "Member moved" is deliberately NOT in this list - the alert stays open so a
# mod can still press Claim / Resolve.
ALERT_DONE_FOOTERS = ("Resolved", "Member left", "Cancelled", "Resolved ✅", "Resolved ☑️", "Member left ❌", "Cancelled ⚪")
# What the "Status" field of the mod alert says in each final state.
ALERT_STATUS = {
    "Resolved": "🟢 `Resolved`",
    "Member left": "🔴 `Member left`",
    "Cancelled": "⚪ `Cancelled`",
    "Member moved": "🔵 `Member moved`",
}

# When a member joins the help channel, the bot sends a short message that @mentions them
# (so they get a notification) and deletes it again after PING_DELETE_AFTER seconds.
# Edits to the shared panel never notify anyone, which is why this tiny ping exists.
# Set PING_ON_JOIN = False if you only want the mention inside the panel.
PING_ON_JOIN = True
PING_DELETE_AFTER = 5
# Custom emoji (paste the IDs of other emoji here if you ever change them).
# If one of them is an ANIMATED emoji, change "<:" to "<a:" / animated=False to animated=True.
SUPPORT_TITLE_EMOJI = "<:support:1555003977173573684>"   # shown in front of the panel title
CANCEL_EMOJI = discord.PartialEmoji(name="cancel", id=1554671367142645770, animated=False)
PANEL_TITLE = f"{SUPPORT_TITLE_EMOJI} Support Request Received"
PURPLE = discord.Color.purple()  # the colour of EVERY embed - change it here to recolour all panels at once
_bg_tasks: set = set()


async def cache_invites(guild: discord.Guild):
    """Cache all invites for a guild - their use counts, use limits, and inviters."""
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
    """Send a message with exponential backoff retry logic for rate limits."""
    max_attempts = 5
    backoff_delays = [1, 2, 4, 8, 16]  # seconds

    for attempt in range(max_attempts):
        try:
            return await channel.send(**kwargs)
        except discord.HTTPException as e:
            if e.status == 429 and attempt < max_attempts - 1:  # Rate limited
                delay = backoff_delays[attempt]
                print(f"⏳ Rate limited, retrying in {delay}s (attempt {attempt + 1}/{max_attempts})")
                await asyncio.sleep(delay)
            else:
                raise


def set_field(embed: discord.Embed, name: str, value: str, inline: bool = False):
    """Update a field by name if it exists, otherwise add it (never creates duplicates)."""
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
report_room_ids: set[int] = set()  # rooms created by the bot during this run (tracked by ID)


def _is_report_room(channel) -> bool:
    """True for the voice channels the bot creates for each report (not the trigger channel).
    Rooms made since the bot started are recognised by ID; rooms left over from before a
    restart are recognised by category + name."""
    if channel is None or channel.id in REPORT_VC_IDS:
        return False
    if channel.id in report_room_ids:
        return True
    return (
        getattr(channel, "category_id", None) == REPORT_CATEGORY_ID
        and (channel.name == REPORT_CHANNEL_NAME or channel.name in LEGACY_REPORT_CHANNEL_NAMES)
    )


def _alert_emoji_for(member: discord.Member) -> str | None:
    """The emoji this member's nickname should carry RIGHT NOW (None = no emoji)."""
    channel = member.voice.channel if member.voice else None
    if channel is None:
        return None
    if _is_report_room(channel):
        return REPORT_VC_EMOJI
    return ALERT_VC_EMOJIS.get(channel.id)


async def create_report_room(member: discord.Member):
    """Gives the member a report room and moves them into it - WITHOUT spam.
    - One at a time per member (a lock), so a burst of join events can't create many rooms.
    - A member who already has a report room is moved back into it instead of getting a new one.
    - A short cooldown between creations for the same member."""
    guild = member.guild
    lock = _report_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        def _in_trigger() -> bool:
            m = guild.get_member(member.id)
            return bool(m and m.voice and m.voice.channel and m.voice.channel.id in REPORT_VC_IDS)

        # Queued duplicate events: by now the member is already in their room (or left) -> nothing to do.
        if not _in_trigger():
            return

        room = None
        created_new = False
        existing_id = member_report_rooms.get(member.id)
        existing = guild.get_channel(existing_id) if existing_id else None
        if isinstance(existing, discord.VoiceChannel) and _is_report_room(existing):
            room = existing  # reuse their room
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
            # Put the 📛 on right now - don't wait for the voice event (it can arrive late).
            await asyncio.sleep(0.5)
            await set_report_vc_alert(member)
        except discord.Forbidden:
            print("⚠️ Can't move the member — give the bot 'Move Members'")
            if created_new:
                await room.delete(reason="Couldn't move the member in")
        except discord.HTTPException as e:
            # Most likely the member already left the trigger channel.
            print(f"⚠️ Couldn't move {member} into the report room: {e}")
            if created_new:
                try:
                    await room.delete(reason="Member was no longer in voice")
                except discord.HTTPException:
                    pass


async def delete_if_empty_report_room(channel):
    """Deletes an auto-created report room once no real person is left in it."""
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
    """True if the member is CURRENTLY in a waiting / help / report voice channel."""
    return _alert_emoji_for(member) is not None


def _strip_alert_prefix(nick: str | None) -> tuple[str | None, bool]:
    """Remove ALL of our emoji prefixes (current and old ones) from the start of a nickname,
    however many are stacked there. Returns (clean_nick, had_prefix).
    Stripping every one is what stops a member ending up with two emoji."""
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


# Any emoji / symbol a member might already have at the START of their nickname
# (e.g. "♾️ ELT KIRA", "★ Name", "🔥 Name"). When the bot adds its own emoji it REMOVES
# these first, so the member ends up with ONE emoji, not two. Their original nickname is
# remembered and restored when they leave.
_EMOJI_CHARS = (
    "\U0001F000-\U0001FAFF"   # emoji, pictographs, flags, skin tones
    "\U000E0020-\U000E007F"   # tag characters (used in some flags)
    "\u00A9\u00AE\u203C\u2049\u2122\u2139"
    "\u2194-\u21AA\u221E"      # a few arrows + the infinity sign
    "\u231A-\u23FF"            # watches, hourglass ⏳, media controls
    "\u24C2\u25AA-\u25FE"
    "\u2600-\u27BF"            # misc symbols + dingbats (♾ ☑ ✅ ⛔ ★ ✦ ...)
    "\u2934\u2935\u2B00-\u2BFF\u3030\u303D\u3297\u3299"
    "\u200D\uFE0F\u20E3"       # joiner, variation selector, keycap
)
_LEADING_EMOJI_RE = re.compile(f"^[{_EMOJI_CHARS}\\s]+")


def _strip_leading_emoji(text: str | None) -> str:
    """Remove every emoji / symbol (and the spaces around them) from the START of a name."""
    if not text:
        return ""
    return _LEADING_EMOJI_RE.sub("", text).strip()


def _has_exact_prefix(nick: str | None, emoji: str) -> bool:
    """True only if the nickname is exactly `emoji` + a name that does NOT itself start with
    another emoji/symbol (so "📛 ♾️ Name" counts as wrong and gets fixed to "📛 Name")."""
    if not nick or not nick.startswith(f"{emoji} "):
        return False
    rest = nick[len(emoji) + 1:]
    return bool(rest) and not _LEADING_EMOJI_RE.match(rest)


def get_report_vc_base_nickname(member: discord.Member) -> str | None:
    """Return the nickname that should be restored after leaving an alert VC."""
    if member.id not in report_vc_base_nicknames:
        nick, had_prefix = _strip_alert_prefix(member.nick)

        # If the bot already added the emoji before a restart, only our prefix was removed.
        # If what remains is the username / display name, this was probably the bot's
        # fallback for a member who originally had no server nickname.
        if had_prefix and (not nick or nick in (member.name, member.global_name)):
            nick = None

        report_vc_base_nicknames[member.id] = nick

    return report_vc_base_nicknames[member.id]


async def set_report_vc_alert(member: discord.Member):
    """Make the member's nickname match where they are RIGHT NOW.

    Waiting for Move / Waiting for Help voice channel -> nickname gets the ⏳ prefix.
    REPORT voice channel                              -> nickname gets the 📛 prefix.
    Anywhere else                                     -> the original nickname is restored.

    The desired state is read from the member's live voice state (not from whichever
    event called us), and edits are serialized per member, so rapid join/leave/move
    sequences always end in the correct state. Any emoji the member already has at the
    start of their nickname (the bot's own, an old one, or their personal one like ♾️) is
    removed while they wait, so there is only ever ONE emoji. Their original nickname is
    restored when they leave.
    """
    lock = _nick_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        fresh = member.guild.get_member(member.id) or member
        emoji = _alert_emoji_for(fresh)
        active = emoji is not None

        if active:
            base_nick = get_report_vc_base_nickname(fresh)
            # Use the member's own name WITHOUT any emoji/symbol they already have at the
            # start of it (e.g. "♾️ ELT KIRA" -> "ELT KIRA"), so the bot's emoji REPLACES
            # theirs instead of being stacked in front of it. Their original nickname stays
            # saved in report_vc_base_nicknames and comes back when they leave.
            visible_name = ""
            for candidate in (base_nick, fresh.global_name, fresh.name):
                visible_name = _strip_leading_emoji(candidate)
                if visible_name:
                    break
            visible_name = visible_name or fresh.name
            # Discord server nicknames are limited to 32 characters.
            target_nick = f"{emoji} {visible_name}"[:32]
        else:
            _, had_prefix = _strip_alert_prefix(fresh.nick)
            if had_prefix:
                target_nick = get_report_vc_base_nickname(fresh)
            else:
                # No emoji on the name (or they changed their nickname themselves) -
                # nothing to restore; just forget what we remembered.
                report_vc_base_nicknames.pop(fresh.id, None)
                return

        if fresh.nick == target_nick:
            if not active:
                report_vc_base_nicknames.pop(fresh.id, None)
            return  # already in the right state

        try:
            await fresh.edit(nick=target_nick, reason="Waiting/help/report VC occupancy changed")
            print(f"✏️ {fresh} nickname -> {target_nick!r}")
            if not active:
                # Done with this member for now — stop tracking so a later, different
                # nickname they set themselves isn't mistaken for one we set.
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


# ====================== Persistent buttons (survive restarts) ======================
# Each button is a DynamicItem: the member ID lives inside the custom_id, and the bot
# rebuilds the button from that id when someone clicks it — even on messages sent
# before the bot restarted. The custom_id formats are the same as the old ones, so
# messages that were already posted start working again too.

def _already_done(message: discord.Message, done_footers: tuple[str, ...]) -> bool:
    return bool(message.embeds and message.embeds[0].footer.text in done_footers)


class VerifyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"verify_(?P<action>accept|reject)_(?P<uid>[0-9]+)",
):
    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "accept":
            label, style = "☑️ Verify", discord.ButtonStyle.secondary
        else:
            label, style = "❌ Reject", discord.ButtonStyle.secondary
        super().__init__(
            discord.ui.Button(
                label=label,
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
        # Defer first to prevent timeout
        await interaction.response.defer()

        if _already_done(interaction.message, ("Verified ☑️", "Verified ✅", "Rejected ❌")):
            return await interaction.followup.send("This request was already handled.", ephemeral=True)

        embed = interaction.message.embeds[0].copy()

        if self.action == "accept":
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
            embed.add_field(name="Verified", value=f"☑️ {interaction.user.mention}", inline=False)
            embed.set_footer(text="Verified ☑️")
            member_inviters.pop(self.member_id, None)
            await interaction.edit_original_response(embed=embed, view=verify_view(self.member_id, disabled=True))

            if WELCOME_CHANNEL_ID:
                welcome_channel = guild.get_channel(WELCOME_CHANNEL_ID)
                if welcome_channel:
                    try:
                        await welcome_channel.send(
                            f"🎉 Welcome {member.mention}, you're verified — glad to have you in the server!"
                        )
                    except discord.HTTPException as e:
                        print(f"⚠️ Couldn't send welcome message: {e}")
        else:
            embed.color = PURPLE
            embed.add_field(name="Rejected", value=f"❌ {interaction.user.mention}", inline=False)
            embed.set_footer(text="Rejected ❌")
            await interaction.edit_original_response(embed=embed, view=verify_view(self.member_id, disabled=True))


def verify_view(member_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyButton("accept", member_id, disabled))
    view.add_item(VerifyButton("reject", member_id, disabled))
    return view


# ====================== MOD panel (help alerts) ======================
def _is_claimed(embed: discord.Embed) -> bool:
    return get_field(embed, "Claimed by") is not None


async def resolve_alert(interaction: discord.Interaction, member_id: int, note: str | None = None):
    """Marks a mod alert as resolved (used by both the one-click button and the note popup)
    and updates the member's panel to 'Resolved'."""
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

    # Tell the member (their panel gets a final status and its buttons are disabled).
    await close_help_request(
        member_id,
        f"🟢 Resolved by {interaction.user.mention}",
        PURPLE,
        note="## All done\nA **moderator** has handled your request.\n*Thank you for your patience.*",
    )


class ResolveNoteModal(discord.ui.Modal, title="Resolve Help Request"):
    """Popup asking what the problem was, shown when a mod clicks Resolve."""

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
    """Buttons on the mod alert:
        claim    -> "I'm on it" (the member sees who is handling them)
        resolved -> close it in one click
        note     -> close it, with a popup for what the problem was
    None of these touch any roles."""

    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "claim":
            label, style = "Claim", discord.ButtonStyle.secondary
        elif action == "resolved":
            label, style = "Mark as Resolved", discord.ButtonStyle.secondary
        else:
            label, style = "Resolve", discord.ButtonStyle.secondary
        super().__init__(
            discord.ui.Button(
                label=label,
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
            set_field(embed, "Status", "🟣 `Claimed`", inline=True)
            set_field(embed, "Claimed by", interaction.user.mention, inline=True)
            await interaction.response.edit_message(embed=embed, view=help_view(self.member_id, claimed=True))

            entry = help_panels.get(self.member_id)
            if entry is not None:
                entry["claimed"] = True
            await set_panel_status(
                self.member_id,
                f"🟣 {interaction.user.mention} is handling your request",
                PURPLE,
                note="## A moderator is on it\nSomeone has picked up your request and will be with you in a moment.\n*Please stay in the voice channel.*",
            )


def help_view(member_id: int, claimed: bool = False, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(HelpButton("claim", member_id, disabled or claimed))
    view.add_item(HelpButton("note", member_id, disabled))
    return view


class IssueReportView(discord.ui.View):
    """'Mark as Seen' button for issue reports. It flips the report to green, shows WHO saw it
    and WHEN, so mods can tell what's already been looked at at a glance."""

    def __init__(self, seen: bool = False):
        super().__init__(timeout=None)
        if seen:
            self.mark_seen.label = "Seen"
            self.mark_seen.disabled = True

    @discord.ui.button(label="Mark as Seen", style=discord.ButtonStyle.secondary, custom_id="issue_report_seen")
    async def mark_seen(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0].copy()
        if any(f.name == "Seen by" for f in embed.fields):
            return await interaction.response.send_message("Already marked as seen.", ephemeral=True)
        embed.color = PURPLE
        set_field(embed, "Status", "🟢 `Seen`", inline=True)
        set_field(embed, "Seen by", interaction.user.mention, inline=True)
        set_field(embed, "Seen At", discord.utils.format_dt(discord.utils.utcnow(), "R"), inline=True)
        await interaction.response.edit_message(embed=embed, view=IssueReportView(seen=True))


# ====================== MEMBER panel (ONE shared panel for everyone) ======================
def build_shared_embed(guild: discord.Guild) -> discord.Embed:
    """The single panel in #describe-issue. It lists everyone currently waiting for a moderator."""
    queue = sorted(
        ((mid, e) for mid, e in help_panels.items() if e.get("show")),
        key=lambda item: item[1]["joined_at"],
    )
    if queue:
        lines = [
            f"**{i}.** <@{mid}> — {e['status']} • {discord.utils.format_dt(e['joined_at'], 'R')}"
            for i, (mid, e) in enumerate(queue[:15], start=1)
        ]
        if len(queue) > 15:
            lines.append(f"*…and {len(queue) - 15} more*")
        queue_text = "\n".join(lines)
    else:
        queue_text = "*Nobody is waiting right now.*"

    embed = discord.Embed(
        title=PANEL_TITLE,
        description=(
            "## Moderator Support\n"
            "Need help from a **moderator**? Join the **Waiting for Help** voice channel and you will "
            "be added to the queue below automatically. A moderator will join you as soon as one is available.\n"
            "\n"
            "### How it works\n"
            "**1.** Join the voice channel and stay in it, so we can find you.\n"
            "**2.** Press **Describe Issue** and tell us what happened, who is involved and when it happened.\n"
            "**3.** Press **Cancel Request** if you no longer need help.\n"
            "\n"
            "### Current queue\n"
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
    """Edits the ONE shared panel (or creates it if it doesn't exist / was deleted)."""
    global shared_panel
    channel = guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        print("⚠️ Couldn't find the #describe-issue channel — check MEMBER_HELP_PANEL_CHANNEL_ID")
        return
    async with _panel_lock:
        embed = build_shared_embed(guild)
        if shared_panel is not None:
            try:
                await shared_panel.edit(content=None, embed=embed, view=panel_view())  # content=None wipes any old text above the panel
                return
            except discord.NotFound:
                shared_panel = None  # someone deleted it -> send a new one below
            except discord.HTTPException as e:
                print(f"⚠️ Failed to update the shared panel: {e}")
                return
        try:
            shared_panel = await send_with_retry(channel, embed=embed, view=panel_view())
            print("✅ Shared support panel sent")
        except Exception as e:
            print(f"❌ Failed to send the shared panel: {e}")


async def ensure_shared_panel(guild: discord.Guild):
    """On startup: reuse the newest panel already in the channel and delete any extra/old ones,
    so there is exactly ONE panel."""
    global shared_panel
    channel = guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return
    found = []
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id == bot.user.id and msg.embeds and "Support Request Received" in (msg.embeds[0].title or ""):
                found.append(msg)  # newest first
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


async def ping_member(channel: discord.TextChannel, member: discord.Member):
    """Short @mention message so the member gets a notification; it deletes itself."""
    try:
        msg = await channel.send(
            f"{member.mention} your request is in — check the panel.",
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
        await asyncio.sleep(PING_DELETE_AFTER)
        await msg.delete()
    except discord.HTTPException:
        pass


async def set_panel_status(member_id: int, status: str, color: discord.Color | None = None, note: str | None = None):
    """Changes the member's status line in the shared panel (color / note are kept only so old
    calls still work)."""
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
):
    """Finishes a help request: the member is removed from the shared panel. If `alert_footer`
    is given, the mod alert is updated too (used when the member leaves, cancels, or gets moved).
    (status / color / note / skip_panel are only kept so older calls still work.)

    lock_alert=True  -> the alert's buttons are disabled (request is over).
    lock_alert=False -> the alert only gets its new Status; Claim / Resolve stay pressable
                        so a mod can still handle it and leave a note."""
    entry = help_panels.pop(member_id, None)
    if entry is None:
        return

    alert = entry.get("alert")
    if alert_footer and alert is not None:
        try:
            fresh = await alert.channel.fetch_message(alert.id)  # get the latest version (claims, etc.)
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

    # Take the member off the shared panel.
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await refresh_shared_panel(guild)


# ====================== Recovery (restarts) + leave grace period ======================
def entry_from_alert(msg: discord.Message, config: dict) -> dict:
    """Rebuilds a help_panels entry from an alert message that is still open."""
    embed = msg.embeds[0]
    claimed_by = get_field(embed, "Claimed by")
    issue = get_field(embed, "Issue")
    described = bool(issue) and "hasn't described" not in issue
    if claimed_by:
        status = f"🟣 {claimed_by} is handling your request"
    elif described:
        status = "🟡 Message sent — waiting for a moderator"
    else:
        status = "🟡 Waiting for a moderator"
    return {
        "alert": msg,
        "described": described,
        "claimed": bool(claimed_by),
        "show": config.get("send_member_panel", True),
        "status": status,
        "joined_at": msg.created_at,
    }


async def scan_latest_alerts(channel) -> dict[int, discord.Message]:
    """{member_id: newest mod alert the bot posted for them} (open or finished)."""
    latest: dict[int, discord.Message] = {}
    try:
        async for msg in channel.history(limit=100):  # newest first
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
    """Open = still needs handling. ('Member moved' alerts are open on purpose, but the
    member is no longer waiting, so they don't count as a live request.)"""
    footer = msg.embeds[0].footer.text if msg.embeds else None
    return footer not in ALERT_DONE_FOOTERS and footer != "Member moved"


async def recover_help_request_for(member: discord.Member) -> bool:
    """Used when a member presses a panel button but the bot has no record of their request
    (for example the bot restarted while they were waiting). Re-attaches their still-open
    mod alert. Never creates a new alert."""
    channel = member.guild.get_channel(HELP_ALERT_CHANNEL_ID)
    vc = member.voice.channel if member.voice else None
    if channel is None or vc is None or vc.id not in HELP_VC_CONFIG:
        return False
    msg = (await scan_latest_alerts(channel)).get(member.id)
    if msg is None or not _alert_is_open(msg):
        return False
    if member.id not in help_panels:
        help_panels[member.id] = entry_from_alert(msg, HELP_VC_CONFIG[vc.id])
        await refresh_shared_panel(member.guild)
    return True


async def recover_help_requests(guild: discord.Guild):
    """Runs once at startup so a restart / redeploy never loses the queue:
    - members waiting with a still-open alert  -> their request is re-attached
    - members waiting with no alert at all (or whose last one ended by leaving / cancelling)
      -> a fresh alert is sent
    - members waiting whose last alert was Resolved -> left alone
    - open alerts of members who are no longer waiting -> closed as 'Member left'"""
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
        if mid not in waiting and _alert_is_open(msg):
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
            help_panels[mid] = entry_from_alert(msg, config)
            print(f"♻️ Recovered help request for {m}")
        elif msg is not None and msg.embeds and msg.embeds[0].footer.text in ("Resolved", "Resolved ✅", "Resolved ☑️"):
            continue  # already handled, still sitting in the channel
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
        return  # they are back
    await close_help_request(member_id, "🔴 Left the queue", PURPLE, alert_footer="Member left")


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
        # Answer the member first so the popup never times out, then do the rest.
        # Ephemeral - only the member who submitted it sees this confirmation.
        await interaction.response.send_message(
            "Got it, thanks. Your message has been sent to the moderators and one of them will be with you shortly.",
            ephemeral=True,
        )

        member = interaction.guild.get_member(self.member_id) if interaction.guild else None
        reports_channel = interaction.guild.get_channel(ISSUE_REPORTS_CHANNEL_ID) if interaction.guild else None

        # 1) Put the issue straight on the mod alert so everything is in one place.
        entry = help_panels.get(self.member_id)
        if entry is not None:
            entry["described"] = True
            alert = entry.get("alert")
            if alert is not None:
                try:
                    fresh = await alert.channel.fetch_message(alert.id)
                    if fresh.embeds and fresh.embeds[0].footer.text not in ALERT_DONE_FOOTERS:
                        embed = fresh.embeds[0].copy()
                        set_field(embed, "Issue", f">>> {self.issue.value}"[:1024], inline=False)
                        await fresh.edit(embed=embed, view=help_view(self.member_id, claimed=_is_claimed(embed)))
                except discord.HTTPException as e:
                    print(f"⚠️ Couldn't add the issue to the mod alert: {e}")

            if not entry.get("claimed"):
                await set_panel_status(
                    self.member_id,
                    "🟡 Message sent — waiting for a moderator",
                    PURPLE,
                    note="## Message received\nYour message has reached the **moderators**.\n*One of them will read it and be with you shortly. Please stay in the voice channel.*",
                )

        # 2) Also post it to the issue reports channel (kept from before).
        if reports_channel is None:
            print("⚠️ Couldn't find the issue reports channel — check ISSUE_REPORTS_CHANNEL_ID")
        else:
            embed = discord.Embed(
                title="Issue Report",
                description=(
                    f"## New issue from <@{self.member_id}>\n"
                    "*Sent from the support panel.*\n"
                    "\n"
                    "### Their message\n"
                    f">>> {self.issue.value}"
                ),
                color=PURPLE,
            )
            if member:
                embed.set_author(name=f"{member.display_name} ({member})", icon_url=member.display_avatar.url)
            else:
                embed.set_author(name=f"Unknown member ({self.member_id})")

            voice = member.voice.channel if member and member.voice and member.voice.channel else None
            alert_msg = entry.get("alert") if entry else None

            # Row 1: who
            embed.add_field(name="Member", value=f"<@{self.member_id}>", inline=True)
            embed.add_field(name="User ID", value=f"`{self.member_id}`", inline=True)
            embed.add_field(name="Submitted", value=discord.utils.format_dt(discord.utils.utcnow(), "R"), inline=True)
            # Row 2: where / state / link to the live request
            embed.add_field(name="Voice Channel", value=voice.mention if voice else "`Not in a voice channel`", inline=True)
            embed.add_field(name="Status", value="🟡 `Not seen yet`", inline=True)
            if alert_msg is not None:
                embed.add_field(name="Help Request", value=f"[Jump to request]({alert_msg.jump_url})", inline=True)

            embed.set_footer(
                text="ELITE LEADERS COMMUNITY • Issue Reports",
                icon_url=interaction.guild.icon.url if interaction.guild and interaction.guild.icon else None,
            )
            embed.timestamp = discord.utils.utcnow()
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
            label, emoji = "Describe Issue", None
        else:
            label, emoji = "Cancel Request", CANCEL_EMOJI
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,  # grey
                custom_id=f"member_{action}",
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        entry = help_panels.get(interaction.user.id)
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        in_help = bool(member and member.voice and member.voice.channel and member.voice.channel.id in HELP_VC_CONFIG)

        if entry is None and in_help:
            # The bot may have restarted while they were waiting -> re-attach their open alert.
            if await recover_help_request_for(member):
                entry = help_panels.get(interaction.user.id)

        if entry is None or not entry.get("show"):
            if in_help:
                text = "Your request is already closed. Leave and rejoin the **Waiting for Help** voice channel to open a new one."
            else:
                text = "You don't have an active request. Join the **Waiting for Help** voice channel and you will be added to this panel."
            await interaction.response.send_message(text, ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        member_id = interaction.user.id
        if self.action == "describe":
            await interaction.response.send_modal(DescribeIssueModal(member_id))
            return

        await interaction.response.send_message(
            "Your request has been cancelled. Join the help channel again any time you need a **moderator**.",
            ephemeral=True,
        )
        # Close the mod alert too, so nobody chases a request that was cancelled.
        await close_help_request(
            member_id,
            "⚪ Cancelled",
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
    # Re-register every button so they keep working after a restart / redeploy.
    bot.add_dynamic_items(VerifyButton, HelpButton, MemberPanelButton)
    bot.add_view(IssueReportView())


async def nickname_watchdog():
    """Safety net so EVERY member in a waiting / help / report room always has their emoji.
    Every 15 seconds it checks everyone sitting in those channels and fixes any nickname
    that is missing its emoji or has more than one (for example if a nickname edit was
    rate-limited or lost a race). Members who already have exactly the right emoji are
    skipped, so it costs nothing."""
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
        return  # reconnect - the one-time nickname sync below already ran
    _startup_sync_done = True

    watchdog_task = asyncio.create_task(nickname_watchdog())
    _bg_tasks.add(watchdog_task)  # keep a reference so it is never garbage-collected

    # The emoji feature can't work without this permission - say so loudly at startup.
    if not guild.me.guild_permissions.manage_nicknames:
        print("⚠️ The bot is missing the 'Manage Nicknames' permission — the ⏳/📛 nickname emoji will NOT work until you give it")

    # Sync nickname state in case people were already waiting when the bot started
    # (or it crashed mid-rename last time). This covers Waiting for Move, Waiting for
    # Help AND all REPORT channels.
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

    # Report rooms created before a restart: delete the empty ones, sync the occupied ones.
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

    # Clean up leftover emoji nicknames (e.g. someone left while the bot was offline)
    for m in list(guild.members):
        if m.bot or _in_alert_vc(m):
            continue
        if _strip_alert_prefix(m.nick)[1]:
            await set_report_vc_alert(m)  # not in an alert VC -> restores the original nickname

    # Make sure there is exactly ONE shared panel in #describe-issue (deletes old extra ones).
    await ensure_shared_panel(guild)

    # Rebuild the help queue after a restart / redeploy, so waiting members keep working buttons.
    await recover_help_requests(guild)
    await refresh_shared_panel(guild)


@bot.event
async def on_invite_create(invite: discord.Invite):
    """Refresh invite cache when a new invite is created."""
    if invite.guild and invite.guild.id == GUILD_ID:
        await cache_invites(invite.guild)
        print(f"📝 Invite created: {invite.code}")


@bot.event
async def on_invite_delete(invite: discord.Invite):
    """Refresh invite cache when an invite is deleted.
    (Careful: single-use invites vanish the moment they're used, so we must NOT
    overwrite the cache here — on_member_join needs the old entry to spot them.)"""
    if invite.guild and invite.guild.id == GUILD_ID:
        print(f"🗑️ Invite deleted: {invite.code}")


@bot.event
async def on_member_join(member: discord.Member):
    """Track who invited the member by comparing invite use counts (and catching single-use
    invites, which Discord deletes the instant they're used, before this even runs)."""
    if member.guild.id != GUILD_ID:
        return

    print(f"➕ {member} joined the server")

    # Give Discord a second to update the invite counters
    await asyncio.sleep(1)

    try:
        current_invites = await member.guild.invites()
        current_by_code = {invite.code: invite for invite in current_invites}
        old_cache = invite_cache.get(member.guild.id, {})

        inviter = None

        # Case 1: an invite that still exists went up in use count
        for invite in current_invites:
            old_uses = old_cache.get(invite.code, {}).get("uses", 0)
            if (invite.uses or 0) > old_uses:
                inviter = invite.inviter
                print(f"👤 {member} was invited by {inviter} (invite {invite.code})")
                break

        # Case 2: a limited-use invite (e.g. a single-use link) got used up and Discord
        # deleted it, so it's no longer in current_invites to compare against at all.
        # If an invite we knew about is now gone, and it was exactly one use away from
        # its limit, that almost certainly means this join is what used it up.
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
            # Could be vanity URL or invite info not available
            print(f"❓ {member} joined via unknown invite (possibly vanity URL)")
            member_inviters[member.id] = None

        # Update cache
        invite_cache[member.guild.id] = {
            invite.code: {"uses": invite.uses or 0, "max_uses": invite.max_uses, "inviter": invite.inviter}
            for invite in current_invites
        }
    except discord.Forbidden:
        print("⚠️ Bot doesn't have permission to view invites")
    except discord.HTTPException as e:
        print(f"⚠️ Failed to read invites: {e}")


async def find_latest_verification(channel: discord.TextChannel, member_id: int) -> discord.Message | None:
    """Newest verification request the bot posted for this member (open or finished).
    Used after a restart, when the in-memory cache is empty."""
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
    """Anti-spam wrapper: one verification request at a time per member."""
    lock = _verify_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        await _send_verification_alert(member, voice_channel)


async def _send_verification_alert(member: discord.Member, voice_channel: discord.VoiceChannel):
    """Posts the 'awaiting verification' embed with Verify/Reject buttons."""
    # Only alert for members who have the Unverified role
    unverified_role = member.guild.get_role(UNVERIFIED_ROLE_ID)
    if unverified_role is None or unverified_role not in member.roles:
        print(f"⏭️ Skipped {member}: no Unverified role")
        return

    verification_channel = member.guild.get_channel(VERIFICATION_CHANNEL_ID)
    if verification_channel is None:
        print("⚠️ Couldn't find the verification channel — check VERIFICATION_CHANNEL_ID")
        return

    # ---------- anti-spam: has this member already got a request? ----------
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
            entry = verify_alerts[member.id] = {"message": existing, "rejoins": 0, "last_edit": 0.0}

    if existing is not None and existing.embeds:
        if existing.embeds[0].footer.text == VERIFY_OPEN_FOOTER:
            # Still open -> NEVER post another one. Just count the re-join on the existing request.
            entry["rejoins"] = entry.get("rejoins", 0) + 1
            print(f"⏭️ {member} re-joined, verification request already open (x{entry['rejoins']})")
            if time.monotonic() - entry.get("last_edit", 0.0) >= 5:
                try:
                    embed = existing.embeds[0].copy()
                    set_field(embed, "Re-joined the voice channel", f"`{entry['rejoins']}` time(s) since this request", inline=False)
                    await existing.edit(embed=embed)
                    entry["last_edit"] = time.monotonic()
                except discord.HTTPException:
                    pass
            return
        age = (discord.utils.utcnow() - existing.created_at).total_seconds()
        if age < VERIFY_REALERT_COOLDOWN:
            print(f"⏭️ {member} re-joined too soon after the last request was handled ({int(age)}s) — skipped")
            return

    print(f"📤 Sending verification message for {member}")

    # Build description with invite info if available
    inviter = member_inviters.get(member.id)
    invited_by_text = f"Invited by: {inviter.mention}" if inviter else "Invited by: Unknown (vanity URL or unknown invite)"
    joined_text = discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown"

    # Warning for brand-new accounts (younger than NEW_ACCOUNT_WARNING_DAYS days).
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
                f"{WARN_EMOJI} **New account warning**\n"
                f"> This account was created {discord.utils.format_dt(member.created_at, 'R')} "
                f"(only **{age_label}**, under {NEW_ACCOUNT_WARNING_DAYS} days). "
                f"Check them carefully before verifying.\n\n"
            )

    embed = discord.Embed(
        title="Member awaiting verification",
        description=(
            f"{warning_text}"
            f"Member: {member.mention}\n"
            f"ID: `{member.id}`\n"
            f"Account created: {discord.utils.format_dt(member.created_at, 'R')}\n"
            f"Joined server: {joined_text}\n"
            f"{invited_by_text}\n"
            f"Joined voice channel: *{voice_channel.name}*"
        ),
        color=PURPLE,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=VERIFY_OPEN_FOOTER)

    try:
        sent = await send_with_retry(
            verification_channel,
            content="@everyone A member in the voice channel needs verification",
            embed=embed,
            view=verify_view(member.id),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        verify_alerts[member.id] = {"message": sent, "rejoins": 0, "last_edit": 0.0}
        print(f"✅ Verification message sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send verification message: {e}")


def reserve_help_request(member: discord.Member, config: dict) -> bool:
    """Creates the member's request entry right away (before any await), so a second voice event
    can never start a second request. Returns False if they already have one."""
    if member.id in help_panels:
        return False
    help_panels[member.id] = {
        "alert": None,
        "described": False,
        "claimed": False,
        "show": config.get("send_member_panel", True),  # listed on the shared panel?
        "status": "🟡 Waiting for a moderator",
        "joined_at": discord.utils.utcnow(),
    }
    return True


async def send_help_alert(member: discord.Member, voice_channel: discord.VoiceChannel, config: dict, ping: bool = True):
    """Posts the mod panel (alert + Claim / Resolve buttons) and, if turned on for this
    channel, adds the member to the shared panel in #describe-issue. `config` is this VC's entry from HELP_VC_CONFIG."""
    if not reserve_help_request(member, config):
        print(f"⏭️ {member} already has an active help request, skipping")
        return
    await _post_help_alert(member, voice_channel, config, ping)


async def _post_help_alert(member: discord.Member, voice_channel: discord.VoiceChannel, config: dict, ping: bool = True):
    help_channel = member.guild.get_channel(HELP_ALERT_CHANNEL_ID)
    if help_channel is None:
        print("⚠️ Couldn't find the help alert channel — check HELP_ALERT_CHANNEL_ID")
        help_panels.pop(member.id, None)
        return

    emoji = config.get("emoji", "")
    print(f"📤 Sending help alert for {member} ({emoji})")

    # ---------------- MOD panel ----------------
    embed = discord.Embed(
        title=f"{emoji} Help Request".strip(),
        description=(
            f"## {member.mention} needs a moderator\n"
            f"Waiting in {voice_channel.mention} — *join them to help.*"
        ),
        color=PURPLE,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    # Row 1: who
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    # Row 2: where / when / state
    embed.add_field(name="Voice Channel", value=voice_channel.mention, inline=True)
    embed.add_field(name="Waiting Since", value=discord.utils.format_dt(discord.utils.utcnow(), "R"), inline=True)
    embed.add_field(name="Status", value="🟡 `Waiting`", inline=True)
    if config.get("send_member_panel", True):
        # Filled in automatically when the member presses "Describe Issue".
        embed.add_field(name="Issue", value="*The member hasn't described the issue yet.*", inline=False)
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

    # Track this request so the mod alert and the shared panel can update each other.
    entry = help_panels.get(member.id)
    if entry is None:
        # The member already left while the alert was being sent -> close the alert right away.
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

    if not entry.get("show"):
        return  # shared panel turned off for this channel — mod alert only

    # Add the member to the ONE shared panel (their @mention is listed inside it).
    await refresh_shared_panel(member.guild)

    if PING_ON_JOIN and ping:
        ping_channel = member.guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
        if isinstance(ping_channel, discord.TextChannel):
            task = asyncio.create_task(ping_member(ping_channel, member))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """
    Handles all waiting voice channels.

    - Waiting for Move VC:
        * adds ⏳ to the member's nickname (right away, in parallel with the
          verification alert - a slow nickname edit never delays the alert)
        * sends the verification alert
    - Help VC:
        * adds ⏳ to the member's nickname
        * sends the mod alert and adds the member to the ONE shared panel
    - Report VCs:
        * adds 📛 to the member's nickname
    - When a member leaves these VCs:
        * removes the emoji and restores their previous nickname

    Moving directly between these channels swaps the emoji if needed (⏳ <-> 📛).
    """
    if member.guild.id != GUILD_ID or member.bot:
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    # Ignore mute/deafen/stream/camera changes in the same VC.
    if before_id == after_id:
        return

    _nick_failed.discard(member.id)  # new channel -> try renaming again

    # Help panel cleanup when a member leaves a help VC.
    if member.id in help_panels and before_id in HELP_VC_CONFIG:
        if after_id is None:
            # Left voice entirely -> keep the request open for a short grace period. If they come
            # back in time nothing happens; if not, both panels are closed.
            entry = help_panels.get(member.id)
            if entry is not None and member.id not in _leave_tasks:
                _leave_tasks[member.id] = asyncio.create_task(close_after_grace(member.id))
                entry["status_before"] = entry["status"]
                await set_panel_status(member.id, "🟠 Disconnected — waiting for them to reconnect")
        elif after_id != before_id:
            # Moved to another channel -> the member panel finishes, but the mod
            # alert stays OPEN (Claim / Resolve keep working) so it can still be handled
            # and a note can be left.
            await close_help_request(
                member.id,
                "🟢 Resolved — a moderator moved you",
                PURPLE,
                alert_footer="Member moved",
                lock_alert=False,
                note="## All done\nA **moderator** moved you, so this request is finished.\n*Thank you for your patience.*",
            )

    # Nickname emoji: start it right away as its own task so it runs at the same time as
    # the alerts below. set_report_vc_alert() looks at where the member is RIGHT NOW,
    # so it adds the emoji on join/move-in and removes it on leave/move-out.
    nick_task = None
    if (
        before_id in ALERT_VC_IDS
        or after_id in ALERT_VC_IDS
        or _is_report_room(before.channel)
        or _is_report_room(after.channel)
    ):
        nick_task = asyncio.create_task(set_report_vc_alert(member))

    # Someone left an auto-created report room -> delete it if it's now empty.
    if before.channel is not None and _is_report_room(before.channel):
        asyncio.create_task(delete_if_empty_report_room(before.channel))

    try:
        if after_id in REPORT_VC_IDS:
            # Moved into the trigger channel -> give them their own report room.
            await create_report_room(member)

        elif after_id == WAITING_VC_ID:
            if isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)):
                await send_verification_alert(member, after.channel)

        elif after_id in HELP_VC_CONFIG:
            # Back within the grace period? -> keep the same request, no new alert.
            pending = _leave_tasks.pop(member.id, None)
            if pending is not None:
                pending.cancel()
                entry = help_panels.get(member.id)
                if entry is not None:
                    await set_panel_status(member.id, entry.pop("status_before", "🟡 Waiting for a moderator"))
            if isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)):
                await send_help_alert(
                    member,
                    after.channel,
                    HELP_VC_CONFIG[after_id],
                )
    finally:
        # Always wait for the nickname task, even if an alert raised, so it is never orphaned.
        if nick_task is not None:
            await nick_task


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
