import os
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

TOKEN = os.getenv("DISCORD_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "PanelBot")

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

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

created_channels = {}
reaction_role_cache = {}
ready_once = False

ANTI_SCAM_TITLE = "## Don't Type Here"
ANTI_SCAM_BODY = (
    "Don't type here, and this server is only for Fake social media spam messages "
    "and they can be kicked immediately if anyone sends a message here."
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
    joins = [
        x for x in document.get("joins", [])
        if (dt := parse_datetime(x)) and dt >= cutoff
    ]
    leaves = [
        x for x in document.get("leaves", [])
        if (dt := parse_datetime(x)) and dt >= cutoff
    ]
    document["joins"] = joins
    document["leaves"] = leaves
    return document


def get_guild_insights_sync(guild_id):
    guild_id = int(guild_id)
    document = insights_collection.find_one({"_id": guild_id})
    if not document:
        document = {"_id": guild_id, "joins": [], "leaves": []}
    cleanup_events_sync(document)
    insights_collection.replace_one({"_id": guild_id}, document, upsert=True)
    return document


def add_member_event_sync(guild_id, event_type):
    guild_id = int(guild_id)
    field = "joins" if event_type == "join" else "leaves"
    document = insights_collection.find_one_and_update(
        {"_id": guild_id},
        {
            "$setOnInsert": {"joins": [], "leaves": []},
            "$push": {field: iso_now()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    cleanup_events_sync(document)
    insights_collection.replace_one({"_id": guild_id}, document, upsert=True)


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


def increment_kicks_sync(channel_id):
    return anti_scam_collection.find_one_and_update(
        {"_id": int(channel_id)},
        {"$inc": {"kicks": 1}},
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
    def __init__(self, kicks=0):
        super().__init__(timeout=None)
        self.kicks = int(kicks)

        title = make_text(ANTI_SCAM_TITLE)
        separator = make_separator()
        body = make_text(ANTI_SCAM_BODY)
        kick_row = discord.ui.ActionRow()

        self.kick_button = discord.ui.Button(
            label=f"Kicks: {self.kicks}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        kick_row.add_item(self.kick_button)

        self.add_item(
            make_container(
                title,
                separator,
                body,
                separator,
                kick_row,
            )
        )

    def update_kicks(self, kicks=None):
        if kicks is not None:
            self.kicks = int(kicks)
        self.kick_button.label = f"Kicks: {self.kicks}"


class InsightsView(discord.ui.LayoutView):
    def __init__(self, current, joins, leaves):
        super().__init__(timeout=None)

        net = joins - leaves
        status = "📈 Growing" if net > 0 else "📉 Declining" if net < 0 else "➖ Stable"
        growth = f"+{net:,}" if net > 0 else f"{net:,}"

        self.add_item(
            make_container(
                make_text("## 📊 Server Insights"),
                make_separator(),
                make_text(
                    f"**Current Members:** `{current:,}`\n"
                    f"**New Members:** `{joins:,}`\n"
                    f"**Departures:** `{leaves:,}`\n"
                    f"**Net Change:** `{growth}`\n"
                    f"**Status:** {status}"
                ),
            )
        )


class PurgeView(discord.ui.LayoutView):
    def __init__(self, requested, deleted, channel):
        super().__init__(timeout=None)

        self.add_item(
            make_container(
                make_text("## 🧹 Messages Cleared"),
                make_separator(),
                make_text(
                    f"**Requested:** `{requested:,}`\n"
                    f"**Deleted:** `{deleted:,}`\n"
                    f"**Channel:** {channel.mention}"
                ),
            )
        )


class MessageIdModal(discord.ui.Modal, title="Target Message"):
    message_id = discord.ui.TextInput(
        label="Message ID",
        placeholder="Enter the Discord message ID",
        required=True,
        max_length=30,
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction):
        value = str(self.message_id.value).strip()

        if not value.isdigit():
            await interaction.response.send_message(
                "That is not a valid Discord message ID.",
                ephemeral=True,
            )
            return

        self.parent_view.message_id = int(value)
        self.parent_view.refresh()
        await interaction.response.send_message(
            f"Target message set to `{value}`.",
            ephemeral=True,
        )


class EmojiModal(discord.ui.Modal, title="Reaction Emojis"):
    emoji_1 = discord.ui.TextInput(
        label="Emoji 1",
        placeholder="Example: ⭐ or <:custom:123456789>",
        required=True,
        max_length=100,
    )
    emoji_2 = discord.ui.TextInput(
        label="Emoji 2",
        placeholder="Optional",
        required=False,
        max_length=100,
    )
    emoji_3 = discord.ui.TextInput(
        label="Emoji 3",
        placeholder="Optional",
        required=False,
        max_length=100,
    )
    emoji_4 = discord.ui.TextInput(
        label="Emoji 4",
        placeholder="Optional",
        required=False,
        max_length=100,
    )
    emoji_5 = discord.ui.TextInput(
        label="Emoji 5",
        placeholder="Optional",
        required=False,
        max_length=100,
    )

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
            await interaction.response.send_message(
                "You must provide at least one emoji.",
                ephemeral=True,
            )
            return

        if len(values) > 5:
            await interaction.response.send_message(
                "You can use a maximum of 5 emojis.",
                ephemeral=True,
            )
            return

        self.parent_view.emojis = values
        self.parent_view.refresh()

        await interaction.response.send_message(
            f"Saved {len(values)} emoji{'s' if len(values) != 1 else ''}.",
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

        self.message_button = discord.ui.Button(
            label="Message ID",
            style=discord.ButtonStyle.secondary,
            emoji="🆔",
        )
        self.message_button.callback = self.message_id_callback

        self.role_button = discord.ui.Button(
            label="Select Role",
            style=discord.ButtonStyle.secondary,
            emoji="👤",
        )
        self.role_button.callback = self.role_callback

        self.emoji_button = discord.ui.Button(
            label="Emoji",
            style=discord.ButtonStyle.secondary,
            emoji="😀",
        )
        self.emoji_button.callback = self.emoji_callback

        self.save_button = discord.ui.Button(
            label="Save",
            style=discord.ButtonStyle.success,
            emoji="💾",
        )
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
                    "Set the target message, choose up to 5 roles, "
                    "then add up to 5 emojis."
                ),
                make_separator(),
                self.status,
                make_separator(),
                row,
            )
        )

    def refresh(self):
        self.status.content = self.status_text()

    def status_text(self):
        message = f"`{self.message_id}`" if self.message_id else "`Not set`"
        roles = ", ".join(role.mention for role in self.roles) if self.roles else "`Not set`"
        emojis = " ".join(self.emojis) if self.emojis else "`Not set`"

        return (
            f"**Message:** {message}\n"
            f"**Roles:** {roles}\n"
            f"**Emojis:** {emojis}"
        )

    def refresh_status(self):
        self.status.content = self.status_text()

    async def check_author(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who started this setup can use these controls.",
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
            view=picker,
            ephemeral=True,
        )

    async def emoji_callback(self, interaction):
        if not await self.check_author(interaction):
            return

        await interaction.response.send_modal(EmojiModal(self))

    async def save_callback(self, interaction):
        if not await self.check_author(interaction):
            return

        if self.message_id is None:
            await interaction.response.send_message(
                "Set the target Message ID first.",
                ephemeral=True,
            )
            return

        if not self.roles:
            await interaction.response.send_message(
                "Select at least one role.",
                ephemeral=True,
            )
            return

        if not self.emojis:
            await interaction.response.send_message(
                "Add at least one emoji.",
                ephemeral=True,
            )
            return

        if len(self.roles) != len(self.emojis):
            await interaction.response.send_message(
                "The number of roles and emojis must match.",
                ephemeral=True,
            )
            return

        if len(self.roles) > 5 or len(self.emojis) > 5:
            await interaction.response.send_message(
                "You can configure a maximum of 5 roles and 5 emojis.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        me = guild.me
        if me is None:
            await interaction.response.send_message(
                "I could not verify my server permissions.",
                ephemeral=True,
            )
            return

        if not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the Manage Roles permission.",
                ephemeral=True,
            )
            return

        for role in self.roles:
            if role.is_default():
                await interaction.response.send_message(
                    "The @everyone role cannot be used.",
                    ephemeral=True,
                )
                return

            if role.managed:
                await interaction.response.send_message(
                    f"{role.mention} is managed and cannot be assigned.",
                    ephemeral=True,
                )
                return

            if role >= me.top_role:
                await interaction.response.send_message(
                    f"I cannot assign {role.mention} because it is above my highest role.",
                    ephemeral=True,
                )
                return

        target = await find_message_in_guild(guild, self.message_id)

        if target is None:
            await interaction.response.send_message(
                "I could not find that message in this server. "
                "Make sure the bot can view the channel and read message history.",
                ephemeral=True,
            )
            return

        unique_emojis = []
        for emoji in self.emojis:
            if emoji not in unique_emojis:
                unique_emojis.append(emoji)

        if len(unique_emojis) != len(self.emojis):
            await interaction.response.send_message(
                "Each reaction emoji must be unique.",
                ephemeral=True,
            )
            return

        pairs = [
            {
                "emoji": emoji,
                "role_id": role.id,
                "role_name": role.name,
            }
            for emoji, role in zip(self.emojis, self.roles)
        ]

        try:
            for emoji in self.emojis:
                await target.add_reaction(emoji)

            record = {
                "message_id": target.id,
                "channel_id": target.channel.id,
                "guild_id": guild.id,
                "pairs": pairs,
            }

            await mongo_call(save_reaction_role_sync, record)

            reaction_role_cache[target.id] = record

            await interaction.response.send_message(
                f"Reaction roles saved on message `{target.id}`.",
                ephemeral=True,
            )
            self.stop()

        except discord.HTTPException as error:
            await interaction.response.send_message(
                f"Discord returned an error while adding the reactions: {error}",
                ephemeral=True,
            )
        except PyMongoError as error:
            await interaction.response.send_message(
                f"MongoDB error while saving reaction roles: {error}",
                ephemeral=True,
            )


class ReactionRoleMessageView(discord.ui.LayoutView):
    def __init__(self, pairs):
        super().__init__(timeout=None)
        text = "## 🎭 Reaction Roles\nReact below to receive or remove the matching role."

        lines = []
        for pair in pairs:
            lines.append(f"{pair['emoji']}  →  **{pair['role_name']}**")

        self.add_item(
            make_container(
                make_text(text),
                make_separator(),
                make_text("\n".join(lines)),
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
        document = await mongo_call(
            get_guild_insights_sync,
            interaction.guild.id,
        )

        joins = len(document.get("joins", []))
        leaves = len(document.get("leaves", []))
        current = interaction.guild.member_count or 0

        view = InsightsView(
            current=current,
            joins=joins,
            leaves=leaves,
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
    except PyMongoError as error:
        print(
            f"MongoDB join tracking error for guild "
            f"{member.guild.id}: {error}"
        )


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        await mongo_call(
            add_member_event_sync,
            member.guild.id,
            "leave",
        )
    except PyMongoError as error:
        print(
            f"MongoDB leave tracking error for guild "
            f"{member.guild.id}: {error}"
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    data = created_channels.get(message.channel.id)

    if data is None:
        return

    member = message.author

    if not isinstance(member, discord.Member):
        return

    if member.guild_permissions.administrator:
        return

    me = message.guild.me

    if me is None or not me.guild_permissions.kick_members:
        return

    if member.top_role >= me.top_role:
        return

    try:
        await message.delete()
    except (
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException,
    ):
        pass

    try:
        await member.kick(
            reason="Message sent in anti-scam channel",
        )
    except (
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException,
    ):
        return

    try:
        record = await mongo_call(
            increment_kicks_sync,
            message.channel.id,
        )

        kicks = (
            int(record.get("kicks", data["view"].kicks))
            if record
            else data["view"].kicks + 1
        )

        data["view"].update_kicks(kicks)

        if data["message"] is not None:
            try:
                await data["message"].edit(view=data["view"])
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                pass

    except PyMongoError as error:
        print(
            f"MongoDB anti-scam update error for channel "
            f"{message.channel.id}: {error}"
        )


async def restore_anti_scam_channels():
    records = await mongo_call(list_anti_scam_sync)
    stale = []

    for record in records:
        try:
            channel_id = int(record["channel_id"])
            message_id = int(record["message_id"])
            guild_id = int(record["guild_id"])
            kicks = int(record.get("kicks", 0))
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

        view = AntiScamView(kicks)

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
