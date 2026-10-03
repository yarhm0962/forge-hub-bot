import os
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

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


class AntiScamPanel(discord.ui.LayoutView):
    def __init__(self, message):
        super().__init__(timeout=None)
        self.kicks = 0

        self.container = discord.ui.Container(
            discord.ui.TextDisplay("## Don't Type Here"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(message),
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


class AntiScamData:
    def __init__(self, message):
        self.message = message
        self.panel = AntiScamPanel(message)
        self.kicks = 0


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user} ({bot.user.id})")
        print(f"Synced {len(synced)} command(s)")
    except Exception as error:
        print(f"Command sync error: {error}")


@bot.tree.command(
    name="create",
    description="Create an anti-scam moderation channel"
)
@app_commands.describe(
    name="The exact name of the channel to create",
    message="The warning message displayed in the channel"
)
@app_commands.checks.has_permissions(manage_channels=True)
async def create(
    interaction: discord.Interaction,
    name: str,
    message: str
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

        data = AntiScamData(message)
        created_channels[channel.id] = data

        await channel.send(
            view=data.panel
        )

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


@create.error
async def create_error(
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
        data.kicks += 1
        data.panel.kicks = data.kicks
        data.panel.update_kicks()
        await data.panel.container.view.message.edit(view=data.panel)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
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


asyncio.run(start_bot())
