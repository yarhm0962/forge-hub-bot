import os 
import asyncio 
import json 
import re 
import hashlib 
import io 
import ipaddress 
import shutil 
import random 
import subprocess 
import tempfile 
import secrets 
import socket 
import string 
import time 
import urllib .error 
import urllib .request 
from urllib .parse import urlparse 
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
    if document ["total_joins"]==0 and joins :
        document ["total_joins"]=len (joins )
    if document ["total_leaves"]==0 and leaves :
        document ["total_leaves"]=len (leaves )
    return document 


def get_guild_insights_sync (guild_id ):
    guild_id =int (guild_id )
    document =insights_collection .find_one ({"_id":guild_id })
    if not document :
        document ={
        "_id":guild_id ,
        "joins":[],
        "leaves":[],
        "total_joins":0 ,
        "total_leaves":0 ,
        }
    document .setdefault ("joins",[])
    document .setdefault ("leaves",[])
    document .setdefault ("total_joins",0 )
    document .setdefault ("total_leaves",0 )
    if document ["total_joins"]==0 and document ["joins"]:
        document ["total_joins"]=len (document ["joins"])
    if document ["total_leaves"]==0 and document ["leaves"]:
        document ["total_leaves"]=len (document ["leaves"])
    cleanup_events_sync (document )
    insights_collection .replace_one ({"_id":guild_id },document ,upsert =True )
    return document 


def add_member_event_sync (guild_id ,event_type ):
    guild_id =int (guild_id )
    field ="joins"if event_type =="join"else "leaves"
    total_field ="total_joins"if event_type =="join"else "total_leaves"
    insights_collection .update_one (
    {"_id":guild_id },
    {
    "$setOnInsert":{
    "joins":[],
    "leaves":[],
    "total_joins":0 ,
    "total_leaves":0 ,
    }
    },
    upsert =True ,
    )
    document =insights_collection .find_one_and_update (
    {"_id":guild_id },
    {
    "$push":{field :iso_now ()},
    "$inc":{total_field :1 },
    },
    return_document =ReturnDocument .AFTER ,
    )
    cleanup_events_sync (document )
    insights_collection .replace_one ({"_id":guild_id },document ,upsert =True )
    return document 


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
        return 0 ,0 ,True 
    joined =current -previous 
    left =previous -current 
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


