const { ChannelType } = require('discord.js');
const storage = require('./storage');
const { randomEmoji } = require('./emojiPalette');
const { applyEmojiToMember, removeEmojiFromMember, stripEmojiPrefixes } = require('./nickname');
const { buildPanelEmbed, buildPanelComponents, buildPanelAttachments } = require('./panelView');
const { buildGamePanelEmbed, buildGamePanelComponents } = require('./gamePanelView');
const { refreshDashboard } = require('./dashboard');

const pendingDeletions = new Set(); // channelIds with a delete check already queued

// Emoji used on every game channel's name — fixed, not per-user, since game
// channels aren't tied to a synced nickname emoji the way regular temp
// channels are.
const GAME_CHANNEL_EMOJI = '🎮';

// Fixed (non-temp) voice channels that should still sync their emoji onto
// anyone sitting in them, same as temp channels do — just for these two
// specific static channels rather than every generated temp channel.
const STATIC_EMOJI_SYNC_CHANNEL_IDS = new Set([
  '1517941337700176003',
  '1543001094781665370',
  '1513904253423587451',
  '1519068432316760286',
  '1543346189276160241',
]);

// Grabs whatever emoji the channel's own name starts with, so renaming the
// channel automatically changes what gets applied — no separate config to
// keep in sync. No trailing-space requirement here (channel names often
// don't have one), unlike the nickname-prefix matcher in nickname.js.
const LEADING_EMOJI_RE = /^\p{Extended_Pictographic}\uFE0F?/u;
function getChannelLeadingEmoji(channel) {
  const match = channel?.name?.match(LEADING_EMOJI_RE);
  return match ? match[0] : null;
}

// Discord's channel-name validation rejects a few things that easily slip
// into a name built from someone's raw display name: repeated whitespace,
// leading/trailing whitespace, and it enforces a 100-character cap. This
// keeps the generated name inside those rules instead of finding out via a
// failed API call.
function sanitizeChannelName(name) {
  return name
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 100);
}

// Converts A-Z/a-z into Mathematical Bold Italic Sans-Serif unicode
// characters (𝘁𝗵𝗶𝘀 𝘁𝗲𝘅𝘁 𝘀𝘁𝘆𝗹𝗲). Numbers, spaces, and symbols (★, emoji)
// are left untouched since that unicode block has no digit/symbol variants.
function toStyledText(text) {
  const upperBase = 0x1d468; // bold italic sans A
  const lowerBase = 0x1d482; // bold italic sans a
  return text
    .split('')
    .map((ch) => {
      const code = ch.charCodeAt(0);
      if (code >= 65 && code <= 90) return String.fromCodePoint(upperBase + (code - 65));
      if (code >= 97 && code <= 122) return String.fromCodePoint(lowerBase + (code - 97));
      return ch;
    })
    .join('');
}

// Extra permissions the channel owner gets on their own channel, on top of
// whatever the panel buttons already let them do — mainly so they can also
// use Discord's own right-click menu to move/mute/deafen people in it, and
// so locking the channel can never lock the owner out of their own channel.
//
// ViewChannel/SendMessages/ReadMessageHistory/Speak are explicit here (not
// just Connect) because the bot moves the owner into the channel directly
// via member.voice.setChannel(), which bypasses permission checks — without
// these, an owner could be sitting in a channel they don't actually have
// permission to see or type in, especially once the base overwrites below
// lock the channel down to the Verified role.
const OWNER_CHANNEL_PERMISSIONS = {
  ViewChannel: true,
  Connect: true,
  Speak: true,
  SendMessages: true,
  ReadMessageHistory: true,
};

async function updateOwnerPermissions(channel, oldOwnerId, newOwnerId) {
  try {
    if (oldOwnerId && oldOwnerId !== newOwnerId) {
      await channel.permissionOverwrites.delete(oldOwnerId).catch(() => {});
    }
    if (newOwnerId) {
      await channel.permissionOverwrites.edit(newOwnerId, OWNER_CHANNEL_PERMISSIONS);
    }
  } catch (err) {
    console.warn(`[permissions] could not update owner overwrite: ${err.message}`);
  }
}

