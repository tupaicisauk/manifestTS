import os
import json
import time
import discord
from discord.ext import tasks
from discord import app_commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build
import aiohttp
import requests

# =============== KEEP-ALIVE (OPTIONAL) ===============
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# =============== DISCORD SETUP ===============
intents = discord.Intents.default()
intents.guilds = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FOLDER_ID = os.getenv("FOLDER_ID")  # Google Drive folder ID

# =============== GOOGLE DRIVE SETUP ===============
SCOPES = ["https://www.googleapis.com/auth/drive"]
SERVICE_ACCOUNT_INFO = os.getenv("GDRIVE_CREDENTIALS")
if not SERVICE_ACCOUNT_INFO:
    raise ValueError("GDRIVE_CREDENTIALS environment variable is required")

creds = service_account.Credentials.from_service_account_info(
    json.loads(SERVICE_ACCOUNT_INFO), scopes=SCOPES
)
drive_service = build("drive", "v3", credentials=creds)

# =============== CONFIG PERSISTENCE (per-guild) ===============
CONFIG_FILE = "bot_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

config = load_config()

def ensure_guild_config(guild_id: int):
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {
            "upload_channel": None,
            "update_channel": None,
            "request_channel": None,
            "request_role": None
        }
        save_config(config)

# =============== DRIVE CACHE (anti-spam) ===============
known_files = {}
ENABLE_UPLOAD_WATCH = False

# tambahan anti-spam
NOTIFIED_FILE = "notified.json"

def load_notified():
    if os.path.exists(NOTIFIED_FILE):
        with open(NOTIFIED_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_notified(data):
    with open(NOTIFIED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(data), f, indent=2)

notified_files = load_notified()

def initialize_known_files():
    global known_files
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute()
        items = results.get("files", [])
        known_files = {
            f["name"]: {
                "id": f["id"],
                "mtime": f.get("modifiedTime", ""),
                "ctime": f.get("createdTime", ""),
                "size": f.get("size", "0")
            }
            for f in items
        }
        print(f"Initialized cache: {len(known_files)} files.")
    except Exception as e:
        print("Error initializing known_files:", e)

def count_manifests_in_cache(appid: str):
    prefix = f"{appid}"
    count = 0
    for name in known_files.keys():
        if name.startswith(prefix) or f"{prefix}.zip" in name:
            count += 1
    return count

# =============== STEAM INFO HELPER ===============
async def fetch_steam_info(appid: str):
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
                entry = data.get(str(appid), {})
                if entry.get("success"):
                    g = entry["data"]
                    devs = g.get("developers") or []
                    dev = devs[0] if devs else None
                    release_date = g.get("release_date", {}).get("date") or None
                    return {
                        "name": g.get("name", f"AppID {appid}"),
                        "header": g.get("header_image"),
                        "steam": f"https://store.steampowered.com/app/{appid}",
                        "steamdb": f"https://steamdb.info/app/{appid}",
                        "release_date": release_date,
                        "developer": dev,
                        "description": g.get("short_description", "")[:800]
                    }
    except Exception:
        pass
    return {
        "name": f"AppID {appid}",
        "header": None,
        "steam": f"https://store.steampowered.com/app/{appid}",
        "steamdb": f"https://steamdb.info/app/{appid}",
        "release_date": None,
        "developer": None,
        "description": ""
    }

# =============== /gen COMMAND ===============
@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    if interaction.guild_id:
        ensure_guild_config(interaction.guild_id)

    await interaction.response.defer(ephemeral=False)
    start_t = time.perf_counter()

    try:
        q = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(q=q, fields="files(id,name,createdTime,modifiedTime,size)").execute()
        items = results.get("files", [])

        if not items:
            info = await fetch_steam_info(appid)
            embed_nf = discord.Embed(
                title="🚨 Game Requested (Not Found)",
                description=f"User {interaction.user.mention} request AppID **{appid}**",
                color=discord.Color.red()
            )
            embed_nf.add_field(name="🔗 Steam", value=f"[Open]({info['steam']})", inline=True)
            embed_nf.add_field(name="📊 SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
            if info.get("developer"): embed_nf.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
            if info.get("release_date"): embed_nf.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
            if info.get("header"): embed_nf.set_image(url=info["header"])
            embed_nf.timestamp = discord.utils.utcnow()
            embed_nf.set_footer(text="Requested via /gen")

            await interaction.followup.send(embed=embed_nf, ephemeral=False)

            conf = config.get(str(interaction.guild_id), {})
            req_ch = conf.get("request_channel")
            req_role = conf.get("request_role")
            if req_ch:
                ch = bot.get_channel(req_ch)
                if ch:
                    mention_txt = f"<@&{req_role}>" if req_role else None
                    if mention_txt:
                        await ch.send(content=mention_txt, embed=embed_nf)
                    else:
                        await ch.send(embed=embed_nf)
            return

        f = items[0]
        file_id, file_name = f["id"], f["name"]
        created = f.get("createdTime", "")
        modified = f.get("modifiedTime", "")
        size_kb = int(f.get("size", 0)) // 1024

        info = await fetch_steam_info(appid)
        elapsed = time.perf_counter() - start_t
        release_date = info.get("release_date") or created[:10] or modified[:10]

        embed = discord.Embed(title="✅ Manifest Retrieved", color=discord.Color.purple())
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)
        embed.add_field(name="📅 Release Date", value=release_date, inline=True)
        if info.get("developer"): embed.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
        embed.add_field(name="⏱️ Time", value=f"{elapsed:.2f}s", inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        embed.add_field(name="📥 Download", value="File hanya bisa diunduh oleh requester (lihat bawah).", inline=False)
        if info.get("description"): embed.add_field(name="ℹ️ Info", value=info["description"], inline=False)
        if info.get("header"): embed.set_image(url=info["header"])
        embed.timestamp = discord.utils.utcnow()
        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed, ephemeral=False)

        tmp_path = f"/tmp/{file_name}"
        r = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
            headers={"Authorization": f"Bearer {creds.token}"},
            stream=True, timeout=120
        )
        with open(tmp_path, "wb") as out_f:
            for chunk in r.iter_content(chunk_size=1 << 14):
                if chunk:
                    out_f.write(chunk)

        await interaction.followup.send(
            content="📥 File manifest siap diunduh:",
            file=discord.File(tmp_path, file_name),
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error saat menjalankan /gen: {e}", ephemeral=False)

# =============== BACKGROUND MONITOR ===============
@tasks.loop(minutes=1)
async def check_new_files():
    global known_files, ENABLE_UPLOAD_WATCH, notified_files
    if not ENABLE_UPLOAD_WATCH:
        return
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id,name,createdTime,modifiedTime,size)"
        ).execute()
        items = results.get("files", [])

        for f in items:
            fname = f["name"]
            fid = f["id"]
            mtime = f.get("modifiedTime", "")
            ctime = f.get("createdTime", "")
            fsize = f.get("size", "0")
            appid = fname.replace(".zip", "")
            info = await fetch_steam_info(appid)

            # NEW
            if fname not in known_files:
                known_files[fname] = {"id": fid, "mtime": mtime, "ctime": ctime, "size": fsize}

                # anti-spam hanya untuk Added
                if fname in notified_files:
                    continue
                notified_files.add(fname)
                save_notified(notified_files)

                total_files = count_manifests_in_cache(appid)
                for gid, conf in list(config.items()):
                    ch_id = conf.get("upload_channel")
                    if ch_id and (ch := bot.get_channel(ch_id)):
                        embed = discord.Embed(
                            title=f"🆕 New Game Added — {info['name']} ({appid})",
                            description=f"**{info['name']}** (`{appid}`) ditambahkan ke drive.",
                            color=discord.Color.blue()
                        )
                        if info.get("developer"): embed.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
                        if info.get("release_date"): embed.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
                        embed.add_field(name="📦 Manifest Files", value=str(total_files), inline=True)
                        embed.add_field(name="📅 Upload Date", value=ctime[:10], inline=True)
                        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
                        if info.get("header"): embed.set_image(url=info["header"])
                        embed.timestamp = discord.utils.utcnow()
                        embed.set_footer(text="Reported by TechStation Manifest")
                        await ch.send(embed=embed)

            # UPDATED
            else:
                if (known_files[fname]["size"] != fsize) or (known_files[fname]["mtime"] != mtime):
                    known_files[fname] = {"id": fid, "mtime": mtime, "ctime": ctime, "size": fsize}
                    total_files = count_manifests_in_cache(appid)
                    for gid, conf in list(config.items()):
                        ch_id = conf.get("update_channel")
                        if ch_id and (ch := bot.get_channel(ch_id)):
                            embed = discord.Embed(
                                title=f"♻️ Game Updated — {info['name']} ({appid})",
                                description=f"**{info['name']}** (`{appid}`) diperbarui (file size berubah).",
                                color=discord.Color.orange()
                            )
                            if info.get("developer"): embed.add_field(name="👨‍💼 Developer", value=info["developer"], inline=True)
                            if info.get("release_date"): embed.add_field(name="📅 Release Date", value=info["release_date"], inline=True)
                            embed.add_field(name="📦 Manifest Files", value=str(total_files), inline=True)
                            embed.add_field(name="📅 Upload Date", value=known_files[fname].get("ctime", "")[:10], inline=True)
                            embed.add_field(name="🔁 Update Date", value=mtime[:10], inline=True)
                            embed.add_field(name="📦 New Size", value=f"{int(fsize)//1024} KB", inline=True)
                            embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
                            if info.get("header"): embed.set_image(url=info["header"])
                            embed.timestamp = discord.utils.utcnow()
                            embed.set_footer(text="Reported by TechStation Manifest")
                            await ch.send(embed=embed)

    except Exception as e:
        print("check_new_files error:", e)

