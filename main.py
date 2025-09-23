# main.py
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
FOLDER_ID = os.getenv("FOLDER_ID")  # folder id di Google Drive

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

config = load_config()  # {guild_id_str: {"upload_channel": id_or_null, "update_channel": id_or_null}}

def ensure_guild_config(guild_id: int):
    gid = str(guild_id)
    if gid not in config:
        config[gid] = {"upload_channel": None, "update_channel": None}
        save_config(config)

# =============== DRIVE CACHE (anti-spam) ===============
# known_files: { filename: {"id": id, "mtime": "...", "size": "12345"} }
known_files = {}
ENABLE_UPLOAD_WATCH = False

def initialize_known_files():
    global known_files
    try:
        results = drive_service.files().list(
            q=f"'{FOLDER_ID}' in parents",
            fields="files(id, name, modifiedTime, size)"
        ).execute()
        items = results.get("files", [])
        known_files = {
            f["name"]: {"id": f["id"], "mtime": f.get("modifiedTime", ""), "size": f.get("size", "0")}
            for f in items
        }
        print(f"Initialized cache: {len(known_files)} files.")
    except Exception as e:
        print("Error initializing known_files:", e)

# =============== STEAM INFO HELPER ===============
async def fetch_steam_info(appid: str):
    """Best-effort fetch from store.steampowered.com/api/appdetails."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            async with session.get(url, timeout=15) as resp:
                data = await resp.json()
                entry = data.get(str(appid), {})
                if entry.get("success"):
                    g = entry["data"]
                    return {
                        "name": g.get("name", f"AppID {appid}"),
                        "header": g.get("header_image"),
                        "steam": f"https://store.steampowered.com/app/{appid}",
                        "steamdb": f"https://steamdb.info/app/{appid}",
                        # best-effort fields
                        "is_free": g.get("is_free", False),
                        "platforms": g.get("platforms", {}),
                        "categories": g.get("categories", []),
                        "developers": g.get("developers", []),
                        "publishers": g.get("publishers", []),
                        "metacritic": g.get("metacritic", {}),
                        "description": g.get("short_description", "")[:500],
                    }
    except Exception:
        pass
    return {
        "name": f"AppID {appid}",
        "header": None,
        "steam": f"https://store.steampowered.com/app/{appid}",
        "steamdb": f"https://steamdb.info/app/{appid}",
        "is_free": False,
        "platforms": {},
        "categories": [],
        "developers": [],
        "publishers": [],
        "metacritic": {},
        "description": ""
    }

# =============== /gen COMMAND ===============
@tree.command(name="gen", description="Ambil manifest (.zip) dari Google Drive via AppID")
async def gen(interaction: discord.Interaction, appid: str):
    # ensure guild config exists (for logging)
    if interaction.guild_id:
        ensure_guild_config(interaction.guild_id)

    # public embed - defer so we can show quickly
    await interaction.response.defer(ephemeral=False)

    start_t = time.perf_counter()
    try:
        q = f"name contains '{appid}.zip' and '{FOLDER_ID}' in parents"
        results = drive_service.files().list(q=q, fields="files(id,name,createdTime,modifiedTime,size)").execute()
        items = results.get("files", [])

        if not items:
            await interaction.followup.send(f"❌ File untuk AppID `{appid}` tidak ditemukan.", ephemeral=False)
            # optionally log request not found to this guild's update channel
            if interaction.guild_id:
                conf = config.get(str(interaction.guild_id), {})
                upd_ch = conf.get("update_channel")
                if upd_ch:
                    ch = bot.get_channel(upd_ch)
                    if ch:
                        info = await fetch_steam_info(appid)
                        embed_nf = discord.Embed(
                            title="📌 Game Requested (Not Found)",
                            description=f"User {interaction.user.mention} request AppID **{appid}**",
                            color=discord.Color.purple()
                        )
                        embed_nf.add_field(name="Steam Store", value=f"[Open]({info['steam']})", inline=True)
                        embed_nf.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
                        embed_nf.set_footer(text="Requested via /gen")
                        await ch.send(embed=embed_nf)
            return

        f = items[0]
        file_id = f["id"]
        file_name = f["name"]
        created = f.get("createdTime", "")
        modified = f.get("modifiedTime", "")
        size_bytes = int(f.get("size", 0))
        size_kb = size_bytes // 1024

        info = await fetch_steam_info(appid)

        elapsed = time.perf_counter() - start_t

        # Compose rich embed (public)
        embed = discord.Embed(title="✅ Manifest Retrieved", color=discord.Color.purple())
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)

        # description/info/store
        if info.get("description"):
            embed.add_field(name="💙 Game Info", value=info["description"], inline=False)
        embed.add_field(name="🛒 Store Page", value=f"[Open]({info['steam']})", inline=True)

        # supports / update / requester / time
        platform_str = ", ".join([k for k, v in info.get("platforms", {}).items() if v]) or "—"
        embed.add_field(name="🖥️ Platforms", value=platform_str, inline=True)

        # Support online / update status are not provided explicitly in API; best-effort placeholders
        supports_online = "Yes" if "Online" in (", ".join([c.get("description","") for c in info.get("categories",[])])) else "Unknown"
        embed.add_field(name="🟢 Supports Online", value=supports_online, inline=True)

        update_status = "Unknown"
        embed.add_field(name="📅 Update Status", value=update_status, inline=True)

        embed.add_field(name="⏱️ Time", value=f"{elapsed:.2f}s", inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)

        # Download Links (we'll clarify file is private below)
        embed.add_field(name="📥 Download Links", value="Private download (file dikirim di bawah hanya untuk requester).", inline=False)

        if info.get("header"):
            embed.set_image(url=info["header"])

        embed.set_footer(text="Generated by TechStation Manifest")

        # send public embed
        await interaction.followup.send(embed=embed, ephemeral=False)

        # download file to temp and send ephemeral file (only requester sees)
        tmp_path = f"/tmp/{file_name}"
        try:
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                             headers={"Authorization": f"Bearer {creds.token}"},
                             stream=True, timeout=120)
            with open(tmp_path, "wb") as out_f:
                for chunk in r.iter_content(chunk_size=1 << 14):
                    if chunk:
                        out_f.write(chunk)
        except Exception as ex:
            await interaction.followup.send(f"⚠️ Gagal mengunduh file dari Drive: {ex}", ephemeral=True)
            return

        # ephemeral followup with file (only requester can see/download)
        await interaction.followup.send(content="📥 File manifest siap diunduh :", file=discord.File(tmp_path, file_name), ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error saat menjalankan /gen: {e}", ephemeral=False)

# =============== BACKGROUND MONITOR ===============
@tasks.loop(minutes=1)
async def check_new_files():
    global known_files, ENABLE_UPLOAD_WATCH
    if not ENABLE_UPLOAD_WATCH:
        return
    try:
        results = drive_service.files().list(q=f"'{FOLDER_ID}' in parents", fields="files(id,name,createdTime,modifiedTime,size)").execute()
        items = results.get("files", [])
        for f in items:
            fname = f["name"]
            fid = f["id"]
            mtime = f.get("modifiedTime", "")
            fsize = f.get("size", "0")
            appid = fname.replace(".zip", "")
            info = await fetch_steam_info(appid)

            # New file
            if fname not in known_files:
                known_files[fname] = {"id": fid, "mtime": mtime, "size": fsize}
                # broadcast to all guilds that configured upload_channel
                for gid, conf in list(config.items()):
                    ch_id = conf.get("upload_channel")
                    if ch_id:
                        ch = bot.get_channel(ch_id)
                        if ch:
                            embed = discord.Embed(title="🆕 New Game Added", description=f"**{info['name']}** (`{appid}`) ditambahkan.", color=discord.Color.purple())
                            embed.add_field(name="Upload Date", value=f.get("createdTime","")[:10], inline=True)
                            if info.get("header"):
                                embed.set_thumbnail(url=info.get("header"))
                            await ch.send(embed=embed)

            # existing -> check size changed
            else:
                if known_files[fname]["size"] != fsize:
                    known_files[fname] = {"id": fid, "mtime": mtime, "size": fsize}
                    for gid, conf in list(config.items()):
                        ch_id = conf.get("update_channel")
                        if ch_id:
                            ch = bot.get_channel(ch_id)
                            if ch:
                                embed = discord.Embed(title="♻️ Game Updated", description=f"**{info['name']}** (`{appid}`) diperbarui (size berubah).", color=discord.Color.purple())
                                embed.add_field(name="Update Date", value=mtime[:10], inline=True)
                                embed.add_field(name="New Size", value=f"{int(fsize)//1024} KB", inline=True)
                                if info.get("header"):
                                    embed.set_thumbnail(url=info.get("header"))
                                await ch.send(embed=embed)
    except Exception as e:
        print("check_new_files error:", e)

# =============== /notif on|off ===============
@tree.command(name="notif", description="Aktif/Nonaktif monitor Drive (added/updated)")
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
@tree.command(name="channeluploadsetup", description="Set channel untuk notif file baru (Added)")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Added** diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="Set channel untuk notif file update (size berubah)")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel **Updated** diset ke {channel.mention}")

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
