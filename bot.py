import os
import base64
import asyncio
import json
import re
import io
import ipaddress
import time
import urllib .error
import urllib .request
from datetime import datetime ,timedelta ,timezone
from pathlib import Path

import discord
from discord import app_commands
from discord .ext import commands
from pymongo import MongoClient ,ReturnDocument
from pymongo .errors import PyMongoError

TOKEN =os .getenv ("DISCORD_TOKEN")
MONGODB_URI =os .getenv ("MONGODB_URI")
MONGODB_DATABASE =os .getenv ("MONGODB_DATABASE","PanelBot")
PASTEFY_API_TOKEN =os .getenv ("PASTEFY_API_TOKEN")
PASTEFY_MAX_BYTES =5 *1024 *1024

if not TOKEN :
    raise RuntimeError ("DISCORD_TOKEN environment variable is missing")

if not MONGODB_URI :
    raise RuntimeError ("MONGODB_URI environment variable is missing")

mongo_client =MongoClient (
MONGODB_URI ,
serverSelectionTimeoutMS =10000 ,
connectTimeoutMS =10000 ,
socketTimeoutMS =10000 ,
)

mongo_db =mongo_client [MONGODB_DATABASE ]
insights_collection =mongo_db ["server_insights"]
anti_scam_collection =mongo_db ["anti_scam_channels"]
reaction_roles_collection =mongo_db ["reaction_roles"]
member_snapshots_collection =mongo_db ["member_snapshots"]

intents =discord .Intents .default ()
intents .guilds =True
intents .members =True
intents .message_content =True

bot =commands .Bot (command_prefix =("!","."),intents =intents )

created_channels ={}
reaction_role_cache ={}
ready_once =False


ANTI_SCAM_TITLE ="## 🛡️ Anti-Scam Protection"
ANTI_SCAM_BODY =(
"This channel is reserved for suspicious or fake social-media spam.\n"
"**Do not send messages here.** Messages are removed automatically and the sender "
"may be kicked."
)


def utc_now ():
    return datetime .now (timezone .utc )


def iso_now ():
    return utc_now ().isoformat ()


def parse_datetime (value ):
    if not isinstance (value ,str ):
        return None
    try :
        result =datetime .fromisoformat (value )
        if result .tzinfo is None :
            result =result .replace (tzinfo =timezone .utc )
        return result .astimezone (timezone .utc )
    except ValueError :
        return None


def cleanup_events_sync (document ):
    cutoff =utc_now ()-timedelta (days =30 )
    joins =[]
    leaves =[]
    for event in document .get ("joins",[]):
        value =event .get ("timestamp")if isinstance (event ,dict )else event
        dt =parse_datetime (value )
        if dt and dt >=cutoff :
            joins .append (value )
    for event in document .get ("leaves",[]):
        value =event .get ("timestamp")if isinstance (event ,dict )else event
        dt =parse_datetime (value )
        if dt and dt >=cutoff :
            leaves .append (value )
    document ["joins"]=joins
    document ["leaves"]=leaves
    document .setdefault ("total_joins",0 )
    document .setdefault ("total_leaves",0 )
    return document


def ensure_insights_document_sync (guild_id ):
    guild_id =int (guild_id )
    insights_collection .update_one (
    {"_id":guild_id },
    {
    "$setOnInsert":{
    "joins":[],
    "leaves":[],
    "total_joins":0 ,
    "total_leaves":0 ,
    "created_at":iso_now (),
    }
    },
    upsert =True ,
    )


def cleanup_insights_sync (guild_id ):
    guild_id =int (guild_id )
    ensure_insights_document_sync (guild_id )
    cutoff =(
    utc_now ()-timedelta (days =30 )
    ).isoformat ()
    insights_collection .update_one (
    {"_id":guild_id },
    {
    "$pull":{
    "joins":{"$lt":cutoff },
    "leaves":{"$lt":cutoff },
    }
    },
    )


def get_guild_insights_sync (guild_id ):
    guild_id =int (guild_id )
    cleanup_insights_sync (guild_id )
    document =insights_collection .find_one ({"_id":guild_id })
    if not document :
        ensure_insights_document_sync (guild_id )
        document =insights_collection .find_one ({"_id":guild_id })
    return cleanup_events_sync (document or {
    "_id":guild_id ,
    "joins":[],
    "leaves":[],
    "total_joins":0 ,
    "total_leaves":0 ,
    })


def record_member_events_sync (guild_id ,join_count =0 ,leave_count =0 ):
    guild_id =int (guild_id )
    join_count =max (0 ,int (join_count ))
    leave_count =max (0 ,int (leave_count ))
    if join_count ==0 and leave_count ==0 :
        return get_guild_insights_sync (guild_id )
    ensure_insights_document_sync (guild_id )
    now =iso_now ()
    update ={
    "$inc":{
    "total_joins":join_count ,
    "total_leaves":leave_count ,
    },
    "$push":{},
    }
    if join_count :
        update ["$push"]["joins"]={"$each":[now ]*join_count }
    if leave_count :
        update ["$push"]["leaves"]={"$each":[now ]*leave_count }
    insights_collection .update_one ({"_id":guild_id },update )
    cleanup_insights_sync (guild_id )
    return insights_collection .find_one ({"_id":guild_id })


def add_member_event_sync (guild_id ,event_type ):
    if event_type =="join":
        return record_member_events_sync (guild_id ,join_count =1 )
    return record_member_events_sync (guild_id ,leave_count =1 )


def get_member_snapshot_sync (guild_id ):
    document =member_snapshots_collection .find_one ({"_id":int (guild_id )})
    if not document :
        return None
    return {int (member_id )for member_id in document .get ("member_ids",[])}


def save_member_snapshot_sync (guild_id ,member_ids ):
    member_snapshots_collection .replace_one (
    {"_id":int (guild_id )},
    {
    "_id":int (guild_id ),
    "member_ids":[int (member_id )for member_id in member_ids ],
    "updated_at":iso_now (),
    },
    upsert =True ,
    )


def add_member_to_snapshot_sync (guild_id ,member_id ):
    member_snapshots_collection .update_one (
    {"_id":int (guild_id )},
    {
    "$addToSet":{"member_ids":int (member_id )},
    "$set":{"updated_at":iso_now ()},
    },
    upsert =True ,
    )


def remove_member_from_snapshot_sync (guild_id ,member_id ):
    member_snapshots_collection .update_one (
    {"_id":int (guild_id )},
    {
    "$pull":{"member_ids":int (member_id )},
    "$set":{"updated_at":iso_now ()},
    },
    )


def reconcile_member_snapshot_sync (guild_id ,current_member_ids ):
    guild_id =int (guild_id )
    current ={int (member_id )for member_id in current_member_ids }
    previous =get_member_snapshot_sync (guild_id )
    if previous is None :
        save_member_snapshot_sync (guild_id ,current )
        return set(),set(),True

    joined =current -previous
    left =previous -current

    if joined or left :
        record_member_events_sync (
        guild_id ,
        join_count =len (joined ),
        leave_count =len (left ),
        )

    save_member_snapshot_sync (guild_id ,current )
    return joined ,left ,False


def list_anti_scam_sync ():
    return list (anti_scam_collection .find ({}))


def save_anti_scam_sync (record ):
    channel_id =int (record ["channel_id"])
    data =dict (record )
    data ["_id"]=channel_id
    anti_scam_collection .replace_one (
    {"_id":channel_id },
    data ,
    upsert =True ,
    )


def delete_anti_scam_sync (channel_id ):
    anti_scam_collection .delete_one ({"_id":int (channel_id )})


def increment_anti_scam_sync (channel_id ,kicked ,guild_id =None ):
    increments ={"violations":1 }
    if kicked :
        increments ["kicks"]=1
    set_on_insert ={
    "channel_id":int (channel_id ),
    "kicks":0 ,
    "violations":0 ,
    }
    if guild_id is not None :
        set_on_insert ["guild_id"]=int (guild_id )
    return anti_scam_collection .find_one_and_update (
    {"_id":int (channel_id )},
    {
    "$inc":increments ,
    "$setOnInsert":set_on_insert ,
    },
    upsert =True ,
    return_document =ReturnDocument .AFTER ,
    )