// Discord does NOT automatically copy a category's permission overwrites
// onto a new channel created under it via the API (that only happens when
// someone manually clicks "Sync Permissions" in the client, or calls
// channel.lockPermissions() like below) — so without this, every temp/game
// channel is created wide open (or, if the guild's @everyone role itself
// doesn't have ViewChannel, wide CLOSED) regardless of how the category
// is actually configured for regular members. This copies whatever the
// category currently allows straight onto the new channel, right after
// creation, so a normal member sees the exact same thing in the new temp
// channel that they already see in the category around it — no role IDs
// needed, and it stays correct even if the category's permissions change
// later.
async function syncToCategoryPermissions(channel) {
  try {
    await channel.lockPermissions();
  } catch (err) {
    console.warn(`[permissions] could not sync ${channel.name} to its category's permissions: ${err.message}`);
  }
}

// Optional: a specific role that should be locked out of every temp/game
// channel entirely — view, connect, and send all denied — regardless of
// what the category or any other role allows. Set config.blockedRoleId
// (same place as categoryId/gameCategoryId) to turn this on; until then it
// quietly does nothing. This is applied AFTER syncToCategoryPermissions and
// BEFORE the owner's own overwrite, so a role-wide deny here can still
// never lock the channel's actual owner out of their own channel.
async function denyBlockedRole(channel, config) {
  if (!config.blockedRoleId) return;
  try {
    await channel.permissionOverwrites.edit(config.blockedRoleId, {
      ViewChannel: false,
      Connect: false,
      SendMessages: false,
    });
  } catch (err) {
    console.warn(`[permissions] could not apply blockedRoleId overwrite on ${channel.name}: ${err.message}`);
  }
}

// Saves the channel's current name/limit/locked/trusted state under its
// owner, so the next channel that owner creates can start off the same way.
// Called right before a temp channel is torn down, wherever that happens.
// Game channels are session-based (the game picked has no reason to carry
// over to the next one) so they're skipped here entirely.
function snapshotOwnerSettings(tempData) {
  if (!tempData || !tempData.ownerId) return;
  if (tempData.type === 'game') return;
  storage.setUserSettings(tempData.ownerId, {
    customName: tempData.customName || null,
    limit: tempData.limit || 0,
    locked: !!tempData.locked,
    trusted: tempData.trusted || [],
    cleanupIntervalMinutes:
      typeof tempData.cleanupIntervalMinutes === 'number' ? tempData.cleanupIntervalMinutes : 10,
  });
}

// The one place a temp channel actually gets deleted — snapshots the
// owner's settings first, then clears the live record, then removes the
// Discord channel itself. Shared by regular temp channels and game
// channels alike.
async function destroyTempChannel(guild, channel, channelId, tempData) {
  snapshotOwnerSettings(tempData);
  storage.deleteTempChannel(channelId);
  if (channel) {
    await channel.delete().catch(() => {});
  }
  await refreshDashboard(guild).catch(() => {});
}

// Re-renders the panel embed/buttons in place after a setting changes
// (lock state, limit, emoji, owner, etc.) so the status lines shown to the
// owner never go stale. Safe to call even if the panel message was somehow
// deleted — it just quietly does nothing. Regular temp-channel panel only;
// game channels are refreshed inline via interaction.update() instead.
async function refreshPanelMessage(channel, tempData) {
  if (!tempData || !tempData.panelMessageId) return;
  try {
    const ownerMember = await channel.guild.members.fetch(tempData.ownerId).catch(() => null);
    let message = await channel.messages.fetch(tempData.panelMessageId).catch(() => null);
    if (!message) {
      // Self-healing: the panel message is gone (deleted by accident, wiped
      // by a bug, whatever) — repost a fresh one instead of leaving the
      // channel without any controls at all.
      console.warn(`[tempvc] panel message missing in ${channel.name} — reposting a new one`);
      try {
        message = await channel.send({
          embeds: [buildPanelEmbed(ownerMember, tempData)],
          components: buildPanelComponents(),
          files: buildPanelAttachments(),
        });
        tempData.panelMessageId = message.id;
        storage.setTempChannel(channel.id, tempData);
      } catch (err) {
        console.warn(`[tempvc] could not repost missing panel in ${channel.name}: ${err.message}`);
      }
      return;
    }
    await message.edit({
      embeds: [buildPanelEmbed(ownerMember, tempData)],
      components: buildPanelComponents(),
      files: buildPanelAttachments(),
    });
  } catch (err) {
    console.warn(`[tempvc] could not refresh panel message: ${err.message}`);
  }
}

