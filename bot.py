import os
import asyncio
import json
import re
import secrets
import string
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "PanelBot")
PASTEFY_API_TOKEN = os.getenv("PASTEFY_API_TOKEN")
PASTEFY_MAX_BYTES = 5 * 1024 * 1024

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI environment variable is missing")

mongo_client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=10000,
)

mongo_db = mongo_client[MONGODB_DATABASE]
insights_collection = mongo_db["server_insights"]
anti_scam_collection = mongo_db["anti_scam_channels"]
reaction_roles_collection = mongo_db["reaction_roles"]
member_snapshots_collection = mongo_db["member_snapshots"]

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=("!", "."), intents=intents)

created_channels = {}
reaction_role_cache = {}
ready_once = False

ANTI_SCAM_TITLE = "## 🛡️ Anti-Scam Protection"
ANTI_SCAM_BODY = (
    "This channel is reserved for suspicious or fake social-media spam.\n"
    "**Do not send messages here.** Messages are removed automatically and the sender "
    "may be kicked."
)


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def parse_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def cleanup_events_sync(document):
    cutoff = utc_now() - timedelta(days=30)
    joins = []
    leaves = []
    for event in document.get("joins", []):
        value = event.get("timestamp") if isinstance(event, dict) else event
        dt = parse_datetime(value)
        if dt and dt >= cutoff:
            joins.append(value)
    for event in document.get("leaves", []):
        value = event.get("timestamp") if isinstance(event, dict) else event
        dt = parse_datetime(value)
        if dt and dt >= cutoff:
            leaves.append(value)
    document["joins"] = joins
    document["leaves"] = leaves
    document.setdefault("total_joins", 0)
    document.setdefault("total_leaves", 0)
    if document["total_joins"] == 0 and joins:
        document["total_joins"] = len(joins)
    if document["total_leaves"] == 0 and leaves:
        document["total_leaves"] = len(leaves)
    return document


def get_guild_insights_sync(guild_id):
    guild_id = int(guild_id)
    document = insights_collection.find_one({"_id": guild_id})
    if not document:
        document = {
            "_id": guild_id,
            "joins": [],
            "leaves": [],
            "total_joins": 0,
            "total_leaves": 0,
        }
    document.setdefault("joins", [])
    document.setdefault("leaves", [])
    document.setdefault("total_joins", 0)
    document.setdefault("total_leaves", 0)
    if document["total_joins"] == 0 and document["joins"]:
        document["total_joins"] = len(document["joins"])
    if document["total_leaves"] == 0 and document["leaves"]:
        document["total_leaves"] = len(document["leaves"])
    cleanup_events_sync(document)
    insights_collection.replace_one({"_id": guild_id}, document, upsert=True)
    return document