def save_reaction_role_sync (record ):
    message_id =int (record ["message_id"])
    data =dict (record )
    data ["_id"]=message_id
    reaction_roles_collection .replace_one (
    {"_id":message_id },
    data ,
    upsert =True ,
    )


def get_reaction_role_sync (message_id ):
    return reaction_roles_collection .find_one ({"_id":int (message_id )})


def delete_reaction_role_sync (message_id ):
    reaction_roles_collection .delete_one ({"_id":int (message_id )})


def list_reaction_roles_sync ():
    return list (reaction_roles_collection .find ({}))



async def mongo_call (function ,*args ):
    return await asyncio .to_thread (function ,*args )


def make_container (*items ,accent_color =None ):
    container =discord .ui .Container (*items )
    if accent_color is not None :
        container .accent_color =accent_color
    return container


def make_text (content ):
    return discord .ui .TextDisplay (content )


def make_separator ():
    return discord .ui .Separator (
    spacing =discord .SeparatorSpacing .small ,
    visible =True ,
    )


class AntiScamView (discord .ui .LayoutView ):
    def __init__ (self ,kicks =0 ,violations =0 ):
        super ().__init__ (timeout =None )
        self .kicks =int (kicks )
        self .violations =int (violations )

        self .kick_button =discord .ui .Button (
        label =f"{self .kicks :,} kicks",
        style =discord .ButtonStyle .danger ,
        emoji ="🛡️",
        disabled =True ,
        )

        self .violation_button =discord .ui .Button (
        label =f"{self .violations :,} blocked",
        style =discord .ButtonStyle .secondary ,
        emoji ="🚫",
        disabled =True ,
        )

        status =discord .ui .Section (
        make_text ("### 🟢 Protection is active"),
        make_text (
        "This channel is monitored continuously. Messages are removed and "
        "members who can be moderated are kicked automatically."
        ),
        accessory =self .kick_button ,
        )

        self .add_item (
        make_container (
        make_text ("## 🛡️ Anti-Scam Protection"),
        make_text ("Automatic enforcement for this protected channel."),
        make_separator (),
        status ,
        make_separator (),
        make_text (
        "### Channel rules\n"
        "• Do not send messages in this channel.\n"
        "• Messages are removed automatically.\n"
        "• Moderation is applied when the bot has permission.\n"
        "• Administrators are not automatically kicked."
        ),
        make_separator (),
        discord .ui .ActionRow (self .violation_button ),
        accent_color =0xED4245 ,
        )
        )

    def update_stats (self ,kicks =None ,violations =None ):
        if kicks is not None :
            self .kicks =int (kicks )
        if violations is not None :
            self .violations =int (violations )
        self .kick_button .label =f"{self .kicks :,} kicks"
        self .violation_button .label =f"{self .violations :,} blocked"

    def update_kicks (self ,kicks =None ):
        self .update_stats (kicks =kicks )


class InsightsView (discord .ui .LayoutView ):
    def __init__ (self ,guild ,document ):
        super ().__init__ (timeout =None )
        joins =list (document .get ("joins",[]))
        leaves =list (document .get ("leaves",[]))
        net =len (joins )-len (leaves )
        lifetime_joins =int (document .get ("total_joins",0 ))
        lifetime_leaves =int (document .get ("total_leaves",0 ))
        lifetime_net =lifetime_joins -lifetime_leaves
        status ="📈 Growing"if net >0 else "📉 Declining"if net <0 else "➖ Stable"
        net_text =f"+{net :,}"if net >0 else f"{net :,}"
        lifetime_net_text =f"+{lifetime_net :,}"if lifetime_net >0 else f"{lifetime_net :,}"

        self .add_item (
        make_container (
        make_text (f"## 📊 Server Insights · {guild .name }"),
        make_separator (),
        make_text (
        f"**Current Members:** `{guild .member_count or 0 :,}`   **Status:** {status }\n"
        f"**30-Day Net:** `{net_text }`   **Lifetime Net:** `{lifetime_net_text }`"
        ),
        make_separator (),
        make_text (
        "### 🗓️ Last 30 Days\n"
        f"**Joined:** `{len (joins ):,}`   **Left:** `{len (leaves ):,}`   **Net:** `{net_text }`\n"
        "Activity automatically expires after 30 days."
        ),
        make_separator (),
        make_text (
        "### ♾️ Lifetime Totals\n"
        f"**Total Joins:** `{lifetime_joins :,}`\n"
        f"**Total Departures:** `{lifetime_leaves :,}`\n"
        f"**Lifetime Net:** `{lifetime_net_text }`"
        ),
        make_separator (),
        make_text (
        "💾 **MongoDB Persistence**\n"
        "Member activity is stored persistently. The activity list uses a rolling 30-day window, while lifetime totals remain available."
        ),
        )
        )


class PastefyResultView (discord .ui .LayoutView ):
    def __init__ (self ,filename ,raw_url ):
        super ().__init__ (timeout =None )
        self .add_item (
        make_container (
        make_text ("## 📋 Pastefy Upload Complete"),
        make_separator (),
        make_text (
        f"**File:** `{discord .utils .escape_markdown (filename )}`\n"
        f"**Raw URL:** {raw_url }"
        ),
        make_separator (),
        discord .ui .ActionRow (
        discord .ui .Button (
        label ="View Raw Result",
        style =discord .ButtonStyle .link ,
        url =raw_url ,
        )
        ),
        )
        )


def create_pastefy_paste_sync (filename ,content ,token ):
    payload ={
    "title":filename ,
    "content":content ,
    "visibility":"UNLISTED",
    "encrypted":False ,
    "type":"PASTE",
    }
    data =json .dumps (payload ,ensure_ascii =False ).encode ("utf-8")
    auth =token .strip ()
    if not auth .lower ().startswith ("bearer "):
        auth =f"Bearer {auth }"
    request =urllib .request .Request (
    "https://pastefy.app/api/v2/paste",
    data =data ,
    headers ={
    "Authorization":auth ,
    "Content-Type":"application/json",
    "Accept":"application/json",
    "User-Agent":"PanelBot/1.0",
    },
    method ="POST",
    )
    try :
        with urllib .request .urlopen (request ,timeout =30 )as response :
            body =response .read ().decode ("utf-8")
    except urllib .error .HTTPError as error :
        try :
            detail =error .read ().decode ("utf-8",errors ="replace")
        except Exception :
            detail =""
        raise RuntimeError (f"Pastefy API returned HTTP {error .code }: {detail [:500 ]}")from error
    except urllib .error .URLError as error :
        raise RuntimeError (f"Could not connect to Pastefy: {error .reason }")from error
    try :
        result =json .loads (body )
    except json .JSONDecodeError as error :
        raise RuntimeError ("Pastefy returned an invalid response.")from error
    paste =result .get ("paste")if isinstance (result ,dict )else None
    raw_url =paste .get ("raw_url")if isinstance (paste ,dict )else None
    if not raw_url or not isinstance (raw_url ,str ):
        raise RuntimeError ("Pastefy did not return a raw URL.")
    return raw_url


