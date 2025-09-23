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
from bs4 import BeautifulSoup

# =============== KEEP-ALIVE (Render/Optional) ===============
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
FOLDER_ID = os.getenv("FOLDER_ID")

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
            "request_role": None,
            "patch_channel": None
        }
        save_config(config)

# =============== DRIVE CACHE (anti-spam) ===============
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
    """Fetch info dari Steam API, fallback SteamDB kalau gagal/DRM info."""
    info = {
        "name": f"AppID {appid}",
        "header": None,
        "steam": f"https://store.steampowered.com/app/{appid}",
        "steamdb": f"https://steamdb.info/app/{appid}",
        "release_date": "Unknown",
        "drm": None,
        "description": ""
    }
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200 and resp.content_type == "application/json":
                    data = await resp.json()
                    entry = data.get(str(appid), {})
                    if entry.get("success"):
                        g = entry["data"]
                        info.update({
                            "name": g.get("name", f"AppID {appid}"),
                            "header": g.get("header_image"),
                            "release_date": g.get("release_date", {}).get("date", "Unknown"),
                            "description": g.get("short_description", "")[:500]
                        })
    except Exception as e:
        print("fetch_steam_info error:", e)

    # Fallback: scrape DRM info dari SteamDB
    try:
        r = requests.get(f"https://steamdb.info/app/{appid}/info/", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            drm_row = soup.find("td", string="3rd-Party DRM")
            if drm_row:
                drm_val = drm_row.find_next("td").get_text(strip=True)
                info["drm"] = drm_val
    except Exception as e:
        print("SteamDB scrape error:", e)

    return info

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
            await interaction.followup.send(f"❌ File untuk AppID `{appid}` tidak ditemukan.")
            if interaction.guild_id:
                conf = config.get(str(interaction.guild_id), {})
                req_ch = conf.get("request_channel")
                if req_ch:
                    ch = bot.get_channel(req_ch)
                    if ch:
                        info = await fetch_steam_info(appid)
                        role_id = conf.get("request_role")
                        mention_txt = f"<@&{role_id}>" if role_id else ""
                        embed_nf = discord.Embed(
                            title="📌 Game Requested (Not Found)",
                            description=f"User {interaction.user.mention} request AppID **{appid}** {mention_txt}",
                            color=discord.Color.red()
                        )
                        embed_nf.add_field(name="Steam Store", value=f"[Open]({info['steam']})", inline=True)
                        embed_nf.add_field(name="SteamDB", value=f"[Open]({info['steamdb']})", inline=True)
                        if info.get("header"):
                            embed_nf.set_thumbnail(url=info["header"])
                        embed_nf.set_footer(text="Requested via /gen")
                        await ch.send(embed=embed_nf)
            return

        f = items[0]
        file_id, file_name = f["id"], f["name"]
        size_kb = int(f.get("size", 0)) // 1024
        info = await fetch_steam_info(appid)
        elapsed = time.perf_counter() - start_t

        color = discord.Color.purple() if not info.get("drm") else discord.Color.orange()
        embed = discord.Embed(title="✅ Manifest Retrieved", color=color)
        embed.add_field(name="🎮 Game", value=info["name"], inline=True)
        embed.add_field(name="🆔 AppID", value=appid, inline=True)
        embed.add_field(name="📦 File Size", value=f"{size_kb} KB", inline=True)
        embed.add_field(name="📅 Release Date", value=info.get("release_date", "Unknown"), inline=True)
        embed.add_field(name="⏱️ Time", value=f"{elapsed:.2f}s", inline=True)
        embed.add_field(name="👤 Requester", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔗 Links", value=f"[Steam]({info['steam']}) | [SteamDB]({info['steamdb']})", inline=False)
        embed.add_field(name="📥 Download", value="File hanya bisa diunduh oleh requester (lihat bawah).", inline=False)
        if info.get("description"):
            embed.add_field(name="ℹ️ Info", value=info["description"], inline=False)
        if info.get("drm"):
            embed.add_field(name="🔒 DRM", value=info["drm"], inline=False)
        if info.get("header"):
            embed.set_image(url=info["header"])
        embed.set_footer(text="Generated by TechStation Manifest")

        await interaction.followup.send(embed=embed)

        tmp_path = f"/tmp/{file_name}"
        try:
            r = requests.get(f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
                             headers={"Authorization": f"Bearer {creds.token}"}, stream=True, timeout=120)
            with open(tmp_path, "wb") as out_f:
                for chunk in r.iter_content(chunk_size=1 << 14):
                    if chunk:
                        out_f.write(chunk)
        except Exception as ex:
            await interaction.followup.send(f"⚠️ Gagal mengunduh file: {ex}", ephemeral=True)
            return
        await interaction.followup.send(content="📥 File manifest siap diunduh:", file=discord.File(tmp_path, file_name), ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error saat menjalankan /gen: {e}")

# =============== Channel Setup Commands ===============
@tree.command(name="channeluploadsetup", description="Set channel notif file baru (Added)")
async def channeluploadsetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["upload_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel Added diset ke {channel.mention}")

@tree.command(name="channelupdatesetup", description="Set channel notif file update")
async def channelupdatesetup(interaction: discord.Interaction, channel: discord.TextChannel):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["update_channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(f"✅ Channel Updated diset ke {channel.mention}")

@tree.command(name="channelrequestsetup", description="Set channel & role mention untuk request Not Found")
async def channelrequestsetup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
    ensure_guild_config(interaction.guild_id)
    config[str(interaction.guild_id)]["request_channel"] = channel.id
    config[str(interaction.guild_id)]["request_role"] = role.id if role else None
    save_config(config)
    mention_txt = role.mention if role else "None"
    await interaction.response.send_message(f"✅ Channel Request diset ke {channel.mention}, role mention: {mention_txt}")

# =============== ON READY ===============
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot logged in as {bot.user} — in {len(bot.guilds)} guilds")
    initialize_known_files()

# =============== START ===============
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
