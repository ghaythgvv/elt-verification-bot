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
   - MODERATOR PANEL: the bot posts an alert pinging @everyone with 2 buttons:
         Claim                 -> the member instantly sees WHO is handling them
         Resolve               -> closes the request (popup for an optional note about the
                                  problem); the member panel updates by itself
     The voice channel name is a clickable link, so a MODERATOR can jump straight in.
   - MEMBER PANEL: the member gets a clean panel with a live Status
         🟡 Waiting for a MODERATOR  ->  🟣 <moderator> is handling your request
         ->  🟢 Resolved  (or 🔴 left the queue / ⚪ cancelled)
     plus "Describe Issue" and "Cancel Request" buttons.
   - When the member presses "Describe Issue", their text is added to the MODERATOR alert
     (so moderators see everything in ONE place) and also posted to ISSUE_REPORTS_CHANNEL_ID.
   - If the member leaves, cancels, or gets moved, the MODERATOR alert closes by itself.
   - Once the member panel is finished (🟢 resolved / 🔴 left / ⚪ cancelled) it is
     DELETED automatically 10 minutes later (set PANEL_DELETE_AFTER to change this).
   - Each waiting-for-help channel has its own emoji shown in the alert title, and can
     independently turn the member-facing panel on or off (see HELP_VC_CONFIG below).
3) When a member joins one of the "REPORT" voice channels:
   - No message is sent anywhere. Instead the bot prefixes that member's own server
     nickname with ⛔ while they're waiting in the channel, and removes
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
    - Set DISCORD_TOKEN as an environment variable (Railway > Variables).