def add_member_event_sync(guild_id, event_type):
    guild_id = int(guild_id)
    field = "joins" if event_type == "join" else "leaves"
    total_field = "total_joins" if event_type == "join" else "total_leaves"
    insights_collection.update_one(
        {"_id": guild_id},
        {
            "$setOnInsert": {
                "joins": [],
                "leaves": [],
                "total_joins": 0,
                "total_leaves": 0,
            }
        },
        upsert=True,
    )
    document = insights_collection.find_one_and_update(
        {"_id": guild_id},
        {
            "$push": {field: iso_now()},
            "$inc": {total_field: 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    cleanup_events_sync(document)
    insights_collection.replace_one({"_id": guild_id}, document, upsert=True)
    return document


def get_member_snapshot_sync(guild_id):
    document = member_snapshots_collection.find_one({"_id": int(guild_id)})
    if not document:
        return None
    return {int(member_id) for member_id in document.get("member_ids", [])}


def save_member_snapshot_sync(guild_id, member_ids):
    member_snapshots_collection.replace_one(
        {"_id": int(guild_id)},
        {
            "_id": int(guild_id),
            "member_ids": [int(member_id) for member_id in member_ids],
            "updated_at": iso_now(),
        },
        upsert=True,
    )


def add_member_to_snapshot_sync(guild_id, member_id):
    member_snapshots_collection.update_one(
        {"_id": int(guild_id)},
        {
            "$addToSet": {"member_ids": int(member_id)},
            "$set": {"updated_at": iso_now()},
        },
        upsert=True,
    )


def remove_member_from_snapshot_sync(guild_id, member_id):
    member_snapshots_collection.update_one(
        {"_id": int(guild_id)},
        {
            "$pull": {"member_ids": int(member_id)},
            "$set": {"updated_at": iso_now()},
        },
    )


def reconcile_member_snapshot_sync(guild_id, current_member_ids):
    guild_id = int(guild_id)
    current = {int(member_id) for member_id in current_member_ids}
    previous = get_member_snapshot_sync(guild_id)
    if previous is None:
        save_member_snapshot_sync(guild_id, current)
        return 0, 0, True
    joined = current - previous
    left = previous - current
    save_member_snapshot_sync(guild_id, current)
    return joined, left, False

def list_anti_scam_sync():
    return list(anti_scam_collection.find({}))


def save_anti_scam_sync(record):
    channel_id = int(record["channel_id"])
    data = dict(record)
    data["_id"] = channel_id
    anti_scam_collection.replace_one(
        {"_id": channel_id},
        data,
        upsert=True,
    )


def delete_anti_scam_sync(channel_id):
    anti_scam_collection.delete_one({"_id": int(channel_id)})


def increment_anti_scam_sync(channel_id, kicked):
    increments = {"violations": 1}
    if kicked:
        increments["kicks"] = 1
    return anti_scam_collection.find_one_and_update(
        {"_id": int(channel_id)},
        {"$inc": increments},
        return_document=ReturnDocument.AFTER,
    )


def save_reaction_role_sync(record):
    message_id = int(record["message_id"])
    data = dict(record)
    data["_id"] = message_id
    reaction_roles_collection.replace_one(
        {"_id": message_id},
        data,
        upsert=True,
    )


def get_reaction_role_sync(message_id):
    return reaction_roles_collection.find_one({"_id": int(message_id)})


def delete_reaction_role_sync(message_id):
    reaction_roles_collection.delete_one({"_id": int(message_id)})


def list_reaction_roles_sync():
    return list(reaction_roles_collection.find({}))


async def mongo_call(function, *args):
    return await asyncio.to_thread(function, *args)


def make_container(*items, accent_color=None):
    container = discord.ui.Container(*items)
    if accent_color is not None:
        container.accent_color = accent_color
    return container


def make_text(content):
    return discord.ui.TextDisplay(content)


def make_separator():
    return discord.ui.Separator(
        spacing=discord.SeparatorSpacing.small,
        visible=True,
    )


class AntiScamView(discord.ui.LayoutView):
    def __init__(self, kicks=0, violations=0):
        super().__init__(timeout=None)
        self.kicks = int(kicks)
        self.violations = int(violations)

        self.kick_button = discord.ui.Button(
            label=f"{self.kicks:,} kicks",
            style=discord.ButtonStyle.danger,
            emoji="🛡️",
            disabled=True,
        )

        self.violation_button = discord.ui.Button(
            label=f"{self.violations:,} blocked",
            style=discord.ButtonStyle.secondary,
            emoji="🚫",
            disabled=True,
        )

        status = discord.ui.Section(
            make_text("### 🟢 Protection is active"),
            make_text(
                "This channel is monitored continuously. Messages are removed and "
                "members who can be moderated are kicked automatically."
            ),
            accessory=self.kick_button,
        )

        self.add_item(
            make_container(
                make_text("## 🛡️ Anti-Scam Protection"),
                make_text("Automatic enforcement for this protected channel."),
                make_separator(),
                status,
                make_separator(),
                make_text(
                    "### Channel rules\n"
                    "• Do not send messages in this channel.\n"
                    "• Messages are removed automatically.\n"
                    "• Moderation is applied when the bot has permission.\n"
                    "• Administrators are not automatically kicked."
                ),
                make_separator(),
                discord.ui.ActionRow(self.violation_button),
                accent_color=0xED4245,
            )
        )

    def update_stats(self, kicks=None, violations=None):
        if kicks is not None:
            self.kicks = int(kicks)
        if violations is not None:
            self.violations = int(violations)
        self.kick_button.label = f"{self.kicks:,} kicks"
        self.violation_button.label = f"{self.violations:,} blocked"

    def update_kicks(self, kicks=None):
        self.update_stats(kicks=kicks)


class InsightsView(discord.ui.LayoutView):
    def __init__(self, guild, document):
        super().__init__(timeout=None)
        joins = list(document.get("joins", []))
        leaves = list(document.get("leaves", []))
        net = len(joins) - len(leaves)
        lifetime_joins = int(document.get("total_joins", 0))
        lifetime_leaves = int(document.get("total_leaves", 0))
        lifetime_net = lifetime_joins - lifetime_leaves
        status = "📈 Growing" if net > 0 else "📉 Declining" if net < 0 else "➖ Stable"
        net_text = f"+{net:,}" if net > 0 else f"{net:,}"
        lifetime_net_text = f"+{lifetime_net:,}" if lifetime_net > 0 else f"{lifetime_net:,}"

        self.add_item(
            make_container(
                make_text(f"## 📊 Server Insights · {guild.name}"),
                make_separator(),
                make_text(
                    f"**Current Members:** `{guild.member_count or 0:,}`   **Status:** {status}\n"
                    f"**30-Day Net:** `{net_text}`   **Lifetime Net:** `{lifetime_net_text}`"
                ),
                make_separator(),
                make_text(
                    "### 🗓️ Last 30 Days\n"
                    f"**Joined:** `{len(joins):,}`   **Left:** `{len(leaves):,}`   **Net:** `{net_text}`\n"
                    "Activity automatically expires after 30 days."
                ),
                make_separator(),
                make_text(
                    "### ♾️ Lifetime Totals\n"
                    f"**Total Joins:** `{lifetime_joins:,}`\n"
                    f"**Total Departures:** `{lifetime_leaves:,}`\n"
                    f"**Lifetime Net:** `{lifetime_net_text}`"
                ),
                make_separator(),
                make_text(
                    "💾 **MongoDB Persistence**\n"
                    "Member activity is stored persistently. The activity list uses a rolling 30-day window, while lifetime totals remain available."
                ),
            )
        )


class PastefyResultView(discord.ui.LayoutView):
    def __init__(self, filename, raw_url):
        super().__init__(timeout=None)
        self.add_item(
            make_container(
                make_text("## 📋 Pastefy Upload Complete"),
                make_separator(),
                make_text(
                    f"**File:** `{discord.utils.escape_markdown(filename)}`\n"
                    f"**Raw URL:** {raw_url}"
                ),
                make_separator(),
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="View Raw Result",
                        style=discord.ButtonStyle.link,
                        url=raw_url,
                    )
                ),
            )
        )


def create_pastefy_paste_sync(filename, content, token):
    payload = {
        "title": filename,
        "content": content,
        "visibility": "UNLISTED",
        "encrypted": False,
        "type": "PASTE",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    auth = token.strip()
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    request = urllib.request.Request(
        "https://pastefy.app/api/v2/paste",
        data=data,
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PanelBot/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(f"Pastefy API returned HTTP {error.code}: {detail[:500]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not connect to Pastefy: {error.reason}") from error
    try:
        result = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError("Pastefy returned an invalid response.") from error
    paste = result.get("paste") if isinstance(result, dict) else None
    raw_url = paste.get("raw_url") if isinstance(paste, dict) else None
    if not raw_url or not isinstance(raw_url, str):
        raise RuntimeError("Pastefy did not return a raw URL.")
    return raw_url


@bot.command(name="pastefy")
async def pastefy_command(ctx: commands.Context):
    attachments = list(ctx.message.attachments)
    if len(attachments) != 1:
        await ctx.send("Upload exactly one `.lua` or `.txt` file with `.pastefy`.")
        return
    attachment = attachments[0]
    filename = os.path.basename(attachment.filename or "")
    extension = os.path.splitext(filename)[1].lower()
    if extension not in {".lua", ".txt"}:
        await ctx.send("Only `.lua` and `.txt` files are supported.")
        return
    if not PASTEFY_API_TOKEN:
        await ctx.send("Pastefy is not configured. Set the `PASTEFY_API_TOKEN` environment variable.")
        return
    if attachment.size is not None and attachment.size > PASTEFY_MAX_BYTES:
        await ctx.send("That file is too large. The maximum size is 5 MB.")
        return
    status = await ctx.send("⏳ Uploading your file to Pastefy...")
    try:
        raw = await attachment.read()
        if len(raw) > PASTEFY_MAX_BYTES:
            await status.edit(content="That file is too large. The maximum size is 5 MB.")
            return
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            await status.edit(content="The uploaded file must be valid UTF-8 text.")
            return
        if not content:
            await status.edit(content="The uploaded file is empty.")
            return
        raw_url = await asyncio.to_thread(
            create_pastefy_paste_sync,
            filename,
            content,
            PASTEFY_API_TOKEN,
        )
        await status.edit(content=None, view=PastefyResultView(filename, raw_url))
    except Exception as error:
        await status.edit(content=f"Pastefy upload failed: {error}")


class PurgeView(discord.ui.LayoutView):
    def __init__(self, requested, deleted, channel):
        super().__init__(timeout=None)
        self.add_item(
            make_container(
                make_text("## 🧹 Purge Complete"),
                make_separator(),
                make_text("The cleanup finished successfully."),
                make_separator(),
                make_text(
                    f"### 📋 Results\n"
                    f"**Requested:** `{requested:,}`\n"
                    f"**Deleted:** `{deleted:,}`\n"
                    f"**Channel:** {channel.mention}"
                ),
            )
        )

class MessageIdModal(discord.ui.Modal, title="Set Target Message"):
    message_id = discord.ui.TextInput(
        label="Message ID",
        placeholder="Paste the Discord message ID here",
        required=True,
        max_length=30,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction):
        value = str(self.message_id.value).strip()
        if not value.isdigit():
            await interaction.response.send_message("❌ That is not a valid Discord message ID.", ephemeral=True)
            return
        self.parent_view.message_id = int(value)
        self.parent_view.refresh()
        await interaction.response.send_message(f"✅ Target message set to `{value}`.", ephemeral=True)


class EmojiModal(discord.ui.Modal, title="Set Reaction Emojis"):
    emoji_1 = discord.ui.TextInput(label="Emoji 1", placeholder="⭐ or <:custom:123456789>", required=True, max_length=100)
    emoji_2 = discord.ui.TextInput(label="Emoji 2", placeholder="Optional", required=False, max_length=100)
    emoji_3 = discord.ui.TextInput(label="Emoji 3", placeholder="Optional", required=False, max_length=100)
    emoji_4 = discord.ui.TextInput(label="Emoji 4", placeholder="Optional", required=False, max_length=100)
    emoji_5 = discord.ui.TextInput(label="Emoji 5", placeholder="Optional", required=False, max_length=100)

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction):
        values = [
            str(self.emoji_1.value).strip(),
            str(self.emoji_2.value).strip(),
            str(self.emoji_3.value).strip(),
            str(self.emoji_4.value).strip(),
            str(self.emoji_5.value).strip(),
        ]
        values = [value for value in values if value]
        if not values:
            await interaction.response.send_message("❌ Add at least one emoji.", ephemeral=True)
            return
        if len(values) > 5:
            await interaction.response.send_message("❌ You can use a maximum of 5 emojis.", ephemeral=True)
            return
        self.parent_view.emojis = values
        self.parent_view.refresh()
        await interaction.response.send_message(
            f"✅ {len(values)} reaction emoji{'s' if len(values) != 1 else ''} saved.",
            ephemeral=True,
        )

class RolePickerView(discord.ui.View):
    def __init__(self, parent_view):
        super().__init__(timeout=300)
        self.parent_view = parent_view

        self.select = discord.ui.RoleSelect(
            placeholder="Select up to 5 roles",
            min_values=1,
            max_values=5,
        )
        self.select.callback = self.role_selected
        self.add_item(self.select)

    async def role_selected(self, interaction):
        try:
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This selector can only be used inside a server.",
                    ephemeral=True,
                )
                return

            selected = interaction.data.get("values", []) if interaction.data else []
            resolved = []

            for role_id in selected[:5]:
                role = interaction.guild.get_role(int(role_id))
                if role is not None:
                    resolved.append(role)

            if not resolved:
                await interaction.response.send_message(
                    "No valid roles were selected.",
                    ephemeral=True,
                )
                return

            self.parent_view.roles = resolved
            self.parent_view.refresh()

            await interaction.response.send_message(
                "Selected: " + ", ".join(role.mention for role in resolved),
                ephemeral=True,
            )
        except Exception as error:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"Could not save the selected roles: {error}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"Could not save the selected roles: {error}",
                    ephemeral=True,
                )


