import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands


TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


class AntiScamView(discord.ui.LayoutView):
    def __init__(self, message_text: str, kicks: int = 0):
        super().__init__(timeout=None)

        label = "kick: 1" if kicks == 1 else f"kicks: {kicks}"

        container = discord.ui.Container(
            discord.ui.TextDisplay("## Don't Type Here"),
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.small
            ),
            discord.ui.TextDisplay(message_text),
            discord.ui.ActionRow(
                discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    custom_id="anti_scam_kick_counter"
                )
            )
        )

        self.add_item(container)


class AntiScamData:
    def __init__(self, message_id: int, warning: str):
        self.message_id = message_id
        self.warning = warning
        self.kicks = 0


anti_scam_channels: dict[int, AntiScamData] = {}


class CreateGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="create",
            description="Create server management features."
        )


class AntiGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="anti",
            description="Create anti-abuse features."
        )


create_group = CreateGroup()
anti_group = AntiGroup()


@anti_group.command(
    name="scam",
    description="Create an anti-scam channel."
)
@app_commands.describe(
    name="The exact name of the channel to create.",
    message="The warning message displayed in the channel."
)
@app_commands.default_permissions(manage_guild=True)
async def anti_scam(
    interaction: discord.Interaction,
    name: str,
    message: str
):
    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True
        )
        return

    bot_member = guild.me

    if bot_member is None:
        await interaction.response.send_message(
            "I could not determine my permissions in this server.",
            ephemeral=True
        )
        return

    missing = []

    if not bot_member.guild_permissions.manage_channels:
        missing.append("Manage Channels")

    if not bot_member.guild_permissions.kick_members:
        missing.append("Kick Members")

    if not bot_member.guild_permissions.send_messages:
        missing.append("Send Messages")

    if missing:
        await interaction.response.send_message(
            "I am missing: " + ", ".join(missing),
            ephemeral=True
        )
        return

    if not name.strip():
        await interaction.response.send_message(
            "The channel name cannot be empty.",
            ephemeral=True
        )
        return

    if not message.strip():
        await interaction.response.send_message(
            "The message cannot be empty.",
            ephemeral=True
        )
        return

    existing = discord.utils.find(
        lambda channel: channel.name == name,
        guild.text_channels
    )

    if existing:
        await interaction.response.send_message(
            f"A channel named `{name}` already exists.",
            ephemeral=True
        )
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        ),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            manage_channels=True
        )
    }

    await interaction.response.defer(ephemeral=True)

    try:
        channel = await guild.create_text_channel(
            name=name,
            overwrites=overwrites,
            reason=f"Anti-scam channel created by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to create that channel.",
            ephemeral=True
        )
        return
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"Discord returned an error while creating the channel: {error}",
            ephemeral=True
        )
        return

    data = AntiScamData(
        message_id=0,
        warning=message
    )

    anti_scam_channels[channel.id] = data

    try:
        panel = await channel.send(
            view=AntiScamView(message, 0)
        )

        data.message_id = panel.id

    except discord.Forbidden:
        anti_scam_channels.pop(channel.id, None)

        try:
            await channel.delete(
                reason="Could not send anti-scam panel"
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            "The channel was created, but I could not send the anti-scam panel.",
            ephemeral=True
        )
        return

    except discord.HTTPException as error:
        anti_scam_channels.pop(channel.id, None)

        try:
            await channel.delete(
                reason="Could not send anti-scam panel"
            )
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            f"Could not send the anti-scam panel: {error}",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        f"Created {channel.mention} successfully.",
        ephemeral=True
    )


async def handle_anti_scam_message(message: discord.Message):
    if message.author.bot:
        return

    data = anti_scam_channels.get(message.channel.id)

    if data is None:
        return

    if message.guild is None:
        return

    member = message.author

    if not isinstance(member, discord.Member):
        return

    if member.id == message.guild.owner_id:
        return

    if member.guild_permissions.administrator:
        return

    bot_member = message.guild.me

    if bot_member is None:
        return

    if member.top_role >= bot_member.top_role:
        return

    try:
        await member.kick(
            reason=f"Message sent in anti-scam channel #{message.channel.name}"
        )
    except discord.Forbidden:
        return
    except discord.HTTPException:
        return

    data.kicks += 1

    try:
        panel = await message.channel.fetch_message(data.message_id)

        await panel.edit(
            view=AntiScamView(
                data.warning,
                data.kicks
            )
        )
    except discord.NotFound:
        pass
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    await handle_anti_scam_message(message)
    await bot.process_commands(message)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    if not getattr(bot, "_commands_synced", False):
        try:
            synced = await bot.tree.sync()
            bot._commands_synced = True
            print(f"Synced {len(synced)} application command(s)")
        except discord.HTTPException as error:
            print(f"Command sync failed: {error}")


async def start_bot():
    while True:
        try:
            await bot.start(TOKEN)
            break

        except discord.LoginFailure:
            print("Discord rejected the bot token.")
            print("Check DISCORD_TOKEN in Render.")
            await asyncio.sleep(60)

        except discord.HTTPException as error:
            if error.status == 429:
                retry_after = 60.0

                try:
                    retry_after = float(
                        error.response.headers.get(
                            "Retry-After",
                            retry_after
                        )
                    )
                except (AttributeError, TypeError, ValueError):
                    pass

                retry_after = max(retry_after, 5.0)

                print(
                    f"Discord rate-limited the bot. "
                    f"Waiting {retry_after:.1f} seconds before retrying."
                )

                await asyncio.sleep(retry_after)
                continue

            print(f"Discord HTTP error: {error}")
            await asyncio.sleep(30)

        except Exception as error:
            print(f"Unexpected startup error: {error}")
            await asyncio.sleep(30)


asyncio.run(start_bot())