def increment_anti_scam_sync (channel_id ,kicked ):
    increments ={"violations":1 }
    if kicked :
        increments ["kicks"]=1 
    return anti_scam_collection .find_one_and_update (
    {"_id":int (channel_id )},
    {"$inc":increments },
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


LUA_PROCESS_MAX_BYTES =2 *1024 *1024 
LUA_TOOL_TIMEOUT =45 
LUADEC_EXECUTABLE =os .getenv ("LUADEC_EXECUTABLE")
UNLUAC_JAR =os .getenv ("UNLUAC_JAR")
JAVA_EXECUTABLE =os .getenv ("JAVA_EXECUTABLE")
PROMETHEUS_EXECUTABLE =os .getenv ("PROMETHEUS_EXECUTABLE")
PROMETHEUS_PRESET =os .getenv ("PROMETHEUS_PRESET","Strong")


def resolve_executable (configured ,candidates ):
    values =[]
    if configured :
        values .append (configured )
    values .extend (candidates )
    for value in values :
        if not value :
            continue 
        if os .path .isabs (value )and os .path .isfile (value )and os .access (value ,os .X_OK ):
            return value 
        found =shutil .which (value )
        if found :
            return found 
    return None 


def find_luadec ():
    return resolve_executable (LUADEC_EXECUTABLE ,[
    "/home/container/luadec",
    "/home/container/bin/luadec",
    "/home/container/bin/luadec51",
    "/usr/local/bin/luadec",
    "/usr/bin/luadec",
    "luadec",
    "luadec51",
    ])


def find_java ():
    return resolve_executable (JAVA_EXECUTABLE ,["/usr/bin/java","/usr/local/bin/java","java"])


def find_prometheus ():
    return resolve_executable (PROMETHEUS_EXECUTABLE ,[
    "/home/container/.local/bin/prometheus-lua",
    "/home/container/prometheus-lua",
    "/usr/local/bin/prometheus-lua",
    "/usr/bin/prometheus-lua",
    "prometheus-lua",
    ])


def is_lua_bytecode (data ):
    return data .startswith (b"\x1bLua")


def decode_lua_string_literal (value ):
    if len (value )<2 or value [0 ]not in {'"',"'"}or value [-1 ]!=value [0 ]:
        return None 
    body =value [1 :-1 ]
    out =bytearray ()
    i =0 
    simple ={"a":7 ,"b":8 ,"f":12 ,"n":10 ,"r":13 ,"t":9 ,"v":11 ,"\\":92 ,'"':34 ,"'":39 }
    while i <len (body ):
        ch =body [i ]
        if ch !="\\":
            out .extend (ch .encode ("utf-8"))
            i +=1 
            continue 
        i +=1 
        if i >=len (body ):
            return None 
        esc =body [i ]
        if esc in simple :
            out .append (simple [esc ])
            i +=1 
            continue 
        if esc in {"x","X"}and i +2 <len (body ):
            piece =body [i +1 :i +3 ]
            if re .fullmatch (r"[0-9A-Fa-f]{2}",piece ):
                out .append (int (piece ,16 ))
                i +=3 
                continue 
        if esc .isdigit ():
            j =i 
            while j <len (body )and j <i +3 and body [j ].isdigit ():
                j +=1 
            number =int (body [i :j ])
            if number >255 :
                return None 
            out .append (number )
            i =j 
            continue 
        if esc =="z":
            i +=1 
            while i <len (body )and body [i ].isspace ():
                i +=1 
            continue 
        if esc =="\n":
            i +=1 
            continue 
        out .extend (esc .encode ("utf-8"))
        i +=1 
    return bytes (out )


def lua_quote_bytes (value ):
    text =value .decode ("utf-8",errors ="replace")
    text =text .replace ("\\","\\\\").replace ('"','\\"').replace ("\r","\\r").replace ("\n","\\n")
    return f'"{text }"'


def static_deobfuscate_lua (source ):
    result =source .lstrip ("\ufeff")
    for _ in range (8 ):
        previous =result 

        def replace_string_char (match ):
            args =match .group (1 )
            parts =[part .strip ()for part in args .split (",")if part .strip ()]
            if not parts or any (not re .fullmatch (r"-?\d+",part )for part in parts ):
                return match .group (0 )
            values =[int (part )for part in parts ]
            if any (value <0 or value >255 for value in values ):
                return match .group (0 )
            return lua_quote_bytes (bytes (values ))

        result =re .sub (r"string\.char\s*\(([^()]*)\)",replace_string_char ,result ,flags =re .DOTALL )

        def replace_reverse (match ):
            decoded =decode_lua_string_literal (match .group (1 ))
            return lua_quote_bytes (decoded [::-1 ])if decoded is not None else match .group (0 )

        result =re .sub (r"string\.reverse\s*\(\s*([\"'][^\"']*[\"'])\s*\)",replace_reverse ,result )

        def replace_rep (match ):
            literal =decode_lua_string_literal (match .group (1 ))
            count =int (match .group (2 ))
            if literal is None or count <0 or count >10000 :
                return match .group (0 )
            value =literal *count 
            if len (value )>100000 :
                return match .group (0 )
            return lua_quote_bytes (value )

        result =re .sub (r"string\.rep\s*\(\s*([\"'][^\"']*[\"'])\s*,\s*(\d+)\s*\)",replace_rep ,result )

        def replace_concat (match ):
            left =decode_lua_string_literal (match .group (1 ))
            right =decode_lua_string_literal (match .group (2 ))
            if left is None or right is None :
                return match .group (0 )
            return lua_quote_bytes (left +right )

        result =re .sub (r"(\"[^\"]*\"|'[^']*')\s*\.\.\s*(\"[^\"]*\"|'[^']*')",replace_concat ,result )

        def replace_simple_arithmetic (match ):
            a =int (match .group (1 ))
            op =match .group (2 )
            b =int (match .group (3 ))
            if op =="+":
                value =a +b 
            elif op =="-":
                value =a -b 
            elif op =="*":
                value =a *b 
            elif op =="/":
                if b ==0 :
                    return match .group (0 )
                value =a /b 
            else :
                return match .group (0 )
            if isinstance (value ,float )and value .is_integer ():
                value =int (value )
            return str (value )

        result =re .sub (r"(?<![\w.])(-?\d+)\s*([+\-*/])\s*(-?\d+)(?![\w.])",replace_simple_arithmetic ,result )
        if result ==previous :
            break 
    return result if result .strip ()else source 


def printable_strings (data ,minimum =4 ):
    strings =[]
    current =bytearray ()
    for byte in data :
        if 32 <=byte <=126 or byte ==9 :
            current .append (byte )
        else :
            if len (current )>=minimum :
                strings .append (current .decode ("ascii",errors ="replace"))
            current .clear ()
    if len (current )>=minimum :
        strings .append (current .decode ("ascii",errors ="replace"))
    return strings 


def make_static_dump (filename ,data ):
    digest =hashlib .sha256 (data ).hexdigest ()
    kind ="Lua bytecode"if is_lua_bytecode (data )else "Lua/TXT source"
    strings =printable_strings (data )
    lines =[
    "LUA STATIC DUMP",
    "================",
    f"File: {filename }",
    f"Size: {len (data ):,} bytes",
    f"Format: {kind }",
    f"SHA-256: {digest }",
    "",
    f"Printable strings ({len (strings ):,}):",
    ]
    lines .extend (f"[{index :04d}] {value }"for index ,value in enumerate (strings [:5000 ],1 ))
    if len (strings )>5000 :
        lines .append (f"... {len (strings )-5000 :,} additional strings omitted ...")
    return "\n".join (lines )+"\n"


def run_luadec (data ,mode ,workdir ,filename ):
    luadec =find_luadec ()
    if not luadec :
        return None ,"LuaDec is not installed."
    safe_name =os .path .basename (filename )or "input.lua"
    input_path =os .path .join (workdir ,safe_name )
    with open (input_path ,"wb")as handle :
        handle .write (data )
    command =[luadec ]
    if mode =="dump":
        command .append ("-dis")
    command .append (input_path )
    try :
        completed =subprocess .run (
        command ,
        stdin =subprocess .DEVNULL ,
        stdout =subprocess .PIPE ,
        stderr =subprocess .STDOUT ,
        timeout =LUA_TOOL_TIMEOUT ,
        check =False ,
        text =True ,
        encoding ="utf-8",
        errors ="replace",
        )
    except subprocess .TimeoutExpired :
        return None ,f"LuaDec timed out after {LUA_TOOL_TIMEOUT } seconds."
    except OSError as error :
        return None ,f"Could not start LuaDec: {error }"
    output =completed .stdout or ""
    if completed .returncode !=0 :
        return None ,f"LuaDec exited with code {completed .returncode }: {output [-1500 :]}"
    if not output .strip ():
        return None ,"LuaDec returned an empty result."
    return output ,None 


def run_unluac (data ,workdir ,filename ):
    if not UNLUAC_JAR or not os .path .isfile (UNLUAC_JAR ):
        return None ,"unluac.jar is not configured."
    java =find_java ()
    if not java :
        return None ,"Java is not installed."
    safe_name =os .path .basename (filename )or "input.luac"
    input_path =os .path .join (workdir ,safe_name )
    with open (input_path ,"wb")as handle :
        handle .write (data )
    try :
        completed =subprocess .run (
        [java ,"-jar",UNLUAC_JAR ,input_path ],
        stdin =subprocess .DEVNULL ,
        stdout =subprocess .PIPE ,
        stderr =subprocess .STDOUT ,
        timeout =LUA_TOOL_TIMEOUT ,
        check =False ,
        text =True ,
        encoding ="utf-8",
        errors ="replace",
        )
    except subprocess .TimeoutExpired :
        return None ,f"unluac timed out after {LUA_TOOL_TIMEOUT } seconds."
    except OSError as error :
        return None ,f"Could not start unluac: {error }"
    output =completed .stdout or ""
    if completed .returncode !=0 :
        return None ,f"unluac exited with code {completed .returncode }: {output [-1500 :]}"
    if not output .strip ():
        return None ,"unluac returned an empty result."
    return output ,None 


def build_lua_results (filename ,data ,workdir ):
    dump_result =None 
    dump_method =None 
    if is_lua_bytecode (data ):
        dump_result ,dump_error =run_luadec (data ,"dump",workdir ,filename )
    else :
        dump_error =None 
    if dump_result is None :
        dump_result =make_static_dump (filename ,data )
        dump_method ="Static Lua dump"if not dump_error else f"Static Lua dump; LuaDec unavailable: {dump_error }"
    else :
        dump_method ="LuaDec bytecode disassembly"

    if is_lua_bytecode (data ):
        deobf_result ,deobf_error =run_unluac (data ,workdir ,filename )
        deobf_method ="unluac bytecode decompilation"
        if deobf_result is None :
            deobf_result ,deobf_error =run_luadec (data ,"deobf",workdir ,filename )
            deobf_method ="LuaDec bytecode decompilation"
        if deobf_result is None :
            deobf_result =dump_result 
            deobf_method =f"Static bytecode analysis; decompiler unavailable: {deobf_error or 'unknown error'}"
            deobf_name ="deobfuscated.lua.txt"
        else :
            deobf_name ="deobfuscated.lua"
    else :
        deobf_result =static_deobfuscate_lua (data .decode ("utf-8-sig",errors ="replace"))
        deobf_method ="Static Lua cleanup"
        deobf_name ="deobfuscated.lua"
    return dump_result ,dump_method ,deobf_result ,deobf_method ,deobf_name 


def cap_result (text ):
    data =text .encode ("utf-8")
    if len (data )<=PASTEFY_MAX_BYTES :
        return text ,False 
    return data [:PASTEFY_MAX_BYTES ].decode ("utf-8",errors ="ignore"),True 


LUA_SOURCE_EXTENSIONS={".lua",".luau",".txt"}
OBF_MAX_OUTPUT_BYTES=5*1024*1024


def lua_long_bracket_end(source,start):
    if start>=len(source) or source[start]!="[":
        return None
    index=start+1
    while index<len(source) and source[index]=="=":
        index+=1
    if index>=len(source) or source[index]!="[":
        return None
    close="]"+"="*(index-start-1)+"]"
    end=source.find(close,index+1)
    if end<0:
        return None
    return end+len(close),source[start:end+len(close)]


def lex_lua_source(source):
    tokens=[]
    operators=("//=","...","::","//","<<",">>","==","~=","<=",">=","..","+=","-=","*=","/=","%=","^=","&=","|=")
    i=0
    n=len(source)
    while i<n:
        ch=source[i]
        if ch.isspace():
            i+=1
            continue
        if i==0 and source.startswith("#!",i):
            end=source.find("\n",i)
            if end<0:
                end=n
            tokens.append(("directive",source[i:end]))
            i=end
            continue
        if source.startswith("--",i):
            if source.startswith("--!",i):
                end=source.find("\n",i)
                if end<0:
                    end=n
                tokens.append(("directive",source[i:end]))
                i=end
                continue
            long_result=lua_long_bracket_end(source,i+2)
            if long_result is not None and source[i+2:i+3]=="[":
                i=long_result[0]
                continue
            end=source.find("\n",i)
            i=n if end<0 else end
            continue
        if ch in {"'","\""}:
            quote=ch
            j=i+1
            while j<n:
                if source[j]=="\\":
                    j+=2
                    continue
                if source[j]==quote:
                    j+=1
                    break
                j+=1
            if j>n or j==i+1 or source[j-1]!=quote:
                raise ValueError("Unterminated Lua string literal.")
            tokens.append(("string",source[i:j]))
            i=j
            continue
        long_result=lua_long_bracket_end(source,i)
        if long_result is not None:
            end,value=long_result
            tokens.append(("string",value))
            i=end
            continue
        if ch.isalpha() or ch=="_":
            j=i+1
            while j<n and (source[j].isalnum() or source[j]=="_"):
                j+=1
            tokens.append(("ident",source[i:j]))
            i=j
            continue
        if ch.isdigit() or (ch=="." and i+1<n and source[i+1].isdigit()):
            match=re.match(r"(?:0[xX][0-9A-Fa-f]+(?:\.[0-9A-Fa-f]*)?(?:[pP][+-]?\d+)?|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)",source[i:])
            if match:
                value=match.group(0)
                tokens.append(("number",value))
                i+=len(value)
                continue
        matched=None
        for operator in operators:
            if source.startswith(operator,i):
                matched=operator
                break
        if matched is not None:
            tokens.append(("op",matched))
            i+=len(matched)
            continue
        tokens.append(("op",ch))
        i+=1
    return tokens


def decode_lua_long_literal(value):
    if not value.startswith("["):
        return None
    index=1
    while index<len(value) and value[index]=="=":
        index+=1
    if index>=len(value) or value[index]!="[":
        return None
    closing="]"+"="*(index-1)+"]"
    if not value.endswith(closing):
        return None
    body=value[index+1:-len(closing)]
    if body.startswith("\n"):
        body=body[1:]
    return body.encode("utf-8")


def decode_lua_literal_bytes(value):
    if value.startswith(("'","\"")):
        return decode_lua_string_literal(value)
    return decode_lua_long_literal(value)


def lua_identifier_name(used,prefix="_x"):
    while True:
        name=f"{prefix}{secrets.token_hex(7)}"
        if name not in used:
            used.add(name)
            return name


def lua_render_tokens(tokens):
    pieces=[]
    previous=None
    word_kinds={"ident","number"}
    for kind,value in tokens:
        if kind=="directive":
            if pieces:
                pieces.append("\n")
            pieces.append(value)
            pieces.append("\n")
            previous=None
            continue
        if previous is not None:
            prev_kind,prev_value=previous
            need_space=False
            if prev_kind in word_kinds and kind in word_kinds:
                need_space=True
            if prev_value in {"+","-"} and value in {"+","-"}:
                need_space=True
            if prev_value=="/" and value=="/":
                need_space=True
            if prev_value=="." and kind=="number":
                need_space=True
            if prev_kind=="number" and value.startswith("."):
                need_space=True
            if need_space:
                pieces.append(" ")
        pieces.append(value)
        previous=(kind,value)
    return "".join(pieces).strip()+"\n"


def transform_lua_numbers(tokens):
    output=[]
    for kind,value in tokens:
        if kind!="number" or not re.fullmatch(r"\d+",value):
            output.append((kind,value))
            continue
        number=int(value)
        if number in {0,1,2} or number>1000000:
            output.append((kind,value))
            continue
        left=random.SystemRandom().randint(3,97)
        right=random.SystemRandom().randint(2,41)
        base=number//left
        remainder=number-(base*left)
        if base==0:
            divisor=random.SystemRandom().randint(2,11)
            left_value=number*divisor
            expression=f"({left_value}/{divisor})"
        else:
            expression=f"(({base}*{left})+{remainder})"
        subtokens=lex_lua_source(expression)
        output.extend(subtokens)
    return output


def build_lua_string_pool(string_values,used):
    if not string_values:
        return [],""
    decoder=lua_identifier_name(used,"_d")
    pool=lua_identifier_name(used,"_p")
    step=random.SystemRandom().randint(3,17)
    lines=[f"local {decoder}=function(a,k)local b={{}} for i=1,#a do b[i]=string.char((a[i]-k-i*{step})%256) end return table.concat(b) end",f"local {pool}={{}}"]
    replacements={}
    shuffled=list(enumerate(string_values,1))
    for index,value in shuffled:
        raw=decode_lua_literal_bytes(value)
        if raw is None:
            replacements[value]=value
            continue
        key=random.SystemRandom().randint(11,239)
        encoded=[(byte+key+(position+1)*step)%256 for position,byte in enumerate(raw)]
        if not encoded:
            encoded=[0]
        chunks=[]
        for start in range(0,len(encoded),180):
            chunks.append(",".join(str(number) for number in encoded[start:start+180]))
        array="{"+",".join(chunks)+"}" if len(chunks)==1 else "{"+",".join(str(number) for number in encoded)+"}"
        lines.append(f"{pool}[{index}]={decoder}({array},{key})")
        replacements[value]=f"{pool}[{index}]"
    return replacements,"\n".join(lines)+"\n"


def build_lua_anti_tamper(used):
    rawget_name=lua_identifier_name(used,"_r")
    rawset_name=lua_identifier_name(used,"_w")
    type_name=lua_identifier_name(used,"_t")
    pcall_name=lua_identifier_name(used,"_c")
    error_name=lua_identifier_name(used,"_e")
    tostring_name=lua_identifier_name(used,"_n")
    getmetatable_name=lua_identifier_name(used,"_m")
    debug_name=lua_identifier_name(used,"_g")
    string_name=lua_identifier_name(used,"_b")
    byte_name=lua_identifier_name(used,"_y")
    check_name=lua_identifier_name(used,"_q")
    safe=lua_identifier_name(used,"_s")
    reason=lua_identifier_name(used,"_rj")
    sentinel=lua_identifier_name(used,"_v")
    sentinel_value=secrets.token_hex(18)
    checksum=sum((index+1)*byte for index,byte in enumerate(sentinel_value.encode("utf-8")))
    lines=[
        f'local {rawget_name}=rawget',
        f'local {rawset_name}=rawset',
        f'local {type_name}=type',
        f'local {pcall_name}=pcall',
        f'local {error_name}=error',
        f'local {tostring_name}=tostring',
        f'local {getmetatable_name}=getmetatable',
        f'local {string_name}={rawget_name}(_G,"string")',
        f'local {byte_name}={rawget_name}({string_name},"byte")',
        f'local {debug_name}={rawget_name}(_G,"debug")',
        f'local {safe}=true',
        f'local {reason}=""',
        f'local {sentinel}="{sentinel_value}"',
        f'local {check_name}=function()',
        f'if {rawget_name}(_G,"rawget")~={rawget_name} then {safe}=false {reason}="rawget hook detected" return false end',
        f'if {rawget_name}(_G,"rawset")~={rawset_name} then {safe}=false {reason}="rawset hook detected" return false end',
        f'if {rawget_name}(_G,"type")~={type_name} then {safe}=false {reason}="type hook detected" return false end',
        f'if {rawget_name}(_G,"pcall")~={pcall_name} then {safe}=false {reason}="pcall hook detected" return false end',
        f'if {rawget_name}(_G,"error")~={error_name} then {safe}=false {reason}="error hook detected" return false end',
        f'if {rawget_name}(_G,"tostring")~={tostring_name} then {safe}=false {reason}="tostring hook detected" return false end',
        f'if {rawget_name}(_G,"string")~={string_name} then {safe}=false {reason}="string library hook detected" return false end',
        f'if {rawget_name}({string_name},"byte")~={byte_name} then {safe}=false {reason}="string byte hook detected" return false end',
        f'local _sum=0',
        f'for _i=1,#{sentinel} do _sum=_sum+(_i*{byte_name}({sentinel},_i)) end',
        f'if _sum~={checksum} then {safe}=false {reason}="embedded integrity fingerprint changed" return false end',
        f'if {getmetatable_name} and {type_name}({getmetatable_name})=="function" then',
        f'local _a,_b={pcall_name}({getmetatable_name},_G)',
        f'if not _a then {safe}=false {reason}="global metatable access changed" return false end',
        'end',
        f'if {debug_name}~=nil and {type_name}({debug_name})=="table" then',
        f'local _h={rawget_name}({debug_name},"gethook")',
        f'local _s={rawget_name}({debug_name},"sethook")',
        f'if _h~=nil and {type_name}(_h)~="function" then {safe}=false {reason}="debug hook API changed" return false end',
        f'if _s~=nil and {type_name}(_s)~="function" then {safe}=false {reason}="debug setter changed" return false end',
        f'if _h~=nil then',
        f'local _ok,_hook={pcall_name}(_h)',
        f'if not _ok then {safe}=false {reason}="debug hook probe failed" return false end',
        f'if _hook~=nil then {safe}=false {reason}="debug hook detected" return false end',
        'end',
        f'local _i={rawget_name}({debug_name},"getinfo")',
        f'if _i~=nil then',
        f'if {type_name}(_i)~="function" then {safe}=false {reason}="debug info API changed" return false end',
        f'local _ok,_info={pcall_name}(_i,{check_name},"S")',
        f'if not _ok or {type_name}(_info)~="table" then {safe}=false {reason}="function integrity metadata failed" return false end',
        f'if type(_info.linedefined)~="number" or type(_info.lastlinedefined)~="number" or _info.lastlinedefined<_info.linedefined then {safe}=false {reason}="function line metadata changed" return false end',
        'end',
        'end',
        f'local _env={rawget_name}(_G,"getfenv")',
        f'if _env and {type_name}(_env)=="function" then',
        f'local _ok,_got={pcall_name}(_env,0)',
        f'if _ok and {type_name}(_got)=="table" and _got~=_G then {safe}=false {reason}="environment mismatch detected" return false end',
        'end',
        f'return {safe}',
        'end',
        f'if not {check_name}() then {error_name}("Anti-tamper blocked execution: "..{reason},0) end',
    ]
    return "\n".join(lines)+"\n"


def run_prometheus(source,filename,workdir):
    executable=find_prometheus()
    if not executable:
        return None,None,None,"Prometheus is not installed."
    safe_name=os.path.basename(filename) or "input.lua"
    if not safe_name.lower().endswith((".lua",".luau")):
        safe_name=os.path.splitext(safe_name)[0]+".lua"
    input_path=os.path.join(workdir,safe_name)
    with open(input_path,"w",encoding="utf-8",newline="") as handle:
        handle.write(source)
    command=[executable,"--preset",PROMETHEUS_PRESET,input_path]
    if safe_name.lower().endswith(".lua"):
        output_path=os.path.join(workdir,safe_name[:-4]+".obfuscated.lua")
    else:
        output_path=os.path.join(workdir,safe_name+".obfuscated.lua")
    command.extend(["--out",output_path])
    try:
        completed=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90,check=False,text=True,encoding="utf-8",errors="replace",cwd=workdir)
    except subprocess.TimeoutExpired:
        return None,None,None,"Prometheus timed out after 90 seconds."
    except OSError as error:
        return None,None,None,f"Could not start Prometheus: {error}"
    if completed.returncode!=0:
        return None,None,None,f"Prometheus exited with code {completed.returncode}: {(completed.stdout or '')[-1500:]}"
    if not os.path.isfile(output_path):
        candidates=[]
        for entry in os.listdir(workdir):
            full=os.path.join(workdir,entry)
            if os.path.isfile(full) and entry!=safe_name and entry.lower().endswith(".lua"):
                candidates.append(full)
        if candidates:
            candidates.sort(key=lambda value:os.path.getmtime(value),reverse=True)
            output_path=candidates[0]
    if not os.path.isfile(output_path):
        return None,None,None,"Prometheus completed without producing an output file."
    try:
        with open(output_path,"rb") as handle:
            data=handle.read(OBF_MAX_OUTPUT_BYTES+1)
    except OSError as error:
        return None,None,None,f"Could not read the Prometheus output: {error}"
    if len(data)>OBF_MAX_OUTPUT_BYTES:
        return None,None,None,"The Prometheus result is larger than the 5 MB output limit."
    try:
        result=data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None,None,None,"Prometheus returned non-UTF-8 output."
    if not result.strip():
        return None,None,None,"Prometheus returned an empty result."
    features=[
        ("Engine",f"Prometheus `{PROMETHEUS_PRESET}` preset"),
        ("Constant protection","enabled by engine preset"),
        ("Control-flow protection","enabled by engine preset"),
        ("Anti-tamper","engine preset with integrity and tamper checks"),
        ("Minification","enabled by engine preset"),
        ("Attribution","Based on Prometheus by Elias Oelschner"),
    ]
    return result,features,"Prometheus",None

