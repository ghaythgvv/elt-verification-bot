"""
Discord Verification Bot
=======================================
How it works:
1) When a member joins the "Waiting for Move" voice channel:
   - The bot posts an embed in the "VERIFICATION" text channel, pinging @everyone,
     with a Verify button and a Reject button. Anyone can click the buttons.
   - Shows who invited the member if available.
2) When a member joins one of the "Waiting for Help" voice channels:
   - The bot posts a simpler embed (no invited-by / joined-server info) pinging @everyone,
     with a "Mark as Resolved" button. No roles are changed for this flow.
   - Each waiting-for-help channel has its own emoji shown in the alert title, and can
     independently turn the member-facing "Describe Issue" report form on or off
     (see HELP_VC_CONFIG below).
   - The member-facing panel is never auto-deleted (not on resolve, not on leaving the VC) —
     it stays up until a staff member removes it manually.
   - When the member submits "Describe Issue", it's posted as an embed to
     ISSUE_REPORTS_CHANNEL_ID, and the member gets a private (ephemeral) confirmation.
3) When a member joins one of the "REPORT" voice channels:
   - No message is sent anywhere. Instead the bot prefixes that member's own server
     nickname with an alert emoji (⛔) while they're waiting in the channel, and removes
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
    - Set DISCORD_TOKEN as an environment variable (Railway > Variables).
"""

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

HELP_ALERT_CHANNEL_ID = 1551163762084683866  # channel where the staff alert gets posted (the Waiting for Help voice channel's own chat)
MEMBER_HELP_PANEL_CHANNEL_ID = 1553786062579699722  # channel where the member-facing panel gets posted
ISSUE_REPORTS_CHANNEL_ID = 1553196616146493460  # channel where "Describe Issue" submissions get posted

# Per-VC settings for the "waiting for help" flow (staff alert + optional member panel):
#   - emoji: shown in the staff alert embed title, so you can tell at a glance which
#     channel triggered it
#   - send_member_panel: whether the member-facing "Describe Issue" report form panel
#     gets sent for this channel (False = staff alert only, no report form)
HELP_VC_CONFIG = {
    1517941411151085691: {"emoji": "🔔", "send_member_panel": True},
}

# The "REPORT" voice channels: no message is ever sent for these. Instead, while a member
# is waiting in one of these channels, the bot prefixes THEIR OWN nickname with
# REPORT_VC_EMOJI. As soon as they leave the channel, their nickname is restored.
REPORT_VC_EMOJI = "⛔"
REPORT_VC_IDS = {
    1552746347113746453,
    1552746364775956620,
    1553159336925073410,
}

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
# nickname set") with REPORT_VC_EMOJI stripped off, so we can restore it exactly once
# they leave the REPORT voice channel.
report_vc_base_nicknames = {}

# help_panels[member_id] = {"message": discord.Message, "described": bool}
# Tracks the member-facing "waiting for help" panel so we can update its Status field
# when the member leaves/gets moved out of the help channel.
help_panels = {}

_startup_sync_done = False  # on_ready can fire many times (reconnects) - only sync nicknames once


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


def get_report_vc_base_nickname(member: discord.Member) -> str | None:
    """Returns member.nick with the REPORT_VC_EMOJI prefix stripped off (if present),
    caching the result so later calls always restore the true original nickname even
    after we've already prefixed it. None means "member had no nickname set"."""
    if member.id not in report_vc_base_nicknames:
        nick = member.nick
        prefix = f"{REPORT_VC_EMOJI} "
        if nick and nick.startswith(prefix):
            nick = nick[len(prefix):]
            # We used the username as a stand-in when they had no nickname, so after a
            # restart "⛔ username" should go back to "no nickname", not to "username".
            if not nick or nick == member.name:
                nick = None
        report_vc_base_nicknames[member.id] = nick
    return report_vc_base_nicknames[member.id]