class ReactionRoleSetupView(discord.ui.LayoutView):
    def __init__(self, author_id):
        super().__init__(timeout=900)
        self.author_id = author_id
        self.message_id = None
        self.roles = []
        self.emojis = []

        self.message_button = discord.ui.Button(label="Message", style=discord.ButtonStyle.secondary, emoji="🆔")
        self.message_button.callback = self.message_id_callback
        self.role_button = discord.ui.Button(label="Roles", style=discord.ButtonStyle.secondary, emoji="🎭")
        self.role_button.callback = self.role_callback
        self.emoji_button = discord.ui.Button(label="Emojis", style=discord.ButtonStyle.secondary, emoji="✨")
        self.emoji_button.callback = self.emoji_callback
        self.save_button = discord.ui.Button(label="Save Setup", style=discord.ButtonStyle.success, emoji="✅")
        self.save_button.callback = self.save_callback

        row = discord.ui.ActionRow()
        row.add_item(self.message_button)
        row.add_item(self.role_button)
        row.add_item(self.emoji_button)
        row.add_item(self.save_button)

        self.status = make_text(self.status_text())
        self.add_item(
            make_container(
                make_text("## 🎭 Reaction Role Setup"),
                make_separator(),
                make_text(
                    "Create a reaction-role message in a few simple steps.\n"
                    "Set the message, choose up to 5 roles, then match each role with an emoji."
                ),
                make_separator(),
                make_text("### ⚙️ Configuration"),
                self.status,
                make_separator(),
                make_text("### 🔧 Controls"),
                row,
            )
        )

    def refresh(self):
        self.status.content = self.status_text()

    def status_text(self):
        message = f"`{self.message_id}`" if self.message_id else "`Not set`"
        roles = ", ".join(role.mention for role in self.roles) if self.roles else "`Not set`"
        emojis = "  ".join(self.emojis) if self.emojis else "`Not set`"
        pair_count = min(len(self.roles), len(self.emojis))
        return (
            f"**Target Message:** {message}\n"
            f"**Roles:** {roles}\n"
            f"**Emojis:** {emojis}\n"
            f"**Pairs Ready:** `{pair_count}/5`"
        )

    async def check_author(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who started this setup can use these controls.",
                ephemeral=True,
            )
            return False
        return True

    async def message_id_callback(self, interaction):
        if not await self.check_author(interaction):
            return
        await interaction.response.send_modal(MessageIdModal(self))

    async def role_callback(self, interaction):
        if not await self.check_author(interaction):
            return
        picker = RolePickerView(self)
        await interaction.response.send_message(
            "🎭 **Select Roles**\nChoose up to 5 roles. Their order will be matched with the emoji order.",
            view=picker,
            ephemeral=True,
        )

    async def emoji_callback(self, interaction):
        if not await self.check_author(interaction):
            return
        await interaction.response.send_modal(EmojiModal(self))


