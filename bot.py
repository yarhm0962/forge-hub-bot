import os,io,json,asyncio,hashlib,hmac,random,re,secrets,shutil,subprocess,sys,tempfile,string
from datetime import datetime,timedelta,timezone
import discord
from discord import app_commands
from discord.ext import commands

TOKEN=os.getenv("DISCORD_TOKEN")
INSIGHTS_FILE="server_insights.json"
ANTI_SCAM_FILE="anti_scam_channels.json"
MAX_OBFUSCATE_BYTES=2*1024*1024
if not TOKEN: raise RuntimeError("DISCORD_TOKEN environment variable is missing")

intents=discord.Intents.default()
intents.guilds=True
intents.members=True
intents.message_content=True
bot=commands.Bot(command_prefix="!",intents=intents)

ANTI_SCAM_MESSAGE=("This channel is protected by the server moderation system.\n\n"
"Please do not send messages here. Messages sent in this channel may result in an immediate kick from the server.\n\n"
"If you have read and understood this notice, react with 👍 below.")

KEYWORDS={
    "and","break","do","else","elseif","end","false","for","function","goto","if","in",
    "local","nil","not","or","repeat","return","then","true","until","while"
}

MULTI_OPS=("...","==","~=","<=",">=","::","//","<<",">>","..",
           "+=","-=","*=","/=","%=","^=","&=","|=","->")

IDENT_RE=re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_RE=re.compile(
    r"(?:0[xX][0-9A-Fa-f]+(?:\.[0-9A-Fa-f]*)?(?:[pP][+-]?[0-9]+)?"
    r"|0[bB][01]+"
    r"|(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?)"
)

class LuaToken:
    __slots__=("kind","value")
    def __init__(self,kind,value):
        self.kind=kind
        self.value=value

def long_bracket_end(source,start):
    if start>=len(source) or source[start]!="[":
        return None
    i=start+1
    while i<len(source) and source[i]=="=":
        i+=1
    if i>=len(source) or source[i]!="[":
        return None
    eq=i-start-1
    close="]"+("="*eq)+"]"
    end=source.find(close,i+1)
    if end<0:
        return None
    return end+len(close)

def lua_lex(source):
    tokens=[]
    i=0
    n=len(source)
    while i<n:
        c=source[i]
        if c.isspace():
            i+=1
            continue
        if source.startswith("--",i):
            lb=long_bracket_end(source,i+2)
            if lb is not None:
                i=lb
            else:
                j=source.find("\n",i+2)
                i=n if j<0 else j+1
            continue
        if c in "\"'":
            quote=c
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
                raise ValueError("Unterminated string literal")
            tokens.append(LuaToken("string",source[i:j]))
            i=j
            continue
        lb=long_bracket_end(source,i)
        if lb is not None:
            tokens.append(LuaToken("string",source[i:lb]))
            i=lb
            continue
        m=IDENT_RE.match(source,i)
        if m:
            value=m.group(0)
            tokens.append(LuaToken("keyword" if value in KEYWORDS else "ident",value))
            i=m.end()
            continue
        m=NUMBER_RE.match(source,i)
        if m:
            tokens.append(LuaToken("number",m.group(0)))
            i=m.end()
            continue
        matched=None
        for op in MULTI_OPS:
            if source.startswith(op,i):
                matched=op
                break
        if matched is not None:
            tokens.append(LuaToken("op",matched))
            i+=len(matched)
            continue
        tokens.append(LuaToken("op",c))
        i+=1
    return tokens

def all_identifier_values(tokens):
    return {t.value for t in tokens if t.kind=="ident"}

def previous_token(tokens,i):
    return tokens[i-1] if i>0 else None

def next_token(tokens,i):
    return tokens[i+1] if i+1<len(tokens) else None

def is_table_key(tokens,i):
    prev=previous_token(tokens,i)
    nxt=next_token(tokens,i)
    if prev and prev.value in (".",":"):
        return True
    if nxt and nxt.value=="=" and prev and prev.value in ("{",",",";"):
        return True
    return False

def is_property_name(tokens,i):
    prev=previous_token(tokens,i)
    return bool(prev and prev.value in (".",":"))