async function createTempChannel(member, guild, config) {
  const emoji = storage.getUserEmoji(member.id) || randomEmoji();
  const saved = storage.getUserSettings(member.id);
  // member.displayName can still have a stuck emoji prefix on it if an
  // earlier nickname edit failed (e.g. the bot's role sits below this
  // member's role, so Discord silently rejected the rename). Stripping it
  // here too — not just in nickname.js — is what stops that leftover emoji
  // from also leaking into the new channel's name and showing up doubled.
  const cleanDisplayName = stripEmojiPrefixes(member.displayName);
  const baseName = (saved && saved.customName) || `${cleanDisplayName}'s Channel`;
  const channelName = sanitizeChannelName(`${emoji} ${baseName}`);

  let channel;
  try {
    channel = await guild.channels.create({
      name: channelName,
      type: ChannelType.GuildVoice,
      parent: config.categoryId || null,
      userLimit: (saved && saved.limit) || 0,
    });
  } catch (err) {
    console.warn(`[tempvc] rejected name "${channelName}" (${err.message}) — retrying with a plain fallback name`);
    try {
      channel = await guild.channels.create({
        name: `${emoji} Channel`.slice(0, 100),
        type: ChannelType.GuildVoice,
        parent: config.categoryId || null,
        userLimit: (saved && saved.limit) || 0,
      });
    } catch (err2) {
      console.error(`[tempvc] could not create a temp channel for ${member.user.tag} even with a fallback name: ${err2.message}`);
      return;
    }
  }

  const tempDataRecord = {
    guildId: guild.id,
    ownerId: member.id,
    emoji,
    customName: (saved && saved.customName) || null,
    limit: (saved && saved.limit) || 0,
    locked: !!(saved && saved.locked),
    trusted: (saved && saved.trusted) || [],
    // Defaults to 10 minutes unless the owner has changed it before and it
    // got carried over via their saved profile.
    cleanupIntervalMinutes:
      saved && typeof saved.cleanupIntervalMinutes === 'number' ? saved.cleanupIntervalMinutes : 10,
    lastPurgeAt: Date.now(),
    createdAt: Date.now(),
  };
  storage.setTempChannel(channel.id, tempDataRecord);

  // Order matters: sync to the category first (so this channel behaves
  // like any other channel a normal member already sees in there), then
  // force ViewChannel on for everyone regardless of what the category
  // said, then apply the blocked-role deny (if configured) on top, then
  // finally the owner's own overwrite — a member-specific overwrite always
  // beats a role-specific one, so this order guarantees the owner is never
  // the one who ends up locked out by any of the previous steps.
  await syncToCategoryPermissions(channel);
  await channel.permissionOverwrites.edit(guild.roles.everyone, { ViewChannel: true }).catch((err) => {
    console.warn(`[permissions] could not force ViewChannel on ${channel.name}: ${err.message}`);
  });
  await denyBlockedRole(channel, config);
  await updateOwnerPermissions(channel, null, member.id);

  // Restore the locked state and re-grant anyone who was trusted before —
  // do this before anyone (including the owner) actually joins.
  if (saved && saved.locked) {
    await channel.permissionOverwrites.edit(guild.roles.everyone, { Connect: false }).catch(() => {});
  }
  if (saved && saved.trusted && saved.trusted.length) {
    // Grant every trusted user's permission at once instead of waiting on
    // each one sequentially — meaningfully faster for anyone with a long
    // trusted list, with no downside since they don't depend on each other.
    await Promise.all(
      saved.trusted.map((userId) =>
        channel.permissionOverwrites.edit(userId, { Connect: true }).catch(() => {})
      )
    );
  }

  // Post the control panel right in this channel's own chat, so it's there
  // the moment anyone opens it — no need to go find a shared panel channel.
  // The message id gets saved so later setting changes (lock, limit, emoji,
  // transfer) can refresh this same message's status lines in place.
  try {
    const panelMessage = await channel.send({
      embeds: [buildPanelEmbed(member, tempDataRecord)],
      components: buildPanelComponents(),
      files: buildPanelAttachments(),
    });
    tempDataRecord.panelMessageId = panelMessage.id;
    storage.setTempChannel(channel.id, tempDataRecord);
  } catch (err) {
    console.warn(`[tempvc] could not post the panel in ${channel.name}: ${err.message}`);
  }

  try {
    await member.voice.setChannel(channel);
  } catch (err) {
    console.warn(`[tempvc] could not move ${member.user.tag} into their new channel: ${err.message}`);
    await channel.delete().catch(() => {});
    storage.deleteTempChannel(channel.id);
  }

  await refreshDashboard(guild).catch(() => {});
}

