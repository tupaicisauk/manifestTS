# main.py
import os, json, time, discord, aiohttp, requests
from discord.ext import tasks
from discord import app_commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =============== KEEP-ALIVE ===============
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# =============== DISCORD SETUP ===============
intents = discord.Intents.default()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")

# =============== GOOGLE DRIVE ===============
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")
creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# =============== CONFIG (per guild) ===============
CONFIG_FILE = "bot_config.json"
def load_config():
    if os.path.exists(CONFIG_FILE):
        return json.load(open(CONFIG_FILE, "r", encoding="utf-8"))
    return {}

def save_config(cfg): json.dump(cfg, open(CONFIG_FILE,"w",encoding="utf-8"), indent=2)

config = load_config()  # {gid: {"upload_channel":..,"update_channel":..,"request_channel":..,"request_role":..}}

def ensure_guild_config(gid:int):
    gid=str(gid)
    if gid not in config:
        config[gid]={"upload_channel":None,"update_channel":None,"request_channel":None,"request_role":None}
        save_config(config)

# =============== CACHE FILES ===============
known_files = {}
ENABLE_UPLOAD_WATCH = False
def initialize_known_files():
    global known_files
    res=drive_service.files().list(q=f"'{FOLDER_ID}' in parents",fields="files(id,name,modifiedTime,size)").execute()
    items=res.get("files",[])
    known_files={f["name"]:{"id":f["id"],"mtime":f.get("modifiedTime",""),"size":f.get("size","0")} for f in items}
    print(f"Initialized cache: {len(known_files)} files.")

# =============== STEAM HELPER ===============
async def fetch_steam_info(appid:str):
    try:
        async with aiohttp.ClientSession() as s:
            url=f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with s.get(url,timeout=15) as r:
                data=await r.json()
                entry=data.get(str(appid),{})
                if entry.get("success"):
                    g=entry["data"]
                    return {
                        "name":g.get("name",f"AppID {appid}"),
                        "header":g.get("header_image"),
                        "steam":f"https://store.steampowered.com/app/{appid}",
                        "steamdb":f"https://steamdb.info/app/{appid}",
                        "release":g.get("release_date",{}).get("date","N/A"),
                        "desc":g.get("short_description","")[:500]
                    }
    except Exception: pass
    return {
        "name":f"AppID {appid}","header":None,
        "steam":f"https://store.steampowered.com/app/{appid}",
        "steamdb":f"https://steamdb.info/app/{appid}",
        "release":"N/A","desc":""
    }