def generate_obfuscated_name(rng,used,length=10):
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    first="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
    while True:
        name=rng.choice(first)+"".join(rng.choice(alphabet) for _ in range(length-1))
        if name not in used and name not in KEYWORDS:
            used.add(name)
            return name

def collect_local_candidates(tokens):
    counts={}
    declaration_indexes=set()

    def add(index):
        if 0<=index<len(tokens) and tokens[index].kind=="ident":
            name=tokens[index].value
            counts[name]=counts.get(name,0)+1
            declaration_indexes.add(index)

    i=0
    while i<len(tokens):
        if tokens[i].value=="local":
            j=i+1
            if j<len(tokens) and tokens[j].value=="function":
                j+=1
                if j<len(tokens) and tokens[j].kind=="ident":
                    add(j)
                i=j
                continue
            while j<len(tokens):
                if tokens[j].kind!="ident":
                    break
                add(j)
                j+=1
                if j<len(tokens) and tokens[j].value==",":
                    j+=1
                    continue
                break
        elif tokens[i].value=="function":
            j=i+1
            if j<len(tokens) and tokens[j].kind=="ident":
                j+=1
            if j<len(tokens) and tokens[j].value==":":
                j+=1
                if j<len(tokens) and tokens[j].kind=="ident":
                    j+=1
            if j<len(tokens) and tokens[j].value=="(":
                depth=1
                j+=1
                while j<len(tokens) and depth:
                    if tokens[j].value=="(":
                        depth+=1
                    elif tokens[j].value==")":
                        depth-=1
                        if depth==0:
                            break
                    elif depth==1 and tokens[j].kind=="ident":
                        add(j)
                    j+=1
                i=j
        i+=1

    return counts,declaration_indexes

def choose_local_mappings(tokens,rng):
    counts,declaration_indexes=collect_local_candidates(tokens)
    all_names=all_identifier_values(tokens)
    mapping={}
    used=set(all_names)

    for name,count in counts.items():
        if name in KEYWORDS or len(name)<2:
            continue
        occurrences=sum(1 for t in tokens if t.kind=="ident" and t.value==name)
        if occurrences<2:
            continue
        mapping[name]=generate_obfuscated_name(rng,used,rng.randint(9,14))

    return mapping,declaration_indexes

def transform_identifiers(tokens,mapping,declaration_indexes):
    out=[]
    for i,t in enumerate(tokens):
        if t.kind=="ident" and t.value in mapping:
            if i in declaration_indexes or not is_table_key(tokens,i):
                if not is_property_name(tokens,i):
                    out.append(LuaToken("ident",mapping[t.value]))
                    continue
        out.append(t)
    return out

def transform_numbers(tokens,rng):
    out=[]
    for t in tokens:
        if t.kind!="number":
            out.append(t)
            continue
        value=t.value
        if not re.fullmatch(r"[0-9]{1,3}",value):
            out.append(t)
            continue
        number=int(value)
        if number<2 or number>99:
            out.append(t)
            continue
        if rng.random()<0.5:
            a=rng.randint(1,number-1)
            b=number-a
            out.extend([
                LuaToken("op","("),
                LuaToken("number",str(a)),
                LuaToken("op","+"),
                LuaToken("number",str(b)),
                LuaToken("op",")")
            ])
        else:
            c=rng.randint(1,number)
            d=number+c
            out.extend([
                LuaToken("op","("),
                LuaToken("number",str(d)),
                LuaToken("op","-"),
                LuaToken("number",str(c)),
                LuaToken("op",")")
            ])
    return out

def pool_strings(tokens,rng):
    entries=[]
    index={}
    for t in tokens:
        if t.kind=="string" and t.value not in index:
            index[t.value]=len(entries)+1
            entries.append(t.value)

    if not entries:
        return tokens,None

    order=list(range(len(entries)))
    rng.shuffle(order)
    remap={}
    pool_values=[]

    for new_index,old_index in enumerate(order,1):
        remap[old_index+1]=new_index
        pool_values.append(entries[old_index])

    used=all_identifier_values(tokens)
    pool_name="__"+generate_obfuscated_name(rng,used,12)

    out=[]
    for t in tokens:
        if t.kind=="string":
            original_index=index[t.value]
            out.extend([
                LuaToken("ident",pool_name),
                LuaToken("op","["),
                LuaToken("number",str(remap[original_index])),
                LuaToken("op","]")
            ])
        else:
            out.append(t)

    declaration=[
        LuaToken("keyword","local"),
        LuaToken("ident",pool_name),
        LuaToken("op","="),
        LuaToken("op","{")
    ]

    for i,value in enumerate(pool_values):
        if i:
            declaration.append(LuaToken("op",","))
        declaration.append(LuaToken("string",value))

    declaration.append(LuaToken("op","}"))
    return out,declaration