# =============== /notif on|off ===============
@tree.command(name="notif", description="🔔 Aktif/Nonaktif monitor Drive (added/updated)")
async def notif(interaction: discord.Interaction, mode: str):
    global ENABLE_UPLOAD_WATCH
    m = mode.lower()
    if m == "on":
        ENABLE_UPLOAD_WATCH = True
        initialize_known_files()
        await interaction.response.send_message("🔔 Notifikasi Drive: **AKTIF**")
    elif m == "off":
        ENABLE_UPLOAD_WATCH = False
        await interaction.response.send_message("🔕 Notifikasi Drive: **NONAKTIF**")
    else:
        await interaction.response.send_message("Gunakan `/notif on` atau `/notif off`")

# =============== channel setup (per guild) ===============
@tree.command(name="channeluploadsetup", description="📌 Set channel untuk notif file baru (Added)")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel Added diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="📌 Set channel untuk notif file update")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel Updated diset ke {channel.mention}")

@tree.command(name="channelrequestsetup", description="📌 Set channel + role mention untuk request (Not Found)")
async def channelrequestsetup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["request_channel"] = channel.id
    config[str(interaction.guild_id)]["request_role"] = role.id if role else None
    save_config(config)
    txt = f"✅ Channel Request diset ke {channel.mention}"
    if role:
        txt += f" dan role mention diset ke {role.mention}"
    await interaction.response.send_message(txt)

# =============== ON READY ===============
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot logged in as {bot.user} — in {len(bot.guilds)} guilds")
    initialize_known_files()
    check_new_files.start()

# =============== START ===============
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
