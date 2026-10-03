import os
import json
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
INSIGHTS_FILE = "server_insights.json"
ANTI_SCAM_FILE = "anti_scam_channels.json"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

ANTI_SCAM_MESSAGE = (
    "This channel is protected by the server moderation system.\n\n"
    "Please do not send messages here. Messages sent in this channel "
    "may result in an immediate kick from the server.\n\n"
    "If you have read and understood this notice, react with 👍 below."
)

created_channels = {}


def load_json(filename, default):
    if not os.path.exists(filename):
        return default

    try:
        with open(filename, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data
    except (json.JSONDecodeError, OSError):
        return default


def save_json(filename, data):
    temporary_file = f"{filename}.tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    os.replace(temporary_file, filename)


insights_data = load_json(INSIGHTS_FILE, {})
anti_scam_data = load_json(ANTI_SCAM_FILE, {})


def guild_insights(guild_id):
    key = str(guild_id)

    if key not in insights_data:
        insights_data[key] = {
            "joins": [],
            "leaves": []
        }

    return insights_data[key]


def cleanup_events(data):
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    for event_type in ("joins", "leaves"):
        cleaned = []

        for timestamp in data.get(event_type, []):
            try:
                event_time = datetime.fromisoformat(timestamp)

                if event_time >= cutoff:
                    cleaned.append(timestamp)
            except ValueError:
                continue

        data[event_type] = cleaned


class AntiScamPanel(discord.ui.LayoutView):
    def __init__(self, kicks=0):
        super().__init__(timeout=None)
        self.kicks = kicks

        self.container = discord.ui.Container(
            discord.ui.TextDisplay("## 🛡️ Anti-Scam Protection"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(ANTI_SCAM_MESSAGE),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                "### ⚠️ Automatic Moderation\n"
                "This channel is monitored automatically. "
                "Messages sent here are removed and the sender may be "
                "kicked from the server."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.ActionRow(
                discord.ui.Button(
                    label=f"kicks: {kicks}",
                    style=discord.ButtonStyle.secondary,
                    disabled=True
                )
            )
        )

        self.add_item(self.container)

    def update_kicks(self):
        for item in self.container.children:
            if isinstance(item, discord.ui.ActionRow):
                for button in item.children:
                    if isinstance(button, discord.ui.Button):
                        button.label = f"kicks: {self.kicks}"


class InsightsPanel(discord.ui.LayoutView):
    def __init__(self, guild):
        super().__init__(timeout=None)

        data = guild_insights(guild.id)
        cleanup_events(data)

        joins = len(data["joins"])
        leaves = len(data["leaves"])
        current_members = guild.member_count or 0
        net_growth = joins - leaves

        if net_growth > 0:
            growth = f"+{net_growth:,}"
            status = "📈 Growing"
        elif net_growth < 0:
            growth = f"{net_growth:,}"
            status = "📉 Declining"
        else:
            growth = "0"
            status = "➖ Stable"

        self.container = discord.ui.Container(
            discord.ui.TextDisplay("## 📊 Server Insights"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"### {guild.name}\n"
                "A clean overview of member activity across "
                "the last 30 days."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"👥 **Current Members**\n"
                f"`{current_members:,}`"
            ),
            discord.ui.TextDisplay(
                f"🟢 **New Members**\n"
                f"`{joins:,}` joined during the last 30 days."
            ),
            discord.ui.TextDisplay(
                f"🔴 **Departures**\n"
                f"`{leaves:,}` left during the last 30 days."
            ),
            discord.ui.TextDisplay(
                f"📈 **Net Change**\n"
                f"`{growth}` members"
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"**{status}**\n"
                "Join and leave activity is automatically tracked "
                "over a rolling 30-day period."
            )
        )

        self.add_item(self.container)


class PurgePanel(discord.ui.LayoutView):
    def __init__(self, count, deleted):
        super().__init__(timeout=None)

        self.container = discord.ui.Container(
            discord.ui.TextDisplay("## 🧹 Messages Cleared"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"Successfully cleared **{deleted:,}** message"
                f"{'s' if deleted != 1 else ''}."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"**Requested:** `{count:,}`\n"
                f"**Deleted:** `{deleted:,}`\n"
                f"**Channel:** {self.channel_name}"
            )
        )

        self.add_item(self.container)

    @property
    def channel_name(self):
        return "Current channel"


create_group = app_commands.Group(
    name="create",
    description="Create server tools"
)

anti_group = app_commands.Group(
    name="anti",
    description="Anti moderation tools",
    parent=create_group
)

server_group = app_commands.Group(
    name="server",
    description="Server information and tools"
)


@anti_group.command(
    name="scam",
    description="Create an anti-scam protection channel"
)
@app_commands.describe(
    name="The exact name of the channel to create"
)
@app_commands.checks.has_permissions(manage_channels=True)
async def anti_scam(
    interaction: discord.Interaction,
    name: str
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    me = interaction.guild.me

    if me is None:
        await interaction.followup.send(
            "I could not verify my server permissions.",
            ephemeral=True
        )
        return

    if not me.guild_permissions.manage_channels:
        await interaction.followup.send(
            "I need the Manage Channels permission.",
            ephemeral=True
        )
        return

    if not me.guild_permissions.kick_members:
        await interaction.followup.send(
            "I need the Kick Members permission.",
            ephemeral=True
        )
        return

    try:
        channel = await interaction.guild.create_text_channel(name)

        panel = AntiScamPanel()

        sent_message = await channel.send(view=panel)

        try:
            await sent_message.add_reaction("👍")
        except discord.HTTPException:
            pass

        anti_scam_data[str(channel.id)] = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "message_id": sent_message.id,
            "kicks": 0
        }

        save_json(ANTI_SCAM_FILE, anti_scam_data)

        created_channels[channel.id] = {
            "panel": panel,
            "message": sent_message,
            "guild_id": interaction.guild.id
        }

        await interaction.followup.send(
            f"Created {channel.mention}.",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create or manage that channel.",
            ephemeral=True
        )
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord returned an error: {error}",
            ephemeral=True
        )


@server_group.command(
    name="insights",
    description="View member activity from the last 30 days"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def server_insights(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    data = guild_insights(interaction.guild.id)
    cleanup_events(data)
    save_json(INSIGHTS_FILE, insights_data)

    await interaction.response.send_message(
        view=InsightsPanel(interaction.guild)
    )


@bot.tree.command(
    name="purge",
    description="Delete recent messages from the current channel"
)
@app_commands.describe(
    count="Number of messages to delete (1-1000)"
)
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 1, 1000]
):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            "This command can only be used in a text channel.",
            ephemeral=True
        )
        return

    me = interaction.guild.me

    if me is None:
        await interaction.response.send_message(
            "I could not verify my permissions.",
            ephemeral=True
        )
        return

    permissions = interaction.channel.permissions_for(me)

    if not permissions.manage_messages:
        await interaction.response.send_message(
            "I need the Manage Messages permission in this channel.",
            ephemeral=True
        )
        return

    if not permissions.read_message_history:
        await interaction.response.send_message(
            "I need the Read Message History permission in this channel.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        deleted_messages = await interaction.channel.purge(
            limit=count,
            bulk=True,
            reason=f"Purge requested by {interaction.user}"
        )

        deleted = len(deleted_messages)

        await interaction.followup.send(
            view=PurgePanel(count, deleted),
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to delete messages in this channel.",
            ephemeral=True
        )
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord returned an error while purging messages: {error}",
            ephemeral=True
        )


