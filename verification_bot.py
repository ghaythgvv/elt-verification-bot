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

Requirements:
    pip install discord.py

Before running:
    - Enable SERVER MEMBERS INTENT for your bot in the Discord Developer Portal.
    - Put your token in place of "PUT_YOUR_BOT_TOKEN_HERE" below (from Developer Portal > your bot > Bot > Token).
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
MEMBER_HELP_PANEL_CHANNEL_ID = 1517941411151085691  # channel where the member-facing panel gets posted
ISSUE_REPORTS_CHANNEL_ID = 1553196616146493460  # channel where "Describe Issue" submissions get posted

# Per-VC settings for the "waiting for help" flow (staff alert + optional member panel):
#   - emoji: shown in the staff alert embed title, so you can tell at a glance which
#     channel triggered it
#   - send_member_panel: whether the member-facing "Describe Issue" report form panel
#     gets sent for this channel (False = staff alert only, no report form)
HELP_VC_CONFIG = {
    1517941411151085691: {"emoji": "🆘", "send_member_panel": True},
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
intents.members = True
intents.voice_states = True  # needed to detect members joining the voice channel
intents.message_content = True  # ensure message content intent is enabled
intents.invites = True  # needed to track invites

bot = commands.Bot(command_prefix="!", intents=intents)

# Invite tracking
# invite_cache[guild_id][code] = {"uses": int, "max_uses": int, "inviter": discord.User}
invite_cache = {}
member_inviters = {}  # {member_id: inviter_user_object}

# report_vc_base_nicknames[member_id] = that member's nickname (or None, meaning "no
# nickname set") with REPORT_VC_EMOJI stripped off, so we can restore it exactly once
# they leave the REPORT voice channel.
report_vc_base_nicknames = {}


async def cache_invites(guild: discord.Guild):
    """Cache all invites for a guild - their use counts, use limits, and inviters."""
    try:
        invites = await guild.invites()
        invite_cache[guild.id] = {
            invite.code: {"uses": invite.uses, "max_uses": invite.max_uses, "inviter": invite.inviter}
            for invite in invites
        }
        print(f"✅ Cached {len(invites)} invites for guild {guild.id}")
    except discord.Forbidden:
        print(f"⚠️ Bot doesn't have permission to view invites in guild {guild.id}")


async def send_with_retry(channel, **kwargs):
    """Send a message with exponential backoff retry logic for rate limits."""
    max_attempts = 5
    backoff_delays = [1, 2, 4, 8, 16]  # seconds

    for attempt in range(max_attempts):
        try:
            return await channel.send(**kwargs)
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limited
                if attempt < max_attempts - 1:
                    delay = backoff_delays[attempt]
                    print(f"⏳ Rate limited, retrying in {delay}s (attempt {attempt + 1}/{max_attempts})")
                    await asyncio.sleep(delay)
                else:
                    print(f"❌ Failed to send message after {max_attempts} attempts")
                    raise
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
        report_vc_base_nicknames[member.id] = nick
    return report_vc_base_nicknames[member.id]


async def set_report_vc_alert(member: discord.Member, active: bool):
    """Prefixes/un-prefixes a member's own nickname with REPORT_VC_EMOJI while they're
    waiting in a REPORT voice channel. Never sends any message - the renamed nickname
    itself is the alert."""
    base_nick = get_report_vc_base_nickname(member)
    target_nick = f"{REPORT_VC_EMOJI} {base_nick or member.name}" if active else base_nick

    if member.nick == target_nick:
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
    except discord.HTTPException as e:
        print(f"⚠️ Failed to update {member}'s nickname: {e}")


class VerifyView(discord.ui.View):
    """Verify/Reject buttons. timeout=None so they keep working even after a bot restart."""

    def __init__(self, member_id: int):
        super().__init__(timeout=None)
        self.member_id = member_id
        self.children[0].custom_id = f"verify_accept_{member_id}"
        self.children[1].custom_id = f"verify_reject_{member_id}"

    @discord.ui.button(label="✅ Verify", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer first to prevent timeout
        await interaction.response.defer()

        guild = interaction.guild
        member = guild.get_member(self.member_id)
        if member is None:
            return await interaction.followup.send("That member isn't in the server anymore (they may have left).", ephemeral=True)

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

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Verified", value=f"✅ {interaction.user.mention}", inline=False)
        embed.set_footer(text="Verified ✅")
        for item in self.children:
            item.disabled = True
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)

        if WELCOME_CHANNEL_ID:
            welcome_channel = guild.get_channel(WELCOME_CHANNEL_ID)
            if welcome_channel:
                await welcome_channel.send(f"🎉 Welcome {member.mention}, you're verified — glad to have you in the server!")

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer first to prevent timeout
        await interaction.response.defer()

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Rejected", value=f"❌ {interaction.user.mention}", inline=False)
        embed.set_footer(text="Rejected ❌")
        for item in self.children:
            item.disabled = True
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)


