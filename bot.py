import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


class KickCounterView(discord.ui.LayoutView):
    def __init__(self, warning_message, kick_count=0):
        super().__init__(timeout=None)

        self.warning_message = warning_message
        self.kick_count = kick_count

        self.container = discord.ui.Container(
            discord.ui.TextDisplay("## Don't Type Here"),
            discord.ui.Separator(
                visible=True,
                spacing=discord.SeparatorSpacing.small
            ),
            discord.ui.TextDisplay(warning_message),
            discord.ui.ActionRow(
                discord.ui.Button(
                    label=self.get_label(),
                    style=discord.ButtonStyle.secondary,
                    disabled=True,
                    custom_id="anti_scam_kick_counter"
                )
            )
        )

        self.add_item(self.container)

    def get_label(self):
        if self.kick_count == 1:
            return "kick: 1"
        return f"kicks: {self.kick_count}"


class AntiScamChannel:
    def __init__(self, channel_id, warning_message):
        self.channel_id = channel_id
        self.warning_message = warning_message
        self.kick_count = 0
        self.message = None


anti_scam_channels = {}


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Logged in as {bot.user}")
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Command sync error: {e}")


@bot.tree.command(
    name="create_anti_scam",
    description="Create an anti-scam channel that kicks anyone who sends a message."
)
@app_commands.describe(
    name="The exact name of the channel to create.",
    message="The warning message displayed inside the channel."
)
@app_commands.default_permissions(manage_guild=True)
async def create_anti_scam(
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

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to use this command.",
            ephemeral=True
        )
        return

    guild = interaction.guild

    existing = discord.utils.get(guild.text_channels, name=name)

    if existing:
        await interaction.response.send_message(
            f"A channel named `{name}` already exists.",
            ephemeral=True
        )
        return

    me = guild.me

    if me is None:
        await interaction.response.send_message(
            "I could not determine my server permissions.",
            ephemeral=True
        )
        return

    required = [
        ("Manage Channels", me.guild_permissions.manage_channels),
        ("Kick Members", me.guild_permissions.kick_members)
    ]

    missing = [permission for permission, enabled in required if not enabled]

    if missing:
        await interaction.response.send_message(
            "I am missing these permissions: " + ", ".join(missing),
            ephemeral=True
        )
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        ),
        me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
            manage_channels=True
        )
    }

    try:
        channel = await guild.create_text_channel(
            name=name,
            overwrites=overwrites,
            reason=f"Anti-scam channel created by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to create the channel.",
            ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"Discord returned an error while creating the channel: {e}",
            ephemeral=True
        )
        return

    tracker = AntiScamChannel(
        channel_id=channel.id,
        warning_message=message
    )

    anti_scam_channels[channel.id] = tracker

    view = KickCounterView(message, 0)

    try:
        sent_message = await channel.send(view=view)
        tracker.message = sent_message
    except discord.HTTPException as e:
        await channel.delete(reason="Failed to create anti-scam panel")
        anti_scam_channels.pop(channel.id, None)

        await interaction.response.send_message(
            f"The channel was created, but I could not send the panel: {e}",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Created {channel.mention} successfully.",
        ephemeral=True
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    tracker = anti_scam_channels.get(message.channel.id)

    if tracker is None:
        await bot.process_commands(message)
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

        tracker.kick_count += 1

        if tracker.message:
            try:
                await tracker.message.edit(
                    view=KickCounterView(
                        tracker.warning_message,
                        tracker.kick_count
                    )
                )
            except discord.HTTPException:
                pass

    except discord.Forbidden:
        print(
            f"Could not kick {member} from {message.guild.name}: "
            "insufficient role hierarchy or permissions."
        )

    except discord.HTTPException as e:
        print(f"Kick failed: {e}")


bot.run(TOKEN)