def obfuscate_lua_source(source):
    if not source.strip():
        raise ValueError("The Lua source is empty.")
    tokens=lex_lua_source(source.lstrip("\ufeff"))
    directives=[value for kind,value in tokens if kind=="directive"]
    tokens=[token for token in tokens if token[0]!="directive"]
    used={value for kind,value in tokens if kind=="ident"}
    string_values=[]
    seen=set()
    for kind,value in tokens:
        if kind=="string" and value not in seen:
            seen.add(value)
            string_values.append(value)
    replacements,pool_block=build_lua_string_pool(string_values,used)
    transformed=[]
    for kind,value in tokens:
        if kind=="string" and value in replacements:
            replacement=replacements[value]
            transformed.extend(lex_lua_source(replacement))
        else:
            transformed.append((kind,value))
    transformed=transform_lua_numbers(transformed)
    body=lua_render_tokens(transformed)
    anti=build_lua_anti_tamper(used)
    junk=[]
    for _ in range(4):
        a=random.SystemRandom().randint(37,997)
        b=random.SystemRandom().randint(13,71)
        c=random.SystemRandom().randint(7,53)
        junk.append(f"local {lua_identifier_name(used,'_j')}=(({a}*{b})+{c})")
    header="\n".join(junk)+"\n"
    directive_block=("\n".join(directives)+"\n") if directives else ""
    output=directive_block+anti+header+pool_block+body
    if len(output.encode("utf-8"))>OBF_MAX_OUTPUT_BYTES:
        raise ValueError("The obfuscated result is larger than the 5 MB output limit.")
    features=[
        ("String protection","shuffled byte-wise constant pool"),
        ("Numeric folding","multi-term constant expressions"),
        ("Dead-code noise","4 randomized inert locals"),
        ("Anti-tamper","multi-point global, environment, debug-hook, and metadata integrity checks"),
        ("Minification","comments removed and syntax compacted"),
    ]
    return output,features