class ReactionRoleMessageView(discord.ui.LayoutView):
    def __init__(self, pairs):
        super().__init__(timeout=None)
        lines = [f"`{index}`  {pair['emoji']}  **{pair['role_name']}**" for index, pair in enumerate(pairs, 1)]
        self.add_item(
            make_container(
                make_text("## 🎭 Reaction Roles"),
                make_separator(),
                make_text(
                    "React with the emoji beside a role to receive it.\n"
                    "Remove your reaction to give the role back."
                ),
                make_separator(),
                make_text("### Available Roles\n" + "\n".join(lines)),
                make_separator(),
                make_text("✨ **Automatic:** Role changes happen instantly after you react."),
            )
        )

async def find_message_in_guild(guild, message_id):
    message_id = int(message_id)

    for channel in guild.text_channels:
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        except discord.Forbidden:
            continue
        except discord.HTTPException:
            continue

    return None


async def restore_reaction_roles():
    records = await mongo_call(list_reaction_roles_sync)
    stale = []

    for record in records:
        try:
            guild_id = int(record["guild_id"])
            channel_id = int(record["channel_id"])
            message_id = int(record["message_id"])
            pairs = record["pairs"]
        except (KeyError, TypeError, ValueError):
            stale.append(record.get("_id"))
            continue

        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            stale.append(message_id)
            continue

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            stale.append(message_id)
            continue
        except discord.Forbidden:
            continue
        except discord.HTTPException:
            continue

        valid_pairs = []
        for pair in pairs:
            try:
                emoji = str(pair["emoji"])
                role_id = int(pair["role_id"])
                role = guild.get_role(role_id)
            except (KeyError, TypeError, ValueError):
                continue

            if role is None:
                continue

            valid_pairs.append(
                {
                    "emoji": emoji,
                    "role_id": role.id,
                    "role_name": role.name,
                }
            )

            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException):
                pass

        if not valid_pairs:
            stale.append(message_id)
            continue

        record["pairs"] = valid_pairs
        reaction_role_cache[message_id] = record

        try:
            await message.edit(
                view=ReactionRoleMessageView(valid_pairs)
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

    for message_id in stale:
        if message_id is not None:
            await mongo_call(delete_reaction_role_sync, message_id)


@bot.event
async def on_raw_reaction_add(payload):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return

    record = reaction_role_cache.get(payload.message_id)
    if record is None:
        record = await mongo_call(get_reaction_role_sync, payload.message_id)
        if record:
            reaction_role_cache[payload.message_id] = record

    if not record:
        return

    emoji_value = str(payload.emoji)

    for pair in record.get("pairs", []):
        if str(pair.get("emoji")) != emoji_value:
            continue

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(int(pair["role_id"]))
        member = guild.get_member(payload.user_id)

        if role is None or member is None:
            return

        if role.is_default() or role.managed:
            return

        me = guild.me
        if me is None or role >= me.top_role:
            return

        try:
            await member.add_roles(
                role,
                reason="Reaction role",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        return


@bot.event
async def on_raw_reaction_remove(payload):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return

    record = reaction_role_cache.get(payload.message_id)
    if record is None:
        record = await mongo_call(get_reaction_role_sync, payload.message_id)
        if record:
            reaction_role_cache[payload.message_id] = record

    if not record:
        return

    emoji_value = str(payload.emoji)

    for pair in record.get("pairs", []):
        if str(pair.get("emoji")) != emoji_value:
            continue

        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(int(pair["role_id"]))
        member = guild.get_member(payload.user_id)

        if role is None or member is None:
            return

        if role.is_default() or role.managed:
            return

        try:
            await member.remove_roles(
                role,
                reason="Reaction role removed",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        return


create_group = app_commands.Group(
    name="create",
    description="Create server tools",
)

anti_group = app_commands.Group(
    name="anti",
    description="Anti moderation tools",
    parent=create_group,
)

server_group = app_commands.Group(
    name="server",
    description="Server information and tools",
)

add_group = app_commands.Group(
    name="add",
    description="Add server tools",
)

update_group = app_commands.Group(
    name="update",
    description="Post update logs",
)

reaction_group = app_commands.Group(
    name="reaction",
    description="Reaction role tools",
    parent=add_group,
)


@anti_group.command(
    name="scam",
    description="Create an anti-scam protection channel",
)
@app_commands.describe(name="The exact name of the channel to create")
@app_commands.checks.has_permissions(manage_channels=True)
async def anti_scam(interaction: discord.Interaction, name: str):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    me = interaction.guild.me

    if me is None:
        await interaction.followup.send(
            "I could not verify my server permissions.",
            ephemeral=True,
        )
        return

    if not me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "I need the Manage Channels permission.",
            ephemeral=True,
        )
        return

    if not me.guild_permissions.kick_members:
        await interaction.followup.send(
            "I need the Kick Members permission.",
            ephemeral=True,
        )
        return

    clean_name = name.strip()

    if not clean_name:
        await interaction.followup.send(
            "The channel name cannot be empty.",
            ephemeral=True,
        )
        return

    try:
        channel = await interaction.guild.create_text_channel(
            clean_name,
            reason=f"Anti-scam channel created by {interaction.user}",
        )

        view = AntiScamView()

        message = await channel.send(view=view)

        try:
            await message.add_reaction("👍")
        except discord.HTTPException:
            pass

        record = {
            "channel_id": channel.id,
            "guild_id": interaction.guild.id,
            "message_id": message.id,
            "kicks": 0,
            "violations": 0,
        }

        await mongo_call(save_anti_scam_sync, record)

        created_channels[channel.id] = {
            "view": view,
            "message": message,
            "guild_id": interaction.guild.id,
        }

        await interaction.followup.send(
            f"Created {channel.mention}.",
            ephemeral=True,
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create or manage that channel.",
            ephemeral=True,
        )
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord returned an error: {error}",
            ephemeral=True,
        )
    except PyMongoError as error:
        try:
            await channel.delete(reason="MongoDB persistence failed")
        except Exception:
            pass

        await interaction.followup.send(
            f"MongoDB error while saving the channel: {error}",
            ephemeral=True,
        )
    except Exception as error:
        await interaction.followup.send(
            f"Unexpected error: {error}",
            ephemeral=True,
        )


@server_group.command(
    name="insights",
    description="View member activity from the last 30 days",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def server_insights(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        current_member_ids = []
        async for member in interaction.guild.fetch_members(limit=None):
            current_member_ids.append(member.id)

        joined, left, initialized = await mongo_call(
            reconcile_member_snapshot_sync,
            interaction.guild.id,
            current_member_ids,
        )

        if not initialized:
            for _ in joined:
                await mongo_call(
                    add_member_event_sync,
                    interaction.guild.id,
                    "join",
                )
            for _ in left:
                await mongo_call(
                    add_member_event_sync,
                    interaction.guild.id,
                    "leave",
                )

        document = await mongo_call(
            get_guild_insights_sync,
            interaction.guild.id,
        )

        view = InsightsView(
            guild=interaction.guild,
            document=document,
        )

        await interaction.followup.send(view=view)

    except PyMongoError as error:
        await interaction.followup.send(
            f"MongoDB error: {error}",
            ephemeral=True,
        )
    except Exception as error:
        await interaction.followup.send(
            f"Unexpected error: {error}",
            ephemeral=True,
        )


async def find_guild_message(guild, message_id):
    message_id = int(message_id)
    channels = []
    try:
        channels = await guild.fetch_channels()
    except (discord.Forbidden, discord.HTTPException):
        channels = list(guild.channels)

    checked = set()
    for channel in channels:
        channel_id = getattr(channel, "id", None)
        if channel_id in checked:
            continue
        checked.add(channel_id)

        if isinstance(channel, discord.TextChannel):
            try:
                return await channel.fetch_message(message_id)
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException):
                continue

        if isinstance(channel, discord.ForumChannel):
            for thread in channel.threads:
                try:
                    return await thread.fetch_message(message_id)
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    continue

    for channel in guild.text_channels:
        if channel.id in checked:
            continue
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        except (discord.Forbidden, discord.HTTPException):
            continue

    return None

@update_group.command(
    name="logs",
    description="Post an update log",
)
@app_commands.describe(
    title="Update title",
    version="Update version, such as 2.6",
    change_logs="Message ID containing the change logs",
    message="Optional small update message",
    channel="Channel where the update will be posted",
)
async def update_logs(
    interaction: discord.Interaction,
    title: str,
    version: str,
    change_logs: str,
    channel: discord.TextChannel,
    message: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to use this command.",
            ephemeral=True,
        )
        return

    me = interaction.guild.me
    if me is None:
        await interaction.response.send_message(
            "I could not verify my server permissions.",
            ephemeral=True,
        )
        return

    permissions = channel.permissions_for(me)
    if not permissions.view_channel or not permissions.send_messages:
        await interaction.response.send_message(
            "I need View Channel and Send Messages permissions in the selected channel.",
            ephemeral=True,
        )
        return

    if not permissions.mention_everyone:
        await interaction.response.send_message(
            "I need the Mention @everyone permission in the selected channel.",
            ephemeral=True,
        )
        return

    clean_title = title.strip()
    clean_version = version.strip()
    clean_message_id = change_logs.strip()
    clean_message = message.strip() if message else None

    if not clean_title:
        await interaction.response.send_message(
            "The title cannot be empty.",
            ephemeral=True,
        )
        return

    version_match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", clean_version)
    if not version_match:
        await interaction.response.send_message(
            "The version must contain numbers such as 2, 2.6, or 2.6.0.",
            ephemeral=True,
        )
        return

    major = version_match.group(1)
    minor = version_match.group(2) or "0"
    patch = version_match.group(3) or "0"
    normalized_version = f"{major}.{minor}.{patch}"

    if not re.fullmatch(r"\d{15,22}", clean_message_id):
        await interaction.response.send_message(
            "The change logs value must be a Discord message ID.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        source_message = await find_guild_message(
            interaction.guild,
            int(clean_message_id),
        )

        if source_message is None:
            await interaction.followup.send(
                "I could not find that message in a channel I can access.",
                ephemeral=True,
            )
            return

        change_content = source_message.content.strip()
        if not change_content:
            await interaction.followup.send(
                "The selected message does not contain any text to use as the change logs.",
                ephemeral=True,
            )
            return

        change_content = change_content.replace("```", "`\u200b``")

        container = make_container(
            make_text(f"## {clean_title}"),
            make_text(f"-# {normalized_version}"),
            make_separator(),
            make_text(
                f"### CHANGE LOGS\n```diff\n{change_content}```"
            ),
            *(
                [make_text(clean_message)]
                if clean_message
                else []
            ),
        )

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(make_text("@everyone"))
        view.add_item(container)

        sent_message = await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )

        await interaction.followup.send(
            f"Update log posted in {channel.mention}. Message ID: `{sent_message.id}`",
            ephemeral=True,
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I do not have permission to access the selected message or post the update in that channel.",
            ephemeral=True,
        )
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord returned an error while posting the update: {error}",
            ephemeral=True,
        )
    except ValueError:
        await interaction.followup.send(
            "The change logs message ID is invalid.",
            ephemeral=True,
        )
    except Exception as error:
        await interaction.followup.send(
            f"Unexpected error while posting the update: {error}",
            ephemeral=True,
        )