"""

from __future__ import annotations

import os
import asyncio

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

HELP_ALERT_CHANNEL_ID = 1551163762084683866  # channel where the MODERATOR alert gets posted (the Waiting for Help voice channel's own chat)
MEMBER_HELP_PANEL_CHANNEL_ID = 1553786062579699722  # channel where the member-facing panel gets posted
ISSUE_REPORTS_CHANNEL_ID = 1553196616146493460  # channel where "Describe Issue" submissions get posted

# Per-VC settings for the "waiting for help" flow (MODERATOR alert + optional member panel):
#   - emoji: shown in the MODERATOR alert title, so you can tell at a glance which
#     channel triggered it
#   - send_member_panel: whether the member-facing panel (Describe Issue / Cancel)
#     gets sent for this channel (False = MODERATOR alert only, no member panel)
HELP_VC_CONFIG = {
    1517941411151085691: {"emoji": "", "send_member_panel": True},
}

# Nickname emojis. While a member is sitting in one of these voice channels, the bot
# prefixes THEIR OWN server nickname with the channel's emoji. As soon as they leave,
# their nickname is restored.
#   - Waiting for Move + every Waiting for Help channel -> WAITING_VC_EMOJI (⏳)
#   - REPORT channels                                   -> REPORT_VC_EMOJI  (⛔)
WAITING_VC_EMOJI = "⏳"
REPORT_VC_EMOJI = "⛔"
REPORT_VC_IDS = {
    1517940974125318166,
    1554981588880850964,  # new REPORT channel (members joining it get their own room + the ⛔ emoji)
}
REPORT_CATEGORY_ID = 1517941029221695760
REPORT_CHANNEL_NAME = "⛔┃𝗥𝗘𝗣𝗢𝗥𝗧"  # name of the voice channel created for each report

# {voice channel id: emoji to put on the member's nickname}
ALERT_VC_EMOJIS = {
    WAITING_VC_ID: WAITING_VC_EMOJI,
    **{vc_id: WAITING_VC_EMOJI for vc_id in HELP_VC_CONFIG},
    **{vc_id: REPORT_VC_EMOJI for vc_id in REPORT_VC_IDS},
}
ALERT_VC_IDS = set(ALERT_VC_EMOJIS)
ALERT_EMOJIS = set(ALERT_VC_EMOJIS.values())

# Roles
UNVERIFIED_ROLE_ID = 1513904174079934657  # removed from the member at verify time if they have it (not given automatically anymore)
VERIFIED_ROLE_ID = 1513904156350353511    # given after verification

EXTRA_ROLES_ON_VERIFY = [1513904151309058159]  # MEMBER role - given alongside Verified
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
#     "message": member-facing panel message (or None if the panel is turned off),
#     "alert": the MODERATOR alert message,
#     "described": bool, "claimed": bool,
# }
# Lets the two panels stay in sync with each other (claim / resolve / leave / cancel).
help_panels = {}

_startup_sync_done = False  # on_ready can fire many times (reconnects) - only sync nicknames once

# Footers that mean "this MODERATOR alert is finished" (buttons are disabled).
ALERT_DONE_FOOTERS = ("Resolved", "Member left", "Cancelled", "Resolved ✅", "Member left ❌", "Cancelled ⚪")
# What the "Status" field of the MODERATOR alert says in each final state.
ALERT_STATUS = {
    "Resolved": "🟢 `Resolved`",
    "Member left": "🔴 `Member left`",
    "Cancelled": "⚪ `Cancelled`",
}

# Finished member panels (resolved / left / cancelled) delete themselves after this long.
PANEL_DELETE_AFTER = 10 * 60  # seconds = 10 minutes
# Custom emoji (paste the IDs of other emoji here if you ever change them).
# If one of them is an ANIMATED emoji, change "<:" to "<a:" / animated=False to animated=True.
SUPPORT_TITLE_EMOJI = "<:support:1555003977173573684>"   # shown in front of the panel title
CANCEL_EMOJI = discord.PartialEmoji(name="cancel", id=1554671367142645770, animated=False)
PANEL_TITLE = f"{SUPPORT_TITLE_EMOJI} Support Request Received"
PURPLE = discord.Color.purple()  # the colour of EVERY embed - change it here to recolour all panels at once
_bg_tasks: set = set()


async def delete_later(message: discord.Message, delay: float):
    """Waits, then deletes the message (quietly ignores it if it's already gone)."""
    await asyncio.sleep(max(delay, 0))
    try:
        await message.delete()
        print(f"🗑️ Deleted finished member panel {message.id}")
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def schedule_delete(message: discord.Message, delay: float = PANEL_DELETE_AFTER):
    task = asyncio.create_task(delete_later(message, delay))
    _bg_tasks.add(task)  # keep a reference so the task is never garbage-collected
    task.add_done_callback(_bg_tasks.discard)


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


# ====================== Nickname emoji (⏳ waiting / ⛔ report) ======================
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
        and channel.name == REPORT_CHANNEL_NAME
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
    """Creates a fresh report voice channel in the REPORT category and moves the member into it."""
    guild = member.guild
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

    report_room_ids.add(room.id)
    try:
        await member.move_to(room, reason="Moved into their report room")
        print(f"✅ Report room created for {member}")
        # Put the ⛔ on right now - don't wait for the voice event (it can arrive late).
        await asyncio.sleep(0.5)
        await set_report_vc_alert(member)
    except discord.Forbidden:
        print("⚠️ Can't move the member — give the bot 'Move Members'")
        await room.delete(reason="Couldn't move the member in")
    except discord.HTTPException as e:
        # Most likely the member already left the trigger channel.
        print(f"⚠️ Couldn't move {member} into the report room: {e}")
        await room.delete(reason="Member was no longer in voice")


async def delete_if_empty_report_room(channel):
    """Deletes an auto-created report room once no real person is left in it."""
    if not _is_report_room(channel):
        return
    if any(not m.bot for m in channel.members):
        return
    try:
        await channel.delete(reason="Report room is empty")
        report_room_ids.discard(channel.id)
        print(f"🗑️ Deleted empty report room {channel.id}")
    except discord.HTTPException:
        pass


def _in_alert_vc(member: discord.Member) -> bool:
    """True if the member is CURRENTLY in a waiting / help / report voice channel."""
    return _alert_emoji_for(member) is not None


def _strip_alert_prefix(nick: str | None) -> tuple[str | None, bool]:
    """Remove one of OUR emoji prefixes from a nickname. Returns (clean_nick, had_prefix)."""
    if nick:
        for emoji in ALERT_EMOJIS:
            prefix = f"{emoji} "
            if nick.startswith(prefix):
                return nick[len(prefix):], True
    return nick, False


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
    REPORT voice channel                              -> nickname gets the ⛔ prefix.
    Anywhere else                                     -> the original nickname is restored.

    The desired state is read from the member's live voice state (not from whichever
    event called us), and edits are serialized per member, so rapid join/leave/move
    sequences always end in the correct state.
    """
    lock = _nick_locks.setdefault(member.id, asyncio.Lock())
    async with lock:
        fresh = member.guild.get_member(member.id) or member
        emoji = _alert_emoji_for(fresh)
        active = emoji is not None

        if active:
            base_nick = get_report_vc_base_nickname(fresh)
            visible_name = base_nick or fresh.global_name or fresh.name
            visible_name, _ = _strip_alert_prefix(visible_name)  # never double up the emoji
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
            label, style = "✅ Verify", discord.ButtonStyle.secondary
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

        if _already_done(interaction.message, ("Verified ✅", "Rejected ❌")):
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
            embed.add_field(name="Verified", value=f"✅ {interaction.user.mention}", inline=False)
            embed.set_footer(text="Verified ✅")
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


# ====================== MODERATOR panel (help alerts) ======================
def _is_claimed(embed: discord.Embed) -> bool:
    return get_field(embed, "Claimed by") is not None


async def resolve_alert(interaction: discord.Interaction, member_id: int, note: str | None = None):
    """Marks a MODERATOR alert as resolved (used by both the one-click button and the note popup)
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
        note="## All done\nA **MODERATOR** has handled your request.\n*Thanks for your patience.*",
    )