// Same shape as createTempChannel, but for the game join-to-create channel:
// no per-user emoji/name/limit restore, no nickname sync — just a fresh
// "🎮 Game" room (styled) with the game-picker panel posted in it.
async function createGameChannel(member, guild, config) {
  const channelName = sanitizeChannelName(`${GAME_CHANNEL_EMOJI} ${toStyledText('Game')}`);

  let channel;
  try {
    channel = await guild.channels.create({
      name: channelName,
      type: ChannelType.GuildVoice,
      parent: config.gameCategoryId || null,
    });
  } catch (err) {
    console.error(`[gamevc] could not create a game channel for ${member.user.tag}: ${err.message}`);
    return;
  }

  const tempDataRecord = {
    guildId: guild.id,
    ownerId: member.id,
    type: 'game',
    emoji: GAME_CHANNEL_EMOJI,
    game: null,
    gameEmoji: null,
    createdAt: Date.now(),
  };
  storage.setTempChannel(channel.id, tempDataRecord);

  // Same ordering as createTempChannel above: category sync, then force
  // everyone able to see the channel regardless of what the category
  // allows (game channels should be visible to any member, not gated),
  // then the blocked-role deny, then the owner's own overwrite last so it
  // always wins.
  await syncToCategoryPermissions(channel);
  await channel.permissionOverwrites.edit(guild.roles.everyone, { ViewChannel: true }).catch((err) => {
    console.warn(`[permissions] could not force ViewChannel on ${channel.name}: ${err.message}`);
  });
  await denyBlockedRole(channel, config);
  await updateOwnerPermissions(channel, null, member.id);

  try {
    const panelMessage = await channel.send({
      content: `<@${member.id}>`,
      embeds: [buildGamePanelEmbed(member, tempDataRecord)],
      components: buildGamePanelComponents(tempDataRecord),
    });
    tempDataRecord.panelMessageId = panelMessage.id;
    storage.setTempChannel(channel.id, tempDataRecord);
  } catch (err) {
    console.warn(`[gamevc] could not post the game panel in ${channel.name}: ${err.message}`);
  }

  try {
    await member.voice.setChannel(channel);
  } catch (err) {
    console.warn(`[gamevc] could not move ${member.user.tag} into their new game channel: ${err.message}`);
    await channel.delete().catch(() => {});
    storage.deleteTempChannel(channel.id);
    return;
  }

  await refreshDashboard(guild).catch(() => {});
}