# =============== /gen COMMAND ===============
@tree.command(name="gen",description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction:discord.Interaction, appid:str):
    if interaction.guild_id: ensure_guild_config(interaction.guild_id)
    await interaction.response.defer(ephemeral=False)
    start=time.perf_counter()
    try:
        q=f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        res=drive_service.files().list(q=q,fields="files(id,name,createdTime,modifiedTime,size)").execute()
        items=res.get("files",[])
        if not items:
            # not found -> kirim ke request_channel
            if interaction.guild_id:
                conf=config.get(str(interaction.guild_id),{})
                ch=bot.get_channel(conf.get("request_channel"))
                role_id=conf.get("request_role")
                if ch:
                    info=await fetch_steam_info(appid)
                    emb=discord.Embed(title="❌ Game Requested (Not Found)",color=discord.Color.red())
                    emb.add_field(name="👤 User",value=interaction.user.mention,inline=True)
                    emb.add_field(name="🆔 AppID",value=appid,inline=True)
                    emb.add_field(name="🔗 Links",value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})",inline=False)
                    if info["header"]: emb.set_thumbnail(url=info["header"])
                    emb.set_footer(text="Requested via /gen")
                    mention=f"<@&{role_id}>" if role_id else ""
                    await ch.send(content=mention if mention else None,embed=emb)
            await interaction.followup.send(f"❌ File untuk AppID `{appid}` tidak ditemukan.")
            return

        f=items[0]
        fid, fname=f["id"], f["name"]
        size_kb=int(f.get("size",0))//1024
        info=await fetch_steam_info(appid)
        elapsed=time.perf_counter()-start

        emb=discord.Embed(title="✅ Manifest Retrieved",color=discord.Color.purple())
        emb.add_field(name="🎮 Game",value=info["name"],inline=True)
        emb.add_field(name="🆔 AppID",value=appid,inline=True)
        emb.add_field(name="📦 File Size",value=f"{size_kb} KB",inline=True)
        emb.add_field(name="📅 Release Date",value=info["release"],inline=True)
        emb.add_field(name="⏱️ Time",value=f"{elapsed:.2f}s",inline=True)
        emb.add_field(name="👤 Requester",value=interaction.user.mention,inline=True)
        emb.add_field(name="🔗 Links",value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})",inline=False)
        emb.add_field(name="📥 Download",value="File hanya bisa diunduh oleh requester (lihat bawah).",inline=False)
        if info["desc"]: emb.add_field(name="ℹ️ Info",value=info["desc"],inline=False)
        if info["header"]: emb.set_image(url=info["header"])
        emb.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=emb)

        # download & send ephemeral file
        tmp=f"/tmp/{fname}"
        r=requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media",
                       headers={"Authorization":f"Bearer {creds.token}"},stream=True)
        with open(tmp,"wb") as o:
            for c in r.iter_content(1<<14):
                if c:o.write(c)
        await interaction.followup.send(content="📥 File siap diunduh:",file=discord.File(tmp,fname),ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# =============== BACKGROUND WATCH ===============
@tasks.loop(minutes=1)
async def check_new_files():
    global known_files
    if not ENABLE_UPLOAD_WATCH: return
    try:
        res=drive_service.files().list(q=f"'{FOLDER_ID}' in parents",fields="files(id,name,createdTime,modifiedTime,size)").execute()
        for f in res.get("files",[]):
            fname, fid=f["name"], f["id"]
            fsize=f.get("size","0"); mtime=f.get("modifiedTime","")
            appid=fname.replace(".zip",""); info=await fetch_steam_info(appid)

            if fname not in known_files: # Added
                known_files[fname]={"id":fid,"mtime":mtime,"size":fsize}
                for gid,conf in config.items():
                    ch=bot.get_channel(conf.get("upload_channel"))
                    if ch:
                        emb=discord.Embed(title="🆕 New Game Added",description=f"**{info['name']}** (`{appid}`)",color=discord.Color.green())
                        emb.add_field(name="📅 Upload Date",value=f.get("createdTime","")[:10],inline=True)
                        if info["header"]: emb.set_thumbnail(url=info["header"])
                        await ch.send(embed=emb)
            else: # Updated
                if known_files[fname]["size"]!=fsize:
                    known_files[fname]={"id":fid,"mtime":mtime,"size":fsize}
                    for gid,conf in config.items():
                        ch=bot.get_channel(conf.get("update_channel"))
                        if ch:
                            emb=discord.Embed(title="♻️ Game Updated",description=f"**{info['name']}** (`{appid}`)",color=discord.Color.orange())
                            emb.add_field(name="📅 Update Date",value=mtime[:10],inline=True)
                            emb.add_field(name="📦 New Size",value=f"{int(fsize)//1024} KB",inline=True)
                            if info["header"]: emb.set_thumbnail(url=info["header"])
                            await ch.send(embed=emb)
    except Exception as e: print("check_new_files error:",e)

# =============== COMMANDS ===============
@tree.command(name="notif",description="Aktif/Nonaktif monitor Drive")
async def notif(inter:discord.Interaction,mode:str):
    global ENABLE_UPLOAD_WATCH
    if mode.lower()=="on":
        ENABLE_UPLOAD_WATCH=True; initialize_known_files()
        await inter.response.send_message("🔔 Notifikasi Drive AKTIF")
    elif mode.lower()=="off":
        ENABLE_UPLOAD_WATCH=False
        await inter.response.send_message("🔕 Notifikasi Drive NONAKTIF")
    else: await inter.response.send_message("Gunakan /notif on|off")

@tree.command(name="channeluploadsetup",description="Set channel notif file baru")
async def ch_up(inter:discord.Interaction,channel:discord.TextChannel):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["upload_channel"]=channel.id; save_config(config)
    await inter.response.send_message(f"✅ Upload channel set ke {channel.mention}")

@tree.command(name="channelupdatesetup",description="Set channel notif file update")
async def ch_upd(inter:discord.Interaction,channel:discord.TextChannel):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["update_channel"]=channel.id; save_config(config)
    await inter.response.send_message(f"✅ Update channel set ke {channel.mention}")

@tree.command(name="channelrequestsetup",description="Set channel notif request not found + role mention")
async def ch_req(inter:discord.Interaction,channel:discord.TextChannel,role:discord.Role=None):
    ensure_guild_config(inter.guild_id)
    config[str(inter.guild_id)]["request_channel"]=channel.id
    config[str(inter.guild_id)]["request_role"]=role.id if role else None
    save_config(config)
    txt=f"✅ Request channel set ke {channel.mention}"
    if role: txt+=f" + mention {role.mention}"
    await inter.response.send_message(txt)

# =============== READY ===============
@bot.event
async def on_ready():
    await tree.sync(); initialize_known_files(); check_new_files.start()
    print(f"Logged in as {bot.user}")

# =============== START ===============
if __name__=="__main__":
    keep_alive(); bot.run(DISCORD_TOKEN)