@bot.event
async def on_member_join(member: discord.Member):
    data = guild_insights(member.guild.id)

    data["joins"].append(
        datetime.now(timezone.utc).isoformat()
    )

    cleanup_events(data)
    save_json(INSIGHTS_FILE, insights_data)


@bot.event
async def on_member_remove(member: discord.Member):
    data = guild_insights(member.guild.id)

    data["leaves"].append(
        datetime.now(timezone.utc).isoformat()
    )

    cleanup_events(data)
    save_json(INSIGHTS_FILE, insights_data)


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

    if me is None:
        return

    if not me.guild_permissions.kick_members:
        return

    if member.top_role >= me.top_role:
        return

    try:
        await message.delete()
    except (
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException
    ):
        pass

    try:
        await member.kick(
            reason="Message sent in anti-scam channel"
        )

        data["panel"].kicks += 1
        data["panel"].update_kicks()

        record = anti_scam_data.get(str(message.channel.id))

        if record:
            record["kicks"] = data["panel"].kicks
            save_json(ANTI_SCAM_FILE, anti_scam_data)

        if data["message"]:
            try:
                await data["message"].edit(
                    view=data["panel"]
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                pass

    except (
        discord.Forbidden,
        discord.NotFound,
        discord.HTTPException
    ):
        pass


async def restore_anti_scam_channels():
    stale_channels = []

    for key, record in list(anti_scam_data.items()):
        try:
            channel_id = int(record["channel_id"])
            message_id = int(record["message_id"])
            guild_id = int(record["guild_id"])
            kicks = int(record.get("kicks", 0))
        except (KeyError, TypeError, ValueError):
            stale_channels.append(key)
            continue

        guild = bot.get_guild(guild_id)

        if guild is None:
            continue

        channel = guild.get_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            stale_channels.append(key)
            continue

        panel = AntiScamPanel(kicks=kicks)

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            stale_channels.append(key)
            continue
        except discord.Forbidden:
            created_channels[channel_id] = {
                "panel": panel,
                "message": None,
                "guild_id": guild_id
            }
            continue
        except discord.HTTPException:
            continue

        try:
            await message.edit(view=panel)
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException
        ):
            pass

        created_channels[channel_id] = {
            "panel": panel,
            "message": message,
            "guild_id": guild_id
        }

    for key in stale_channels:
        anti_scam_data.pop(key, None)

    save_json(ANTI_SCAM_FILE, anti_scam_data)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()

        await restore_anti_scam_channels()

        print(f"Logged in as {bot.user} ({bot.user.id})")
        print(f"Synced {len(synced)} command(s)")
        print(f"Restored {len(created_channels)} anti-scam channel(s)")

    except Exception as error:
        print(f"Startup error: {error}")


@anti_scam.error
async def anti_scam_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Channels permission to use this command."
    else:
        message = f"Command error: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True
        )


@server_insights.error
async def server_insights_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Server permission to use this command."
    else:
        message = f"Command error: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True
        )


@purge.error
async def purge_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the Manage Messages permission to use this command."
    else:
        message = f"Command error: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(
            message,
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message,
            ephemeral=True
        )


async def start_bot():
    while True:
        try:
            await bot.start(TOKEN)
            break

        except discord.HTTPException as error:
            retry_after = getattr(error, "retry_after", 30)

            print(f"Discord connection error: {error}")
            print(f"Retrying in {retry_after:.1f} seconds...")

            await asyncio.sleep(retry_after)

        except discord.LoginFailure:
            print("Invalid Discord bot token.")
            break

        except Exception as error:
            print(f"Bot error: {error}")
            await asyncio.sleep(30)


bot.tree.add_command(create_group)
bot.tree.add_command(server_group)

asyncio.run(start_bot())