@reaction_group.command(
    name="role",
    description="Create reaction roles on an existing message",
)
async def add_reaction_role(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You need the Manage Roles permission to use this command.",
            ephemeral=True,
        )
        return

    me = interaction.guild.me

    if me is None or not me.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "I need the Manage Roles permission.",
            ephemeral=True,
        )
        return

    view = ReactionRoleSetupView(interaction.user.id)

    await interaction.response.send_message(
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="purge",
    description="Delete recent messages from the current channel",
)
@app_commands.describe(count="Number of messages to delete (1-1000)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 1, 1000],
):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "This command can only be used in a text channel.",
            ephemeral=True,
        )
        return

    if interaction.guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True,
        )
        return

    me = interaction.guild.me

    if me is None:
        await interaction.response.send_message(
            "I could not verify my permissions.",
            ephemeral=True,
        )
        return

    permissions = interaction.channel.permissions_for(me)

    if not permissions.manage_messages:
        await interaction.response.send_message(
            "I need the Manage Messages permission in this channel.",
            ephemeral=True,
        )
        return

    if not permissions.read_message_history:
        await interaction.response.send_message(
            "I need the Read Message History permission in this channel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await interaction.channel.purge(
            limit=int(count),
            bulk=True,
            reason=f"Purge requested by {interaction.user}",
        )

        view = PurgeView(
            requested=int(count),
            deleted=len(deleted),
            channel=interaction.channel,
        )

        await interaction.followup.send(
            view=view,
            ephemeral=True,
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to delete messages in this channel.",
            ephemeral=True,
        )
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord returned an error while purging messages: {error}",
            ephemeral=True,
        )