@bot .command (name ="pastefy")
async def pastefy_command (ctx :commands .Context ):
    attachments =list (ctx .message .attachments )
    if len (attachments )!=1 :
        await ctx .send ("Upload exactly one `.lua` or `.txt` file with `.pastefy`.")
        return
    attachment =attachments [0 ]
    filename =os .path .basename (attachment .filename or "")
    extension =os .path .splitext (filename )[1 ].lower ()
    if extension not in {".lua",".luau",".txt"}:
        await ctx .send ("Only `.lua`, `.luau`, and `.txt` files are supported.")
        return
    if not PASTEFY_API_TOKEN :
        await ctx .send ("Pastefy is not configured. Set the `PASTEFY_API_TOKEN` environment variable.")
        return
    if attachment .size is not None and attachment .size >PASTEFY_MAX_BYTES :
        await ctx .send ("That file is too large. The maximum size is 5 MB.")
        return
    status =await ctx .send ("⏳ Uploading your file to Pastefy...")
    try :
        raw =await attachment .read ()
        if len (raw )>PASTEFY_MAX_BYTES :
            await status .edit (content ="That file is too large. The maximum size is 5 MB.")
            return
        try :
            content =raw .decode ("utf-8-sig")
        except UnicodeDecodeError :
            await status .edit (content ="The uploaded file must be valid UTF-8 text.")
            return
        if not content :
            await status .edit (content ="The uploaded file is empty.")
            return
        raw_url =await asyncio .to_thread (
        create_pastefy_paste_sync ,
        filename ,
        content ,
        PASTEFY_API_TOKEN ,
        )
        await status .edit (content =None ,view =PastefyResultView (filename ,raw_url ))
    except Exception as error :
        await status .edit (content =f"Pastefy upload failed: {error }")




































































class CustomImageResultView(discord.ui.LayoutView):
    def __init__(self,filename):
        super().__init__(timeout=None)
        safe_filename=discord.utils.escape_markdown(filename)
        self.add_item(
            make_container(
                make_text("## 🎨 Custom Server Profile"),
                make_text("The bot's server-specific avatar has been updated successfully."),
                make_separator(),
                make_text(
                    f"**Image:** `{safe_filename}`\n"
                    "**Scope:** `This server only`\n"
                    "**Status:** `Active`"
                ),
                make_separator(),
                make_text("✨ Other servers keep the bot's existing profile."),
                accent_color=0x5865F2,
            )
        )