class ResolveNoteModal(discord.ui.Modal, title="Resolve Help Request"):
    """Popup asking what the problem was, shown when a MODERATOR clicks Resolve."""

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
    """Buttons on the MODERATOR alert:
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
                note="## A MODERATOR is on it\nSomeone picked up your request and will be with you in a moment.",
            )


def help_view(member_id: int, claimed: bool = False, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(HelpButton("claim", member_id, disabled or claimed))
    view.add_item(HelpButton("note", member_id, disabled))
    return view


class IssueReportView(discord.ui.View):
    """'Mark as Seen' button for issue reports. It flips the report to green, shows WHO saw it
    and WHEN, so MODERATORS can tell what's already been looked at at a glance."""

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


# ====================== MEMBER panel ======================
def build_finished_embed(embed: discord.Embed, status: str, color: discord.Color, note: str | None) -> discord.Embed:
    """Copy of the member panel in its final state (final status, final message, delete notice)."""
    embed = embed.copy()
    embed.color = color
    embed.set_field_at(0, name="Status", value=status, inline=True)
    if note:
        embed.description = note
    icon = embed.footer.icon_url if embed.footer else None
    embed.set_footer(
        text="ELITE LEADERS COMMUNITY • This message will be deleted in 10 minutes",
        icon_url=icon,
    )
    return embed


async def set_panel_status(member_id: int, status: str, color: discord.Color, note: str | None = None):
    """Changes the Status line (and optionally the message) on the member's panel
    but keeps its buttons working."""
    entry = help_panels.get(member_id)
    if entry is None or entry.get("message") is None:
        return
    try:
        message = entry["message"]
        embed = message.embeds[0].copy()
        embed.color = color
        embed.set_field_at(0, name="Status", value=status, inline=True)
        if note:
            embed.description = note
        await message.edit(embed=embed, view=panel_view(member_id))
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        print(f"⚠️ Failed to update member panel status: {e}")


async def close_help_request(
    member_id: int,
    status: str,
    color: discord.Color,
    alert_footer: str | None = None,
    skip_panel: bool = False,
    note: str | None = None,
):
    """Finishes a help request: the member panel gets its final look, its buttons are
    disabled, and it is DELETED after 10 minutes. If `alert_footer` is given, the MODERATOR
    alert is closed too (used when the member leaves, cancels, or gets moved)."""
    entry = help_panels.pop(member_id, None)
    if entry is None:
        return

    panel = entry.get("message")
    if panel is not None and not skip_panel:
        try:
            embed = build_finished_embed(panel.embeds[0], status, color, note)
            await panel.edit(embed=embed, view=panel_view(member_id, disabled=True))
            schedule_delete(panel)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"⚠️ Failed to update member panel: {e}")
            schedule_delete(panel)

    alert = entry.get("alert")
    if alert_footer and alert is not None:
        try:
            fresh = await alert.channel.fetch_message(alert.id)  # get the latest version (claims, etc.)
            if fresh.embeds and fresh.embeds[0].footer.text not in ALERT_DONE_FOOTERS:
                embed = fresh.embeds[0].copy()
                embed.color = color
                set_field(embed, "Status", ALERT_STATUS.get(alert_footer, alert_footer), inline=True)
                embed.set_footer(text=alert_footer, icon_url=embed.footer.icon_url)
                await fresh.edit(embed=embed, view=help_view(member_id, disabled=True))
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            print(f"⚠️ Failed to update MODERATOR alert: {e}")


