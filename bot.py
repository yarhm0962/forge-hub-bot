import os,io,json,asyncio,hashlib,random,re,secrets,string
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

KEYWORDS={"and","break","do","else","elseif","end","false","for","function","goto","if","in","local","nil","not","or","repeat","return","then","true","until","while"}
BUILTINS={"assert","collectgarbage","coroutine","debug","dofile","error","getmetatable","io","ipairs","load","loadfile","math","next","os","pairs","pcall","print","rawequal","rawget","rawlen","rawset","require","select","setmetatable","string","table","tonumber","tostring","type","utf8","warn","xpcall"}
TOKEN_RE=re.compile(r"""(?P<space>\s+)|(?P<comment>--[^\n]*)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<number>0[xX][0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>\.\.\.|\.\.|==|~=|<=|>=|<<|>>|\/\/|::|[+\-*\/%^#=<>~&|;:,.\[\](){}])""",re.VERBOSE)

created_channels={}

class Token:
    def __init__(self,kind,value): self.kind,self.value=kind,value

class LuaLexer:
    def tokenize(self,source):
        tokens=[]; p=0
        while p<len(source):
            if source.startswith("--[",p):
                e=self.long_block_end(source,p+2)
                if e is not None: p=e; continue
            if source[p]=="[":
                e=self.long_block_end(source,p)
                if e is not None:
                    tokens.append(Token("longstr",source[p:e])); p=e; continue
            m=TOKEN_RE.match(source,p)
            if not m:
                tokens.append(Token("raw",source[p])); p+=1; continue
            kind,value=m.lastgroup,m.group(0); p=m.end()
            if kind not in {"space","comment"}: tokens.append(Token(kind,value))
        return tokens
    def long_block_end(self,source,start):
        if start>=len(source) or source[start]!="[": return None
        i=start+1
        while i<len(source) and source[i]=="=": i+=1
        if i>=len(source) or source[i]!="[": return None
        closing="]"+source[start+1:i]+"]"; e=source.find(closing,i+1)
        return None if e==-1 else e+len(closing)

class NameGenerator:
    def __init__(self,seed): self.rng=random.Random(seed); self.used=set()
    def generate(self):
        while True:
            v=self.rng.choice(string.ascii_letters)+"".join(self.rng.choice(string.ascii_letters+string.digits+"_") for _ in range(self.rng.randint(7,14)))
            if v not in self.used and v not in KEYWORDS and v not in BUILTINS:
                self.used.add(v); return v

class LuaTransformer:
    def __init__(self,seed=None,strength=5,string_pool=True,number_transform=True):
        self.seed=seed if seed is not None else secrets.randbits(64)
        self.rng=random.Random(self.seed); self.names=NameGenerator(self.seed)
        self.strength=max(1,min(5,strength)); self.string_pool=string_pool; self.number_transform=number_transform
    def collect_local_names(self,t):
        names=set(); i=0
        while i<len(t):
            if t[i].kind=="ident" and t[i].value=="local":
                j=i+1
                if j<len(t) and t[j].value=="function":
                    j+=1
                    if j<len(t) and t[j].kind=="ident": names.add(t[j].value)
                    i=j; continue
                if j<len(t) and t[j].kind=="ident": names.add(t[j].value)
                while j<len(t):
                    x=t[j]
                    if x.kind=="ident" and x.value not in KEYWORDS: names.add(x.value)
                    if x.value in {"=",";"}: break
                    if x.kind=="op" and x.value not in {",","("}: break
                    j+=1
            i+=1
        return names
    def collect_parameters(self,t):
        names=set(); i=0
        while i<len(t):
            if t[i].kind=="ident" and t[i].value=="function":
                j=i+1
                while j<len(t) and t[j].value!="(": j+=1
                j+=1
                while j<len(t) and t[j].value!=")":
                    if t[j].kind=="ident" and t[j].value not in KEYWORDS: names.add(t[j].value)
                    j+=1
                i=j
            i+=1
        return names
    def mangle_identifiers(self,t):
        names=self.collect_local_names(t)|self.collect_parameters(t)
        mapping={n:self.names.generate() for n in sorted(names) if n not in KEYWORDS and n not in BUILTINS}
        out=[]
        for i,x in enumerate(t):
            if x.kind!="ident" or x.value not in mapping: out.append(x); continue
            prev=t[i-1] if i else None; nxt=t[i+1] if i+1<len(t) else None
            if (prev and prev.value==".") or (nxt and nxt.value==":"): out.append(x)
            else: out.append(Token("ident",mapping[x.value]))
        return out
    def transform_numbers(self,t):
        if not self.number_transform or self.strength<2: return t
        out=[]
        for x in t:
            if x.kind!="number": out.append(x); continue
            try:
                if x.value.lower().startswith("0x") or any(c in x.value for c in ".eE"): out.append(x); continue
                n=int(x.value)
                if n in (0,1): v=f"({n})"
                elif n==2: v="(1+1)"
                elif n==3: v="(2+1)"
                elif 3<n<256:
                    a=self.rng.randint(1,n-1); v=f"({a}+{n-a})"
                else: out.append(x); continue
                out.append(Token("raw",v))
            except ValueError: out.append(x)
        return out
    def string_pool_pass(self,t):
        if not self.string_pool or self.strength<3: return t,{}
        pool={}
        for x in t:
            if x.kind=="string" and len(x.value)>=8 and "\\" not in x.value: pool.setdefault(x.value,self.names.generate())
        out=[Token("ident",pool[x.value]) if x.kind=="string" and x.value in pool else x for x in t]
        return out,pool
    def render(self,t):
        out=[]; prev=None
        for x in t:
            if prev and prev.kind in {"ident","number"} and x.kind in {"ident","number"}: out.append(" ")
            if prev and prev.value in {"+","-"} and x.value in {"+","-"}: out.append(" ")
            out.append(x.value); prev=x
        return "".join(out)
    def transform(self,source):
        t=LuaLexer().tokenize(source); t=self.mangle_identifiers(t); t=self.transform_numbers(t); t,pool=self.string_pool_pass(t)
        body=self.render(t)
        if pool:
            body=";".join(f"local {name}={literal}" for literal,name in pool.items())+";"+body
        return body

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
        self.container=discord.ui.Container(
            discord.ui.TextDisplay("## 🔐 Obfuscation Complete"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay("Your Lua source was transformed successfully."),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay("### 📥 Protected File"),
            discord.ui.File(f"attachment://{name}"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay(f"**Output**\n`{name}`\n\n**Original Size**\n`{original:,} bytes`\n\n**Protected Size**\n`{protected:,} bytes`\n\n**SHA-256**\n`{digest[:16]}...`"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay("### ⚙️ Protection\nIdentifier mangling, number transformation, and string pooling are enabled at strength 5."),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small,visible=True),
            discord.ui.TextDisplay("The output is source transformation, not encryption. Test the generated file before production use.")
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
        protected=LuaTransformer(strength=5,string_pool=True,number_transform=True).transform(source)
        if not protected.strip(): raise RuntimeError("The obfuscator produced an empty output.")
        data=protected.encode("utf-8"); digest=sha256_text(protected); base=os.path.splitext(os.path.basename(filename))[0]
        output_name=f"{base}.obfuscated{ext}"
        output=discord.File(io.BytesIO(data),filename=output_name)
        await interaction.followup.send(view=ObfuscatePanel(output_name,len(raw),len(data),digest),files=[output])
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
