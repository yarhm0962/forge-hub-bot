import os,asyncio
from datetime import datetime,timedelta,timezone
import discord
from discord import app_commands
from discord.ext import commands

TOKEN=os.getenv("DISCORD_TOKEN")
if not TOKEN: raise RuntimeError("DISCORD_TOKEN environment variable is missing")

intents=discord.Intents.default()
intents.guilds=True
intents.members=True
intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)
created_channels={}

ANTI_SCAM_MESSAGE=("This channel is protected by the server moderation system.\n\n"
"Please do not send messages here. Messages sent in this channel may result in an immediate kick from the server.\n\n"
"If you have read and understood this notice, react with 👍 below.")

MONGODB_URI="mongodb+srv://xyrielzen16_db_user:saisai1324@panelbot.aubckg7.mongodb.net/?appName=PanelBot"
MONGODB_DB_NAME="PanelBot"

try:
    from pymongo import MongoClient
except ImportError as error:
    raise RuntimeError("pymongo is required. Install it with: pip install pymongo") from error

mongo_client=MongoClient(MONGODB_URI,serverSelectionTimeoutMS=10000)
mongo_db=mongo_client[MONGODB_DB_NAME]
insights_collection=mongo_db["server_insights"]
anti_scam_collection=mongo_db["anti_scam_channels"]

def cleanup_events(data):
    cutoff=datetime.now(timezone.utc)-timedelta(days=30)
    for kind in ("joins","leaves"):
        data[kind]=[x for x in data.get(kind,[]) if _valid_recent(x,cutoff)]

def _valid_recent(value,cutoff):
    try:
        return datetime.fromisoformat(value)>=cutoff
    except (TypeError,ValueError):
        return False

async def get_guild_insights(gid):
    def operation():
        data=insights_collection.find_one({"_id":str(gid)})
        if not data:
            data={"_id":str(gid),"joins":[],"leaves":[]}
        cleanup_events(data)
        insights_collection.replace_one({"_id":str(gid)},data,upsert=True)
        return data
    return await asyncio.to_thread(operation)

async def save_guild_insights(gid,data):
    data={
        "_id":str(gid),
        "joins":data.get("joins",[]),
        "leaves":data.get("leaves",[])
    }
    cleanup_events(data)
    await asyncio.to_thread(
        insights_collection.replace_one,
        {"_id":str(gid)},
        data,
        upsert=True
    )

async def get_anti_scam_record(channel_id):
    return await asyncio.to_thread(
        anti_scam_collection.find_one,
        {"_id":str(channel_id)}
    )

async def save_anti_scam_record(record):
    record=dict(record)
    record["_id"]=str(record.get("channel_id"))
    await asyncio.to_thread(
        anti_scam_collection.replace_one,
        {"_id":record["_id"]},
        record,
        upsert=True
    )

async def load_anti_scam_records():
    return await asyncio.to_thread(
        lambda:list(anti_scam_collection.find({}))
    )

async def delete_anti_scam_record(channel_id):
    await asyncio.to_thread(
        anti_scam_collection.delete_one,
        {"_id":str(channel_id)}
    )