def set_guild_bot_avatar_sync(guild_id,image_data_uri):
    payload=json.dumps({"avatar":image_data_uri}).encode("utf-8")
    request=urllib.request.Request(
        f"https://discord.com/api/v10/guilds/{int(guild_id)}/members/@me",
        data=payload,
        headers={
            "Authorization":f"Bot {TOKEN}",
            "Content-Type":"application/json",
            "User-Agent":"PanelBot/1.0",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request,timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as error:
        try:
            detail=error.read().decode("utf-8",errors="replace")
        except Exception:
            detail=""
        raise RuntimeError(f"Discord API returned HTTP {error.code}: {detail[:700]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not connect to Discord: {error.reason}") from error


class CmdsView(discord.ui.LayoutView):
    def __init__(self,ctx,prefix_entries,slash_entries,page=0):
        super().__init__(timeout=300)
        self.ctx=ctx
        self.prefix_entries=prefix_entries
        self.slash_entries=slash_entries
        self.page=max(0,page)
        self.previous_button=discord.ui.Button(label="Previous",style=discord.ButtonStyle.secondary,emoji="◀️")
        self.next_button=discord.ui.Button(label="Next",style=discord.ButtonStyle.primary,emoji="▶️")
        self.previous_button.callback=self.previous_page
        self.next_button.callback=self.next_page
        self.render()

    @property
    def max_page(self):
        prefix_pages=(len(self.prefix_entries)+3)//4
        slash_pages=(len(self.slash_entries)+3)//4
        return max(1,prefix_pages,slash_pages)

    def render(self):
        self.clear_items()
        start=self.page*4
        prefix_page=self.prefix_entries[start:start+4]
        slash_page=self.slash_entries[start:start+4]
        prefix_text="\n\n".join(f"**.{name}**\n> {discord.utils.escape_markdown(description or 'Prefix command')}" for name,description in prefix_page)
        slash_text="\n\n".join(f"**/{name}**\n> {discord.utils.escape_markdown(description or 'Slash command')}" for name,description in slash_page)
        sections=[]
        if prefix_text:
            sections.extend([make_text("### ⚡ Prefix Commands"),make_text(prefix_text)])
        if slash_text:
            if sections:
                sections.append(make_separator())
            sections.extend([make_text("### ◆ Slash Commands"),make_text(slash_text)])
        if not sections:
            sections.append(make_text("No commands are currently available."))
        self.previous_button.disabled=self.page<=0
        self.next_button.disabled=self.page>=self.max_page-1
        self.add_item(make_container(
            make_text("## 🧭 Command Center"),
            make_text(f"Browse every available bot command.\n**Page {self.page+1}/{self.max_page}** · **{len(self.prefix_entries)+len(self.slash_entries)} total commands**"),
            make_separator(),
            *sections,
            make_separator(),
            discord.ui.ActionRow(self.previous_button,self.next_button),
            accent_color=0x5865F2,
        ))

    async def previous_page(self,interaction):
        if interaction.user.id!=self.ctx.author.id:
            await interaction.response.send_message("This command list belongs to the user who opened it.",ephemeral=True)
            return
        if self.page>0:
            self.page-=1
            self.render()
        await interaction.response.edit_message(view=self)

    async def next_page(self,interaction):
        if interaction.user.id!=self.ctx.author.id:
            await interaction.response.send_message("This command list belongs to the user who opened it.",ephemeral=True)
            return
        if self.page<self.max_page-1:
            self.page+=1
            self.render()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        self.previous_button.disabled=True
        self.next_button.disabled=True
        message=getattr(self,"message",None)
        if message:
            try:
                self.render()
                await message.edit(view=self)
            except (discord.HTTPException,discord.NotFound):
                pass














class PurgeView (discord .ui .LayoutView ):
    def __init__ (self ,requested ,deleted ,channel ):
        super ().__init__ (timeout =None )
        self .add_item (
        make_container (
        make_text ("## 🧹 Purge Complete"),
        make_separator (),
        make_text ("The cleanup finished successfully."),
        make_separator (),
        make_text (
        f"### 📋 Results\n"
        f"**Requested:** `{requested :,}`\n"
        f"**Deleted:** `{deleted :,}`\n"
        f"**Channel:** {channel .mention }"
        ),
        )
        )

class MessageIdModal (discord .ui .Modal ,title ="Set Target Message"):
    message_id =discord .ui .TextInput (
    label ="Message ID",
    placeholder ="Paste the Discord message ID here",
    required =True ,
    max_length =30 ,
    )

    def __init__ (self ,parent_view ):
        super ().__init__ ()
        self .parent_view =parent_view

    async def on_submit (self ,interaction ):
        value =str (self .message_id .value ).strip ()
        if not value .isdigit ():
            await interaction .response .send_message ("❌ That is not a valid Discord message ID.",ephemeral =True )
            return
        self .parent_view .message_id =int (value )
        self .parent_view .refresh ()
        await interaction .response .send_message (f"✅ Target message set to `{value }`.",ephemeral =True )


class EmojiModal (discord .ui .Modal ,title ="Set Reaction Emojis"):
    emoji_1 =discord .ui .TextInput (label ="Emoji 1",placeholder ="⭐ or <:custom:123456789>",required =True ,max_length =100 )
    emoji_2 =discord .ui .TextInput (label ="Emoji 2",placeholder ="Optional",required =False ,max_length =100 )
    emoji_3 =discord .ui .TextInput (label ="Emoji 3",placeholder ="Optional",required =False ,max_length =100 )
    emoji_4 =discord .ui .TextInput (label ="Emoji 4",placeholder ="Optional",required =False ,max_length =100 )
    emoji_5 =discord .ui .TextInput (label ="Emoji 5",placeholder ="Optional",required =False ,max_length =100 )

    def __init__ (self ,parent_view ):
        super ().__init__ ()
        self .parent_view =parent_view

    async def on_submit (self ,interaction ):
        values =[
        str (self .emoji_1 .value ).strip (),
        str (self .emoji_2 .value ).strip (),
        str (self .emoji_3 .value ).strip (),
        str (self .emoji_4 .value ).strip (),
        str (self .emoji_5 .value ).strip (),
        ]
        values =[value for value in values if value ]
        if not values :
            await interaction .response .send_message ("❌ Add at least one emoji.",ephemeral =True )
            return
        if len (values )>5 :
            await interaction .response .send_message ("❌ You can use a maximum of 5 emojis.",ephemeral =True )
            return
        self .parent_view .emojis =values
        self .parent_view .refresh ()
        await interaction .response .send_message (
        f"✅ {len (values )} reaction emoji{'s'if len (values )!=1 else ''} saved.",
        ephemeral =True ,
        )

class RolePickerView (discord .ui .View ):
    def __init__ (self ,parent_view ):
        super ().__init__ (timeout =300 )
        self .parent_view =parent_view

        self .select =discord .ui .RoleSelect (
        placeholder ="Select up to 5 roles",
        min_values =1 ,
        max_values =5 ,
        )
        self .select .callback =self .role_selected
        self .add_item (self .select )

    async def role_selected (self ,interaction ):
        try :
            if interaction .guild is None :
                await interaction .response .send_message (
                "This selector can only be used inside a server.",
                ephemeral =True ,
                )
                return

            selected =interaction .data .get ("values",[])if interaction .data else []
            resolved =[]

            for role_id in selected [:5 ]:
                role =interaction .guild .get_role (int (role_id ))
                if role is not None :
                    resolved .append (role )

            if not resolved :
                await interaction .response .send_message (
                "No valid roles were selected.",
                ephemeral =True ,
                )
                return

            self .parent_view .roles =resolved
            self .parent_view .refresh ()

            await interaction .response .send_message (
            "Selected: "+", ".join (role .mention for role in resolved ),
            ephemeral =True ,
            )
        except Exception as error :
            if interaction .response .is_done ():
                await interaction .followup .send (
                f"Could not save the selected roles: {error }",
                ephemeral =True ,
                )
            else :
                await interaction .response .send_message (
                f"Could not save the selected roles: {error }",
                ephemeral =True ,
                )


class ReactionRoleSetupView (discord .ui .LayoutView ):
    def __init__ (self ,author_id ):
        super ().__init__ (timeout =900 )
        self .author_id =author_id
        self .message_id =None
        self .roles =[]
        self .emojis =[]

        self .message_button =discord .ui .Button (label ="Message",style =discord .ButtonStyle .secondary ,emoji ="🆔")
        self .message_button .callback =self .message_id_callback
        self .role_button =discord .ui .Button (label ="Roles",style =discord .ButtonStyle .secondary ,emoji ="🎭")
        self .role_button .callback =self .role_callback
        self .emoji_button =discord .ui .Button (label ="Emojis",style =discord .ButtonStyle .secondary ,emoji ="✨")
        self .emoji_button .callback =self .emoji_callback
        self .save_button =discord .ui .Button (label ="Save Setup",style =discord .ButtonStyle .success ,emoji ="✅")
        self .save_button .callback =self .save_callback

        row =discord .ui .ActionRow ()
        row .add_item (self .message_button )
        row .add_item (self .role_button )
        row .add_item (self .emoji_button )
        row .add_item (self .save_button )

        self .status =make_text (self .status_text ())
        self .add_item (
        make_container (
        make_text ("## 🎭 Reaction Role Setup"),
        make_separator (),
        make_text (
        "Create a reaction-role message in a few simple steps.\n"
        "Set the message, choose up to 5 roles, then match each role with an emoji."
        ),
        make_separator (),
        make_text ("### ⚙️ Configuration"),
        self .status ,
        make_separator (),
        make_text ("### 🔧 Controls"),
        row ,
        )
        )

    def refresh (self ):
        self .status .content =self .status_text ()

    def status_text (self ):
        message =f"`{self .message_id }`"if self .message_id else "`Not set`"
        roles =", ".join (role .mention for role in self .roles )if self .roles else "`Not set`"
        emojis ="  ".join (self .emojis )if self .emojis else "`Not set`"
        pair_count =min (len (self .roles ),len (self .emojis ))
        return (
        f"**Target Message:** {message }\n"
        f"**Roles:** {roles }\n"
        f"**Emojis:** {emojis }\n"
        f"**Pairs Ready:** `{pair_count }/5`"
        )

    async def check_author (self ,interaction ):
        if interaction .user .id !=self .author_id :
            await interaction .response .send_message (
            "❌ Only the person who started this setup can use these controls.",
            ephemeral =True ,
            )
            return False
        return True

    async def message_id_callback (self ,interaction ):
        if not await self .check_author (interaction ):
            return
        await interaction .response .send_modal (MessageIdModal (self ))

    async def role_callback (self ,interaction ):
        if not await self .check_author (interaction ):
            return
        picker =RolePickerView (self )
        await interaction .response .send_message (
        "🎭 **Select Roles**\nChoose up to 5 roles. Their order will be matched with the emoji order.",
        view =picker ,
        ephemeral =True ,
        )

    async def emoji_callback (self ,interaction ):
        if not await self .check_author (interaction ):
            return
        await interaction .response .send_modal (EmojiModal (self ))

    async def save_callback (self ,interaction ):
        if not await self .check_author (interaction ):
            return

        if interaction .guild is None :
            await interaction .response .send_message (
            "This setup can only be saved inside a server.",
            ephemeral =True ,
            )
            return

        if self .message_id is None :
            await interaction .response .send_message (
            "Set the target message first.",
            ephemeral =True ,
            )
            return

        if not self .roles :
            await interaction .response .send_message (
            "Select at least one role.",
            ephemeral =True ,
            )
            return

        if not self .emojis :
            await interaction .response .send_message (
            "Set at least one emoji.",
            ephemeral =True ,
            )
            return

        if len (self .roles )!=len (self .emojis ):
            await interaction .response .send_message (
            "The number of roles and emojis must match.",
            ephemeral =True ,
            )
            return

        await interaction .response .defer (ephemeral =True ,thinking =True )

        try :
            message =await find_message_in_guild (interaction .guild ,self .message_id )
            if message is None :
                await interaction .followup .send (
                "I could not find that message in this server.",
                ephemeral =True ,
                )
                return

            me =interaction .guild .me
            if me is None or not me .guild_permissions .manage_roles or not me .guild_permissions .add_reactions :
                await interaction .followup .send (
                "I need Manage Roles and Add Reactions permissions.",
                ephemeral =True ,
                )
                return

            pairs =[]
            for role ,emoji in zip (self .roles ,self .emojis ):
                if role .is_default ()or role .managed :
                    raise RuntimeError (f"The role {role .name} cannot be assigned by the bot.")
                if role >=me .top_role :
                    raise RuntimeError (f"The role {role .name} is higher than or equal to my highest role.")
                if not str (emoji ).strip ():
                    raise RuntimeError ("Every selected role must have an emoji.")
                pairs .append ({
                "emoji":str (emoji ).strip (),
                "role_id":role .id ,
                "role_name":role .name ,
                })

            record={
            "message_id":message .id ,
            "channel_id":message .channel.id ,
            "guild_id":interaction .guild.id ,
            "pairs":pairs ,
            "updated_at":iso_now (),
            }

            await mongo_call (save_reaction_role_sync,record )
            reaction_role_cache [message .id ]=record

            failed=[]
            for pair in pairs :
                try :
                    await message .add_reaction (pair ["emoji"])
                except (discord .Forbidden ,discord .HTTPException ):
                    failed .append (pair ["emoji"])

            await message .edit (view =ReactionRoleMessageView (pairs ))

            result="✅ Reaction roles saved and will remain active after bot restarts."
            if failed :
                result +="\n⚠️ Could not add: "+", ".join (failed )
            await interaction .followup .send (result ,ephemeral =True )
        except PyMongoError as error :
            await interaction .followup .send (
            f"MongoDB error while saving reaction roles: {error }",
            ephemeral =True ,
            )
        except (discord .Forbidden ,discord .HTTPException )as error :
            await interaction .followup .send (
            f"Discord error while creating reaction roles: {error }",
            ephemeral =True ,
            )
        except Exception as error :
            await interaction .followup .send (
            f"Could not save reaction roles: {error }",
            ephemeral =True ,
            )


class ReactionRoleMessageView (discord .ui .LayoutView ):
    def __init__ (self ,pairs ):
        super ().__init__ (timeout =None )
        lines =[f"`{index }`  {pair ['emoji']}  **{pair ['role_name']}**"for index ,pair in enumerate (pairs ,1 )]
        self .add_item (
        make_container (
        make_text ("## 🎭 Reaction Roles"),
        make_separator (),
        make_text (
        "React with the emoji beside a role to receive it.\n"
        "Remove your reaction to give the role back."
        ),
        make_separator (),
        make_text ("### Available Roles\n"+"\n".join (lines )),
        make_separator (),
        make_text ("✨ **Automatic:** Role changes happen instantly after you react."),
        )
        )

async def find_message_in_guild (guild ,message_id ):
    message_id =int (message_id )

    for channel in guild .text_channels :
        try :
            return await channel .fetch_message (message_id )
        except discord .NotFound :
            continue
        except discord .Forbidden :
            continue
        except discord .HTTPException :
            continue

    return None


async def restore_reaction_roles ():
    records =await mongo_call (list_reaction_roles_sync )
    stale =[]

    for record in records :
        try :
            guild_id =int (record ["guild_id"])
            channel_id =int (record ["channel_id"])
            message_id =int (record ["message_id"])
            pairs =record ["pairs"]
        except (KeyError ,TypeError ,ValueError ):
            stale .append (record .get ("_id"))
            continue

        guild =bot .get_guild (guild_id )
        if guild is None :
            continue

        channel =guild .get_channel (channel_id )
        if not isinstance (channel ,discord .TextChannel ):
            stale .append (message_id )
            continue

        try :
            message =await channel .fetch_message (message_id )
        except discord .NotFound :
            stale .append (message_id )
            continue
        except discord .Forbidden :
            continue
        except discord .HTTPException :
            continue

        valid_pairs =[]
        for pair in pairs :
            try :
                emoji =str (pair ["emoji"])
                role_id =int (pair ["role_id"])
                role =guild .get_role (role_id )
            except (KeyError ,TypeError ,ValueError ):
                continue

            if role is None :
                continue

            valid_pairs .append (
            {
            "emoji":emoji ,
            "role_id":role .id ,
            "role_name":role .name ,
            }
            )

            try :
                await message .add_reaction (emoji )
            except (discord .Forbidden ,discord .HTTPException ):
                pass

        if not valid_pairs :
            stale .append (message_id )
            continue

        record ["pairs"]=valid_pairs
        reaction_role_cache [message_id ]=record

        try :
            await message .edit (
            view =ReactionRoleMessageView (valid_pairs )
            )
        except (discord .Forbidden ,discord .NotFound ,discord .HTTPException ):
            pass

    for message_id in stale :
        if message_id is not None :
            await mongo_call (delete_reaction_role_sync ,message_id )


@bot .event
async def on_raw_reaction_add (payload ):
    if payload .guild_id is None or payload .user_id ==bot .user .id :
        return

    record =reaction_role_cache .get (payload .message_id )
    if record is None :
        record =await mongo_call (get_reaction_role_sync ,payload .message_id )
        if record :
            reaction_role_cache [payload .message_id ]=record

    if not record :
        return

    emoji_value =str (payload .emoji )

    for pair in record .get ("pairs",[]):
        if str (pair .get ("emoji"))!=emoji_value :
            continue

        guild =bot .get_guild (payload .guild_id )
        if guild is None :
            return

        role =guild .get_role (int (pair ["role_id"]))
        member =guild .get_member (payload .user_id )

        if role is None or member is None :
            return

        if role .is_default ()or role .managed :
            return

        me =guild .me
        if me is None or role >=me .top_role :
            return

        try :
            await member .add_roles (
            role ,
            reason ="Reaction role",
            )
        except (discord .Forbidden ,discord .HTTPException ):
            pass
        return


@bot .event
async def on_raw_reaction_remove (payload ):
    if payload .guild_id is None or payload .user_id ==bot .user .id :
        return

    record =reaction_role_cache .get (payload .message_id )
    if record is None :
        record =await mongo_call (get_reaction_role_sync ,payload .message_id )
        if record :
            reaction_role_cache [payload .message_id ]=record

    if not record :
        return

    emoji_value =str (payload .emoji )

    for pair in record .get ("pairs",[]):
        if str (pair .get ("emoji"))!=emoji_value :
            continue

        guild =bot .get_guild (payload .guild_id )
        if guild is None :
            return

        role =guild .get_role (int (pair ["role_id"]))
        member =guild .get_member (payload .user_id )

        if role is None or member is None :
            return

        if role .is_default ()or role .managed :
            return

        try :
            await member .remove_roles (
            role ,
            reason ="Reaction role removed",
            )
        except (discord .Forbidden ,discord .HTTPException ):
            pass
        return




@bot.command(name="cmds")
async def commands_list_command(ctx:commands.Context):
    prefix_entries=[(command.qualified_name,command.help or command.description or "Prefix command") for command in sorted(bot.commands,key=lambda item:item.qualified_name.lower())]

    slash_entries=[]
    def walk(group,prefix=""):
        for command in sorted(group.commands,key=lambda item:item.name.lower()):
            qualified=f"{prefix} {command.name}".strip()
            if isinstance(command,app_commands.Group):
                walk(command,qualified)
            else:
                slash_entries.append((qualified,command.description or "Slash command"))

    for command in sorted(bot.tree.get_commands(),key=lambda item:item.name.lower()):
        if isinstance(command,app_commands.Group):
            walk(command,command.name)
        else:
            slash_entries.append((command.name,command.description or "Slash command"))

    prefix_entries.sort(key=lambda item:item[0].lower())
    slash_entries.sort(key=lambda item:item[0].lower())
    view=CmdsView(ctx,prefix_entries,slash_entries)
    message=await ctx.send(view=view)
    view.message=message




create_group =app_commands .Group (
name ="create",
description ="Create server tools",
)

anti_group =app_commands .Group (
name ="anti",
description ="Anti moderation tools",
parent =create_group ,
)

server_group =app_commands .Group (
name ="server",
description ="Server information and tools",
)

add_group =app_commands .Group (
name ="add",
description ="Add server tools",
)

update_group =app_commands .Group (
name ="update",
description ="Post update logs",
)

upload_group =app_commands .Group (
name ="upload",
description ="Upload and preview scripts",
)

custom_group =app_commands .Group (
name ="custom",
description ="Customize this server's bot profile",
)

reaction_group =app_commands .Group (
name ="reaction",
description ="Reaction role tools",
parent =add_group ,
)


@anti_group .command (
name ="scam",
description ="Create an anti-scam protection channel",
)
@app_commands .describe (name ="The exact name of the channel to create")
@app_commands .checks .has_permissions (manage_channels =True )
async def anti_scam (interaction :discord .Interaction ,name :str ):
    if interaction .guild is None :
        await interaction .response .send_message (
        "This command can only be used inside a server.",
        ephemeral =True ,
        )
        return

    await interaction .response .defer (ephemeral =True )

    me =interaction .guild .me

    if me is None :
        await interaction .followup .send (
        "I could not verify my server permissions.",
        ephemeral =True ,
        )
        return

    if not me .guild_permissions .manage_channels :
        await interaction .followup .send (
        "I need the Manage Channels permission.",
        ephemeral =True ,
        )
        return

    if not me .guild_permissions .kick_members :
        await interaction .followup .send (
        "I need the Kick Members permission.",
        ephemeral =True ,
        )
        return

    clean_name =name .strip ()

    if not clean_name :
        await interaction .followup .send (
        "The channel name cannot be empty.",
        ephemeral =True ,
        )
        return

    try :
        channel =await interaction .guild .create_text_channel (
        clean_name ,
        reason =f"Anti-scam channel created by {interaction .user }",
        )

        view =AntiScamView ()

        message =await channel .send (view =view )

        try :
            await message .add_reaction ("👍")
        except discord .HTTPException :
            pass

        record ={
        "channel_id":channel .id ,
        "guild_id":interaction .guild .id ,
        "message_id":message .id ,
        "kicks":0 ,
        "violations":0 ,
        }

        await mongo_call (save_anti_scam_sync ,record )

        created_channels [channel .id ]={
        "view":view ,
        "message":message ,
        "guild_id":interaction .guild .id ,
        }

        await interaction .followup .send (
        f"Created {channel .mention }.",
        ephemeral =True ,
        )

    except discord .Forbidden :
        await interaction .followup .send (
        "I don't have permission to create or manage that channel.",
        ephemeral =True ,
        )
    except discord .HTTPException as error :
        await interaction .followup .send (
        f"Discord returned an error: {error }",
        ephemeral =True ,
        )
    except PyMongoError as error :
        try :
            await channel .delete (reason ="MongoDB persistence failed")
        except Exception :
            pass

        await interaction .followup .send (
        f"MongoDB error while saving the channel: {error }",
        ephemeral =True ,
        )
    except Exception as error :
        await interaction .followup .send (
        f"Unexpected error: {error }",
        ephemeral =True ,
        )


@server_group .command (
name ="insights",
description ="View member activity from the last 30 days",
)
@app_commands .checks .has_permissions (manage_guild =True )
async def server_insights (interaction :discord .Interaction ):
    if interaction .guild is None :
        await interaction .response .send_message (
        "This command can only be used inside a server.",
        ephemeral =True ,
        )
        return

    await interaction .response .defer ()

    try :
        current_member_ids =[]
        async for member in interaction .guild .fetch_members (limit =None ):
            current_member_ids .append (member .id )

        joined ,left ,initialized =await mongo_call (
        reconcile_member_snapshot_sync ,
        interaction .guild .id ,
        current_member_ids ,
        )

        document =await mongo_call (
        get_guild_insights_sync ,
        interaction .guild .id ,
        )

        view =InsightsView (
        guild =interaction .guild ,
        document =document ,
        )

        await interaction .followup .send (view =view )

    except PyMongoError as error :
        await interaction .followup .send (
        f"MongoDB error: {error }",
        ephemeral =True ,
        )
    except Exception as error :
        await interaction .followup .send (
        f"Unexpected error: {error }",
        ephemeral =True ,
        )


async def find_guild_message (guild ,message_id ):
    message_id =int (message_id )
    channels =[]
    try :
        channels =await guild .fetch_channels ()
    except (discord .Forbidden ,discord .HTTPException ):
        channels =list (guild .channels )

    checked =set ()
    for channel in channels :
        channel_id =getattr (channel ,"id",None )
        if channel_id in checked :
            continue
        checked .add (channel_id )

        if isinstance (channel ,discord .TextChannel ):
            try :
                return await channel .fetch_message (message_id )
            except discord .NotFound :
                continue
            except (discord .Forbidden ,discord .HTTPException ):
                continue

        if isinstance (channel ,discord .ForumChannel ):
            for thread in channel .threads :
                try :
                    return await thread .fetch_message (message_id )
                except discord .NotFound :
                    continue
                except (discord .Forbidden ,discord .HTTPException ):
                    continue

    for channel in guild .text_channels :
        if channel .id in checked :
            continue
        try :
            return await channel .fetch_message (message_id )
        except discord .NotFound :
            continue
        except (discord .Forbidden ,discord .HTTPException ):
            continue

    return None

@update_group .command (
name ="logs",
description ="Post an update log",
)
@app_commands .describe (
title ="Update title",
version ="Update version, such as 2.6",
change_logs ="Message ID containing the change logs",
message ="Optional small update message",
script_channel ="Optional Discord channel link for the script",
channel ="Channel where the update will be posted",
)
async def update_logs (
interaction :discord .Interaction ,
title :str ,
version :str ,
change_logs :str ,
channel :discord .TextChannel ,
message :str |None =None ,
script_channel :str |None =None ,
):
    if interaction .guild is None :
        await interaction .response .send_message (
        "This command can only be used inside a server.",
        ephemeral =True ,
        )
        return

    if not interaction .user .guild_permissions .manage_guild :
        await interaction .response .send_message (
        "You need the Manage Server permission to use this command.",
        ephemeral =True ,
        )
        return

    me =interaction .guild .me
    if me is None :
        await interaction .response .send_message (
        "I could not verify my server permissions.",
        ephemeral =True ,
        )
        return

    permissions =channel .permissions_for (me )
    if not permissions .view_channel or not permissions .send_messages :
        await interaction .response .send_message (
        "I need View Channel and Send Messages permissions in the selected channel.",
        ephemeral =True ,
        )
        return

    if not permissions .mention_everyone :
        await interaction .response .send_message (
        "I need the Mention @everyone permission in the selected channel.",
        ephemeral =True ,
        )
        return

    clean_title =title .strip ()
    clean_version =version .strip ()
    clean_message_id =change_logs .strip ()
    clean_message =message .strip ()if message else None
    clean_script_channel =script_channel .strip ()if script_channel else None

    if not clean_title :
        await interaction .response .send_message (
        "The title cannot be empty.",
        ephemeral =True ,
        )
        return

    version_match =re .fullmatch (r"(\d+)(?:\.(\d+))?(?:\.(\d+))?",clean_version )
    if not version_match :
        await interaction .response .send_message (
        "The version must contain numbers such as 2, 2.6, or 2.6.0.",
        ephemeral =True ,
        )
        return

    version_text =f"version {clean_version}"

    if not re .fullmatch (r"\d{15,22}",clean_message_id ):
        await interaction .response .send_message (
        "The change logs value must be a Discord message ID.",
        ephemeral =True ,
        )
        return

    script_channel_id =None
    if clean_script_channel :
        script_link_match =re .fullmatch (
        r"https://(?:discord\.com|discordapp\.com)/channels/(\d{15,22})/(\d{15,22})",
        clean_script_channel ,
        )
        if not script_link_match :
            await interaction .response .send_message (
            "The script channel must be a valid Discord channel link.",
            ephemeral =True ,
            )
            return

        script_guild_id ,script_channel_id =map (int ,script_link_match .groups ())
        if script_guild_id != interaction .guild .id :
            await interaction .response .send_message (
            "The script channel link must point to a channel in this server.",
            ephemeral =True ,
            )
            return

        script_channel_obj =interaction .guild .get_channel (script_channel_id )
        if script_channel_obj is None :
            try :
                script_channel_obj =await interaction .guild .fetch_channel (script_channel_id )
            except (discord .NotFound ,discord .Forbidden ,discord .HTTPException ):
                script_channel_obj =None

        if script_channel_obj is None :
            await interaction .response .send_message (
            "The script channel link points to a channel that could not be found in this server.",
            ephemeral =True ,
            )
            return

        script_channel_id =script_channel_obj .id

    await interaction .response .defer (ephemeral =True )

    try :
        source_message =await find_guild_message (
        interaction .guild ,
        int (clean_message_id ),
        )

        if source_message is None :
            await interaction .followup .send (
            "I could not find that message in a channel I can access.",
            ephemeral =True ,
            )
            return

        change_content =source_message .content .strip ()
        if not change_content :
            await interaction .followup .send (
            "The selected message does not contain any text to use as the change logs.",
            ephemeral =True ,
            )
            return

        change_content =change_content .replace ("```","`\u200b``")

        script_components =[
        make_separator (),
        make_text ("⛓️‍💥 **Check for the script here**"),
        discord .ui .ActionRow (
            discord .ui .Button (
            label ="Check Script",
            emoji ="📜",
            style =discord .ButtonStyle .link ,
            url =clean_script_channel ,
            )
        ),
        ]if clean_script_channel else []

        container =make_container (
        make_text (f"## {clean_title }"),
        make_text (f"-# {version_text }"),
        make_separator (),
        make_text (
        f"### CHANGE LOGS\n```diff\n{change_content }```"
        ),
        *(
        [make_text (clean_message ),make_separator ()]
        if clean_message
        else []
        ),
        *script_components ,
        accent_color =0x5865F2 ,
        )

        view =discord .ui .LayoutView (timeout =None )
        view .add_item (make_text ("@everyone"))
        view .add_item (container )

        sent_message =await channel .send (
        view =view ,
        allowed_mentions =discord .AllowedMentions (everyone =True ),
        )

        await interaction .followup .send (
        f"Update log posted in {channel .mention }. Message ID: `{sent_message .id }`",
        ephemeral =True ,
        )

    except discord .Forbidden :
        await interaction .followup .send (
        "I do not have permission to access the selected message or post the update in that channel.",
        ephemeral =True ,
        )
    except discord .HTTPException as error :
        await interaction .followup .send (
        f"Discord returned an error while posting the update: {error }",
        ephemeral =True ,
        )
    except ValueError :
        await interaction .followup .send (
        "The change logs message ID is invalid.",
        ephemeral =True ,
        )
    except Exception as error :
        await interaction .followup .send (
        f"Unexpected error while posting the update: {error }",
        ephemeral =True ,
        )


@upload_group .command (
name ="script",
description ="Post a Lua/Luau script as a normal code block",
)
@app_commands .describe (
title ="Required title for the script post",
script ="Required Lua/Luau script text",
)
async def upload_script (
interaction :discord .Interaction ,
title :str ,
script :str ,
):
    clean_title=re.sub(r"^#+\s*", "", title.strip())
    clean_script=script.strip("\n")

    if not clean_title:
        await interaction.response.send_message("The title cannot be empty.",ephemeral=True)
        return

    if not clean_script:
        await interaction.response.send_message("The script cannot be empty.",ephemeral=True)
        return

    if len(clean_title)>256:
        await interaction.response.send_message("The title is too long. Keep it under 256 characters.",ephemeral=True)
        return

    if len(clean_script)>6000:
        await interaction.response.send_message("The script is too long for a slash-command field. Keep it under 6,000 characters.",ephemeral=True)
        return

    def split_code_parts(value,max_length=1600):
        lines=value.splitlines() or [""]
        parts=[]
        current=[]
        current_length=0
        for line in lines:
            added=len(line)+1
            if current and current_length+added>max_length:
                parts.append("\n".join(current))
                current=[]
                current_length=0
            if len(line)>max_length:
                if current:
                    parts.append("\n".join(current))
                    current=[]
                    current_length=0
                for index in range(0,len(line),max_length):
                    parts.append(line[index:index+max_length])
                continue
            current.append(line)
            current_length+=added
        if current or not parts:
            parts.append("\n".join(current))
        return parts

    parts=split_code_parts(clean_script)
    total=len(parts)

    try:
        for index,part in enumerate(parts,1):
            heading=f"## {discord.utils.escape_markdown(clean_title)}"
            if total>1:
                heading+=f" · Part {index}/{total}"
            safe_part=part.replace("```","`\u200b``")
            content=f"{heading}\n```lua\n{safe_part}\n```"
            if index==1:
                await interaction.response.send_message(content=content)
            else:
                await interaction.followup.send(content)
    except discord.HTTPException as error:
        message=f"Could not post the script: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message,ephemeral=True)
        else:
            await interaction.response.send_message(message,ephemeral=True)


@custom_group .command (
name ="image",
description ="Set the bot's avatar for this server only",
)
@app_commands .describe (image ="Required PNG, JPG, JPEG, or GIF image")
@app_commands .checks .has_permissions (manage_guild =True )
async def custom_image (
interaction :discord .Interaction ,
image :discord .Attachment ,
):
    if interaction .guild is None :
        await interaction .response .send_message (
        "This command can only be used inside a server.",
        ephemeral =True ,
        )
        return

    filename =os .path .basename (image .filename or "image")
    extension =os .path .splitext (filename )[1 ].lower ()
    content_type =(image .content_type or "").lower ()
    type_map ={
    "image/png":"image/png",
    "image/jpeg":"image/jpeg",
    "image/jpg":"image/jpeg",
    "image/gif":"image/gif",
    }
    mime =type_map .get (content_type )
    if mime is None :
        mime ={
        ".png":"image/png",
        ".jpg":"image/jpeg",
        ".jpeg":"image/jpeg",
        ".gif":"image/gif",
        }.get (extension )

    if mime is None :
        await interaction .response .send_message (
        "❌ Please upload a PNG, JPG, JPEG, or GIF image.",
        ephemeral =True ,
        )
        return

    if image .size is not None and image .size >8 *1024 *1024 :
        await interaction .response .send_message (
        "❌ The image is too large. The maximum allowed size is 8 MB.",
        ephemeral =True ,
        )
        return

    await interaction .response .defer (ephemeral =True ,thinking =True )
    try :
        data =await image .read ()
    except discord .HTTPException as error :
        await interaction .followup .send (
        f"❌ I could not read that image: {error }",
        ephemeral =True ,
        )
        return

    if not data :
        await interaction .followup .send (
        "❌ The uploaded image is empty.",
        ephemeral =True ,
        )
        return

    if len (data )>8 *1024 *1024 :
        await interaction .followup .send (
        "❌ The image is too large. The maximum allowed size is 8 MB.",
        ephemeral =True ,
        )
        return

    data_uri =f"data:{mime };base64,"+base64 .b64encode (data ).decode ("ascii")

    try :
        await asyncio .to_thread (
        set_guild_bot_avatar_sync ,
        interaction .guild .id ,
        data_uri ,
        )
        await interaction .followup .send (
        view =CustomImageResultView (filename ),
        ephemeral =True ,
        )
    except RuntimeError as error :
        await interaction .followup .send (
        f"❌ Could not update the bot's server avatar.\n`{discord.utils.escape_markdown(str(error)[:1500])}`",
        ephemeral =True ,
        )
    except discord .HTTPException as error :
        await interaction .followup .send (
        f"❌ Discord returned an error: {error }",
        ephemeral =True ,
        )


@reaction_group .command (
name ="role",
description ="Create reaction roles on an existing message",
)
async def add_reaction_role (interaction :discord .Interaction ):
    if interaction .guild is None :
        await interaction .response .send_message (
        "This command can only be used inside a server.",
        ephemeral =True ,
        )
        return

    if not interaction .user .guild_permissions .manage_roles :
        await interaction .response .send_message (
        "You need the Manage Roles permission to use this command.",
        ephemeral =True ,
        )
        return

    me =interaction .guild .me

    if me is None or not me .guild_permissions .manage_roles :
        await interaction .response .send_message (
        "I need the Manage Roles permission.",
        ephemeral =True ,
        )
        return

    view =ReactionRoleSetupView (interaction .user .id )

    await interaction .response .send_message (
    view =view ,
    ephemeral =True ,
    )


@bot .tree .command (
name ="purge",
description ="Delete recent messages from the current channel",
)
@app_commands .describe (count ="Number of messages to delete (1-1000)")
@app_commands .checks .has_permissions (manage_messages =True )
async def purge (
interaction :discord .Interaction ,
count :app_commands .Range [int ,1 ,1000 ],
):
    if not isinstance (interaction .channel ,discord .TextChannel ):
        await interaction .response .send_message (
        "This command can only be used in a text channel.",
        ephemeral =True ,
        )
        return

    if interaction .guild is None :
        await interaction .response .send_message (
        "This command can only be used inside a server.",
        ephemeral =True ,
        )
        return

    me =interaction .guild .me

    if me is None :
        await interaction .response .send_message (
        "I could not verify my permissions.",
        ephemeral =True ,
        )
        return

    permissions =interaction .channel .permissions_for (me )

    if not permissions .manage_messages :
        await interaction .response .send_message (
        "I need the Manage Messages permission in this channel.",
        ephemeral =True ,
        )
        return

    if not permissions .read_message_history :
        await interaction .response .send_message (
        "I need the Read Message History permission in this channel.",
        ephemeral =True ,
        )
        return

    await interaction .response .defer (ephemeral =True )

    try :
        deleted =await interaction .channel .purge (
        limit =int (count ),
        bulk =True ,
        reason =f"Purge requested by {interaction .user }",
        )

        view =PurgeView (
        requested =int (count ),
        deleted =len (deleted ),
        channel =interaction .channel ,
        )

        await interaction .followup .send (
        view =view ,
        ephemeral =True ,
        )

    except discord .Forbidden :
        await interaction .followup .send (
        "I don't have permission to delete messages in this channel.",
        ephemeral =True ,
        )
    except discord .HTTPException as error :
        await interaction .followup .send (
        f"Discord returned an error while purging messages: {error }",
        ephemeral =True ,
        )


@bot .event
async def on_member_join (member :discord .Member ):
    try :
        await mongo_call (
        add_member_event_sync ,
        member .guild .id ,
        "join",
        )
        await mongo_call (add_member_to_snapshot_sync ,member .guild .id ,member .id )
    except PyMongoError as error :
        print (f"MongoDB join tracking error for guild {member .guild .id }: {error }")


@bot .event
async def on_member_remove (member :discord .Member ):
    try :
        await mongo_call (
        add_member_event_sync ,
        member .guild .id ,
        "leave",
        )
        await mongo_call (remove_member_from_snapshot_sync ,member .guild .id ,member .id )
    except PyMongoError as error :
        print (f"MongoDB leave tracking error for guild {member .guild .id }: {error }")


@bot .event
async def on_message (message :discord .Message ):
    if message .author .bot :
        return

    data =created_channels .get (message .channel .id )
    if data is not None :
        member =message .author
        if isinstance (member ,discord .Member )and not member .guild_permissions .administrator :
            try :
                await message .delete ()
            except (discord .Forbidden ,discord .NotFound ,discord .HTTPException ):
                pass

            me =message .guild .me
            kicked =False
            if me is not None and me .guild_permissions .kick_members and member .top_role <me .top_role :
                try :
                    await member .kick (reason ="Message sent in anti-scam channel")
                    kicked =True
                except (discord .Forbidden ,discord .NotFound ,discord .HTTPException ):
                    pass

            try :
                record =await mongo_call (
                increment_anti_scam_sync ,
                message .channel .id ,
                kicked ,
                message .guild .id ,
                )
                if record :
                    data ["view"].update_stats (
                    kicks =int (record .get ("kicks",0 )),
                    violations =int (record .get ("violations",0 )),
                    )
                    if data .get ("message")is not None :
                        try :
                            await data ["message"].edit (view =data ["view"])
                        except (discord .NotFound ,discord .Forbidden ,discord .HTTPException ):
                            pass
            except PyMongoError as error :
                print (f"MongoDB anti-scam update error for channel {message .channel .id }: {error }")

    await bot .process_commands (message )


async def restore_anti_scam_channels ():
    records =await mongo_call (list_anti_scam_sync )
    stale =[]

    for record in records :
        try :
            channel_id =int (record ["channel_id"])
            message_id =int (record ["message_id"])
            guild_id =int (record ["guild_id"])
            kicks =int (record .get ("kicks",0 ))
            violations =int (record .get ("violations",kicks ))
        except (KeyError ,TypeError ,ValueError ):
            stale .append (record .get ("_id"))
            continue

        guild =bot .get_guild (guild_id )

        if guild is None :
            continue

        channel =guild .get_channel (channel_id )

        if not isinstance (channel ,discord .TextChannel ):
            stale .append (channel_id )
            continue

        view =AntiScamView (kicks ,violations )

        try :
            message =await channel .fetch_message (message_id )
        except discord .NotFound :
            stale .append (channel_id )
            continue
        except discord .Forbidden :
            created_channels [channel_id ]={
            "view":view ,
            "message":None ,
            "guild_id":guild_id ,
            }
            continue
        except discord .HTTPException :
            continue

        try :
            await message .edit (view =view )
        except (
        discord .Forbidden ,
        discord .NotFound ,
        discord .HTTPException ,
        ):
            pass

        created_channels [channel_id ]={
        "view":view ,
        "message":message ,
        "guild_id":guild_id ,
        }

    for channel_id in stale :
        if channel_id is not None :
            await mongo_call (
            delete_anti_scam_sync ,
            channel_id ,
            )


@bot .event
async def on_ready ():
    global ready_once

    if ready_once :
        return

    try :
        await mongo_call (
        mongo_client .admin .command ,
        "ping",
        )

        for guild in bot .guilds :
            try :
                member_ids =[member .id for member in guild .members ]
                joined ,left ,initialized =await mongo_call (
                reconcile_member_snapshot_sync ,
                guild .id ,
                member_ids ,
                )
                if joined or left :
                    print (
                    f"Reconciled {guild .name }: {len (joined )} missed joins, {len (left )} missed departures"
                    )
                await mongo_call (get_guild_insights_sync ,guild .id )
            except PyMongoError as error :
                print (f"MongoDB member reconciliation error for guild {guild .id }: {error }")

        synced =await bot .tree .sync ()

        await restore_anti_scam_channels ()
        await restore_reaction_roles ()

        ready_once =True

        print (
        f"Logged in as {bot .user } ({bot .user .id })"
        )
        print (
        f"Connected to MongoDB database: {MONGODB_DATABASE }"
        )
        print (
        f"Synced {len (synced )} command(s)"
        )
        print (
        f"Restored {len (created_channels )} anti-scam channel(s)"
        )
        print (
        f"Restored {len (reaction_role_cache )} reaction-role message(s)"
        )

    except PyMongoError as error :
        print (f"MongoDB startup error: {error }")
    except Exception as error :
        print (f"Startup error: {error }")


@anti_scam .error
async def anti_scam_error (
interaction :discord .Interaction ,
error :app_commands .AppCommandError ,
):
    message =(
    "You need the Manage Channels permission to use this command."
    if isinstance (error ,app_commands .MissingPermissions )
    else f"Command error: {error }"
    )

    if interaction .response .is_done ():
        await interaction .followup .send (
        message ,
        ephemeral =True ,
        )
    else :
        await interaction .response .send_message (
        message ,
        ephemeral =True ,
        )


@server_insights .error
async def server_insights_error (
interaction :discord .Interaction ,
error :app_commands .AppCommandError ,
):
    message =(
    "You need the Manage Server permission to use this command."
    if isinstance (error ,app_commands .MissingPermissions )
    else f"Command error: {error }"
    )

    if interaction .response .is_done ():
        await interaction .followup .send (
        message ,
        ephemeral =True ,
        )
    else :
        await interaction .response .send_message (
        message ,
        ephemeral =True ,
        )


@add_reaction_role .error
async def add_reaction_role_error (
interaction :discord .Interaction ,
error :app_commands .AppCommandError ,
):
    message =f"Command error: {error }"

    if interaction .response .is_done ():
        await interaction .followup .send (
        message ,
        ephemeral =True ,
        )
    else :
        await interaction .response .send_message (
        message ,
        ephemeral =True ,
        )


@purge .error
async def purge_error (
interaction :discord .Interaction ,
error :app_commands .AppCommandError ,
):
    message =(
    "You need the Manage Messages permission to use this command."
    if isinstance (error ,app_commands .MissingPermissions )
    else f"Command error: {error }"
    )

    if interaction .response .is_done ():
        await interaction .followup .send (
        message ,
        ephemeral =True ,
        )
    else :
        await interaction .response .send_message (
        message ,
        ephemeral =True ,
        )


bot .tree .add_command (create_group )
bot .tree .add_command (server_group )
bot .tree .add_command (add_group )
bot .tree .add_command (update_group )
bot .tree .add_command (upload_group )
bot .tree .add_command (custom_group )


async def start_bot ():
    global ready_once

    while True :
        try :
            await bot .start (TOKEN )
            break
        except discord .LoginFailure :
            print ("Invalid Discord bot token.")
            break
        except discord .HTTPException as error :
            retry_after =getattr (error ,"retry_after",30 )
            print (f"Discord connection error: {error }")
            print (f"Retrying in {retry_after :.1f} seconds...")
            await asyncio .sleep (retry_after )
        except Exception as error :
            print (f"Bot error: {error }")
            await asyncio .sleep (30 )
        finally :
            ready_once =False


asyncio .run (start_bot ())