async def set_report_vc_alert(member: discord.Member, active: bool):
    """Prefixes/un-prefixes a member's own nickname with REPORT_VC_EMOJI while they're
    waiting in a REPORT voice channel. Never sends any message - the renamed nickname
    itself is the alert."""
    base_nick = get_report_vc_base_nickname(member)
    if active:
        target_nick = f"{REPORT_VC_EMOJI} {base_nick or member.name}"[:32]  # Discord nick limit is 32
    else:
        target_nick = base_nick

    if member.nick == target_nick:
        if not active:
            report_vc_base_nicknames.pop(member.id, None)
        return  # already in the right state

    try:
        await member.edit(nick=target_nick, reason="Report VC occupancy changed")
        print(f"✏️ {member} nickname -> {target_nick!r}")
        if not active:
            # Done with this member for now — stop tracking so a later, different
            # nickname they set themselves isn't mistaken for one we set.
            report_vc_base_nicknames.pop(member.id, None)
    except discord.Forbidden:
        print(f"⚠️ Bot doesn't have permission to change {member}'s nickname")
        if not active:
            report_vc_base_nicknames.pop(member.id, None)
    except discord.HTTPException as e:
        print(f"⚠️ Failed to update {member}'s nickname: {e}")


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
            label, style = "✅ Verify", discord.ButtonStyle.success
        else:
            label, style = "❌ Reject", discord.ButtonStyle.danger
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

            embed.color = discord.Color.green()
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
            embed.color = discord.Color.red()
            embed.add_field(name="Rejected", value=f"❌ {interaction.user.mention}", inline=False)
            embed.set_footer(text="Rejected ❌")
            await interaction.edit_original_response(embed=embed, view=verify_view(self.member_id, disabled=True))


def verify_view(member_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyButton("accept", member_id, disabled))
    view.add_item(VerifyButton("reject", member_id, disabled))
    return view


class ResolveNoteModal(discord.ui.Modal, title="Resolve Help Request"):
    """Popup asking what the problem was, shown when staff click Mark as Resolved."""

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
        if interaction.message is None or not interaction.message.embeds:
            return await interaction.response.send_message("Couldn't find the alert message.", ephemeral=True)
        if _already_done(interaction.message, ("Resolved ✅",)):
            return await interaction.response.send_message("This request was already resolved.", ephemeral=True)

        embed = interaction.message.embeds[0].copy()
        embed.color = discord.Color.green()
        if self.note.value:
            embed.add_field(name="What happened", value=self.note.value, inline=False)
        embed.add_field(name="Resolved", value=f"✅ {interaction.user.mention}", inline=False)
        embed.set_footer(text="Resolved ✅")
        await interaction.response.edit_message(embed=embed, view=help_view(self.member_id, disabled=True))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Resolve modal error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)


class HelpResolveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"help_resolved_(?P<uid>[0-9]+)",
):
    """'Mark as Resolved' button for help alerts. Opens a popup asking what the problem was.
    Doesn't touch any roles."""

    def __init__(self, member_id: int, disabled: bool = False):
        super().__init__(
            discord.ui.Button(
                label="✅ Mark as Resolved",
                style=discord.ButtonStyle.success,
                custom_id=f"help_resolved_{member_id}",
                disabled=disabled,
            )
        )
        self.member_id = member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["uid"]))

    async def callback(self, interaction: discord.Interaction):
        if _already_done(interaction.message, ("Resolved ✅",)):
            return await interaction.response.send_message("This request was already resolved.", ephemeral=True)
        await interaction.response.send_modal(ResolveNoteModal(self.member_id))