// Renames a game channel to match the picked game, optionally moves it into
// that game's own category, and records both in storage. Used both for the
// fixed game-list buttons and the "Other" modal (which has no categoryId).
//
// Returns true if the rename actually went through, false if Discord
// rejected it (most commonly its "channel name can only change twice per
// 10 minutes" limit) — the caller uses this to warn the owner that their
// pick was saved even though the visible channel name hasn't caught up yet.
async function setChannelGame(channel, tempData, channelId, gameName, emoji, categoryId) {
  // Channel NAMES can only contain plain Unicode characters — Discord
  // silently can't render a custom emoji there (whether it's one of your
  // server's or one uploaded to the bot via the Developer Portal), and what
  // you get instead is that raw numeric snowflake showing up in the name.
  // So instead of any emoji, the name is styled using the bold-italic-sans
  // unicode set (𝘁𝗵𝗶𝘀 𝘁𝗲𝘅𝘁 𝘀𝘁𝘆𝗹𝗲) — the game's own emoji only shows on
  // the button and in the embed text, both of which render custom emoji
  // just fine. (Any lock/mic icon you see to the left of a voice channel's
  // name in Discord's own UI is the client showing that the channel's
  // permissions are restricted — it's not part of the channel name string
  // at all, so there's nothing to set here for it.)
  const finalName = sanitizeChannelName(toStyledText(gameName));
  let renamed = true;
  try {
    await channel.setName(finalName);
  } catch (err) {
    renamed = false;
    console.warn(`[gamevc] could not rename channel to "${finalName}": ${err.message}`);
  }

  if (categoryId && channel.parentId !== categoryId) {
    try {
      await channel.setParent(categoryId, { lockPermissions: false });
    } catch (err) {
      console.warn(`[gamevc] could not move channel to category ${categoryId}: ${err.message}`);
    }
  }

  tempData.game = gameName;
  tempData.gameEmoji = emoji;
  tempData.gameSetAt = Date.now();
  storage.setTempChannel(channelId, tempData);
  return renamed;
}

async function onJoinTracked(member, tempData) {
  if (tempData.type === 'game') return; // game channels don't sync a nickname emoji
  await applyEmojiToMember(member, tempData.emoji);
}

async function onLeaveTracked(member, channelId, guild, tempData) {
  if (!tempData || tempData.type !== 'game') {
    await removeEmojiFromMember(member);
  }
  scheduleEmptyCheck(channelId, guild);
}

// Checks (and deletes) an empty channel as soon as the current event-loop
// tick clears, instead of waiting on a fixed timer. That still lets any
// voice state update that's already in flight (e.g. someone else moving
// into this same channel right as the last person leaves) get applied
// first, so an about-to-be-occupied channel doesn't get deleted out from
// under them — it just doesn't add unnecessary extra delay on top of that.
function scheduleEmptyCheck(channelId, guild) {
  if (pendingDeletions.has(channelId)) return;
  pendingDeletions.add(channelId);
  setImmediate(async () => {
    pendingDeletions.delete(channelId);
    const tempData = storage.getTempChannel(channelId);
    const channel = guild.channels.cache.get(channelId);
    if (!channel) {
      snapshotOwnerSettings(tempData);
      storage.deleteTempChannel(channelId);
      await refreshDashboard(guild).catch(() => {});
      return;
    }
    if (channel.members.size === 0) {
      await destroyTempChannel(guild, channel, channelId, tempData);
    }
  });
}

async function handleVoiceStateUpdate(oldState, newState) {
  const guild = newState.guild || oldState.guild;
  const member = newState.member || oldState.member;
  if (!member || member.user.bot) return;

  const oldChannelId = oldState.channelId;
  const newChannelId = newState.channelId;
  if (oldChannelId === newChannelId) return;

  const joinedStaticChannel = newChannelId && STATIC_EMOJI_SYNC_CHANNEL_IDS.has(newChannelId);
  const leftStaticChannel = oldChannelId && STATIC_EMOJI_SYNC_CHANNEL_IDS.has(oldChannelId);

  const config = storage.getGuildConfig(guild.id);
  const oldTempData = oldChannelId ? storage.getTempChannel(oldChannelId) : null;

  // Always resolve any "leaving" cleanup FIRST — whether that's a temp
  // channel's own emoji tracking or a static synced channel — before
  // applying whatever emoji the destination calls for. Doing this in the
  // opposite order let a temp channel's leave-cleanup strip an emoji that
  // had just been applied a moment earlier by joining a static channel.
  if (oldTempData) {
    await onLeaveTracked(member, oldChannelId, guild, oldTempData);
  } else if (leftStaticChannel && !joinedStaticChannel) {
    await removeEmojiFromMember(member);
  }

  if (joinedStaticChannel) {
    const channel = guild.channels.cache.get(newChannelId);
    const emoji = getChannelLeadingEmoji(channel);
    if (emoji) await applyEmojiToMember(member, emoji);
  }

  if (!config) return;

  if (newChannelId === config.joinToCreateId) {
    await createTempChannel(member, guild, config);
    return;
  }

  if (newChannelId === config.gameJoinToCreateId) {
    await createGameChannel(member, guild, config);
    return;
  }

  if (newChannelId) {
    const newTempData = storage.getTempChannel(newChannelId);
    if (newTempData) {
      await onJoinTracked(member, newTempData);
    }
  }
}