@bot.event
async def on_member_join(member: discord.Member):
    try:
        await mongo_call(
            add_member_event_sync,
            member.guild.id,
            "join",
        )
        await mongo_call(add_member_to_snapshot_sync, member.guild.id, member.id)
    except PyMongoError as error:
        print(f"MongoDB join tracking error for guild {member.guild.id}: {error}")


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        await mongo_call(
            add_member_event_sync,
            member.guild.id,
            "leave",
        )
        await mongo_call(remove_member_from_snapshot_sync, member.guild.id, member.id)
    except PyMongoError as error:
        print(f"MongoDB leave tracking error for guild {member.guild.id}: {error}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    data = created_channels.get(message.channel.id)
    if data is not None:
        member = message.author
        if isinstance(member, discord.Member) and not member.guild_permissions.administrator:
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

            me = message.guild.me
            kicked = False
            if me is not None and me.guild_permissions.kick_members and member.top_role < me.top_role:
                try:
                    await member.kick(reason="Message sent in anti-scam channel")
                    kicked = True
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass

            try:
                record = await mongo_call(
                    increment_anti_scam_sync,
                    message.channel.id,
                    kicked,
                )
                if record:
                    data["view"].update_stats(
                        kicks=int(record.get("kicks", 0)),
                        violations=int(record.get("violations", 0)),
                    )
                    if data.get("message") is not None:
                        try:
                            await data["message"].edit(view=data["view"])
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
            except PyMongoError as error:
                print(f"MongoDB anti-scam update error for channel {message.channel.id}: {error}")

    await bot.process_commands(message)


async def restore_anti_scam_channels():
    records = await mongo_call(list_anti_scam_sync)
    stale = []

    for record in records:
        try:
            channel_id = int(record["channel_id"])
            message_id = int(record["message_id"])
            guild_id = int(record["guild_id"])
            kicks = int(record.get("kicks", 0))
            violations = int(record.get("violations", kicks))
        except (KeyError, TypeError, ValueError):
            stale.append(record.get("_id"))
            continue

        guild = bot.get_guild(guild_id)

        if guild is None:
            continue

        channel = guild.get_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            stale.append(channel_id)
            continue

        view = AntiScamView(kicks, violations)

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            stale.append(channel_id)
            continue
        except discord.Forbidden:
            created_channels[channel_id] = {
                "view": view,
                "message": None,
                "guild_id": guild_id,
            }
            continue
        except discord.HTTPException:
            continue

        try:
            await message.edit(view=view)
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
        ):
            pass

        created_channels[channel_id] = {
            "view": view,
            "message": message,
            "guild_id": guild_id,
        }

    for channel_id in stale:
        if channel_id is not None:
            await mongo_call(
                delete_anti_scam_sync,
                channel_id,
            )