def help_view(member_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(HelpResolveButton(member_id, disabled))
    return view


class IssueReportView(discord.ui.View):
    """Simple 'Mark as Seen' button for issue reports, so staff can tell what's already
    been looked at without anyone having to delete or reply to the embed."""

    def __init__(self, seen: bool = False):
        super().__init__(timeout=None)
        if seen:
            self.mark_seen.label = "✅ Seen"
            self.mark_seen.disabled = True

    @discord.ui.button(label="✅ Mark as Seen", style=discord.ButtonStyle.success, custom_id="issue_report_seen")
    async def mark_seen(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0].copy()
        if any(f.name == "Seen by" for f in embed.fields):
            return await interaction.response.send_message("Already marked as seen.", ephemeral=True)
        embed.color = discord.Color.green()
        embed.add_field(name="Seen by", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=embed, view=IssueReportView(seen=True))


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
        member = interaction.guild.get_member(self.member_id) if interaction.guild else None
        reports_channel = interaction.guild.get_channel(ISSUE_REPORTS_CHANNEL_ID) if interaction.guild else None

        if reports_channel is None:
            print("⚠️ Couldn't find the issue reports channel — check ISSUE_REPORTS_CHANNEL_ID")
        else:
            embed = discord.Embed(
                title="📝 New Issue Report",
                description=f">>> {self.issue.value}",
                color=discord.Color.orange(),
            )
            if member:
                embed.set_author(name=f"{member.display_name} ({member})", icon_url=member.display_avatar.url)
            else:
                embed.set_author(name=f"Unknown member ({self.member_id})")
            embed.add_field(name="Member", value=f"<@{self.member_id}>", inline=True)
            embed.add_field(name="User ID", value=f"`{self.member_id}`", inline=True)
            embed.set_footer(text="Member help panel")
            embed.timestamp = discord.utils.utcnow()
            try:
                await send_with_retry(reports_channel, embed=embed, view=IssueReportView())
                print(f"✅ Issue report sent for {member or self.member_id}")
            except Exception as e:
                print(f"❌ Failed to send issue report: {e}")

        panel_entry = help_panels.get(self.member_id)
        if panel_entry is not None:
            panel_entry["described"] = True

        # Ephemeral - only the member who submitted it sees this confirmation.
        await interaction.response.send_message(
            "✅ Thanks — your issue has been sent to staff. Someone will be with you shortly.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"❌ Describe-issue modal error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)


class MemberPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"member_(?P<action>describe|cancel)_(?P<uid>[0-9]+)",
):
    """Buttons on the panel shown to the member themselves while they wait for staff."""

    def __init__(self, action: str, member_id: int, disabled: bool = False):
        if action == "describe":
            label, style = "📝 Describe Issue", discord.ButtonStyle.primary
        else:
            label, style = "❌ Cancel Request", discord.ButtonStyle.secondary
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
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

        embed = interaction.message.embeds[0].copy()
        embed.color = discord.Color.greyple()
        embed.set_field_at(0, name="Status", value="⚪ Cancelled by member", inline=True)
        await interaction.response.edit_message(embed=embed, view=panel_view(self.member_id, disabled=True))
        help_panels.pop(self.member_id, None)