class ObfuscationResultView(discord.ui.LayoutView):
    def __init__(self,filename,result,raw_url,features):
        super().__init__(timeout=900)
        self.result=result
        self.filename=filename
        download=discord.ui.Button(label="Download Protected Lua",style=discord.ButtonStyle.success,emoji="⬇️")
        download.callback=self.download_result
        buttons=[download]
        if raw_url:
            buttons.insert(0,discord.ui.Button(label="View Raw Obfuscated",style=discord.ButtonStyle.link,emoji="🔗",url=raw_url))
        safe_filename=discord.utils.escape_markdown(filename)
        output_name=discord.utils.escape_markdown(os.path.splitext(filename)[0]+".obfuscated.lua")
        digest=hashlib.sha256(result.encode("utf-8")).hexdigest()
        self.add_item(
            make_container(
                make_text("### 🛡️ Done Obfuscated & Protected"),
                make_text(f"`{safe_filename}` has been protected with the built-in high-strength Lua profile."),
                make_separator(),
                make_text("### 🔐 Protection Stack\n"+"\n".join(f"**{name}:** `{value}`" for name,value in features)),
                make_separator(),
                make_text(f"**Output:** `{output_name}` · `{len(result.encode('utf-8')):,} bytes`\n**SHA-256:** `{digest[:20]}...`"),
                discord.ui.ActionRow(*buttons),
                make_separator(),
                make_text("⚠️ Obfuscation raises reverse-engineering cost but is not a guarantee of secrecy. Keep API keys, tokens, and other secrets out of distributed Lua code."),
                accent_color=0x57F287,
            )
        )

    async def download_result(self,interaction):
        try:
            await interaction.response.send_message(file=discord.File(io.BytesIO(self.result.encode("utf-8")),filename=os.path.splitext(self.filename)[0]+".obfuscated.lua"),ephemeral=True)
        except discord.HTTPException as error:
            await interaction.response.send_message(f"Download failed: {error}",ephemeral=True)