class AntiScamPanel(discord.ui.LayoutView):
    def __init__(self,kicks=0):
        super().__init__(timeout=None)
        self.kicks=kicks
        self.container=discord.ui.Container(
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
                "This channel is monitored automatically. Messages sent here "
                "are removed and the sender may be kicked from the server."
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
            if isinstance(item,discord.ui.ActionRow):
                for button in item.children:
                    if isinstance(button,discord.ui.Button):
                        button.label=f"kicks: {self.kicks}"

class InsightsPanel(discord.ui.LayoutView):
    def __init__(self,guild,data):
        super().__init__(timeout=None)
        cleanup_events(data)

        joins=len(data["joins"])
        leaves=len(data["leaves"])
        current=guild.member_count or 0
        net=joins-leaves

        growth=f"+{net:,}" if net>0 else f"{net:,}"
        status=(
            "📈 Growing"
            if net>0
            else "📉 Declining"
            if net<0
            else "➖ Stable"
        )

        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 📊 Server Insights"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"### {guild.name}\n"
                "A clean overview of member activity across the last 30 days."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"👥 **Current Members**\n`{current:,}`"
            ),
            discord.ui.TextDisplay(
                f"🟢 **New Members**\n`{joins:,}` joined during the last 30 days."
            ),
            discord.ui.TextDisplay(
                f"🔴 **Departures**\n`{leaves:,}` left during the last 30 days."
            ),
            discord.ui.TextDisplay(
                f"📈 **Net Change**\n`{growth}` members"
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"**{status}**\n"
                "Join and leave activity is automatically tracked over "
                "a rolling 30-day period."
            )
        )
        self.add_item(self.container)

class PurgePanel(discord.ui.LayoutView):
    def __init__(self,count,deleted):
        super().__init__(timeout=None)
        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 🧹 Messages Cleared"),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"Successfully cleared **{deleted:,}** "
                f"message{'s' if deleted!=1 else ''}."
            ),
            discord.ui.Separator(
                spacing=discord.SeparatorSpacing.small,
                visible=True
            ),
            discord.ui.TextDisplay(
                f"**Requested:** `{count:,}`\n"
                f"**Deleted:** `{deleted:,}`\n"
                "**Channel:** Current channel"
            )
        )
        self.add_item(self.container)

create_group=app_commands.Group(
    name="create",
    description="Create server tools"
)

anti_group=app_commands.Group(
    name="anti",
    description="Anti moderation tools",
    parent=create_group
)