def needs_space(a,b):
    if a is None or b is None:
        return False
    if a.kind in ("ident","keyword","number") and b.kind in ("ident","keyword","number"):
        return True
    if a.value=="-" and b.value=="-":
        return True
    if a.value=="." and b.value==".":
        return True
    if a.value=="." and b.kind=="number":
        return True
    if a.value=="-" and b.value==">":
        return True
    return False

def render_lua(tokens):
    pieces=[]
    prev=None
    for t in tokens:
        if needs_space(prev,t):
            pieces.append(" ")
        pieces.append(t.value)
        prev=t
    return "".join(pieces)

def validate_lua_source(source):
    luac=shutil.which("luac")
    if luac:
        fd,path=tempfile.mkstemp(suffix=".lua")
        os.close(fd)
        try:
            with open(path,"w",encoding="utf-8",newline="\n") as f:
                f.write(source)
            result=subprocess.run(
                [luac,"-p",path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            if result.returncode!=0:
                return False,result.stderr.strip() or "luac rejected the generated Lua"
            return True,"luac syntax validation passed"
        except subprocess.TimeoutExpired:
            return False,"luac validation timed out"
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    return True,"luac not installed; structural validation completed"

def obfuscate_lua(source,seed=None):
    if seed is None:
        seed=random.SystemRandom().randint(1,2**63-1)

    rng=random.Random(seed)
    tokens=lua_lex(source)

    mapping,declaration_indexes=choose_local_mappings(tokens,rng)
    tokens=transform_identifiers(tokens,mapping,declaration_indexes)
    tokens=transform_numbers(tokens,rng)

    tokens,pool_declaration=pool_strings(tokens,rng)
    if pool_declaration:
        tokens=pool_declaration+tokens

    output=render_lua(tokens)
    if not output.endswith("\n"):
        output+="\n"

    ok,validation=validate_lua_source(output)
    if not ok:
        raise ValueError(validation)

    return output,seed,validation,len(mapping),bool(pool_declaration)

def load_json(name,default):
    if not os.path.exists(name): return default
    try:
        with open(name,"r",encoding="utf-8") as f: return json.load(f)
    except (json.JSONDecodeError,OSError): return default

def save_json(name,data):
    tmp=name+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(data,f,indent=2)
    os.replace(tmp,name)

insights_data=load_json(INSIGHTS_FILE,{})
anti_scam_data=load_json(ANTI_SCAM_FILE,{})

def guild_insights(gid):
    k=str(gid)
    if k not in insights_data: insights_data[k]={"joins":[],"leaves":[]}
    return insights_data[k]

def cleanup_events(data):
    cutoff=datetime.now(timezone.utc)-timedelta(days=30)
    for kind in ("joins","leaves"):
        data[kind]=[x for x in data.get(kind,[]) if _valid_recent(x,cutoff)]

def _valid_recent(value,cutoff):
    try: return datetime.fromisoformat(value)>=cutoff
    except ValueError: return False

def sha256_text(value): return hashlib.sha256(value.encode("utf-8")).hexdigest()

class AntiScamPanel(discord.ui.LayoutView):
    def __init__(self,kicks=0):
        super().__init__(timeout=None); self.kicks=kicks
        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 🛡️ Anti-Scam Protection"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(ANTI_SCAM_MESSAGE),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay("### ⚠️ Automatic Moderation\nThis channel is monitored automatically. Messages sent here are removed and the sender may be kicked from the server."),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.ActionRow(discord.ui.Button(label=f"kicks: {kicks}",style=discord.ButtonStyle.secondary,disabled=True))
        ); self.add_item(self.container)
    def update_kicks(self):
        for item in self.container.children:
            if isinstance(item,discord.ui.ActionRow):
                for button in item.children:
                    if isinstance(button,discord.ui.Button): button.label=f"kicks: {self.kicks}"

class InsightsPanel(discord.ui.LayoutView):
    def __init__(self,guild):
        super().__init__(timeout=None); d=guild_insights(guild.id); cleanup_events(d)
        joins,leaves=len(d["joins"]),len(d["leaves"]); current=guild.member_count or 0; net=joins-leaves
        growth=f"+{net:,}" if net>0 else f"{net:,}"
        status="📈 Growing" if net>0 else "📉 Declining" if net<0 else "➖ Stable"
        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 📊 Server Insights"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(f"### {guild.name}\nA clean overview of member activity across the last 30 days."),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(f"👥 **Current Members**\n`{current:,}`"),
            discord.ui.TextDisplay(f"🟢 **New Members**\n`{joins:,}` joined during the last 30 days."),
            discord.ui.TextDisplay(f"🔴 **Departures**\n`{leaves:,}` left during the last 30 days."),
            discord.ui.TextDisplay(f"📈 **Net Change**\n`{growth}` members"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(f"**{status}**\nJoin and leave activity is automatically tracked over a rolling 30-day period.")
        ); self.add_item(self.container)

class PurgePanel(discord.ui.LayoutView):
    def __init__(self,count,deleted):
        super().__init__(timeout=None)
        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 🧹 Messages Cleared"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(f"Successfully cleared **{deleted:,}** message{'s' if deleted!=1 else ''}."),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(f"**Requested:** `{count:,}`\n**Deleted:** `{deleted:,}`\n**Channel:** Current channel")
        ); self.add_item(self.container)