// Deletes any tracked channel that's empty right now. Used on a timer and at
// startup so the bot cleans up properly even after a restart or brief outage.
async function sweepEmptyChannels(client) {
  const all = storage.getAllTempChannels();
  for (const channelId of Object.keys(all)) {
    const data = all[channelId];
    const guild = client.guilds.cache.get(data.guildId);
    if (!guild) continue;
    const channel = guild.channels.cache.get(channelId);
    if (!channel) {
      snapshotOwnerSettings(data);
      storage.deleteTempChannel(channelId);
      await refreshDashboard(guild).catch(() => {});
      continue;
    }
    if (channel.members.size === 0) {
      await destroyTempChannel(guild, channel, channelId, data);
    }
  }
}

async function reconcileOnStartup(client) {
  await sweepEmptyChannels(client);
  startPeriodicCleanup(client);
}

// Wipes every message in a temp channel's text chat except the panel itself,
// so the chat doesn't fill up with clutter over time.
async function purgeChannelMessages(channel, tempData) {
  // Without a known panel message id, we can't safely tell the panel apart
  // from anything else — skip this channel rather than risk deleting it.
  // This only affects channels created before panelMessageId existed; any
  // channel created from now on will always have one.
  if (!tempData || !tempData.panelMessageId) {
    console.warn(`[cleanup] skipping ${channel.name} — no known panel message id`);
    return;
  }
  try {
    // fetch({ limit: 100 }) only ever returns one page — a channel with more
    // than 100 messages needs repeated passes to actually get emptied out.
    // Loop until a fetch comes back with nothing left to delete.
    let totalDeleted = 0;
    while (true) {
      const messages = await channel.messages.fetch({ limit: 100 });
      const toDelete = messages.filter((m) => m.id !== tempData.panelMessageId);
      if (toDelete.size === 0) break;

      if (toDelete.size === 1) {
        await toDelete.first().delete().catch(() => {});
        totalDeleted += 1;
      } else {
        // Discord's bulk delete refuses messages older than 14 days; passing
        // `true` here tells discord.js to silently skip those instead of
        // throwing and aborting the whole batch.
        const deleted = await channel.bulkDelete(toDelete, true).catch((err) => {
          console.warn(`[cleanup] bulkDelete failed in ${channel.name}: ${err.message}`);
          return null;
        });
        if (!deleted) break; // avoid looping forever on a repeated failure
        totalDeleted += deleted.size;
        // bulkDelete silently skips messages older than 14 days rather than
        // deleting them — if none of this batch was actually removable,
        // stop instead of re-fetching the same stuck messages forever.
        if (deleted.size === 0) break;
      }

      // If this fetch returned fewer than the full page, there's nothing
      // more to page through.
      if (messages.size < 100) break;
    }
    if (totalDeleted > 0) {
      console.log(`[cleanup] deleted ${totalDeleted} message(s) in ${channel.name}`);
    }
  } catch (err) {
    console.warn(`[cleanup] could not purge messages in ${channel.name}: ${err.message}`);
  }
}