class ScriptUploadView(discord.ui.LayoutView):
    def __init__(self, title, script):
        super().__init__(timeout=1800)
        self.title_text=title
        self.script=script
        self.copy_button=discord.ui.Button(
            label="Copy Script",
            style=discord.ButtonStyle.primary,
            emoji="📋",
        )
        self.download_button=discord.ui.Button(
            label="Download .lua",
            style=discord.ButtonStyle.secondary,
            emoji="⬇️",
        )
        self.copy_button.callback=self.copy_script
        self.download_button.callback=self.download_script
        safe_title=discord.utils.escape_markdown(title)
        preview=self._preview(script)
        lines=script.count("\n")+1
        size=len(script.encode("utf-8"))
        self.add_item(
            make_container(
                make_text(f"## {safe_title}"),
                make_text("-# 📤 Script Upload · Ready to copy or download"),
                make_separator(),
                make_text(
                    f"### 🧾 Script Preview\n```lua\n{preview}\n```"
                ),
                make_separator(),
                make_text(
                    f"**Language:** `Lua / Luau`   **Lines:** `{lines:,}`   **Size:** `{size:,} bytes`"
                ),
                make_separator(),
                discord.ui.ActionRow(self.copy_button, self.download_button),
                make_separator(),
                make_text("✨ **Clean preview** · Use **Copy Script** for a copy-ready code block."),
                accent_color=0x5865F2,
            )
        )

    def _preview(self, script):
        limit=3300
        if len(script)<=limit:
            return script
        return script[:limit]+"\n\n-- … preview truncated. Use Copy Script for the full text. --"

    async def copy_script(self, interaction):
        safe_title=discord.utils.escape_markdown(self.title_text)
        if len(self.script)<=3850:
            view=discord.ui.LayoutView(timeout=300)
            view.add_item(
                make_container(
                    make_text(f"## 📋 Copy Ready · {safe_title}"),
                    make_separator(),
                    make_text(f"```lua\n{self.script}\n```"),
                    make_separator(),
                    make_text("Tap the code block's native **Copy** control to copy the script."),
                    accent_color=0x57F287,
                )
            )
            await interaction.response.send_message(view=view,ephemeral=True)
            return
        await interaction.response.send_message(
            f"The script is too long for a single copy-ready Discord code block. Use **Download .lua** for the complete `{safe_title}` script.",
            ephemeral=True,
        )

    async def download_script(self, interaction):
        filename=re.sub(r"[^A-Za-z0-9._-]+","_",self.title_text).strip("._") or "script"
        if not filename.lower().endswith((".lua",".luau")):
            filename += ".lua"
        try:
            await interaction.response.send_message(
                file=discord.File(io.BytesIO(self.script.encode("utf-8")),filename=filename),
                ephemeral=True,
            )
        except discord.HTTPException as error:
            await interaction.response.send_message(f"Download failed: {error}",ephemeral=True)


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