class ObfuscatePanel(discord.ui.LayoutView):
    def __init__(self,name,original,protected,digest):
        super().__init__(timeout=None)
        original_kb=original/1024
        protected_kb=protected/1024
        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 🔐 Obfuscation Complete"),
            discord.ui.TextDisplay("Your Lua source has been successfully protected and is ready to download."),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(
                f"### 📦 Output File\n"
                f"`{name}`\n\n"
                f"**Original** · `{original_kb:.1f} KB`\n"
                f"**Protected** · `{protected_kb:.1f} KB`"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(
                "### 🛡️ Protection\n"
                "• Identifier mangling\n"
                "• Number transformation\n"
                "• String pooling\n"
                "• Randomized transformation seed"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(
                f"### 🔎 File Integrity\n"
                f"**SHA-256**\n`{digest[:16]}...`"
            ),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(
                "📎 **The protected Lua file is attached to this message.**\n"
                "Download the attachment below to use the obfuscated source."
            ),
            discord.ui.File(f"attachment://{name}")
        )
        self.add_item(self.container)

create_group=app_commands.Group(name="create",description="Create server tools")
anti_group=app_commands.Group(name="anti",description="Anti moderation tools",parent=create_group)
server_group=app_commands.Group(name="server",description="Server information and tools")

@anti_group.command(name="scam",description="Create an anti-scam protection channel")
@app_commands.describe(name="The exact name of the channel to create")
@app_commands.checks.has_permissions(manage_channels=True)
async def anti_scam(interaction:discord.Interaction,name:str):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server.",ephemeral=True); return
    await interaction.response.defer(ephemeral=True); me=interaction.guild.me
    if me is None:
        await interaction.followup.send("I could not verify my server permissions.",ephemeral=True); return
    if not me.guild_permissions.manage_channels:
        await interaction.followup.send("I need the Manage Channels permission.",ephemeral=True); return
    if not me.guild_permissions.kick_members:
        await interaction.followup.send("I need the Kick Members permission.",ephemeral=True); return
    try:
        channel=await interaction.guild.create_text_channel(name); panel=AntiScamPanel(); msg=await channel.send(view=panel)
        try: await msg.add_reaction("👍")
        except discord.HTTPException: pass
        anti_scam_data[str(channel.id)]={"guild_id":interaction.guild.id,"channel_id":channel.id,"message_id":msg.id,"kicks":0}
        save_json(ANTI_SCAM_FILE,anti_scam_data)
        created_channels[channel.id]={"panel":panel,"message":msg,"guild_id":interaction.guild.id}
        await interaction.followup.send(f"Created {channel.mention}.",ephemeral=True)
    except discord.Forbidden: await interaction.followup.send("I don't have permission to create or manage that channel.",ephemeral=True)
    except discord.HTTPException as error: await interaction.followup.send(f"Discord returned an error: {error}",ephemeral=True)

@server_group.command(name="insights",description="View member activity from the last 30 days")
@app_commands.checks.has_permissions(manage_guild=True)
async def server_insights(interaction:discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used inside a server.",ephemeral=True); return
    data=guild_insights(interaction.guild.id); cleanup_events(data); save_json(INSIGHTS_FILE,insights_data)
    await interaction.response.send_message(view=InsightsPanel(interaction.guild))

@bot.tree.command(name="purge",description="Delete recent messages from the current channel")
@app_commands.describe(count="Number of messages to delete (1-1000)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction:discord.Interaction,count:app_commands.Range[int,1,1000]):
    if not isinstance(interaction.channel,discord.TextChannel):
        await interaction.response.send_message("This command can only be used in a text channel.",ephemeral=True); return
    me=interaction.guild.me
    if me is None:
        await interaction.response.send_message("I could not verify my permissions.",ephemeral=True); return
    permissions=interaction.channel.permissions_for(me)
    if not permissions.manage_messages:
        await interaction.response.send_message("I need the Manage Messages permission in this channel.",ephemeral=True); return
    if not permissions.read_message_history:
        await interaction.response.send_message("I need the Read Message History permission in this channel.",ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted=await interaction.channel.purge(limit=count,bulk=True,reason=f"Purge requested by {interaction.user}")
        await interaction.followup.send(view=PurgePanel(count,len(deleted)),ephemeral=True)
    except discord.Forbidden: await interaction.followup.send("I don't have permission to delete messages in this channel.",ephemeral=True)
    except discord.HTTPException as error: await interaction.followup.send(f"Discord returned an error while purging messages: {error}",ephemeral=True)

@bot.tree.command(name="obfuscate",description="Obfuscate a Lua source file")
@app_commands.describe(file="Lua or TXT source file to obfuscate")
async def obfuscate(interaction:discord.Interaction,file:discord.Attachment):
    filename=file.filename or ""; ext=os.path.splitext(filename)[1].lower()
    if ext not in {".lua",".txt"}:
        await interaction.response.send_message("Unsupported file type. Please upload a `.lua` or `.txt` file.",ephemeral=True); return
    if file.size is not None and file.size>MAX_OBFUSCATE_BYTES:
        await interaction.response.send_message("The file is too large. The maximum supported size is 2 MB.",ephemeral=True); return
    await interaction.response.defer()
    try:
        raw=await file.read()
        if len(raw)>MAX_OBFUSCATE_BYTES:
            await interaction.followup.send("The file is too large. The maximum supported size is 2 MB.",ephemeral=True); return
        try: source=raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            await interaction.followup.send("The file must contain valid UTF-8 text.",ephemeral=True); return
        if not source.strip():
            await interaction.followup.send("The uploaded file is empty.",ephemeral=True); return
        protected,seed,validation,mangled_count,pooled_strings=obfuscate_lua(source)
        if not protected.strip(): raise RuntimeError("The obfuscator produced an empty output.")
        data=protected.encode("utf-8")
        digest=sha256_text(protected)
        base=os.path.splitext(os.path.basename(filename))[0]
        output_name=f"{base}.obfuscated{ext}"
        output=discord.File(io.BytesIO(data),filename=output_name)
        await interaction.followup.send(
            view=ObfuscatePanel(output_name,len(raw),len(data),digest),
            files=[output]
        )
    except discord.HTTPException as error:
        try: await interaction.followup.send(f"Discord returned an error while sending the protected file: {error}",ephemeral=True)
        except discord.HTTPException: pass
    except Exception as error:
        try: await interaction.followup.send(f"Obfuscation failed: {error}",ephemeral=True)
        except discord.HTTPException: pass

@bot.event
async def on_member_join(member):
    d=guild_insights(member.guild.id); d["joins"].append(datetime.now(timezone.utc).isoformat()); cleanup_events(d); save_json(INSIGHTS_FILE,insights_data)

@bot.event
async def on_member_remove(member):
    d=guild_insights(member.guild.id); d["leaves"].append(datetime.now(timezone.utc).isoformat()); cleanup_events(d); save_json(INSIGHTS_FILE,insights_data)

@bot.event
async def on_message(message):
    if message.author.bot: return
    data=created_channels.get(message.channel.id)
    if data is None: return
    member=message.author
    if not isinstance(member,discord.Member) or member.guild_permissions.administrator: return
    me=message.guild.me
    if me is None or not me.guild_permissions.kick_members or member.top_role>=me.top_role: return
    try: await message.delete()
    except (discord.Forbidden,discord.NotFound,discord.HTTPException): pass
    try:
        await member.kick(reason="Message sent in anti-scam channel")
        data["panel"].kicks+=1; data["panel"].update_kicks()
        record=anti_scam_data.get(str(message.channel.id))
        if record: record["kicks"]=data["panel"].kicks; save_json(ANTI_SCAM_FILE,anti_scam_data)
        if data["message"]:
            try: await data["message"].edit(view=data["panel"])
            except (discord.NotFound,discord.Forbidden,discord.HTTPException): pass
    except (discord.Forbidden,discord.NotFound,discord.HTTPException): pass

async def restore_anti_scam_channels():
    stale=[]
    for key,record in list(anti_scam_data.items()):
        try: cid,mid,gid,kicks=int(record["channel_id"]),int(record["message_id"]),int(record["guild_id"]),int(record.get("kicks",0))
        except (KeyError,TypeError,ValueError): stale.append(key); continue
        guild=bot.get_guild(gid)
        if guild is None: continue
        channel=guild.get_channel(cid)
        if not isinstance(channel,discord.TextChannel): stale.append(key); continue
        panel=AntiScamPanel(kicks)
        try: msg=await channel.fetch_message(mid)
        except discord.NotFound: stale.append(key); continue
        except discord.Forbidden: created_channels[cid]={"panel":panel,"message":None,"guild_id":gid}; continue
        except discord.HTTPException: continue
        try: await msg.edit(view=panel)
        except (discord.Forbidden,discord.NotFound,discord.HTTPException): pass
        created_channels[cid]={"panel":panel,"message":msg,"guild_id":gid}
    for key in stale: anti_scam_data.pop(key,None)
    save_json(ANTI_SCAM_FILE,anti_scam_data)

@bot.event
async def on_ready():
    try:
        synced=await bot.tree.sync(); await restore_anti_scam_channels()
        print(f"Logged in as {bot.user} ({bot.user.id})")
        print(f"Synced {len(synced)} command(s)")
        print(f"Restored {len(created_channels)} anti-scam channel(s)")
    except Exception as error: print(f"Startup error: {error}")

@anti_scam.error
async def anti_scam_error(interaction,error):
    message="You need the Manage Channels permission to use this command." if isinstance(error,app_commands.MissingPermissions) else f"Command error: {error}"
    if interaction.response.is_done(): await interaction.followup.send(message,ephemeral=True)
    else: await interaction.response.send_message(message,ephemeral=True)

@server_insights.error
async def server_insights_error(interaction,error):
    message="You need the Manage Server permission to use this command." if isinstance(error,app_commands.MissingPermissions) else f"Command error: {error}"
    if interaction.response.is_done(): await interaction.followup.send(message,ephemeral=True)
    else: await interaction.response.send_message(message,ephemeral=True)

@purge.error
async def purge_error(interaction,error):
    message="You need the Manage Messages permission to use this command." if isinstance(error,app_commands.MissingPermissions) else f"Command error: {error}"
    if interaction.response.is_done(): await interaction.followup.send(message,ephemeral=True)
    else: await interaction.response.send_message(message,ephemeral=True)

async def start_bot():
    while True:
        try:
            await bot.start(TOKEN); break
        except discord.HTTPException as error:
            retry_after=getattr(error,"retry_after",30); print(f"Discord connection error: {error}"); print(f"Retrying in {retry_after:.1f} seconds..."); await asyncio.sleep(retry_after)
        except discord.LoginFailure:
            print("Invalid Discord bot token."); break
        except Exception as error:
            print(f"Bot error: {error}"); await asyncio.sleep(30)

bot.tree.add_command(create_group)
bot.tree.add_command(server_group)
asyncio.run(start_bot())