server_group=app_commands.Group(
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
@app_commands.checks.has_permissions(
    manage_channels=True
)
async def anti_scam(interaction:discord.Interaction,name:str):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    me=interaction.guild.me

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
        channel=await interaction.guild.create_text_channel(name)
        panel=AntiScamPanel()
        msg=await channel.send(view=panel)

        try:
            await msg.add_reaction("👍")
        except discord.HTTPException:
            pass

        await save_anti_scam_record({
            "guild_id":interaction.guild.id,
            "channel_id":channel.id,
            "message_id":msg.id,
            "kicks":0
        })

        created_channels[channel.id]={
            "panel":panel,
            "message":msg,
            "guild_id":interaction.guild.id
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
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def server_insights(interaction:discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            "This command can only be used inside a server.",
            ephemeral=True
        )
        return

    data=await get_guild_insights(interaction.guild.id)
    cleanup_events(data)
    await save_guild_insights(
        interaction.guild.id,
        data
    )

    await interaction.response.send_message(
        view=InsightsPanel(
            interaction.guild,
            data
        )
    )

@bot.tree.command(
    name="purge",
    description="Delete recent messages from the current channel"
)
@app_commands.describe(
    count="Number of messages to delete (1-1000)"
)
@app_commands.checks.has_permissions(
    manage_messages=True
)
async def purge(
    interaction:discord.Interaction,
    count:app_commands.Range[int,1,1000]
):
    if not isinstance(
        interaction.channel,
        discord.TextChannel
    ):
        await interaction.response.send_message(
            "This command can only be used in a text channel.",
            ephemeral=True
        )
        return

    me=interaction.guild.me

    if me is None:
        await interaction.response.send_message(
            "I could not verify my permissions.",
            ephemeral=True
        )
        return

    permissions=interaction.channel.permissions_for(me)

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
        deleted=await interaction.channel.purge(
            limit=count,
            bulk=True,
            reason=f"Purge requested by {interaction.user}"
        )

        await interaction.followup.send(
            view=PurgePanel(
                count,
                len(deleted)
            ),
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
async def on_member_join(member):
    data=await get_guild_insights(member.guild.id)
    data["joins"].append(
        datetime.now(timezone.utc).isoformat()
    )
    cleanup_events(data)
    await save_guild_insights(
        member.guild.id,
        data
    )

@bot.event
async def on_member_remove(member):
    data=await get_guild_insights(member.guild.id)
    data["leaves"].append(
        datetime.now(timezone.utc).isoformat()
    )
    cleanup_events(data)
    await save_guild_insights(
        member.guild.id,
        data
    )

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    data=created_channels.get(message.channel.id)

    if data is None:
        return

    member=message.author

    if (
        not isinstance(member,discord.Member)
        or member.guild_permissions.administrator
    ):
        return

    me=message.guild.me

    if (
        me is None
        or not me.guild_permissions.kick_members
        or member.top_role>=me.top_role
    ):
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

        data["panel"].kicks+=1
        data["panel"].update_kicks()

        record=await get_anti_scam_record(
            message.channel.id
        )

        if record:
            record["kicks"]=data["panel"].kicks
            await save_anti_scam_record(record)

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
    stale=[]
    records=await load_anti_scam_records()

    for record in records:
        try:
            cid=int(record["channel_id"])
            mid=int(record["message_id"])
            gid=int(record["guild_id"])
            kicks=int(record.get("kicks",0))
        except (
            KeyError,
            TypeError,
            ValueError
        ):
            stale.append(record.get("_id"))
            continue

        guild=bot.get_guild(gid)

        if guild is None:
            continue

        channel=guild.get_channel(cid)

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            stale.append(record.get("_id"))
            continue

        panel=AntiScamPanel(kicks)

        try:
            msg=await channel.fetch_message(mid)
        except discord.NotFound:
            stale.append(record.get("_id"))
            continue
        except discord.Forbidden:
            created_channels[cid]={
                "panel":panel,
                "message":None,
                "guild_id":gid
            }
            continue
        except discord.HTTPException:
            continue

        try:
            await msg.edit(view=panel)
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException
        ):
            pass

        created_channels[cid]={
            "panel":panel,
            "message":msg,
            "guild_id":gid
        }

    for key in stale:
        if key:
            await delete_anti_scam_record(key)

@bot.event
async def on_ready():
    try:
        synced=await bot.tree.sync()
        await restore_anti_scam_channels()

        print(
            f"Logged in as {bot.user} ({bot.user.id})"
        )
        print(
            f"Synced {len(synced)} command(s)"
        )
        print(
            f"Restored {len(created_channels)} anti-scam channel(s)"
        )

    except Exception as error:
        print(
            f"Startup error: {error}"
        )

@anti_scam.error
async def anti_scam_error(
    interaction,
    error
):
    message=(
        "You need the Manage Channels permission to use this command."
        if isinstance(
            error,
            app_commands.MissingPermissions
        )
        else f"Command error: {error}"
    )

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
    interaction,
    error
):
    message=(
        "You need the Manage Server permission to use this command."
        if isinstance(
            error,
            app_commands.MissingPermissions
        )
        else f"Command error: {error}"
    )

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
    interaction,
    error
):
    message=(
        "You need the Manage Messages permission to use this command."
        if isinstance(
            error,
            app_commands.MissingPermissions
        )
        else f"Command error: {error}"
    )

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
    await asyncio.to_thread(
        mongo_client.admin.command,
        "ping"
    )

    print("MongoDB connection established.")

    while True:
        try:
            await bot.start(TOKEN)
            break

        except discord.HTTPException as error:
            retry_after=getattr(
                error,
                "retry_after",
                30
            )

            print(
                f"Discord connection error: {error}"
            )
            print(
                f"Retrying in {retry_after:.1f} seconds..."
            )

            await asyncio.sleep(
                retry_after
            )

        except discord.LoginFailure:
            print(
                "Invalid Discord bot token."
            )
            break

        except Exception as error:
            print(
                f"Bot error: {error}"
            )
            await asyncio.sleep(30)

bot.tree.add_command(create_group)
bot.tree.add_command(server_group)

asyncio.run(start_bot())