class LuaResultsView (discord .ui .LayoutView ):
    def __init__ (self ,filename ,dump_result ,dump_method ,deobf_result ,deobf_method ,deobf_name ,dump_raw ,deobf_raw ):
        super ().__init__ (timeout =900 )
        self .dump_result =dump_result
        self .deobf_result =deobf_result
        self .deobf_name =deobf_name

        self .dump_download =discord .ui .Button (
        label ="Download Dump",
        style =discord .ButtonStyle .secondary ,
        emoji ="📄",
        )
        self .dump_download .callback =self .download_dump

        self .deobf_download =discord .ui .Button (
        label ="Download Deobf",
        style =discord .ButtonStyle .success ,
        emoji ="⬇️",
        )
        self .deobf_download .callback =self .download_deobf

        dump_buttons =[]
        if dump_raw :
            dump_buttons .append (
            discord .ui .Button (
            label ="View Raw Dump",
            style =discord .ButtonStyle .link ,
            emoji ="🔎",
            url =dump_raw ,
            )
            )
        dump_buttons .append (self .dump_download )

        deobf_buttons =[]
        if deobf_raw :
            deobf_buttons .append (
            discord .ui .Button (
            label ="View Raw Deobf",
            style =discord .ButtonStyle .link ,
            emoji ="🔗",
            url =deobf_raw ,
            )
            )
        deobf_buttons .append (self .deobf_download )

        input_name =discord .utils .escape_markdown (filename )
        dump_name ="dump.txt"
        deobf_file =discord .utils .escape_markdown (deobf_name )
        dump_size =len (dump_result .encode ("utf-8"))
        deobf_size =len (deobf_result .encode ("utf-8"))

        self .add_item (
        make_container (
        make_text ("### 📜 Done Dumped & Deobf"),
        make_text (f"`{input_name }` was processed successfully."),
        make_separator (),
        make_text (
        f"### 📦 Dump\n"
        f"**Method:** `{discord .utils .escape_markdown (dump_method )}`\n"
        f"**Output:** `{dump_name }` · `{dump_size :,} bytes`"
        ),
        discord .ui .ActionRow (*dump_buttons ),
        make_separator (),
        make_text (
        f"### 🧹 Deobf\n"
        f"**Method:** `{discord .utils .escape_markdown (deobf_method )}`\n"
        f"**Output:** `{deobf_file }` · `{deobf_size :,} bytes`"
        ),
        discord .ui .ActionRow (*deobf_buttons ),
        accent_color =0x5865F2 ,
        )
        )

    async def download_dump (self ,interaction ):
        try :
            await interaction .response .send_message (
            file =discord .File (
            io .BytesIO (self .dump_result .encode ("utf-8")),
            filename ="dump.txt",
            ),
            ephemeral =True ,
            )
        except discord .HTTPException as error :
            await interaction .response .send_message (
            f"Download failed: {error }",
            ephemeral =True ,
            )

    async def download_deobf (self ,interaction ):
        try :
            await interaction .response .send_message (
            file =discord .File (
            io .BytesIO (self .deobf_result .encode ("utf-8")),
            filename =self .deobf_name ,
            ),
            ephemeral =True ,
            )
        except discord .HTTPException as error :
            await interaction .response .send_message (
            f"Download failed: {error }",
            ephemeral =True ,
            )

    async def on_timeout (self ):
        self .stop ()


