import os
import json
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "server_insights.json"

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

created_channels = {}

ANTI_SCAM_MESSAGE = (
    "This channel is protected by the server moderation system.\n\n"
    "Do not send messages here. Any message sent in this channel may result "
    "in an immediate kick from the server.\n\n"
    "If you have read and understood this notice, react with 👍 below."
)


def load_insights():
    if not os.path.exists(DATA_FILE):
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_insights(data):
    temporary_file = f"{DATA_FILE}.tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    os.replace(temporary_file, DATA_FILE)


insights_data = load_insights()


def guild_data(guild_id):
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
    def __init__(self):
        super().__init__(timeout=None)
        self.kicks = 0

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
                "### ⚠️ Moderation Notice\n"
                "Messages sent here are monitored automatically. "
                "Please read the notice above before interacting with this channel."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="kicks: 0",
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

        data = guild_data(guild.id)
        cleanup_events(data)

        joins = len(data["joins"])
        leaves = len(data["leaves"])
        current_members = guild.member_count or 0
        net_growth = joins - leaves

        if net_growth > 0:
            growth_text = f"📈 +{net_growth}"
        elif net_growth < 0:
            growth_text = f"📉 {net_growth}"
        else:
            growth_text = "➖ 0"

        self.container = discord.ui.Container(
            discord.ui.TextDisplay("## 📊 Server Insights"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"### {guild.name}\n"
                "Here is the server's activity summary for the last 30 days."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"👥 **Current Members**\n"
                f"`{current_members:,}`"
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"🟢 **Joined — Last 30 Days**\n"
                f"`{joins:,}`"
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"🔴 **Left — Last 30 Days**\n"
                f"`{leaves:,}`"
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"📈 **Net Growth — Last 30 Days**\n"
                f"`{growth_text}`"
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                "### ℹ️ About These Numbers\n"
                "Join and leave totals are tracked automatically by the bot "
                "and calculated over a rolling 30-day period."
            )
        )

        self.add_item(self.container)


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

        data = AntiScamPanel()
        created_channels[channel.id] = {
            "panel": data,
            "message": None
        }

        sent_message = await channel.send(view=data)

        created_channels[channel.id]["message"] = sent_message

        try:
            await sent_message.add_reaction("👍")
        except discord.HTTPException:
            pass

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
    description="View the server's last 30 days of member insights"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def server_insights(
    interaction: discord.Interaction
):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    data = guild_data(interaction.guild.id)
    cleanup_events(data)
    save_insights(insights_data)

    view = InsightsPanel(interaction.guild)

    await interaction.response.send_message(
        view=view,
        ephemeral=False
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


@bot.event
async def on_member_join(member: discord.Member):
    data = guild_data(member.guild.id)

    data["joins"].append(
        datetime.now(timezone.utc).isoformat()
    )

    cleanup_events(data)
    save_insights(insights_data)


@bot.event
async def on_member_remove(member: discord.Member):
    data = guild_data(member.guild.id)

    data["leaves"].append(
        datetime.now(timezone.utc).isoformat()
    )

    cleanup_events(data)
    save_insights(insights_data)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()

        print(f"Logged in as {bot.user} ({bot.user.id})")
        print(f"Synced {len(synced)} command(s)")
    except Exception as error:
        print(f"Command sync error: {error}")


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
        discord.HTTPException
    ):
        pass

    try:
        await member.kick(
            reason="Message sent in anti-scam channel"
        )

        panel = data["panel"]
        panel.kicks += 1
        panel.update_kicks()

        sent_message = data["message"]

        if sent_message:
            try:
                await sent_message.edit(view=panel)
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