@bot.event
async def on_ready():
    global ready_once

    if ready_once:
        return

    try:
        await mongo_call(
            mongo_client.admin.command,
            "ping",
        )

        for guild in bot.guilds:
            try:
                member_ids = [member.id for member in guild.members]
                joined, left, initialized = await mongo_call(
                    reconcile_member_snapshot_sync,
                    guild.id,
                    member_ids,
                )
                if not initialized:
                    for _ in joined:
                        await mongo_call(
                            add_member_event_sync,
                            guild.id,
                            "join",
                        )
                    for _ in left:
                        await mongo_call(
                            add_member_event_sync,
                            guild.id,
                            "leave",
                        )
                    if joined or left:
                        print(
                            f"Reconciled {guild.name}: {len(joined)} missed joins, {len(left)} missed departures"
                        )
                await mongo_call(get_guild_insights_sync, guild.id)
            except PyMongoError as error:
                print(f"MongoDB member reconciliation error for guild {guild.id}: {error}")

        synced = await bot.tree.sync()

        await restore_anti_scam_channels()
        await restore_reaction_roles()

        ready_once = True

        print(
            f"Logged in as {bot.user} ({bot.user.id})"
        )
        print(
            f"Connected to MongoDB database: {MONGODB_DATABASE}"
        )
        print(
            f"Synced {len(synced)} command(s)"
        )
        print(
            f"Restored {len(created_channels)} anti-scam channel(s)"
        )
        print(
            f"Restored {len(reaction_role_cache)} reaction-role message(s)"
        )

    except PyMongoError as error:
        print(f"MongoDB startup error: {error}")
    except Exception as error:
        print(f"Startup error: {error}")