def is_public_http_url (value ):
    try :
        parsed =urlparse (value )
    except ValueError :
        return False 
    if parsed .scheme .lower ()not in {"http","https"}or not parsed .hostname or parsed .username or parsed .password :
        return False 
    try :
        addresses =socket .getaddrinfo (parsed .hostname ,parsed .port or (443 if parsed .scheme .lower ()=="https"else 80 ),type =socket .SOCK_STREAM )
    except (socket .gaierror ,OSError ,ValueError ):
        return False 
    for address in addresses :
        try :
            ip =ipaddress .ip_address (address [4 ][0 ])
        except ValueError :
            return False 
        if ip .is_private or ip .is_loopback or ip .is_link_local or ip .is_multicast or ip .is_reserved or ip .is_unspecified :
            return False 
    return True 


class SafeRedirectHandler (urllib .request .HTTPRedirectHandler ):
    def redirect_request (self ,req ,fp ,code ,msg ,headers ,newurl ):
        if not is_public_http_url (newurl ):
            raise urllib .error .URLError ("Redirect target is not a public HTTP(S) URL.")
        return super ().redirect_request (req ,fp ,code ,msg ,headers ,newurl )


def fetch_remote_lua_sync (url ):
    if not is_public_http_url (url ):
        raise RuntimeError ("The raw link must be a public HTTP(S) URL.")
    request =urllib .request .Request (
    url ,
    headers ={"Accept":"text/plain,text/*;q=0.9,*/*;q=0.1","User-Agent":"PanelBot/1.0"},
    method ="GET",
    )
    opener =urllib .request .build_opener (SafeRedirectHandler )
    try :
        with opener .open (request ,timeout =15 )as response :
            content_length =response .headers .get ("Content-Length")
            if content_length :
                try :
                    if int (content_length )>LUA_PROCESS_MAX_BYTES :
                        raise RuntimeError ("That raw file is too large. The maximum size is 2 MB.")
                except ValueError :
                    pass 
            chunks =[]
            total =0 
            while True :
                chunk =response .read (65536 )
                if not chunk :
                    break 
                total +=len (chunk )
                if total >LUA_PROCESS_MAX_BYTES :
                    raise RuntimeError ("That raw file is too large. The maximum size is 2 MB.")
                chunks .append (chunk )
    except urllib .error .HTTPError as error :
        raise RuntimeError (f"The raw link returned HTTP {error .code }.")from error 
    except urllib .error .URLError as error :
        raise RuntimeError (f"Could not fetch the raw link: {error .reason }")from error 
    data =b"".join (chunks )
    if not data :
        raise RuntimeError ("The raw file is empty.")
    try :
        data .decode ("utf-8-sig")
    except UnicodeDecodeError as error :
        raise RuntimeError ("The raw link must contain a UTF-8 Lua or TXT file.")from error 
    path_name =os .path .basename (urlparse (url ).path )
    filename =path_name if path_name .lower ().endswith ((".lua",".luau",".txt"))else "remote.lua"
    return filename ,data 


async def process_l_command (ctx ,filename ,data ):
    status =await ctx .send (f"⏳ **Lua Toolkit**\nProcessing `{discord .utils .escape_markdown (filename )}`...")
    workdir =tempfile .mkdtemp (prefix ="lua_tool_")
    try :
        dump_result ,dump_method ,deobf_result ,deobf_method ,deobf_name =await asyncio .to_thread (
        build_lua_results ,
        filename ,
        data ,
        workdir ,
        )
        dump_result ,dump_truncated =cap_result (dump_result )
        deobf_result ,deobf_truncated =cap_result (deobf_result )
        if dump_truncated :
            dump_method +=" · output capped at 5 MB"
        if deobf_truncated :
            deobf_method +=" · output capped at 5 MB"
        dump_raw =None 
        deobf_raw =None 
        if PASTEFY_API_TOKEN :
            results =await asyncio .gather (
            asyncio .to_thread (create_pastefy_paste_sync ,"dump.txt",dump_result ,PASTEFY_API_TOKEN ),
            asyncio .to_thread (create_pastefy_paste_sync ,deobf_name ,deobf_result ,PASTEFY_API_TOKEN ),
            return_exceptions =True ,
            )
            if isinstance (results [0 ],str ):
                dump_raw =results [0 ]
            if isinstance (results [1 ],str ):
                deobf_raw =results [1 ]
        view =LuaResultsView (
        filename ,
        dump_result ,
        dump_method ,
        deobf_result ,
        deobf_method ,
        deobf_name ,
        dump_raw ,
        deobf_raw ,
        )
        await status .edit (content =None ,view =view )
    except Exception as error :
        await status .edit (content =f"❌ **Lua processing failed**\n`{discord .utils .escape_markdown (str (error )[:1500 ])}`")
    finally :
        shutil .rmtree (workdir ,ignore_errors =True )