class ResolveNoteModal(discord.ui.Modal, title="Resolve Help Request"):
    """Popup asking what the problem was, shown when staff click Mark as Resolved."""

    note = discord.ui.TextInput(
        label="What was the problem? (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="What happened / why they needed help...",
        required=False,
        max_length=500,
    )

    def __init__(self, view: "HelpRequestView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        if self.note.value:
            embed.add_field(name="What happened", value=self.note.value, inline=False)
        embed.add_field(name="Resolved", value=f"✅ {interaction.user.mention}", inline=False)
        embed.set_footer(text="Resolved ✅")
        for item in self.view.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpRequestView(discord.ui.View):
    """Single 'Mark as Resolved' button for help alerts. Opens a popup asking what the problem was. Doesn't touch any roles."""

    def __init__(self, member_id: int):
        super().__init__(timeout=None)
        self.member_id = member_id
        self.children[0].custom_id = f"help_resolved_{member_id}"

    @discord.ui.button(label="✅ Mark as Resolved", style=discord.ButtonStyle.success)
    async def resolve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResolveNoteModal(self))


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
                description=self.issue.value,
                color=discord.Color.orange(),
            )
            embed.add_field(name="Member", value=f"<@{self.member_id}>", inline=True)
            embed.add_field(name="ID", value=f"`{self.member_id}`", inline=True)
            if member:
                embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="Submitted via the member help panel")
            embed.timestamp = discord.utils.utcnow()
            try:
                await send_with_retry(reports_channel, embed=embed)
                print(f"✅ Issue report sent for {member or self.member_id}")
            except Exception as e:
                print(f"❌ Failed to send issue report: {e}")

        # Ephemeral - only the member who submitted it sees this confirmation.
        await interaction.response.send_message(
            "✅ Thanks — your issue has been sent to staff. Someone will be with you shortly.",
            ephemeral=True,
        )