@anti_scam.error
async def anti_scam_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    message = (
        "You need the Manage Channels permission to use this command."
        if isinstance(error, app_commands.MissingPermissions)
        else f"Command error: {error}"
    )

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


@server_insights.error
async def server_insights_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    message = (
        "You need the Manage Server permission to use this command."
        if isinstance(error, app_commands.MissingPermissions)
        else f"Command error: {error}"
    )

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


@add_reaction_role.error
async def add_reaction_role_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    message = f"Command error: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


@purge.error
async def purge_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    message = (
        "You need the Manage Messages permission to use this command."
        if isinstance(error, app_commands.MissingPermissions)
        else f"Command error: {error}"
    )

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True,
        )


bot.tree.add_command(create_group)
bot.tree.add_command(server_group)
bot.tree.add_command(add_group)
bot.tree.add_command(update_group)


async def start_bot():
    global ready_once

    while True:
        try:
            await bot.start(TOKEN)
            break
        except discord.LoginFailure:
            print("Invalid Discord bot token.")
            break
        except discord.HTTPException as error:
            retry_after = getattr(error, "retry_after", 30)
            print(f"Discord connection error: {error}")
            print(f"Retrying in {retry_after:.1f} seconds...")
            await asyncio.sleep(retry_after)
        except Exception as error:
            print(f"Bot error: {error}")
            await asyncio.sleep(30)
        finally:
            ready_once = False


asyncio.run(start_bot())