@bot .command (name ="l")
async def lua_tool_command (ctx :commands .Context ,source :str =None ):
    attachments =list (ctx .message .attachments )
    if len (attachments )>1 :
        await ctx .send ("Use exactly one `.lua`, `.luau`, or `.txt` attachment, or provide one raw HTTP(S) link.")
        return 
    if attachments and source :
        await ctx .send ("Use either one Lua attachment or one raw HTTP(S) link, not both.")
        return 
    if not attachments and not source :
        await ctx .send ("Usage: `.l` with one `.lua`, `.luau`, or `.txt` attachment, or `.l <raw link>`.")
        return 
    if source :
        status =await ctx .send ("⏳ **Lua Toolkit**\nFetching the raw Lua/TXT file...")
        try :
            filename ,data =await asyncio .to_thread (fetch_remote_lua_sync ,source .strip ())
        except Exception as error :
            await status .edit (content =f"❌ **Could not fetch the raw link**\n`{discord .utils .escape_markdown (str (error )[:1500 ])}`")
            return 
        await status .delete ()
    else :
        attachment =attachments [0 ]
        filename =os .path .basename (attachment .filename or "lua_input.lua")
        extension =os .path .splitext (filename )[1 ].lower ()
        if extension not in {".lua",".luau",".txt"}:
            await ctx .send ("Only `.lua`, `.luau`, and `.txt` files are supported.")
            return 
        if attachment .size is not None and attachment .size >LUA_PROCESS_MAX_BYTES :
            await ctx .send ("That file is too large. The maximum size is 2 MB.")
            return 
        try :
            data =await attachment .read ()
        except discord .HTTPException as error :
            await ctx .send (f"I could not read that file: {error }")
            return 
        if len (data )>LUA_PROCESS_MAX_BYTES :
            await ctx .send ("That file is too large. The maximum size is 2 MB.")
            return 
        if not data :
            await ctx .send ("The uploaded file is empty.")
            return 
        try :
            data .decode ("utf-8-sig")
        except UnicodeDecodeError :
            await ctx .send ("The uploaded file must be valid UTF-8 Lua or TXT text.")
            return 
    await process_l_command (ctx ,filename ,data )


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


async def process_obf_command(ctx,filename,data):
    status=await ctx.send(f"⏳ **Obfuscator**\nProtecting `{discord.utils.escape_markdown(filename)}`...")
    workdir=tempfile.mkdtemp(prefix="lua_obf_")
    try:
        source=data.decode("utf-8-sig")
    except UnicodeDecodeError:
        shutil.rmtree(workdir,ignore_errors=True)
        await status.edit(content="❌ **Obfuscation failed**\nThe input must be valid UTF-8 Lua/Luau source.")
        return
    try:
        prometheus_result,features,engine,prometheus_error=await asyncio.to_thread(run_prometheus,source,filename,workdir)
        if prometheus_result is not None:
            result=prometheus_result
        else:
            result,features=await asyncio.to_thread(obfuscate_lua_source,source)
            engine="Built-in"
            features=[
                ("Engine","Built-in high-strength profile"),
                ("String protection","shuffled byte-wise constant pool"),
                ("Numeric folding","multi-term constant expressions"),
                ("Dead-code noise","4 randomized inert locals"),
                ("Anti-tamper","multi-point global, environment, debug-hook, and metadata integrity checks"),
                ("Minification","comments removed and syntax compacted"),
            ]
        raw_url=None
        if PASTEFY_API_TOKEN:
            try:
                raw_url=await asyncio.to_thread(create_pastefy_paste_sync,os.path.splitext(filename)[0]+".obfuscated.lua",result,PASTEFY_API_TOKEN)
            except Exception:
                raw_url=None
        view=ObfuscationResultView(filename,result,raw_url,features)
        await status.edit(content=None,view=view)
    except Exception as error:
        await status.edit(content=f"❌ **Obfuscation failed**\n`{discord.utils.escape_markdown(str(error)[:1500])}`")
    finally:
        shutil.rmtree(workdir,ignore_errors=True)


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


@bot.command(name="obf")
async def lua_obfuscate_command(ctx:commands.Context,source:str=None):
    attachments=list(ctx.message.attachments)
    if len(attachments)>1:
        await ctx.send("Use exactly one `.lua`, `.luau`, or `.txt` attachment, or provide one raw HTTP(S) link.")
        return
    if attachments and source:
        await ctx.send("Use either one Lua attachment or one raw HTTP(S) link, not both.")
        return
    if not attachments and not source:
        await ctx.send("Usage: `.obf` with one `.lua`, `.luau`, or `.txt` attachment, or `.obf <raw link>`." )
        return
    if source:
        status=await ctx.send("⏳ **Obfuscator**\nFetching the raw Lua/Luau file...")
        try:
            filename,data=await asyncio.to_thread(fetch_remote_lua_sync,source.strip())
            await status.delete()
            await process_obf_command(ctx,filename,data)
        except Exception as error:
            await status.edit(content=f"❌ **Obfuscation failed**\n`{discord.utils.escape_markdown(str(error)[:1500])}`")
        return
    attachment=attachments[0]
    filename=os.path.basename(attachment.filename or "input.lua")
    extension=os.path.splitext(filename)[1].lower()
    if extension not in {".lua",".luau",".txt"}:
        await ctx.send("Only `.lua`, `.luau`, and `.txt` files are supported.")
        return
    if attachment.size is not None and attachment.size>LUA_PROCESS_MAX_BYTES:
        await ctx.send("That file is too large. The maximum size is 2 MB.")
        return
    try:
        data=await attachment.read()
    except discord.HTTPException as error:
        await ctx.send(f"Could not read the attachment: {error}")
        return
    if len(data)>LUA_PROCESS_MAX_BYTES:
        await ctx.send("That file is too large. The maximum size is 2 MB.")
        return
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        await ctx.send("The uploaded file must be valid UTF-8 Lua/Luau text.")
        return
    await process_obf_command(ctx,filename,data)


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

        if not initialized :
            for _ in joined :
                await mongo_call (
                add_member_event_sync ,
                interaction .guild .id ,
                "join",
                )
            for _ in left :
                await mongo_call (
                add_member_event_sync ,
                interaction .guild .id ,
                "leave",
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
channel ="Channel where the update will be posted",
)
async def update_logs (
interaction :discord .Interaction ,
title :str ,
version :str ,
change_logs :str ,
channel :discord .TextChannel ,
message :str |None =None ,
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

        container =make_container (
        make_text (f"## {clean_title }"),
        make_text (f"-# {version_text }"),
        make_separator (),
        make_text (
        f"### CHANGE LOGS\n```diff\n{change_content }```"
        ),
        *(
        [make_text (clean_message )]
        if clean_message 
        else []
        ),
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
description ="Post a clean Components V2 script preview",
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

    try:
        await interaction.response.send_message(view=ScriptUploadView(clean_title,clean_script))
    except discord.HTTPException as error:
        message=f"Could not post the script preview: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(message,ephemeral=True)
        else:
            await interaction.response.send_message(message,ephemeral=True)


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
                if not initialized :
                    for _ in joined :
                        await mongo_call (
                        add_member_event_sync ,
                        guild .id ,
                        "join",
                        )
                    for _ in left :
                        await mongo_call (
                        add_member_event_sync ,
                        guild .id ,
                        "leave",
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
