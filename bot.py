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

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
created_channels = {}
ready_once = False

ANTI_SCAM_MESSAGE = (
    "This channel is protected by the server moderation system.\n\n"
    "Please do not send messages here. Messages sent in this channel may result in an immediate kick from the server.\n\n"
    "If you have read and understood this notice, react with 👍 below."
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
    joins = [x for x in document.get("joins", []) if (dt := parse_datetime(x)) and dt >= cutoff]
    leaves = [x for x in document.get("leaves", []) if (dt := parse_datetime(x)) and dt >= cutoff]
    document["joins"] = joins
    document["leaves"] = leaves
    return document


def get_guild_insights_sync(guild_id):
    document = insights_collection.find_one({"_id": int(guild_id)})
    if not document:
        document = {"_id": int(guild_id), "joins": [], "leaves": []}
    cleanup_events_sync(document)
    insights_collection.replace_one({"_id": int(guild_id)}, document, upsert=True)
    return document


def add_member_event_sync(guild_id, event_type):
    guild_id = int(guild_id)
    field = "joins" if event_type == "join" else "leaves"
    document = insights_collection.find_one_and_update(
        {"_id": guild_id},
        {"$setOnInsert": {"joins": [], "leaves": []}, "$push": {field: iso_now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    cleanup_events_sync(document)
    insights_collection.replace_one({"_id": guild_id}, document, upsert=True)


def get_anti_scam_sync(channel_id):
    return anti_scam_collection.find_one({"_id": int(channel_id)})


def save_anti_scam_sync(record):
    channel_id = int(record["channel_id"])
    data = dict(record)
    data["_id"] = channel_id
    anti_scam_collection.replace_one({"_id": channel_id}, data, upsert=True)


def delete_anti_scam_sync(channel_id):
    anti_scam_collection.delete_one({"_id": int(channel_id)})


def list_anti_scam_sync():
    return list(anti_scam_collection.find({}))


def increment_kicks_sync(channel_id):
    return anti_scam_collection.find_one_and_update(
        {"_id": int(channel_id)},
        {"$inc": {"kicks": 1}},
        return_document=ReturnDocument.AFTER,
    )


async def mongo_call(function, *args):
    return await asyncio.to_thread(function, *args)


class AntiScamView(discord.ui.View):
    def __init__(self, kicks=0):
        super().__init__(timeout=None)
        self.kicks = int(kicks)
        self.kick_button = discord.ui.Button(
            label=f"Kicks: {self.kicks}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )
        self.add_item(self.kick_button)

    def update_kicks(self, kicks=None):
        if kicks is not None:
            self.kicks = int(kicks)
        self.kick_button.label = f"Kicks: {self.kicks}"


class InsightsView(discord.ui.View):
    def __init__(self, current, joins, leaves):
        super().__init__(timeout=None)
        self.current = current
        self.joins = joins
        self.leaves = leaves


class PurgeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)


create_group = app_commands.Group(name="create", description="Create server tools")
anti_group = app_commands.Group(name="anti", description="Anti moderation tools", parent=create_group)
server_group = app_commands.Group(name="server", description="Server information and tools")


@anti_group.command(name="scam", description="Create an anti-scam protection channel")
@app_commands.describe(name="The exact name of the channel to create")
@app_commands.checks.has_permissions(manage_channels=True)
async def anti_scam(interaction: discord.Interaction, name: str):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    me = interaction.guild.me

    if me is None:
        await interaction.followup.send("I could not verify my server permissions.", ephemeral=True)
        return

    if not me.guild_permissions.manage_channels:
        await interaction.followup.send("I need the Manage Channels permission.", ephemeral=True)
        return

    if not me.guild_permissions.kick_members:
        await interaction.followup.send("I need the Kick Members permission.", ephemeral=True)
        return

    clean_name = name.strip()
    if not clean_name:
        await interaction.followup.send("The channel name cannot be empty.", ephemeral=True)
        return

    try:
        channel = await interaction.guild.create_text_channel(clean_name, reason=f"Anti-scam channel created by {interaction.user}")
        view = AntiScamView()
        message = await channel.send(
            embed=discord.Embed(
                title="🛡️ Anti-Scam Protection",
                description=ANTI_SCAM_MESSAGE,
                color=discord.Color.red(),
            ),
            view=view,
        )

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
        created_channels[channel.id] = {"view": view, "message": message, "guild_id": interaction.guild.id}
        await interaction.followup.send(f"Created {channel.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to create or manage that channel.", ephemeral=True)
    except discord.HTTPException as error:
        await interaction.followup.send(f"Discord returned an error: {error}", ephemeral=True)
    except PyMongoError as error:
        try:
            await channel.delete(reason="MongoDB persistence failed")
        except Exception:
            pass
        await interaction.followup.send(f"MongoDB error while saving the channel: {error}", ephemeral=True)
    except Exception as error:
        await interaction.followup.send(f"Unexpected error: {error}", ephemeral=True)


@server_group.command(name="insights", description="View member activity from the last 30 days")
@app_commands.checks.has_permissions(manage_guild=True)
async def server_insights(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    await interaction.response.defer()

    try:
        document = await mongo_call(get_guild_insights_sync, interaction.guild.id)
        joins = len(document.get("joins", []))
        leaves = len(document.get("leaves", []))
        current = interaction.guild.member_count or 0
        net = joins - leaves
        growth = f"+{net:,}" if net > 0 else f"{net:,}"
        status = "📈 Growing" if net > 0 else "📉 Declining" if net < 0 else "➖ Stable"

        embed = discord.Embed(title="📊 Server Insights", color=discord.Color.blurple())
        embed.description = f"**{interaction.guild.name}**\nMember activity across the last 30 days."
        embed.add_field(name="👥 Current Members", value=f"`{current:,}`", inline=False)
        embed.add_field(name="🟢 New Members", value=f"`{joins:,}` joined during the last 30 days.", inline=False)
        embed.add_field(name="🔴 Departures", value=f"`{leaves:,}` left during the last 30 days.", inline=False)
        embed.add_field(name="📈 Net Change", value=f"`{growth}` members", inline=False)
        embed.add_field(name="Status", value=status, inline=False)
        await interaction.followup.send(embed=embed)
    except PyMongoError as error:
        await interaction.followup.send(f"MongoDB error: {error}", ephemeral=True)
    except Exception as error:
        await interaction.followup.send(f"Unexpected error: {error}", ephemeral=True)


@bot.tree.command(name="purge", description="Delete recent messages from the current channel")
@app_commands.describe(count="Number of messages to delete (1-1000)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, count: app_commands.Range[int, 1, 1000]):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("This command can only be used in a text channel.", ephemeral=True)
        return

    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
        return

    me = interaction.guild.me
    if me is None:
        await interaction.response.send_message("I could not verify my permissions.", ephemeral=True)
        return

    permissions = interaction.channel.permissions_for(me)
    if not permissions.manage_messages:
        await interaction.response.send_message("I need the Manage Messages permission in this channel.", ephemeral=True)
        return

    if not permissions.read_message_history:
        await interaction.response.send_message("I need the Read Message History permission in this channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        deleted = await interaction.channel.purge(limit=int(count), bulk=True, reason=f"Purge requested by {interaction.user}")
        embed = discord.Embed(
            title="🧹 Messages Cleared",
            description=f"Successfully cleared **{len(deleted):,}** message{'s' if len(deleted) != 1 else ''}.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Requested", value=f"`{int(count):,}`")
        embed.add_field(name="Deleted", value=f"`{len(deleted):,}`")
        embed.add_field(name="Channel", value=interaction.channel.mention)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to delete messages in this channel.", ephemeral=True)
    except discord.HTTPException as error:
        await interaction.followup.send(f"Discord returned an error while purging messages: {error}", ephemeral=True)


@bot.event
async def on_member_join(member: discord.Member):
    try:
        await mongo_call(add_member_event_sync, member.guild.id, "join")
    except PyMongoError as error:
        print(f"MongoDB join tracking error for guild {member.guild.id}: {error}")


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        await mongo_call(add_member_event_sync, member.guild.id, "leave")
    except PyMongoError as error:
        print(f"MongoDB leave tracking error for guild {member.guild.id}: {error}")


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
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass

    try:
        await member.kick(reason="Message sent in anti-scam channel")
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return

    try:
        record = await mongo_call(increment_kicks_sync, message.channel.id)
        kicks = int(record.get("kicks", data["view"].kicks)) if record else data["view"].kicks + 1
        data["view"].update_kicks(kicks)
        if data["message"] is not None:
            try:
                await data["message"].edit(view=data["view"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    except PyMongoError as error:
        print(f"MongoDB anti-scam update error for channel {message.channel.id}: {error}")


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
            created_channels[channel_id] = {"view": view, "message": None, "guild_id": guild_id}
            continue
        except discord.HTTPException:
            continue

        try:
            await message.edit(view=view)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass

        created_channels[channel_id] = {"view": view, "message": message, "guild_id": guild_id}

    for channel_id in stale:
        if channel_id is not None:
            await mongo_call(delete_anti_scam_sync, channel_id)


@bot.event
async def on_ready():
    global ready_once
    if ready_once:
        return

    try:
        await mongo_call(mongo_client.admin.command, "ping")
        synced = await bot.tree.sync()
        await restore_anti_scam_channels()
        ready_once = True
        print(f"Logged in as {bot.user} ({bot.user.id})")
        print(f"Connected to MongoDB database: {MONGODB_DATABASE}")
        print(f"Synced {len(synced)} command(s)")
        print(f"Restored {len(created_channels)} anti-scam channel(s)")
    except PyMongoError as error:
        print(f"MongoDB startup error: {error}")
    except Exception as error:
        print(f"Startup error: {error}")


@anti_scam.error
async def anti_scam_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    message = "You need the Manage Channels permission to use this command." if isinstance(error, app_commands.MissingPermissions) else f"Command error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@server_insights.error
async def server_insights_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    message = "You need the Manage Server permission to use this command." if isinstance(error, app_commands.MissingPermissions) else f"Command error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@purge.error
async def purge_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    message = "You need the Manage Messages permission to use this command." if isinstance(error, app_commands.MissingPermissions) else f"Command error: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.tree.add_command(create_group)
bot.tree.add_command(server_group)


async def start_bot():
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