def panel_view(member_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(MemberPanelButton("describe", member_id, disabled))
    view.add_item(MemberPanelButton("cancel", member_id, disabled))
    return view


# ================================ Events ================================
@bot.event
async def setup_hook():
    # Re-register every button so they keep working after a restart / redeploy.
    bot.add_dynamic_items(VerifyButton, HelpResolveButton, MemberPanelButton)
    bot.add_view(IssueReportView())


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

    # Sync REPORT VC nickname state in case people were already waiting when the bot
    # started (or it crashed mid-rename last time).
    for vc_id in REPORT_VC_IDS:
        channel = guild.get_channel(vc_id)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            for waiting_member in channel.members:
                if not waiting_member.bot:
                    await set_report_vc_alert(waiting_member, True)
        elif channel is not None:
            print(f"⚠️ REPORT_VC_IDS has {vc_id}, but that's a {type(channel).__name__}, not a voice channel — skipping it")
        else:
            print(f"⚠️ REPORT_VC_IDS has {vc_id}, but I can't find that channel")

    # Clean up leftover emoji nicknames (e.g. someone left while the bot was offline)
    prefix = f"{REPORT_VC_EMOJI} "
    waiting_ids = {
        m.id
        for vc_id in REPORT_VC_IDS
        for m in getattr(guild.get_channel(vc_id), "members", [])
    }
    for m in guild.members:
        if m.bot or m.id in waiting_ids or not m.nick or not m.nick.startswith(prefix):
            continue
        clean = m.nick[len(prefix):]
        try:
            await m.edit(
                nick=None if (not clean or clean == m.name) else clean,
                reason="Removing leftover report alert emoji",
            )
            print(f"🧹 Removed leftover emoji from {m}")
        except (discord.Forbidden, discord.HTTPException):
            pass


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
        color=discord.Color.gold(),
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
    """Posts a 'needs help' embed - no invited-by/joined-server info, no role changes, just an
    alert + resolve button. `config` is this VC's entry from HELP_VC_CONFIG (emoji + whether
    the member-facing report form panel should be sent)."""
    help_channel = member.guild.get_channel(HELP_ALERT_CHANNEL_ID)
    if help_channel is None:
        print("⚠️ Couldn't find the help alert channel — check HELP_ALERT_CHANNEL_ID")
        return

    emoji = config.get("emoji", "🔔")
    print(f"📤 Sending help alert for {member} ({emoji})")

    embed = discord.Embed(
        title=f"{emoji} Member needs help".strip(),
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Member", value=member.mention, inline=True)
    embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    embed.add_field(name="Voice Channel", value=f"*{voice_channel.name}*", inline=False)
    embed.set_footer(
        text="ELITE LEADERS COMMUNITY • Click Mark as Resolved once this is handled",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )
    embed.timestamp = discord.utils.utcnow()

    try:
        await send_with_retry(
            help_channel,
            content="@everyone A member in the voice channel needs help",
            embed=embed,
            view=help_view(member.id),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        print(f"✅ Help alert sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send help alert: {e}")

    if not config.get("send_member_panel", True):
        # Report form turned off for this channel — staff alert only.
        return

    member_panel_channel = member.guild.get_channel(MEMBER_HELP_PANEL_CHANNEL_ID)
    if member_panel_channel is None:
        print("⚠️ Couldn't find the member panel channel — check MEMBER_HELP_PANEL_CHANNEL_ID")
        return

    member_embed = discord.Embed(
        title="Support Request Received",
        description="A staff member will be with you shortly. Thanks for your patience.",
        color=discord.Color.blurple(),
    )
    member_embed.set_thumbnail(url=member.display_avatar.url)
    member_embed.add_field(name="Status", value="🟡 Waiting for staff", inline=True)
    member_embed.add_field(name="Estimated Wait", value="~1-5 minutes", inline=True)
    member_embed.add_field(name="Tip", value="Use the button below to describe your issue so staff can help faster", inline=False)
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
        help_panels[member.id] = {"message": panel_message, "described": False}
    except Exception as e:
        print(f"❌ Failed to send member panel: {e}")


async def update_help_panel_status(member_id: int, status: str, color: discord.Color):
    """Edits a member's help panel Status field and disables its buttons, used when
    they leave the voice channel or get moved elsewhere. The panel itself is NOT deleted."""
    entry = help_panels.pop(member_id, None)
    if entry is None:
        return

    message = entry["message"]
    try:
        embed = message.embeds[0].copy()
        embed.color = color
        embed.set_field_at(0, name="Status", value=status, inline=True)
        await message.edit(embed=embed, view=panel_view(member_id, disabled=True))
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        print(f"⚠️ Failed to update help panel status: {e}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Routes to the verification alert, the help alert, or the silent REPORT-VC emoji
    toggle depending on which waiting channel was joined/left."""
    if member.guild.id != GUILD_ID:
        return

    if member.bot:
        return  # bots (including this one) never trigger verification, help, or report alerts

    joined_id = after.channel.id if after.channel else None
    came_from_id = before.channel.id if before.channel else None

    if joined_id == came_from_id:
        return  # no actual channel change (e.g. mute/deafen toggle)

    # If this member has an open "waiting for help" panel and they just left the help
    # channel, reflect what happened: fully disconnecting counts as giving up (red),
    # while landing in a different channel counts as staff having moved them (green).
    if member.id in help_panels and came_from_id in HELP_VC_CONFIG:
        if joined_id is None:
            await update_help_panel_status(member.id, "🔴 Left the queue", discord.Color.red())
        else:
            await update_help_panel_status(member.id, "🟢 Resolved — moved by staff", discord.Color.green())

    if joined_id == WAITING_VC_ID:
        await send_verification_alert(member, after.channel)
    elif joined_id in HELP_VC_CONFIG:
        await send_help_alert(member, after.channel, HELP_VC_CONFIG[joined_id])
    elif joined_id in REPORT_VC_IDS and isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)):
        await set_report_vc_alert(member, True)

    # Only clear the REPORT-VC emoji if they *didn't* move straight into another REPORT
    # channel — otherwise this would immediately undo the "add emoji" call right above,
    # leaving the member with no emoji even though they're still waiting in a REPORT VC.
    if (
        came_from_id in REPORT_VC_IDS
        and joined_id not in REPORT_VC_IDS
        and isinstance(before.channel, (discord.VoiceChannel, discord.StageChannel))
    ):
        await set_report_vc_alert(member, False)


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