class MemberHelpPanelView(discord.ui.View):
    """Panel shown to the member themselves while they wait for staff."""

    def __init__(self, member_id: int):
        super().__init__(timeout=None)
        self.member_id = member_id
        self.children[0].custom_id = f"member_describe_{member_id}"
        self.children[1].custom_id = f"member_cancel_{member_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.member_id:
            await interaction.response.send_message("This panel isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="📝 Describe Issue", style=discord.ButtonStyle.primary)
    async def describe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DescribeIssueModal(self.member_id))

    @discord.ui.button(label="❌ Cancel Request", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.greyple()
        embed.set_field_at(0, name="Status", value="⚪ Cancelled by member", inline=True)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        await cache_invites(guild)
        # Sync REPORT VC nickname state in case people were already waiting when the bot
        # started (or it crashed mid-rename last time).
        for vc_id in REPORT_VC_IDS:
            channel = guild.get_channel(vc_id)
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                for waiting_member in channel.members:
                    await set_report_vc_alert(waiting_member, True)
            elif channel is not None:
                print(f"⚠️ REPORT_VC_IDS has {vc_id}, but that's a {type(channel).__name__}, not a voice channel — skipping it")
    else:
        print(f"❌ Guild {GUILD_ID} not found!")


@bot.event
async def on_invite_create(invite: discord.Invite):
    """Refresh invite cache when a new invite is created."""
    if invite.guild.id == GUILD_ID:
        await cache_invites(invite.guild)
        print(f"📝 Invite created: {invite.code}")


@bot.event
async def on_invite_delete(invite: discord.Invite):
    """Refresh invite cache when an invite is deleted."""
    if invite.guild.id == GUILD_ID:
        await cache_invites(invite.guild)
        print(f"🗑️ Invite deleted: {invite.code}")


@bot.event
async def on_member_join(member: discord.Member):
    """Track who invited the member by comparing invite use counts (and catching single-use
    invites, which Discord deletes the instant they're used, before this even runs)."""
    if member.guild.id != GUILD_ID:
        return

    print(f"➕ {member} joined the server")

    # Give bot a second to see the updated invites
    await asyncio.sleep(1)

    try:
        current_invites = await member.guild.invites()
        current_by_code = {invite.code: invite for invite in current_invites}
        old_cache = invite_cache.get(member.guild.id, {})

        inviter = None

        # Case 1: an invite that still exists went up in use count
        for invite in current_invites:
            old_uses = old_cache.get(invite.code, {}).get("uses", 0)
            if invite.uses > old_uses:
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
        await cache_invites(member.guild)
    except discord.Forbidden:
        print(f"⚠️ Bot doesn't have permission to view invites")


async def send_verification_alert(member: discord.Member, voice_channel: discord.VoiceChannel):
    """Posts the 'awaiting verification' embed with Verify/Reject buttons."""
    # Skip if already verified
    verified_role = member.guild.get_role(VERIFIED_ROLE_ID)
    if verified_role and verified_role in member.roles:
        return

    verification_channel = member.guild.get_channel(VERIFICATION_CHANNEL_ID)
    if verification_channel is None:
        print("⚠️ Couldn't find the verification channel — check VERIFICATION_CHANNEL_ID")
        return

    print(f"📤 Sending verification message for {member}")

    # Build description with invite info if available
    inviter = member_inviters.get(member.id)
    invited_by_text = f"Invited by: {inviter.mention}" if inviter else "Invited by: Unknown (vanity URL or unknown invite)"

    embed = discord.Embed(
        title="Member awaiting verification",
        description=(
            f"Member: {member.mention}\n"
            f"ID: `{member.id}`\n"
            f"Account created: {discord.utils.format_dt(member.created_at, 'R')}\n"
            f"Joined server: {discord.utils.format_dt(member.joined_at, 'R')}\n"
            f"{invited_by_text}\n"
            f"Joined voice channel: *{voice_channel.name}*"
        ),
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Click Verify to let this member in")

    view = VerifyView(member.id)

    try:
        await send_with_retry(
            verification_channel,
            content="@everyone A member in the voice channel needs verification",
            embed=embed,
            view=view,
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

    emoji = config.get("emoji", "🆘")
    print(f"📤 Sending help alert for {member} ({emoji})")

    embed = discord.Embed(
        title=f"{emoji} Member needs help",
        description=(
            f"Member: {member.mention}\n"
            f"ID: `{member.id}`\n"
            f"Account created: {discord.utils.format_dt(member.created_at, 'R')}\n"
            f"Joined voice channel: *{voice_channel.name}*"
        ),
        color=discord.Color.gold(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Click Mark as Resolved once this is handled")

    view = HelpRequestView(member.id)

    try:
        await send_with_retry(
            help_channel,
            content="@everyone A member in the voice channel needs help",
            embed=embed,
            view=view,
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
        title="🆘 Support Request Received",
        description="A staff member will be with you shortly. Thanks for your patience.",
        color=discord.Color.blurple(),
    )
    member_embed.set_thumbnail(url=member.display_avatar.url)
    member_embed.add_field(name="Status", value="🟡 Waiting for staff", inline=True)
    member_embed.add_field(name="Estimated Wait", value="~3-5 minutes", inline=True)
    member_embed.add_field(name="Tip", value="Use the button below to describe your issue so staff can help faster", inline=False)
    member_embed.set_footer(
        text="ELITE LEADERS COMMUNITY • Support System",
        icon_url=member.guild.icon.url if member.guild.icon else None,
    )
    member_embed.timestamp = discord.utils.utcnow()

    member_view = MemberHelpPanelView(member.id)

    try:
        panel_message = await send_with_retry(
            member_panel_channel,
            content=member.mention,
            embed=member_embed,
            view=member_view,
        )
        print(f"✅ Member panel sent for {member}")
    except Exception as e:
        print(f"❌ Failed to send member panel: {e}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Routes to the verification alert, the help alert, or the silent REPORT-VC emoji
    toggle depending on which waiting channel was joined/left."""
    if member.guild.id != GUILD_ID:
        return

    joined_id = after.channel.id if after.channel else None
    came_from_id = before.channel.id if before.channel else None

    if joined_id == came_from_id:
        return  # no actual channel change (e.g. mute/deafen toggle)

    if joined_id == WAITING_VC_ID:
        await send_verification_alert(member, after.channel)
    elif joined_id in HELP_VC_CONFIG:
        await send_help_alert(member, after.channel, HELP_VC_CONFIG[joined_id])
    elif joined_id in REPORT_VC_IDS and isinstance(after.channel, (discord.VoiceChannel, discord.StageChannel)):
        await set_report_vc_alert(member, True)

    if came_from_id in REPORT_VC_IDS and isinstance(before.channel, (discord.VoiceChannel, discord.StageChannel)):
        await set_report_vc_alert(member, False)


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway's Variables tab, then redeploy.")

bot.run(TOKEN)