async function purgeAllTempChannels(client) {
  const all = storage.getAllTempChannels();
  const now = Date.now();
  for (const channelId of Object.keys(all)) {
    const data = all[channelId];
    // 0 (or missing, for very old records, or for game channels which never
    // set this) means auto-delete is off for this channel — skip it
    // entirely rather than defaulting it on.
    const intervalMinutes = data.cleanupIntervalMinutes;
    if (!intervalMinutes) continue;

    const dueAt = (data.lastPurgeAt || data.createdAt || 0) + intervalMinutes * 60 * 1000;
    if (now < dueAt) continue;

    const guild = client.guilds.cache.get(data.guildId);
    if (!guild) continue;
    const channel = guild.channels.cache.get(channelId);
    if (!channel) continue;

    await purgeChannelMessages(channel, data);
    data.lastPurgeAt = now;
    storage.setTempChannel(channelId, data);
    await refreshPanelMessage(channel, data); // updates the countdown to the next run
  }
}

// Checked every minute rather than run on a single fixed timer, so each
// channel's own interval (5 min, 30 min, 1 hour, etc.) is respected
// independently instead of everyone sharing one global schedule.
const CLEANUP_CHECK_INTERVAL_MS = 60 * 1000;
let cleanupIntervalStarted = false;

function startPeriodicCleanup(client) {
  if (cleanupIntervalStarted) return; // guard against double-registration on reconnect
  cleanupIntervalStarted = true;
  setInterval(() => {
    purgeAllTempChannels(client).catch((err) => console.warn(`[cleanup] sweep failed: ${err.message}`));
  }, CLEANUP_CHECK_INTERVAL_MS);
  console.log('[cleanup] Periodic message cleanup started — checking every minute against each channel\'s own timer.');
}

module.exports = {
  handleVoiceStateUpdate,
  sweepEmptyChannels,
  reconcileOnStartup,
  updateOwnerPermissions,
  destroyTempChannel,
  refreshPanelMessage,
  snapshotOwnerSettings,
  setChannelGame,
  toStyledText,
};

// ============================================================================
// BOT BOOTSTRAP — this is what actually starts the bot.
// Everything above only DEFINES functions. Nothing ran the bot until now,
// which is why the process was exiting immediately with no login.
//
// This section requires interactionHandler.js and gameInteractionHandler.js,
// which in turn require this same file (to call setChannelGame,
// refreshPanelMessage, etc.) — that circular require is intentional and
// safe ONLY because this code sits below module.exports above: by the time
// this line runs, this file's exports are already fully set, so the other
// two files get the real functions instead of an empty object.
// ============================================================================

const { Client, GatewayIntentBits } = require('discord.js');
const { handleInteraction } = require('./interactionHandler');
const { handleGameInteraction } = require('./gameInteractionHandler');

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildVoiceStates,
    GatewayIntentBits.GuildMembers,
  ],
});

client.once('ready', async () => {
  console.log(`✅ Logged in as ${client.user.tag}`);

  // Seed the game "join to create" channel every startup — storage resets
  // on redeploy, so this can't rely on being set once and staying set.
  // GUILD_ID comes from the environment variable already configured on
  // Railway.
  if (process.env.GUILD_ID) {
    storage.setGuildConfig(process.env.GUILD_ID, {
      gameJoinToCreateId: '1553121517879951480',
      gameCategoryId: '1513904233471283252',
      dashboardChannelId: '1553107162929037442',
      gameAnnounceChannelId: '1513904271337197741',
    });
    console.log('[startup] game join-to-create channel and category configured.');
  } else {
    console.warn('[startup] GUILD_ID env var is missing — game channel creation will not trigger.');
  }

  await reconcileOnStartup(client);
});

client.on('voiceStateUpdate', (oldState, newState) => {
  handleVoiceStateUpdate(oldState, newState).catch((err) =>
    console.error('[voiceStateUpdate] unhandled error:', err)
  );
});

client.on('interactionCreate', async (interaction) => {
  try {
    await handleInteraction(interaction);
    await handleGameInteraction(interaction);
  } catch (err) {
    console.error('[interactionCreate] unhandled error:', err);
  }
});

client.login(process.env.DISCORD_TOKEN);