class DescribeIssueModal(discord.ui.Modal, title="Describe Your Issue"):
    issue = discord.ui.TextInput(
        label="What do you need help with?",
        style=discord.TextStyle.paragraph,
        placeholder="Briefly describe what's going on...",
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
            "Thanks — your issue has been sent to the MODERATORS. One will be with you shortly.",
            ephemeral=True,
        )

        member = interaction.guild.get_member(self.member_id) if interaction.guild else None
        reports_channel = interaction.guild.get_channel(ISSUE_REPORTS_CHANNEL_ID) if interaction.guild else None

        # 1) Put the issue straight on the MODERATOR alert so everything is in one place.
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
                    print(f"⚠️ Couldn't add the issue to the MODERATOR alert: {e}")

            if not entry.get("claimed"):
                await set_panel_status(
                    self.member_id,
                    "🟡 Issue sent — waiting for a MODERATOR",
                    PURPLE,
                    note="## Message received\nYour message reached the **MODERATORS**.\n*One will read it and be with you shortly.*",
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
    template=r"member_(?P<action>describe|cancel)_(?P<uid>[0-9]+)",
):
    """Buttons on the panel shown to the member themselves while they wait for a MODERATOR."""

    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "describe":
            label, emoji = "Describe Issue", None
        else:
            label, emoji = "Cancel Request", CANCEL_EMOJI
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,  # grey
                custom_id=f"member_{action}_{member_id}",
                disabled=disabled,
            )
        )
        self.action = action
        self.member_id = member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(match["action"], int(match["uid"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This panel isn't for you.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        if self.action == "describe":
            await interaction.response.send_modal(DescribeIssueModal(self.member_id))
            return

        cancel_note = "## Request cancelled\nJoin the help channel again any time you need a **MODERATOR**."
        embed = build_finished_embed(
            interaction.message.embeds[0],
            "⚪ You cancelled this request",
            PURPLE,
            cancel_note,
        )
        await interaction.response.edit_message(embed=embed, view=panel_view(self.member_id, disabled=True))
        schedule_delete(interaction.message)  # gone in 10 minutes

        # Close the MODERATOR alert too, so nobody chases a request that was cancelled.
        await close_help_request(
            self.member_id,
            "⚪ You cancelled this request",
            PURPLE,
            alert_footer="Cancelled",
            skip_panel=True,  # already edited + scheduled above
        )


def panel_view(member_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MemberPanelButton("describe", member_id, disabled))
    view.add_item(MemberPanelButton("cancel", member_id, disabled))
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
    that is missing its emoji (for example if a nickname edit was rate-limited or lost a race).
    Members who already have the right emoji are skipped, so it costs nothing."""
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
                        if want and not (m.nick or "").startswith(f"{want} "):
                            print(f"🔧 Watchdog: {m} is missing {want}, fixing")
                            await set_report_vc_alert(m)
        except Exception as e:
            print(f"⚠️ Nickname watchdog error: {e}")
        await asyncio.sleep(15)


async def cleanup_finished_panels(guild: discord.Guild):
    """After a restart: finished member panels (all buttons disabled) that were waiting to be
    deleted get deleted now if they're older than 10 minutes, or re-scheduled for the time left."""
    channel = guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return
    now = discord.utils.utcnow()
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id != bot.user.id or not msg.embeds:
                continue
            if "Support Request Received" not in (msg.embeds[0].title or ""):
                continue
            buttons = [c for row in msg.components for c in getattr(row, "children", [])]
            if not buttons or not all(getattr(c, "disabled", False) for c in buttons):
                continue  # still active
            finished_at = msg.edited_at or msg.created_at
            remaining = PANEL_DELETE_AFTER - (now - finished_at).total_seconds()
            schedule_delete(msg, remaining)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ Couldn't clean up old member panels (needs 'Read Message History'): {e}")


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
        print("⚠️ The bot is missing the 'Manage Nicknames' permission — the ⏳/⛔ nickname emoji will NOT work until you give it")

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

    # Finished member panels left over from before a restart -> delete when their 10 minutes are up
    await cleanup_finished_panels(guild)


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


async def send_verification_alert(member: discord.Member, voice_channel: discord.VoiceChannel):
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

    print(f"📤 Sending verification message for {member}")

    # Build description with invite info if available
    inviter = member_inviters.get(member.id)
    invited_by_text = f"Invited by: {inviter.mention}" if inviter else "Invited by: Unknown (vanity URL or unknown invite)"
    joined_text = discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown"

    embed = discord.Embed(
        title="Member awaiting verification",
        description=(
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
    embed.set_footer(text="Click Verify to let this member in")

    try:
        await send_with_retry(
            verification_channel,
            content="@everyone A member in the voice channel needs verification",
            embed=embed,
            view=verify_view(member.id),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        print(f"✅ Verification message sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send verification message: {e}")


async def send_help_alert(member: discord.Member, voice_channel: discord.VoiceChannel, config: dict):
    """Posts the MODERATOR panel (alert + Claim / Resolve buttons) and, if turned on for this
    channel, the member panel. `config` is this VC's entry from HELP_VC_CONFIG."""
    help_channel = member.guild.get_channel(HELP_ALERT_CHANNEL_ID)
    if help_channel is None:
        print("⚠️ Couldn't find the help alert channel — check HELP_ALERT_CHANNEL_ID")
        return

    emoji = config.get("emoji", "")
    print(f"📤 Sending help alert for {member} ({emoji})")

    # ---------------- MODERATOR panel ----------------
    embed = discord.Embed(
        title=f"{emoji} Help Request".strip(),
        description=(
            f"## {member.mention} needs a MODERATOR\n"
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
            content="@everyone A member needs a MODERATOR",
            embed=embed,
            view=help_view(member.id),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        print(f"✅ Help alert sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send help alert: {e}")

    # Track this request so the two panels can update each other.
    help_panels[member.id] = {
        "message": None,
        "alert": alert_message,
        "described": False,
        "claimed": False,
    }

    if not config.get("send_member_panel", True):
        # Member panel turned off for this channel — MODERATOR alert only.
        return

    # ---------------- MEMBER panel ----------------
    member_panel_channel = member.guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if member_panel_channel is None:
        print("⚠️ Couldn't find the member panel channel — check MEMBER_HELP_PANEL_CHANNEL_ID")
        return

    member_embed = discord.Embed(
        title=PANEL_TITLE,
        description=(
            f"## You're in the queue, {member.mention}\n"
            "A **MODERATOR** will join you as soon as possible.\n"
            "*Please stay in the voice channel.*\n"
            "\n"
            "### What you can do\n"
            "> `Describe Issue` — tell the MODERATORS what's wrong so they arrive prepared.\n"
            "> `Cancel Request` — use this if you no longer need help."
        ),
        color=PURPLE,
    )
    member_embed.set_thumbnail(url=member.display_avatar.url)
    member_embed.add_field(name="Status", value="🟡 Waiting for a MODERATOR", inline=True)
    member_embed.add_field(name="Requested", value=discord.utils.format_dt(discord.utils.utcnow(), "R"), inline=True)
    member_embed.add_field(name="Estimated Wait", value="`~1-5 minutes`", inline=True)
    member_embed.set_footer(
        text="ELITE LEADERS COMMUNITY • Support System",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )
    member_embed.timestamp = discord.utils.utcnow()

    try:
        panel_message = await send_with_retry(
            member_panel_channel,
            content=member.mention,
            embed=member_embed,
            view=panel_view(member.id),
        )
        print(f"✅ Member panel sent for {member}")
        help_panels[member.id]["message"] = panel_message
    except Exception as e:
        print(f"❌ Failed to send member panel: {e}")


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
        * sends the MODERATOR alert / member panel
    - Report VCs:
        * adds ⛔ to the member's nickname
    - When a member leaves these VCs:
        * removes the emoji and restores their previous nickname

    Moving directly between these channels swaps the emoji if needed (⏳ <-> ⛔).
    """
    if member.guild.id != GUILD_ID or member.bot:
        return

    before_id = before.channel.id if before.channel else None
    after_id = after.channel.id if after.channel else None

    # Ignore mute/deafen/stream/camera changes in the same VC.
    if before_id == after_id:
        return

    _nick_failed.discard(member.id)  # new channel -> try renaming again

    # Help panel cleanup when a member leaves a help VC. Both panels get closed.
    if member.id in help_panels and before_id in HELP_VC_CONFIG:
        if after_id is None:
            await close_help_request(
                member.id,
                "🔴 You left the queue",
                PURPLE,
                alert_footer="Member left",
                note="## You left the queue\nThis request was closed. Join the help channel again any time you need a **MODERATOR**.",
            )
        elif after_id != before_id:
            await close_help_request(
                member.id,
                "🟢 Resolved — a MODERATOR moved you",
                PURPLE,
                alert_footer="Resolved",
                note="## All done\nA **MODERATOR** moved you, so this request is finished.\n*Thanks for your patience.*",
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
